"""Config flow for the BonnVoyage integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    LocationSelector,
    LocationSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.location import distance
import voluptuous as vol

from .api.bcp import STATIC_FACILITIES, BcpClient
from .api.bonn_traffic import BonnTrafficClient
from .api.exceptions import (
    ApiAuthError,
    ApiRateLimitError,
    ApiRouteNotFoundError,
    BonnVoyageApiError,
)
from .api.geocode import NominatimClient
from .api.models import (
    ParkingFacility,
    RoutePoint,
    RouteRequest,
    TrafficSegment,
    TransitStop,
)
from .api.routing import PROVIDERS, RouteProvider, create_provider
from .api.transitous import TransitousClient
from .const import (
    CONF_ADD_VIA,
    CONF_API_KEY,
    CONF_AREA,
    CONF_DELAY_ALERT_THRESHOLD,
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATION,
    CONF_DIRECTION,
    CONF_FACILITY_ID,
    CONF_LOCATION,
    CONF_NAME,
    CONF_PARKINGS,
    CONF_PROVIDER,
    CONF_QUERY,
    CONF_SEGMENT_ID,
    CONF_START,
    CONF_STOP_ID,
    CONF_STREET_NAME,
    CONF_VIA,
    DEFAULT_DELAY_ALERT_MIN,
    DEFAULT_DELAY_THRESHOLD_MIN,
    DEFAULT_RADIUS_M,
    DOMAIN,
    FLOW_STALE_MAX_AGE,
    MAX_SEGMENT_PICK_DISTANCE_M,
    MIN_ROUTE_DISTANCE_M,
    PICK_ON_MAP,
    PRESELECT_MAX,
    PRESELECT_RADIUS_M,
    SUBENTRY_TYPE_PARKING,
    SUBENTRY_TYPE_ROUTE,
    SUBENTRY_TYPE_SEGMENT,
    SUBENTRY_TYPE_STOP,
)
from .segment_names import (
    SegmentNameCache,
    point_to_segment_m,
    segment_direction,
    segment_length_km,
)

_LOGGER = logging.getLogger(__name__)

SEGMENT_REPICK = "__repick__"
SEGMENT_CANDIDATES = 3
GEOCODE_BUDGET_S = 4

PROVIDER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROVIDER, default="tomtom"): SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=name, label=name.title())
                    for name in PROVIDERS
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

API_KEY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    }
)


async def _async_validate_provider(
    hass: HomeAssistant, provider_name: str, api_key: str
) -> str | None:
    """Validate a provider key; return an error key or None."""
    provider = create_provider(provider_name, async_get_clientsession(hass), api_key)
    try:
        await provider.async_validate_key()
    except ApiAuthError:
        return "invalid_auth"
    except ApiRateLimitError:
        return "rate_limited"
    except BonnVoyageApiError:
        return "cannot_connect"
    return None


async def _async_fetch_fresh_facilities(
    flow: ConfigFlow | ConfigSubentryFlow,
) -> dict[str, ParkingFacility]:
    """Fetch selectable facilities, hiding dead feed entries.

    Entries whose feed timestamp is older than FLOW_STALE_MAX_AGE (e.g. the
    "karstadt" garage, frozen since 2018) are not offered for configuration.
    """
    client = BcpClient(async_get_clientsession(flow.hass))
    statuses = await client.async_fetch_statuses()
    now = dt_util.utcnow()
    return {
        facility_id: STATIC_FACILITIES.get(
            facility_id, ParkingFacility(facility_id, facility_id, None, None)
        )
        for facility_id, status in statuses.items()
        if status.updated_at is not None
        and now - status.updated_at < FLOW_STALE_MAX_AGE
    }


def _distance_km(
    flow: ConfigFlow | ConfigSubentryFlow, facility: ParkingFacility
) -> float | None:
    """Distance from the Home Assistant home location in kilometers."""
    if facility.latitude is None or facility.longitude is None:
        return None
    meters = distance(
        flow.hass.config.latitude,
        flow.hass.config.longitude,
        facility.latitude,
        facility.longitude,
    )
    return None if meters is None else meters / 1000


def _facility_options(
    flow: ConfigFlow | ConfigSubentryFlow, facilities: dict[str, ParkingFacility]
) -> list[SelectOptionDict]:
    """Build select options sorted by distance from home."""
    with_distance = [
        (facility, _distance_km(flow, facility)) for facility in facilities.values()
    ]
    with_distance.sort(key=lambda item: (item[1] is None, item[1] or 0.0))
    return [
        SelectOptionDict(
            value=facility.facility_id,
            label=(
                facility.name if dist is None else f"{facility.name} ({dist:.1f} km)"
            ),
        )
        for facility, dist in with_distance
    ]


def _zone_options(hass: HomeAssistant) -> list[SelectOptionDict]:
    """Zone entities plus the pick-on-map option (zone.home first)."""
    zones = sorted(
        hass.states.async_all("zone"),
        key=lambda state: (state.entity_id != "zone.home", state.name),
    )
    options = [
        SelectOptionDict(value=state.entity_id, label=state.name)
        for state in zones
        if "latitude" in state.attributes
    ]
    options.append(SelectOptionDict(value=PICK_ON_MAP, label="Auf Karte wählen …"))
    return options


def _location_schema(hass: HomeAssistant, center: RoutePoint | None) -> vol.Schema:
    """Map picker schema with a sensible default center."""
    default = {
        "latitude": center.latitude if center else hass.config.latitude,
        "longitude": center.longitude if center else hass.config.longitude,
    }
    return vol.Schema(
        {vol.Required(CONF_LOCATION, default=default): LocationSelector()}
    )


class BonnVoyageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial BonnVoyage setup."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._area: dict[str, Any] | None = None
        self._facilities: dict[str, ParkingFacility] = {}
        self._selected_parkings: list[str] = []
        self._provider_data: dict[str, str] = {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentry flows supported by this integration."""
        return {
            SUBENTRY_TYPE_PARKING: ParkingSubentryFlowHandler,
            SUBENTRY_TYPE_ROUTE: RouteSubentryFlowHandler,
            SUBENTRY_TYPE_SEGMENT: SegmentSubentryFlowHandler,
            SUBENTRY_TYPE_STOP: StopSubentryFlowHandler,
        }

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return BonnVoyageOptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step: area for traffic messages."""
        if user_input is not None:
            self._area = user_input[CONF_AREA]
            return await self.async_step_parking()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_AREA,
                        default={
                            "latitude": self.hass.config.latitude,
                            "longitude": self.hass.config.longitude,
                            "radius": DEFAULT_RADIUS_M,
                        },
                    ): LocationSelector(LocationSelectorConfig(radius=True))
                }
            ),
        )

    async def async_step_parking(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step: select parking facilities."""
        if user_input is not None:
            self._selected_parkings = user_input[CONF_PARKINGS]
            return await self.async_step_routes_menu()

        if not self._facilities:
            try:
                self._facilities = await _async_fetch_fresh_facilities(self)
            except BonnVoyageApiError as err:
                _LOGGER.warning("BCP feed unavailable during setup: %s", err)
                return await self.async_step_parking_unavailable()
            if not self._facilities:
                return await self.async_step_parking_unavailable()

        preselected = [
            facility.facility_id
            for facility, dist in sorted(
                (
                    (facility, _distance_km(self, facility))
                    for facility in self._facilities.values()
                ),
                key=lambda item: (item[1] is None, item[1] or 0.0),
            )
            if dist is not None and dist * 1000 <= PRESELECT_RADIUS_M
        ][:PRESELECT_MAX]

        return self.async_show_form(
            step_id="parking",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PARKINGS, default=preselected): SelectSelector(
                        SelectSelectorConfig(
                            options=_facility_options(self, self._facilities),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_parking_unavailable(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer retry or continuing without parking facilities."""
        return self.async_show_menu(
            step_id="parking_unavailable",
            menu_options=["parking", "routes_menu"],
        )

    async def async_step_routes_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Menu: set up a travel time provider now or finish without."""
        return self.async_show_menu(
            step_id="routes_menu",
            menu_options=["provider", "finish"],
        )

    async def async_step_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional travel time provider with key validation."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await _async_validate_provider(
                self.hass, user_input[CONF_PROVIDER], user_input[CONF_API_KEY]
            )
            if error is None:
                self._provider_data = {
                    CONF_PROVIDER: user_input[CONF_PROVIDER],
                    CONF_API_KEY: user_input[CONF_API_KEY],
                }
                return await self.async_step_finish()
            errors["base"] = error

        return self.async_show_form(
            step_id="provider", data_schema=PROVIDER_SCHEMA, errors=errors
        )

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the entry with parking subentries."""
        return self.async_create_entry(
            title="BonnVoyage",
            data={CONF_AREA: self._area, **self._provider_data},
            subentries=[
                ConfigSubentryData(
                    data={CONF_FACILITY_ID: facility_id},
                    subentry_type=SUBENTRY_TYPE_PARKING,
                    title=self._facilities[facility_id].name,
                    unique_id=facility_id,
                )
                for facility_id in self._selected_parkings
            ],
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for the routing provider key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new API key and validate it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            error = await _async_validate_provider(
                self.hass,
                entry.data.get(CONF_PROVIDER, "tomtom"),
                user_input[CONF_API_KEY],
            )
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: user_input[CONF_API_KEY]}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=API_KEY_SCHEMA, errors=errors
        )


class ParkingSubentryFlowHandler(ConfigSubentryFlow):
    """Add a single parking facility after initial setup."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._remaining: dict[str, ParkingFacility] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Select one of the not yet configured facilities."""
        entry = self._get_entry()

        if user_input is not None:
            facility_id = user_input[CONF_FACILITY_ID]
            return self.async_create_entry(
                title=self._remaining[facility_id].name,
                data={CONF_FACILITY_ID: facility_id},
                unique_id=facility_id,
            )

        configured = {
            subentry.unique_id
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_PARKING
        }
        try:
            facilities = await _async_fetch_fresh_facilities(self)
        except BonnVoyageApiError:
            return self.async_abort(reason="bcp_unavailable")

        if not facilities:
            return self.async_abort(reason="no_parkings")
        self._remaining = {
            facility_id: facility
            for facility_id, facility in facilities.items()
            if facility_id not in configured
        }
        if not self._remaining:
            return self.async_abort(reason="all_configured")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_FACILITY_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=_facility_options(self, self._remaining),
                            multiple=False,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )


class RouteSubentryFlowHandler(ConfigSubentryFlow):
    """Add a route via the multi-step wizard (zone or map per endpoint)."""

    def __init__(self) -> None:
        """Initialize the wizard state."""
        super().__init__()
        self._provider_data: dict[str, str] = {}
        self._start: RoutePoint | None = None
        self._start_label = ""
        self._destination: RoutePoint | None = None
        self._destination_label = ""
        self._via: RoutePoint | None = None
        self._pending_settings: dict[str, Any] = {}

    def _configured_provider(self) -> tuple[str, str] | None:
        """Provider from the parent entry or the inline step."""
        if self._provider_data:
            return (
                self._provider_data[CONF_PROVIDER],
                self._provider_data[CONF_API_KEY],
            )
        data = self._get_entry().data
        if data.get(CONF_PROVIDER) and data.get(CONF_API_KEY):
            return (data[CONF_PROVIDER], data[CONF_API_KEY])
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Entry point: inline provider setup if no key exists (no dead end)."""
        if self._configured_provider() is None:
            return await self.async_step_provider()
        return await self.async_step_start()

    async def async_step_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Inline provider setup; the key is stored on final completion."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await _async_validate_provider(
                self.hass, user_input[CONF_PROVIDER], user_input[CONF_API_KEY]
            )
            if error is None:
                self._provider_data = {
                    CONF_PROVIDER: user_input[CONF_PROVIDER],
                    CONF_API_KEY: user_input[CONF_API_KEY],
                }
                return await self.async_step_start()
            errors["base"] = error

        return self.async_show_form(
            step_id="provider", data_schema=PROVIDER_SCHEMA, errors=errors
        )

    async def async_step_start(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Start point: zone or map."""
        if user_input is not None:
            choice = user_input[CONF_START]
            state = self.hass.states.get(choice)
            if choice == PICK_ON_MAP or state is None:
                return await self.async_step_start_map()
            self._start = RoutePoint(
                state.attributes["latitude"], state.attributes["longitude"]
            )
            self._start_label = state.name
            return await self.async_step_dest()

        return self.async_show_form(
            step_id="start",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_START, default="zone.home"): SelectSelector(
                        SelectSelectorConfig(
                            options=_zone_options(self.hass),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_start_map(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the start point on the map."""
        if user_input is not None:
            location = user_input[CONF_LOCATION]
            self._start = RoutePoint(location["latitude"], location["longitude"])
            self._start_label = (
                f"{self._start.latitude:.3f}, {self._start.longitude:.3f}"
            )
            return await self.async_step_dest()
        return self.async_show_form(
            step_id="start_map", data_schema=_location_schema(self.hass, None)
        )

    async def async_step_dest(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Destination: zone or map."""
        if user_input is not None:
            choice = user_input[CONF_DESTINATION]
            state = self.hass.states.get(choice)
            if choice == PICK_ON_MAP or state is None:
                return await self.async_step_dest_map()
            self._destination = RoutePoint(
                state.attributes["latitude"], state.attributes["longitude"]
            )
            self._destination_label = state.name
            return await self.async_step_settings()

        return self.async_show_form(
            step_id="dest",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DESTINATION, default=PICK_ON_MAP): SelectSelector(
                        SelectSelectorConfig(
                            options=_zone_options(self.hass),
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_dest_map(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the destination on the map, centered on the start."""
        if user_input is not None:
            location = user_input[CONF_LOCATION]
            self._destination = RoutePoint(location["latitude"], location["longitude"])
            self._destination_label = (
                f"{self._destination.latitude:.3f}, {self._destination.longitude:.3f}"
            )
            return await self.async_step_settings()
        return self.async_show_form(
            step_id="dest_map", data_schema=_location_schema(self.hass, self._start)
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Name, threshold and optional via; validates with a test route."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._pending_settings = user_input
            if user_input.get(CONF_ADD_VIA):
                return await self.async_step_via_map()
            return await self._async_validate_and_create(errors)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_NAME,
                    default=f"{self._start_label} → {self._destination_label}",
                ): str,
                vol.Required(
                    CONF_DELAY_THRESHOLD, default=DEFAULT_DELAY_THRESHOLD_MIN
                ): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=1,
                            max=120,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional(CONF_ADD_VIA, default=False): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    async def async_step_via_map(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick the optional via point on the map."""
        if user_input is not None:
            location = user_input[CONF_LOCATION]
            self._via = RoutePoint(location["latitude"], location["longitude"])
            return await self._async_validate_and_create({})
        return self.async_show_form(
            step_id="via_map", data_schema=_location_schema(self.hass, self._start)
        )

    async def _async_validate_and_create(
        self, errors: dict[str, str]
    ) -> SubentryFlowResult:
        """Run all validations, then create the subentry."""
        entry = self._get_entry()
        name = self._pending_settings[CONF_NAME].strip()
        assert self._start is not None and self._destination is not None

        if any(
            subentry.title == name
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_ROUTE
        ):
            errors["base"] = "name_exists"
        else:
            meters = distance(
                self._start.latitude,
                self._start.longitude,
                self._destination.latitude,
                self._destination.longitude,
            )
            if meters is not None and meters < MIN_ROUTE_DISTANCE_M:
                errors["base"] = "points_too_close"
            else:
                errors_key = await self._async_test_route()
                if errors_key:
                    errors["base"] = errors_key

        if errors:
            return await self._async_show_settings_with_errors(errors)

        if self._provider_data:
            # Persist the inline-entered provider on the parent entry as the
            # final action (triggers one reload together with the new subentry).
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, **self._provider_data}
            )

        return self.async_create_entry(
            title=name,
            data={
                CONF_START: {
                    "latitude": self._start.latitude,
                    "longitude": self._start.longitude,
                },
                CONF_DESTINATION: {
                    "latitude": self._destination.latitude,
                    "longitude": self._destination.longitude,
                },
                CONF_VIA: (
                    {
                        "latitude": self._via.latitude,
                        "longitude": self._via.longitude,
                    }
                    if self._via
                    else None
                ),
                CONF_DELAY_THRESHOLD: self._pending_settings[CONF_DELAY_THRESHOLD],
            },
        )

    async def _async_show_settings_with_errors(
        self, errors: dict[str, str]
    ) -> SubentryFlowResult:
        """Re-show the settings form with errors and preserved values."""
        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_DELAY_THRESHOLD): vol.All(
                        NumberSelector(
                            NumberSelectorConfig(
                                min=1,
                                max=120,
                                step=1,
                                mode=NumberSelectorMode.BOX,
                                unit_of_measurement="min",
                            )
                        ),
                        vol.Coerce(int),
                    ),
                    vol.Optional(CONF_ADD_VIA, default=False): BooleanSelector(),
                }
            ),
            self._pending_settings,
        )
        return self.async_show_form(
            step_id="settings", data_schema=schema, errors=errors
        )

    async def _async_test_route(self) -> str | None:
        """Calculate a probe route; return an error key or None."""
        provider_conf = self._configured_provider()
        assert provider_conf is not None
        assert self._start is not None and self._destination is not None
        provider: RouteProvider = create_provider(
            provider_conf[0], async_get_clientsession(self.hass), provider_conf[1]
        )
        try:
            await provider.async_route(
                RouteRequest(
                    start=self._start, destination=self._destination, via=self._via
                )
            )
        except ApiRouteNotFoundError:
            return "route_not_found"
        except ApiAuthError:
            return "invalid_auth"
        except ApiRateLimitError:
            return "rate_limited"
        except BonnVoyageApiError:
            return "cannot_connect"
        return None


class SegmentSubentryFlowHandler(ConfigSubentryFlow):
    """Watch a city traffic segment: point -> named candidates -> name."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._segments: dict[str, TrafficSegment] = {}
        self._candidates: list[SegmentCandidate] = []
        self._picked: SegmentCandidate | None = None
        self._last_point: dict[str, float] | None = None
        self._geocoder_failed = False

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 1: pick a point near the street of interest."""
        errors: dict[str, str] = {}

        if not self._segments:
            client = BonnTrafficClient(async_get_clientsession(self.hass))
            try:
                self._segments = await client.async_fetch_segments()
            except BonnVoyageApiError:
                return self.async_abort(reason="segments_unavailable")

        if user_input is not None:
            self._last_point = user_input[CONF_LOCATION]
            nearest = _nearest_segments(
                self._segments,
                self._last_point["latitude"],
                self._last_point["longitude"],
            )
            if not nearest:
                errors["base"] = "no_segment_nearby"
            else:
                await self._async_resolve_candidates(nearest)
                return await self.async_step_select()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LOCATION,
                    default=self._last_point
                    or {
                        "latitude": self.hass.config.latitude,
                        "longitude": self.hass.config.longitude,
                    },
                ): LocationSelector()
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def _async_resolve_candidates(
        self, nearest: list[tuple[TrafficSegment, float]]
    ) -> None:
        """Resolve street names (cache first, then Nominatim, 4 s budget).

        Naming must never block the selection: failures fall back to
        "Abschnitt #<id>" labels.
        """
        cache = SegmentNameCache(self.hass)
        geocoder = NominatimClient(async_get_clientsession(self.hass))
        self._geocoder_failed = False
        candidates: list[SegmentCandidate] = []
        try:
            async with asyncio.timeout(GEOCODE_BUDGET_S):
                for segment, meters in nearest:
                    street = await cache.async_get(segment.segment_id)
                    if street is None:
                        street = await geocoder.async_reverse_street(
                            segment.midpoint[0], segment.midpoint[1]
                        )
                        if street:
                            await cache.async_set(segment.segment_id, street)
                        else:
                            self._geocoder_failed = True
                    candidates.append(
                        SegmentCandidate(
                            segment, meters, street, segment_direction(segment)
                        )
                    )
        except TimeoutError:
            self._geocoder_failed = True
            for segment, meters in nearest[len(candidates) :]:
                candidates.append(
                    SegmentCandidate(segment, meters, None, segment_direction(segment))
                )
        self._candidates = candidates

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 2: explicitly confirm one of the named candidates."""
        entry = self._get_entry()
        errors: dict[str, str] = {}
        configured = {
            subentry.unique_id
            for subentry in entry.subentries.values()
            if subentry.subentry_type == SUBENTRY_TYPE_SEGMENT
        }

        if user_input is not None:
            choice = user_input[CONF_SEGMENT_ID]
            if choice == SEGMENT_REPICK:
                return await self.async_step_user()
            if choice in configured:
                errors["base"] = "already_configured"
            else:
                self._picked = next(
                    candidate
                    for candidate in self._candidates
                    if candidate.segment.segment_id == choice
                )
                return await self.async_step_name()

        options = [
            SelectOptionDict(
                value=candidate.segment.segment_id,
                label=candidate.label(
                    watched=candidate.segment.segment_id in configured
                ),
            )
            for candidate in self._candidates
        ]
        options.append(
            SelectOptionDict(
                value=SEGMENT_REPICK,
                label="Keiner davon - Punkt auf der Karte neu setzen",
            )
        )
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SEGMENT_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "geocoder_note": (
                    " Hinweis: Straßennamen konnten gerade nicht ermittelt "
                    "werden - die Auswahl funktioniert trotzdem."
                    if self._geocoder_failed
                    else ""
                )
            },
        )

    async def async_step_name(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Step 3: name the watched segment (prefilled) and finish."""
        entry = self._get_entry()
        errors: dict[str, str] = {}
        assert self._picked is not None
        picked = self._picked

        if user_input is not None:
            name = user_input[CONF_NAME].strip() or picked.default_name()
            if any(
                subentry.title == name
                for subentry in entry.subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE_SEGMENT
            ):
                errors["base"] = "name_exists"
            else:
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_SEGMENT_ID: picked.segment.segment_id,
                        CONF_STREET_NAME: picked.street,
                        CONF_DIRECTION: picked.direction,
                    },
                    unique_id=picked.segment.segment_id,
                )

        schema = vol.Schema(
            {vol.Required(CONF_NAME, default=picked.default_name()): str}
        )
        return self.async_show_form(
            step_id="name",
            data_schema=schema,
            errors=errors,
            description_placeholders={"segment_label": picked.label(watched=False)},
        )


@dataclass
class SegmentCandidate:
    """One selectable segment with resolved metadata."""

    segment: TrafficSegment
    meters: float
    street: str | None
    direction: str

    def default_name(self) -> str:
        """Prefilled device name."""
        if self.street:
            return f"{self.street} ({self.direction})"
        return f"Abschnitt #{self.segment.segment_id}"

    def label(self, *, watched: bool) -> str:
        """Selector label with recognition anchors."""
        name = self.street or f"Abschnitt #{self.segment.segment_id}"
        parts = [
            f"{name} (Richtung {self.direction})",
            f"{self.meters:.0f} m entfernt",
            f"{segment_length_km(self.segment)} km langer Abschnitt",
        ]
        if self.segment.raw_status:
            status = self.segment.raw_status
            if self.segment.speed_kmh is not None:
                status = f"{status} ({self.segment.speed_kmh} km/h)"
            parts.append(f"aktuell: {status}")
        label = f"{parts[0]} - " + ", ".join(parts[1:])
        if watched:
            label += " - wird bereits beobachtet"
        return label


def _nearest_segments(
    segments: dict[str, TrafficSegment], latitude: float, longitude: float
) -> list[tuple[TrafficSegment, float]]:
    """Closest segments by point-to-polyline distance, within the pick radius."""
    ranked = sorted(
        (
            (segment, point_to_segment_m(latitude, longitude, segment))
            for segment in segments.values()
        ),
        key=lambda item: item[1],
    )
    return [
        (segment, meters)
        for segment, meters in ranked[:SEGMENT_CANDIDATES]
        if meters <= MAX_SEGMENT_PICK_DISTANCE_M
    ]


class StopSubentryFlowHandler(ConfigSubentryFlow):
    """Watch a public transport stop (transitous.org search)."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._stops: dict[str, TransitStop] = {}
        self._threshold: int = DEFAULT_DELAY_ALERT_MIN

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Search a stop by name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._threshold = user_input[CONF_DELAY_ALERT_THRESHOLD]
            client = TransitousClient(async_get_clientsession(self.hass))
            try:
                results = await client.async_search_stops(user_input[CONF_QUERY])
            except BonnVoyageApiError:
                return self.async_abort(reason="transitous_unavailable")
            if not results:
                errors["base"] = "no_stops_found"
            else:
                self._stops = {stop.stop_id: stop for stop in results}
                return await self.async_step_select()

        schema = self.add_suggested_values_to_schema(
            vol.Schema(
                {
                    vol.Required(CONF_QUERY): str,
                    vol.Required(
                        CONF_DELAY_ALERT_THRESHOLD, default=DEFAULT_DELAY_ALERT_MIN
                    ): vol.All(
                        NumberSelector(
                            NumberSelectorConfig(
                                min=1,
                                max=60,
                                step=1,
                                mode=NumberSelectorMode.BOX,
                                unit_of_measurement="min",
                            )
                        ),
                        vol.Coerce(int),
                    ),
                }
            ),
            user_input,
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick one of the found stops."""
        entry = self._get_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            stop = self._stops[user_input[CONF_STOP_ID]]
            if any(
                subentry.unique_id == stop.stop_id
                for subentry in entry.subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE_STOP
            ):
                errors["base"] = "already_configured"
            else:
                return self.async_create_entry(
                    title=stop.name,
                    data={
                        CONF_STOP_ID: stop.stop_id,
                        CONF_DELAY_ALERT_THRESHOLD: self._threshold,
                    },
                    unique_id=stop.stop_id,
                )

        def _dist(stop: TransitStop) -> float:
            meters = distance(
                self.hass.config.latitude,
                self.hass.config.longitude,
                stop.latitude,
                stop.longitude,
            )
            return meters if meters is not None else float("inf")

        options = [
            SelectOptionDict(
                value=stop.stop_id,
                label=f"{stop.name} ({_dist(stop) / 1000:.1f} km)",
            )
            for stop in sorted(self._stops.values(), key=_dist)
        ]
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP_ID): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
            errors=errors,
        )


class BonnVoyageOptionsFlowHandler(OptionsFlow):
    """Options: adjust the traffic message area."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current = self.config_entry.options.get(
            CONF_AREA, self.config_entry.data.get(CONF_AREA)
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_AREA, default=current): LocationSelector(
                        LocationSelectorConfig(radius=True)
                    )
                }
            ),
        )

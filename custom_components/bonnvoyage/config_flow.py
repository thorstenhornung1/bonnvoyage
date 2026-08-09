"""Config flow for the BonnVoyage integration."""

from __future__ import annotations

from collections.abc import Mapping
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
from .api.models import ParkingFacility, RoutePoint, RouteRequest, TrafficSegment
from .api.routing import PROVIDERS, RouteProvider, create_provider
from .const import (
    CONF_ADD_VIA,
    CONF_API_KEY,
    CONF_AREA,
    CONF_DELAY_THRESHOLD,
    CONF_DESTINATION,
    CONF_FACILITY_ID,
    CONF_LOCATION,
    CONF_NAME,
    CONF_PARKINGS,
    CONF_PROVIDER,
    CONF_SEGMENT_ID,
    CONF_START,
    CONF_VIA,
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
)

_LOGGER = logging.getLogger(__name__)

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
    """Watch the city traffic segment closest to a picked point."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._segments: dict[str, TrafficSegment] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick a point; the closest segment is watched."""
        errors: dict[str, str] = {}
        entry = self._get_entry()

        if not self._segments:
            client = BonnTrafficClient(async_get_clientsession(self.hass))
            try:
                self._segments = await client.async_fetch_segments()
            except BonnVoyageApiError:
                return self.async_abort(reason="segments_unavailable")

        if user_input is not None:
            location = user_input[CONF_LOCATION]
            segment, meters = _closest_segment(
                self._segments, location["latitude"], location["longitude"]
            )
            if segment is None or meters > MAX_SEGMENT_PICK_DISTANCE_M:
                errors["base"] = "no_segment_nearby"
            elif any(
                subentry.unique_id == segment.segment_id
                for subentry in entry.subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE_SEGMENT
            ):
                errors["base"] = "already_configured"
            else:
                name = user_input.get(CONF_NAME, "").strip()
                return self.async_create_entry(
                    title=name or f"Stadtstrecke {segment.segment_id}",
                    data={CONF_SEGMENT_ID: segment.segment_id},
                    unique_id=segment.segment_id,
                )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LOCATION,
                    default={
                        "latitude": self.hass.config.latitude,
                        "longitude": self.hass.config.longitude,
                    },
                ): LocationSelector(),
                vol.Optional(CONF_NAME, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


def _closest_segment(
    segments: dict[str, TrafficSegment], latitude: float, longitude: float
) -> tuple[TrafficSegment | None, float]:
    """Find the segment with the closest support point to the given point."""
    best: TrafficSegment | None = None
    best_meters = float("inf")
    for segment in segments.values():
        for lat, lon in segment.points:
            meters = distance(latitude, longitude, lat, lon)
            if meters is not None and meters < best_meters:
                best = segment
                best_meters = meters
    return best, best_meters


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

"""Routing provider port for BonnVoyage (ports & adapters)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, Protocol

import aiohttp

from ..models import RouteRequest, RouteResult


class RouteProvider(Protocol):
    """Port every routing provider adapter implements."""

    provider_id: str
    supports_live_traffic: bool

    async def async_validate_key(self) -> None:
        """Validate the API key with a minimal test request."""

    async def async_route(self, request: RouteRequest) -> RouteResult:
        """Calculate a route with live traffic."""


def _create_tomtom(session: aiohttp.ClientSession, api_key: str) -> RouteProvider:
    from .tomtom import TomTomProvider

    return TomTomProvider(session, api_key)


PROVIDERS: Final[dict[str, Callable[[aiohttp.ClientSession, str], RouteProvider]]] = {
    "tomtom": _create_tomtom,
}


def create_provider(
    name: str, session: aiohttp.ClientSession, api_key: str
) -> RouteProvider:
    """Create a provider adapter by registry name."""
    return PROVIDERS[name](session, api_key)

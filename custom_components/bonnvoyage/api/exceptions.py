"""Exception taxonomy for the BonnVoyage API layer.

API clients raise only these exceptions; mapping to Home Assistant semantics
(UpdateFailed, ConfigEntryAuthFailed, ...) happens exclusively in the
coordinators.
"""

from __future__ import annotations


class BonnVoyageApiError(Exception):
    """Base class for all API layer errors."""


class ApiConnectionError(BonnVoyageApiError):
    """Network problem: timeout, DNS, TLS, non-2xx response."""


class ApiResponseTooLargeError(ApiConnectionError):
    """The response exceeded the configured size cap."""


class ApiParseError(BonnVoyageApiError):
    """The response could not be parsed or failed validation."""


class ApiAuthError(BonnVoyageApiError):
    """The remote service rejected our credentials (routing providers, M3)."""


class ApiRateLimitError(BonnVoyageApiError):
    """The remote service reported too many requests (routing providers, M3)."""


class ApiRouteNotFoundError(BonnVoyageApiError):
    """No route could be calculated for the given points."""

"""Shared HTTP helpers for the BonnVoyage API clients."""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import aiohttp

from .exceptions import ApiConnectionError, ApiResponseTooLargeError

USER_AGENT: Final = "BonnVoyage/0.1 (Home Assistant integration)"
REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=15)


async def async_get_capped(
    session: aiohttp.ClientSession,
    url: str,
    max_bytes: int,
    *,
    params: dict[str, str] | None = None,
    status_handler: Callable[[int, bytes], None] | None = None,
) -> bytes:
    """GET a URL with timeout, User-Agent and a hard response size cap.

    Secrets (e.g. API keys) must be passed via ``params`` — error messages
    only ever contain the bare ``url``. ``status_handler`` lets callers map
    non-200 statuses to their own exceptions (it gets status and body and
    must raise); without it any non-200 raises ApiConnectionError.
    """
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            params=params,
        ) as response:
            if response.status != 200:
                if status_handler is not None:
                    body = await response.content.read(64 * 1024)
                    status_handler(response.status, body)
                raise ApiConnectionError(f"{url} returned HTTP {response.status}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.content.iter_chunked(16 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise ApiResponseTooLargeError(f"{url} exceeded {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
    except TimeoutError as err:
        raise ApiConnectionError(f"Timeout fetching {url}") from err
    except aiohttp.ClientError as err:
        raise ApiConnectionError(f"Error fetching {url}: {err}") from err

"""Provider resolution for Jev: native TypeSafe or OpenRouter's Decisions API.

The typesafe-sdk always builds its request URL as base_url + "/v1/systemone".
OpenRouter serves the same model under its Decisions API at
"/api/alpha/decisions" with identical wire protocol and Bearer auth, so
OpenRouter support is a path-rewriting transport wrapped around the SDK's
own HTTP transport.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx2

OPENROUTER_KEY_PREFIX = "sk-or-"
OPENROUTER_BASE_URL = "https://openrouter.ai"
OPENROUTER_MODEL = "typesafe/jev-1.13"
NATIVE_MODEL = "jev-latest"

_SYSTEM_ONE_PATH = "/v1/systemone"
_DECISIONS_PATH = "/api/alpha/decisions"


def rewrite_path(path: str) -> str:
    """Map the SDK's fixed systemone path to OpenRouter's decisions path."""
    if path == _SYSTEM_ONE_PATH:
        return _DECISIONS_PATH
    return path


class OpenRouterTransport(httpx2.BaseTransport):
    """Sync transport rewriting systemone requests to OpenRouter's decisions endpoint."""

    def __init__(self) -> None:
        self._inner = httpx2.HTTPTransport()

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        request.url = request.url.copy_with(path=rewrite_path(request.url.path))
        return self._inner.handle_request(request)


class AsyncOpenRouterTransport(httpx2.AsyncBaseTransport):
    """Async transport rewriting systemone requests to OpenRouter's decisions endpoint."""

    def __init__(self) -> None:
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        request.url = request.url.copy_with(path=rewrite_path(request.url.path))
        return await self._inner.handle_async_request(request)


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str
    client_kwargs: dict[str, Any] = field(default_factory=dict)


def _is_openrouter_key(api_key: str | None) -> bool:
    return bool(api_key) and api_key.startswith(OPENROUTER_KEY_PREFIX)


def resolve_provider(api_key: str | None, *, transport_factory: type[Any] = OpenRouterTransport) -> ProviderConfig:
    """Resolve which provider/model/client kwargs to use for Jev calls.

    JEV_PROVIDER (openrouter|typesafe) overrides the key-prefix sniff.
    """
    override = os.environ.get("JEV_PROVIDER", "").strip().lower()
    use_openrouter = override == "openrouter" or (not override and _is_openrouter_key(api_key))

    if use_openrouter:
        return ProviderConfig(
            name="openrouter",
            model=OPENROUTER_MODEL,
            client_kwargs={"base_url": OPENROUTER_BASE_URL, "transport": transport_factory()},
        )
    return ProviderConfig(name="typesafe", model=NATIVE_MODEL, client_kwargs={})


def resolve_async_provider(api_key: str | None) -> ProviderConfig:
    """Like resolve_provider, but wires the async transport for AsyncTypeSafeClient."""
    return resolve_provider(api_key, transport_factory=AsyncOpenRouterTransport)

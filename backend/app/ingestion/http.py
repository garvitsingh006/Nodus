"""Minimal, injectable HTTP transport shared by production connectors.

Classifies every non-2xx response into a ``ConnectorError`` with an explicit
retryable/permanent verdict up front, so a connector never has to guess from
an arbitrary exception type. The real transport uses the standard library
only (consistent with ``backend.app.ingestion.github_public``); tests inject
a deterministic fake transport instead of hitting the network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.ingestion.connector import permanent, retryable


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def json(self):
        import json
        return json.loads(self.body or b"{}")


Transport = Callable[[str, str, dict[str, str], bytes | None], HttpResponse]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> HttpResponse:
    try:
        with urlopen(Request(url, method=method, headers=headers, data=body), timeout=15) as response:
            return HttpResponse(response.status, dict(response.headers), response.read())
    except HTTPError as exc:
        return HttpResponse(exc.code, dict(exc.headers or {}), exc.read())
    except (URLError, TimeoutError) as exc:
        raise retryable(f"Network error reaching {url}: {exc}") from exc


class HttpClient:
    """Classifies HTTP outcomes once, here, so every connector gets the same
    retryable/permanent verdicts for rate limits, auth failures and server
    errors instead of reimplementing it per provider."""

    def __init__(self, transport: Transport = urllib_transport):
        self.transport = transport

    def request(self, method: str, url: str, *, headers: dict[str, str] | None = None,
               body: bytes | None = None) -> HttpResponse:
        response = self.transport(method, url, dict(headers or {}), body)
        retry_after = response.headers.get("Retry-After")
        if response.status == 429 or (response.status == 403 and retry_after):
            raise retryable(f"Rate limited by {url} (Retry-After={retry_after})")
        if response.status in (401, 403):
            raise permanent(f"Authentication/authorization failed for {url}: HTTP {response.status}")
        if response.status == 404:
            raise permanent(f"Not found: {url}")
        if response.status >= 500:
            raise retryable(f"Source server error {response.status} for {url}")
        if response.status >= 400:
            raise permanent(f"Request failed with HTTP {response.status} for {url}")
        return response

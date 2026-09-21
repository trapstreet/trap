from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import httpx


class ApiError(Exception):
    """A trapstreet API call failed — bad status, unreachable server, or invalid token.
    Carries a user-facing message; the CLI maps it to a clean error (no traceback).
    ``status`` is the HTTP status when there was one, so a caller can tell a token the
    server refused (401) from a server it could not reach (None)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ApiClient:
    """Authenticated HTTP client for the trapstreet API."""

    def __init__(self, server: str, api_key: str, timeout: int = 30) -> None:
        self._server = server.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    @cached_property
    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._server,
            headers={
                "authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            timeout=self._timeout,
        )

    def get_me(self) -> dict[str, Any]:
        try:
            resp = self._client.get("/api/me", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ApiError("token is invalid", status=401) from None
            status = e.response.status_code
            raise ApiError(f"server error ({status})", status=status) from None
        except httpx.RequestError:
            raise ApiError("server unreachable") from None

    def verified_user_id(self) -> str | None:
        """The stable account id behind this token, from ``/api/me``, or None when the
        server does not report one. Raises ``ApiError`` like ``get_me``."""
        user = self.get_me().get("user")
        if not isinstance(user, dict):
            return None
        identifier = user.get("id")
        return identifier if isinstance(identifier, str) else None

    def capabilities(self) -> dict[str, Any]:
        """What this server supports (``GET /api/v2/capabilities``), sent with **no**
        credentials — this probe can run before the user has agreed to submit anything
        (to inform the confirmation itself), so it must not identify them to the server
        first. Built explicitly via ``build_request``/``send`` rather than the shorter
        ``self._client.get(...)``, so a future refactor back onto that shortcut fails a
        test (below) instead of silently re-attaching ``self._client``'s default
        ``authorization`` header. ``Headers.pop(..., None)`` rather than ``del``: ``del``
        raises ``KeyError`` when the header isn't there (checked directly against this
        codebase's httpx) — defensive here even though every request built from
        ``self._client`` carries that default header today.

        A server that does not answer — offline, older, or a network hiccup — is read
        as "supports nothing new": the caller then takes the conservative branch rather
        than guessing. ``httpx.HTTPError`` covers both a failed request and a non-2xx
        status (``raise_for_status``); ``ValueError`` covers a body that is not valid
        JSON (``.json()`` raises ``json.JSONDecodeError``, a ``ValueError`` subclass)."""
        try:
            request = self._client.build_request("GET", "/api/v2/capabilities")
            request.headers.pop("authorization", None)
            response = self._client.send(request)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}

    def submit(self, report_path: Path) -> dict[str, Any]:
        # Content-addressed ingest: the task identity travels inside the report
        # (provenance.task.{repo,commit,subdirectory}), not the URL — so no task_id
        # path segment.
        try:
            resp = self._client.post("/api/submit", content=report_path.read_bytes())
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise ApiError(f"http {e.response.status_code}: {_error_detail(e.response)}") from None
        except httpx.RequestError as e:
            raise ApiError(f"connection error: {e}") from None


def _error_detail(resp: httpx.Response) -> str:
    """The server's JSON {error} message when present, else the raw body text."""
    try:
        error = resp.json().get("error")
    except (ValueError, AttributeError):
        return resp.text
    return str(error) if error else resp.text

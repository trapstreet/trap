from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any

import httpx


class ApiError(Exception):
    """A trapstreet API call failed — bad status, unreachable server, or invalid token.
    Carries a user-facing message; the CLI maps it to a clean error (no traceback)."""


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
                raise ApiError("token is invalid") from None
            raise ApiError(f"server error ({e.response.status_code})") from None
        except httpx.RequestError:
            raise ApiError("server unreachable") from None

    def submit(self, report_path: Path) -> dict[str, Any]:
        # Content-addressed ingest: the task identity travels inside the report
        # (provenance.task.{repo,commit}), not the URL — so no task_id path segment.
        try:
            resp = self._client.post("/api/submit", content=report_path.read_bytes())
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            raise ApiError(f"http {e.response.status_code}: {e.response.text}") from None
        except httpx.RequestError as e:
            raise ApiError(f"connection error: {e}") from None

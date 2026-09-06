"""HTTP for live progress: two calls, short timeouts, no heroics.

Separate from :class:`trap.auth.client.ApiClient` because the failure contract
is the opposite one. `tp submit` failing is a real error the user must see and
act on -- it exits non-zero. A progress ping failing is a non-event: the run is
what matters, the events are already durable on disk, and the only correct
response is to shrug and try again later. Sharing a class would mean one of
those two behaviours was wrong.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

import httpx


class LiveApiError(Exception):
    """A live-sync call did not succeed. Always caught by the tracker."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def credential_rejected(self) -> bool:
        """401/403: stop using this credential for the rest of the run.

        Retrying a rejected token just produces more rejections, and -- more
        importantly -- must never cause a fallback to some other stored
        credential or server.
        """
        return self.status in (401, 403)


class LiveClient:
    """Authenticated client for the v2 live-progress endpoints."""

    #: Deliberately short. This sits next to a run, and a slow server must not
    #: be able to hold a case up even if something one day awaits a send.
    TIMEOUT = 8.0

    def __init__(self, server: str, api_key: str, timeout: float | None = None) -> None:
        self._server = server.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout if timeout is not None else self.TIMEOUT

    @property
    def server(self) -> str:
        return self._server

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

    def close(self) -> None:
        if "_client" in self.__dict__:
            self._client.close()

    def whoami(self) -> str | None:
        """The stable account id for this credential, or None if the server does not say.

        None is not an error: an older deployment has no id to give, and the
        run still syncs -- it just cannot be safely resumed under a different
        login later.
        """
        data = self._request("GET", "/api/me")
        user = data.get("user")
        if isinstance(user, dict):
            identifier = user.get("id")
            return identifier if isinstance(identifier, str) else None
        identifier = data.get("id")
        return identifier if isinstance(identifier, str) else None

    def ensure_session(
        self,
        client_run_id: str,
        *,
        snapshot: dict[str, Any] | None = None,
        runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotently open the session. Safe to repeat: the server keys on client_run_id."""
        return self._request(
            "PUT",
            f"/api/v2/local-runs/{client_run_id}",
            json={"snapshot": snapshot or {}, "runtime": runtime or {}},
        )

    def send_events(self, run_ref: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Send a batch. The response's ``ack_seq`` is the contiguous high-water mark."""
        return self._request(
            "POST",
            f"/api/v2/local-runs/{run_ref}/events",
            json={"events": events},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise LiveApiError(f"http {e.response.status_code}", status=e.response.status_code) from None
        except httpx.RequestError as e:
            raise LiveApiError(f"unreachable: {type(e).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise LiveApiError("response was not JSON") from None
        return body if isinstance(body, dict) else {}

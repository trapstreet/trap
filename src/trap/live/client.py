"""HTTP for live progress and site grading: a handful of calls, short timeouts, no heroics.

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

from trap import __version__

#: The server's answer to a CLI it will no longer talk to: HTTP 426 with this
#: code, and an ``error`` text that carries the install command.
CLIENT_TOO_OLD = "CLIENT_TOO_OLD"


class LiveApiError(Exception):
    """A live-sync call did not succeed. Always caught by the tracker."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: dict[str, Any] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        #: The error body, when the server sent a JSON object: ``{"error": <message>,
        #: "code": <machine code>}`` on every route. Mostly advisory; the two
        #: things a caller may branch on are the properties below.
        self.payload: dict[str, Any] = payload or {}
        #: A ``Retry-After`` the server gave in whole seconds, else None.
        self.retry_after = retry_after

    @property
    def credential_rejected(self) -> bool:
        """401/403: stop using this credential for the rest of the run.

        Retrying a rejected token just produces more rejections, and -- more
        importantly -- must never cause a fallback to some other stored
        credential or server.
        """
        return self.status in (401, 403)

    @property
    def client_too_old(self) -> bool:
        """The server refuses this build of tp outright. Terminal, never retried:
        the feature is switched off for the run and the server's own words --
        which name the install command -- are shown once."""
        return self.status == 426 or self.payload.get("code") == CLIENT_TOO_OLD

    @property
    def server_message(self) -> str | None:
        """The human ``error`` text from the body, when there is one."""
        message = self.payload.get("error")
        return message if isinstance(message, str) and message else None


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
                "user-agent": f"tp/{__version__}",
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

    def checkpoint(
        self,
        run_ref: str,
        *,
        checkpoint_id: str,
        expected_producer_generation: int,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover from a gap the server can never ack past.

        A compare-and-swap on the producer generation: the server accepts only
        if it still holds ``expected_producer_generation``, and answers
        ``{producer_generation, ack_seq, already_applied}`` with the one it
        opened. A 409 (``code: CONFLICT``, ``error: GENERATION_CONFLICT``) means
        someone else moved it first; the body names no generation, so the
        caller re-reads the run to learn it.
        """
        return self._request(
            "POST",
            f"/api/v2/local-runs/{run_ref}/checkpoint",
            json={
                "checkpoint_id": checkpoint_id,
                "expected_producer_generation": expected_producer_generation,
                "snapshot": snapshot,
            },
        )

    # -- site grading ------------------------------------------------------

    def resolve_evaluation(self, *, repo: str, commit: str, path: str | None) -> dict[str, Any]:
        """The admitted evaluation revision for a task checkout, if there is one.

        ``{revision_id, cases_total, admitted}`` on success; a 404 (raised as
        ``LiveApiError``) means the task has no admitted revision, which is the
        ordinary case and not an error to show.
        """
        params = {"repo": repo, "commit": commit}
        if path is not None:
            params["path"] = path
        return self._request("GET", "/api/v2/evaluations/resolve", params=params)

    def open_evaluation(
        self,
        *,
        revision_id: str,
        client_run_id: str,
        runtime: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Open a server-graded run. Idempotent on ``client_run_id``: the same id
        opens the same run, so a retry cannot create a second one. ``runtime``
        declares who is running it -- the same block the session PUT sends --
        and ``context`` is the opening description (see :mod:`trap.live.context`),
        stored with the run when it is created; a retry never replaces it."""
        body: dict[str, Any] = {
            "revision_id": revision_id,
            "client_run_id": client_run_id,
            "runtime": runtime or {},
        }
        if context is not None:
            body["context"] = context
        return self._request("POST", "/api/v2/evaluations", json=body)

    def put_context(self, run_ref: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Describe a run: merge ``patch`` into what the site knows about it.

        Accepted on both channels (the private local run and the graded one)
        under the run's server id or its ``client_run_id``. The site answers
        ``{accepted, ignored, context, run}``; a 400 names a field that is not
        the client's to send, and is the one answer worth not retrying.
        """
        return self._request("POST", f"/api/v2/runs/{run_ref}/context", json=patch)

    def submit_answers(self, run_id: str, cases_results: list[dict[str, Any]]) -> dict[str, Any]:
        """Hand in answers for the site to judge, in the report's own ``cases_results`` shape."""
        return self._request(
            "POST",
            f"/api/v2/runs/{run_id}/submissions",
            json={"cases_results": cases_results},
        )

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise LiveApiError(
                f"http {e.response.status_code}",
                status=e.response.status_code,
                payload=_json_object(e.response),
                retry_after=_retry_after(e.response),
            ) from None
        except httpx.RequestError as e:
            raise LiveApiError(f"unreachable: {type(e).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise LiveApiError("response was not JSON") from None
        return body if isinstance(body, dict) else {}


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """The response body when it is a JSON object, else an empty one.

    An error body is advisory: a server that answers a 409 with HTML must not
    turn a recoverable conflict into a crash.
    """
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _retry_after(response: httpx.Response) -> int | None:
    """``Retry-After`` in whole seconds, or None. The HTTP-date form is ignored:
    a backoff of its own is good enough, and a clock comparison is not worth
    getting wrong."""
    value = response.headers.get("retry-after", "").strip()
    return int(value) if value.isdigit() else None

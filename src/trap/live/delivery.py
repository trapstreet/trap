"""The one flow that gets a run's queue to the server.

Two things send progress: the sender thread while `tp run` is going, and
`tp sync` afterwards. They used to differ in a way that mattered -- the sender
opened the session once at start and then only posted events, and `tp sync`
never opened it at all -- so a run that began offline could never be recovered:
its events were posted, forever, at a session that did not exist.

So both now do exactly the same thing, in the same order, through this module:

1. **verify** the frozen identity -- ask the server who this token is, and
   compare with the account the sidecar froze at run start;
2. **ensure** the session, idempotently, whenever the server has not yet
   named it -- a PUT keyed on ``client_run_id`` that is safe to repeat;
3. **send** the queue, batch by batch, persisting each acknowledgement.

Nothing is ever posted before step 2 has succeeded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from trap import __version__
from trap.live.client import LiveClient
from trap.live.identity import LiveSession

#: Why a credential may not be handed the queue. The two callers word these
#: differently -- a one-line notice mid-run, a full sentence from `tp sync` --
#: so this layer only names the reason.
RefusalReason = Literal[
    "unowned",  # the sidecar froze no account, and the caller did not ask to claim it
    "unidentified",  # the caller asked to claim, but the server would not say who this is
    "other_account",  # the sidecar's account is not the one this token belongs to
]


def verify_identity(
    client: LiveClient, *, frozen_user_id: str | None, claim: bool
) -> tuple[RefusalReason | None, str | None]:
    """Ask the server who this token is and compare with a frozen account.

    Returns ``(None, verified_id)`` when the queue may be delivered under this
    credential, else ``(reason, None)``. A frozen account continues under any
    later token of the same account. No frozen account is not adopted by
    default -- whoever is logged in now is not necessarily who ran it -- unless
    the caller claims it explicitly; the id handed back is then the one to
    freeze. Raises ``LiveApiError`` when the server cannot answer.

    Shared by the progress sidecar and the graded-run sidecar, and only ever
    called from a session open or from `tp sync`: the in-run answers sender
    makes no identity call of its own, because the open already proved the
    token on this server.
    """
    user_id = client.whoami()
    if frozen_user_id is None:
        if not claim:
            return "unowned", None
        if user_id is None:
            return "unidentified", None
        return None, user_id
    if user_id is None or user_id != frozen_user_id:
        return "other_account", None
    return None, user_id


def tp_runtime() -> dict[str, Any]:
    """Who is running this: what both the session PUT and the graded-run open
    declare. One place, so the two payloads cannot drift. ``trap_version`` is
    sent as it is -- ``0.0.0+unknown`` included -- and the server decides."""
    return {"orchestrator": "tp", "executor": "tp", "trap_version": __version__}


class Delivery:
    """One run's delivery pipeline: identity, session, then events.

    Holds the snapshot the session is described with, because a re-PUT replaces
    the server's copy: every ensure must send the whole description, or a retry
    would blank what the first call wrote.
    """

    def __init__(
        self,
        client: LiveClient,
        session: LiveSession,
        run_dir: Path,
        *,
        snapshot: dict[str, Any],
    ) -> None:
        self._client = client
        self._session = session
        self._run_dir = run_dir
        self._snapshot = snapshot

    @property
    def session(self) -> LiveSession:
        return self._session

    @property
    def reference(self) -> str:
        """The server's own id once known, else the client's -- the server resolves both."""
        return self._session.run_id or self._session.client_run_id

    def establish(self, *, claim: bool) -> RefusalReason | None:
        """Verify who we are, then make sure the session exists. Raises ``LiveApiError``.

        The session is only (re)opened while the server has never named it: a
        known ``run_id`` is proof it exists, and one PUT per run is enough.
        """
        refusal = self.verify(claim=claim)
        if refusal is not None:
            return refusal
        if self._session.run_id is None:
            self.ensure()
        return None

    def verify(self, *, claim: bool) -> RefusalReason | None:
        """Check the current credential against the account frozen in the sidecar.

        A frozen account continues under any later token of the same account.
        A sidecar that froze *no* account is not adopted by default -- whoever is
        logged in now is not necessarily who ran it -- unless the caller claims
        it explicitly, which then freezes the verified id so the question is
        never open again. Raises ``LiveApiError`` when the server cannot answer.
        """
        refusal, verified = verify_identity(self._client, frozen_user_id=self._session.user_id, claim=claim)
        if refusal is not None:
            return refusal
        if self._session.user_id is None:
            self._session.user_id = verified
            self.persist()
        return None

    def ensure(self) -> dict[str, Any]:
        """Idempotently open the session; record the server's id for it.

        Returns the server's view of the run (its ``producer_generation`` is what a
        conflicting checkpoint needs to re-read). Raises ``LiveApiError``.
        """
        data = self._client.ensure_session(
            self._session.client_run_id,
            snapshot=self._snapshot,
            runtime=tp_runtime(),
        )
        run = data.get("run")
        if not isinstance(run, dict):
            return {}
        if isinstance(run.get("id"), str) and run["id"] != self._session.run_id:
            self._session.run_id = run["id"]
            self.persist()
        return run

    def server_generation(self) -> int | None:
        """The producer generation the server currently holds, re-read via ensure.

        The checkpoint conflict answer names no generation (it is a bare 409), so
        the only way to learn the right one is to ask for the run again.
        """
        generation = self.ensure().get("producer_generation")
        return generation if isinstance(generation, int) else None

    def send(self, events: list[dict[str, Any]]) -> int | None:
        """Post one batch; persist and return the new acknowledgement.

        None means the server took the request but moved nothing -- a conflict,
        or a generation it no longer accepts. Raises ``LiveApiError``.
        """
        data = self._client.send_events(self.reference, events)
        ack = data.get("ack_seq")
        if not isinstance(ack, int) or ack <= self._session.acked_seq:
            return None
        self._session.acked_seq = ack
        self.persist()
        return ack

    def checkpoint(
        self,
        *,
        checkpoint_id: str,
        expected_producer_generation: int,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Open a new producer generation after a gap. Raises ``LiveApiError``;
        a 409 means the server holds a different generation -- see
        :meth:`server_generation`."""
        return self._client.checkpoint(
            self.reference,
            checkpoint_id=checkpoint_id,
            expected_producer_generation=expected_producer_generation,
            snapshot=snapshot,
        )

    def persist(self) -> None:
        """Write the sidecar. A sidecar that cannot be written costs a duplicate
        send next time -- which the server dedupes -- never the queue."""
        try:
            self._session.save(self._run_dir)
        except OSError:
            return

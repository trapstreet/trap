"""Delivering a finished run's queued progress, later.

`tp run` mirrors progress while it happens, but a CLI has no daemon: when the
process exits, whatever the network never took stays in the run's outbox. This
module is the other half of that promise -- `tp sync` picks a run up afterwards
and hands the queue over.

Three rules shape everything here.

*The queue belongs to one account on one server.* It was written under a frozen
identity, so before a single event is sent the current credential is checked
against it. A rotated token for the same person is fine; a different person, or
a different server, means the events stay on disk rather than being delivered to
whoever happens to be logged in now.

*Being offline is not a failure.* Nothing about a run changed because the site
did not hear about it. Sync says what happened and exits 0; only a genuine
trap-level problem -- wrong arguments, an unreadable workspace, a credential the
server refuses -- is an error.

*A gap is never papered over.* If the events the server still needs are gone
from disk, no amount of sending will make its contiguous acknowledgement move
past the hole. Then, and only then, sync sends a checkpoint: a snapshot of where
the run actually got to, which opens a new producer generation and makes the
site say out loud that the history is incomplete.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from trap.auth.resolve import ResolvedAuth
from trap.auth.store import CredentialStore, CredentialStoreError
from trap.live.client import LiveApiError, LiveClient
from trap.live.identity import LiveSession
from trap.live.outbox import Outbox, OutboxEvent
from trap.live.tracker import MAX_BATCH
from trap.models.report import ReportData
from trap.workspace import Workspace

#: What a sync attempt ended up being. Only ``refused`` is an error; everything
#: else leaves the user with a run that is either up to date or still queued,
#: both of which are ordinary states.
SyncStatus = Literal[
    "untracked",  # the run was never mirrored -- there is no queue at all
    "up_to_date",  # the server already has everything
    "delivered",  # everything pending went over
    "partial",  # some went over, the rest stayed
    "offline",  # the server could not be reached; nothing was lost
    "stalled",  # the server answered but took nothing
    "recovered",  # a gap was closed with a checkpoint
    "refused",  # wrong account, wrong server, or a rejected token
]

#: Event types that say a run reached an end state, and the execution status
#: each one implies in a checkpoint snapshot.
_TERMINAL_STATUS = {
    "run_finished": "completed",
    "run_failed": "failed",
    "run_cancelled": "cancelled",
}


class SyncReport(BaseModel):
    """What one `tp sync` did, as data the CLI only has to print.

    Keeping the outcome a model rather than printed text means the decision
    "is this an error?" is made once, here, and the command layer cannot
    accidentally turn a queued run into a non-zero exit.
    """

    status: SyncStatus
    delivered: int = 0
    remaining: int = 0
    message: str

    @property
    def refused(self) -> bool:
        """True when the CLI should exit non-zero. Offline never is."""
        return self.status == "refused"


def sync_run(run_dir: Path, *, server_override: str | None = None) -> SyncReport:
    """Deliver ``run_dir``'s queued progress. Never raises."""
    session = LiveSession.load(run_dir)
    if session is None:
        # Either sync was off, or the sidecar never landed. Both mean this run
        # has no identity on the server, and minting one now would invent a
        # second run rather than continue this one -- so sync is simply not
        # available for it, which is not an error.
        return SyncReport(
            status="untracked",
            message="this run was never tracked, so there is no queued progress to send.",
        )
    if server_override is not None and server_override.rstrip("/") != session.server.rstrip("/"):
        return SyncReport(
            status="refused",
            message=(
                f"this run's queue was created against {session.server}, not {server_override} — "
                "a queue cannot move servers. Re-run tp sync without --server."
            ),
        )
    try:
        auth = ResolvedAuth.resolve(CredentialStore(), session.server)
    except CredentialStoreError as e:
        return SyncReport(status="refused", message=f"cannot read the stored credentials: {e}")
    if auth.api_key is None:
        return SyncReport(
            status="refused",
            message=(
                f"not logged in to {session.server}, which is where this run's queue belongs. "
                f"Run tp auth login --server {session.server}."
            ),
        )

    client = LiveClient(session.server, auth.api_key)
    try:
        return _deliver(session, run_dir, client)
    finally:
        client.close()


def _deliver(session: LiveSession, run_dir: Path, client: LiveClient) -> SyncReport:
    """Everything that needs a live client: identity, then the queue itself."""
    outbox = Outbox(run_dir)
    pending = outbox.pending(session.acked_seq, generation=session.producer_generation)
    if not pending:
        return SyncReport(
            status="up_to_date",
            message="nothing queued — the site already has this run's progress.",
        )

    try:
        user_id = client.whoami()
    except LiveApiError as e:
        return _api_failure(e, delivered=0, remaining=len(pending))
    if not _may_deliver(session, user_id):
        return SyncReport(
            status="refused",
            remaining=len(pending),
            message=(
                f"this run's {len(pending)} queued event(s) belong to a different account on "
                f"{session.server}; they stay on disk. Log in as that account to send them."
            ),
        )
    if session.user_id is None and user_id is not None:
        # First successful contact for a run that was tracked entirely offline.
        # Nothing was frozen before, so there is no owner to contradict -- but
        # from here on there is.
        session.user_id = user_id
        session.save(run_dir)

    if pending[0].client_seq > session.acked_seq + 1:
        # Events between the last acknowledgement and the oldest surviving one
        # are gone (cleanup, or a corrupt file). The server's acknowledgement
        # can never move past the hole, so sending is pointless until a
        # checkpoint declares it.
        return _recover_gap(session, run_dir, client, outbox, pending)
    return _send(session, run_dir, client, pending)


def _may_deliver(session: LiveSession, user_id: str | None) -> bool:
    """May this credential be handed the queue?

    A sidecar with no frozen account is one that never reached the server; there
    is nothing to violate, so the current account adopts it. Once an account is
    frozen, only that account -- under any token it later holds -- may continue.
    """
    if session.user_id is None:
        return True
    return session.belongs_to(session.server, user_id)


def _send(session: LiveSession, run_dir: Path, client: LiveClient, pending: list[OutboxEvent]) -> SyncReport:
    """Ship the queue in batches, persisting each acknowledgement as it lands."""
    reference = session.run_id or session.client_run_id
    delivered = 0
    for start in range(0, len(pending), MAX_BATCH):
        batch = pending[start : start + MAX_BATCH]
        try:
            data = client.send_events(reference, [event.wire() for event in batch])
        except LiveApiError as e:
            return _api_failure(e, delivered=delivered, remaining=len(pending) - delivered)
        ack = data.get("ack_seq")
        if not isinstance(ack, int) or ack <= session.acked_seq:
            # The server took the request but moved nothing -- a conflict, or a
            # generation it no longer accepts. Pushing the next batch would only
            # repeat that, so stop and say so.
            break
        session.acked_seq = ack
        session.save(run_dir)
        delivered = sum(1 for event in pending if event.client_seq <= ack)

    remaining = len(pending) - delivered
    if remaining == 0:
        return SyncReport(
            status="delivered",
            delivered=delivered,
            message=f"delivered {delivered} queued event(s) to {client.server}.",
        )
    if delivered:
        return SyncReport(
            status="partial",
            delivered=delivered,
            remaining=remaining,
            message=(
                f"delivered {delivered} of {len(pending)} queued event(s) to {client.server}; "
                f"{remaining} remain — run tp sync again later."
            ),
        )
    return SyncReport(
        status="stalled",
        remaining=remaining,
        message=(
            f"{client.server} acknowledged none of this run's {remaining} queued event(s); they stay on disk."
        ),
    )


def _recover_gap(
    session: LiveSession,
    run_dir: Path,
    client: LiveClient,
    outbox: Outbox,
    pending: list[OutboxEvent],
) -> SyncReport:
    """Close an unbridgeable gap by checkpointing what is still knowable.

    The snapshot only ever reports local execution state -- how far the run got
    -- so adopting it can never overwrite a report, a final status or a platform
    score the server already holds.
    """
    surviving = outbox.read_all()
    snapshot = _snapshot(run_dir, surviving)
    checkpoint_id = f"cp-{uuid.uuid4().hex}"
    reference = session.run_id or session.client_run_id
    lost = pending[0].client_seq - session.acked_seq - 1

    result = _try_checkpoint(client, reference, checkpoint_id, session.producer_generation, snapshot)
    if isinstance(result, LiveApiError) and result.status == 409:
        # Someone opened a generation after we read ours. The server says which
        # one it holds; one retry with that is enough, and a second conflict is
        # a race we stop competing in.
        current = result.payload.get("producer_generation")
        if not isinstance(current, int):
            return SyncReport(
                status="stalled",
                remaining=len(pending),
                message=(
                    "the server rejected this run's checkpoint and did not say which producer "
                    "generation it holds; nothing was changed and the queue stays on disk."
                ),
            )
        result = _try_checkpoint(client, reference, checkpoint_id, current, snapshot)
    if isinstance(result, LiveApiError):
        return _api_failure(result, delivered=0, remaining=len(pending))

    generation = result.get("producer_generation")
    if not isinstance(generation, int):
        return SyncReport(
            status="stalled",
            remaining=len(pending),
            message=(
                "the server accepted this run's checkpoint but did not name the new producer "
                "generation; the queue stays on disk."
            ),
        )
    ack = result.get("ack_seq")
    session.producer_generation = generation
    session.acked_seq = ack if isinstance(ack, int) else 0
    session.save(run_dir)
    return SyncReport(
        status="recovered",
        remaining=0,
        message=(
            f"{lost} progress event(s) were lost from this run's local queue, so its history "
            f"cannot be completed. Sent a checkpoint instead: {client.server} now records this "
            f"run as {snapshot['exec_status']} at {snapshot['cases_done']} of "
            f"{snapshot['cases_total']} case(s), and shows the history as incomplete."
        ),
    )


def _try_checkpoint(
    client: LiveClient,
    reference: str,
    checkpoint_id: str,
    generation: int,
    snapshot: dict[str, Any],
) -> dict[str, Any] | LiveApiError:
    """One checkpoint attempt, with the failure returned rather than raised — the
    caller has to branch on a conflict, which an exception would hide."""
    try:
        return client.checkpoint(
            reference,
            checkpoint_id=checkpoint_id,
            expected_producer_generation=generation,
            snapshot=snapshot,
        )
    except LiveApiError as e:
        return e


def _snapshot(run_dir: Path, events: list[OutboxEvent]) -> dict[str, Any]:
    """Where this run actually got to, from whatever is still on disk.

    Two independent sources agree or the safer one wins: the events that
    survived the gap, and the saved report. Both are local execution facts, and
    neither is allowed to claim more progress than it can show.
    """
    report = _load_report(run_dir)
    totals = [_number(event, "cases_total") for event in events if "cases_total" in event.payload]
    dones = [_number(event, "cases_done") for event in events if "cases_done" in event.payload]
    finished = {
        event.payload["ordinal"]
        for event in events
        if event.type == "case_finished" and "ordinal" in event.payload
    }
    dones.append(len(finished))
    if report is not None:
        dones.append(len(report.cases_results))
    cases_done = max(dones)
    return {
        "exec_status": _exec_status(events, report),
        "cases_done": cases_done,
        # A total below what has already been done would be a nonsense the site
        # would have to render; the cases we can prove ran are the floor.
        "cases_total": max([*totals, cases_done]),
    }


def _number(event: OutboxEvent, key: str) -> int:
    value = event.payload[key]
    return int(value) if isinstance(value, (int, float)) else 0


def _exec_status(events: list[OutboxEvent], report: ReportData | None) -> str:
    """The run's execution status: its last terminal event, or what the report implies.

    A saved report means the run finished -- it is only written at the end. With
    neither, the honest answer is that it was last seen running.
    """
    for event in reversed(events):
        status = _TERMINAL_STATUS.get(event.type)
        if status is not None:
            return status
    return "completed" if report is not None else "running"


def _load_report(run_dir: Path) -> ReportData | None:
    """The run's saved report, or None when it is absent or unreadable."""
    try:
        return ReportData.model_validate_json((run_dir / Workspace.REPORT_FILENAME).read_text())
    except (OSError, ValidationError, ValueError):
        return None


def _api_failure(error: LiveApiError, *, delivered: int, remaining: int) -> SyncReport:
    """Turn a failed call into an outcome. Only a rejected credential is an error."""
    if error.credential_rejected:
        return SyncReport(
            status="refused",
            delivered=delivered,
            remaining=remaining,
            message=(
                "the server rejected this CLI token, so nothing was sent and no other "
                f"credential was tried; {remaining} event(s) stay on disk."
            ),
        )
    if delivered:
        return SyncReport(
            status="partial",
            delivered=delivered,
            remaining=remaining,
            message=(
                f"delivered {delivered} queued event(s), then the server stopped taking them "
                f"({error}); {remaining} remain — run tp sync again later."
            ),
        )
    if error.status is None:
        return SyncReport(
            status="offline",
            remaining=remaining,
            message=(
                f"the server is unreachable ({error}); this run's {remaining} queued event(s) "
                "stay on disk — run tp sync again when you are back online."
            ),
        )
    return SyncReport(
        status="stalled",
        remaining=remaining,
        message=(
            f"the server could not take this run's progress right now ({error}); "
            f"{remaining} event(s) stay on disk."
        ),
    )

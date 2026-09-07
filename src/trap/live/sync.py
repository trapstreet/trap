"""Delivering a finished run's queued progress, later.

`tp run` mirrors progress while it happens, but a CLI has no daemon: when the
process exits, whatever the network never took stays in the run's outbox. This
module is the other half of that promise -- `tp sync` picks a run up afterwards
and hands the queue over, through the same :class:`~trap.live.delivery.Delivery`
flow the in-run sender uses: verify the frozen identity, ensure the session,
drain, persist the acknowledgement.

Three rules shape everything here.

*The queue belongs to one account on one server.* It was written under a frozen
identity, so before a single event is sent the current credential is checked
against it. A rotated token for the same person is fine; a different person, or
a different server, means the events stay on disk rather than being delivered to
whoever happens to be logged in now. A run that froze *no* account -- tracked
under a credential that was never verified -- is not adopted by default either:
``--claim`` says, explicitly, that it belongs to the current account.

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
from trap.live.delivery import Delivery, RefusalReason
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

#: The execution statuses a checkpoint snapshot may name. The server's own
#: list; the contract test checks it against the web source.
EXEC_STATUSES = ("created", "running", "finished", "failed", "cancelled")

#: Event types that say a run reached an end state, and the execution status
#: each one implies in a checkpoint snapshot.
_TERMINAL_STATUS = {
    "run_finished": "finished",
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


def sync_run(run_dir: Path, *, server_override: str | None = None, claim: bool = False) -> SyncReport:
    """Deliver ``run_dir``'s queued progress. Never raises.

    ``claim`` adopts a run that froze no account into the current one; without
    it such a run is refused, because whoever is logged in now is not
    necessarily who ran it.
    """
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
        return _deliver(session, run_dir, client, claim=claim)
    finally:
        client.close()


def _deliver(session: LiveSession, run_dir: Path, client: LiveClient, *, claim: bool) -> SyncReport:
    """Everything that needs a live client: identity, session, then the queue itself."""
    outbox = Outbox(run_dir)
    pending = outbox.pending(session.acked_seq, generation=session.producer_generation)
    if not pending:
        return SyncReport(
            status="up_to_date",
            message="nothing queued — the site already has this run's progress.",
        )

    surviving = outbox.read_all()
    snapshot = _snapshot(run_dir, surviving)
    delivery = Delivery(client, session, run_dir, snapshot={"cases_total": snapshot["cases_total"]})
    try:
        # The same steps the in-run sender takes, in the same order: who are we,
        # then does the session exist. A run that began offline has no server
        # id yet, and this PUT is what finally creates it.
        refusal = delivery.establish(claim=claim)
    except LiveApiError as e:
        return _api_failure(e, delivered=0, remaining=len(pending))
    if refusal is not None:
        return _refused(refusal, session, len(pending))

    if pending[0].client_seq > session.acked_seq + 1:
        # Events between the last acknowledgement and the oldest surviving one
        # are gone (cleanup, or a corrupt file). The server's acknowledgement
        # can never move past the hole, so sending is pointless until a
        # checkpoint declares it.
        return _recover_gap(delivery, snapshot, pending)
    return _send(delivery, pending)


def _refused(reason: RefusalReason, session: LiveSession, remaining: int) -> SyncReport:
    """Word a refusal for the user. Each names what to do about it."""
    if reason == "unowned":
        message = (
            f"this run froze no account: it was tracked before the CLI's pairing with "
            f"{session.server} was verified, so its {remaining} queued event(s) have no "
            "proven owner. Re-run it under a paired account, or pass --claim to adopt it "
            "into the account you are logged in as now."
        )
    elif reason == "unidentified":
        message = (
            f"cannot claim this run: {session.server} did not say which account this token "
            "belongs to, so there is no verified identity to freeze."
        )
    else:
        message = (
            f"this run's {remaining} queued event(s) belong to a different account on "
            f"{session.server}; they stay on disk. Log in as that account to send them."
        )
    return SyncReport(status="refused", remaining=remaining, message=message)


def _send(delivery: Delivery, pending: list[OutboxEvent]) -> SyncReport:
    """Ship the queue in batches, persisting each acknowledgement as it lands."""
    session = delivery.session
    server = delivery.session.server
    delivered = 0
    for start in range(0, len(pending), MAX_BATCH):
        batch = pending[start : start + MAX_BATCH]
        try:
            ack = delivery.send([event.wire() for event in batch])
        except LiveApiError as e:
            return _api_failure(e, delivered=delivered, remaining=len(pending) - delivered)
        if ack is None:
            # The server took the request but moved nothing -- a conflict, or a
            # generation it no longer accepts. Pushing the next batch would only
            # repeat that, so stop and say so.
            break
        delivered = sum(1 for event in pending if event.client_seq <= session.acked_seq)

    remaining = len(pending) - delivered
    if remaining == 0:
        return SyncReport(
            status="delivered",
            delivered=delivered,
            message=f"delivered {delivered} queued event(s) to {server}.",
        )
    if delivered:
        return SyncReport(
            status="partial",
            delivered=delivered,
            remaining=remaining,
            message=(
                f"delivered {delivered} of {len(pending)} queued event(s) to {server}; "
                f"{remaining} remain — run tp sync again later."
            ),
        )
    return SyncReport(
        status="stalled",
        remaining=remaining,
        message=(f"{server} acknowledged none of this run's {remaining} queued event(s); they stay on disk."),
    )


def _recover_gap(delivery: Delivery, snapshot: dict[str, Any], pending: list[OutboxEvent]) -> SyncReport:
    """Close an unbridgeable gap by checkpointing what is still knowable.

    The snapshot only ever reports local execution state -- how far the run got
    -- so adopting it can never overwrite a report, a final status or a platform
    score the server already holds.
    """
    session = delivery.session
    checkpoint_id = f"cp-{uuid.uuid4().hex}"
    lost = pending[0].client_seq - session.acked_seq - 1

    result = _try_checkpoint(delivery, checkpoint_id, session.producer_generation, snapshot)
    if isinstance(result, LiveApiError) and result.status == 409:
        # Someone opened a generation after we read ours. The conflict answer is
        # bare, so the generation the server holds is re-read from the run
        # itself; one retry with that is enough, and a second conflict is a
        # race we stop competing in.
        try:
            current = delivery.server_generation()
        except LiveApiError as e:
            return _api_failure(e, delivered=0, remaining=len(pending))
        if current is None:
            return SyncReport(
                status="stalled",
                remaining=len(pending),
                message=(
                    "the server rejected this run's checkpoint and did not say which producer "
                    "generation it holds; nothing was changed and the queue stays on disk."
                ),
            )
        result = _try_checkpoint(delivery, checkpoint_id, current, snapshot)
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
    delivery.persist()
    return SyncReport(
        status="recovered",
        remaining=0,
        message=(
            f"{lost} progress event(s) were lost from this run's local queue, so its history "
            f"cannot be completed. Sent a checkpoint instead: {session.server} now records this "
            f"run as {snapshot['exec_status']} at {snapshot['cases_done']} of "
            f"{snapshot['cases_total']} case(s), and shows the history as incomplete."
        ),
    )


def _try_checkpoint(
    delivery: Delivery,
    checkpoint_id: str,
    generation: int,
    snapshot: dict[str, Any],
) -> dict[str, Any] | LiveApiError:
    """One checkpoint attempt, with the failure returned rather than raised — the
    caller has to branch on a conflict, which an exception would hide."""
    try:
        return delivery.checkpoint(
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
    return "finished" if report is not None else "running"


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

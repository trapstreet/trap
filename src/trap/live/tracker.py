"""The tracker: turns a run into whitelisted events and gets them sent.

This is the only place that decides what may leave the machine, and it is
written as a translation rather than a filter -- nothing is copied from the run
and then stripped. Each event is built field by field from values this module
computed itself, so a new field on ``CaseResult`` cannot start being uploaded
because someone forgot to add it to a denylist.

The other half of the job is staying out of the way. Every public method is
wrapped so that no exception reaches the runner's callbacks, and sending
happens on a daemon thread the run never joins except for one bounded flush at
the end.

The sender thread owns the network entirely. It opens the session through the
shared :class:`~trap.live.delivery.Delivery` flow -- and keeps trying, with a
jittered backoff, for as long as the run lasts -- and it posts no event until
that has succeeded. The outbox is where every batch is cut from, against the
server's contiguous acknowledgement: the queue between the run and the sender
only says *that* something was appended, never what. So a batch the network
dropped is re-sent, before anything newer, on the sender's next wake, and the
server's acknowledgement can move again. While a case is running and nothing
else has happened for a while, the same thread emits a heartbeat so a long case
does not read as lost contact.

The run's *description* -- what it was made of, see :mod:`trap.live.context`
-- travels beside the events, not among them. It is not progress: it is a
merge-only record the site keeps per run, so it has no sequence number and no
place in the outbox. :meth:`LiveTracker.describe` hands a patch to the sender,
which posts it once the session exists and the outbox is caught up; a patch
the site did not take is dropped with one notice -- the end-of-run description
says everything the opening one did, and what it adds is also in the report.
"""

from __future__ import annotations

import queue
import random
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from trap.live.client import LiveApiError, LiveClient
from trap.live.delivery import Delivery
from trap.live.identity import LiveSession
from trap.live.outbox import Outbox, OutboxError, OutboxEvent
from trap.models.results import CaseResult

#: Wait no longer than this for the queue to drain at the end of a run. The
#: report is already saved; the exit code is already decided. Unsent events
#: stay in the outbox rather than holding the process open.
FLUSH_TIMEOUT_SECONDS = 3.0

#: How many events one request may carry. Matches the server's batch limit.
MAX_BATCH = 100

#: How long a running case may be silent before the sender says it is still
#: there. The site marks a session stale after 45 seconds without anything.
HEARTBEAT_SECONDS = 10.0

#: Retrying the session open: first wait, growth, and the ceiling. Jittered so
#: many CLIs coming back online together do not knock in lockstep.
ENSURE_BACKOFF_INITIAL = 1.0
ENSURE_BACKOFF_CAP = 30.0

#: What woke the sender. ``event``: the run appended something; ``stop``:
#: close() was called; ``idle``: a heartbeat interval passed with nothing new.
Wake = Literal["event", "stop", "idle"]

#: Every event type this module can emit. The contract test checks each one
#: against the server's allowlist; ``_emit`` is only ever called with these.
EMITTED_EVENT_TYPES = frozenset(
    {
        "run_started",
        "case_started",
        "judge_started",
        "judge_finished",
        "case_finished",
        "grader_started",
        "grader_finished",
        "run_finished",
        "run_failed",
        "run_cancelled",
        "heartbeat",
    }
)


def verdict_of(result: CaseResult) -> str | None:
    """A case's outcome, or None when the run cannot honestly claim one.

    ``judge_exit_code == 0`` means the judge *ran* and produced JSON; it does
    not mean the answer was right, and treating it as a pass is the mistake
    this function exists to prevent. A verdict comes from the score, and only
    where the score is unambiguous -- partial credit is a number, not a
    pass/fail, so it reports as neither.
    """
    if result.judge_exit_code not in (None, 0):
        # The measuring apparatus failed. That is not a zero.
        return "error"
    score = _score_of(result)
    if score is None:
        return None
    if score >= 1:
        return "passed"
    if score <= 0:
        return "failed"
    return None


def _score_of(result: CaseResult) -> float | None:
    return plain_score(result.metrics)


def plain_score(metrics: Any) -> float | None:
    """``metrics["score"]`` when it is a plain number -- never a bool, never prose."""
    if not isinstance(metrics, dict):
        return None
    score = metrics.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        return None
    return float(score)


class LiveTracker:
    """Mirrors one run's progress to the paired account.

    Construct with :meth:`start`, which is the only place that touches the
    network before the first case; it does so on the sender thread, so a slow
    or unreachable server delays nothing.
    """

    def __init__(
        self,
        *,
        client: LiveClient,
        session: LiveSession,
        outbox: Outbox,
        run_dir: Path,
        case_ids: Sequence[str],
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._session = session
        self._outbox = outbox
        self._run_dir = run_dir
        # Ordinals, assigned once, in execution order. The mapping stays here:
        # the wire only ever carries the number.
        self._ordinals = {case_id: index + 1 for index, case_id in enumerate(case_ids)}
        self._cases_total = len(case_ids)
        self._delivery = Delivery(client, session, run_dir, snapshot={"cases_total": self._cases_total})
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._disabled = False
        self._notice: str | None = None
        # A description the site did not take is worth one line, but never at
        # the expense of the line that says progress is still on disk.
        self._context_notice: str | None = None
        self._stop = object()
        # Descriptions collected off the queue and not yet posted; sender-owned.
        self._descriptions: list[dict[str, Any]] = []
        # Session state, owned by the sender thread.
        self._clock = clock
        self._rng = rng
        self._ensured = False
        self._next_ensure_at = 0.0
        self._ensure_delay = ENSURE_BACKOFF_INITIAL
        # The case in flight, for heartbeats. Written by the run's thread, read
        # by the sender; a stale read costs one heartbeat, never correctness.
        self._current_ordinal: int | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def run_url(self) -> str:
        """The page for this run, printable before the server has answered.

        The server resolves a run by its client_run_id as well as by its own
        id, which is what lets the URL exist immediately instead of after a
        round trip that the first case would otherwise wait for.
        """
        return f"{self._client.server}/runs/{self._delivery.reference}"

    @property
    def client_run_id(self) -> str:
        """This run's global id — what the final report carries so the upload and
        the private session that watched the same execution name one run."""
        return self._session.client_run_id

    @property
    def notice(self) -> str | None:
        """A single short line to show the user, or None. Never more than one per run.

        Anything about the queue -- a rejected token, an outbox that could not
        be written, progress left on disk -- outranks a description the site
        did not take: the first is actionable, the second is not.
        """
        return self._notice or self._context_notice

    def start(self) -> None:
        """Begin mirroring. Never raises."""
        try:
            self._thread = threading.Thread(target=self._pump, name="trap-live-sync", daemon=True)
            self._thread.start()
            self._emit("run_started", {"cases_total": self._cases_total})
        except Exception as e:  # pragma: no cover - defensive
            self._disable(f"live sync unavailable ({e.__class__.__name__})")

    def close(self) -> None:
        """Stop mirroring, waiting a bounded time for the queue to drain."""
        if self._thread is None:
            return
        try:
            self._queue.put_nowait(self._stop)
            self._thread.join(timeout=FLUSH_TIMEOUT_SECONDS)
        except Exception:  # pragma: no cover - defensive
            pass
        finally:
            self._client.close()
        pending = self._pending_count()
        if pending and self._notice is None:
            self._notice = (
                f"live sync: {pending} progress event(s) not delivered — kept locally in this "
                "run's outbox; run tp sync later"
            )

    def describe(self, patch: dict[str, Any]) -> None:
        """Record what this run is made of (see :mod:`trap.live.context`).

        Handed to the sender as it is: the patch was built field by field by the
        context module, and this method adds nothing to it. Posted after the
        session exists and the outbox is caught up, so the opening description
        never overtakes the session it describes. Never raises, never blocks.
        """
        if self._disabled:
            return
        try:
            self._queue.put_nowait(dict(patch))
        except Exception as e:  # pragma: no cover - defensive
            self._disable(f"live sync off ({e.__class__.__name__})")

    # -- observer callbacks -------------------------------------------------

    def on_case_start(self, case_id: str) -> None:
        ordinal = self._ordinals.get(case_id)
        if ordinal is not None:
            self._current_ordinal = ordinal
            self._emit("case_started", {"ordinal": ordinal})

    def on_case_done(self, result: CaseResult) -> None:
        ordinal = self._ordinals.get(result.case_id)
        if ordinal is None:
            return
        self._current_ordinal = None
        payload: dict[str, float | int | str] = {"ordinal": ordinal}
        verdict = verdict_of(result)
        if verdict is not None:
            payload["verdict"] = verdict
        score = _score_of(result)
        if score is not None:
            payload["score"] = score
        if result.duration:
            payload["duration_ms"] = int(result.duration * 1000)
        if result.cost is not None and result.cost.cost_usd is not None:
            payload["cost_usd"] = result.cost.cost_usd
        self._emit("case_finished", payload)

    def on_judge_started(self, case_id: str) -> None:
        ordinal = self._ordinals.get(case_id)
        if ordinal is not None:
            self._emit("judge_started", {"ordinal": ordinal})

    def on_judge_finished(self, case_id: str, exit_code: int | None, score: float | None) -> None:
        ordinal = self._ordinals.get(case_id)
        if ordinal is None:
            return
        payload: dict[str, float | int | str] = {"ordinal": ordinal}
        if exit_code is not None:
            payload["judge_exit_code"] = exit_code
        if score is not None:
            payload["score"] = score
        self._emit("judge_finished", payload)

    def on_grader_started(self) -> None:
        self._emit("grader_started", {})

    def on_grader_finished(self, exit_code: int | None, score: float | None) -> None:
        payload: dict[str, float | int | str] = {}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if score is not None:
            payload["score"] = score
        self._emit("grader_finished", payload)

    def on_run_finished(
        self,
        *,
        exit_code: int,
        cases_done: int,
        score: float | None = None,
        cost_usd: float | None = None,
    ) -> None:
        payload: dict[str, float | int | str] = {
            "exit_code": exit_code,
            "cases_done": cases_done,
            "cases_total": self._cases_total,
        }
        if score is not None:
            payload["score"] = score
        if cost_usd is not None:
            payload["cost_usd"] = cost_usd
        self._emit("run_finished", payload)

    def on_run_failed(self, error_code: str, cases_done: int) -> None:
        self._emit("run_failed", {"error_code": error_code, "cases_done": cases_done})

    def on_run_cancelled(self, cases_done: int) -> None:
        self._emit("run_cancelled", {"error_code": "interrupted", "cases_done": cases_done})

    # -- internals ----------------------------------------------------------

    def _emit(self, event_type: str, payload: dict[str, float | int | str]) -> None:
        """Append durably, then hand to the sender. Never raises, never blocks."""
        if self._disabled:
            return
        try:
            event = self._outbox.append(
                event_id=f"{self._session.client_run_id}-{uuid.uuid4().hex[:12]}",
                type=event_type,
                payload=payload,
                producer_generation=self._session.producer_generation,
            )
            # A wake token, not the batch: the event is already durable, and the
            # sender cuts every batch from the outbox itself.
            self._queue.put_nowait(event.client_seq)
        except OutboxError as e:
            # A read-only or full disk. The run is unaffected; only the mirror
            # stops, and it stops loudly enough to be seen once.
            self._disable(f"live sync off: cannot write the outbox ({e})")
        except Exception as e:  # pragma: no cover - defensive
            self._disable(f"live sync off ({e.__class__.__name__})")

    def _disable(self, notice: str) -> None:
        self._disabled = True
        if self._notice is None:
            self._notice = notice

    def _pending(self) -> list[OutboxEvent]:
        """Everything the server has not acknowledged, oldest first."""
        return self._outbox.pending(self._session.acked_seq, generation=self._session.producer_generation)

    def _pending_count(self) -> int:
        try:
            return len(self._pending())
        except Exception:  # pragma: no cover - defensive
            return 0

    def _pump(self) -> None:
        """Sender thread: keep draining until told to stop. Never lets an
        exception escape -- a traceback from a background thread is exactly the
        kind of noise this package promises not to make."""
        try:
            while self._drain_once():
                pass
        except Exception as e:
            self._disable(f"live sync off ({e.__class__.__name__})")

    def _drain_once(self) -> bool:
        """One wake of the sender: a session attempt, the outbox, or a heartbeat.

        Split out of the thread body so the rules are testable without racing a
        thread: everything here is synchronous given a filled queue. Returns
        False once the stop sentinel has been seen.

        Every cycle sends from the sidecar's contiguous acknowledgement, never
        from what happened to be queued: a batch the network dropped is re-sent,
        before anything newer, on the very next wake, so the server's ack can
        move again. The queue only says *that* something happened; the outbox
        says what. Until the session is established nothing is posted, and
        whatever arrived meanwhile is already on disk to be sent as the backlog
        the moment the server answers.
        """
        wake = self._collect()
        if self._disabled:
            return wake != "stop"
        if not self._ensured:
            self._try_ensure()
        owed = self._ensured and self._send_pending()
        if self._ensured:
            self._send_descriptions()
        if self._ensured and not owed and wake == "idle" and self._current_ordinal is not None:
            # A quiet interval mid-case with nothing owed: say it is still
            # running. A wake with something owed retries that instead --
            # beating into a dead link would only pile heartbeats into the
            # outbox.
            self._emit("heartbeat", {"ordinal": self._current_ordinal})
        return wake != "stop"

    def _collect(self) -> Wake:
        """Block for the next wake, then swallow every wake already queued.

        Many wakes collapse into one cycle so a burst of events costs one
        request, not one per event; the batch itself is cut from the outbox.
        A description rides the same queue and is kept aside for posting,
        in the order it was given. The wait gives up after a heartbeat
        interval so the sender also runs when nothing is happening -- to
        retry the session, to re-send what the network dropped, or to beat.
        """
        try:
            item = self._queue.get(timeout=HEARTBEAT_SECONDS)
        except queue.Empty:
            return "idle"
        while item is not self._stop:
            if isinstance(item, dict):
                self._descriptions.append(item)
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return "event"
        return "stop"

    def _try_ensure(self) -> None:
        """Verify identity and open the session, if the backoff allows it now.

        The run's own credential is the one that started it, so a sidecar with
        no frozen account is claimed here -- this is the process that owns the
        run, not a later login -- and the verified id is frozen from then on.
        """
        now = self._clock()
        if now < self._next_ensure_at:
            return
        try:
            refusal = self._delivery.establish(claim=True)
        except LiveApiError as e:
            self._handle_api_error(e)
            self._schedule_retry(now)
            return
        if refusal == "unidentified":
            self._disable(f"live sync off: {self._client.server} did not say which account this token is")
            return
        if refusal is not None:
            self._disable("live sync off: the stored credential is not the account this run was frozen to")
            return
        self._ensured = True

    def _schedule_retry(self, now: float) -> None:
        jitter = 0.5 + 0.5 * self._rng()
        self._next_ensure_at = now + self._ensure_delay * jitter
        self._ensure_delay = min(self._ensure_delay * 2, ENSURE_BACKOFF_CAP)

    def _send_pending(self) -> bool:
        """Everything the server has not acknowledged, oldest first, in batches.

        Read from the outbox against the contiguous ack, so a gap -- a batch the
        network dropped while later ones went through -- is filled before
        anything newer is pushed. Stops at the first batch the server does not
        take; the next wake starts again from the ack. True when there was
        anything to send.
        """
        pending = self._pending()
        for start in range(0, len(pending), MAX_BATCH):
            if not self._send([event.wire() for event in pending[start : start + MAX_BATCH]]):
                break
        return bool(pending)

    def _send(self, events: list[dict[str, Any]]) -> bool:
        """Post one batch. False when the server did not take it, or took it
        and moved nothing -- pushing the next batch would only repeat that."""
        try:
            return self._delivery.send(events) is not None
        except LiveApiError as e:
            self._handle_api_error(e)
            return False

    def _send_descriptions(self) -> None:
        """Post the descriptions collected so far, oldest first, once the outbox
        is caught up -- to the run the server named, or the client id it also
        resolves. Each is tried once: a description is not progress, nothing
        on disk depends on it, and the closing one repeats everything the
        opening one said. A refusal that retires the credential or the build
        is handled like any other; anything else costs one line, once.
        """
        if not self._descriptions or self._pending():
            # Nothing to say, or the link just dropped a batch: a description
            # would fail the same way, so it waits for the wake that catches up.
            return
        descriptions, self._descriptions = self._descriptions, []
        for patch in descriptions:
            if self._disabled:
                break  # a retired credential or build: the next would be refused the same way
            try:
                self._client.put_context(self._delivery.reference, patch)
            except LiveApiError as e:
                self._handle_api_error(e)
                if self._context_notice is None:
                    self._context_notice = f"live sync: this run's description was not recorded ({e})"

    def _handle_api_error(self, error: LiveApiError) -> None:
        if error.client_too_old:
            # The server will not talk to this build at all. Not a retry case:
            # its answer names the install command, so that is what is shown,
            # and the outbox is kept for the next build to deliver.
            self._disable("live sync off: " + (error.server_message or "this server needs a newer tp"))
            return
        if error.credential_rejected:
            # Stop using a credential the server refused. Deliberately no
            # fallback to another stored credential or another server: the
            # queue belongs to one account.
            self._disable("live sync off: this server rejected the CLI token")
        # Everything else -- offline, 5xx, rate limited -- is transient: the
        # events stay in the outbox, the next wake tries again, and whatever is
        # still undelivered at the end is reported once, by close().

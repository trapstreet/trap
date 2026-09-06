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
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trap.live.client import LiveApiError, LiveClient
from trap.live.identity import LiveSession
from trap.live.outbox import Outbox, OutboxError
from trap.models.results import CaseResult

#: Wait no longer than this for the queue to drain at the end of a run. The
#: report is already saved; the exit code is already decided. Unsent events
#: stay in the outbox rather than holding the process open.
FLUSH_TIMEOUT_SECONDS = 3.0

#: How many events one request may carry. Matches the server's batch limit.
MAX_BATCH = 100


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
    metrics = result.metrics
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
    ) -> None:
        self._client = client
        self._session = session
        self._outbox = outbox
        self._run_dir = run_dir
        # Ordinals, assigned once, in execution order. The mapping stays here:
        # the wire only ever carries the number.
        self._ordinals = {case_id: index + 1 for index, case_id in enumerate(case_ids)}
        self._cases_total = len(case_ids)
        self._queue: queue.Queue[object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._disabled = False
        self._notice: str | None = None
        self._stop = object()

    # -- lifecycle ---------------------------------------------------------

    @property
    def run_url(self) -> str:
        """The page for this run, printable before the server has answered.

        The server resolves a run by its client_run_id as well as by its own
        id, which is what lets the URL exist immediately instead of after a
        round trip that the first case would otherwise wait for.
        """
        reference = self._session.run_id or self._session.client_run_id
        return f"{self._client.server}/runs/{reference}"

    @property
    def client_run_id(self) -> str:
        """This run's global id — what the final report carries so the upload and
        the private session that watched the same execution name one run."""
        return self._session.client_run_id

    @property
    def notice(self) -> str | None:
        """A single short line to show the user, or None. Never more than one per run."""
        return self._notice

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
                f"live sync: {pending} progress event(s) not delivered — they stay in this run's outbox"
            )

    # -- observer callbacks -------------------------------------------------

    def on_case_start(self, case_id: str) -> None:
        ordinal = self._ordinals.get(case_id)
        if ordinal is not None:
            self._emit("case_started", {"ordinal": ordinal})

    def on_case_done(self, result: CaseResult) -> None:
        ordinal = self._ordinals.get(result.case_id)
        if ordinal is None:
            return
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
            self._queue.put_nowait(event.wire())
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

    def _pending_count(self) -> int:
        try:
            return len(
                self._outbox.pending(self._session.acked_seq, generation=self._session.producer_generation)
            )
        except Exception:  # pragma: no cover - defensive
            return 0

    def _pump(self) -> None:
        """Sender thread: open the session, then ship batches until told to stop."""
        self._ensure_session()
        while self._drain_once():
            pass

    def _drain_once(self) -> bool:
        """Block for one item, coalesce whatever else is queued, send it.

        Split out of the thread body so the batching rules are testable without
        racing a thread: everything here is synchronous given a filled queue.
        Returns False once the stop sentinel has been seen.
        """
        batch, stopping = self._collect()
        if batch:
            self._send(batch)
        return not stopping

    def _collect(self) -> tuple[list[dict[str, Any]], bool]:
        """One blocking take, then everything already waiting, up to a batch.

        Coalescing matters because a run emits several events per case; without
        it a twenty-case run costs sixty requests instead of a handful.
        """
        batch: list[dict[str, Any]] = []
        stopping = False
        item = self._queue.get()
        if item is self._stop:
            stopping = True
        else:
            batch.append(item)  # type: ignore[arg-type]
        while not stopping and len(batch) < MAX_BATCH:
            try:
                extra = self._queue.get_nowait()
            except queue.Empty:
                break
            if extra is self._stop:
                stopping = True
                break
            batch.append(extra)  # type: ignore[arg-type]
        return batch, stopping

    def _ensure_session(self) -> None:
        if self._disabled:
            return
        try:
            # Freeze the account this run belongs to before anything is sent.
            # Without it, events queued now could later be delivered under a
            # different login (see LiveSession.belongs_to).
            if self._session.user_id is None:
                self._session.user_id = self._client.whoami()
                self._persist()
            data = self._client.ensure_session(
                self._session.client_run_id,
                snapshot={"cases_total": self._cases_total},
                runtime={"orchestrator": "tp", "executor": "tp"},
            )
        except LiveApiError as e:
            self._handle_api_error(e)
            return
        run = data.get("run")
        if isinstance(run, dict) and isinstance(run.get("id"), str):
            self._session.run_id = run["id"]
            self._persist()

    def _send(self, events: list[dict[str, Any]]) -> None:
        if self._disabled:
            return
        reference = self._session.run_id or self._session.client_run_id
        try:
            data = self._client.send_events(reference, events)
        except LiveApiError as e:
            self._handle_api_error(e)
            return
        ack = data.get("ack_seq")
        if isinstance(ack, int) and ack > self._session.acked_seq:
            self._session.acked_seq = ack
            self._persist()

    def _handle_api_error(self, error: LiveApiError) -> None:
        if error.credential_rejected:
            # Stop using a credential the server refused. Deliberately no
            # fallback to another stored credential or another server: the
            # queue belongs to one account.
            self._disable("live sync off: this server rejected the CLI token")
            return
        # Everything else -- offline, 5xx, rate limited -- is transient. Say
        # nothing beyond one line, keep the events, let `tp sync` deal with it.
        if self._notice is None:
            self._notice = "live sync: progress not reaching the site — events kept locally"

    def _persist(self) -> None:
        try:
            self._session.save(self._run_dir)
        except Exception:  # pragma: no cover - defensive
            pass

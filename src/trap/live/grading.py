"""Site grading from `tp run`: tp submits the answers, the site judges them.

For a task the site has admitted as an evaluation, the local judge is only a
preview. Each case's answer -- the solver's stdout, nothing else -- goes to the
site as the case finishes, and the site scores it with the task's own judge
against reference answers that never leave its grading worker. The result the
leaderboard trusts is the site's; what this run's report says is what the
local judge thought.

The rules that shape this module:

*Fail-open, always.* No network, a refused revision, a lost connection midway:
the run proceeds exactly as it would have, the report is saved, the exit code
is what the local run earned. The only trace is a summary line and, when
something went wrong, one line saying what.

*Durable first, then sent.* An answer is recorded in the run's answers outbox
(:mod:`trap.live.answers`) before anything is sent, and a daemon thread hands
it over; the run's own thread never waits on the network. A request the site
did not take is retried with a backoff for as long as the run lasts, and
whatever is still unconfirmed when `tp run` exits is left for `tp sync`, which
resends it to the same graded run. The site answers a per-case receipt, so an
answer it skipped or rejected is said by name and never resent as if it were
new.

*One run on the site per run here.* The graded run's id is derived from the
live-sync session's (``<client_run_id>-site``): the site keys a session by
(owner, client_run_id) across BOTH channels, so the same id would collide with
the private progress session rather than join it. The report carries the
graded run's id and URL under ``site_grading``, which is the join. When there
is no live session, a fresh id is minted for the graded run alone.
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from trap.auth.resolve import ResolvedAuth
from trap.auth.store import CredentialStore, CredentialStoreError
from trap.live.answers import (
    UNREADABLE_ANSWER,
    AnswerOutbox,
    AnswerOutboxError,
    AnswerRecord,
    GradedRun,
    classify,
    describe,
    read_answer,
    resend,
    shortfall,
)
from trap.live.client import LiveApiError, LiveClient
from trap.live.delivery import tp_runtime
from trap.live.identity import new_client_run_id
from trap.live.tracker import FLUSH_TIMEOUT_SECONDS
from trap.models.provenance import GitProvenance
from trap.models.report import SiteGrading
from trap.models.results import CaseResult

#: The server-side channel a graded run must be on. Anything else means the id
#: landed on a session the site will not grade, and answers must not be sent.
GRADED_CHANNEL = "platform_grading"

#: How long the sender sleeps when nothing is queued. Wakes come from the run.
IDLE_WAIT_SECONDS = 10.0

#: While something is queued the sender never sleeps past its own backoff, and
#: never less than a tick -- so a one-second retry is not a ten-second one, and
#: the bounded flush at the end still gets its attempts in.
MIN_WAIT_SECONDS = 0.05
MAX_WAIT_SECONDS = 1.0

#: Retrying a request the site did not take: first wait, growth, ceiling.
#: Jittered, like the session open; a ``Retry-After`` wins when the site sent one.
SEND_BACKOFF_INITIAL = 1.0
SEND_BACKOFF_CAP = 30.0

#: What woke the sender: the run recorded an answer, close() was called, or
#: the wait ran out.
Wake = Literal["event", "stop", "idle"]


def site_grading_disabled_by_env() -> bool:
    """``TRAP_NO_SITE_GRADING`` keeps every run in this environment locally judged."""
    return os.environ.get("TRAP_NO_SITE_GRADING", "").strip().lower() in {"1", "true", "yes", "on"}


class SiteGrader:
    """Submits one run's answers to its graded run on the site, case by case.

    The run's thread only ever appends to the answers outbox and wakes the
    sender; the sender owns the network. So a slow or absent site costs the
    run nothing but the bounded flush at the end, and nothing an answer's
    journey does can reach the case that produced it.
    """

    def __init__(
        self,
        *,
        client: LiveClient,
        run_dir: Path,
        graded: GradedRun | None = None,
        ordinals: dict[str, int] | None = None,
        notice: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._run_dir = run_dir
        self._graded = graded
        self._ordinals = ordinals or {}
        self._outbox = AnswerOutbox(run_dir)
        self._notice = notice
        self._off = graded is None
        self._queue: queue.Queue[object] = queue.Queue()
        self._stop_token = object()
        self._thread: threading.Thread | None = None
        self._closing = False
        self._summary_line: str | None = None
        # Sender state, owned by the sender thread.
        self._clock = clock
        self._rng = rng
        self._next_send_at = 0.0
        self._delay = SEND_BACKOFF_INITIAL

    @property
    def opened(self) -> bool:
        """Whether the site opened a graded run for this one. False is the fail-open
        outcome: nothing will be submitted, and ``notice`` says why."""
        return self._graded is not None

    @property
    def url(self) -> str | None:
        return self._graded.url if self._graded is not None else None

    @property
    def notice(self) -> str | None:
        """One line for the user when something went wrong, or None. The first
        thing worth saying is kept."""
        return self._notice

    @property
    def summary_line(self) -> str | None:
        """What became of the answers, in one line; available after :meth:`close`."""
        return self._summary_line

    def summary(self) -> SiteGrading | None:
        """The block the report carries: where the site's verdicts live. Recorded
        whenever a graded run was opened, even if contact was lost later -- the
        run exists on the site either way."""
        if self._graded is None:
            return None
        return SiteGrading(run_id=self._graded.run_id, url=self._graded.url)

    def start(self) -> None:
        """Begin sending on a daemon thread. Never raises."""
        try:
            self._thread = threading.Thread(target=self._pump, name="trap-site-grading", daemon=True)
            self._thread.start()
        except Exception as e:  # pragma: no cover - defensive
            self._stop(f"site grading off ({e.__class__.__name__})")

    def on_case_done(self, result: CaseResult) -> None:
        """Record this case's answer and wake the sender. Never raises, never blocks."""
        if self._off or self._graded is None:
            return
        ordinal = self._ordinals.get(result.case_id)
        if ordinal is None:
            return
        try:
            record = self._record(result, ordinal)
            self._outbox.queue(record)
            self._queue.put_nowait(result.case_id)
        except AnswerOutboxError as e:
            self._stop(
                f"site grading: cannot write the answers outbox ({e}) — later answers are not submitted"
            )
        except Exception as e:  # pragma: no cover - defensive
            self._stop(f"site grading off ({e.__class__.__name__})")

    def close(self) -> None:
        """Stop sending after one bounded flush, and say what became of the answers."""
        in_flight = False
        if self._thread is not None:
            try:
                self._queue.put_nowait(self._stop_token)
                self._thread.join(timeout=FLUSH_TIMEOUT_SECONDS)
            except Exception:  # pragma: no cover - defensive
                pass
            in_flight = self._thread.is_alive()
        if in_flight:
            # A request is still out. Closing the client under it would only
            # turn its answer into an error; the thread closes it when it exits.
            self._closing = True
        else:
            self._client.close()
        self._summary_line = self._summarise()

    @staticmethod
    def _submission(result: CaseResult, answer: str) -> dict[str, Any]:
        """The wire shape of one answer -- kept for the contract test, which checks
        it against the fields the bulk route reads."""
        return AnswerRecord.queued(result, ordinal=0, answer=answer).submission(answer)

    # -- internals ----------------------------------------------------------

    def _record(self, result: CaseResult, ordinal: int) -> AnswerRecord:
        """The queued line for a case -- or, when its stdout cannot be read, a
        line already settled as unreadable, so the summary can name it."""
        try:
            answer = read_answer(self._run_dir, result.case_id)
        except (OSError, ValueError):
            return AnswerRecord.queued(result, ordinal=ordinal, answer="").settled(
                "unreadable", reason=UNREADABLE_ANSWER
            )
        return AnswerRecord.queued(result, ordinal=ordinal, answer=answer)

    def _stop(self, notice: str) -> None:
        self._off = True
        if self._notice is None:
            self._notice = notice

    def _pump(self) -> None:
        """Sender thread: keep draining until told to stop. Never lets an
        exception escape; closes the client itself when close() gave up waiting."""
        try:
            while self._drain_once():
                pass
        except Exception as e:
            self._stop(f"site grading off ({e.__class__.__name__})")
        finally:
            if self._closing:
                self._client.close()

    def _drain_once(self) -> bool:
        """One wake of the sender. Split out of the thread body so the rules are
        testable without racing a thread. Returns False once the stop token has
        been seen; the stop wake makes one last attempt regardless of the
        backoff, so the flush at the end is not lost to a timer."""
        wake = self._collect()
        if self._off:
            return wake != "stop"
        self._send_pending(force=wake == "stop")
        return wake != "stop"

    def _collect(self) -> Wake:
        """Block for the next wake, then swallow every wake already queued."""
        try:
            item = self._queue.get(timeout=self._wait())
        except queue.Empty:
            return "idle"
        while item is not self._stop_token:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return "event"
        return "stop"

    def _wait(self) -> float:
        """How long to sleep: the backoff, clamped, while anything is owed; a
        long idle wait otherwise."""
        if self._off or not self._outbox.pending():
            return IDLE_WAIT_SECONDS
        return min(max(self._next_send_at - self._clock(), MIN_WAIT_SECONDS), MAX_WAIT_SECONDS)

    def _send_pending(self, *, force: bool = False) -> None:
        """Hand over whatever is queued, if the backoff allows it now."""
        now = self._clock()
        if not force and now < self._next_send_at:
            return
        assert self._graded is not None  # _off guards the None case
        outcome = resend(self._client, self._graded, self._outbox, self._run_dir)
        if outcome.delivered:
            self._delay = SEND_BACKOFF_INITIAL  # progress resets the backoff
        if outcome.error is not None and classify(outcome.error) != "retry":
            self._stop(
                f"site grading: {describe(outcome.error, self._graded.run_id)} — "
                f"{outcome.remaining} answer(s) not submitted; kept in this run's answers outbox for tp sync"
            )
            return
        if outcome.remaining:
            self._schedule_retry(now, outcome.error)

    def _schedule_retry(self, now: float, error: LiveApiError | None) -> None:
        if error is not None and error.retry_after:
            wait = float(error.retry_after)
        else:
            wait = self._delay * (0.5 + 0.5 * self._rng())
        self._next_send_at = now + wait
        self._delay = min(self._delay * 2, SEND_BACKOFF_CAP)

    def _summarise(self) -> str | None:
        """One line: how many answers the site has, and what it will never have."""
        if self._graded is None:
            return None
        latest = list(self._outbox.latest().values())
        if not latest:
            return None
        by_state: dict[str, list[AnswerRecord]] = {}
        for record in latest:
            by_state.setdefault(record.state, []).append(record)
        submitted = len(by_state.get("accepted", [])) + len(by_state.get("duplicate", []))
        queued = len(by_state.get("queued", []))
        line = f"site grading: {submitted} of {len(latest)} answer(s) submitted"
        if queued:
            line += f"; {queued} not yet confirmed — kept in this run's answers outbox; run tp sync"
        return line + shortfall(
            rejected=[(r.case_id, r.reason or "REJECTED") for r in by_state.get("rejected", [])],
            skipped=[(r.case_id, r.reason or "SKIPPED") for r in by_state.get("skipped", [])],
            unreadable=[(r.case_id, r.reason or UNREADABLE_ANSWER) for r in by_state.get("unreadable", [])],
        )


def start_site_grading(
    *,
    task: GitProvenance,
    case_ids: Sequence[str],
    run_dir: Path,
    client_run_id: str | None = None,
    server_override: str | None = None,
    enabled: bool = True,
) -> SiteGrader | None:
    """Open a graded run on the site for this task, or return None with nothing said.

    None is the quiet path: switched off, not paired, a task with no git anchor
    (the site cannot know which task it is), or a task the site has not admitted
    for grading -- all ordinary, none worth a line. A server that *should* have
    answered but could not is the one case worth a note, and even then the run
    is unaffected: site grading is simply off for it.
    """
    if not enabled or site_grading_disabled_by_env():
        return None
    if task.repo is None or task.commit is None:
        return None
    try:
        auth = ResolvedAuth.resolve(CredentialStore(), server_override)
    except CredentialStoreError:
        return None
    if not auth.api_key:
        return None

    client = LiveClient(auth.server, auth.api_key)
    grader = _open(
        client,
        run_dir=run_dir,
        repo=task.repo,
        commit=task.commit,
        path=task.subdirectory,
        case_ids=list(case_ids),
        client_run_id=f"{client_run_id}-site" if client_run_id else new_client_run_id(),
        user_id=auth.user_id,
    )
    if grader is None:
        client.close()
    return grader


def _open(
    client: LiveClient,
    *,
    run_dir: Path,
    repo: str,
    commit: str,
    path: str | None,
    case_ids: list[str],
    client_run_id: str,
    user_id: str | None,
) -> SiteGrader | None:
    """Resolve the revision, open the run, write the sidecar, start the sender.

    A grader that never opened, with a notice, is the fail-open outcome for a
    server that answered wrongly or not at all; None is the quiet one."""
    try:
        revision = client.resolve_evaluation(repo=repo, commit=commit, path=path)
    except LiveApiError as e:
        if e.status == 404:
            return None  # no admitted evaluation for this task: the ordinary case
        return _unavailable(client, run_dir, _why(e, f"could not resolve the task ({e})"))
    revision_id = revision.get("revision_id")
    if revision.get("admitted") is not True or not isinstance(revision_id, str):
        return None

    try:
        opened = client.open_evaluation(
            revision_id=revision_id, client_run_id=client_run_id, runtime=tp_runtime()
        )
    except LiveApiError as e:
        return _unavailable(client, run_dir, _why(e, f"could not open a graded run ({e})"))
    run = opened.get("run")
    run_id = run.get("id") if isinstance(run, dict) else None
    if not isinstance(run, dict) or not isinstance(run_id, str):
        return _unavailable(client, run_dir, "the site answered without a run id")
    if run.get("channel", GRADED_CHANNEL) != GRADED_CHANNEL:
        # The id already names a session the site will not grade. Sending
        # answers there would be refused one by one; say it once instead.
        return _unavailable(client, run_dir, "the site holds this run id as a non-graded session")

    url = opened.get("view_url")
    if not isinstance(url, str):
        url = f"{client.server}/runs/{run_id}"
    site_total = revision.get("cases_total")
    graded = GradedRun(
        run_id=run_id,
        client_run_id=client_run_id,
        server=client.server,
        url=url,
        revision_id=revision_id,
        user_id=user_id,
        cases_total=site_total if isinstance(site_total, int) else None,
    )
    try:
        # Both on disk before the first case: a crash one line later still
        # leaves a run `tp sync` can finish.
        AnswerOutbox(run_dir).prepare()
        graded.save(run_dir)
    except (AnswerOutboxError, OSError) as e:
        return _unavailable(client, run_dir, f"cannot write the answers outbox ({e})")

    notice = None
    if graded.cases_total is not None and graded.cases_total != len(case_ids):
        notice = (
            f"site grading: the site's evaluation has {graded.cases_total} case(s) and this run covers "
            f"{len(case_ids)} — the graded run stays unfinished until every case is answered"
        )
    ordinals = {case_id: index + 1 for index, case_id in enumerate(case_ids)}
    grader = SiteGrader(client=client, run_dir=run_dir, graded=graded, ordinals=ordinals, notice=notice)
    grader.start()
    return grader


def _why(error: LiveApiError, fallback: str) -> str:
    """A server that refuses this build says so in its own words -- they name the
    install command -- and those are worth more than ``http 426``."""
    if error.client_too_old:
        return error.server_message or "this server needs a newer tp"
    return fallback


def _unavailable(client: LiveClient, run_dir: Path, reason: str) -> SiteGrader:
    """A grader that will submit nothing and says why, once. The run is judged
    locally, and nothing is kept to send later."""
    notice = f"site grading off for this run: {reason} — answers are judged locally only"
    return SiteGrader(client=client, run_dir=run_dir, notice=notice)

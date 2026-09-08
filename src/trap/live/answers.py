"""The answers outbox: what `tp run` handed the site to grade, and what the site said.

Site grading used to be a conversation with no memory: an answer the site did
not take when its case finished was gone, and the first failure ended
submissions for the run. This module gives that path the same shape live
progress already has -- a durable record on disk, written before anything is
sent, and a receipt per case once the site has answered -- so a dropped request
is retried while the run lasts, and `tp sync` can hand over whatever was still
unconfirmed when it exited.

Two files inside ``<run_dir>/live/``:

``grading.json``
    The graded run this run's answers belong to: the site's id and URL, the
    server, the revision, and the account it was opened under. Written the
    moment the site opens the run.

``answers.jsonl``
    One line per state change of one case's answer, append-only. The **answer
    text is never copied here**: it is ``<run_dir>/<case>/solution/stdout``,
    immutable once the case ran, and a resend re-reads that file and checks
    its digest. The queued line keeps only what the wire needs beside the
    answer -- timing, exit code, self-declared cost -- plus the answer's sha256.
    The last line for a case is its current state.

Nothing here decides what a run does. An outbox that cannot be written turns
site grading off for the run; a receipt only changes what the summary says.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ValidationError

from trap.live.client import LiveApiError, LiveClient
from trap.live.tracker import MAX_BATCH
from trap.models.results import CaseResult
from trap.runner.layout import CaseLayout

#: Where one case's answer is, from this side's point of view.
AnswerState = Literal["queued", "accepted", "duplicate", "rejected", "skipped", "unreadable"]

#: What the site can say about one answer in a bulk receipt (``results[].status``).
#: The contract test checks these against the web source.
RECEIPT_STATUSES: frozenset[str] = frozenset({"accepted", "duplicate", "rejected", "skipped"})

#: Every reason the site can give for an answer it did not accept -- the
#: per-case ``error`` of its submit path plus the two bulk-only skips. The
#: contract test checks these against the web source; the summary quotes them.
KNOWN_REASONS: frozenset[str] = frozenset(
    {
        "NO_SUCH_CASE",
        "ALREADY_ANSWERED",
        "AUTHORITATIVE_FIELD",
        "ARTIFACT_TOO_LARGE",
        "STALE_LEASE",
        "SOLVER_ERRORED",
        "NO_ANSWER",
    }
)

#: Reasons this side names itself, for an answer it could not rebuild from disk.
UNREADABLE_ANSWER = "UNREADABLE_ANSWER"
ANSWER_CHANGED = "ANSWER_CHANGED"

#: What a failed request means for the sender: try again later, stop using
#: this credential, or stop because the site will never take these answers.
Failure = Literal["retry", "credential", "structural"]


def read_answer(run_dir: Path, case_id: str) -> str:
    """The case's answer: the solver's stdout as `tp run` captured it. Raises
    ``OSError`` when the file is missing and ``ValueError`` when it is not text."""
    return CaseLayout.for_case(run_dir, case_id).solution_capture.stdout.read_text()


def sha256_of(answer: str) -> str:
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


class AnswerUnreadable(Exception):
    """The answer on disk is not the one that was queued, or is not there."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class GradedRun(BaseModel):
    """The sidecar ``live/grading.json``: the graded run this run's answers go to."""

    run_id: str
    client_run_id: str
    server: str
    url: str
    revision_id: str
    #: The account the run was opened under, frozen from the credential's stored
    #: id. None when the pairing was never verified; `tp sync --claim` fills it.
    user_id: str | None = None
    cases_total: int | None = None

    FILENAME: ClassVar[str] = "grading.json"

    @classmethod
    def path_in(cls, run_dir: Path) -> Path:
        return run_dir / "live" / cls.FILENAME

    @classmethod
    def load(cls, run_dir: Path) -> GradedRun | None:
        """The sidecar, or None when absent or unreadable -- a corrupt one must
        never stop `tp sync` from handling the progress half."""
        try:
            return cls.model_validate_json(cls.path_in(run_dir).read_text())
        except (OSError, ValidationError, ValueError):
            return None

    def save(self, run_dir: Path) -> None:
        path = self.path_in(run_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.model_dump(), indent=2))
        tmp.replace(path)


class AnswerRecord(BaseModel):
    """One line of ``answers.jsonl``: a case's answer at one point in its life."""

    case_id: str
    ordinal: int
    state: AnswerState
    at: str
    duration: float
    exit_code: int
    client_reported: dict[str, float | int]
    answer_sha256: str
    reason: str | None = None
    digest: str | None = None

    @classmethod
    def queued(cls, result: CaseResult, *, ordinal: int, answer: str) -> AnswerRecord:
        """The record for a case that just finished: the report's own fields plus
        what the site records as self-declared, labelled as the client's word."""
        reported: dict[str, float | int] = {"duration_ms": int(result.duration * 1000)}
        if result.cost is not None and result.cost.cost_usd is not None:
            reported["cost_usd"] = result.cost.cost_usd
        return cls(
            case_id=result.case_id,
            ordinal=ordinal,
            state="queued",
            at=_now(),
            duration=result.duration,
            exit_code=result.exit_code,
            client_reported=reported,
            answer_sha256=sha256_of(answer),
        )

    def settled(
        self, state: AnswerState, *, reason: str | None = None, digest: str | None = None
    ) -> AnswerRecord:
        return self.model_copy(update={"state": state, "at": _now(), "reason": reason, "digest": digest})

    def submission(self, answer: str) -> dict[str, Any]:
        """The wire shape: the report's ``cases_results`` entry plus ``client_reported``."""
        return {
            "case_id": self.case_id,
            "answer": answer,
            "duration": self.duration,
            "exit_code": self.exit_code,
            "client_reported": dict(self.client_reported),
        }

    def wire(self, run_dir: Path) -> dict[str, Any]:
        """Rebuild the exact submission from the run directory.

        The answer is re-read rather than stored twice; the digest check is what
        makes a resend byte-identical -- a stdout that changed underneath the
        queue is refused here, not sent as a different answer.
        """
        try:
            answer = read_answer(run_dir, self.case_id)
        except (OSError, ValueError) as e:
            raise AnswerUnreadable(UNREADABLE_ANSWER) from e
        if sha256_of(answer) != self.answer_sha256:
            raise AnswerUnreadable(ANSWER_CHANGED)
        return self.submission(answer)


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnswerOutboxError(Exception):
    """The answers outbox could not be written. Site grading gives up; the run continues."""


class AnswerOutbox:
    """Append-only record of one run's answers, one line per state change.

    Thread-safe the same way the events outbox is: the run's thread queues,
    the sender settles, and the lock covers each append together with the
    read that decides whether to make it.
    """

    FILENAME = "answers.jsonl"

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "live" / self.FILENAME
        self._lock = Lock()

    def prepare(self) -> None:
        """Create the directory and the file. Called once, before the first case."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError as e:
            raise AnswerOutboxError(str(e)) from e

    def queue(self, record: AnswerRecord) -> bool:
        """Record a case's answer, once. False when the case already has a record:
        a second line would reset a settled case to queued and force a resend."""
        with self._lock:
            if record.case_id in self.latest():
                return False
            self._append(record)
            return True

    def settle(
        self,
        case_id: str,
        state: AnswerState,
        *,
        reason: str | None = None,
        digest: str | None = None,
    ) -> None:
        """Append what became of a queued answer. A case never queued is ignored."""
        with self._lock:
            current = self.latest().get(case_id)
            if current is not None:
                self._append(current.settled(state, reason=reason, digest=digest))

    def read_all(self) -> list[AnswerRecord]:
        return list(self._iter_records())

    def latest(self) -> dict[str, AnswerRecord]:
        """Each case's current state, in the order the cases were first queued."""
        current: dict[str, AnswerRecord] = {}
        for record in self._iter_records():
            current[record.case_id] = record
        return current

    def pending(self) -> list[AnswerRecord]:
        """Answers the site has not settled, in execution order."""
        return sorted(
            (record for record in self.latest().values() if record.state == "queued"),
            key=lambda record: record.ordinal,
        )

    def _append(self, record: AnswerRecord) -> None:
        line = json.dumps(record.model_dump(), separators=(",", ":"))
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        except OSError as e:
            raise AnswerOutboxError(str(e)) from e

    def _iter_records(self) -> Iterator[AnswerRecord]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                yield AnswerRecord.model_validate_json(line)
            except (ValidationError, ValueError):
                # A half-written final line from a killed process: one state
                # change lost, and the case simply reads as it was before.
                continue


# -- receipts ---------------------------------------------------------------


@dataclass
class Settled:
    """What one receipt did to the cases a request carried."""

    accepted: list[str] = field(default_factory=list)
    duplicate: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: Sent, but the site named no outcome for them: they stay queued.
    unsettled: list[str] = field(default_factory=list)

    @property
    def any(self) -> bool:
        return bool(self.accepted or self.duplicate or self.rejected or self.skipped)


def apply_receipt(outbox: AnswerOutbox, sent: list[AnswerRecord], body: dict[str, Any]) -> Settled:
    """Settle each sent case by what the site said about it.

    The per-case ``results`` array is authoritative when present. An older
    server answers with counts only, plus the ``rejected`` and ``skipped``
    lists; then the cases it did not name are taken as accepted **only when**
    ``accepted + duplicates`` is exactly their number -- a body that does not
    tally (or an empty one) settles nothing, and the answers stay queued for
    the next wake, where the site answers "duplicate" at worst.
    """
    verdicts: dict[str, tuple[str, str | None, str | None]] = {}
    wanted = {record.case_id for record in sent}
    results = body.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            case_id, status = item.get("case_id"), item.get("status")
            if case_id in wanted and status in RECEIPT_STATUSES:
                verdicts[case_id] = (status, _text(item.get("reason")), _text(item.get("digest")))
    else:
        for status in ("rejected", "skipped"):
            listed = body.get(status)
            for item in listed if isinstance(listed, list) else []:
                if isinstance(item, dict) and item.get("case") in wanted:
                    verdicts[item["case"]] = (status, _text(item.get("reason")), None)
        unnamed = [case_id for case_id in wanted if case_id not in verdicts]
        if _count(body, "accepted") + _count(body, "duplicates") == len(unnamed):
            for case_id in unnamed:
                verdicts[case_id] = ("accepted", None, None)

    settled = Settled()
    for record in sent:
        verdict = verdicts.get(record.case_id)
        if verdict is None:
            settled.unsettled.append(record.case_id)
            continue
        state, reason, digest = verdict
        outbox.settle(record.case_id, state, reason=reason, digest=digest)  # type: ignore[arg-type]
        if state == "accepted":
            settled.accepted.append(record.case_id)
        elif state == "duplicate":
            settled.duplicate.append(record.case_id)
        elif state == "rejected":
            settled.rejected.append((record.case_id, reason or "REJECTED"))
        else:
            settled.skipped.append((record.case_id, reason or "SKIPPED"))
    return settled


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(body: dict[str, Any], key: str) -> int:
    value = body.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


# -- failures ---------------------------------------------------------------


def classify(error: LiveApiError) -> Failure:
    """What to do about a request the site did not take.

    Only being unreachable, a timeout, a rate limit and a server error are
    worth another try. A rejected token retires the credential. Everything
    else -- including a build the server refuses -- is a fact about this run
    that another request cannot change.
    """
    if error.client_too_old:
        return "structural"
    if error.status == 401:
        return "credential"
    if error.status is None or error.status in (408, 429) or error.status >= 500:
        return "retry"
    return "structural"


def describe(error: LiveApiError, run_id: str) -> str:
    """Why the site would not take the answers, in words a user can act on."""
    if error.client_too_old:
        return error.server_message or "this server needs a newer tp"
    if error.status == 401:
        return "the site rejected the CLI token"
    if error.status == 404:
        # The route is owner-scoped: a deleted run and a token that now belongs
        # to another account look the same from here, and both mean the same.
        return f"the site holds no graded run {run_id} for this account"
    return f"the site refused the answers ({error.server_message or error})"


# -- resending --------------------------------------------------------------


@dataclass
class ResendOutcome:
    """What one pass over the queue did."""

    delivered: int = 0
    remaining: int = 0
    rejected: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    error: LiveApiError | None = None


def resend(
    client: LiveClient,
    graded: GradedRun,
    outbox: AnswerOutbox,
    run_dir: Path,
    *,
    batch: int = MAX_BATCH,
) -> ResendOutcome:
    """Post everything still queued, oldest first, in batches, settling each by
    its receipt. Stops at the first request the site does not take, and at the
    first receipt that settles nothing -- the next wake starts again from the
    queue. Never raises; the failure travels in the outcome."""
    pending = outbox.pending()
    outcome = ResendOutcome()
    for start in range(0, len(pending), batch):
        wire: list[dict[str, Any]] = []
        sent: list[AnswerRecord] = []
        for record in pending[start : start + batch]:
            try:
                wire.append(record.wire(run_dir))
            except AnswerUnreadable as e:
                outbox.settle(record.case_id, "unreadable", reason=e.reason)
                outcome.unreadable.append((record.case_id, e.reason))
                continue
            sent.append(record)
        if not sent:
            continue
        try:
            body = client.submit_answers(graded.run_id, wire)
        except LiveApiError as e:
            outcome.error = e
            break
        settled = apply_receipt(outbox, sent, body)
        outcome.delivered += len(settled.accepted) + len(settled.duplicate)
        outcome.rejected.extend(settled.rejected)
        outcome.skipped.extend(settled.skipped)
        if not settled.any:
            break
    outcome.remaining = len(outbox.pending())
    return outcome


def shortfall(
    rejected: list[tuple[str, str]],
    skipped: list[tuple[str, str]],
    unreadable: list[tuple[str, str]],
) -> str:
    """The cases the site will never grade from this run, for a summary line:
    empty when there are none, else the lists and what they mean."""
    parts = [
        f"{len(items)} {label} ({_listed(items)})"
        for label, items in (
            ("skipped by the site", skipped),
            ("rejected", rejected),
            ("unreadable here", unreadable),
        )
        if items
    ]
    if not parts:
        return ""
    return "; " + "; ".join(parts) + " — the site's run stays unfinished"


def _listed(items: list[tuple[str, str]], limit: int = 5) -> str:
    shown = ", ".join(f"{case_id}: {reason}" for case_id, reason in items[:limit])
    return shown + (", …" if len(items) > limit else "")

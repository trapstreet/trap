"""Tests for site grading from `tp run`: answers submitted, the site judges.

The load-bearing property is the same as live sync's: nothing here may change
what the run does. Every failure -- no revision, no network, a lost connection
after the third case, an answer file that cannot be read -- ends with the run's
report and exit code exactly as a locally judged run would have them, the
answers the site never confirmed still on disk for `tp sync`, and one line
saying what happened.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx
import pytest

from trap import __version__
from trap.live.answers import UNREADABLE_ANSWER, AnswerOutbox, GradedRun
from trap.live.client import LiveApiError, LiveClient
from trap.live.grading import (
    IDLE_WAIT_SECONDS,
    MAX_WAIT_SECONDS,
    MIN_WAIT_SECONDS,
    SEND_BACKOFF_CAP,
    SiteGrader,
    site_grading_disabled_by_env,
    start_site_grading,
)
from trap.models.cost import CaseCost, ModelCost
from trap.models.provenance import GitProvenance
from trap.models.results import CaseResult

ANCHORED = GitProvenance(repo="https://github.com/org/task", commit="abc123", subdirectory="tasks/a")
TP_RUNTIME = {"orchestrator": "tp", "executor": "tp", "trap_version": __version__}
_ADMITTED = {"revision_id": "ev_1", "cases_total": 2, "admitted": True}
ANSWERS = {"c1": "forty-two", "c2": "x"}


class _Site:
    """A LiveClient stand-in: scripted answers to the three grading calls.

    ``submits`` is consumed one request at a time: a ``LiveApiError`` is
    raised, a dict is returned as the body. Once it runs out the site answers a
    counts-only ``{"accepted": 1}`` -- the shape an older server sends -- or,
    with ``receipts``, a per-case ``results`` receipt accepting everything.
    """

    def __init__(
        self,
        *,
        resolve: object | None = None,
        opened: object | None = None,
        submits: list[object] | None = None,
        receipts: bool = False,
        context_error: LiveApiError | None = None,
    ) -> None:
        self.server = "https://srv"
        self._resolve = resolve if resolve is not None else _ADMITTED
        self._opened = (
            opened
            if opened is not None
            else {"run": {"id": "rs_9", "channel": "platform_grading"}, "view_url": "https://srv/runs/rs_9"}
        )
        self._submits = list(submits or [])
        self._receipts = receipts
        self._context_error = context_error
        self.resolved: list[dict] = []
        self.opens: list[dict] = []
        self.submissions: list[tuple[str, list[dict]]] = []
        self.contexts: list[tuple[str, dict]] = []
        self.order: list[str] = []
        self.closed = False

    def resolve_evaluation(self, **kwargs):
        self.resolved.append(kwargs)
        if isinstance(self._resolve, LiveApiError):
            raise self._resolve
        return self._resolve

    def open_evaluation(self, **kwargs):
        self.opens.append(kwargs)
        if isinstance(self._opened, LiveApiError):
            raise self._opened
        return self._opened

    def put_context(self, run_id, patch):
        if self._context_error is not None:
            raise self._context_error
        self.contexts.append((run_id, patch))
        self.order.append("context")
        return {"ok": True}

    def submit_answers(self, run_id, cases_results):
        self.submissions.append((run_id, cases_results))
        self.order.append("answers")
        if self._submits:
            answer = self._submits.pop(0)
            if isinstance(answer, LiveApiError):
                raise answer
            return answer
        if self._receipts:
            return {
                "results": [
                    {"case_id": c["case_id"], "status": "accepted", "digest": "d"} for c in cases_results
                ]
            }
        return {"accepted": 1}

    def close(self) -> None:
        self.closed = True


class _Store:
    def __init__(self, key: str | None = "k", user_id: str | None = "usr_a") -> None:
        self._key = key
        self._user_id = user_id

    def load(self, server):
        from trap.auth.store import Credential

        if self._key is None:
            return None
        return Credential(server=server, api_key=self._key, user_id=self._user_id)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _write_answers(run_dir: Path, answers: dict[str, str] = ANSWERS) -> None:
    for case_id, text in answers.items():
        path = run_dir / case_id / "solution" / "stdout"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _start(
    monkeypatch,
    tmp_path: Path,
    site: _Site | None = None,
    *,
    task=ANCHORED,
    store=None,
    threaded: bool = False,
    answers: dict[str, str] | None = ANSWERS,
    **kwargs,
):
    """Open grading against a scripted site. The sender thread is not started
    unless asked: the synchronous tests drive ``_drain_once`` themselves."""
    monkeypatch.delenv("TRAP_NO_SITE_GRADING", raising=False)
    site = site or _Site()
    monkeypatch.setattr("trap.live.grading.CredentialStore", lambda: store or _Store())
    monkeypatch.setattr("trap.live.grading.LiveClient", lambda *_a, **_k: site)
    if not threaded:
        monkeypatch.setattr(SiteGrader, "start", lambda self: None)
    if answers:
        _write_answers(tmp_path, answers)
    grader = start_site_grading(task=task, case_ids=["c1", "c2"], run_dir=tmp_path, **kwargs)
    return grader, site


def _drain(grader: SiteGrader) -> bool:
    """One synchronous wake of the sender, without waiting on a timer."""
    grader._queue.put_nowait("wake")
    return grader._drain_once()


def _states(run_dir: Path) -> dict[str, str]:
    return {case_id: record.state for case_id, record in AnswerOutbox(run_dir).latest().items()}


def _result(case_id: str = "c1", **kwargs) -> CaseResult:
    return CaseResult(**{"case_id": case_id, "metrics": None, **kwargs})


def test_the_submission_wire_shape_is_the_report_entry_plus_client_reported():
    # The contract test checks these keys against the web route; this one
    # runs everywhere the contract test cannot (no sibling web checkout).
    wire = SiteGrader._submission(_result("c1", exit_code=0, duration=1.5), "42")
    assert wire["case_id"] == "c1" and wire["answer"] == "42"
    assert wire["duration"] == 1.5 and wire["exit_code"] == 0
    assert isinstance(wire["client_reported"], dict)
    assert not {"score", "verdict", "passed", "metrics"} & wire.keys()


# -- when grading is on -------------------------------------------------------


def test_an_admitted_task_opens_a_graded_run_under_an_id_derived_from_the_live_one(monkeypatch, tmp_path):
    # Not the SAME id: the site keys sessions by (owner, client_run_id) across
    # both channels, so reusing the live id would collide with the private
    # progress session instead of joining it.
    grader, site = _start(monkeypatch, tmp_path, client_run_id="r-1")
    assert grader is not None and grader.opened
    assert grader.url == "https://srv/runs/rs_9"
    assert grader.notice is None
    assert site.resolved == [{"repo": ANCHORED.repo, "commit": "abc123", "path": "tasks/a"}]
    assert site.opens == [
        {"revision_id": "ev_1", "client_run_id": "r-1-site", "runtime": TP_RUNTIME, "context": None}
    ]
    summary = grader.summary()
    assert summary is not None and summary.model_dump() == {"run_id": "rs_9", "url": "https://srv/runs/rs_9"}


def test_without_a_live_session_a_fresh_id_is_minted(monkeypatch, tmp_path):
    _grader, site = _start(monkeypatch, tmp_path)
    assert site.opens[0]["client_run_id"].startswith("r-")


def test_opening_a_graded_run_reports_the_tp_version(monkeypatch, tmp_path):
    _grader, site = _start(monkeypatch, tmp_path)
    assert site.opens[0]["runtime"] == TP_RUNTIME
    assert site.opens[0]["runtime"]["trap_version"] == __version__


def test_the_graded_run_is_on_disk_before_the_first_case(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, client_run_id="r-1")
    assert grader is not None
    graded = GradedRun.load(tmp_path)
    assert graded is not None
    assert graded.model_dump() == {
        "run_id": "rs_9",
        "client_run_id": "r-1-site",
        "server": "https://srv",
        "url": "https://srv/runs/rs_9",
        "revision_id": "ev_1",
        "user_id": "usr_a",  # frozen from the pairing, no network call
        "cases_total": 2,
    }
    assert AnswerOutbox(tmp_path).path.is_file()


def test_each_answer_is_the_solvers_stdout_in_the_reports_own_shape(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader.on_case_done(
        CaseResult(
            case_id="c1",
            exit_code=0,
            duration=1.25,
            metrics={"score": 1.0, "expected": "SENTINEL"},
            cost=CaseCost(by_model=[ModelCost(provider="anthropic", cost_usd=0.02)]),
        )
    )
    _drain(grader)
    grader.on_case_done(CaseResult(case_id="c2", exit_code=1, duration=0.5, metrics=None))
    _drain(grader)
    assert [run_id for run_id, _ in site.submissions] == ["rs_9", "rs_9"]
    first, second = (batch[0] for _, batch in site.submissions)
    assert first == {
        "case_id": "c1",
        "answer": "forty-two",
        "duration": 1.25,
        "exit_code": 0,
        "client_reported": {"duration_ms": 1250, "cost_usd": 0.02},
    }
    # An errored solver's case is still reported -- the site skips it itself
    # and keeps the case visibly unanswered -- and an unknown cost is absent.
    assert second["exit_code"] == 1 and second["client_reported"] == {"duration_ms": 500}
    # Nothing the judge said crosses: no score, no metrics, no expected answer.
    assert "SENTINEL" not in json.dumps(site.submissions) and "score" not in json.dumps(site.submissions)
    assert _states(tmp_path) == {"c1": "accepted", "c2": "accepted"}


def test_the_answer_text_is_never_copied_into_the_outbox(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader.on_case_done(_result("c1"))
    on_disk = AnswerOutbox(tmp_path).path.read_text()
    assert "forty-two" not in on_disk and "answer_sha256" in on_disk


def test_a_burst_of_answers_goes_in_one_request(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(receipts=True))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2"))
    _drain(grader)
    assert [[c["case_id"] for c in batch] for _, batch in site.submissions] == [["c1", "c2"]]
    assert _states(tmp_path) == {"c1": "accepted", "c2": "accepted"}


def test_a_view_url_the_site_omits_is_derived(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(opened={"run": {"id": "rs_9"}}))
    assert grader is not None and grader.url == "https://srv/runs/rs_9"


def test_a_case_count_that_differs_from_the_sites_is_said_once(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(resolve={**_ADMITTED, "cases_total": 5}))
    assert grader is not None and grader.opened
    assert grader.notice is not None and "5 case(s)" in grader.notice and "covers 2" in grader.notice


def test_a_case_count_the_site_does_not_give_is_not_compared(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(resolve={"revision_id": "ev_1", "admitted": True}))
    assert grader is not None and grader.notice is None
    graded = GradedRun.load(tmp_path)
    assert graded is not None and graded.cases_total is None


def test_close_releases_the_client(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader.close()
    assert site.closed is True


# -- the quiet paths: None, and nothing said ------------------------------------


def test_the_flag_turns_it_off(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, enabled=False)
    assert grader is None and site.resolved == []


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_env_switch_turns_it_off(monkeypatch, tmp_path, value):
    monkeypatch.setenv("TRAP_NO_SITE_GRADING", value)
    assert site_grading_disabled_by_env() is True
    monkeypatch.setattr("trap.live.grading.CredentialStore", lambda: _Store())
    assert start_site_grading(task=ANCHORED, case_ids=["c1"], run_dir=tmp_path) is None


def test_other_env_values_leave_it_on(monkeypatch):
    monkeypatch.setenv("TRAP_NO_SITE_GRADING", "0")
    assert site_grading_disabled_by_env() is False


@pytest.mark.parametrize(
    "task",
    [GitProvenance(), GitProvenance(repo="https://github.com/org/task"), GitProvenance(commit="abc")],
)
def test_an_unanchored_task_cannot_be_graded(monkeypatch, tmp_path, task):
    grader, site = _start(monkeypatch, tmp_path, task=task)
    assert grader is None and site.resolved == []


def test_an_unpaired_cli_grades_nothing(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, store=_Store(key=None))
    assert grader is None


def test_an_unreadable_credential_file_is_not_a_run_problem(monkeypatch, tmp_path):
    from trap.auth.store import CredentialStoreError

    class _Broken:
        def load(self, _server):
            raise CredentialStoreError("bad file")

    grader, _site = _start(monkeypatch, tmp_path, store=_Broken())
    assert grader is None


def test_a_task_the_site_has_not_admitted_is_the_ordinary_case(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(resolve=LiveApiError("http 404", status=404)))
    assert grader is None
    assert site.opens == [] and site.closed is True


@pytest.mark.parametrize(
    "resolve",
    [
        {"revision_id": "ev_1", "cases_total": 2, "admitted": False},
        {"cases_total": 2, "admitted": True},
        {"revision_id": 7, "admitted": True},
    ],
)
def test_a_revision_that_is_not_admitted_or_not_named_is_not_used(monkeypatch, tmp_path, resolve):
    grader, site = _start(monkeypatch, tmp_path, _Site(resolve=resolve))
    assert grader is None and site.opens == []


# -- fail-open: a grader that never opened, and says why --------------------------


def test_an_unreachable_site_at_start_turns_grading_off_for_the_run(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(resolve=LiveApiError("unreachable")))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "could not resolve" in grader.notice
    assert "judged locally only" in grader.notice
    assert grader.summary() is None and grader.url is None
    # Nothing is submitted, and nothing is queued for later.
    grader.on_case_done(_result("c1"))
    assert site.submissions == []
    assert not AnswerOutbox(tmp_path).path.exists()
    grader.close()
    assert grader.summary_line is None


def test_a_run_the_site_will_not_open_is_reported(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(opened=LiveApiError("http 503", status=503)))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "could not open a graded run" in grader.notice


TOO_OLD = LiveApiError(
    "http 426",
    status=426,
    payload={"code": "CLIENT_TOO_OLD", "error": "Install the pinned tp build: uv tool install --force X"},
)


def test_a_server_that_refuses_this_build_is_quoted_once_and_grading_is_off(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(opened=TOO_OLD))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "Install the pinned tp build" in grader.notice
    assert "http 426" not in grader.notice
    grader.on_case_done(_result("c1"))
    assert site.submissions == []


def test_a_refusal_at_resolve_time_is_quoted_too(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(resolve=TOO_OLD))
    assert grader is not None and grader.notice is not None
    assert "Install the pinned tp build" in grader.notice


def test_a_refusal_without_words_still_says_what_it_is(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(opened=LiveApiError("http 426", status=426)))
    assert grader is not None and grader.notice is not None
    assert "this server needs a newer tp" in grader.notice


def test_an_answer_without_a_run_id_is_reported(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(opened={"view_url": "https://srv/runs/x"}))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "without a run id" in grader.notice


def test_an_id_the_site_holds_as_a_local_session_is_not_submitted_to(monkeypatch, tmp_path):
    # The live session and the graded run share one client_run_id. If the site
    # answers with the local session instead, every submission would be refused;
    # better to say so once and grade locally.
    grader, site = _start(
        monkeypatch, tmp_path, _Site(opened={"run": {"id": "rs_1", "channel": "local_report"}})
    )
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "non-graded session" in grader.notice
    grader.on_case_done(_result("c1"))
    assert site.submissions == []


def test_an_unwritable_answers_outbox_turns_grading_off(monkeypatch, tmp_path):
    def denied(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", denied)
    grader, _site = _start(monkeypatch, tmp_path, answers=None)
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "cannot write the answers outbox" in grader.notice


# -- the sender: retries, receipts, and stopping for good --------------------------


def test_a_transient_failure_keeps_the_answer_queued_and_retries_after_a_backoff(monkeypatch, tmp_path):
    clock = _Clock()
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("unreachable")]))
    assert grader is not None
    grader._clock, grader._rng = clock, lambda: 1.0
    grader.on_case_done(_result("c1"))
    _drain(grader)
    assert len(site.submissions) == 1
    assert grader.notice is None  # transient: nothing to say
    assert _states(tmp_path) == {"c1": "queued"}
    assert grader._next_send_at == 1.0

    _drain(grader)  # the backoff has not elapsed: no request
    assert len(site.submissions) == 1
    clock.now = 1.5
    _drain(grader)
    assert len(site.submissions) == 2 and _states(tmp_path) == {"c1": "accepted"}
    assert site.submissions[1][1][0]["answer"] == "forty-two"  # byte-identical, re-read


def test_the_retry_backs_off_with_jitter_up_to_a_cap(monkeypatch, tmp_path):
    clock = _Clock()
    grader, _site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("unreachable")] * 8))
    assert grader is not None
    grader._clock, grader._rng = clock, lambda: 1.0
    grader.on_case_done(_result("c1"))
    waits: list[float] = []
    for _ in range(8):
        _drain(grader)
        waits.append(grader._next_send_at - clock.now)
        clock.now = grader._next_send_at
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30]
    assert max(waits) == SEND_BACKOFF_CAP


def test_a_retry_after_from_the_site_is_honoured(monkeypatch, tmp_path):
    clock = _Clock()
    grader, _site = _start(
        monkeypatch, tmp_path, _Site(submits=[LiveApiError("http 429", status=429, retry_after=7)])
    )
    assert grader is not None
    grader._clock = clock
    grader.on_case_done(_result("c1"))
    _drain(grader)
    assert grader._next_send_at == 7.0


def test_progress_resets_the_backoff(monkeypatch, tmp_path):
    clock = _Clock()
    site = _Site(submits=[LiveApiError("unreachable")] * 3, receipts=True)
    grader, _site = _start(monkeypatch, tmp_path, site)
    assert grader is not None
    grader._clock, grader._rng = clock, lambda: 1.0
    grader.on_case_done(_result("c1"))
    for _ in range(3):
        _drain(grader)
        clock.now = grader._next_send_at
    assert grader._delay == 8.0
    _drain(grader)  # lands
    assert grader._delay == 1.0 and _states(tmp_path) == {"c1": "accepted"}


def test_a_counts_only_body_that_does_not_tally_leaves_answers_queued(monkeypatch, tmp_path):
    clock = _Clock()
    grader, site = _start(monkeypatch, tmp_path)  # answers {"accepted": 1} to everything
    assert grader is not None
    grader._clock, grader._rng = clock, lambda: 1.0
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2"))
    _drain(grader)  # two sent, one counted: which one? neither is claimed
    assert len(site.submissions) == 1
    assert _states(tmp_path) == {"c1": "queued", "c2": "queued"}
    assert grader._next_send_at == 1.0  # tried again later, where the site says "duplicate"


def test_a_counts_only_body_that_tallies_settles_them(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(submits=[{"accepted": 1, "duplicates": 1}]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2"))
    _drain(grader)
    assert _states(tmp_path) == {"c1": "accepted", "c2": "accepted"}


def test_a_receipt_names_each_case(monkeypatch, tmp_path):
    body = {
        "accepted": 1,
        "duplicates": 0,
        "skipped": [{"case": "c2", "reason": "SOLVER_ERRORED"}],
        "rejected": [],
        "results": [
            {"case_id": "c1", "status": "accepted", "digest": "sha256:abc"},
            {"case_id": "c2", "status": "skipped", "reason": "SOLVER_ERRORED"},
        ],
        "grading": "queued",
    }
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[body]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2", exit_code=1))
    _drain(grader)
    latest = AnswerOutbox(tmp_path).latest()
    assert latest["c1"].state == "accepted" and latest["c1"].digest == "sha256:abc"
    assert latest["c2"].state == "skipped" and latest["c2"].reason == "SOLVER_ERRORED"
    _drain(grader)  # nothing is owed: a settled case is never resent as if new
    assert len(site.submissions) == 1
    grader.close()
    assert grader.summary_line == (
        "site grading: 1 of 2 answer(s) submitted; 1 skipped by the site (c2: SOLVER_ERRORED) "
        "— the site's run stays unfinished"
    )


def test_a_rejected_token_stops_submitting_and_keeps_the_queue(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("http 401", status=401)]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    _drain(grader)
    assert grader.notice == (
        "site grading: the site rejected the CLI token — 1 answer(s) not submitted; "
        "kept in this run's answers outbox for tp sync"
    )
    grader.on_case_done(_result("c2"))  # off: not even queued
    _drain(grader)
    assert len(site.submissions) == 1 and _states(tmp_path) == {"c1": "queued"}
    # The run still exists on the site, so the report still points at it.
    assert grader.summary() is not None


def test_a_graded_run_the_site_no_longer_holds_is_said_by_name(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("http 404", status=404)]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    _drain(grader)
    assert grader.notice is not None
    assert grader.notice.startswith("site grading: the site holds no graded run rs_9 for this account")


def test_a_refused_build_midway_quotes_the_server(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(submits=[TOO_OLD]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    _drain(grader)
    assert grader.notice is not None and "Install the pinned tp build" in grader.notice


def test_the_final_flush_makes_one_attempt_after_the_stop_sentinel(monkeypatch, tmp_path):
    clock = _Clock()
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("unreachable")]))
    assert grader is not None
    grader._clock = clock
    grader.on_case_done(_result("c1"))
    _drain(grader)  # fails; the next try is a second away
    grader._queue.put_nowait(grader._stop_token)
    assert grader._drain_once() is False  # the stop wake ignores the backoff
    assert len(site.submissions) == 2 and _states(tmp_path) == {"c1": "accepted"}


def test_the_final_flush_is_skipped_once_stopped_for_good(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("http 401", status=401)]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    _drain(grader)
    grader._queue.put_nowait(grader._stop_token)
    assert grader._drain_once() is False
    assert len(site.submissions) == 1


def test_the_sender_wakes_for_a_short_backoff_not_a_heartbeat(monkeypatch, tmp_path):
    clock = _Clock()
    grader, _site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader._clock = clock
    assert grader._wait() == IDLE_WAIT_SECONDS  # nothing queued: sleep long
    grader.on_case_done(_result("c1"))
    grader._next_send_at = 0.4
    assert grader._wait() == 0.4
    grader._next_send_at = 30.0
    assert grader._wait() == MAX_WAIT_SECONDS  # never past a second while something is owed
    clock.now = 31.0
    assert grader._wait() == MIN_WAIT_SECONDS  # never a busy loop either
    grader._stop("off")
    assert grader._wait() == IDLE_WAIT_SECONDS


def test_an_idle_wake_with_nothing_owed_sends_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr("trap.live.grading.IDLE_WAIT_SECONDS", 0.01)
    grader, site = _start(monkeypatch, tmp_path)
    assert grader is not None
    assert grader._drain_once() is True  # the wait ran out: an idle wake, not a stop
    assert site.submissions == []


def test_a_case_queued_twice_is_recorded_once(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader.on_case_done(_result("c1"))
    _drain(grader)
    grader.on_case_done(_result("c1"))
    _drain(grader)
    assert len(site.submissions) == 1
    assert [r.state for r in AnswerOutbox(tmp_path).read_all()] == ["queued", "accepted"]


def test_an_unknown_case_is_ignored(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader.on_case_done(_result("not-in-this-run"))
    assert AnswerOutbox(tmp_path).read_all() == [] and site.submissions == []


def test_an_answer_that_cannot_be_read_is_marked_and_the_rest_still_go(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, answers={"c2": "x"})  # no stdout for c1
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2"))
    _drain(grader)
    assert [[c["case_id"] for c in batch] for _, batch in site.submissions] == [["c2"]]
    assert _states(tmp_path) == {"c1": "unreadable", "c2": "accepted"}
    assert AnswerOutbox(tmp_path).latest()["c1"].reason == UNREADABLE_ANSWER
    grader.close()
    assert grader.summary_line == (
        "site grading: 1 of 2 answer(s) submitted; 1 unreadable here (c1: UNREADABLE_ANSWER) "
        "— the site's run stays unfinished"
    )


def test_an_answer_changed_on_disk_is_not_resent_as_new(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("unreachable")]))
    assert grader is not None
    grader._clock = _Clock()
    grader.on_case_done(_result("c1"))
    _drain(grader)  # dropped
    (tmp_path / "c1" / "solution" / "stdout").write_text("something else")
    grader._next_send_at = 0.0
    _drain(grader)
    assert len(site.submissions) == 1
    assert AnswerOutbox(tmp_path).latest()["c1"].reason == "ANSWER_CHANGED"


def test_an_outbox_that_stops_taking_writes_stops_grading_quietly(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path)
    assert grader is not None

    def denied(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", denied)
    grader.on_case_done(_result("c1"))  # must not raise
    monkeypatch.undo()
    assert grader.notice is not None and "cannot write the answers outbox" in grader.notice
    grader.on_case_done(_result("c2"))
    _drain(grader)
    assert site.submissions == []


def test_only_the_first_notice_is_kept(tmp_path):
    grader = SiteGrader(client=_Site(), run_dir=tmp_path, notice="first")  # type: ignore[arg-type]
    grader._stop("second")
    assert grader.notice == "first"


def test_the_pump_never_lets_an_exception_out_of_the_thread(tmp_path):
    grader = SiteGrader(client=_Site(), run_dir=tmp_path)  # type: ignore[arg-type]

    def explode() -> bool:
        raise RuntimeError("the sender is on fire")

    grader._drain_once = explode  # type: ignore[method-assign]
    grader._pump()  # must not raise
    assert grader.notice is not None and "RuntimeError" in grader.notice


# -- the summary line -----------------------------------------------------------


def test_the_summary_says_when_everything_landed(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(receipts=True))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2"))
    _drain(grader)
    grader.close()
    assert grader.summary_line == "site grading: 2 of 2 answer(s) submitted"


def test_the_summary_says_what_is_still_unconfirmed(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("unreachable")] * 2))
    assert grader is not None
    grader._clock = _Clock()
    grader.on_case_done(_result("c1"))
    _drain(grader)
    grader.close()  # no thread: the flush is the sender's, and there is none
    assert grader.summary_line == (
        "site grading: 0 of 1 answer(s) submitted; 1 not yet confirmed — kept in this run's answers "
        "outbox; run tp sync"
    )


def test_no_summary_when_no_case_finished(monkeypatch, tmp_path):
    grader, _site = _start(monkeypatch, tmp_path)
    assert grader is not None
    grader.close()
    assert grader.summary_line is None


# -- the real thread -------------------------------------------------------------


def test_a_threaded_run_lands_every_answer(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(receipts=True), threaded=True)
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.on_case_done(_result("c2"))
    grader.close()
    assert {c["case_id"] for _, batch in site.submissions for c in batch} == {"c1", "c2"}
    assert _states(tmp_path) == {"c1": "accepted", "c2": "accepted"}
    assert grader.summary_line == "site grading: 2 of 2 answer(s) submitted" and site.closed is True


def test_close_says_not_yet_confirmed_when_a_send_is_in_flight(monkeypatch, tmp_path):
    monkeypatch.setattr("trap.live.grading.FLUSH_TIMEOUT_SECONDS", 0.01)
    release = threading.Event()

    class _Slow(_Site):
        def submit_answers(self, run_id, cases_results):
            release.wait(5)
            return super().submit_answers(run_id, cases_results)

    site = _Slow(receipts=True)
    grader, _site = _start(monkeypatch, tmp_path, site, threaded=True)
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.close()
    # The request is still out: the answer is unconfirmed, not unsent, and the
    # client is left to the thread rather than closed under its request.
    assert grader.summary_line is not None and "1 not yet confirmed" in grader.summary_line
    assert site.closed is False
    release.set()
    assert grader._thread is not None
    grader._thread.join(5)
    assert site.closed is True and _states(tmp_path) == {"c1": "accepted"}


# -- the HTTP calls ----------------------------------------------------------------


def _live_client(handler) -> LiveClient:
    client = LiveClient("https://srv", "key")
    client.__dict__["_client"] = httpx.Client(base_url="https://srv", transport=httpx.MockTransport(handler))
    return client


def test_resolve_is_a_get_with_the_task_locator():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, query=dict(request.url.params))
        return httpx.Response(200, json=_ADMITTED)

    result = _live_client(handler).resolve_evaluation(repo="https://github.com/o/r", commit="abc", path="t/a")
    assert result["revision_id"] == "ev_1"
    assert seen == {
        "method": "GET",
        "path": "/api/v2/evaluations/resolve",
        "query": {"repo": "https://github.com/o/r", "commit": "abc", "path": "t/a"},
    }


def test_resolve_omits_the_path_for_a_task_at_the_repo_root():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={})

    _live_client(handler).resolve_evaluation(repo="https://github.com/o/r", commit="abc", path=None)
    assert seen["query"] == {"repo": "https://github.com/o/r", "commit": "abc"}


def test_open_posts_the_revision_and_the_client_run_id():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, body=json.loads(request.content))
        return httpx.Response(201, json={"run": {"id": "rs_9"}, "view_url": "https://srv/runs/rs_9"})

    result = _live_client(handler).open_evaluation(
        revision_id="ev_1", client_run_id="r-1", runtime=TP_RUNTIME
    )
    assert result["run"]["id"] == "rs_9"
    assert seen == {
        "method": "POST",
        "path": "/api/v2/evaluations",
        "body": {"revision_id": "ev_1", "client_run_id": "r-1", "runtime": TP_RUNTIME},
    }


def test_open_without_a_runtime_sends_an_empty_block():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"run": {"id": "rs_9"}})

    _live_client(handler).open_evaluation(revision_id="ev_1", client_run_id="r-1")
    assert seen["body"] == {"revision_id": "ev_1", "client_run_id": "r-1", "runtime": {}}


def test_open_carries_the_opening_description_when_given_one():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"run": {"id": "rs_9"}, "ignored": []})

    _live_client(handler).open_evaluation(
        revision_id="ev_1", client_run_id="r-1", runtime=TP_RUNTIME, context=OPENING
    )
    assert seen["body"] == {
        "revision_id": "ev_1",
        "client_run_id": "r-1",
        "runtime": TP_RUNTIME,
        "context": OPENING,
    }


def test_put_context_posts_the_patch_to_the_runs_context():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "accepted": {"groups": ["timing"]}, "ignored": []})

    result = _live_client(handler).put_context("rs_9", FINAL)
    assert result["accepted"] == {"groups": ["timing"]}
    assert seen == {"method": "POST", "path": "/api/v2/runs/rs_9/context", "body": FINAL}


def test_submit_posts_cases_results_to_the_runs_submissions():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "grading": "queued"})

    result = _live_client(handler).submit_answers("rs_9", [{"case_id": "c1", "answer": "x"}])
    assert result["grading"] == "queued"
    assert seen == {
        "method": "POST",
        "path": "/api/v2/runs/rs_9/submissions",
        "body": {"cases_results": [{"case_id": "c1", "answer": "x"}]},
    }


# -- tp run --------------------------------------------------------------------------


class _FakeGrader:
    def __init__(
        self, *, opened: bool = True, notice: str | None = None, summary_line: str | None = None
    ) -> None:
        self.opened = opened
        self.url = "https://srv/runs/rs_9" if opened else None
        self.notice = notice
        self.summary_line = summary_line
        self.cases: list[str] = []
        self.closed = False
        self.closed_with: dict | None = None
        self.started_with: dict[str, object] = {}

    def on_case_done(self, result) -> None:
        self.cases.append(result.case_id)

    def summary(self):
        from trap.models.report import SiteGrading

        return SiteGrading(run_id="rs_9", url=self.url) if self.opened else None

    def close(self, context: dict | None = None) -> None:
        self.closed = True
        self.closed_with = context


def _use_fake_grader(monkeypatch, grader: _FakeGrader) -> None:
    def start(**kwargs):
        grader.started_with = kwargs
        return grader

    monkeypatch.setattr("trap.cli.start_site_grading", start)


def _run_dir_of(project: Path) -> Path:
    return next((project / ".trap" / "runs").glob("*/t/*"))


def test_tp_run_grades_on_site_and_records_where(make_project, runner, monkeypatch):
    from tests.test_live import _FakeTracker, _use_fake_tracker
    from trap.cli import app

    grader = _FakeGrader(summary_line="site grading: 2 of 2 answer(s) submitted")
    _use_fake_grader(monkeypatch, grader)
    _use_fake_tracker(monkeypatch, _FakeTracker())
    # The description carries tp's version, and a CI build's is derived from the commit
    # hash -- which can itself contain "c1" (0.0.0.dev1+gecc179ea4 did). Pinned, so the
    # leak check below sees only what the run put there.
    monkeypatch.setattr("trap.cli.__version__", "1.2.3")
    project = make_project(cmd="sh -c 'cat'", stdin="input.txt", cases=["c1", "c2"])
    result = runner.invoke(app, ["run", "--no-environment"])
    assert result.exit_code == 0, result.output
    assert "graded on site · https://srv/runs/rs_9" in result.output
    assert "site grading: 2 of 2 answer(s) submitted" in result.output
    assert grader.cases == ["c1", "c2"] and grader.closed is True
    # Opened under the live session's id, with the run's cases in order, and
    # told where the run directory is so the answers can be read back from it.
    assert grader.started_with["client_run_id"] == "r-1"
    assert grader.started_with["case_ids"] == ["c1", "c2"]
    # Told what the run is made of, both when it opens and when it closes:
    # the same description the private session gets, aggregates only.
    opening = grader.started_with["context"]
    assert isinstance(opening, dict) and opening["source"] == "tp" and "timing" not in opening
    assert grader.closed_with is not None and grader.closed_with["timing"]["solver_ms"] >= 0
    assert "c1" not in json.dumps(grader.closed_with)
    run_dir = _run_dir_of(project)
    assert grader.started_with["run_dir"] == run_dir
    assert (run_dir / "c1" / "solution" / "stdout").read_text() == "hi"
    report = json.loads((run_dir / "report.json").read_text())
    assert report["site_grading"] == {"run_id": "rs_9", "url": "https://srv/runs/rs_9"}


def test_json_output_keeps_the_grader_lines_off_the_report(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    grader = _FakeGrader(
        notice="site grading: something", summary_line="site grading: 1 of 1 answer(s) submitted"
    )
    _use_fake_grader(monkeypatch, grader)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    result = runner.invoke(app, ["run", "--no-environment", "--output", "json"])
    assert result.exit_code == 0, result.output
    assert grader.closed is True
    assert "site grading" not in result.output  # the JSON stays machine-readable


def test_a_grader_that_never_opened_prints_nothing_but_its_notice(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    _use_fake_grader(monkeypatch, _FakeGrader(opened=False, notice="site grading off for this run: x"))
    project = make_project(cmd="sh -c 'cat'", cases=["c1"])
    result = runner.invoke(app, ["run", "--no-environment"])
    assert result.exit_code == 0, result.output
    assert "graded on site" not in result.output
    assert "site grading off for this run: x" in result.output
    report = json.loads((_run_dir_of(project) / "report.json").read_text())
    assert report["site_grading"] is None


def test_no_site_grading_passes_the_flag_through(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    grader = _FakeGrader()
    _use_fake_grader(monkeypatch, grader)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment", "--no-site-grading"]).exit_code == 0
    assert grader.started_with["enabled"] is False
    assert grader.started_with["client_run_id"] is None  # no live session: none to share


def test_ctrl_c_closes_the_grader(make_project, runner, monkeypatch):
    from trap.cli import app
    from trap.runner import TaskRunner

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    grader = _FakeGrader()
    _use_fake_grader(monkeypatch, grader)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(TaskRunner, "run", interrupted)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code != 0
    assert grader.closed is True


def test_a_run_with_no_grader_records_none(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    monkeypatch.setattr("trap.cli.start_site_grading", lambda **_kwargs: None)
    project = make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    assert json.loads((_run_dir_of(project) / "report.json").read_text())["site_grading"] is None


# -- describing the graded run ------------------------------------------------------


OPENING = {"schema_version": 1, "source": "tp", "identity": {"launcher": {"name": "tp"}}}
FINAL = {"schema_version": 1, "source": "tp", "timing": {"solver_ms": 12}}


def test_the_opening_description_is_stored_with_the_graded_run(monkeypatch, tmp_path):
    _grader, site = _start(monkeypatch, tmp_path, context=OPENING)
    assert site.opens[0]["context"] == OPENING
    assert site.opens[0]["runtime"] == TP_RUNTIME  # the runtime block still travels beside it


def test_the_closing_description_goes_to_the_graded_run_after_the_answers(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(receipts=True))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader._queue.put_nowait(dict(FINAL))
    _drain(grader)
    assert site.order == ["answers", "context"]
    assert site.contexts == [("rs_9", FINAL)]  # the site's id for the graded run
    assert grader.notice is None


def test_close_hands_the_description_to_the_sender(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(receipts=True), threaded=True)
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader.close(context=FINAL)
    assert site.order == ["answers", "context"] and site.contexts == [("rs_9", FINAL)]
    assert site.closed is True  # posted before the client went away


def test_a_description_the_site_refuses_is_one_line_and_grading_stays_on(monkeypatch, tmp_path):
    grader, _site = _start(
        monkeypatch, tmp_path, _Site(receipts=True, context_error=LiveApiError("http 400", status=400))
    )
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader._queue.put_nowait(dict(OPENING))
    grader._queue.put_nowait(dict(FINAL))
    _drain(grader)
    assert grader.notice == "site grading: the run's description was not recorded (http 400)"
    assert grader._descriptions == []  # each tried once, then dropped; one line for both
    # The answers are unaffected: the next one still goes.
    grader.on_case_done(_result("c2"))
    _drain(grader)
    assert _states(tmp_path) == {"c1": "accepted", "c2": "accepted"}
    assert grader.summary() is not None


def test_no_description_is_posted_once_grading_stopped_for_good(monkeypatch, tmp_path):
    grader, site = _start(monkeypatch, tmp_path, _Site(submits=[LiveApiError("http 401", status=401)]))
    assert grader is not None
    grader.on_case_done(_result("c1"))
    grader._queue.put_nowait(dict(FINAL))
    _drain(grader)  # the answer's refusal stops grading in the same wake
    assert grader.notice is not None and "rejected" in grader.notice
    assert site.contexts == []


def test_a_grader_that_never_opened_takes_a_description_quietly(tmp_path):
    grader = SiteGrader(client=_Site(), run_dir=tmp_path, notice="off")  # type: ignore[arg-type]
    grader.close(context=FINAL)  # no thread, no graded run: nothing to post, nothing raised
    assert grader.notice == "off"

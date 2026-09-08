"""Tests for the answers outbox: durable before sending, settled by receipt.

Two properties carry the module. The answer text is never copied -- the queue
holds a digest and the resend re-reads the case's stdout, so a byte-identical
retry is guaranteed by construction and a changed file is refused rather than
sent as a new answer. And a receipt is applied per case, so what the site
skipped or rejected is named and never resent as if it were new.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trap.live.answers import (
    ANSWER_CHANGED,
    KNOWN_REASONS,
    UNREADABLE_ANSWER,
    AnswerOutbox,
    AnswerOutboxError,
    AnswerRecord,
    AnswerUnreadable,
    GradedRun,
    ResendOutcome,
    apply_receipt,
    classify,
    describe,
    resend,
    sha256_of,
    shortfall,
)
from trap.live.client import LiveApiError
from trap.models.cost import CaseCost, ModelCost
from trap.models.results import CaseResult

GRADED = GradedRun(
    run_id="rs_9",
    client_run_id="r-1-site",
    server="https://srv",
    url="https://srv/runs/rs_9",
    revision_id="ev_1",
    user_id="usr_a",
    cases_total=2,
)


def _stdout(run_dir: Path, case_id: str, text: str) -> None:
    path = run_dir / case_id / "solution" / "stdout"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _outbox(run_dir: Path) -> AnswerOutbox:
    outbox = AnswerOutbox(run_dir)
    outbox.prepare()
    return outbox


def _queued(
    run_dir: Path, outbox: AnswerOutbox, case_id: str, answer: str, *, ordinal: int = 1
) -> AnswerRecord:
    _stdout(run_dir, case_id, answer)
    record = AnswerRecord.queued(
        CaseResult(case_id=case_id, metrics=None, duration=0.5), ordinal=ordinal, answer=answer
    )
    outbox.queue(record)
    return record


# -- the sidecar ----------------------------------------------------------------


def test_the_graded_run_roundtrips_through_its_sidecar(tmp_path: Path):
    GRADED.save(tmp_path)
    loaded = GradedRun.load(tmp_path)
    assert loaded == GRADED
    assert GradedRun.path_in(tmp_path) == tmp_path / "live" / "grading.json"


def test_a_missing_or_corrupt_sidecar_reads_as_none(tmp_path: Path):
    assert GradedRun.load(tmp_path) is None
    path = GradedRun.path_in(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert GradedRun.load(tmp_path) is None


# -- records --------------------------------------------------------------------


def test_a_queued_record_keeps_the_wire_fields_and_a_digest_but_not_the_answer():
    result = CaseResult(
        case_id="c1",
        exit_code=0,
        duration=1.25,
        metrics={"score": 1.0, "expected": "SENTINEL"},
        cost=CaseCost(by_model=[ModelCost(provider="anthropic", cost_usd=0.02)]),
    )
    record = AnswerRecord.queued(result, ordinal=3, answer="forty-two")
    assert record.state == "queued" and record.ordinal == 3
    assert record.client_reported == {"duration_ms": 1250, "cost_usd": 0.02}
    assert record.answer_sha256 == sha256_of("forty-two")
    dumped = record.model_dump_json()
    assert "forty-two" not in dumped and "SENTINEL" not in dumped and "score" not in dumped


def test_an_unknown_cost_is_absent_not_zero():
    result = CaseResult(
        case_id="c1", metrics=None, cost=CaseCost(by_model=[ModelCost(provider="p", cost_usd=None)])
    )
    assert AnswerRecord.queued(result, ordinal=1, answer="").client_reported == {"duration_ms": 0}


def test_the_submission_is_the_reports_own_shape():
    record = AnswerRecord.queued(
        CaseResult(case_id="c1", exit_code=1, duration=0.5, metrics=None), ordinal=1, answer="x"
    )
    assert record.submission("x") == {
        "case_id": "c1",
        "answer": "x",
        "duration": 0.5,
        "exit_code": 1,
        "client_reported": {"duration_ms": 500},
    }


def test_the_wire_rereads_the_answer_from_the_case_stdout(tmp_path: Path):
    _stdout(tmp_path, "c1", "forty-two")
    record = AnswerRecord.queued(CaseResult(case_id="c1", metrics=None), ordinal=1, answer="forty-two")
    assert record.wire(tmp_path)["answer"] == "forty-two"


def test_a_changed_answer_is_refused_not_resent(tmp_path: Path):
    _stdout(tmp_path, "c1", "forty-two")
    record = AnswerRecord.queued(CaseResult(case_id="c1", metrics=None), ordinal=1, answer="forty-two")
    _stdout(tmp_path, "c1", "forty-three")
    with pytest.raises(AnswerUnreadable) as excinfo:
        record.wire(tmp_path)
    assert excinfo.value.reason == ANSWER_CHANGED


def test_a_missing_answer_is_unreadable(tmp_path: Path):
    record = AnswerRecord.queued(CaseResult(case_id="c1", metrics=None), ordinal=1, answer="")
    with pytest.raises(AnswerUnreadable) as excinfo:
        record.wire(tmp_path)
    assert excinfo.value.reason == UNREADABLE_ANSWER


def test_an_answer_that_is_not_text_is_unreadable(tmp_path: Path):
    path = tmp_path / "c1" / "solution" / "stdout"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe\x00bad")
    record = AnswerRecord.queued(CaseResult(case_id="c1", metrics=None), ordinal=1, answer="")
    with pytest.raises(AnswerUnreadable) as excinfo:
        record.wire(tmp_path)
    assert excinfo.value.reason == UNREADABLE_ANSWER


# -- the outbox -----------------------------------------------------------------


def test_prepare_creates_the_file_under_live(tmp_path: Path):
    outbox = _outbox(tmp_path)
    assert outbox.path == tmp_path / "live" / "answers.jsonl" and outbox.path.is_file()
    assert outbox.read_all() == []


def test_prepare_reports_an_unwritable_workspace(tmp_path: Path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(AnswerOutboxError):
        AnswerOutbox(tmp_path).prepare()


def test_a_case_is_queued_once(tmp_path: Path):
    outbox = _outbox(tmp_path)
    record = _queued(tmp_path, outbox, "c1", "a")
    outbox.settle("c1", "accepted", digest="d1")
    # A second queue for the same case must not reset it to queued.
    assert outbox.queue(record) is False
    assert outbox.latest()["c1"].state == "accepted"
    assert outbox.pending() == []


def test_settling_a_case_never_queued_is_ignored(tmp_path: Path):
    outbox = _outbox(tmp_path)
    outbox.settle("ghost", "accepted")
    assert outbox.read_all() == []


def test_pending_is_in_execution_order_and_only_what_is_queued(tmp_path: Path):
    outbox = _outbox(tmp_path)
    _queued(tmp_path, outbox, "c3", "z", ordinal=3)
    _queued(tmp_path, outbox, "c1", "x", ordinal=1)
    _queued(tmp_path, outbox, "c2", "y", ordinal=2)
    outbox.settle("c2", "skipped", reason="SOLVER_ERRORED")
    assert [r.case_id for r in outbox.pending()] == ["c1", "c3"]
    assert list(outbox.latest()) == ["c3", "c1", "c2"]  # first-queued order
    assert outbox.latest()["c2"].reason == "SOLVER_ERRORED"


def test_a_truncated_final_line_costs_one_state_change_not_the_queue(tmp_path: Path):
    outbox = _outbox(tmp_path)
    _queued(tmp_path, outbox, "c1", "a")
    with outbox.path.open("a") as handle:
        handle.write('{"case_id": "c1", "sta')  # killed mid-settle
        handle.write("\n\n")
    assert [r.state for r in outbox.read_all()] == ["queued"]


def test_reading_a_missing_outbox_is_empty(tmp_path: Path):
    assert AnswerOutbox(tmp_path).pending() == []


def test_a_failed_append_is_reported(tmp_path: Path, monkeypatch):
    outbox = _outbox(tmp_path)
    record = _queued(tmp_path, outbox, "c1", "a")

    def denied(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(AnswerOutboxError):
        outbox.queue(record.model_copy(update={"case_id": "c2"}))
    monkeypatch.undo()
    assert [r.case_id for r in outbox.read_all()] == ["c1"]  # nothing half-written


# -- receipts -------------------------------------------------------------------


def _two_sent(tmp_path: Path) -> tuple[AnswerOutbox, list[AnswerRecord]]:
    outbox = _outbox(tmp_path)
    return outbox, [
        _queued(tmp_path, outbox, "c1", "a", ordinal=1),
        _queued(tmp_path, outbox, "c2", "b", ordinal=2),
    ]


def test_a_per_case_receipt_is_authoritative(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    body = {
        "accepted": 0,
        "duplicates": 0,
        "results": [
            {"case_id": "c1", "status": "duplicate", "digest": "d1"},
            {"case_id": "c2", "status": "rejected", "reason": "ALREADY_ANSWERED"},
            {"case_id": "c9", "status": "accepted"},  # not sent: ignored
            "garbage",
            {"case_id": "c2", "status": "graded"},  # not a receipt status: ignored
        ],
    }
    settled = apply_receipt(outbox, sent, body)
    assert settled.duplicate == ["c1"] and settled.rejected == [("c2", "ALREADY_ANSWERED")]
    assert settled.accepted == [] and settled.skipped == [] and settled.unsettled == []
    latest = outbox.latest()
    assert latest["c1"].state == "duplicate" and latest["c1"].digest == "d1"
    assert latest["c2"].state == "rejected" and latest["c2"].reason == "ALREADY_ANSWERED"


def test_a_receipt_that_names_only_some_cases_leaves_the_rest_queued(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    settled = apply_receipt(
        outbox, sent, {"results": [{"case_id": "c2", "status": "skipped", "reason": "NO_ANSWER"}]}
    )
    assert settled.unsettled == ["c1"] and settled.skipped == [("c2", "NO_ANSWER")]
    assert [r.case_id for r in outbox.pending()] == ["c1"]


def test_an_older_server_is_read_from_its_counts_when_they_tally(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    body = {
        "accepted": 1,
        "duplicates": 0,
        "skipped": [{"case": "c2", "reason": "SOLVER_ERRORED"}],
        "rejected": [],
    }
    settled = apply_receipt(outbox, sent, body)
    assert settled.accepted == ["c1"] and settled.skipped == [("c2", "SOLVER_ERRORED")]
    assert outbox.pending() == []


def test_counts_that_do_not_tally_settle_nothing(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    # Two sent, one counted: which one? Neither is claimed; both go again and
    # the site answers "duplicate" for the one it already has.
    settled = apply_receipt(outbox, sent, {"accepted": 1, "duplicates": 0, "skipped": [], "rejected": []})
    assert settled.unsettled == ["c1", "c2"] and not settled.any
    assert len(outbox.pending()) == 2


def test_an_empty_body_settles_nothing(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    assert apply_receipt(outbox, sent, {}).unsettled == ["c1", "c2"]
    assert len(outbox.pending()) == 2


def test_counts_that_are_not_numbers_do_not_count(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    body = {"accepted": True, "duplicates": "2", "rejected": "c1", "skipped": [{"case": "c1"}, 5]}
    settled = apply_receipt(outbox, sent, body)
    assert settled.rejected == [] and settled.skipped == [("c1", "SKIPPED")]
    assert settled.unsettled == ["c2"]


def test_a_rejection_without_a_reason_still_reads_as_rejected(tmp_path: Path):
    outbox, sent = _two_sent(tmp_path)
    body = {
        "results": [
            {"case_id": "c1", "status": "rejected"},
            {"case_id": "c2", "status": "accepted", "digest": 5},
        ]
    }
    settled = apply_receipt(outbox, sent, body)
    assert settled.rejected == [("c1", "REJECTED")] and settled.accepted == ["c2"]
    assert outbox.latest()["c2"].digest is None


# -- failures -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (None, "retry"),
        (408, "retry"),
        (429, "retry"),
        (500, "retry"),
        (503, "retry"),
        (401, "credential"),
        (403, "structural"),  # WRONG_CHANNEL: the run is not a graded one
        (404, "structural"),
        (400, "structural"),
        (409, "structural"),
    ],
)
def test_what_a_failed_request_means(status, expected):
    assert classify(LiveApiError("x", status=status)) == expected


def test_a_refused_build_is_structural_whatever_the_status():
    assert classify(LiveApiError("x", status=400, payload={"code": "CLIENT_TOO_OLD"})) == "structural"


def test_failures_are_described_in_words_a_user_can_act_on():
    assert describe(LiveApiError("http 401", status=401), "rs_9") == "the site rejected the CLI token"
    assert (
        describe(LiveApiError("http 404", status=404), "rs_9")
        == "the site holds no graded run rs_9 for this account"
    )
    too_old = LiveApiError("http 426", status=426, payload={"error": "Install X"})
    assert describe(too_old, "rs_9") == "Install X"
    assert describe(LiveApiError("http 426", status=426), "rs_9") == "this server needs a newer tp"
    forbidden = LiveApiError("http 403", status=403, payload={"error": "this run is not a server-graded run"})
    assert describe(forbidden, "rs_9") == "the site refused the answers (this run is not a server-graded run)"
    assert describe(LiveApiError("http 400", status=400), "rs_9") == "the site refused the answers (http 400)"


# -- resending ------------------------------------------------------------------


class _Site:
    def __init__(self, *answers: object) -> None:
        self._answers = list(answers)
        self.batches: list[list[dict]] = []

    def submit_answers(self, run_id: str, cases_results: list[dict]) -> dict:
        assert run_id == "rs_9"
        self.batches.append(cases_results)
        answer = self._answers.pop(0) if self._answers else None
        if isinstance(answer, LiveApiError):
            raise answer
        if isinstance(answer, dict):
            return answer
        return {
            "results": [{"case_id": c["case_id"], "status": "accepted", "digest": "d"} for c in cases_results]
        }


def test_resend_posts_the_queue_in_batches_and_settles_each(tmp_path: Path):
    outbox = _outbox(tmp_path)
    for ordinal, case_id in enumerate(("c1", "c2", "c3"), start=1):
        _queued(tmp_path, outbox, case_id, f"answer {case_id}", ordinal=ordinal)
    site = _Site()
    outcome = resend(site, GRADED, outbox, tmp_path, batch=2)  # type: ignore[arg-type]
    assert [[c["case_id"] for c in batch] for batch in site.batches] == [["c1", "c2"], ["c3"]]
    assert site.batches[0][0]["answer"] == "answer c1"
    assert (outcome.delivered, outcome.remaining, outcome.error) == (3, 0, None)
    assert {r.state for r in outbox.latest().values()} == {"accepted"}


def test_resend_stops_at_the_first_request_the_site_does_not_take(tmp_path: Path):
    outbox = _outbox(tmp_path)
    for ordinal, case_id in enumerate(("c1", "c2"), start=1):
        _queued(tmp_path, outbox, case_id, "a", ordinal=ordinal)
    site = _Site(LiveApiError("unreachable"))
    outcome = resend(site, GRADED, outbox, tmp_path, batch=1)  # type: ignore[arg-type]
    assert len(site.batches) == 1
    assert (outcome.delivered, outcome.remaining) == (0, 2)
    assert outcome.error is not None and str(outcome.error) == "unreachable"


def test_resend_stops_at_a_receipt_that_settles_nothing(tmp_path: Path):
    outbox = _outbox(tmp_path)
    for ordinal, case_id in enumerate(("c1", "c2"), start=1):
        _queued(tmp_path, outbox, case_id, "a", ordinal=ordinal)
    site = _Site({})
    outcome = resend(site, GRADED, outbox, tmp_path, batch=1)  # type: ignore[arg-type]
    assert len(site.batches) == 1  # pushing the next batch would only repeat that
    assert (outcome.delivered, outcome.remaining) == (0, 2)


def test_an_unreadable_answer_is_settled_here_and_the_rest_still_go(tmp_path: Path):
    outbox = _outbox(tmp_path)
    _queued(tmp_path, outbox, "c1", "a", ordinal=1)
    _queued(tmp_path, outbox, "c2", "b", ordinal=2)
    (tmp_path / "c1" / "solution" / "stdout").unlink()
    site = _Site()
    outcome = resend(site, GRADED, outbox, tmp_path)  # type: ignore[arg-type]
    assert [c["case_id"] for c in site.batches[0]] == ["c2"]
    assert outcome.unreadable == [("c1", UNREADABLE_ANSWER)] and outcome.delivered == 1
    assert outbox.latest()["c1"].state == "unreadable"


def test_a_batch_with_nothing_readable_costs_no_request(tmp_path: Path):
    outbox = _outbox(tmp_path)
    _queued(tmp_path, outbox, "c1", "a", ordinal=1)
    _queued(tmp_path, outbox, "c2", "b", ordinal=2)
    (tmp_path / "c1" / "solution" / "stdout").unlink()
    site = _Site()
    outcome = resend(site, GRADED, outbox, tmp_path, batch=1)  # type: ignore[arg-type]
    assert [[c["case_id"] for c in batch] for batch in site.batches] == [["c2"]]
    assert outcome.remaining == 0


def test_resend_collects_what_the_site_would_not_take(tmp_path: Path):
    outbox = _outbox(tmp_path)
    _queued(tmp_path, outbox, "c1", "a", ordinal=1)
    _queued(tmp_path, outbox, "c2", "b", ordinal=2)
    body = {
        "results": [
            {"case_id": "c1", "status": "skipped", "reason": "SOLVER_ERRORED"},
            {"case_id": "c2", "status": "rejected", "reason": "ARTIFACT_TOO_LARGE"},
        ]
    }
    outcome = resend(_Site(body), GRADED, outbox, tmp_path)  # type: ignore[arg-type]
    assert outcome.skipped == [("c1", "SOLVER_ERRORED")] and outcome.rejected == [
        ("c2", "ARTIFACT_TOO_LARGE")
    ]
    assert (outcome.delivered, outcome.remaining) == (0, 0)


def test_the_shortfall_names_the_cases_the_site_will_never_grade():
    assert shortfall([], [], []) == ""
    line = shortfall([("c4", "ALREADY_ANSWERED")], [("c2", "SOLVER_ERRORED")], [("c5", UNREADABLE_ANSWER)])
    assert line == (
        "; 1 skipped by the site (c2: SOLVER_ERRORED); 1 rejected (c4: ALREADY_ANSWERED); "
        "1 unreadable here (c5: UNREADABLE_ANSWER) — the site's run stays unfinished"
    )
    many = shortfall([(f"c{i}", "NO_SUCH_CASE") for i in range(7)], [], [])
    assert many.count("NO_SUCH_CASE") == 5 and ", …" in many


def test_every_reason_the_summary_can_quote_is_a_string():
    assert all(isinstance(reason, str) and reason.isupper() for reason in KNOWN_REASONS)
    assert ResendOutcome().remaining == 0


def test_the_outbox_line_is_plain_json(tmp_path: Path):
    outbox = _outbox(tmp_path)
    _queued(tmp_path, outbox, "c1", "a")
    line = json.loads(outbox.path.read_text().splitlines()[0])
    assert set(line) == {
        "case_id",
        "ordinal",
        "state",
        "at",
        "duration",
        "exit_code",
        "client_reported",
        "answer_sha256",
        "reason",
        "digest",
    }

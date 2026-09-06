"""Tests for live progress sync.

Two properties are load-bearing and get most of the attention here:

1. **Sync cannot affect the run.** Every failure mode -- no network, a rejected
   token, an unwritable outbox, a corrupt sidecar -- must end with the tracker
   quietly off and nothing raised at the caller.
2. **Only whitelisted progress leaves the machine.** The tests assert on the
   exact payload dicts, because "we strip the bad fields" is not the design;
   "we only ever build the good ones" is.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from trap.auth.store import CredentialStoreError
from trap.live.client import LiveApiError, LiveClient
from trap.live.identity import LiveSession, new_client_run_id
from trap.live.outbox import Outbox, OutboxError
from trap.live.setup import live_disabled_by_env, start_tracking
from trap.live.tracker import LiveTracker, verdict_of
from trap.models.cost import CaseCost, ModelCost
from trap.models.results import CaseResult

# -- identity ----------------------------------------------------------------


def test_client_run_id_is_unique_and_url_safe():
    a, b = new_client_run_id(), new_client_run_id()
    assert a != b
    assert a.startswith("r-") and a[2:].isalnum()


def test_session_roundtrips_through_the_sidecar(tmp_path: Path):
    session = LiveSession(client_run_id="r-1", server="https://srv", user_id="usr_a")
    session.save(tmp_path)
    loaded = LiveSession.load(tmp_path)
    assert loaded is not None
    assert loaded.client_run_id == "r-1"
    assert loaded.user_id == "usr_a"


def test_missing_sidecar_reads_as_none(tmp_path: Path):
    assert LiveSession.load(tmp_path) is None


def test_corrupt_sidecar_reads_as_none_rather_than_raising(tmp_path: Path):
    path = LiveSession.path_in(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert LiveSession.load(tmp_path) is None


def test_saving_twice_replaces_atomically(tmp_path: Path):
    LiveSession(client_run_id="r-1", server="https://srv").save(tmp_path)
    LiveSession(client_run_id="r-1", server="https://srv", acked_seq=7).save(tmp_path)
    loaded = LiveSession.load(tmp_path)
    assert loaded is not None and loaded.acked_seq == 7


@pytest.mark.parametrize(
    ("stored_user", "server", "user", "expected"),
    [
        ("usr_a", "https://srv", "usr_a", True),
        ("usr_a", "https://srv/", "usr_a", True),  # trailing slash is not an identity
        ("usr_a", "https://srv", "usr_b", False),  # a different account
        ("usr_a", "https://other", "usr_a", False),  # a different server
        (None, "https://srv", "usr_a", False),  # identity was never established
        ("usr_a", "https://srv", None, False),  # the server will not say who we are
    ],
)
def test_a_queue_is_only_delivered_to_the_account_that_created_it(stored_user, server, user, expected):
    session = LiveSession(client_run_id="r-1", server="https://srv", user_id=stored_user)
    assert session.belongs_to(server, user) is expected


# -- outbox ------------------------------------------------------------------


def _outbox(tmp_path: Path) -> Outbox:
    outbox = Outbox(tmp_path)
    outbox.prepare()
    return outbox


def test_append_allocates_sequence_numbers_in_order(tmp_path: Path):
    outbox = _outbox(tmp_path)
    first = outbox.append(event_id="e1", type="run_started", payload={})
    second = outbox.append(event_id="e2", type="case_started", payload={"ordinal": 1})
    assert (first.client_seq, second.client_seq) == (1, 2)


def test_prepare_resumes_numbering_from_disk(tmp_path: Path):
    _outbox(tmp_path).append(event_id="e1", type="run_started", payload={})
    resumed = _outbox(tmp_path)
    assert resumed.append(event_id="e2", type="heartbeat", payload={}).client_seq == 2


def test_pending_returns_only_unacked_events(tmp_path: Path):
    outbox = _outbox(tmp_path)
    for i in range(3):
        outbox.append(event_id=f"e{i}", type="heartbeat", payload={})
    assert [e.client_seq for e in outbox.pending(1)] == [2, 3]


def test_a_truncated_final_line_costs_one_event_not_the_queue(tmp_path: Path):
    outbox = _outbox(tmp_path)
    outbox.append(event_id="e1", type="run_started", payload={})
    with outbox.path.open("a") as handle:
        handle.write('{"event_id": "e2", "client_se')  # killed mid-write
    assert [e.event_id for e in outbox.read_all()] == ["e1"]


def test_blank_lines_are_ignored(tmp_path: Path):
    outbox = _outbox(tmp_path)
    outbox.append(event_id="e1", type="run_started", payload={})
    with outbox.path.open("a") as handle:
        handle.write("\n\n")
    assert len(outbox.read_all()) == 1


def test_reading_a_missing_outbox_is_empty_not_an_error(tmp_path: Path):
    assert Outbox(tmp_path).read_all() == []


def test_prepare_reports_an_unwritable_workspace(tmp_path: Path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(OutboxError):
        Outbox(tmp_path).prepare()


def test_a_failed_write_does_not_burn_a_sequence_number(tmp_path: Path, monkeypatch):
    outbox = _outbox(tmp_path)

    def denied(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(OutboxError):
        outbox.append(event_id="e1", type="heartbeat", payload={})
    monkeypatch.undo()
    # The next successful append still gets 1: a gap here would be one the
    # server could never ack past.
    assert outbox.append(event_id="e1", type="heartbeat", payload={}).client_seq == 1


# -- client ------------------------------------------------------------------


def _live_client(handler) -> LiveClient:
    client = LiveClient("https://srv", "key")
    client.__dict__["_client"] = httpx.Client(base_url="https://srv", transport=httpx.MockTransport(handler))
    return client


def test_whoami_reads_the_nested_user_id():
    client = _live_client(lambda r: httpx.Response(200, json={"user": {"id": "usr_a"}}))
    assert client.whoami() == "usr_a"


def test_whoami_reads_a_flat_id():
    client = _live_client(lambda r: httpx.Response(200, json={"id": "usr_a"}))
    assert client.whoami() == "usr_a"


def test_whoami_is_none_when_the_server_does_not_say():
    assert _live_client(lambda r: httpx.Response(200, json={})).whoami() is None
    assert _live_client(lambda r: httpx.Response(200, json={"user": {}})).whoami() is None
    assert _live_client(lambda r: httpx.Response(200, json={"user": 5})).whoami() is None


def test_ensure_session_puts_to_the_client_run_id():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"run": {"id": "rs_1"}})

    assert _live_client(handler).ensure_session("r-1")["run"]["id"] == "rs_1"
    assert seen == {"method": "PUT", "path": "/api/v2/local-runs/r-1"}


def test_send_events_posts_the_batch():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ack_seq": 2})

    result = _live_client(handler).send_events("rs_1", [{"client_seq": 1}])
    assert result["ack_seq"] == 2
    assert seen["body"] == {"events": [{"client_seq": 1}]}


@pytest.mark.parametrize(("status", "rejected"), [(401, True), (403, True), (500, False), (429, False)])
def test_only_401_and_403_retire_the_credential(status, rejected):
    with pytest.raises(LiveApiError) as excinfo:
        _live_client(lambda r: httpx.Response(status)).whoami()
    assert excinfo.value.credential_rejected is rejected


def test_being_offline_is_a_live_api_error_not_a_crash():
    def boom(_request):
        raise httpx.ConnectError("down")

    with pytest.raises(LiveApiError, match="unreachable"):
        _live_client(boom).whoami()


def test_a_non_json_response_is_an_error():
    with pytest.raises(LiveApiError, match="not JSON"):
        _live_client(lambda r: httpx.Response(200, text="<html>")).whoami()


def test_a_json_array_response_reads_as_empty():
    assert _live_client(lambda r: httpx.Response(200, json=[1, 2])).whoami() is None


def test_close_is_safe_before_and_after_the_client_is_built():
    client = LiveClient("https://srv", "key")
    client.close()  # nothing was ever opened
    client = _live_client(lambda r: httpx.Response(200, json={}))
    client.close()


def test_server_is_normalised():
    assert LiveClient("https://srv/", "key").server == "https://srv"


# -- verdicts ----------------------------------------------------------------


def _result(**kwargs) -> CaseResult:
    base = {"case_id": "c1", "metrics": None, "judge_exit_code": 0}
    return CaseResult(**{**base, **kwargs})


def test_a_judge_that_ran_is_not_an_answer_that_was_right():
    # exit 0 with no score: the judge worked, and we still do not know the
    # outcome. Claiming "passed" here is the bug this asserts against.
    assert verdict_of(_result(metrics={"note": "ok"})) is None


def test_a_broken_judge_is_an_error_not_a_zero():
    assert verdict_of(_result(judge_exit_code=124)) == "error"
    assert verdict_of(_result(judge_exit_code=125)) == "error"


def test_scores_at_the_ends_become_verdicts():
    assert verdict_of(_result(metrics={"score": 1})) == "passed"
    assert verdict_of(_result(metrics={"score": 0})) == "failed"
    assert verdict_of(_result(metrics={"score": 1.5})) == "passed"
    assert verdict_of(_result(metrics={"score": -1})) == "failed"


def test_partial_credit_is_a_number_not_a_pass_or_a_fail():
    assert verdict_of(_result(metrics={"score": 0.5})) is None


def test_a_non_numeric_score_yields_no_verdict():
    assert verdict_of(_result(metrics={"score": "high"})) is None
    assert verdict_of(_result(metrics={"score": True})) is None
    assert verdict_of(_result(metrics=["not", "a", "dict"])) is None


def test_no_judge_at_all_yields_no_verdict():
    assert verdict_of(_result(judge_exit_code=None)) is None


# -- tracker -----------------------------------------------------------------


class _Recorder:
    """A LiveClient stand-in that records batches instead of sending them."""

    def __init__(self, *, fail: LiveApiError | None = None) -> None:
        self.server = "https://srv"
        self.batches: list[list[dict]] = []
        self.sessions: list[str] = []
        self._fail = fail
        self.closed = False

    def whoami(self) -> str | None:
        return "usr_a"

    def ensure_session(self, client_run_id, **_kwargs):
        if self._fail:
            raise self._fail
        self.sessions.append(client_run_id)
        return {"run": {"id": "rs_1"}}

    def send_events(self, _run_ref, events):
        if self._fail:
            raise self._fail
        self.batches.append(events)
        return {"ack_seq": max(e["client_seq"] for e in events)}

    def close(self) -> None:
        self.closed = True


def _tracker(tmp_path: Path, client=None, case_ids=("c1", "c2")) -> LiveTracker:
    outbox = Outbox(tmp_path)
    outbox.prepare()
    return LiveTracker(
        client=client or _Recorder(),  # type: ignore[arg-type]
        session=LiveSession(client_run_id="r-1", server="https://srv"),
        outbox=outbox,
        run_dir=tmp_path,
        case_ids=list(case_ids),
    )


def _sent(tracker: LiveTracker, client: _Recorder) -> list[dict]:
    tracker.close()
    return [event for batch in client.batches for event in batch]


def test_a_run_mirrors_its_lifecycle(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("c1")
    tracker.on_case_done(_result(metrics={"score": 1}, duration=1.5))
    tracker.on_run_finished(exit_code=0, cases_done=1, score=0.5)
    events = _sent(tracker, client)

    assert [e["type"] for e in events] == [
        "run_started",
        "case_started",
        "case_finished",
        "run_finished",
    ]
    assert client.sessions == ["r-1"]


def test_the_wire_carries_ordinals_never_case_names(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client, case_ids=("secret-question-about-acme", "c2"))
    tracker.start()
    tracker.on_case_start("secret-question-about-acme")
    events = _sent(tracker, client)

    started = next(e for e in events if e["type"] == "case_started")
    assert started["payload"] == {"ordinal": 1}
    assert "secret-question-about-acme" not in json.dumps(events)


def test_case_finished_carries_only_whitelisted_fields(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_done(
        _result(
            metrics={"score": 1, "expected": "SENTINEL", "agent_answer": "SENTINEL"},
            duration=2.0,
            cost=CaseCost(by_model=[ModelCost(provider="anthropic", cost_usd=0.25)]),
        )
    )
    events = _sent(tracker, client)

    finished = next(e for e in events if e["type"] == "case_finished")
    assert set(finished["payload"]) == {"ordinal", "verdict", "score", "duration_ms", "cost_usd"}
    assert finished["payload"]["duration_ms"] == 2000
    assert finished["payload"]["cost_usd"] == 0.25
    assert "SENTINEL" not in json.dumps(events)


def test_an_unknown_case_id_is_ignored(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("not-in-this-run")
    tracker.on_case_done(_result(case_id="not-in-this-run", metrics={"score": 1}))
    events = _sent(tracker, client)
    assert [e["type"] for e in events] == ["run_started"]


def test_grader_and_cancellation_events(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_grader_started()
    tracker.on_grader_finished(exit_code=0, score=0.75)
    tracker.on_grader_finished(exit_code=None, score=None)
    tracker.on_run_failed("judge_error", cases_done=1)
    tracker.on_run_cancelled(cases_done=1)
    events = _sent(tracker, client)

    graded = [e["payload"] for e in events if e["type"] == "grader_finished"]
    assert graded[0] == {"exit_code": 0, "score": 0.75}
    # Nothing to report is an empty payload, not invented zeros.
    assert graded[1] == {}
    by_type = {e["type"]: e["payload"] for e in events}
    assert by_type["run_failed"] == {"error_code": "judge_error", "cases_done": 1}
    assert by_type["run_cancelled"] == {"error_code": "interrupted", "cases_done": 1}


def test_run_finished_without_a_score_or_cost_omits_them(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_run_finished(exit_code=3, cases_done=0)
    events = _sent(tracker, client)
    payload = next(e for e in events if e["type"] == "run_finished")["payload"]
    assert payload == {"exit_code": 3, "cases_done": 0, "cases_total": 2}


def test_the_url_exists_before_the_server_answers(tmp_path: Path):
    tracker = _tracker(tmp_path)
    assert tracker.run_url == "https://srv/runs/r-1"


def test_the_url_switches_to_the_canonical_id_once_known(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.close()
    assert tracker.run_url == "https://srv/runs/rs_1"


def test_a_rejected_token_stops_sync_and_says_so_once(tmp_path: Path):
    client = _Recorder(fail=LiveApiError("http 401", status=401))
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("c1")
    tracker.close()
    assert tracker.notice is not None
    assert "rejected" in tracker.notice


def test_being_offline_keeps_the_events_and_says_so_once(tmp_path: Path):
    client = _Recorder(fail=LiveApiError("unreachable"))
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("c1")
    tracker.close()
    assert tracker.notice is not None
    assert "kept locally" in tracker.notice
    # The events are still on disk for a later sync.
    assert len(Outbox(tmp_path).read_all()) == 2


def test_an_unwritable_outbox_disables_sync_without_raising(tmp_path: Path, monkeypatch):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()

    def denied(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", denied)
    tracker.on_case_start("c1")  # must not raise
    monkeypatch.undo()
    assert tracker.notice is not None and "outbox" in tracker.notice
    # Once disabled it stays disabled rather than retrying every case.
    tracker.on_case_start("c2")
    tracker.close()


def test_close_is_a_no_op_when_the_tracker_never_started(tmp_path: Path):
    tracker = _tracker(tmp_path)
    tracker.close()
    assert tracker.notice is None


def test_the_ack_is_persisted_to_the_sidecar(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("c1")
    tracker.close()
    session = LiveSession.load(tmp_path)
    assert session is not None
    assert session.acked_seq >= 1
    assert session.user_id == "usr_a"


# -- setup -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_env_switch_turns_sync_off(monkeypatch, value):
    monkeypatch.setenv("TRAP_NO_LIVE", value)
    assert live_disabled_by_env() is True


@pytest.mark.parametrize("value", ["", "0", "no", "maybe"])
def test_other_env_values_leave_sync_on(monkeypatch, value):
    monkeypatch.setenv("TRAP_NO_LIVE", value)
    assert live_disabled_by_env() is False


def test_no_env_var_leaves_sync_on(monkeypatch):
    monkeypatch.delenv("TRAP_NO_LIVE", raising=False)
    assert live_disabled_by_env() is False


def test_the_flag_turns_sync_off(tmp_path: Path):
    assert start_tracking(run_dir=tmp_path, case_ids=["c1"], enabled=False) is None


def test_an_unpaired_cli_tracks_nothing_and_says_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRAP_NO_LIVE", raising=False)
    monkeypatch.delenv("TRAPSTREET_API_KEY", raising=False)
    monkeypatch.setattr("trap.live.setup.CredentialStore", lambda: _StoreStub(key=None))
    assert start_tracking(run_dir=tmp_path, case_ids=["c1"]) is None


def test_an_unreadable_credential_file_is_not_a_run_problem(tmp_path: Path, monkeypatch):
    from trap.auth.store import CredentialStoreError

    monkeypatch.delenv("TRAP_NO_LIVE", raising=False)

    def boom(*_args, **_kwargs):
        raise CredentialStoreError("bad file")

    monkeypatch.setattr("trap.live.setup.ResolvedAuth.resolve", boom)
    assert start_tracking(run_dir=tmp_path, case_ids=["c1"]) is None


def test_an_unwritable_run_dir_tracks_nothing(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRAP_NO_LIVE", raising=False)
    monkeypatch.setattr("trap.live.setup.CredentialStore", lambda: _StoreStub(key="k"))

    def denied(*_args, **_kwargs):
        raise OSError("read-only")

    monkeypatch.setattr(Path, "mkdir", denied)
    assert start_tracking(run_dir=tmp_path, case_ids=["c1"]) is None


def test_a_paired_cli_starts_tracking(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TRAP_NO_LIVE", raising=False)
    monkeypatch.setattr("trap.live.setup.CredentialStore", lambda: _StoreStub(key="k"))
    sent: list[list[dict]] = []
    monkeypatch.setattr(
        "trap.live.setup.LiveClient",
        lambda *a, **k: _Recorder(),  # type: ignore[arg-type]
    )
    tracker = start_tracking(run_dir=tmp_path, case_ids=["c1"])
    assert tracker is not None
    tracker.close()
    assert LiveSession.load(tmp_path) is not None
    assert sent == []


class _StoreStub:
    """Stands in for CredentialStore: one server, one optional key."""

    def __init__(self, key: str | None) -> None:
        self._key = key

    def load(self, server: str):
        from trap.auth.store import Credential

        if self._key is None:
            return None
        return Credential(server=server, api_key=self._key)


# -- CLI wiring --------------------------------------------------------------


def test_mirrored_without_a_mirror_is_the_primary_callback():
    from trap.cli import _mirrored

    seen: list[str] = []
    callback = _mirrored(seen.append, None)
    callback("x")
    assert seen == ["x"]


def test_mirrored_calls_both_in_order():
    from trap.cli import _mirrored

    order: list[str] = []
    _mirrored(lambda v: order.append(f"terminal:{v}"), lambda v: order.append(f"live:{v}"))("c1")
    assert order == ["terminal:c1", "live:c1"]


def test_a_broken_mirror_cannot_break_the_run():
    from trap.cli import _mirrored

    seen: list[str] = []

    def explode(_value):
        raise RuntimeError("the mirror is on fire")

    _mirrored(seen.append, explode)("c1")  # must not raise
    assert seen == ["c1"]


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({"score": 0.75}, 0.75),
        ({"score": 1}, 1.0),
        ({"score": True}, None),  # a bool is not a score
        ({"score": "high"}, None),
        ({}, None),
        (None, None),
        (["not", "a", "dict"], None),
    ],
)
def test_grader_score_only_reads_a_plain_number(metrics, expected):
    from trap.cli import _grader_score

    assert _grader_score(metrics) == expected


class _FakeTracker:
    """Records what `tp run` asks of a tracker, without any network or disk."""

    def __init__(self, notice: str | None = None) -> None:
        self.run_url = "https://srv/runs/r-1"
        self.client_run_id = "r-1"
        self.notice = notice
        self.calls: list[str] = []
        self.finished: dict[str, object] | None = None

    def on_case_start(self, case_id: str) -> None:
        self.calls.append(f"case_start:{case_id}")

    def on_case_done(self, result) -> None:
        self.calls.append(f"case_done:{result.case_id}")

    def on_run_finished(self, **kwargs) -> None:
        self.finished = kwargs

    def on_run_cancelled(self, cases_done: int) -> None:
        self.calls.append(f"cancelled:{cases_done}")

    def close(self) -> None:
        self.calls.append("closed")


def _use_fake_tracker(monkeypatch, tracker: _FakeTracker) -> None:
    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: tracker)


def test_tp_run_prints_the_run_url_and_mirrors_the_outcome(make_project, runner, monkeypatch):
    from tests.conftest import GRADER_PASS, JUDGE_SCORE
    from trap.cli import app

    tracker = _FakeTracker()
    _use_fake_tracker(monkeypatch, tracker)
    make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        cases=["c1"],
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
        grader_src=GRADER_PASS,
    )
    result = runner.invoke(app, ["run", "--no-environment"])

    assert result.exit_code == 0, result.output
    assert "https://srv/runs/r-1" in result.output
    assert tracker.calls[:2] == ["case_start:c1", "case_done:c1"]
    assert tracker.calls[-1] == "closed"
    assert tracker.finished == {"exit_code": 0, "cases_done": 1, "score": 1.0}


def test_a_sync_notice_is_shown_but_does_not_change_the_exit_code(make_project, runner, monkeypatch):
    from tests.conftest import GRADER_PASS, JUDGE_SCORE
    from trap.cli import app

    _use_fake_tracker(monkeypatch, _FakeTracker(notice="live sync: kept locally"))
    make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        cases=["c1"],
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
        grader_src=GRADER_PASS,
    )
    result = runner.invoke(app, ["run", "--no-environment"])

    assert result.exit_code == 0, result.output
    assert "kept locally" in result.output


def test_ctrl_c_reports_a_cancellation_it_can_confirm(make_project, runner, monkeypatch):
    from trap.cli import app
    from trap.runner import TaskRunner

    tracker = _FakeTracker()
    _use_fake_tracker(monkeypatch, tracker)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(TaskRunner, "run", interrupted)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    runner.invoke(app, ["run", "--no-environment"])

    # A confirmed cancellation, and only ever from here: a SIGKILL leaves no
    # event at all, which the site shows as lost contact rather than failure.
    assert tracker.calls == ["cancelled:0", "closed"]
    assert tracker.finished is None


# -- batching and the remaining edges ----------------------------------------


def test_a_burst_is_coalesced_into_one_batch(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    # Fill the queue without a thread, then drain it synchronously.
    for ordinal in (1, 2):
        tracker._queue.put_nowait({"client_seq": ordinal, "type": "case_started"})
    tracker._queue.put_nowait(tracker._stop)

    assert tracker._drain_once() is False  # the stop sentinel was seen
    assert len(client.batches) == 1
    assert len(client.batches[0]) == 2


def test_the_stop_sentinel_alone_sends_nothing(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker._queue.put_nowait(tracker._stop)
    assert tracker._drain_once() is False
    assert client.batches == []


def test_draining_continues_while_events_keep_arriving(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker._queue.put_nowait({"client_seq": 1, "type": "heartbeat"})
    assert tracker._drain_once() is True  # no sentinel yet
    assert len(client.batches) == 1


def test_a_batch_is_capped(tmp_path: Path, monkeypatch):
    from trap.live import tracker as tracker_module

    monkeypatch.setattr(tracker_module, "MAX_BATCH", 2)
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    for seq in range(1, 4):
        tracker._queue.put_nowait({"client_seq": seq, "type": "heartbeat"})
    tracker._drain_once()
    assert len(client.batches[0]) == 2


def test_undelivered_events_are_reported_once_at_the_end(tmp_path: Path):
    client = _Recorder(fail=LiveApiError("unreachable"))
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("c1")
    tracker.close()
    # Both the "not reaching the site" line and the count line are candidates;
    # only one is ever shown.
    assert tracker.notice is not None
    assert tracker.notice.count("live sync") == 1


def test_the_pending_count_is_reported_when_nothing_else_was(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_start("c1")
    # Never drained: the queue is full and the ack never advanced.
    tracker._thread = object()  # type: ignore[assignment]
    tracker.close()
    assert tracker.notice is not None and "not delivered" in tracker.notice


def test_run_finished_carries_cost_when_it_is_known(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_run_finished(exit_code=0, cases_done=2, score=1.0, cost_usd=0.5)
    events = _sent(tracker, client)
    payload = next(e for e in events if e["type"] == "run_finished")["payload"]
    assert payload["cost_usd"] == 0.5


def test_a_disabled_tracker_opens_no_session_and_sends_nothing(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker._disable("off")
    tracker._ensure_session()
    tracker._send([{"client_seq": 1}])
    assert client.sessions == [] and client.batches == []


def test_the_account_is_only_resolved_once(tmp_path: Path):
    calls: list[int] = []

    class _CountingRecorder(_Recorder):
        def whoami(self):
            calls.append(1)
            return "usr_a"

    client = _CountingRecorder()
    tracker = _tracker(tmp_path, client)
    tracker._ensure_session()
    tracker._ensure_session()
    assert len(calls) == 1


def test_a_session_response_without_an_id_leaves_the_client_id_in_the_url(tmp_path: Path):
    class _NoId(_Recorder):
        def ensure_session(self, client_run_id, **_kwargs):
            return {}

    tracker = _tracker(tmp_path, _NoId())
    tracker._ensure_session()
    assert tracker.run_url == "https://srv/runs/r-1"


def test_the_live_client_builds_a_real_http_client():
    client = LiveClient("https://srv", "key", timeout=1.0)
    assert str(client._client.base_url).rstrip("/") == "https://srv"
    client.close()


def test_ctrl_c_without_a_tracker_still_propagates(make_project, runner, monkeypatch):
    from trap.cli import app
    from trap.runner import TaskRunner

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(TaskRunner, "run", interrupted)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    result = runner.invoke(app, ["run", "--no-environment"])
    assert result.exit_code != 0


def test_a_case_with_nothing_measurable_sends_only_its_ordinal(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    # No verdict (judge ran, no score), no duration, no cost.
    tracker.on_case_done(_result(metrics={"note": "ok"}, duration=0.0, cost=None))
    events = _sent(tracker, client)
    payload = next(e for e in events if e["type"] == "case_finished")["payload"]
    assert payload == {"ordinal": 1}


def test_a_case_with_an_unpriced_model_omits_the_cost(tmp_path: Path):
    client = _Recorder()
    tracker = _tracker(tmp_path, client)
    tracker.start()
    tracker.on_case_done(
        _result(
            metrics={"score": 0},
            cost=CaseCost(by_model=[ModelCost(provider="local", cost_usd=None)]),
        )
    )
    events = _sent(tracker, client)
    payload = next(e for e in events if e["type"] == "case_finished")["payload"]
    # An unknown cost is not a zero cost, so it is simply absent.
    assert "cost_usd" not in payload
    assert payload["verdict"] == "failed"


def test_only_the_first_notice_is_kept(tmp_path: Path):
    tracker = _tracker(tmp_path)
    tracker._disable("first")
    tracker._disable("second")
    assert tracker.notice == "first"


def test_an_ack_that_does_not_advance_is_ignored(tmp_path: Path):
    class _Stuck(_Recorder):
        def send_events(self, _run_ref, events):
            self.batches.append(events)
            return {"ack_seq": 0}

    client = _Stuck()
    tracker = _tracker(tmp_path, client)
    tracker._session.acked_seq = 5
    tracker._send([{"client_seq": 1}])
    assert tracker._session.acked_seq == 5


def test_a_response_without_an_ack_is_ignored(tmp_path: Path):
    class _Silent(_Recorder):
        def send_events(self, _run_ref, events):
            self.batches.append(events)
            return {}

    tracker = _tracker(tmp_path, _Silent())
    tracker._send([{"client_seq": 1}])
    assert tracker._session.acked_seq == 0


# -- the outbox across producer generations ----------------------------------


def test_pending_can_be_scoped_to_one_generation(tmp_path: Path):
    outbox = _outbox(tmp_path)
    outbox.append(event_id="e1", type="heartbeat", payload={}, producer_generation=1)
    outbox.append(event_id="e2", type="heartbeat", payload={}, producer_generation=2)
    # A retired generation is refused by the server for good, so it is not
    # "pending" — counting it would mean retrying it for ever.
    assert [e.event_id for e in outbox.pending(0, generation=2)] == ["e2"]
    assert [e.event_id for e in outbox.pending(0)] == ["e1", "e2"]


def test_prepare_numbers_within_the_generation_it_is_given(tmp_path: Path):
    outbox = _outbox(tmp_path)
    for seq in range(3):
        outbox.append(event_id=f"e{seq}", type="heartbeat", payload={}, producer_generation=1)
    resumed = Outbox(tmp_path)
    resumed.prepare(2)
    # Generation 2 starts its own numbering at 1; the old numbers belong to a
    # generation the server no longer accepts.
    assert resumed.append(event_id="new", type="heartbeat", payload={}).client_seq == 1


# -- the checkpoint endpoint -------------------------------------------------


def test_checkpoint_posts_the_compare_and_swap():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"producer_generation": 2, "ack_seq": 0})

    result = _live_client(handler).checkpoint(
        "rs_1",
        checkpoint_id="cp-1",
        expected_producer_generation=1,
        snapshot={"exec_status": "completed", "cases_done": 2, "cases_total": 2},
    )
    assert result == {"producer_generation": 2, "ack_seq": 0}
    assert seen["path"] == "/api/v2/local-runs/rs_1/checkpoint"
    assert seen["body"] == {
        "checkpoint_id": "cp-1",
        "expected_producer_generation": 1,
        "snapshot": {"exec_status": "completed", "cases_done": 2, "cases_total": 2},
    }


def test_a_conflict_carries_the_generation_the_server_holds():
    handler = lambda _r: httpx.Response(409, json={"producer_generation": 5})  # noqa: E731
    with pytest.raises(LiveApiError) as excinfo:
        _live_client(handler).checkpoint(
            "rs_1", checkpoint_id="cp-1", expected_producer_generation=1, snapshot={}
        )
    assert excinfo.value.payload == {"producer_generation": 5}


@pytest.mark.parametrize("response", [httpx.Response(409, text="<html>"), httpx.Response(409, json=[1])])
def test_an_error_body_that_is_not_an_object_reads_as_empty(response):
    # A server that answers a conflict with HTML must not turn a recoverable
    # conflict into a crash.
    with pytest.raises(LiveApiError) as excinfo:
        _live_client(lambda _r: response).whoami()
    assert excinfo.value.payload == {}


# -- tp sync -----------------------------------------------------------------


def _write_outbox(run_dir: Path, entries: list[tuple[int, str, dict, int]]) -> None:
    """Write an outbox line by line, so a test can create the gap it needs."""
    path = run_dir / "live" / "outbox.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                {
                    "event_id": f"e{seq}",
                    "client_seq": seq,
                    "producer_generation": generation,
                    "type": type_,
                    "payload": payload,
                    "client_occurred_at": "2026-09-07T00:00:00+00:00",
                }
            )
            + "\n"
            for seq, type_, payload, generation in entries
        )
    )


def _queued(
    run_dir: Path,
    entries: list[tuple[int, str, dict, int]] | None = None,
    *,
    acked_seq: int = 0,
    user_id: str | None = "usr_a",
    generation: int = 1,
    run_id: str | None = None,
    server: str = "https://srv",
) -> LiveSession:
    """A run directory as `tp run` would leave it: a sidecar and a queue."""
    if entries is None:
        entries = [(1, "run_started", {"cases_total": 2}, 1), (2, "case_started", {"ordinal": 1}, 1)]
    _write_outbox(run_dir, entries)
    session = LiveSession(
        client_run_id="r-1",
        server=server,
        user_id=user_id,
        run_id=run_id,
        acked_seq=acked_seq,
        producer_generation=generation,
    )
    session.save(run_dir)
    return session


class _SyncClient:
    """A LiveClient stand-in for `tp sync`: scripted answers, recorded calls."""

    def __init__(
        self,
        *,
        user_id: str | None = "usr_a",
        whoami_error: LiveApiError | None = None,
        sends: list[object] | None = None,
        checkpoints: list[object] | None = None,
    ) -> None:
        self.server = "https://srv"
        self._user_id = user_id
        self._whoami_error = whoami_error
        self._sends = list(sends or [])
        self._checkpoints = list(checkpoints or [])
        self.batches: list[list[dict]] = []
        self.refs: list[str] = []
        self.checkpoints_sent: list[dict] = []
        self.closed = False

    def whoami(self) -> str | None:
        if self._whoami_error is not None:
            raise self._whoami_error
        return self._user_id

    def send_events(self, run_ref: str, events: list[dict]):
        self.batches.append(events)
        self.refs.append(run_ref)
        answer = self._sends.pop(0) if self._sends else {"ack_seq": max(e["client_seq"] for e in events)}
        if isinstance(answer, LiveApiError):
            raise answer
        return answer

    def checkpoint(self, run_ref: str, **kwargs):
        self.checkpoints_sent.append({"run_ref": run_ref, **kwargs})
        answer = self._checkpoints.pop(0)
        if isinstance(answer, LiveApiError):
            raise answer
        return answer

    def close(self) -> None:
        self.closed = True


def _run_sync(tmp_path, monkeypatch, client=None, *, key="k", server_override=None, store=None):
    from trap.live.sync import sync_run

    monkeypatch.setattr("trap.live.sync.CredentialStore", lambda: store or _StoreStub(key=key))
    monkeypatch.setattr("trap.live.sync.LiveClient", lambda *_a, **_k: client or _SyncClient())
    return sync_run(tmp_path, server_override=server_override)


def test_an_untracked_run_is_said_plainly_and_is_not_an_error(tmp_path: Path, monkeypatch):
    outcome = _run_sync(tmp_path, monkeypatch)
    assert outcome.status == "untracked"
    assert outcome.refused is False
    assert "never tracked" in outcome.message


def test_a_queue_cannot_be_redirected_to_another_server(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, server_override="https://elsewhere")
    assert outcome.status == "refused"
    assert "cannot move servers" in outcome.message


def test_naming_the_same_server_a_different_way_is_not_a_disagreement(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    client = _SyncClient()
    outcome = _run_sync(tmp_path, monkeypatch, client, server_override="https://srv/")
    assert outcome.status == "delivered"


def test_an_unreadable_credential_store_refuses(tmp_path: Path, monkeypatch):
    _queued(tmp_path)

    class _Broken:
        def load(self, _server):
            raise CredentialStoreError("auth.json is not JSON")

    outcome = _run_sync(tmp_path, monkeypatch, store=_Broken())
    assert outcome.status == "refused"
    assert "auth.json is not JSON" in outcome.message


def test_syncing_without_a_credential_for_that_server_says_where_to_log_in(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, key=None)
    assert outcome.status == "refused"
    assert "tp auth login --server https://srv" in outcome.message


def test_an_empty_queue_needs_no_network(tmp_path: Path, monkeypatch):
    _queued(tmp_path, acked_seq=2)
    client = _SyncClient()
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert outcome.status == "up_to_date"
    assert client.batches == []


def test_events_from_a_retired_generation_are_not_pending(tmp_path: Path, monkeypatch):
    _queued(tmp_path, [(1, "run_started", {}, 1)], generation=2)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient())
    assert outcome.status == "up_to_date"


def test_being_offline_leaves_the_queue_and_is_not_an_error(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(whoami_error=LiveApiError("unreachable")))
    assert (outcome.status, outcome.remaining, outcome.refused) == ("offline", 2, False)
    assert "unreachable" in outcome.message


def test_a_rejected_token_stops_there_and_tries_nothing_else(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    client = _SyncClient(whoami_error=LiveApiError("http 401", status=401))
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert outcome.status == "refused"
    assert client.batches == []
    assert "no other credential was tried" in outcome.message


def test_a_queue_is_never_handed_to_another_account(tmp_path: Path, monkeypatch):
    session = _queued(tmp_path, user_id="usr_a")
    client = _SyncClient(user_id="usr_b")
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert outcome.status == "refused"
    assert client.batches == []
    # And the queue is still on disk, for its owner.
    assert len(Outbox(tmp_path).pending(session.acked_seq)) == 2


def test_a_rotated_token_for_the_same_person_still_delivers(tmp_path: Path, monkeypatch):
    _queued(tmp_path, user_id="usr_a")
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(user_id="usr_a"))
    assert outcome.status == "delivered"


def test_a_run_tracked_entirely_offline_freezes_its_account_on_first_contact(tmp_path: Path, monkeypatch):
    _queued(tmp_path, user_id=None)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(user_id="usr_a"))
    assert outcome.status == "delivered"
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.user_id == "usr_a"


def test_a_server_that_will_not_say_who_we_are_leaves_the_account_unfrozen(tmp_path: Path, monkeypatch):
    _queued(tmp_path, user_id=None)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(user_id=None))
    assert outcome.status == "delivered"
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.user_id is None


def test_delivering_the_queue_records_the_acknowledgement(tmp_path: Path, monkeypatch):
    _queued(tmp_path, run_id="rs_1")
    client = _SyncClient()
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert (outcome.status, outcome.delivered, outcome.remaining) == ("delivered", 2, 0)
    assert client.refs == ["rs_1"]  # the server's own id once it is known
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.acked_seq == 2
    # Nothing to do the second time round.
    assert _run_sync(tmp_path, monkeypatch, _SyncClient()).status == "up_to_date"


def test_a_long_queue_goes_in_batches(tmp_path: Path, monkeypatch):
    entries = [(seq, "heartbeat", {}, 1) for seq in range(1, 251)]
    _queued(tmp_path, entries)
    client = _SyncClient()
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert [len(batch) for batch in client.batches] == [100, 100, 50]
    assert (outcome.status, outcome.delivered) == ("delivered", 250)


def test_losing_the_network_midway_delivers_what_it_can(tmp_path: Path, monkeypatch):
    entries = [(seq, "heartbeat", {}, 1) for seq in range(1, 151)]
    _queued(tmp_path, entries)
    client = _SyncClient(sends=[{"ack_seq": 100}, LiveApiError("unreachable")])
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert (outcome.status, outcome.delivered, outcome.remaining) == ("partial", 100, 50)
    assert "run tp sync again later" in outcome.message
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.acked_seq == 100


def test_a_server_that_acknowledges_nothing_stops_the_send(tmp_path: Path, monkeypatch):
    entries = [(seq, "heartbeat", {}, 1) for seq in range(1, 151)]
    _queued(tmp_path, entries)
    # A conflict: the request was taken, the window did not move. Pushing the
    # next batch would only repeat that.
    conflict = {"ack_seq": 0, "conflicts": [{"event_id": "e1", "reason": "EVENT_CONFLICT"}]}
    client = _SyncClient(sends=[conflict])
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert (outcome.status, outcome.remaining) == ("stalled", 150)
    assert len(client.batches) == 1


def test_a_partly_acknowledged_queue_reports_what_remains(tmp_path: Path, monkeypatch):
    entries = [(seq, "heartbeat", {}, 1) for seq in range(1, 151)]
    _queued(tmp_path, entries)
    # The first batch lands; the second is taken but acknowledges nothing new.
    client = _SyncClient(sends=[{"ack_seq": 100}, {"ack_seq": 100}])
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert (outcome.status, outcome.delivered, outcome.remaining) == ("partial", 100, 50)


def test_a_response_with_no_ack_at_all_stops_the_send(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(sends=[{}]))
    assert outcome.status == "stalled"


def test_a_server_error_keeps_the_queue_without_calling_it_offline(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(sends=[LiveApiError("http 503", status=503)]))
    assert (outcome.status, outcome.remaining) == ("stalled", 2)
    assert "stay on disk" in outcome.message


def test_a_rejected_token_partway_through_still_refuses(tmp_path: Path, monkeypatch):
    _queued(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(sends=[LiveApiError("http 401", status=401)]))
    assert outcome.status == "refused"


# -- gap recovery ------------------------------------------------------------


def _gapped(run_dir: Path, **kwargs) -> LiveSession:
    """A queue whose oldest surviving event is not the next one the server needs."""
    entries = [
        (7, "case_finished", {"ordinal": 1}, 1),
        (8, "run_finished", {"cases_done": 2, "cases_total": 3, "exit_code": 0}, 1),
    ]
    return _queued(run_dir, entries, acked_seq=2, **kwargs)


def test_a_gap_is_declared_with_a_checkpoint_not_papered_over(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(checkpoints=[{"producer_generation": 2, "ack_seq": 0}])
    outcome = _run_sync(tmp_path, monkeypatch, client)

    assert outcome.status == "recovered"
    assert client.batches == []  # sending into a hole is pointless
    sent = client.checkpoints_sent[0]
    assert sent["expected_producer_generation"] == 1
    assert sent["snapshot"] == {"exec_status": "completed", "cases_done": 2, "cases_total": 3}
    assert sent["checkpoint_id"].startswith("cp-")
    # The user is told the history is incomplete, in those words.
    assert "4 progress event(s) were lost" in outcome.message
    assert "incomplete" in outcome.message
    # The new generation is adopted, and its numbering starts fresh.
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None
    assert (reloaded.producer_generation, reloaded.acked_seq) == (2, 0)
    # A second sync has nothing to retry: the old generation is retired.
    assert _run_sync(tmp_path, monkeypatch, _SyncClient()).status == "up_to_date"


def test_a_conflicting_checkpoint_is_retried_once_with_the_server_generation(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(
        checkpoints=[
            LiveApiError("http 409", status=409, payload={"producer_generation": 4}),
            {"producer_generation": 5, "ack_seq": 0},
        ]
    )
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert outcome.status == "recovered"
    assert [c["expected_producer_generation"] for c in client.checkpoints_sent] == [1, 4]
    # The same logical checkpoint, so the retry is idempotent server-side.
    assert len({c["checkpoint_id"] for c in client.checkpoints_sent}) == 1
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.producer_generation == 5


def test_a_conflict_that_names_no_generation_gives_up_cleanly(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(checkpoints=[LiveApiError("http 409", status=409, payload={})])
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert outcome.status == "stalled"
    assert len(client.checkpoints_sent) == 1
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.producer_generation == 1  # nothing changed


def test_a_second_conflict_stops_competing(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(
        checkpoints=[
            LiveApiError("http 409", status=409, payload={"producer_generation": 4}),
            LiveApiError("http 409", status=409, payload={"producer_generation": 6}),
        ]
    )
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert (outcome.status, len(client.checkpoints_sent)) == ("stalled", 2)


def test_a_checkpoint_that_cannot_be_sent_leaves_everything_alone(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(checkpoints=[LiveApiError("unreachable")])
    outcome = _run_sync(tmp_path, monkeypatch, client)
    assert (outcome.status, outcome.remaining) == ("offline", 2)


def test_a_checkpoint_answer_without_a_generation_is_not_adopted(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    outcome = _run_sync(tmp_path, monkeypatch, _SyncClient(checkpoints=[{"ack_seq": 0}]))
    assert outcome.status == "stalled"
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.producer_generation == 1


def test_a_checkpoint_ack_is_adopted_when_the_server_sends_one(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(checkpoints=[{"producer_generation": 2, "ack_seq": 3}])
    _run_sync(tmp_path, monkeypatch, client)
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.acked_seq == 3


def test_a_checkpoint_without_an_ack_restarts_the_window_at_zero(tmp_path: Path, monkeypatch):
    _gapped(tmp_path)
    client = _SyncClient(checkpoints=[{"producer_generation": 2}])
    _run_sync(tmp_path, monkeypatch, client)
    reloaded = LiveSession.load(tmp_path)
    assert reloaded is not None and reloaded.acked_seq == 0


# -- what a checkpoint snapshot may claim ------------------------------------


def _snapshot_of(tmp_path: Path, monkeypatch, entries: list[tuple[int, str, dict, int]]) -> dict:
    _queued(tmp_path, entries, acked_seq=1)
    client = _SyncClient(checkpoints=[{"producer_generation": 2, "ack_seq": 0}])
    _run_sync(tmp_path, monkeypatch, client)
    return client.checkpoints_sent[0]["snapshot"]


def test_a_snapshot_of_a_run_still_in_flight_says_so(tmp_path: Path, monkeypatch):
    snapshot = _snapshot_of(tmp_path, monkeypatch, [(5, "case_started", {"ordinal": 2}, 1)])
    assert snapshot == {"exec_status": "running", "cases_done": 0, "cases_total": 0}


@pytest.mark.parametrize(
    ("event_type", "status"),
    [("run_finished", "completed"), ("run_failed", "failed"), ("run_cancelled", "cancelled")],
)
def test_a_snapshot_reads_the_last_terminal_event(tmp_path: Path, monkeypatch, event_type, status):
    snapshot = _snapshot_of(tmp_path, monkeypatch, [(5, event_type, {"cases_done": 4}, 1)])
    assert snapshot["exec_status"] == status
    # A total below what already ran would be a nonsense to render.
    assert snapshot == {"exec_status": status, "cases_done": 4, "cases_total": 4}


def test_a_snapshot_counts_the_cases_that_survived(tmp_path: Path, monkeypatch):
    snapshot = _snapshot_of(
        tmp_path,
        monkeypatch,
        [
            (5, "case_finished", {"ordinal": 2}, 1),
            (6, "case_finished", {"ordinal": 3}, 1),
            (7, "case_finished", {"ordinal": 3}, 1),  # a duplicate is one case
            (8, "case_finished", {}, 1),  # and a malformed one is no case at all
        ],
    )
    assert (snapshot["cases_done"], snapshot["exec_status"]) == (2, "running")


def test_a_snapshot_ignores_a_count_that_is_not_a_number(tmp_path: Path, monkeypatch):
    snapshot = _snapshot_of(
        tmp_path, monkeypatch, [(5, "run_finished", {"cases_done": "lots", "cases_total": 9}, 1)]
    )
    assert (snapshot["cases_done"], snapshot["cases_total"]) == (0, 9)


def test_a_snapshot_falls_back_to_the_saved_report(tmp_path: Path, monkeypatch):
    from trap.models import CaseResult, Provenance, ReportData

    report = ReportData(
        provenance=Provenance(),
        cases_results=(
            CaseResult(case_id="c1", metrics=None),
            CaseResult(case_id="c2", metrics=None),
        ),
        grader_metrics=None,
        started_at_utc="2026-09-07T00:00:00+00:00",
        finished_at_utc="2026-09-07T00:00:01+00:00",
    )
    (tmp_path / "report.json").write_text(report.model_dump_json())
    # No terminal event survived, but a report on disk is only ever written by a
    # run that finished.
    snapshot = _snapshot_of(tmp_path, monkeypatch, [(5, "case_started", {"ordinal": 1}, 1)])
    assert snapshot == {"exec_status": "completed", "cases_done": 2, "cases_total": 2}


def test_a_corrupt_report_is_read_as_no_report(tmp_path: Path, monkeypatch):
    (tmp_path / "report.json").write_text("{not json")
    snapshot = _snapshot_of(tmp_path, monkeypatch, [(5, "case_started", {"ordinal": 1}, 1)])
    assert snapshot["exec_status"] == "running"


# -- the tp sync command -----------------------------------------------------


def _run_dir_of(project: Path) -> Path:
    return next((project / ".trap" / "runs").glob("*/t/*"))


def _a_finished_run(make_project, runner, monkeypatch) -> Path:
    """Run something small with sync off, and return the run directory."""
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    project = make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    return _run_dir_of(project)


def test_tp_sync_on_an_untracked_run_says_so_and_exits_zero(make_project, runner, monkeypatch):
    from trap.cli import app

    _a_finished_run(make_project, runner, monkeypatch)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "never tracked" in result.output


def test_tp_sync_delivers_a_queued_run(make_project, runner, monkeypatch):
    from trap.cli import app

    run_dir = _a_finished_run(make_project, runner, monkeypatch)
    _queued(run_dir)
    client = _SyncClient()
    monkeypatch.setattr("trap.live.sync.CredentialStore", lambda: _StoreStub(key="k"))
    monkeypatch.setattr("trap.live.sync.LiveClient", lambda *_a, **_k: client)

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "delivered 2 queued event(s)" in result.output
    assert client.closed is True


def test_tp_sync_exits_two_when_it_refuses(make_project, runner, monkeypatch):
    from trap.cli import app

    run_dir = _a_finished_run(make_project, runner, monkeypatch)
    _queued(run_dir, user_id="usr_a")
    monkeypatch.setattr("trap.live.sync.CredentialStore", lambda: _StoreStub(key="k"))
    monkeypatch.setattr("trap.live.sync.LiveClient", lambda *_a, **_k: _SyncClient(user_id="usr_b"))

    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2
    assert "different account" in result.output


def test_tp_sync_without_any_run_points_at_tp_run(make_project, runner, monkeypatch):
    from trap.cli import app

    make_project(cmd="sh -c 'cat'", cases=["c1"])
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2
    assert "tp run" in result.output


def test_tp_sync_names_a_run_that_is_not_there(make_project, runner, monkeypatch):
    from trap.cli import app

    _a_finished_run(make_project, runner, monkeypatch)
    result = runner.invoke(app, ["sync", "--run", "2020-01-01T00:00:00"])
    assert result.exit_code == 2
    assert "no run 2020-01-01T00:00:00" in result.output


def test_tp_sync_needs_a_solution_it_can_read(runner, tmp_path, monkeypatch):
    from trap.cli import app

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 2


def test_tp_run_records_the_session_in_the_report(make_project, runner, monkeypatch):
    from trap.cli import app

    tracker = _FakeTracker()
    _use_fake_tracker(monkeypatch, tracker)
    project = make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0

    report = json.loads((_run_dir_of(project) / "report.json").read_text())
    # What lets `tp submit` hand the website the same run the site already watched.
    assert report["client_run_id"] == "r-1"


def test_a_run_with_no_session_reports_none(make_project, runner, monkeypatch):
    run_dir = _a_finished_run(make_project, runner, monkeypatch)
    assert json.loads((run_dir / "report.json").read_text())["client_run_id"] is None


def test_the_tracker_publishes_the_id_the_report_needs(tmp_path: Path):
    assert _tracker(tmp_path).client_run_id == "r-1"

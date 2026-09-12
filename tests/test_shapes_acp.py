"""tp shape acp: one case through an agent that speaks ACP, against a scripted agent."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from trap.shapes._case import Deadline, ShapeExit
from trap.shapes.acp.connection import AGENT_EXITED, AcpConnection, AcpError
from trap.shapes.acp.session import (
    ConfigMismatch,
    MessageCollector,
    apply_config,
    describe_agent,
    grant_once,
    option_values,
    reported_no_model_use,
    run_case,
)

from .conftest import PY, process_gone

FAKE = Path(__file__).with_name("fake_acp_agent.py")
AGENT = [PY, str(FAKE)]


def _log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _sent(path: Path, method: str) -> list[dict]:
    return [e["received"] for e in _log(path) if e.get("received", {}).get("method") == method]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    """Pick the scripted agent's mode; returns the path its log goes to."""
    log = tmp_path / "agent.log"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))

    def _mode(mode: str) -> Path:
        monkeypatch.setenv("FAKE_ACP_MODE", mode)
        return log

    return _mode


def _connect(tmp_path: Path, updates: list, on_request=None, on_update=None) -> AcpConnection:
    def refuse(method, params):
        raise AcpError(-32601, f"no {method}")

    return AcpConnection(
        AGENT,
        env=dict(os.environ),
        cwd=tmp_path,
        on_update=on_update or updates.append,
        on_request=on_request or refuse,
    )


def test_requests_get_their_results_and_updates_stream_in(fake, tmp_path):
    fake("ok")
    updates: list = []
    conn = _connect(tmp_path, updates)
    try:
        assert (
            conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)["protocolVersion"]
            == 1
        )
        session = conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        assert session["sessionId"] == "s1"
        result = conn.call(
            "session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10
        )
    finally:
        conn.close()
    assert result["stopReason"] == "end_turn"
    assert [u["sessionUpdate"] for u in updates].count("agent_message_chunk") == 3


def test_an_agent_error_is_raised_with_its_code(fake, tmp_path):
    fake("ok")
    conn = _connect(tmp_path, [])
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        with pytest.raises(AcpError) as e:
            conn.call(
                "session/set_config_option", {"sessionId": "s1", "configId": "model", "value": "opus"}, 10
            )
    finally:
        conn.close()
    assert e.value.code == -32602


def test_an_agent_that_exits_fails_the_pending_request(fake, tmp_path):
    fake("crash")
    conn = _connect(tmp_path, [])
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        with pytest.raises(AcpError) as e:
            conn.call("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10)
    finally:
        conn.close()
    assert e.value.code == AGENT_EXITED


def test_the_agents_own_requests_are_answered_by_the_handler(fake, tmp_path):
    log = fake("permission")
    conn = _connect(tmp_path, [], on_request=lambda method, params: {"outcome": {"outcome": "cancelled"}})
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        conn.call("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10)
    finally:
        conn.close()
    reply = next(e["permission_reply"] for e in _log(log) if "permission_reply" in e)
    assert reply == {"jsonrpc": "2.0", "id": "perm-1", "result": {"outcome": {"outcome": "cancelled"}}}


def test_a_timed_out_wait_can_still_collect_the_late_response(fake, tmp_path):
    fake("hang")
    conn = _connect(tmp_path, [])
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        pending = conn.request(
            "session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}
        )
        with pytest.raises(TimeoutError):
            pending.wait(0.3)
        conn.notify("session/cancel", {"sessionId": "s1"})
        assert pending.wait(10) == {"stopReason": "cancelled"}
    finally:
        conn.close()


# --- Ruling 1: the reader thread must never die silently -------------------------------


def test_an_on_update_that_raises_fails_the_in_flight_request_promptly(fake, tmp_path):
    fake("ok")

    def bad_update(update: dict) -> None:
        raise TypeError("boom")

    def unused(method: str, params: dict) -> dict:
        raise AcpError(-32601, f"no {method}")

    conn = AcpConnection(AGENT, env=dict(os.environ), cwd=tmp_path, on_update=bad_update, on_request=unused)
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        started = time.monotonic()
        with pytest.raises(AcpError) as e:
            conn.call("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10)
        elapsed = time.monotonic() - started
        assert elapsed < 2, "the request should fail as soon as on_update blows up, not at its own timeout"
        assert e.value.message.startswith("trap could not handle a message from the agent:")
        assert "boom" in e.value.message

        # every request made afterwards fails the same way — no fresh attempt to talk to
        # a reader thread that is no longer running.
        with pytest.raises(AcpError) as e2:
            conn.call("session/cancel", {"sessionId": "s1"}, 10)
        assert e2.value.code == e.value.code
        assert e2.value.message == e.value.message
    finally:
        conn.close()


def test_the_handlers_acp_error_becomes_the_agents_error_reply(fake, tmp_path):
    log = fake("permission")
    conn = _connect(
        tmp_path, [], on_request=lambda method, params: (_ for _ in ()).throw(AcpError(-32000, "denied"))
    )
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        conn.call("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10)
    finally:
        conn.close()
    reply = next(e["permission_reply"] for e in _log(log) if "permission_reply" in e)
    assert reply == {"jsonrpc": "2.0", "id": "perm-1", "error": {"code": -32000, "message": "denied"}}


def test_a_buggy_on_request_handler_fails_the_connection_too(fake, tmp_path):
    fake("permission")

    def buggy(method: str, params: dict) -> dict:
        raise KeyError("no such field")

    conn = _connect(tmp_path, [], on_request=buggy)
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        with pytest.raises(AcpError) as e:
            conn.call("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10)
        assert e.value.message.startswith("trap could not handle a message from the agent:")
    finally:
        conn.close()


# --- Coverage: lines no protocol-shaped conversation reaches on its own ----------------


def test_non_json_and_non_object_stdout_lines_are_skipped(fake, tmp_path):
    fake("garbage")
    updates: list = []
    conn = _connect(tmp_path, updates)
    try:
        assert (
            conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)["protocolVersion"]
            == 1
        )
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        result = conn.call(
            "session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10
        )
    finally:
        conn.close()
    assert result["stopReason"] == "end_turn"


def test_a_response_for_an_unknown_id_and_a_bare_message_are_ignored(fake, tmp_path):
    fake("stray")
    conn = _connect(tmp_path, [])
    try:
        assert (
            conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)["protocolVersion"]
            == 1
        )
        # if the stray messages had confused the reader, this second round trip would
        # never come back.
        session = conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        assert session["sessionId"] == "s1"
    finally:
        conn.close()


def test_a_write_after_close_hits_a_closed_file(fake, tmp_path):
    """close() closes our end cleanly (the agent is still alive, just told to stop via
    EOF), so a write after that lands on an already-closed file: _send's ValueError
    branch, not the broken-pipe OSError branch below."""
    fake("ok")
    conn = _connect(tmp_path, [])
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
    finally:
        conn.close()
    conn.notify("session/cancel", {"sessionId": "s1"})  # must not raise

    with pytest.raises(AcpError) as e:
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 1)
    assert e.value.code == AGENT_EXITED
    assert e.value.message == "the agent exited"


def test_close_tolerates_a_broken_pipe_from_a_write_after_the_agent_crashed(fake, tmp_path):
    """The crashed agent's process (and its end of the stdin pipe) is gone once the
    reader thread notices, but our own stdin is still open: notify() after that hits a
    broken pipe — _send's OSError branch, not the closed-file one above — and, since the
    write's bytes never left our buffer, close() then re-attempts the same failing flush
    and must swallow the OSError that comes out of *its* stdin.close() too."""
    fake("crash")
    conn = _connect(tmp_path, [])
    try:
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        conn.call("session/new", {"cwd": str(tmp_path), "mcpServers": []}, 10)
        with pytest.raises(AcpError) as e:
            conn.call("session/prompt", {"sessionId": "s1", "prompt": [{"type": "text", "text": "q"}]}, 10)
        assert e.value.code == AGENT_EXITED
        conn.notify("session/cancel", {"sessionId": "s1"})  # writes into the broken pipe; must not raise
    finally:
        conn.close()  # must not raise either, despite the unflushed bytes from notify()


def test_close_escalates_to_sigkill_for_an_agent_that_ignores_sigterm(fake, tmp_path):
    log = fake("hang_hard")
    conn = _connect(tmp_path, [])
    try:
        # the child is spawned, and its pid logged, before the agent reads its first message
        conn.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        agent_pid = conn._proc.pid
        child_pid = next(e["child_pid"] for e in _log(log) if "child_pid" in e)
    finally:
        conn.close(grace=0.3)

    assert process_gone(agent_pid), "the agent itself outlived close()"
    assert process_gone(child_pid), "the agent's own child outlived close()"


# --- Task 4: one case over ACP ---------------------------------------------------------


def _chunk(text: str, mid: str | None = None) -> dict:
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    if mid is not None:
        update["messageId"] = mid
    return update


def test_only_the_last_message_is_the_answer():
    c = MessageCollector()
    for u in [
        _chunk("Let me look.", "m1"),
        {"sessionUpdate": "tool_call"},
        _chunk("4", "m2"),
        _chunk("2", "m2"),
    ]:
        c.on_update(u)
    assert (c.messages, c.final) == (["Let me look.", "42"], "42")


def test_without_message_ids_a_tool_call_starts_a_new_message():
    c = MessageCollector()
    for u in [_chunk("thinking "), _chunk("aloud"), {"sessionUpdate": "tool_call"}, _chunk("final")]:
        c.on_update(u)
    assert c.messages == ["thinking aloud", "final"]


def test_a_lone_chunk_with_no_message_id_still_starts_its_own_message():
    """The very first update, with no messageId: ``mid is None and self._boundary`` is
    the only true disjunct, and it alone must be enough to start a message — this is the
    "no-message-id path"'s branch where there is no prior tool call to set the boundary."""
    c = MessageCollector()
    c.on_update(_chunk("solo"))
    assert c.messages == ["solo"]


@pytest.mark.parametrize(
    ("result", "none"),
    [
        ({"stopReason": "end_turn", "usage": {"inputTokens": 5, "outputTokens": 1}}, False),
        ({"stopReason": "end_turn", "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}}, True),
        ({"stopReason": "end_turn", "usage": None, "_meta": {"quota": {"model_usage": []}}}, True),
        ({"stopReason": "end_turn"}, False),  # an agent that reports nothing is not accused
    ],
)
def test_reported_no_model_use(result, none):
    assert reported_no_model_use(result) is none


class _Conn:
    """Answers session/set_config_option without echoing configOptions, recording calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, method, params, timeout):
        self.calls.append((method, params))
        return {}


def _options(model: str = "default", effort: str = "default") -> list[dict]:
    return [
        {
            "id": "model",
            "category": "model",
            "currentValue": model,
            "options": [{"value": v} for v in ("default", "sonnet", "haiku")],
        },
        {
            "id": "effort",
            "category": "thought_level",
            "currentValue": effort,
            "options": [{"group": "g", "options": [{"value": "low"}, {"value": "high"}]}],
        },
    ]


def test_the_model_must_be_one_the_agent_lists():
    with pytest.raises(ConfigMismatch) as e:
        apply_config(_Conn(), "s1", _options(), model="claude-sonnet-5", options={}, timeout=5)
    assert "default, sonnet, haiku" in str(e.value)


def test_config_sets_only_what_differs_and_reads_grouped_values():
    conn = _Conn()
    ran = apply_config(
        conn, "s1", _options(model="haiku"), model="haiku", options={"effort": "low"}, timeout=5
    )
    assert conn.calls == [
        ("session/set_config_option", {"sessionId": "s1", "configId": "effort", "value": "low"})
    ]
    assert ran == {"model": "haiku", "effort": "low"}


def test_an_unknown_option_is_named_with_the_ones_there_are():
    with pytest.raises(ConfigMismatch) as e:
        apply_config(_Conn(), "s1", _options(), model="haiku", options={"speed": "fast"}, timeout=5)
    assert "speed" in str(e.value) and "model, effort" in str(e.value)


def test_apply_config_raises_when_the_value_offered_is_not_in_the_list():
    with pytest.raises(ConfigMismatch) as e:
        apply_config(_Conn(), "s1", _options(), model="haiku", options={"effort": "medium"}, timeout=5)
    assert "low, high" in str(e.value)


class _EchoingConn:
    """Answers session/set_config_option by echoing back configOptions, like a real agent."""

    def __init__(self, options: list[dict], *, mismatch: bool = False) -> None:
        self._options = options
        self._mismatch = mismatch

    def call(self, method, params, timeout):
        if params["configId"] == "model" and not self._mismatch:
            self._options[0]["currentValue"] = params["value"]
        return {"configOptions": self._options}


def test_a_setting_that_does_not_take_is_a_config_mismatch():
    conn = _EchoingConn(_options(), mismatch=True)
    with pytest.raises(ConfigMismatch) as e:
        apply_config(conn, "s1", conn._options, model="haiku", options={}, timeout=5)
    assert "stayed" in str(e.value)


def test_apply_config_reads_back_echoed_values_when_they_do_take():
    conn = _EchoingConn(_options())
    ran = apply_config(conn, "s1", conn._options, model="haiku", options={}, timeout=5)
    assert ran == {"model": "haiku", "effort": "default"}


def _run(
    tmp_path: Path, *, model: str = "haiku", options=None, deadline: float = 20.0, cancel_grace: float = 0.5
):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    return run_case(
        AGENT,
        env=dict(os.environ),
        workdir=work,
        question="what is 6*7?",
        model=model,
        options=options or {},
        meta=None,
        deadline=Deadline(deadline),
        cancel_grace=cancel_grace,
    )


def test_a_case_answers_with_the_agents_last_message(fake, tmp_path):
    log = fake("ok")
    out = _run(tmp_path, options={"effort": "low"})
    assert (out.answer, out.exit_code) == ("42", ShapeExit.OK)
    assert _sent(log, "initialize")[0]["params"] == {"protocolVersion": 1, "clientCapabilities": {}}
    new = _sent(log, "session/new")[0]["params"]
    assert new["mcpServers"] == [] and Path(new["cwd"]).is_absolute()
    assert "_meta" not in new
    sets = [(m["params"]["configId"], m["params"]["value"]) for m in _sent(log, "session/set_config_option")]
    assert sets == [("model", "haiku"), ("effort", "low")]
    assert any("model=haiku" in n for n in out.notes)
    assert any("self-reported session cost" in n for n in out.notes)


@pytest.mark.parametrize(
    ("mode", "answer", "code"),
    [
        ("no_message_id", "final answer", ShapeExit.OK),
        ("refusal", "I can't help with that.", ShapeExit.REFUSAL),
        ("auth_error", "", ShapeExit.AGENT_ERROR),
        ("no_model_use", "", ShapeExit.AGENT_ERROR),
        ("crash", "", ShapeExit.AGENT_ERROR),
    ],
)
def test_how_a_turn_ends_decides_answer_and_exit(fake, tmp_path, mode, answer, code):
    fake(mode)
    out = _run(tmp_path)
    assert (out.answer, out.exit_code) == (answer, code)


def test_permission_is_granted_once_never_always(fake, tmp_path):
    log = fake("permission")
    out = _run(tmp_path)
    assert out.answer == "perm-ok"
    reply = next(e["permission_reply"] for e in _log(log) if "permission_reply" in e)
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "once"}}


def test_permission_falls_back_to_reject_once_then_cancelled(fake, tmp_path):
    log = fake("permission_no_once")
    out = _run(tmp_path)
    assert out.answer == "perm-ok"
    reply = next(e["permission_reply"] for e in _log(log) if "permission_reply" in e)
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "no"}}


def test_permission_is_cancelled_when_nothing_is_offered_to_grant(fake, tmp_path):
    log = fake("permission_none_offered")
    out = _run(tmp_path)
    assert out.answer == "perm-ok"
    reply = next(e["permission_reply"] for e in _log(log) if "permission_reply" in e)
    assert reply["result"] == {"outcome": {"outcome": "cancelled"}}


def test_an_unlisted_model_fails_before_the_prompt(fake, tmp_path):
    log = fake("ok")
    out = _run(tmp_path, model="opus")
    assert out.exit_code == ShapeExit.CONFIG_ERROR
    assert any("default, sonnet, haiku" in n for n in out.notes)
    assert _sent(log, "session/prompt") == []


def test_at_the_deadline_the_session_is_cancelled(fake, tmp_path):
    # Ruling 2: 1s for the whole spawn+handshake+prompt risks a flake on a slow runner;
    # 3s gives real headroom while still finishing well under a human-noticeable wait.
    log = fake("hang")
    started = time.monotonic()
    out = _run(tmp_path, deadline=3.0)
    assert (out.answer, out.exit_code) == ("partial", ShapeExit.TIMEOUT)
    assert _sent(log, "session/cancel") != []
    assert time.monotonic() - started < 10


def test_an_agent_that_ignores_cancel_is_killed_with_its_children(fake, tmp_path):
    log = fake("hang_hard")
    out = _run(tmp_path, deadline=1.0, cancel_grace=0.5)
    assert out.exit_code == ShapeExit.TIMEOUT
    pid = next(e["child_pid"] for e in _log(log) if "child_pid" in e)
    assert process_gone(pid), "the agent's child outlived the case"


def test_describe_lists_the_options_without_prompting(fake, tmp_path):
    log = fake("ok")
    options = describe_agent(AGENT, env=dict(os.environ), cwd=tmp_path, meta=None)
    assert {o["id"]: o["values"] for o in options} == {
        "model": ["default", "sonnet", "haiku"],
        "effort": ["default", "low", "high"],
    }
    assert _sent(log, "session/prompt") == []


# --- Ruling 3: meta must be tested ------------------------------------------------------


def test_meta_reaches_session_new_as_meta(fake, tmp_path):
    log = fake("ok")
    work = tmp_path / "work"
    work.mkdir()
    run_case(
        AGENT,
        env=dict(os.environ),
        workdir=work,
        question="q",
        model="haiku",
        options={},
        meta={"run_id": "r1"},
        deadline=Deadline(20.0),
    )
    params = _sent(log, "session/new")[0]["params"]
    assert params["_meta"] == {"run_id": "r1"}


@pytest.mark.parametrize("meta", [None, {}])
def test_no_meta_key_when_meta_is_empty(fake, tmp_path, meta):
    log = fake("ok")
    work = tmp_path / "work"
    work.mkdir()
    run_case(
        AGENT,
        env=dict(os.environ),
        workdir=work,
        question="q",
        model="haiku",
        options={},
        meta=meta,
        deadline=Deadline(20.0),
    )
    params = _sent(log, "session/new")[0]["params"]
    assert "_meta" not in params


def test_describe_agent_passes_meta_too(fake, tmp_path):
    log = fake("ok")
    describe_agent(AGENT, env=dict(os.environ), cwd=tmp_path, meta={"run_id": "r2"})
    params = _sent(log, "session/new")[0]["params"]
    assert params["_meta"] == {"run_id": "r2"}


# --- Ruling 4: a malformed agent message ends the case at ShapeExit.AGENT_ERROR --------


def test_a_malformed_update_ends_the_case_promptly_as_agent_error(fake, tmp_path):
    fake("null_chunk")
    started = time.monotonic()
    out = _run(tmp_path, deadline=20.0)
    assert out.exit_code == ShapeExit.AGENT_ERROR
    assert out.answer == ""
    assert time.monotonic() - started < 5, "a handler bug must not be mistaken for a hung agent"
    assert any("trap could not handle a message from the agent" in n for n in out.notes)


# --- Ruling 5: coverage for session-open error paths and describe_agent errors ----------


def test_a_session_new_that_fails_is_an_agent_error(fake, tmp_path):
    fake("bad_session_new")
    out = _run(tmp_path)
    assert out.exit_code == ShapeExit.AGENT_ERROR
    assert any("could not open a session" in n for n in out.notes)


def test_describe_agent_surfaces_a_broken_handshake(fake, tmp_path):
    fake("bad_session_new")
    with pytest.raises(AcpError):
        describe_agent(AGENT, env=dict(os.environ), cwd=tmp_path, meta=None)


def test_a_deadline_that_expires_before_the_handshake_finishes_is_a_timeout(fake, tmp_path):
    fake("hang_handshake")
    out = _run(tmp_path, deadline=0.3)
    assert out.exit_code == ShapeExit.TIMEOUT
    assert any("deadline reached before the question was sent" in n for n in out.notes)


def test_an_unmapped_stop_reason_is_an_agent_error(fake, tmp_path):
    fake("unknown_stop")
    out = _run(tmp_path)
    assert out.exit_code == ShapeExit.AGENT_ERROR
    assert any("unexpected stopReason" in n for n in out.notes)


# --- Coverage: branches no scripted-agent conversation reaches on its own --------------


def test_message_collector_ignores_an_update_kind_it_does_not_know():
    c = MessageCollector()
    c.on_update({"sessionUpdate": "plan", "entries": []})
    assert c.messages == [] and c.cost is None


def test_grant_once_refuses_any_method_other_than_permission():
    with pytest.raises(AcpError) as e:
        grant_once([])("some/other_method", {})
    assert "some/other_method" in str(e.value)


def test_option_values_skips_entries_that_are_not_mappings():
    assert option_values({"options": ["not-a-dict", {"value": "x"}]}) == ["x"]


def test_apply_config_raises_when_the_agent_has_no_model_option_at_all():
    no_model = [
        {
            "id": "effort",
            "category": "thought_level",
            "currentValue": "default",
            "options": [{"value": "default"}],
        }
    ]
    with pytest.raises(ConfigMismatch) as e:
        apply_config(_Conn(), "s1", no_model, model="haiku", options={}, timeout=5)
    assert "offers no model option" in str(e.value)

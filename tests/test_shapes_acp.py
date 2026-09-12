"""tp shape acp: one case through an agent that speaks ACP, against a scripted agent."""

from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path

import pytest

from trap.cli import app
from trap.shapes._case import Deadline, ShapeError, ShapeExit
from trap.shapes.acp import bridge, hints
from trap.shapes.acp.connection import AGENT_EXITED, AcpConnection, AcpError
from trap.shapes.acp.session import (
    ConfigMismatch,
    MessageCollector,
    _open_session,
    apply_config,
    describe_agent,
    grant_once,
    option_values,
    reported_no_model_use,
    run_case,
)

from .conftest import JUDGE_SCORE, PY, case_capture, process_gone

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


# --- the reader thread never dies silently: a handler bug fails every pending request --


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


# --- one case over ACP: config, the last-message answer, permissions, the deadline -----


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


def test_a_turn_that_ends_right_after_a_tool_call_has_an_empty_answer():
    """A no-messageId turn that ends on a tool call, with no text after it, must answer
    with "" — not with whatever it said before the tool call. The tool call closes that
    message immediately rather than waiting for a chunk that never comes."""
    c = MessageCollector()
    for u in [_chunk("thinking "), _chunk("aloud"), {"sessionUpdate": "tool_call"}]:
        c.on_update(u)
    assert c.messages == ["thinking aloud", ""]
    assert c.final == ""


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


# --- timeout is a total budget across a function's calls, not a fresh one per call -----


class _SlowConn:
    """A recording conn (like ``_Conn``) that takes a moment to answer, so a caller that
    treats ``timeout`` as a total budget must pass its second call a smaller value than
    its first — one that already spent some of the budget waiting for the first reply."""

    def __init__(self, delay: float) -> None:
        self.timeouts: list[float] = []
        self._delay = delay

    def call(self, method, params, timeout):
        self.timeouts.append(timeout)
        time.sleep(self._delay)
        return {}


def test_apply_config_treats_timeout_as_a_total_budget_not_per_call():
    conn = _SlowConn(0.2)
    apply_config(conn, "s1", _options(), model="haiku", options={"effort": "low"}, timeout=5)
    assert len(conn.timeouts) == 2
    assert conn.timeouts[1] < conn.timeouts[0]


def test_open_session_treats_timeout_as_a_total_budget_not_per_call(tmp_path):
    conn = _SlowConn(0.2)
    _open_session(conn, workdir=tmp_path, meta=None, timeout=5)
    assert len(conn.timeouts) == 2
    assert conn.timeouts[1] < conn.timeouts[0]


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
    # 1s for the whole spawn+handshake+prompt risks a flake on a slow runner; 3s gives
    # real headroom while still finishing well under a human-noticeable wait.
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


# --- meta reaches session/new as _meta, present or absent ------------------------------


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


# --- a malformed agent message ends the case at ShapeExit.AGENT_ERROR ------------------


def test_a_malformed_update_ends_the_case_promptly_as_agent_error(fake, tmp_path):
    """``null_chunk``'s ``content.text: null`` makes MessageCollector.on_update raise
    (``None`` is not a str to concatenate) — deliberately: the collector stays strict
    about shape instead of silently swallowing a bad field. AcpConnection's reader thread
    turns that raise into an AcpError that fails the in-flight session/prompt; this test
    is not about the collector but about proving _converse maps *that* AcpError to
    AGENT_ERROR, promptly, like any other agent-side failure — not into a hang until the
    deadline, and not mislabelled as the agent having crashed."""
    fake("null_chunk")
    started = time.monotonic()
    out = _run(tmp_path, deadline=20.0)
    assert out.exit_code == ShapeExit.AGENT_ERROR
    assert out.answer == ""
    assert time.monotonic() - started < 5, "a handler bug must not be mistaken for a hung agent"
    assert any("trap could not handle a message from the agent" in n for n in out.notes)


# --- session-open error paths (config mismatch, timeout, agent error) and describe_agent
# errors -------------------------------------------------------------------------------


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


# --- what tp knows about particular agents (hints.py) -----------------------------------


def test_claude_is_kept_away_from_the_runners_own_settings():
    assert hints.session_meta("claude-acp") == {"claudeCode": {"options": {"settingSources": ["project"]}}}
    assert hints.session_meta("codex-acp") is None
    assert hints.session_meta(None) is None


@pytest.mark.parametrize(
    ("auth", "key", "proxied"),
    [
        ({"auth_mode": "chatgpt"}, "sk", False),
        ({"auth_mode": "apikey"}, None, True),
        (None, "sk", True),
        (None, None, False),
    ],
)
def test_codex_is_pointed_at_the_proxy_only_under_an_api_key_login(tmp_path, auth, key, proxied):
    auth_file = tmp_path / "auth.json"
    if auth is not None:
        auth_file.write_text(json.dumps(auth))
    env = {"OPENAI_BASE_URL": "http://127.0.0.1:9", **({"OPENAI_API_KEY": key} if key else {})}
    got = hints.extra_env("codex-acp", env, codex_auth=auth_file)
    expected = {"CODEX_CONFIG": json.dumps({"openai_base_url": "http://127.0.0.1:9"})} if proxied else {}
    assert got == expected
    assert hints.extra_env("claude-acp", env, codex_auth=auth_file) == {}


def test_a_skill_goes_where_claude_loads_project_skills(tmp_path):
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\n")
    work = tmp_path / "work"
    work.mkdir()
    hints.install_skill("claude-acp", skill, work)
    assert (work / ".claude" / "skills" / "my-skill" / "SKILL.md").is_file()
    with pytest.raises(ShapeError):
        hints.install_skill("codex-acp", skill, work)


def test_install_skill_requires_a_skill_md(tmp_path):
    skill = tmp_path / "empty-skill"
    skill.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(ShapeError) as e:
        hints.install_skill("claude-acp", skill, work)
    assert "SKILL.md" in str(e.value)


# --- tp shape acp: end to end, the way a trap.yaml `cmd:` line actually runs it ---------


def _bridge_cmd(*extra: str) -> str:
    return shlex.join(
        [
            PY,
            "-m",
            "trap.shapes.acp",
            "--agent-cmd",
            shlex.join(AGENT),
            "--model",
            "haiku",
            "--deadline",
            "30",
            *extra,
        ]
    )


def test_the_bridge_is_a_solution_like_any_other(make_project, runner, fake, tmp_path, monkeypatch):
    log = fake("ok")
    monkeypatch.setenv("POINTER", str(tmp_path / "task"))
    sol = make_project(
        cmd=_bridge_cmd("--scrub", str(tmp_path / "task")),
        inputs={"c1": {"question.txt": "what is 6*7?", "notes.txt": "n"}},
        expected={"c1": {"answer.txt": "42"}},
        judge_src=JUDGE_SCORE,
    )
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    out, meta = case_capture(sol)
    assert (out.strip(), meta["exit_code"]) == ("42", 0)
    start = next(e for e in _log(log) if "cwd" in e)
    assert not Path(start["cwd"]).is_relative_to(tmp_path.resolve())
    assert start["files"] == ["notes.txt", "question.txt"]
    assert "TRAP_MANIFEST" not in start["env"] and "POINTER" not in start["env"]
    stderr = (next((sol / ".trap").rglob("c1/solution/stderr"))).read_text()
    assert "agent config: effort=default, model=haiku" in stderr
    report = json.loads(next((sol / ".trap").rglob("report.json")).read_text())
    assert report["cases_results"][0]["metrics"] == {"score": 1.0}


def test_a_turn_that_is_not_an_answer_leaves_stdout_empty(make_project, runner, fake):
    fake("no_model_use")
    sol = make_project(cmd=_bridge_cmd(), inputs={"c1": {"question.txt": "q"}})
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    out, meta = case_capture(sol)
    assert (out, meta["exit_code"]) == ("", ShapeExit.AGENT_ERROR)


def test_tp_shape_acp_describe_prints_the_values_model_accepts(runner, fake):
    fake("ok")
    res = runner.invoke(app, ["shape", "acp", "--agent-cmd", shlex.join(AGENT), "--describe"])
    assert res.exit_code == 0, res.output
    options = json.loads(res.stdout)
    assert options[0] == {
        "id": "model",
        "category": "model",
        "current": "default",
        "values": ["default", "sonnet", "haiku"],
    }


def test_tp_shape_acp_without_a_model_says_where_to_find_one(runner):
    res = runner.invoke(app, ["shape", "acp", "--agent-cmd", shlex.join(AGENT)])
    assert res.exit_code == ShapeExit.CONFIG_ERROR
    assert "--describe" in res.stderr


def test_a_missing_agent_cmd_exits_24(runner):
    res = runner.invoke(app, ["shape", "acp"])
    assert res.exit_code == ShapeExit.CONFIG_ERROR
    assert "--agent-cmd" in res.stderr


def test_an_empty_agent_cmd_is_a_config_error(runner):
    res = runner.invoke(app, ["shape", "acp", "--agent-cmd", "   "])
    assert res.exit_code == ShapeExit.CONFIG_ERROR
    assert "cannot parse --agent-cmd" in res.stderr


# --- coverage: bridge.main run in-process, against a case built under tmp_path ----------
#
# The tests above run the bridge only inside `tp run` subprocesses (a real `tp shape acp`
# or `python -m trap.shapes.acp` child), which pytest-cov cannot see. These call main()
# directly, with TRAP_MANIFEST set via monkeypatch to a manifest for a case built under
# tmp_path — the pattern tests/test_shapes_case.py uses for CaseSandbox.


def _case_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    case = tmp_path / "task" / "inputs" / "c1"
    for name, text in files.items():
        (case / name).parent.mkdir(parents=True, exist_ok=True)
        (case / name).write_text(text)
    case.mkdir(parents=True, exist_ok=True)
    return case


def _set_manifest(monkeypatch: pytest.MonkeyPatch, case: Path) -> None:
    monkeypatch.setenv("TRAP_MANIFEST", json.dumps({"inputs_dir": str(case), "outputs_dir": "/nowhere"}))


def test_a_non_executable_agent_is_a_config_error(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    agent_path = tmp_path / "not-executable"
    agent_path.write_text("#!/bin/sh\necho hi\n")
    agent_path.chmod(0o644)
    code = bridge.main(["--agent-cmd", str(agent_path), "--model", "haiku", "--deadline", "5"])
    assert code == ShapeExit.CONFIG_ERROR
    assert "cannot start the agent" in capsys.readouterr().err


def test_describe_with_a_broken_agent_command_is_a_config_error(tmp_path, capsys):
    agent_path = tmp_path / "not-executable"
    agent_path.write_text("#!/bin/sh\n")
    agent_path.chmod(0o644)
    code = bridge.main(["--agent-cmd", str(agent_path), "--describe"])
    assert code == ShapeExit.CONFIG_ERROR
    assert "cannot start the agent" in capsys.readouterr().err


def test_describe_reports_a_broken_handshake_as_agent_error(fake, capsys):
    fake("bad_session_new")
    code = bridge.main(["--agent-cmd", shlex.join(AGENT), "--describe"])
    assert code == ShapeExit.AGENT_ERROR
    assert "could not open a session" in capsys.readouterr().err


def test_a_malformed_option_is_a_config_error(capsys):
    code = bridge.main(["--agent-cmd", shlex.join(AGENT), "--model", "haiku", "--option", "bad"])
    assert code == ShapeExit.CONFIG_ERROR
    assert "ID=VALUE" in capsys.readouterr().err


def test_bridge_prints_nothing_when_the_turn_is_not_an_answer(fake, tmp_path, monkeypatch, capsys):
    fake("no_model_use")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    code = bridge.main(["--agent-cmd", shlex.join(AGENT), "--model", "haiku", "--deadline", "20"])
    assert code == ShapeExit.AGENT_ERROR
    assert capsys.readouterr().out == ""


def test_bridge_gives_claude_acp_its_meta(fake, tmp_path, monkeypatch):
    log = fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    code = bridge.main(
        ["--agent-cmd", shlex.join(AGENT), "--agent-id", "claude-acp", "--model", "haiku", "--deadline", "20"]
    )
    assert code == ShapeExit.OK
    new = _sent(log, "session/new")[0]["params"]
    assert new["_meta"] == {"claudeCode": {"options": {"settingSources": ["project"]}}}


def test_bridge_omits_meta_for_an_agent_with_no_hints(fake, tmp_path, monkeypatch):
    log = fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    code = bridge.main(["--agent-cmd", shlex.join(AGENT), "--model", "haiku", "--deadline", "20"])
    assert code == ShapeExit.OK
    new = _sent(log, "session/new")[0]["params"]
    assert "_meta" not in new


def test_bridge_installs_a_skill_for_claude_acp(fake, tmp_path, monkeypatch):
    log = fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("hello skill")
    code = bridge.main(
        [
            "--agent-cmd",
            shlex.join(AGENT),
            "--agent-id",
            "claude-acp",
            "--model",
            "haiku",
            "--skill",
            str(skill),
            "--deadline",
            "20",
        ]
    )
    assert code == ShapeExit.OK
    # The case's sandbox (the agent's cwd) is gone by the time main() returns — sandbox.close()
    # runs before this assertion — so what the agent saw at startup is only in its own log.
    start = next(e for e in _log(log) if "cwd" in e)
    assert ".claude" in start["files"]


def test_bridge_refuses_a_skill_for_an_unsupported_agent(fake, tmp_path, monkeypatch, capsys):
    fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    skill = tmp_path / "my-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("hi")
    code = bridge.main(
        [
            "--agent-cmd",
            shlex.join(AGENT),
            "--agent-id",
            "codex-acp",
            "--model",
            "haiku",
            "--skill",
            str(skill),
            "--deadline",
            "20",
        ]
    )
    assert code == ShapeExit.CONFIG_ERROR
    assert "installing a skill is supported for" in capsys.readouterr().err


def test_bridge_sets_a_named_option(fake, tmp_path, monkeypatch):
    log = fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    code = bridge.main(
        ["--agent-cmd", shlex.join(AGENT), "--model", "haiku", "--option", "effort=high", "--deadline", "20"]
    )
    assert code == ShapeExit.OK
    sets = [(m["params"]["configId"], m["params"]["value"]) for m in _sent(log, "session/set_config_option")]
    assert ("effort", "high") in sets


def test_bridge_points_codex_at_the_proxy_under_an_api_key_login(fake, tmp_path, monkeypatch):
    log = fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "apikey"}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    code = bridge.main(
        ["--agent-cmd", shlex.join(AGENT), "--agent-id", "codex-acp", "--model", "haiku", "--deadline", "20"]
    )
    assert code == ShapeExit.OK
    start = next(e for e in _log(log) if "cwd" in e)
    assert "CODEX_CONFIG" in start["env"]


def test_bridge_does_not_point_codex_at_the_proxy_under_a_chatgpt_login(fake, tmp_path, monkeypatch):
    log = fake("ok")
    case = _case_dir(tmp_path, {"question.txt": "q"})
    _set_manifest(monkeypatch, case)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt"}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9")
    code = bridge.main(
        ["--agent-cmd", shlex.join(AGENT), "--agent-id", "codex-acp", "--model", "haiku", "--deadline", "20"]
    )
    assert code == ShapeExit.OK
    start = next(e for e in _log(log) if "cwd" in e)
    assert "CODEX_CONFIG" not in start["env"]


def test_describe_does_not_leak_the_manifest(fake, monkeypatch):
    log = fake("ok")
    monkeypatch.setenv("TRAP_MANIFEST", json.dumps({"inputs_dir": "/somewhere", "outputs_dir": "/nowhere"}))
    code = bridge.main(["--agent-cmd", shlex.join(AGENT), "--describe"])
    assert code == 0
    start = next(e for e in _log(log) if "cwd" in e)
    assert "TRAP_MANIFEST" not in start["env"]


def test_acp_main_module_imports_cleanly():
    import trap.shapes.acp.__main__  # noqa: F401

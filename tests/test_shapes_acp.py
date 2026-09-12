"""tp shape acp: one case through an agent that speaks ACP, against a scripted agent."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from trap.shapes.acp.connection import AGENT_EXITED, AcpConnection, AcpError

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


def test_writes_after_the_agent_exited_are_swallowed_not_raised(fake, tmp_path):
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


def test_close_tolerates_a_missing_stdin_and_a_stdin_that_wont_close(tmp_path):
    # Neither branch is reachable through a real AcpConnection (its Popen is always
    # created with stdin=PIPE), so exercise close() directly against a couple of
    # already-dead real processes standing in for a Popen with no/broken stdin.
    no_stdin = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
    no_stdin.wait()
    assert no_stdin.stdin is None
    conn = object.__new__(AcpConnection)
    conn._proc = no_stdin
    conn.close(grace=0.5)  # the "if stdin is not None" branch is False; nothing to close

    class _UnclosableStdin:
        def close(self) -> None:
            raise OSError("simulated: the pipe cannot be closed")

    broken_stdin = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
    broken_stdin.wait()
    broken_stdin.stdin = _UnclosableStdin()
    conn2 = object.__new__(AcpConnection)
    conn2._proc = broken_stdin
    conn2.close(grace=0.5)  # the OSError from closing stdin is swallowed


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

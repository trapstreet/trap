"""Every way out of a shape's main() ends in a ShapeExit code.

The guide's exit table is a promise: a shape ends 0/20/21/22/23/24/124, with a
``[trap]`` line on stderr when it fails — never an uncaught traceback, never a status
outside the table. Branch coverage cannot hold that line (a line that raises still counts
as covered), so each shape gets one test here that feeds garbage at every boundary it has
— its arguments, the case's files, the program, agent or vendor on the other side — and
checks only that the case ends in the table."""

from __future__ import annotations

import json
import shlex
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from trap.shapes import command, direct
from trap.shapes._case import ShapeExit
from trap.shapes.acp import bridge

from .conftest import PY

pytestmark = pytest.mark.usefixtures("signal_handlers_unchanged")

FAKE_AGENT = shlex.join([PY, str(Path(__file__).with_name("fake_acp_agent.py"))])
EXITS = {int(code) for code in ShapeExit}
NOT_UTF8 = "import sys; sys.stdout.buffer.write(bytes([111, 107, 32, 255, 254]))"


def _case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest: str | None = "case",
    question: bytes | None = b"what is 6*7?",
    extra: dict[str, bytes] | None = None,
    dangling: bool = False,
) -> None:
    """A case under tmp_path, and TRAP_MANIFEST pointing at it — or, for the manifest
    boundary, unset (None) or set to the literal text given."""
    case = tmp_path / "task" / "inputs" / "c1"
    case.mkdir(parents=True)
    if question is not None:
        (case / "question.txt").write_bytes(question)
    for name, data in (extra or {}).items():
        (case / name).write_bytes(data)
    if dangling:
        (case / "data.csv").symlink_to(tmp_path / "gone.csv")
    if manifest == "case":
        monkeypatch.setenv("TRAP_MANIFEST", json.dumps({"inputs_dir": str(case), "outputs_dir": "/nowhere"}))
    elif manifest is None:
        monkeypatch.delenv("TRAP_MANIFEST", raising=False)
    else:
        monkeypatch.setenv("TRAP_MANIFEST", manifest)


def _exit_status(main, argv: list[str]) -> int:
    """What the process would exit with: main's return value, or the code argparse
    exits with on bad arguments."""
    try:
        return main(argv)
    except SystemExit as e:
        return int(e.code)  # type: ignore[arg-type]


def _assert_in_table(code: int, err: str) -> None:
    assert code in EXITS, f"exit {code} is not a ShapeExit code"
    if code != ShapeExit.OK:
        assert any(line.startswith("[trap]") for line in err.splitlines()), err
    assert "Traceback" not in err


# Files a case can hold that a shape cannot take: the manifest, the question, an input.
CASE_GARBAGE = [
    pytest.param({"manifest": None}, id="manifest-unset"),
    pytest.param({"manifest": "not json"}, id="manifest-not-json"),
    pytest.param({"manifest": "[1, 2]"}, id="manifest-a-list"),
    pytest.param({"manifest": '{"inputs_dir": 5}'}, id="manifest-inputs-dir-a-number"),
    pytest.param({"question": None}, id="question-missing"),
    pytest.param({"question": b"caf\xe9?"}, id="question-not-utf8"),
    pytest.param({"dangling": True}, id="input-a-dangling-symlink"),
]


# --- tp shape cmd: its arguments, the case's files, the program -----------------------


@pytest.mark.parametrize(
    ("case", "argv"),
    [
        pytest.param({}, ["--deadline", "soon"], id="args-bad-deadline"),
        pytest.param({}, [], id="args-no-template"),
        pytest.param({}, ["--template", "tool", "--no-such-flag"], id="args-unknown-flag"),
        pytest.param({}, ["--template", "tool 'unclosed"], id="template-unparseable"),
        pytest.param({}, ["--template", "   "], id="template-empty"),
        pytest.param({}, ["--template", "{repo}/tool {prompt}"], id="template-repo-without-repo"),
        pytest.param({}, ["--template", "no-such-program-xyz {prompt}"], id="program-missing"),
        pytest.param({}, ["--template", "{tmp}/not-executable {prompt}"], id="program-not-executable"),
        pytest.param({}, ["--template", f"{PY} -c '{NOT_UTF8}'"], id="program-prints-invalid-utf8"),
        pytest.param({}, ["--template", "cat", "--prompt-file", "/etc/passwd"], id="prompt-file-absolute"),
        pytest.param({}, ["--template", "cat", "--prompt-file", "../x"], id="prompt-file-dotdot"),
        *(pytest.param(p.values[0], ["--template", "cat"], id=p.id) for p in CASE_GARBAGE),
    ],
)
def test_cmd_ends_in_a_shape_exit_code_whatever_it_is_fed(tmp_path, monkeypatch, capsys, case, argv):
    _case(tmp_path, monkeypatch, **case)
    program = tmp_path / "not-executable"
    program.write_text("#!/bin/sh\necho hi\n")
    program.chmod(0o644)
    argv = [a.replace("{tmp}", str(tmp_path)) for a in argv]
    code = _exit_status(command.main, [*argv, "--deadline", "20"] if "--deadline" not in argv else argv)
    _assert_in_table(code, capsys.readouterr().err)


# --- tp shape acp: its arguments, the case's files, the agent and what it replies -----

AGENT_MODES = [
    "ok",
    "garbage",
    "stray",
    "crash",
    "auth_error",
    "no_model_use",
    "null_chunk",
    "unknown_stop",
    "bad_session_new",
    "error_string",
    "list_result",
    "dict_config",
    "junk_config",
    "surrogate",
]


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
@pytest.mark.parametrize(
    ("case", "mode", "argv"),
    [
        pytest.param({}, "ok", [], id="args-no-agent-cmd"),
        pytest.param({}, "ok", ["--agent-cmd", FAKE_AGENT, "--deadline", "soon"], id="args-bad-deadline"),
        pytest.param({}, "ok", ["--agent-cmd", "   ", "--model", "haiku"], id="args-empty-agent-cmd"),
        pytest.param({}, "ok", ["--agent-cmd", "agent 'unclosed", "--model", "haiku"], id="args-unparseable"),
        pytest.param({}, "ok", ["--agent-cmd", FAKE_AGENT], id="args-no-model"),
        pytest.param(
            {}, "ok", ["--agent-cmd", FAKE_AGENT, "--model", "haiku", "--option", "x"], id="args-option"
        ),
        pytest.param({}, "ok", ["--agent-cmd", FAKE_AGENT, "--model", "opus"], id="model-not-offered"),
        pytest.param(
            {},
            "ok",
            ["--agent-cmd", FAKE_AGENT, "--model", "haiku", "--option", "speed=1"],
            id="option-unknown",
        ),
        pytest.param(
            {},
            "ok",
            ["--agent-cmd", FAKE_AGENT, "--agent-id", "claude-acp", "--model", "haiku", "--skill", "{tmp}"],
            id="skill-without-skill-md",
        ),
        pytest.param(
            {},
            "ok",
            [
                "--agent-cmd",
                FAKE_AGENT,
                "--agent-id",
                "claude-acp",
                "--model",
                "haiku",
                "--skill",
                "{tmp}/no-such-skill",
            ],
            id="skill-missing-dir",
        ),
        pytest.param(
            {},
            "ok",
            ["--agent-cmd", FAKE_AGENT, "--model", "haiku", "--prompt-file", "/etc/passwd"],
            id="prompt-file-absolute",
        ),
        pytest.param(
            {},
            "ok",
            ["--agent-cmd", FAKE_AGENT, "--model", "haiku", "--prompt-file", "../x"],
            id="prompt-file-dotdot",
        ),
        pytest.param({}, "ok", ["--agent-cmd", "no-such-agent-xyz", "--model", "haiku"], id="agent-missing"),
        pytest.param(
            {}, "ok", ["--agent-cmd", "{tmp}/not-executable", "--model", "haiku"], id="agent-not-exec"
        ),
        pytest.param(
            {}, "ok", ["--agent-cmd", "sh -c 'exit 0'", "--model", "haiku"], id="agent-exits-at-once"
        ),
        pytest.param(
            {},
            "ok",
            ["--agent-cmd", "sh -c 'printf \"\\377\\376\\n\"; exit 5'", "--model", "haiku"],
            id="agent-prints-bytes-and-exits",
        ),
        *(
            pytest.param({}, mode, ["--agent-cmd", FAKE_AGENT, "--model", "haiku"], id=mode)
            for mode in AGENT_MODES
        ),
        *(
            pytest.param({}, mode, ["--agent-cmd", FAKE_AGENT, "--describe"], id=f"describe-{mode}")
            for mode in ("crash", "bad_session_new", "error_string", "dict_config", "junk_config")
        ),
        *(
            pytest.param(p.values[0], "ok", ["--agent-cmd", FAKE_AGENT, "--model", "haiku"], id=p.id)
            for p in CASE_GARBAGE
        ),
    ],
)
def test_acp_ends_in_a_shape_exit_code_whatever_it_is_fed(tmp_path, monkeypatch, capsys, case, mode, argv):
    _case(tmp_path, monkeypatch, **case)
    monkeypatch.setenv("FAKE_ACP_MODE", mode)
    monkeypatch.delenv("FAKE_ACP_LOG", raising=False)
    agent = tmp_path / "not-executable"
    agent.write_text("#!/bin/sh\n")
    agent.chmod(0o644)
    argv = [a.replace("{tmp}", str(tmp_path)) for a in argv]
    code = _exit_status(bridge.main, [*argv, "--deadline", "20"] if "--deadline" not in argv else argv)
    _assert_in_table(code, capsys.readouterr().err)


# --- tp shape direct: its arguments, the case's files, the key, the URL, the vendor ----


def _vendor(body: bytes, status: int = 200) -> tuple[HTTPServer, str]:
    class Vendor(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), Vendor)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _reply(data: object, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(data).encode()


OPENAI = ["--model", "gpt-test"]
ANTHROPIC = ["--model", "claude-test"]


@pytest.mark.parametrize(
    ("case", "env", "argv", "reply"),
    [
        pytest.param({}, {}, [], None, id="args-no-model"),
        pytest.param({}, {}, [*OPENAI, "--provider", "nope"], None, id="args-unknown-provider"),
        pytest.param({}, {}, ["--model", "llama-4"], None, id="args-provider-unknowable"),
        pytest.param({}, {}, [*OPENAI, "--deadline", "soon"], None, id="args-bad-deadline"),
        pytest.param({}, {}, [*OPENAI, "--system-file", "{tmp}/none.md"], None, id="system-file-missing"),
        pytest.param({}, {}, [*OPENAI, "--system-file", "{tmp}/bad.md"], None, id="system-file-not-utf8"),
        pytest.param({"extra": {"ledger.txt": b"1"}}, {}, OPENAI, None, id="input-besides-the-question"),
        pytest.param({}, {"OPENAI_API_KEY": None}, OPENAI, None, id="key-unset"),
        pytest.param({}, {"OPENAI_API_KEY": "sk-café"}, OPENAI, None, id="key-not-ascii"),
        pytest.param({}, {"OPENAI_API_KEY": "sk-k\n"}, OPENAI, _reply({}), id="key-with-a-newline"),
        pytest.param({}, {"OPENAI_BASE_URL": "http://127.0.0.1:abc"}, OPENAI, None, id="url-invalid"),
        pytest.param({}, {"OPENAI_BASE_URL": "localhost:8080"}, OPENAI, None, id="url-no-scheme"),
        pytest.param({}, {"OPENAI_BASE_URL": "http://127.0.0.1:1"}, OPENAI, None, id="url-nothing-listens"),
        pytest.param({}, {}, OPENAI, (200, b"not json"), id="reply-not-json"),
        pytest.param({}, {}, OPENAI, (200, b"[1, 2]"), id="reply-a-list"),
        pytest.param({}, {}, OPENAI, _reply({}), id="reply-empty-object"),
        pytest.param({}, {}, OPENAI, _reply({"error": "boom"}), id="reply-error-status-200"),
        pytest.param({}, {}, OPENAI, _reply({"choices": {"0": {}}}), id="reply-choices-an-object"),
        pytest.param({}, {}, OPENAI, _reply({"choices": ["x"]}), id="reply-choices-strings"),
        pytest.param({}, {}, OPENAI, _reply({"choices": []}), id="reply-choices-empty"),
        pytest.param(
            {},
            {},
            OPENAI,
            _reply({"choices": [{"message": "hi", "finish_reason": "stop"}]}),
            id="reply-message-a-str",
        ),
        pytest.param(
            {},
            {},
            OPENAI,
            _reply({"choices": [{"message": {"content": "hi"}, "finish_reason": ["stop"]}]}),
            id="reply-finish-reason-a-list",
        ),
        pytest.param(
            {}, {}, ANTHROPIC, _reply({"content": 5, "stop_reason": "end_turn"}), id="reply-content-a-number"
        ),
        pytest.param(
            {},
            {},
            ANTHROPIC,
            _reply({"content": [{"type": "text", "text": None}], "stop_reason": "end_turn"}),
            id="reply-text-null",
        ),
        pytest.param(
            {},
            {},
            ANTHROPIC,
            _reply({"content": [{"type": "text", "text": "hi"}], "stop_reason": {"why": "x"}}),
            id="reply-stop-reason-an-object",
        ),
        pytest.param({}, {}, OPENAI, _reply({"error": {"message": "overloaded"}}, 500), id="reply-http-500"),
        pytest.param({}, {}, [*OPENAI, "--prompt-file", "/etc/passwd"], None, id="prompt-file-absolute"),
        pytest.param({}, {}, [*OPENAI, "--prompt-file", "../x"], None, id="prompt-file-dotdot"),
        pytest.param(
            {},
            {},
            OPENAI,
            _reply({"choices": [{"message": {"content": "ok \ud800 done"}, "finish_reason": "stop"}]}),
            id="reply-a-lone-surrogate",
        ),
        *(pytest.param(p.values[0], {}, OPENAI, None, id=p.id) for p in CASE_GARBAGE),
    ],
)
def test_direct_ends_in_a_shape_exit_code_whatever_it_is_fed(
    tmp_path, monkeypatch, capsys, case, env, argv, reply
):
    _case(tmp_path, monkeypatch, **case)
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe")
    status, body = reply or _reply({})
    srv, url = _vendor(body, status)
    try:
        settings = {
            "OPENAI_API_KEY": "k",
            "OPENAI_BASE_URL": url,
            "ANTHROPIC_API_KEY": "k",
            "ANTHROPIC_BASE_URL": url,
            **env,
        }
        for name, value in settings.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        argv = [a.replace("{tmp}", str(tmp_path)) for a in argv]
        code = _exit_status(direct.main, [*argv, "--deadline", "20"] if "--deadline" not in argv else argv)
    finally:
        srv.shutdown()
    _assert_in_table(code, capsys.readouterr().err)

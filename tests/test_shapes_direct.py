"""tp shape direct: the question, once, to a model API — against loopback vendors."""

from __future__ import annotations

import json
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from trap.cli import app
from trap.shapes import direct
from trap.shapes._case import ShapeError, ShapeExit

from .conftest import JUDGE_SCORE, PY, case_capture

ANTHROPIC_OK = {
    "model": "claude-test-1",
    "content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 12, "output_tokens": 3},
}
OPENAI_OK = {
    "model": "gpt-test",
    "choices": [{"message": {"role": "assistant", "content": "hi there"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 9, "completion_tokens": 2},
}


def _vendor(reply: dict, status: int = 200, *, delay: float = 0.0):
    """A loopback vendor answering every POST with ``reply``; ``srv.requests`` keeps each
    request's (path, body)."""
    requests: list[tuple[str, dict]] = []
    payload = json.dumps(reply).encode()

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", 0))
            requests.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
            if delay:
                time.sleep(delay)
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    srv.requests = requests  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _raw_vendor(body: bytes, status: int = 200, content_type: str = "application/json"):
    """A loopback vendor answering every POST with a raw (possibly non-JSON, possibly
    non-object) body — for the response shapes a real vendor should never send but the
    call path must still turn into a clean config/agent error instead of a crash."""

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _case(tmp_path, monkeypatch, files: dict[str, str]) -> None:
    case = tmp_path / "task" / "inputs" / "c1"
    case.mkdir(parents=True)
    for name, text in files.items():
        (case / name).write_text(text)
    monkeypatch.setenv(
        "TRAP_MANIFEST", json.dumps({"inputs_dir": str(case), "outputs_dir": str(tmp_path / "out")})
    )


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("claude-sonnet-5", "anthropic"),
        ("gpt-5.5", "openai"),
        ("o3-mini", "openai"),
        ("deepseek-v4-pro", "deepseek"),
        ("kimi-k3", "moonshot"),
        ("mistral-large-3", "mistral"),
        ("anthropic/claude-sonnet-5", "openrouter"),
        ("llama-4", None),
    ],
)
def test_the_provider_is_read_off_the_model_id(model, provider):
    assert direct.infer_provider(model) == provider


def test_an_anthropic_request_carries_the_question_and_the_skill(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://gw")
    url, headers, body = direct.build_request("anthropic", "claude-x", "Q?", "SKILL")
    assert url == "http://gw/v1/messages"
    assert (headers["x-api-key"], headers["anthropic-version"]) == ("k", direct.ANTHROPIC_VERSION)
    assert body == {
        "model": "claude-x",
        "max_tokens": direct.ANTHROPIC_MAX_TOKENS,
        "system": "SKILL",
        "messages": [{"role": "user", "content": "Q?"}],
    }


def test_an_openai_compatible_request_has_no_output_cap(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.delenv("MISTRAL_BASE_URL", raising=False)
    url, headers, body = direct.build_request("mistral", "mistral-large-3", "Q?", None)
    assert url == "https://api.mistral.ai/v1/chat/completions"
    assert headers["authorization"] == "Bearer k"
    assert body == {"model": "mistral-large-3", "messages": [{"role": "user", "content": "Q?"}]}


def test_an_openai_compatible_request_carries_a_system_prompt_too(monkeypatch):
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    monkeypatch.delenv("MISTRAL_BASE_URL", raising=False)
    _, _, body = direct.build_request("mistral", "mistral-large-3", "Q?", "Be terse.")
    assert body["messages"] == [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "Q?"}]


def test_a_missing_key_is_a_config_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ShapeError) as e:
        direct.build_request("openai", "gpt-5.5", "Q?", None)
    assert e.value.code is ShapeExit.CONFIG_ERROR and "OPENAI_API_KEY" in str(e.value)


def test_replies_are_read_in_each_format():
    assert direct.parse_reply("anthropic", ANTHROPIC_OK) == direct.Reply(
        "hello", "end_turn", {"input_tokens": 12, "output_tokens": 3}
    )
    assert direct.parse_reply("openai", OPENAI_OK).text == "hi there"


@pytest.mark.parametrize(
    ("reply", "code"),
    [
        (direct.Reply("x", "end_turn", {}), ShapeExit.OK),
        (direct.Reply("x", "stop", {}), ShapeExit.OK),
        (direct.Reply("", "end_turn", {}), ShapeExit.AGENT_ERROR),
        (direct.Reply("", "max_tokens", {}), ShapeExit.MAX_TOKENS),
        (direct.Reply("part", "length", {}), ShapeExit.MAX_TOKENS),
        (direct.Reply("no", "refusal", {}), ShapeExit.REFUSAL),
        (direct.Reply("x", "tool_use", {}), ShapeExit.AGENT_ERROR),
    ],
)
def test_how_the_reply_stopped_decides_the_exit(reply, code):
    assert direct.exit_for(reply)[0] is code


def test_an_openai_style_answer_is_printed(tmp_path, monkeypatch, capsys):
    srv, url = _vendor(OPENAI_OK)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        assert direct.main(["--model", "gpt-test"]) == 0
    finally:
        srv.shutdown()
    assert capsys.readouterr().out == "hi there\n"
    assert srv.requests[0][0] == "/chat/completions"


def test_other_input_files_are_refused_before_any_call(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?", "ledger.txt": "1"})
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert direct.main(["--model", "gpt-test"]) == ShapeExit.CONFIG_ERROR
    err = capsys.readouterr().err
    assert "ledger.txt" in err and "tp shape acp" in err


def test_os_junk_beside_the_question_does_not_refuse_the_case(tmp_path, monkeypatch, capsys):
    srv, url = _vendor(OPENAI_OK)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?", ".DS_Store": "finder", "Thumbs.db": "x"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        assert direct.main(["--model", "gpt-test"]) == 0
    finally:
        srv.shutdown()
    assert capsys.readouterr().out == "hi there\n"


def test_the_refusal_names_the_real_extra_files_not_the_junk(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?", "ledger.txt": "1", "desktop.ini": "x"})
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert direct.main(["--model", "gpt-test"]) == ShapeExit.CONFIG_ERROR
    err = capsys.readouterr().err
    assert "(ledger.txt)" in err and "desktop.ini" not in err


def test_an_http_error_is_not_an_answer(tmp_path, monkeypatch, capsys):
    srv, url = _vendor({"error": {"message": "invalid x-api-key"}}, status=401)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        assert direct.main(["--model", "claude-x"]) == ShapeExit.AGENT_ERROR
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert captured.out == "" and "HTTP 401" in captured.err


def test_tp_shape_direct_needs_a_provider_it_can_tell(runner):
    res = runner.invoke(app, ["shape", "direct", "--model", "llama-4"])
    assert res.exit_code == ShapeExit.CONFIG_ERROR
    assert "--provider" in res.stderr


def test_a_direct_run_is_metered_by_the_cost_proxy(make_project, runner, tmp_path, monkeypatch):
    srv, url = _vendor(ANTHROPIC_OK)
    try:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)  # the proxy forwards here
        skill = tmp_path / "SKILL.md"
        skill.write_text("Be terse.")
        cmd = [
            PY,
            "-m",
            "trap.shapes.direct",
            "--model",
            "claude-test-1",
            "--system-file",
            str(skill),
            "--deadline",
            "30",
        ]
        sol = make_project(
            cmd=shlex.join(cmd),
            inputs={"c1": {"question.txt": "Say hello."}},
            expected={"c1": {"answer.txt": "hello"}},
            judge_src=JUDGE_SCORE,
        )
        res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    finally:
        srv.shutdown()
    assert res.exit_code == 0, res.output
    out, meta = case_capture(sol)
    assert (out.strip(), meta["exit_code"]) == ("hello", 0)
    path, body = srv.requests[0]
    assert path == "/v1/messages"
    assert body["system"] == "Be terse." and body["messages"] == [{"role": "user", "content": "Say hello."}]
    report = json.loads(next((sol / ".trap").rglob("report.json")).read_text())
    cost = report["cases_results"][0]["cost"]["by_model"][0]
    assert (cost["provider"], cost["prompt_tokens"], cost["completion_tokens"]) == ("anthropic", 12, 3)


# Bad arguments are a config error (exit 24), the same as every other shape's CLI —
# never argparse's own exit 2, and never a raw traceback.


def test_a_bad_provider_is_a_config_error(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
    with pytest.raises(SystemExit) as e:
        direct.main(["--model", "claude-x", "--provider", "nonexistent-vendor"])
    assert e.value.code == ShapeExit.CONFIG_ERROR
    assert "--provider" in capsys.readouterr().err


def test_a_missing_model_is_a_config_error(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
    with pytest.raises(SystemExit) as e:
        direct.main([])
    assert e.value.code == ShapeExit.CONFIG_ERROR
    assert "--model" in capsys.readouterr().err


# The tests above run direct.main only inside a `tp run` subprocess (the metered test) or
# fully in-process; the ones below round out branch coverage for paths the tests above
# don't reach: a case trap can't even open, a system file that can't be read, the
# request's lower-level failure modes, and how a non-plain-answer reply is (or isn't)
# printed.


def test_running_outside_tp_run_is_a_config_error(monkeypatch, capsys):
    monkeypatch.delenv("TRAP_MANIFEST", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert direct.main(["--model", "gpt-test"]) == ShapeExit.CONFIG_ERROR
    assert "TRAP_MANIFEST" in capsys.readouterr().err


def test_an_unreadable_system_file_is_a_config_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    missing = tmp_path / "no-such-skill.md"
    assert direct.main(["--model", "gpt-test", "--system-file", str(missing)]) == ShapeExit.CONFIG_ERROR
    err = capsys.readouterr().err
    assert err.startswith("[trap]") and "no-such-skill.md" in err


def test_a_system_file_reaches_an_openai_compatible_request_in_process(tmp_path, monkeypatch, capsys):
    srv, url = _vendor(OPENAI_OK)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        skill = tmp_path / "SKILL.md"
        skill.write_text("Be terse.")
        assert direct.main(["--model", "gpt-test", "--system-file", str(skill)]) == 0
    finally:
        srv.shutdown()
    assert capsys.readouterr().out == "hi there\n"
    _, body = srv.requests[0]
    assert body["messages"][0] == {"role": "system", "content": "Be terse."}


def test_a_request_that_times_out_reports_the_deadline(tmp_path, monkeypatch, capsys):
    srv, url = _vendor(OPENAI_OK, delay=1.5)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        code = direct.main(["--model", "gpt-test", "--deadline", "0.01"])
    finally:
        srv.shutdown()
    assert code == ShapeExit.TIMEOUT
    assert "deadline" in capsys.readouterr().err


def test_a_connection_failure_is_an_agent_error(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    # Nothing listens on this loopback port: the request fails before any HTTP status
    # comes back, unlike test_an_http_error_is_not_an_answer's 401.
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1")
    assert direct.main(["--model", "gpt-test"]) == ShapeExit.AGENT_ERROR
    assert "the request failed" in capsys.readouterr().err


def test_a_non_json_reply_is_an_agent_error(tmp_path, monkeypatch, capsys):
    srv, url = _raw_vendor(b"not json at all")
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        code = direct.main(["--model", "gpt-test"])
    finally:
        srv.shutdown()
    assert code == ShapeExit.AGENT_ERROR
    assert "was not JSON" in capsys.readouterr().err


def test_a_json_array_reply_is_an_agent_error(tmp_path, monkeypatch, capsys):
    srv, url = _raw_vendor(b"[1, 2, 3]")
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        code = direct.main(["--model", "gpt-test"])
    finally:
        srv.shutdown()
    assert code == ShapeExit.AGENT_ERROR
    assert "was not a JSON object" in capsys.readouterr().err


def test_a_reply_with_no_visible_text_prints_nothing_to_stdout(tmp_path, monkeypatch, capsys):
    empty = {**ANTHROPIC_OK, "content": [{"type": "thinking", "thinking": "only thoughts"}]}
    srv, url = _vendor(empty)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        code = direct.main(["--model", "claude-x"])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == ShapeExit.AGENT_ERROR
    assert captured.out == ""
    assert "no visible text" in captured.err


def test_an_unexpected_stop_reason_is_an_agent_error_not_an_answer(tmp_path, monkeypatch, capsys):
    weird = {**ANTHROPIC_OK, "stop_reason": "tool_use"}
    srv, url = _vendor(weird)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        code = direct.main(["--model", "claude-x"])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == ShapeExit.AGENT_ERROR
    assert captured.out == ""
    assert "unexpected stop reason" in captured.err


def test_a_refusal_is_still_printed_as_the_answer(tmp_path, monkeypatch, capsys):
    refusal = {
        "model": "gpt-test",
        "choices": [{"message": {"content": "I can't help with that."}, "finish_reason": "content_filter"}],
        "usage": {},
    }
    srv, url = _vendor(refusal)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        code = direct.main(["--model", "gpt-test"])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == ShapeExit.REFUSAL
    assert captured.out == "I can't help with that.\n"
    assert "the model refused" in captured.err


def test_a_max_tokens_cutoff_with_no_text_prints_nothing(tmp_path, monkeypatch, capsys):
    cutoff = {**ANTHROPIC_OK, "content": [], "stop_reason": "max_tokens"}
    srv, url = _vendor(cutoff)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", url)
        code = direct.main(["--model", "claude-x"])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == ShapeExit.MAX_TOKENS
    assert captured.out == ""
    assert "output ceiling" in captured.err


# A --system-file that reads() but isn't valid text, an OpenAI-compatible reply whose
# message.content isn't a plain string, and a reply shaped so unlike a real one that
# parsing itself blows up: none of these may reach the caller as a bare traceback, and a
# vendor's own refusal field must count as a refusal even when nothing else says so.


def test_a_non_utf8_system_file_is_a_config_error_not_a_traceback(tmp_path, monkeypatch, capsys):
    _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    bad = tmp_path / "not-utf8.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8")
    assert direct.main(["--model", "gpt-test", "--system-file", str(bad)]) == ShapeExit.CONFIG_ERROR
    err = capsys.readouterr().err
    assert err.startswith("[trap]") and "not-utf8.md" in err


@pytest.mark.parametrize(
    ("content", "text"),
    [
        ("plain string", "plain string"),
        ([{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}], "answer"),
        (["raw chunk", {"type": "thinking", "thinking": "x"}], ""),
        (None, ""),
    ],
)
def test_openai_style_content_is_always_read_as_a_plain_string(content, text):
    # Mistral's reasoning models (magistral-, routed to "mistral" by infer_provider) send
    # message.content as a list of thinking/text parts rather than a plain string.
    data = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}
    assert direct.parse_reply("mistral", data).text == text


def test_an_openai_refusal_field_is_read_as_a_refusal():
    data = {
        "choices": [
            {"message": {"content": None, "refusal": "I can't help with that."}, "finish_reason": "stop"}
        ],
        "usage": {},
    }
    assert direct.parse_reply("openai", data) == direct.Reply("I can't help with that.", "refusal", {})


def test_an_openai_refusal_field_is_printed_and_exits_20(tmp_path, monkeypatch, capsys):
    refusal = {
        "choices": [
            {"message": {"content": None, "refusal": "I can't help with that."}, "finish_reason": "stop"}
        ],
        "usage": {},
    }
    srv, url = _vendor(refusal)
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        code = direct.main(["--model", "gpt-test"])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == ShapeExit.REFUSAL
    assert captured.out == "I can't help with that.\n"
    assert "the model refused" in captured.err


def test_a_reply_shaped_nothing_like_the_api_is_an_agent_error(tmp_path, monkeypatch, capsys):
    # A 200 body whose "choices" entries are plain strings, not message objects — not
    # anything a real vendor sends, but parse_reply must not let it become a traceback.
    srv, url = _vendor({"choices": ["x"], "usage": {}})
    try:
        _case(tmp_path, monkeypatch, {"question.txt": "Q?"})
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        code = direct.main(["--model", "gpt-test"])
    finally:
        srv.shutdown()
    captured = capsys.readouterr()
    assert code == ShapeExit.AGENT_ERROR
    assert captured.out == ""
    assert "the reply was not in the expected format" in captured.err

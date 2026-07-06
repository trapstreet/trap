from __future__ import annotations

import json

from trap.runner.capture import Capture
from trap.runner.layout import CaseLayout
from trap.runner.proc import CapturedSubprocess

from .conftest import PY


def _proc(cmd, cwd, capture_dir, *, timeout=30):
    return CapturedSubprocess(
        cmd,
        manifest_envvar="M",
        timeout=timeout,
        cwd=cwd,
        manifest="{}",
        capture=Capture.from_dir(capture_dir),
    )


def test_run_captures_streams_and_meta(tmp_path):
    r = _proc("sh -c 'echo out; echo err >&2'", tmp_path, tmp_path / "cap").run()
    assert r.exit_code == 0
    assert "out" in r.stdout and "err" in r.stderr
    assert (tmp_path / "cap" / "stdout").read_text().strip() == "out"
    meta = json.loads((tmp_path / "cap" / "meta.json").read_text())
    assert meta["exit_code"] == 0 and meta["duration"] >= 0


def test_run_timeout(tmp_path):
    r = _proc("sh -c 'sleep 5'", tmp_path, tmp_path / "cap", timeout=1).run()
    assert r.exit_code == 124
    assert "timed out" in r.stderr


def _json_emitter(tmp_path, payload: str) -> str:
    (tmp_path / "emit.py").write_text(f"print({payload!r})")
    return f"{PY} emit.py"


def test_run_metrics_ok(tmp_path):
    cmd = _json_emitter(tmp_path, '{"score": 1.0}')
    out = _proc(cmd, tmp_path, tmp_path / "cap").run_metrics_or_error(runner_name="judge")
    assert out == {"score": 1.0}


def test_run_metrics_nonzero(tmp_path):
    out = _proc("sh -c 'exit 2'", tmp_path, tmp_path / "cap").run_metrics_or_error(runner_name="judge")
    assert "judge exited with status 2" in out["error"]


def test_run_metrics_bad_json(tmp_path):
    out = _proc("sh -c 'echo notjson'", tmp_path, tmp_path / "cap").run_metrics_or_error(runner_name="grader")
    assert "grader produced invalid JSON" in out["error"]


def test_run_metrics_timeout(tmp_path):
    out = _proc("sh -c 'sleep 5'", tmp_path, tmp_path / "cap", timeout=1).run_metrics_or_error(
        runner_name="judge"
    )
    assert "judge timed out" in out["error"]


def test_as_text_normalises():
    f = CapturedSubprocess._as_text
    assert f("s") == "s"
    assert f(b"b") == "b"
    assert f(bytearray(b"x")) == "x"
    assert f(memoryview(b"m")) == "m"
    assert f(None) == ""


def test_capture_and_layout(tmp_path):
    layout = CaseLayout.for_case(tmp_path, "c1")
    assert layout.outputs_dir == tmp_path / "c1" / "solution" / "outputs"
    cap = layout.judge_capture
    cap.write("o", "e", {"exit_code": 0})
    assert cap.stdout.read_text() == "o" and cap.stderr.read_text() == "e"
    assert json.loads(cap.meta.read_text()) == {"exit_code": 0}

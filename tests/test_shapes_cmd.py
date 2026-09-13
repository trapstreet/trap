"""tp shape cmd: a command template around someone else's program."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from trap.cli import app
from trap.shapes._case import ShapeError, ShapeExit
from trap.shapes.command import expand, main

from .conftest import (
    INTERRUPTS,
    JUDGE_SCORE,
    PY,
    case_capture,
    process_gone,
    reap,
    signal_main_thread_when,
    sleeper,
    unlock,
    wait_until,
)

pytestmark = pytest.mark.usefixtures("signal_handlers_unchanged")

TOOL = """
import os, sys
mode = sys.argv[1]
if mode == "arg":
    print("ARG:" + sys.argv[2])
elif mode == "stdin":
    print("STDIN:" + sys.stdin.read())
elif mode == "file":
    print("FILE:" + open(sys.argv[2]).read())
elif mode == "where":
    print(os.getcwd())
    print(os.environ.get("TRAP_MANIFEST", "no-manifest"))
    print(os.environ.get("POINTER", "no-pointer"))
"""


def test_the_question_becomes_one_argument_however_it_is_quoted(tmp_path):
    q = 'it\'s "two" words {repo}'
    argv, stdin = expand("tool --q {prompt} x", question=q, prompt_path=tmp_path / "q.txt", repo=None)
    assert argv == ["tool", "--q", q, "x"]
    assert stdin is None


def test_repo_and_prompt_file_are_substituted(tmp_path):
    argv, stdin = expand(
        "python {repo}/main.py --in={prompt_file}",
        question="q",
        prompt_path=tmp_path / "q.txt",
        repo=tmp_path,
    )
    assert argv == ["python", f"{tmp_path}/main.py", f"--in={tmp_path / 'q.txt'}"]
    assert stdin is None


def test_without_a_prompt_placeholder_the_question_goes_to_stdin(tmp_path):
    assert expand("tool", question="q", prompt_path=tmp_path / "q.txt", repo=None) == (["tool"], "q")


@pytest.mark.parametrize(
    ("template", "message"),
    [("", "empty"), ("tool 'unclosed", "cannot parse"), ("{repo}/x", "no --repo")],
)
def test_a_bad_template_is_a_config_error(tmp_path, template, message):
    with pytest.raises(ShapeError) as e:
        expand(template, question="q", prompt_path=tmp_path / "q.txt", repo=None)
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert message in str(e.value)


def _project(make_project, tmp_path: Path, template: str, **kw) -> Path:
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir(exist_ok=True)
    (tool_dir / "tool.py").write_text(TOOL)
    cmd = shlex.join(
        [PY, "-m", "trap.shapes.command", "--repo", str(tool_dir), "--template", template, "--deadline", "30"]
    )
    return make_project(cmd=cmd, **kw)


def test_a_template_run_is_scored_like_any_solution(make_project, runner, tmp_path):
    sol = _project(
        make_project,
        tmp_path,
        f"{PY} {{repo}}/tool.py arg {{prompt}}",
        inputs={"c1": {"question.txt": "hello"}},
        expected={"c1": {"answer.txt": "ARG:hello"}},
        judge_src=JUDGE_SCORE,
    )
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    out, meta = case_capture(sol)
    assert (out.strip(), meta["exit_code"]) == ("ARG:hello", 0)
    report = json.loads(next((sol / ".trap").rglob("report.json")).read_text())
    assert report["cases_results"][0]["metrics"] == {"score": 1.0}


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        (f"{PY} {{repo}}/tool.py stdin", "STDIN:hello"),
        (f"{PY} {{repo}}/tool.py file {{prompt_file}}", "FILE:hello"),
    ],
)
def test_the_question_can_come_on_stdin_or_as_a_file(make_project, runner, tmp_path, template, expected):
    sol = _project(make_project, tmp_path, template, inputs={"c1": {"question.txt": "hello"}})
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert case_capture(sol)[0].strip() == expected


def test_the_program_runs_in_a_scratch_copy_without_the_manifest(make_project, runner, tmp_path, monkeypatch):
    monkeypatch.setenv("POINTER", str(tmp_path / "task" / "inputs" / "c1"))
    sol = _project(
        make_project, tmp_path, f"{PY} {{repo}}/tool.py where", inputs={"c1": {"question.txt": "q"}}
    )
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    cwd, manifest, pointer = case_capture(sol)[0].splitlines()
    assert not Path(cwd).is_relative_to(tmp_path.resolve())
    assert (manifest, pointer) == ("no-manifest", "no-pointer")


def test_a_missing_program_is_a_config_error(make_project, runner, tmp_path):
    sol = _project(
        make_project, tmp_path, "no-such-program-xyz {prompt}", inputs={"c1": {"question.txt": "q"}}
    )
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output  # a failing solution is a fact about the case, not a trap error
    _, meta = case_capture(sol)
    assert meta["exit_code"] == ShapeExit.CONFIG_ERROR


def test_a_case_with_a_symlinked_input_is_refused_before_the_program_runs(make_project, runner, tmp_path):
    sol = make_project(
        cmd=shlex.join(
            [PY, "-m", "trap.shapes.command", "--template", "cat reference.txt", "--deadline", "30"]
        ),
        inputs={"c1": {"question.txt": "hi"}},
        expected={"c1": {"answer.txt": "secret"}},
    )
    (tmp_path / "task" / "inputs" / "c1" / "reference.txt").symlink_to(
        tmp_path / "task" / "expected" / "c1" / "answer.txt"
    )
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output  # a refused case is a fact about it, not a trap error
    out, meta = case_capture(sol)
    assert meta["exit_code"] == ShapeExit.CONFIG_ERROR
    assert "secret" not in out
    stderr = next((sol / ".trap").rglob("c1/solution/stderr")).read_text()
    assert "secret" not in stderr


#: A program that answers, then leaves a directory in its work dir that no one can list.
ANSWERS_THEN_LOCKS_A_DIR = "sh -c 'echo the-answer; mkdir d && chmod 000 d'"


def test_a_program_that_locks_a_dir_in_its_work_dir_keeps_its_answer(
    make_project, runner, tmp_path, monkeypatch
):
    tmpdir = tmp_path / "tmpdir"  # the shape's own TMPDIR, so its work dir is findable
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    sol = make_project(
        cmd=shlex.join(
            [PY, "-m", "trap.shapes.command", "--template", ANSWERS_THEN_LOCKS_A_DIR, "--deadline", "30"]
        ),
        inputs={"c1": {"question.txt": "q"}},
    )
    try:
        res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
        assert res.exit_code == 0, res.output
        out, meta = case_capture(sol)
        assert (out, meta["exit_code"]) == ("the-answer\n", 0)
        assert list(tmpdir.glob("trap-case-*")) == [], "the work dir was left behind"
    finally:
        unlock(tmpdir)


def _venv_tp() -> bool:
    found = shutil.which("tp")
    return found is not None and Path(found).parent == Path(sys.executable).parent


@pytest.mark.skipif(not _venv_tp(), reason="needs this venv's tp first on PATH (uv run pytest)")
def test_the_documented_form_runs_through_tp_on_path(make_project, runner, tmp_path):
    tool_dir = tmp_path / "tool"
    tool_dir.mkdir()
    (tool_dir / "tool.py").write_text(TOOL)
    template = f"{PY} {{repo}}/tool.py arg {{prompt}}"
    cmd = shlex.join(
        ["tp", "shape", "cmd", "--repo", str(tool_dir), "--template", template, "--deadline", "30"]
    )
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "hi"}})
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert case_capture(sol)[0].strip() == "ARG:hi"


def test_tp_shape_cmd_outside_a_run_says_how_to_use_it(runner, monkeypatch):
    monkeypatch.delenv("TRAP_MANIFEST", raising=False)
    res = runner.invoke(app, ["shape", "cmd", "--template", "echo hi"])
    assert res.exit_code == ShapeExit.CONFIG_ERROR
    assert "TRAP_MANIFEST is not set" in res.stderr


def test_a_missing_template_exits_24(runner):
    res = runner.invoke(app, ["shape", "cmd"])
    assert res.exit_code == ShapeExit.CONFIG_ERROR
    assert "--template" in res.stderr


def test_help_reaches_argparse_not_click(runner):
    # _PASSTHROUGH's help_option_names=[] means click adds no --help of its own, so this
    # is argparse's usage (every shape option named) rather than click's generic one.
    res = runner.invoke(app, ["shape", "cmd", "--help"])
    assert res.exit_code == 0
    assert "--template TEMPLATE" in res.output
    assert "--scrub PATH" in res.output


# The tests above run command.main only inside a `tp run` subprocess (a real `tp shape
# cmd` or `python -m trap.shapes.command` child), which pytest-cov cannot see. These call
# main() in-process instead, with TRAP_MANIFEST set via monkeypatch to a manifest for a
# case built under tmp_path — the pattern tests/test_shapes_case.py uses for CaseSandbox.


def _case_dir(tmp_path: Path, files: dict[str, str]) -> Path:
    case = tmp_path / "task" / "inputs" / "c1"
    for name, text in files.items():
        (case / name).parent.mkdir(parents=True, exist_ok=True)
        (case / name).write_text(text)
    case.mkdir(parents=True, exist_ok=True)
    return case


def _set_manifest(monkeypatch: pytest.MonkeyPatch, case: Path) -> None:
    monkeypatch.setenv("TRAP_MANIFEST", json.dumps({"inputs_dir": str(case), "outputs_dir": "/nowhere"}))


def test_main_runs_a_template_with_repo_in_process(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tool.py").write_text(TOOL)
    code = main(
        ["--template", f"{PY} {{repo}}/tool.py arg {{prompt}}", "--repo", str(repo), "--deadline", "30"]
    )
    assert code == 0
    assert capsys.readouterr().out.strip() == "ARG:hi"


def test_main_runs_a_template_without_repo_in_process(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    tool = tmp_path / "tool.py"
    tool.write_text(TOOL)
    code = main(["--template", f"{PY} {tool} stdin", "--deadline", "30"])
    assert code == 0
    assert capsys.readouterr().out.strip() == "STDIN:hi"


def test_main_reports_a_missing_program_as_a_config_error_in_process(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    code = main(["--template", "no-such-program-xyz {prompt}", "--deadline", "30"])
    assert code == ShapeExit.CONFIG_ERROR
    assert "command not found: no-such-program-xyz" in capsys.readouterr().err


def test_main_reports_a_program_it_cannot_execute_as_a_config_error(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    program = tmp_path / "not-executable"
    program.write_text("#!/bin/sh\necho hi\n")
    program.chmod(0o644)
    code = main(["--template", f"{program} {{prompt}}", "--deadline", "30"])
    assert code == ShapeExit.CONFIG_ERROR
    err = capsys.readouterr().err
    assert err.startswith("[trap] cannot start") and "not-executable" in err


def test_a_program_killed_by_a_signal_exits_128_plus_the_signal(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    code = main(["--template", "sh -c 'echo partial; kill -KILL $$'", "--deadline", "30"])
    assert code == 128 + signal.SIGKILL
    assert capsys.readouterr().out == "partial\n"


def test_main_keeps_the_answer_of_a_program_that_locks_a_dir_in_its_work_dir(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    tmpdir = tmp_path / "tmpdir"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    try:
        code = main(["--template", ANSWERS_THEN_LOCKS_A_DIR, "--deadline", "30"])
        assert (code, capsys.readouterr().out) == (0, "the-answer\n")
        assert list(tmpdir.iterdir()) == [], "the work dir was left behind"
    finally:
        unlock(tmpdir)


def test_main_relays_output_that_is_not_utf8_instead_of_crashing(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    program = tmp_path / "bytes.py"
    program.write_text("import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe')")
    code = main(["--template", f"{PY} {program}", "--deadline", "30"])
    assert code == 0
    assert capsys.readouterr().out.startswith("ok ")


@pytest.mark.parametrize("sig", INTERRUPTS)
def test_an_interrupted_cmd_takes_its_program_and_its_work_dir_down(tmp_path, sig):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    pidfile, cwdfile = tmp_path / "pid", tmp_path / "cwd"
    program = shlex.join(["sh", "-c", f"pwd > {cwdfile}; " + sleeper(pidfile)[2]])
    env = {**os.environ, "TRAP_MANIFEST": json.dumps({"inputs_dir": str(case), "outputs_dir": "/nowhere"})}
    shape = subprocess.Popen(
        [PY, "-m", "trap.shapes.command", "--template", program, "--deadline", "30"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert wait_until(pidfile.exists), "the program never started"
        shape.send_signal(sig)
        _, err = shape.communicate(timeout=5)
        assert process_gone(int(pidfile.read_text()), timeout=1.0), "the program outlived the shape"
    finally:
        shape.kill()
        if pidfile.exists():
            reap(int(pidfile.read_text()))
    assert shape.returncode == 128 + sig
    assert not Path(cwdfile.read_text().strip()).exists(), "the work directory was left behind"
    assert "Traceback" not in err


def test_tp_shape_cmd_exits_130_on_ctrl_c(runner, tmp_path, monkeypatch, stray_signals):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    pidfile = tmp_path / "pid"
    signal_main_thread_when(pidfile.exists, signal.SIGINT)
    try:
        res = runner.invoke(
            app, ["shape", "cmd", "--template", shlex.join(sleeper(pidfile)), "--deadline", "30"]
        )
        assert res.exit_code == 128 + signal.SIGINT
        assert process_gone(int(pidfile.read_text()), timeout=1.0)
    finally:
        reap(int(pidfile.read_text()))


def test_main_reports_a_bad_template_as_a_config_error_in_process(tmp_path, monkeypatch, capsys):
    case = _case_dir(tmp_path, {"question.txt": "hi"})
    _set_manifest(monkeypatch, case)
    code = main(["--template", "", "--deadline", "30"])
    assert code == ShapeExit.CONFIG_ERROR
    assert "the command template is empty" in capsys.readouterr().err

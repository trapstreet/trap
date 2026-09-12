"""The plumbing every built-in shape shares: sandbox, env scrub, deadline, group kill."""

from __future__ import annotations

import argparse
import json
import locale
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from trap.shapes._case import (
    CaseSandbox,
    Deadline,
    ShapeError,
    ShapeExit,
    ShapeParser,
    add_case_args,
    fail,
    kill_group,
    open_case,
    run_group,
    scrubbed_env,
)

from .conftest import process_gone


def _case(tmp_path: Path, files: dict[str, str]) -> Path:
    case = tmp_path / "task" / "inputs" / "c1"
    for name, text in files.items():
        (case / name).parent.mkdir(parents=True, exist_ok=True)
        (case / name).write_text(text)
    case.mkdir(parents=True, exist_ok=True)
    expected = tmp_path / "task" / "expected" / "c1"
    expected.mkdir(parents=True)
    (expected / "answer.txt").write_text("secret")
    return case


def _manifest(case: Path) -> dict[str, str]:
    return {"TRAP_MANIFEST": json.dumps({"inputs_dir": str(case), "outputs_dir": "/nowhere"})}


def test_the_sandbox_is_a_copy_of_the_case_outside_the_task(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?", "data/ledger.txt": "1,2"})
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    try:
        assert box.question == "what?"
        assert (box.workdir / "data" / "ledger.txt").read_text() == "1,2"
        assert not box.workdir.is_relative_to(tmp_path.resolve())
        assert box.extra_inputs() == ["data/ledger.txt"]
    finally:
        box.close()
    assert not box.workdir.exists()


def test_os_junk_files_are_not_extra_inputs(tmp_path):
    case = _case(
        tmp_path,
        {
            "question.txt": "what?",
            ".DS_Store": "finder",
            "desktop.ini": "explorer",
            "data/Thumbs.db": "explorer",
            "data/.DS_Store": "finder",
            "data/ledger.txt": "1,2",
        },
    )
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    try:
        assert box.extra_inputs() == ["data/ledger.txt"]
    finally:
        box.close()


def test_a_sandbox_without_a_manifest_says_how_to_run_it():
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ={})
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "tp run" in str(e.value)


def test_a_sandbox_with_an_unparseable_manifest_says_so():
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(
            manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ={"TRAP_MANIFEST": "not json"}
        )
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "TRAP_MANIFEST" in str(e.value)


def test_a_case_without_the_prompt_file_is_a_config_error(tmp_path):
    case = _case(tmp_path, {"prompt.md": "what?"})
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "question.txt" in str(e.value)


def test_inputs_that_cannot_be_copied_are_a_config_error_and_leave_no_work_dir(tmp_path, monkeypatch):
    case = _case(tmp_path, {"question.txt": "what?"})
    (case / "data.csv").symlink_to(tmp_path / "gone.csv")  # dangling: copying it fails
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "cannot copy this case's inputs" in str(e.value)
    assert list(scratch.iterdir()) == [], "the half-filled work directory was left behind"


def test_a_question_that_is_not_utf8_is_a_config_error(tmp_path):
    case = _case(tmp_path, {})
    (case / "question.txt").write_bytes(b"caf\xe9?")
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    try:
        with pytest.raises(ShapeError) as e:
            _ = box.question
    finally:
        box.close()
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "question.txt" in str(e.value) and "UTF-8" in str(e.value)


def test_a_question_that_cannot_be_read_is_a_config_error(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    try:
        box.prompt_path.unlink()
        with pytest.raises(ShapeError) as e:
            _ = box.question
    finally:
        box.close()
    assert e.value.code is ShapeExit.CONFIG_ERROR


def test_the_env_loses_the_manifest_and_anything_naming_a_prefix(tmp_path):
    task = tmp_path / "task"
    env = {
        "TRAP_MANIFEST": "{}",
        "MY_MANIFEST": "{}",
        "POINTER": str(task / "inputs" / "c1"),
        "TASK_ROOT": str(task),
        "HOME": "/home/x",
        "PATH": f"{task / 'inputs' / '.venv' / 'bin'}:/usr/bin",
    }
    kept = scrubbed_env(env, manifest_envvar="MY_MANIFEST", prefixes=[task / "inputs"])
    assert kept == {"TASK_ROOT": str(task), "HOME": "/home/x", "PATH": env["PATH"]}
    assert scrubbed_env(env, manifest_envvar="MY_MANIFEST", prefixes=[task]) == {
        "HOME": "/home/x",
        "PATH": env["PATH"],
    }


def test_open_case_scrubs_the_inputs_root_and_every_scrub_path(tmp_path):
    case = _case(tmp_path, {"question.txt": "q"})
    parser = argparse.ArgumentParser()
    add_case_args(parser)
    args = parser.parse_args(["--scrub", str(tmp_path / "task")])
    environ = {**_manifest(case), "A": str(case), "B": str(tmp_path / "task"), "C": "keep"}
    box, env = open_case(args, environ)
    box.close()
    assert env == {"C": "keep"}


def test_open_case_defaults_to_the_real_environment(tmp_path, monkeypatch):
    case = _case(tmp_path, {"question.txt": "q"})
    monkeypatch.setenv("TRAP_MANIFEST", json.dumps({"inputs_dir": str(case), "outputs_dir": "/nowhere"}))
    parser = argparse.ArgumentParser()
    add_case_args(parser)
    args = parser.parse_args([])
    box, env = open_case(args)
    box.close()
    assert "TRAP_MANIFEST" not in env
    assert env["PATH"] == os.environ["PATH"]


def test_shape_parser_exits_24_on_bad_arguments(capsys):
    parser = ShapeParser(prog="tp-shape-test", description="a shape")
    add_case_args(parser)
    with pytest.raises(SystemExit) as e:
        parser.parse_args(["--deadline", "soon"])
    assert e.value.code == ShapeExit.CONFIG_ERROR
    err = capsys.readouterr().err
    assert err.startswith("usage:")
    assert "[trap]" in err


def test_fail_prints_the_message_and_returns_the_code(capsys):
    code = fail(ShapeError(ShapeExit.AGENT_ERROR, "the agent crashed"))
    assert code == ShapeExit.AGENT_ERROR
    assert capsys.readouterr().err == "[trap] the agent crashed\n"


def test_run_group_collects_output_and_exit_code(tmp_path):
    out, err, code = run_group(
        ["sh", "-c", "cat; echo oops >&2; exit 3"],
        cwd=tmp_path,
        env=os.environ,
        stdin="hi",
        deadline=Deadline(10),
    )
    assert (out, err.strip(), code) == ("hi", "oops", 3)


NOT_UTF8 = "import sys, time; sys.stdout.buffer.write(b'ok \\xff\\xfe'); sys.stdout.flush(); time.sleep({})"


def _decoded(raw: bytes) -> str:
    """``raw`` as a text-mode pipe decodes it with bad bytes replaced."""
    return raw.decode(locale.getpreferredencoding(False), errors="replace")


def test_run_group_replaces_output_that_is_not_utf8(tmp_path):
    out, _, code = run_group(
        [sys.executable, "-c", NOT_UTF8.format(0)],
        cwd=tmp_path,
        env=os.environ,
        stdin=None,
        deadline=Deadline(10),
    )
    assert (out, code) == (_decoded(b"ok \xff\xfe"), 0)


def test_run_group_replaces_bad_output_collected_after_the_deadline_too(tmp_path):
    out, _, code = run_group(
        [sys.executable, "-c", NOT_UTF8.format(30)],
        cwd=tmp_path,
        env=os.environ,
        stdin=None,
        deadline=Deadline(1),
    )
    assert (out, code) == (_decoded(b"ok \xff\xfe"), ShapeExit.TIMEOUT)


def test_the_deadline_kills_the_whole_group(tmp_path):
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    out, _, code = run_group(
        ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; echo started; wait"],
        cwd=tmp_path,
        env=os.environ,
        stdin=None,
        deadline=Deadline(1),
    )
    assert code == ShapeExit.TIMEOUT
    assert "started" in out
    assert time.monotonic() - started < 10
    assert process_gone(int(pidfile.read_text())), "the grandchild outlived the deadline"


def test_kill_group_is_a_noop_once_the_group_is_already_gone(tmp_path):
    proc = subprocess.Popen(["sh", "-c", "exit 0"], cwd=tmp_path, start_new_session=True)
    proc.wait()
    kill_group(proc)  # already reaped; os.killpg must raise ProcessLookupError, not blow up


def test_kill_group_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    # The child must finish installing the trap before we signal it, or a SIGTERM that
    # beats "trap" to the punch kills it outright and the SIGKILL escalation goes untested.
    ready = tmp_path / "ready"
    proc = subprocess.Popen(["sh", "-c", f"trap '' TERM; touch {ready}; sleep 30"], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child never reached its trap"
        kill_group(proc, grace=0.3)
        assert process_gone(proc.pid)
    finally:
        proc.wait(timeout=5)


class _NeverCommunicates:
    """A ``Popen`` stand-in whose ``communicate`` always times out — used to force the
    branch where the post-kill collection attempt itself times out."""

    def __init__(self, pid: int) -> None:
        self.pid = pid

    def communicate(self, input=None, timeout=None):
        raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)

    def wait(self, timeout=None):
        return 0


def test_run_group_gives_up_if_the_kill_never_frees_the_pipes(monkeypatch, tmp_path):
    dead = subprocess.Popen(["sh", "-c", "exit 0"], cwd=tmp_path, start_new_session=True)
    dead.wait()
    fake = _NeverCommunicates(dead.pid)
    monkeypatch.setattr("trap.shapes._case.subprocess.Popen", lambda *a, **k: fake)
    out, err, code = run_group(["ignored"], cwd=tmp_path, env={}, stdin=None, deadline=Deadline(0))
    assert out == ""
    assert err == "\n[trap] deadline reached; process group killed\n"
    assert code == ShapeExit.TIMEOUT

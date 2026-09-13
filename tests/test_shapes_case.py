"""The plumbing every built-in shape shares: sandbox, env scrub, deadline, group kill."""

from __future__ import annotations

import argparse
import json
import locale
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from trap.shapes._case import (
    MAX_NAMED_SYMLINKS,
    CaseSandbox,
    Deadline,
    ShapeError,
    ShapeExit,
    ShapeParser,
    add_case_args,
    fail,
    kill_group,
    kill_now,
    open_case,
    run_group,
    scrubbed_env,
)

from .conftest import INTERRUPTS, process_gone, reap, signal_main_thread_when, sleeper, wait_until

pytestmark = pytest.mark.usefixtures("signal_handlers_unchanged")


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


def test_inputs_that_cannot_be_copied_for_another_reason_are_a_config_error_and_leave_no_work_dir(
    tmp_path, monkeypatch
):
    case = _case(tmp_path, {"question.txt": "what?"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copytree", boom)
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "cannot copy this case's inputs" in str(e.value)
    assert list(scratch.iterdir()) == [], "the half-filled work directory was left behind"


# --- a symlink anywhere in a case's inputs is refused, never copied through -----------
#
# CaseSandbox.open's isolation promise is that a program or agent can only ever see this
# case's own inputs — never expected/. A symlink breaks that promise regardless of what it
# points at (the answers, another case, even a file inside this same case): a shape has no
# way to tell a safe link from a dangerous one, so every link is refused, unconditionally.


def test_a_symlink_to_the_answer_is_refused_by_name_and_leaves_no_work_dir(tmp_path, monkeypatch):
    case = _case(tmp_path, {"question.txt": "what?"})
    answer = case.parent.parent / "expected" / "c1" / "answer.txt"
    (case / "reference.txt").symlink_to(answer)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    message = str(e.value)
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "symlinks" in message and "reference.txt" in message
    assert "secret" not in message, "the answer the link points at must never appear in the refusal"
    assert list(scratch.iterdir()) == [], "a trap-case-* work dir was left behind"


def test_a_symlinked_subdirectory_is_refused(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    (case / "data").symlink_to(case.parent.parent / "expected" / "c1")
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "data" in str(e.value)


def test_a_symlink_pointing_inside_the_case_itself_is_still_refused(tmp_path):
    # Pins the reject-all policy: this link never reaches outside the case's own inputs,
    # yet a shape has no principled way to trust it more than one that does.
    case = _case(tmp_path, {"question.txt": "what?"})
    (case / "alias.txt").symlink_to("question.txt")
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "alias.txt" in str(e.value)


def test_the_prompt_file_itself_a_symlink_is_refused(tmp_path):
    case = _case(tmp_path, {})
    real = tmp_path / "real_question.txt"
    real.write_text("what?")
    (case / "question.txt").symlink_to(real)
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "question.txt" in str(e.value) and "symlinks" in str(e.value)


def test_an_inputs_dir_that_is_itself_a_symlink_is_refused(tmp_path):
    # Only a hand-made manifest can point inputs_dir at a symlink — the runner always
    # resolves it first — but a shape must not trust a manifest that does.
    real_case = _case(tmp_path, {"question.txt": "what?"})
    link = tmp_path / "task" / "inputs" / "c1-link"
    link.symlink_to(real_case)
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(link))
    message = str(e.value)
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "symlink" in message and str(link) in message


def test_more_symlinks_than_the_cap_are_named_and_the_rest_are_counted(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    target = case.parent.parent / "expected" / "c1" / "answer.txt"
    names = [f"link{i}.txt" for i in range(MAX_NAMED_SYMLINKS + 3)]
    for name in names:
        (case / name).symlink_to(target)
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    message = str(e.value)
    for name in sorted(names)[:MAX_NAMED_SYMLINKS]:
        assert name in message
    assert "and 3 more" in message


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


# --- an interrupt takes the child's whole group down before the shape goes -------------
#
# The child runs in its own session, so the terminal's Ctrl-C never reaches it, and under
# tp run the runner SIGKILLs the shape 0.25 s after that Ctrl-C: whatever the shape does
# about its child must happen at once, in the signal handler.


@pytest.mark.parametrize("sig", INTERRUPTS)
def test_an_interrupt_kills_the_group_at_once_and_exits_128_plus_the_signal(tmp_path, stray_signals, sig):
    pidfile = tmp_path / "pid"
    signal_main_thread_when(pidfile.exists, sig)
    try:
        with pytest.raises(SystemExit) as e:
            run_group(sleeper(pidfile), cwd=tmp_path, env=os.environ, stdin=None, deadline=Deadline(20))
        assert e.value.code == 128 + sig
        assert process_gone(int(pidfile.read_text()), timeout=1.0), "the grandchild outlived the interrupt"
    finally:
        reap(int(pidfile.read_text()))
    assert all(signal.getsignal(s) is stray_signals for s in INTERRUPTS), "the old handlers were not put back"


def test_run_group_kills_the_group_whatever_exception_stops_it(tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"

    def interrupted(self, input=None, timeout=None):
        assert wait_until(pidfile.exists)
        raise KeyboardInterrupt  # not from a signal: the handler never ran, run_group must still kill

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted)
    try:
        with pytest.raises(KeyboardInterrupt):
            run_group(sleeper(pidfile), cwd=tmp_path, env=os.environ, stdin=None, deadline=Deadline(20))
        assert process_gone(int(pidfile.read_text()), timeout=1.0)
    finally:
        reap(int(pidfile.read_text()))


def test_off_the_main_thread_run_group_leaves_signal_handlers_alone(tmp_path):
    seen: dict[str, object] = {}

    def work() -> None:
        seen["result"] = run_group(
            ["sh", "-c", "echo hi"], cwd=tmp_path, env=os.environ, stdin=None, deadline=Deadline(10)
        )

    before = {s: signal.getsignal(s) for s in INTERRUPTS}
    worker = threading.Thread(target=work)
    worker.start()
    worker.join(timeout=10)
    assert seen["result"] == ("hi\n", "", 0)
    assert {s: signal.getsignal(s) for s in INTERRUPTS} == before


def test_kill_group_tolerates_a_group_with_only_an_unreaped_child_left(tmp_path):
    proc = subprocess.Popen(["sh", "-c", "exit 0"], cwd=tmp_path, start_new_session=True)
    assert wait_until(lambda: _is_zombie(proc.pid))
    kill_group(proc)  # macOS refuses to signal a zombie-only group with EPERM; that is "gone" too
    proc.wait(timeout=5)


def test_kill_now_is_a_noop_once_the_group_is_gone(tmp_path):
    proc = subprocess.Popen(["sh", "-c", "exit 0"], cwd=tmp_path, start_new_session=True)
    proc.wait()
    kill_now(proc.pid)


def _is_zombie(pid: int) -> bool:
    """True once ``pid`` has exited but has not been waited for."""
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    return state.strip().startswith("Z")

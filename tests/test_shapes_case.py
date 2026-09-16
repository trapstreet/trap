"""The plumbing every built-in shape shares: sandbox, env scrub, deadline, group kill."""

from __future__ import annotations

import argparse
import json
import locale
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from trap.shapes import _case as case_module
from trap.shapes._case import (
    MAX_NAMED_SYMLINKS,
    CaseSandbox,
    Deadline,
    ShapeError,
    ShapeExit,
    ShapeParser,
    _secure_and_verify,
    add_case_args,
    copy_tree_without_symlinks,
    fail,
    kill_group,
    kill_now,
    open_case,
    remove_tree,
    run_group,
    scrubbed_env,
)

from .conftest import INTERRUPTS, process_gone, reap, signal_main_thread_when, sleeper, unlock, wait_until

pytestmark = pytest.mark.usefixtures("signal_handlers_unchanged")


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """The temp directory work dirs are made in, for this test only: one a test leaves
    behind — on purpose, or by failing — is pytest's to remove, never a stray
    ``trap-case-*`` in the real TMPDIR, and is unlocked first whatever modes it holds."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    yield scratch
    unlock(scratch)


def _raises(error: BaseException):
    """A stand-in for any callable that fails with ``error`` however it is called."""

    def fail(*args, **kwargs):
        raise error

    return fail


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


# --- a symlink in a case's inputs is never copied through ------------------------------
#
# CaseSandbox.open's isolation promise is that a program or agent can only ever see this
# case's own inputs — never expected/. A link to a regular file under the directory that
# holds the case (the inputs root, when the runner started the shape) is copied as that
# file: the runner has already refused one that reaches the answers, and a shape cannot see
# where they are. Every other link — out of that directory, to a directory, to nothing — is
# refused, and the work dir never holds a link.


def _shared(case: Path) -> Path:
    """inputs/context/data.csv beside ``case``: a file every case may link to."""
    context = case.parent / "context"
    context.mkdir()
    (context / "data.csv").write_text("day,count\n1,42\n")
    return context / "data.csv"


@pytest.mark.parametrize(
    ("name", "target"),
    [
        pytest.param("data.csv", "../context/data.csv", id="beside the case"),
        pytest.param("alias.txt", "question.txt", id="in the case itself"),
        pytest.param("deep/data.csv", "../../context/latest.csv", id="nested, through a chain of links"),
    ],
)
def test_a_link_to_a_file_under_the_inputs_root_is_copied_as_that_file(
    tmp_path, scratch, name: str, target: str
):
    case = _case(tmp_path, {"question.txt": "what?"})
    shared = _shared(case)
    (shared.parent / "latest.csv").symlink_to("data.csv")
    (case / name).parent.mkdir(exist_ok=True)
    (case / name).symlink_to(target)
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    try:
        copied = box.workdir / name
        assert not copied.is_symlink() and copied.is_file()
        assert copied.read_text() == (case / name).read_text()
        assert name in box.extra_inputs()
        assert [p for p in box.workdir.rglob("*") if p.is_symlink()] == []
    finally:
        box.close()
    assert list(scratch.iterdir()) == []


def test_a_file_link_under_a_read_only_input_directory_is_still_copied(tmp_path, scratch):
    case = _case(tmp_path, {"question.txt": "what?"})
    _shared(case)
    (case / "data").mkdir()
    (case / "data" / "data.csv").symlink_to("../../context/data.csv")
    os.chmod(case / "data", 0o555)
    try:
        box = CaseSandbox.open(
            manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
        )
        try:
            assert (box.workdir / "data" / "data.csv").read_text() == "day,count\n1,42\n"
        finally:
            box.close()
    finally:
        os.chmod(case / "data", 0o755)
    assert list(scratch.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "make_target"),
    [
        pytest.param("data.csv", lambda task: task / "shared.csv", id="a file outside the inputs root"),
        pytest.param("data", lambda task: task / "inputs" / "context", id="a directory in the inputs root"),
        pytest.param("gone.csv", lambda task: task / "inputs" / "context" / "gone.csv", id="dangling"),
        pytest.param("loop.csv", lambda task: task / "inputs" / "c1" / "loop.csv", id="a loop"),
    ],
)
def test_any_other_link_is_refused_by_name_and_leaves_no_work_dir(tmp_path, scratch, name: str, make_target):
    case = _case(tmp_path, {"question.txt": "what?"})
    _shared(case)
    task = tmp_path / "task"
    (task / "shared.csv").write_text("s3cr3t")
    (case / name).symlink_to(make_target(task))
    (case / "ok.csv").symlink_to("../context/data.csv")  # one it may copy, not named
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    message = str(e.value)
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "symlinks" in message and name in message and "ok.csv" not in message
    assert "s3cr3t" not in message
    assert list(scratch.iterdir()) == [], "a trap-case-* work dir was left behind"


def test_a_link_that_lands_in_the_work_dir_while_file_links_are_copied_is_still_refused(
    tmp_path, scratch, monkeypatch
):
    # The work dir is checked again after the linked files are written into it.
    case = _case(tmp_path, {"question.txt": "what?"})
    _shared(case)
    (case / "data.csv").symlink_to("../context/data.csv")
    monkeypatch.setattr(case_module, "_copy_linked_file", lambda target, dest: os.symlink(target, dest))
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "data.csv" in str(e.value)
    assert list(scratch.iterdir()) == []


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


def test_the_prompt_file_itself_a_symlink_is_refused(tmp_path):
    case = _case(tmp_path, {})
    real = tmp_path / "real_question.txt"
    real.write_text("what?")
    (case / "question.txt").symlink_to(real)
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "question.txt" in str(e.value) and "symlinks" in str(e.value)


def test_a_hand_made_manifest_pointing_inputs_dir_at_a_symlink_is_refused(tmp_path):
    # The runner always hands a shape a resolved inputs_dir — a case directory that is
    # itself a link is followed by the runner before a shape ever sees it — so only a
    # hand-made manifest can reach this check, and a shape must not trust one that does.
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


# --- copy_tree_without_symlinks: the one copy every shape uses for a case's inputs and
# for --skill never creates a link in the destination, and reports every one it found --
# (every link, unless it is told where a link to a file may point)


def test_copy_tree_without_symlinks_creates_no_link_and_reports_every_one(tmp_path):
    src = tmp_path / "src"
    (src / "data").mkdir(parents=True)
    (src / "data" / "real.txt").write_text("kept")
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cr3t")
    (src / "data" / "nested_link.txt").symlink_to(secret)  # a link inside a real subdir
    (src / "top_link.txt").symlink_to(secret)  # a file link at the top
    (src / "linked_dir").symlink_to(src / "data")  # a directory link at the top
    dst = tmp_path / "dst"

    links = copy_tree_without_symlinks(src, dst)

    assert links == sorted(["data/nested_link.txt", "top_link.txt", "linked_dir"])
    assert (dst / "data" / "real.txt").read_text() == "kept"
    assert not (dst / "data" / "nested_link.txt").exists()
    assert not (dst / "top_link.txt").exists()
    assert not (dst / "linked_dir").exists()
    for p in dst.rglob("*"):
        assert not p.is_symlink(), p


def test_a_directory_the_walk_cannot_read_refuses_the_case_and_leaves_no_work_dir(tmp_path, monkeypatch):
    case = _case(tmp_path, {"question.txt": "what?"})
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def fake_walk(root, followlinks=False, onerror=None):
        if onerror is not None:
            onerror(OSError("permission denied"))
        return
        yield  # pragma: no cover - never reached; makes this a generator function

    monkeypatch.setattr(os, "walk", fake_walk)
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert list(scratch.iterdir()) == [], "a trap-case-* work dir was left behind"


def test_secure_and_verify_finds_a_symlink_that_slipped_past_the_ignore_callback(tmp_path):
    # symlinks=True is a backstop for a link the ignore callback missed; this tests that
    # backstop directly by planting links straight into an already-"copied" tree.
    dst = tmp_path / "dst"
    (dst / "sub").mkdir(parents=True)
    (dst / "sub" / "file.txt").write_text("x")
    os.chmod(dst / "sub", 0o555)  # a directory copystat left read-only
    outside_secret = tmp_path / "elsewhere.txt"
    outside_secret.write_text("s3cr3t")
    (dst / "link.txt").symlink_to(outside_secret)
    linked_dir_target = tmp_path / "real_dir"
    linked_dir_target.mkdir()
    linked_dir_target.chmod(0o750)
    (dst / "linked_dir").symlink_to(linked_dir_target)

    links = _secure_and_verify(dst)

    assert sorted(links) == ["link.txt", "linked_dir"]
    assert stat.S_IMODE((dst / "sub").stat().st_mode) & 0o700 == 0o700, "the real dir was not made writable"
    # chmod must never reach through the dir symlink to what it names.
    assert stat.S_IMODE(linked_dir_target.stat().st_mode) == 0o750


def test_secure_and_verify_fails_closed_when_the_walk_cannot_read_a_directory(tmp_path, monkeypatch):
    dst = tmp_path / "dst"
    dst.mkdir()

    def fake_walk(root, followlinks=False, onerror=None):
        if onerror is not None:
            onerror(OSError("permission denied"))
        return
        yield  # pragma: no cover - never reached; makes this a generator function

    monkeypatch.setattr(os, "walk", fake_walk)
    with pytest.raises(OSError):
        _secure_and_verify(dst)


def test_remove_tree_tolerates_a_path_that_is_already_gone(tmp_path, capsys):
    remove_tree(tmp_path / "already-gone")  # must not raise
    assert capsys.readouterr().err == "", "nothing was left, so there is nothing to warn about"


def test_remove_tree_never_reaches_through_a_root_that_is_a_link(tmp_path, capsys):
    target = tmp_path / "target"
    target.mkdir()
    (target / "kept.txt").write_text("not the tree's")
    target.chmod(0o550)
    link = tmp_path / "link"
    link.symlink_to(target)
    try:
        remove_tree(link)
        assert stat.S_IMODE(target.stat().st_mode) == 0o550, "cleanup chmod'd what the link names"
        assert (target / "kept.txt").read_text() == "not the tree's"
    finally:
        target.chmod(0o755)
    assert capsys.readouterr().err.startswith(f"[trap] could not remove the work dir {link}: ")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can lstat through a 000 directory regardless")
def test_remove_tree_reports_a_root_whose_parent_went_unsearchable(tmp_path, capsys):
    # os.path.lexists(root) returns False on *any* lstat error, not only "gone" — so a
    # program that left the work dir's own parent unsearchable (its own `chmod 000 ..`)
    # used to make the work dir read as already removed, and the warning never fired.
    parent = tmp_path / "denied"
    parent.mkdir()
    root = parent / "workdir"
    root.mkdir()
    (root / "file.txt").write_text("x")
    parent.chmod(0o000)
    try:
        remove_tree(root)  # must not raise
    finally:
        parent.chmod(0o755)
    err = capsys.readouterr().err
    assert err.startswith(f"[trap] could not remove the work dir {root}: ")
    assert err.count("\n") == 1, "one line, not a traceback"


def test_remove_tree_names_the_full_path_of_a_nested_failure(tmp_path, monkeypatch, capsys):
    # _open_up_directories repairs every directory mode it *can* chmod, but one it can't
    # (an immutable flag, say) it leaves alone — its own docstring says as much: skipped,
    # "left for the removal that follows to report." rmtree still walks into that
    # directory and hits a denied unlink inside it, and shutil's descriptor-based unlink
    # reports the failing path two ways that disagree: onexc's own ``path`` argument gets
    # the full path it built up while walking ("d/f"), but the exception it hands
    # alongside names only the bare entry it unlinked ("f"), since that unlink ran
    # relative to an already-open directory fd (confirmed directly against a real
    # rmtree()/onexc() run, not just read from the source). Reproduced here without
    # fighting the filesystem for the exact mode that defeats _open_up_directories.
    root = tmp_path / "workdir"
    root.mkdir()
    nested = os.path.join(os.fspath(root), "d", "f")

    def fake_rmtree(path, onexc):
        err = PermissionError(13, "Permission denied")
        err.filename = "f"
        onexc(os.unlink, nested, err)

    monkeypatch.setattr(shutil, "rmtree", fake_rmtree)
    remove_tree(root)  # must not raise
    err = capsys.readouterr().err
    assert nested in err, err
    assert err.count("\n") == 1, "one line, not a traceback"


def test_remove_tree_uses_the_lstat_error_when_rmtree_recorded_none_of_its_own(tmp_path, monkeypatch, capsys):
    # A root rmtree could not even reach (nothing recorded, no exception of its own) can
    # still be reported: the final lstat that finds it "maybe left behind" carries a
    # reason of its own, and that is what gets printed.
    root = tmp_path / "workdir"
    root.mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)  # records nothing, removes nothing
    real_lstat = os.lstat

    def flaky_lstat(path, *a, **k):
        if os.fspath(path) == os.fspath(root):
            raise PermissionError(13, "synthetic failure reaching the root")
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(os, "lstat", flaky_lstat)
    remove_tree(root)  # must not raise
    err = capsys.readouterr().err
    assert err.startswith(f"[trap] could not remove the work dir {root}: ")
    assert "synthetic failure reaching the root" in err
    assert err.count("\n") == 1, "one line, not a traceback"


def test_remove_tree_falls_back_to_reason_unknown_when_nothing_explains_the_leftover(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "workdir"
    root.mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)  # records nothing, removes nothing
    remove_tree(root)  # must not raise
    assert capsys.readouterr().err == f"[trap] could not remove the work dir {root}: reason unknown\n"


# --- close() removes whatever the case's program left, and never reaches past it -------
#
# By the time close() runs the program is gone, but the work dir was the program's to
# shape: it can leave a directory no one can list (000), one that can be listed but not
# searched (444), or a read-only one holding a link — to nothing, or to a file outside.

LOCKED_BY_THE_CHILD = {
    "an unlistable dir": "mkdir d && chmod 000 d",
    "an unsearchable dir holding a file": "mkdir d && echo x > d/f && chmod 444 d",
    "a read-only dir holding a dangling link": "mkdir d && ln -s nowhere d/l && chmod 555 d",
}


@pytest.mark.parametrize("script", list(LOCKED_BY_THE_CHILD.values()), ids=list(LOCKED_BY_THE_CHILD))
def test_close_removes_whatever_the_child_left_locked(tmp_path, scratch, capsys, script):
    case = _case(tmp_path, {"question.txt": "what?"})
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    subprocess.run(["sh", "-c", script], cwd=box.workdir, check=True)
    box.close()
    assert not box.workdir.exists()
    assert list(scratch.iterdir()) == [], "a trap-case-* work dir was left behind"
    assert capsys.readouterr().err == ""


def test_close_never_changes_a_file_outside_the_work_dir_that_a_link_names(tmp_path, scratch):
    case = _case(tmp_path, {"question.txt": "what?"})
    outside = tmp_path / "outside.txt"
    outside.write_text("not the case's")
    outside.chmod(0o444)
    before = stat.S_IMODE(outside.stat().st_mode)
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    script = f"mkdir ro && ln -s {shlex.quote(str(outside))} ro/l && chmod 555 ro"
    subprocess.run(["sh", "-c", script], cwd=box.workdir, check=True)
    try:
        box.close()
        assert (before, stat.S_IMODE(outside.stat().st_mode)) == (0o444, 0o444), (
            "cleanup chmod'd through a link"
        )
        assert not box.workdir.exists()
    finally:
        outside.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes from a read-only directory regardless")
def test_a_work_dir_that_cannot_be_removed_is_left_with_a_warning_and_its_parent_untouched(
    tmp_path, monkeypatch, capsys
):
    # A read-only TMPDIR: everything inside the work dir can go, the work dir itself
    # cannot — and the only way to change that is to reach outside the work dir.
    parent = tmp_path / "readonly-tmp"
    parent.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(parent))
    case = _case(tmp_path, {"question.txt": "what?"})
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    parent.chmod(0o555)
    try:
        box.close()  # must not raise
        assert stat.S_IMODE(parent.stat().st_mode) == 0o555, "cleanup chmod'd the work dir's parent"
        assert box.workdir.is_dir()
    finally:
        parent.chmod(0o755)
    err = capsys.readouterr().err
    assert err.startswith(f"[trap] could not remove the work dir {box.workdir}: ")
    assert err.count("\n") == 1, "one line, not a traceback"


def test_close_never_raises_even_when_the_removal_itself_fails(tmp_path, scratch, monkeypatch, capsys):
    case = _case(tmp_path, {"question.txt": "what?"})
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    monkeypatch.setattr(shutil, "rmtree", _raises(OSError("the disk went away")))
    box.close()
    assert (
        capsys.readouterr().err == f"[trap] could not remove the work dir {box.workdir}: the disk went away\n"
    )


@pytest.mark.parametrize("refusal", ["a symlink in the inputs", "a copy that fails"])
def test_a_refused_case_is_still_exit_24_when_its_work_dir_cannot_be_removed(
    tmp_path, scratch, monkeypatch, capsys, refusal
):
    case = _case(tmp_path, {"question.txt": "what?"})
    if refusal == "a symlink in the inputs":
        (case / "reference.txt").symlink_to(case.parent.parent / "expected" / "c1" / "answer.txt")
    else:
        monkeypatch.setattr(shutil, "copytree", _raises(OSError("disk full")))
    monkeypatch.setattr(shutil, "rmtree", _raises(OSError("the disk went away")))
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "could not remove the work dir" in capsys.readouterr().err


# --- the work directory is private, and writable even from a read-only input ----------


def test_the_work_dir_is_private_even_when_the_inputs_dir_is_wide_open(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    os.chmod(case, 0o777)
    try:
        box = CaseSandbox.open(
            manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
        )
        try:
            assert stat.S_IMODE(box.workdir.stat().st_mode) == 0o700
        finally:
            box.close()
    finally:
        os.chmod(case, 0o755)


def test_the_work_dir_is_private_before_its_copy_is_walked(tmp_path, monkeypatch):
    # The copy takes the inputs' mode (0777 here) when copytree finishes; the walk that
    # follows can take a while on a big case, and the work dir must not sit open to
    # everyone meanwhile.
    case = _case(tmp_path, {"question.txt": "what?"})
    os.chmod(case, 0o777)
    modes_at_the_walk: list[int] = []
    walk = case_module._secure_and_verify

    def spy(dst: Path) -> list[str]:
        modes_at_the_walk.append(stat.S_IMODE(os.stat(dst).st_mode))
        return walk(dst)

    monkeypatch.setattr(case_module, "_secure_and_verify", spy)
    try:
        CaseSandbox.open(
            manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
        ).close()
    finally:
        os.chmod(case, 0o755)
    assert modes_at_the_walk == [0o700]


def test_a_read_only_input_dir_still_yields_a_writable_copy_and_close_leaves_nothing(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?", "data/ledger.txt": "1,2"})
    data_dir = case / "data"
    os.chmod(data_dir, 0o555)
    try:
        box = CaseSandbox.open(
            manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
        )
        try:
            assert stat.S_IMODE((box.workdir / "data").stat().st_mode) & 0o700 == 0o700
        finally:
            box.close()
        assert not box.workdir.exists()
    finally:
        os.chmod(data_dir, 0o755)


def test_a_refused_case_with_a_read_only_dir_holding_a_link_leaves_no_work_dir(tmp_path, monkeypatch):
    case = _case(tmp_path, {"question.txt": "what?"})
    data_dir = case / "data"
    data_dir.mkdir()
    target = case.parent.parent / "expected" / "c1" / "answer.txt"
    (data_dir / "link.txt").symlink_to(target)
    os.chmod(data_dir, 0o555)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    try:
        with pytest.raises(ShapeError) as e:
            CaseSandbox.open(
                manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
            )
        assert e.value.code is ShapeExit.CONFIG_ERROR
        assert list(scratch.iterdir()) == [], "a trap-case-* work dir was left behind"
    finally:
        os.chmod(data_dir, 0o755)


def test_close_removes_a_work_dir_a_child_made_read_only(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    box = CaseSandbox.open(
        manifest_envvar="TRAP_MANIFEST", prompt_file="question.txt", environ=_manifest(case)
    )
    made = box.workdir / "made_by_child"
    made.mkdir()
    (made / "file.txt").write_text("x")
    os.chmod(made, 0o555)
    box.close()
    assert not box.workdir.exists()


# --- --prompt-file must stay a plain relative path inside the case --------------------


def test_prompt_file_must_not_be_an_absolute_path(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(manifest_envvar="TRAP_MANIFEST", prompt_file="/etc/passwd", environ=_manifest(case))
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "/etc/passwd" in str(e.value)


def test_prompt_file_must_not_escape_the_case_with_dotdot(tmp_path):
    case = _case(tmp_path, {"question.txt": "what?"})
    with pytest.raises(ShapeError) as e:
        CaseSandbox.open(
            manifest_envvar="TRAP_MANIFEST", prompt_file="../secret.txt", environ=_manifest(case)
        )
    assert e.value.code is ShapeExit.CONFIG_ERROR
    assert "../secret.txt" in str(e.value)


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


def test_a_normal_exit_still_kills_whatever_the_program_backgrounded(tmp_path):
    # argv can exit 0 with a grandchild still running in its process group — output
    # redirected away, say — and proc.communicate() returns as soon as argv itself is
    # gone, without waiting on that grandchild. run_group must take the whole group down
    # anyway, or that grandchild outlives the shape into cleanup.
    pidfile = tmp_path / "pid"
    out, _, code = run_group(
        ["sh", "-c", f"sleep 30 >/dev/null 2>&1 & echo $! > {pidfile}; echo done"],
        cwd=tmp_path,
        env=os.environ,
        stdin=None,
        deadline=Deadline(10),
    )
    assert (out, code) == ("done\n", 0)
    assert process_gone(int(pidfile.read_text()), timeout=1.0), "the backgrounded process outlived the shape"


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

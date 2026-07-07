from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import git
import pytest

from trap.git_ops import GitOpsError, LocalRepo, ParsedGitUrl, RemoteRepo
from trap.git_ops.rev import DefaultBranch, NamedRef, PinnedSha, RevStrategy


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run(path, "init", "-q", "-b", branch)
    _run(path, "config", "user.email", "t@t")
    _run(path, "config", "user.name", "t")
    (path / "trap.yaml").write_text("cmd: x")
    _run(path, "add", "-A")
    _run(path, "commit", "-qm", "c1")
    return path


# -- ParsedGitUrl -------------------------------------------------------------


@pytest.mark.parametrize(
    "s,remote",
    [
        ("git+https://github.com/o/r", True),
        ("https://x/y", True),
        ("git@github.com:o/r.git", True),
        ("ssh://git@h/o/r", True),
        ("file:///tmp/x", True),
        ("./local", False),
        ("../task", False),
        ("/abs", False),
        ("foo", False),
    ],
)
def test_looks_remote(s, remote):
    assert ParsedGitUrl.looks_remote(s) is remote


def test_from_full_url_all_parts():
    p = ParsedGitUrl.from_full_url("git+https://github.com/org/repo@v1.0#subdirectory=tasks/a")
    assert p.repo == "https://github.com/org/repo"
    assert p.rev == "v1.0"
    assert p.subdirectory == "tasks/a"
    assert p.basename == "repo"


def test_from_full_url_ignores_other_fragments():
    p = ParsedGitUrl.from_full_url("https://x/r#egg=foo&subdirectory=sub")
    assert p.subdirectory == "sub"  # the non-subdirectory fragment part is skipped


def test_normalised_url_forms():
    assert (
        ParsedGitUrl.from_full_url("git@github.com:org/my-repo.git").normalised_url
        == "https://github.com/org/my-repo"
    )
    assert ParsedGitUrl.from_full_url("ssh://git@gitlab.com/u/r").normalised_url == "https://gitlab.com/u/r"
    assert ParsedGitUrl.from_full_url("https://github.com/u/r.git").normalised_url == "https://github.com/u/r"


def test_from_full_url_rejects_local():
    with pytest.raises(GitOpsError):
        ParsedGitUrl.from_full_url("./local")


def test_for_rev_classification():
    assert isinstance(RevStrategy.for_rev(None), DefaultBranch)
    assert isinstance(RevStrategy.for_rev("a1b2c3d"), PinnedSha)
    assert isinstance(RevStrategy.for_rev("v1.0"), NamedRef)


def test_remote_local_dir_subdirectory(tmp_path):
    rr = RemoteRepo(ParsedGitUrl.from_full_url("git+https://x/r#subdirectory=sub"), tmp_path / "root")
    assert rr.local_dir == tmp_path / "root" / "sub"


# -- RemoteRepo: clone / sync via file:// repos -------------------------------


def test_clone_default_branch_then_sync_noop(tmp_path):
    src = _repo(tmp_path / "src")
    rr = RemoteRepo(ParsedGitUrl.from_full_url(f"file://{src}"), tmp_path / "clone")
    assert rr.ensure(progress_func=lambda m: None) is True  # fresh clone
    assert (tmp_path / "clone" / "trap.yaml").exists()
    assert rr.ensure() is False  # default branch never auto-updates


def test_clone_failure_raises(tmp_path):
    rr = RemoteRepo(ParsedGitUrl.from_full_url("git+file:///nonexistent-trap-xyz"), tmp_path / "c")
    with pytest.raises(GitOpsError):
        rr.ensure()


def test_sync_non_git_dir_raises(tmp_path):
    (tmp_path / "c").mkdir()
    rr = RemoteRepo(ParsedGitUrl.from_full_url(f"file://{tmp_path}/src"), tmp_path / "c")
    with pytest.raises(GitOpsError):
        rr.ensure()


def test_sync_url_mismatch_raises(tmp_path):
    src = _repo(tmp_path / "src")
    other = _repo(tmp_path / "other")
    dest = tmp_path / "clone"
    RemoteRepo(ParsedGitUrl.from_full_url(f"file://{src}"), dest).ensure()
    with pytest.raises(GitOpsError):
        RemoteRepo(ParsedGitUrl.from_full_url(f"file://{other}"), dest).ensure()


def test_pinned_sha_clone_and_reconcile(tmp_path):
    src = _repo(tmp_path / "src")
    sha = git.Repo(src).head.commit.hexsha
    rr = RemoteRepo(ParsedGitUrl.from_full_url(f"file://{src}@{sha}"), tmp_path / "clone")
    assert rr.ensure() is True
    assert rr.ensure() is False  # HEAD matches the pinned sha


def test_pinned_sha_reconcile_mismatch(tmp_path):
    repo = git.Repo(_repo(tmp_path / "src"))
    with pytest.raises(GitOpsError):
        PinnedSha("deadbeef").reconcile(repo, tmp_path, None)
    assert PinnedSha(repo.head.commit.hexsha[:12]).reconcile(repo, tmp_path, None) is False


def test_named_ref_branch_fast_forward(tmp_path):
    src = _repo(tmp_path / "src", branch="main")
    rr = RemoteRepo(ParsedGitUrl.from_full_url(f"file://{src}@main"), tmp_path / "clone")
    assert rr.ensure() is True  # clone branch
    assert rr.ensure(progress_func=lambda m: None) is False  # up to date (fetch w/ progress)
    (src / "trap.yaml").write_text("cmd: y")
    _run(src, "commit", "-qam", "c2")
    assert rr.ensure(progress_func=lambda m: None) is True  # fast-forwarded (update w/ progress)


def test_named_ref_fetch_failure(tmp_path):
    src = _repo(tmp_path / "src", branch="main")
    rr = RemoteRepo(ParsedGitUrl.from_full_url(f"file://{src}@main"), tmp_path / "clone")
    rr.ensure()
    shutil.rmtree(src)  # source gone → fetch fails
    with pytest.raises(GitOpsError):
        rr.ensure(progress_func=lambda m: None)


def test_named_ref_diverged_branch_errors(tmp_path):
    src = _repo(tmp_path / "src", branch="main")
    clone = tmp_path / "clone"
    rr = RemoteRepo(ParsedGitUrl.from_full_url(f"file://{src}@main"), clone)
    rr.ensure()
    _run(clone, "config", "user.email", "t@t")
    _run(clone, "config", "user.name", "t")
    (clone / "local.txt").write_text("x")  # diverge the clone
    _run(clone, "add", "-A")
    _run(clone, "commit", "-qm", "local")
    (src / "remote.txt").write_text("y")  # diverge the source the other way
    _run(src, "add", "-A")
    _run(src, "commit", "-qm", "remote")
    with pytest.raises(GitOpsError):
        rr.ensure()  # no progress_func → covers the silent fetch/pull paths too


# -- LocalRepo provenance -----------------------------------------------------


def test_provenance_clean_with_origin(tmp_path):
    src = _repo(tmp_path / "src")
    _run(src, "remote", "add", "origin", "https://github.com/o/r.git")
    prov = LocalRepo.provenance_of(src)
    assert prov.repo == "https://github.com/o/r"
    assert len(prov.commit) == 40


def test_provenance_dirty_is_empty(tmp_path):
    src = _repo(tmp_path / "src")
    _run(src, "remote", "add", "origin", "https://github.com/o/r.git")
    (src / "trap.yaml").write_text("changed")  # tracked file modified → dirty
    assert not LocalRepo.provenance_of(src).repo


def test_provenance_no_origin_is_empty(tmp_path):
    assert not LocalRepo.provenance_of(_repo(tmp_path / "src")).repo


def test_provenance_non_git_is_empty(tmp_path):
    assert not LocalRepo.provenance_of(tmp_path).repo
    assert LocalRepo.open(tmp_path) is None

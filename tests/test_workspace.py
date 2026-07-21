"""Unit tests for workspace addressing: solution keys, derived latest, key listing."""

from __future__ import annotations

import json

import pytest

from trap.cli import app
from trap.workspace import SolutionIdentity, Workspace

# -- SolutionIdentity ---------------------------------------------------------------


def test_key_aliases_of_one_path_agree(tmp_path, monkeypatch):
    (tmp_path / "my-sol").mkdir()
    monkeypatch.chdir(tmp_path)
    keys = {
        SolutionIdentity.from_spec("my-sol").dirname,
        SolutionIdentity.from_spec("./my-sol").dirname,
        SolutionIdentity.from_spec(str(tmp_path / "my-sol")).dirname,
    }
    assert len(keys) == 1
    assert next(iter(keys)).startswith("my-sol-")


def test_key_none_is_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert SolutionIdentity.from_spec(None) == SolutionIdentity.from_spec(".")


def test_key_same_name_different_path_differ(tmp_path, monkeypatch):
    (tmp_path / "a" / "sol").mkdir(parents=True)
    (tmp_path / "b" / "sol").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert SolutionIdentity.from_spec("a/sol") != SolutionIdentity.from_spec("b/sol")


def test_key_sanitises_unsafe_name(tmp_path, monkeypatch):
    (tmp_path / "@@@").mkdir()
    monkeypatch.chdir(tmp_path)
    assert SolutionIdentity.from_spec("@@@").dirname.startswith("solution-")


def test_key_remote_url_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with_subdir = SolutionIdentity.from_spec(
        "git+https://github.com/org/repo.git#subdirectory=variants/haiku"
    )
    assert with_subdir.dirname.startswith("haiku-")
    at_root = SolutionIdentity.from_spec("git+https://github.com/org/repo.git")
    assert at_root.dirname.startswith("repo-")
    assert with_subdir != at_root
    # clone location and rev don't change the identity; the .git suffix normalises away
    assert at_root == SolutionIdentity.from_spec("git+https://github.com/org/repo@v1.0")


# -- Workspace.latest_run --------------------------------------------------------


def test_latest_run_picks_newest_completed(tmp_path):
    task_dir = tmp_path / "runs" / "k" / "t"
    task_dir.mkdir(parents=True)
    for ts, with_report in [
        ("2026-07-16T10:00:00", True),
        ("2026-07-16T11:00:00", True),
        ("2026-07-16T12:00:00", False),  # crashed run: no report — never wins
    ]:
        (task_dir / ts).mkdir()
        if with_report:
            (task_dir / ts / "report.json").write_text("{}")
    (task_dir / "notes").mkdir()  # non-timestamp dir is ignored
    (task_dir / "stray.txt").write_text("")  # files are ignored
    (task_dir / "latest").symlink_to("2026-07-16T10:00:00")  # stray symlink is ignored
    assert Workspace(tmp_path, "k", "t").latest_run() == "2026-07-16T11:00:00"


def test_latest_run_empty_and_missing(tmp_path):
    assert Workspace(tmp_path, "k", "t").latest_run() is None
    (tmp_path / "runs" / "k" / "t").mkdir(parents=True)
    assert Workspace(tmp_path, "k", "t").latest_run() is None


def test_resolved_run_without_completed_runs_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="no completed runs"):
        Workspace(tmp_path, "k", "t").resolved_run("latest")


def test_load_missing_named_run_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="no report found"):
        Workspace(tmp_path, "k", "t").load("2026-07-16T10:00:00")


# -- Workspace.solution_keys ------------------------------------------------------


def test_solution_keys(tmp_path):
    ws = Workspace(tmp_path, "k", "t")
    assert ws.solution_keys() == []
    (tmp_path / "runs" / "b-key").mkdir(parents=True)
    (tmp_path / "runs" / "a-key").mkdir()
    (tmp_path / "runs" / "stray.txt").write_text("")
    assert ws.solution_keys() == ["a-key", "b-key"]


# -- the not-found error self-help (through the CLI) -----------------------------


def test_report_miss_lists_known_solutions(make_project, runner):
    sol = make_project(cmd="sh -c 'echo hi'", cases=["c1"])  # cwd == sol
    # another solution's runs exist in this workspace
    (sol / ".trap" / "runs" / "other-sol-abc123" / "t").mkdir(parents=True)
    res = runner.invoke(app, ["report"])
    assert res.exit_code == 2
    assert "no completed runs" in res.output
    assert "other-sol-abc123" in res.output
    # with runs present the likely cause is a mismatch, not a missing tp run
    assert "SOLUTION" in res.output
    assert "tp run first" not in res.output


def test_run_prints_run_id_and_report_path(make_project, runner):
    make_project(cmd="sh -c 'echo hi'", cases=["c1"])
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert "report.json" in res.output


def test_runs_are_solution_scoped(make_project, runner, tmp_path):
    # two variants run from the same cwd/workspace must not share `latest`
    sol = make_project(cmd="sh -c 'echo one'", cases=["c1"])
    other = sol / "other"
    other.mkdir()
    (other / "trap.yaml").write_text(
        json.dumps({"cmd": "sh -c 'echo two'", "tasks": {"t": {"source": str(tmp_path / "task")}}})
    )
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    assert runner.invoke(app, ["run", "other", "--no-environment"]).exit_code == 0
    root = (sol / ".trap").resolve()
    keys = sorted(p.name for p in (root / "runs").iterdir())
    assert len(keys) == 2  # one namespace per solution, each deriving its own latest
    for key in keys:
        assert Workspace(root, key, "t").latest_run() is not None

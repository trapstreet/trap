from __future__ import annotations

import json
from pathlib import Path

import pytest

from trap.git_ops import GitOpsError
from trap.loader import ConfigError, TrapLoader, TraptaskLoader
from trap.models import TaskBinding


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data))
    return path


def test_from_solution_remote_not_allowed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(GitOpsError):
        TrapLoader.from_solution("git+https://example.com/x")


def test_from_solution_clone_to_on_local(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(GitOpsError):
        TrapLoader.from_solution(".", Path("dest"))


def test_from_solution_local_subpath(tmp_path, monkeypatch):
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "sub" / "trap.yaml", {"cmd": "x", "tasks": {"t": {"source": "../task"}}})
    monkeypatch.chdir(tmp_path)
    assert TrapLoader.from_solution("sub").config.cmd == "x"


def test_setup_cmd_forced_runs(tmp_path, monkeypatch):
    _write(
        tmp_path / "trap.yaml",
        {"cmd": "x", "setup_cmd": "touch ran.marker", "tasks": {"t": {"source": "../task"}}},
    )
    monkeypatch.chdir(tmp_path)
    TrapLoader.from_solution(None, setup=True)
    assert (tmp_path / "ran.marker").exists()


def test_select_and_resolve_task(tmp_path):
    loader = TrapLoader(
        _write(tmp_path / "trap.yaml", {"cmd": "x", "tasks": {"a": {"source": "p"}, "b": {"source": "q"}}})
    )
    assert loader.resolve_task(None).alias == "a"  # first
    assert loader.resolve_task("b").alias == "b"
    with pytest.raises(ConfigError):
        loader.resolve_task("z")


def test_resolve_task_no_tasks(tmp_path):
    # `tasks` is required by the schema, but an explicit empty map exercises the guard.
    loader = TrapLoader(_write(tmp_path / "trap.yaml", {"cmd": "x", "tasks": {}}))
    with pytest.raises(ConfigError):
        loader.resolve_task(None)


def test_traptask_discover_no_inputs(tmp_path):
    with pytest.raises(ConfigError):
        TraptaskLoader(tmp_path / "traptask.yaml")


def test_traptask_discover_empty_inputs(tmp_path):
    (tmp_path / "inputs").mkdir()
    with pytest.raises(ConfigError):
        TraptaskLoader(tmp_path / "traptask.yaml")


def test_traptask_discovers_cases(tmp_path):
    (tmp_path / "inputs" / "a").mkdir(parents=True)
    (tmp_path / "inputs" / "b").mkdir(parents=True)
    loader = TraptaskLoader(tmp_path / "traptask.yaml")
    assert {c.id for c in loader.traptask.cases} == {"a", "b"}


def test_traptask_invalid_config(tmp_path):
    _write(tmp_path / "traptask.yaml", {"cases": [{"description": "no id"}]})
    with pytest.raises(ConfigError):
        TraptaskLoader(tmp_path / "traptask.yaml")


def test_traptask_malformed_yaml(tmp_path):
    (tmp_path / "traptask.yaml").write_text("cases: [")
    with pytest.raises(ConfigError):
        TraptaskLoader(tmp_path / "traptask.yaml")


def test_cases_and_tags(tmp_path):
    _write(
        tmp_path / "traptask.yaml",
        {"cases": [{"id": "a", "tags": ["x"]}, {"id": "b"}, {"id": "c", "skip": True}]},
    )
    loader = TraptaskLoader(tmp_path / "traptask.yaml")
    assert {c.id for c in loader.cases} == {"a", "b"}  # skip excluded
    assert {c.id for c in loader.cases_with_tags(["x"])} == {"a"}
    assert {c.id for c in loader.cases_with_tags([])} == {"a", "b"}  # empty → all


def test_from_task_clone_to_on_local(tmp_path):
    with pytest.raises(GitOpsError):
        TraptaskLoader.from_task_binding(
            TaskBinding(alias="t", source="../task", clone_to=Path("x")), tmp_path
        )

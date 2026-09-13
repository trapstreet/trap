"""A task whose case inputs overlap its expected answers is refused before any case runs.

The manifest hands a solution its case's inputs as a resolved path, so a case directory
that is, holds, or sits inside the answers — through a symlink or through ``dirs`` —
would give every solution the answers. The tests build those layouts with real links."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import trap.runner.task as runner_task
from trap.cli import app
from trap.loader import ConfigError, TrapLoader, TraptaskLoader
from trap.runner import TaskRunner

from .conftest import JUDGE_SCORE

#: A solution that leaves ran.txt in its cwd (the solution dir) if it ever starts.
MARKS_THAT_IT_RAN = "sh -c 'touch ran.txt'"


def _squash(text: str) -> str:
    """``text`` without whitespace, so a match survives the terminal wrapping a long path."""
    return "".join(text.split())


def _relink(path: Path, target: str) -> None:
    """Replace the directory at ``path`` with a symlink to ``target`` (relative to its parent)."""
    shutil.rmtree(path)
    path.symlink_to(target)


def _solution_ran(sol: Path) -> bool:
    captures = [p for p in sol.rglob("stdout") if p.parent.name == "solution"]
    return (sol / "ran.txt").exists() or bool(captures)


def _assert_refused(res, sol: Path, case: str, inputs: Path, answers: Path) -> None:
    assert res.exit_code == 2, res.output
    out = _squash(res.output)
    assert _squash("every solution would be handed the answers") in out
    assert _squash(f"{case}: inputs {inputs.resolve()} overlap answers {answers.resolve()}") in out
    assert not _solution_ran(sol)


# --- tp run -------------------------------------------------------------------


def test_a_case_dir_linked_into_expected_is_refused_before_the_solution_runs(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"input.txt": "hi", "answer.txt": "secret"}})
    task = tmp_path / "task"
    _relink(task / "inputs" / "c1", "../expected/c1")
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, "c1", task / "expected" / "c1", task / "expected" / "c1")
    assert _squash(f"refusing to run task {task.resolve()}") in _squash(res.output)


def test_inputs_and_expected_linked_to_one_shared_case_folder_are_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"answer.txt": "secret"}})
    task = tmp_path / "task"
    shared = task / "cases" / "c1"
    shared.mkdir(parents=True)
    (shared / "question.txt").write_text("q")
    (shared / "answer.txt").write_text("secret")
    _relink(task / "inputs" / "c1", "../cases/c1")
    _relink(task / "expected" / "c1", "../cases/c1")
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, "c1", shared, shared)


def test_an_inputs_dir_linked_to_expected_is_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"answer.txt": "secret"}})
    task = tmp_path / "task"
    _relink(task / "inputs", "expected")
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, "c1", task / "expected" / "c1", task / "expected" / "c1")


def test_dirs_naming_one_directory_for_inputs_and_expected_are_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, inputs={"c1": {"question.txt": "q", "answer.txt": "secret"}})
    task = tmp_path / "task"
    config = json.loads((task / "traptask.yaml").read_text())
    config["dirs"] = {"inputs": "inputs/", "expected": "inputs/"}
    (task / "traptask.yaml").write_text(json.dumps(config))
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, "c1", task / "inputs" / "c1", task / "inputs" / "c1")


def test_every_offending_case_is_named(make_project, runner, tmp_path):
    sol = make_project(
        cmd=MARKS_THAT_IT_RAN,
        cases=["c1", "c2", "c3"],
        expected={c: {"answer.txt": "secret"} for c in ("c1", "c2", "c3")},
    )
    task = tmp_path / "task"
    _relink(task / "inputs" / "c1", "../expected/c1")
    _relink(task / "inputs" / "c3", "../expected/c3")
    res = runner.invoke(app, ["run", "--no-environment"])
    for case in ("c1", "c3"):
        _assert_refused(res, sol, case, task / "expected" / case, task / "expected" / case)
    assert _squash("c2: inputs") not in _squash(res.output)


def test_a_case_dir_linked_to_a_folder_without_the_answers_runs_and_scores(make_project, runner, tmp_path):
    sol = make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
    )
    task = tmp_path / "task"
    shared = task / "cases" / "c1"
    shared.mkdir(parents=True)
    (shared / "input.txt").write_text("hello")
    _relink(task / "inputs" / "c1", "../cases/c1")
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["cases_results"][0]["metrics"] == {"score": 1.0}
    assert _solution_ran(sol)


def test_only_the_cases_about_to_run_are_checked(make_project, runner, tmp_path):
    make_project(
        cmd="sh -c 'echo hi'",
        cases=["c1", "c2"],
        expected={"c2": {"answer.txt": "secret"}},
        skip=("c2",),
    )
    _relink(tmp_path / "task" / "inputs" / "c2", "../expected/c2")
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert [r["case_id"] for r in json.loads(res.stdout)["cases_results"]] == ["c1"]


# --- TaskRunner.run -------------------------------------------------------------


def _task_runner(tmp_path: Path) -> TaskRunner:
    tl = TrapLoader.from_solution(None)
    ttl = TraptaskLoader.from_task_binding(tl.resolve_task(None), tl.trap_dir)
    return TaskRunner(tl.config, tl.trap_dir, ttl.traptask_dir, ttl.traptask, tmp_path / "run", False)


def test_the_runner_refuses_the_whole_run_before_its_first_case(make_project, tmp_path):
    # c1 is clean and would run first; the run is refused as a whole because of c2.
    sol = make_project(
        cmd=MARKS_THAT_IT_RAN,
        cases=["c1", "c2"],
        expected={c: {"answer.txt": "secret"} for c in ("c1", "c2")},
    )
    task = tmp_path / "task"
    _relink(task / "inputs" / "c2", "../expected/c2")
    tr = _task_runner(tmp_path)
    with pytest.raises(ConfigError) as e:
        tr.run(iter(tr.traptask_config.cases))
    answers = (task / "expected" / "c2").resolve()
    assert f"refusing to run task {task.resolve()}" in str(e.value)
    assert f"  c2: inputs {answers} overlap answers {answers}" in str(e.value)
    assert not _solution_ran(sol)
    assert not (tmp_path / "run").exists()


def test_the_runner_runs_every_case_it_is_handed_as_an_iterator(make_project, tmp_path):
    make_project(cmd="sh -c 'echo hi'", cases=["c1", "c2"])
    tr = _task_runner(tmp_path)
    results, _, _ = tr.run(iter(tr.traptask_config.cases))
    assert [r.case_id for r in results] == ["c1", "c2"]


# --- answer_overlaps ------------------------------------------------------------


def test_inputs_inside_the_expected_root_overlap_even_away_from_their_own_answers(tmp_path):
    expected = tmp_path / "expected"
    (expected / "c2").mkdir(parents=True)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "c1").symlink_to("../expected/c2")
    [overlap] = runner_task.answer_overlaps(inputs, expected, ["c1"])
    assert (overlap.case_id, overlap.inputs, overlap.answers) == (
        "c1",
        (expected / "c2").resolve(),
        expected.resolve(),
    )


def test_inputs_holding_the_expected_root_overlap(tmp_path):
    (tmp_path / "expected" / "c1").mkdir(parents=True)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "c1").symlink_to("..")
    [overlap] = runner_task.answer_overlaps(tmp_path / "inputs", tmp_path / "expected", ["c1"])
    assert overlap.inputs == tmp_path.resolve()


def test_overlap_is_decided_by_path_components_not_string_prefixes(tmp_path):
    # The expected root <tmp>/x/a is a string prefix of the inputs <tmp>/x/ab/c1, not a parent.
    (tmp_path / "x" / "a" / "c1").mkdir(parents=True)
    (tmp_path / "x" / "ab" / "c1").mkdir(parents=True)
    assert runner_task.answer_overlaps(tmp_path / "x" / "ab", tmp_path / "x" / "a", ["c1"]) == ()


def test_a_missing_expected_dir_is_no_overlap(tmp_path):
    (tmp_path / "inputs" / "c1").mkdir(parents=True)
    assert runner_task.answer_overlaps(tmp_path / "inputs", tmp_path / "expected", ["c1"]) == ()

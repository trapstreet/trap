"""A task whose case inputs overlap its expected answers is refused before any case runs.

The manifest hands a solution its case's inputs as a resolved path, so a case directory
that is, holds, or sits inside the answers — through a symlink, through ``dirs``, or
through another spelling of the same directory — would give every solution the answers.
The tests build those layouts with real links."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import trap.runner.task as runner_task
from trap.cli import app
from trap.loader import ConfigError, TrapLoader, TraptaskLoader
from trap.runner import TaskRunner

from .conftest import JUDGE_SCORE, unlock

#: A solution that leaves ran.txt in its cwd (the solution dir) if it ever starts.
MARKS_THAT_IT_RAN = "sh -c 'touch ran.txt'"
OWN = "its own answers"
ROOT = "the expected root"


def _squash(text: str) -> str:
    """``text`` without whitespace, so a match survives the terminal wrapping a long path."""
    return "".join(text.split())


def _relink(path: Path, target: str) -> None:
    """Replace the directory at ``path`` with a symlink to ``target`` (relative to its parent)."""
    shutil.rmtree(path)
    path.symlink_to(target)


def _set_dirs(task: Path, *, inputs: str, expected: str) -> None:
    """Point the task's traptask.yaml ``dirs`` at ``inputs`` and ``expected``."""
    config = json.loads((task / "traptask.yaml").read_text())
    config["dirs"] = {"inputs": inputs, "expected": expected}
    (task / "traptask.yaml").write_text(json.dumps(config))


def _solution_ran(sol: Path) -> bool:
    captures = [p for p in sol.rglob("stdout") if p.parent.name == "solution"]
    return (sol / "ran.txt").exists() or bool(captures)


def _case_insensitive(path: Path) -> bool:
    """Whether the filesystem holding ``path`` takes two letter cases for one name."""
    probe = path / "case-probe"
    probe.touch()
    try:
        return (path / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def _assert_refused(res, sol: Path, case: str, inputs: Path, relation: str, answers: Path) -> None:
    assert res.exit_code == 2, res.output
    out = _squash(res.output)
    assert _squash("every solution would be handed the answers") in out
    assert _squash(f"{case}: inputs {inputs} {relation} {answers}") in out
    assert not _solution_ran(sol)


def _linked_into_expected(make_project, tmp_path: Path) -> tuple[Path, Path]:
    """A project whose case c1 is a link to its own answers; returns (solution, answers)."""
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"input.txt": "hi", "answer.txt": "secret"}})
    _relink(tmp_path / "task" / "inputs" / "c1", "../expected/c1")
    return sol, (tmp_path / "task" / "expected" / "c1").resolve()


# --- tp run: refused ------------------------------------------------------------


def test_a_case_dir_linked_into_expected_is_refused_before_the_solution_runs(make_project, runner, tmp_path):
    sol, answers = _linked_into_expected(make_project, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, "c1", answers, f"are {OWN}", answers)
    assert _squash(f"refusing to run task {(tmp_path / 'task').resolve()}") in _squash(res.output)


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
    _assert_refused(res, sol, "c1", shared.resolve(), f"are {OWN}", shared.resolve())


def test_an_inputs_dir_linked_to_expected_is_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"answer.txt": "secret"}})
    task = tmp_path / "task"
    _relink(task / "inputs", "expected")
    res = runner.invoke(app, ["run", "--no-environment"])
    answers = (task / "expected" / "c1").resolve()
    _assert_refused(res, sol, "c1", answers, f"are {OWN}", answers)


def test_dirs_naming_one_directory_for_inputs_and_expected_are_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, inputs={"c1": {"question.txt": "q", "answer.txt": "secret"}})
    task = tmp_path / "task"
    _set_dirs(task, inputs="inputs/", expected="inputs/")
    res = runner.invoke(app, ["run", "--no-environment"])
    answers = (task / "inputs" / "c1").resolve()
    _assert_refused(res, sol, "c1", answers, f"are {OWN}", answers)


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
        answers = (task / "expected" / case).resolve()
        _assert_refused(res, sol, case, answers, f"are {OWN}", answers)
    assert _squash("c2: inputs") not in _squash(res.output)


def test_a_link_spelling_expected_in_another_letter_case_is_refused(make_project, runner, tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("the tmp filesystem tells letter cases apart")
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"answer.txt": "secret"}})
    task = tmp_path / "task"
    _relink(task / "inputs" / "c1", "../Expected/c1")
    res = runner.invoke(app, ["run", "--no-environment"])
    inputs = (task / "Expected" / "c1").resolve()
    answers = (task / "expected" / "c1").resolve()
    assert inputs != answers  # two spellings of one directory
    _assert_refused(res, sol, "c1", inputs, f"are {OWN}", answers)


def test_dirs_naming_one_directory_in_two_letter_cases_are_refused(make_project, runner, tmp_path):
    if not _case_insensitive(tmp_path):
        pytest.skip("the tmp filesystem tells letter cases apart")
    sol = make_project(cmd=MARKS_THAT_IT_RAN)
    task = tmp_path / "task"
    (task / "inputs").rename(task / "data")
    (task / "data" / "c1" / "answer.txt").write_text("secret")
    _set_dirs(task, inputs="Data/", expected="data/")
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(
        res, sol, "c1", (task / "Data" / "c1").resolve(), f"are {OWN}", (task / "data" / "c1").resolve()
    )


def test_a_refused_run_opens_nothing_on_the_site(make_project, runner, tmp_path, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: started.append("live session"))
    monkeypatch.setattr("trap.cli.start_site_grading", lambda **_kwargs: started.append("site grading"))
    sol, answers = _linked_into_expected(make_project, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert started == [], "the run reached the site before it was refused"
    _assert_refused(res, sol, "c1", answers, f"are {OWN}", answers)


def test_a_refused_run_asks_nothing_first(make_project, runner, tmp_path, monkeypatch):
    asked: list[object] = []
    monkeypatch.setattr("trap.cli._confirm_unanchored", lambda provenance, **_kw: asked.append(provenance))
    sol, answers = _linked_into_expected(make_project, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert asked == [], "the unanchored-provenance prompt came before the refusal"
    _assert_refused(res, sol, "c1", answers, f"are {OWN}", answers)


def test_a_case_id_that_reads_as_markup_is_printed_as_written(make_project, runner, tmp_path):
    # A case id is the task author's text; "[/x]" would otherwise be a closing markup tag.
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c[/x]"], expected={"c[/x]": {"answer.txt": "secret"}})
    task = tmp_path / "task"
    _relink(task / "inputs" / "c[", "../expected/c[")
    res = runner.invoke(app, ["run", "--no-environment"])
    assert "Traceback" not in res.output
    answers = (task / "expected" / "c[" / "x]").resolve()
    _assert_refused(res, sol, "c[/x]", answers, f"are {OWN}", answers)


def _answers_inside_the_inputs_root(make_project, tmp_path: Path) -> tuple[Path, Path]:
    """A task whose expected root cases/_answers/ sits inside its inputs root cases/;
    returns (solution, task)."""
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c1", "c2"])
    task = tmp_path / "task"
    (task / "inputs").rename(task / "cases")
    for case in ("c1", "c2"):
        (task / "cases" / "_answers" / case).mkdir(parents=True)
        (task / "cases" / "_answers" / case / "answer.txt").write_text(f"secret-{case}")
    _set_dirs(task, inputs="cases/", expected="cases/_answers/")
    return sol, task


def test_a_case_linked_into_answers_inside_the_inputs_root_is_refused(make_project, runner, tmp_path):
    sol, task = _answers_inside_the_inputs_root(make_project, tmp_path)
    _relink(task / "cases" / "c1", "_answers/c2")
    res = runner.invoke(app, ["run", "--no-environment"])
    held = (task / "cases" / "_answers" / "c2").resolve()
    _assert_refused(res, sol, "c1", held, "are the answers of case 'c2'", held)
    assert _squash("c2: inputs") not in _squash(res.output)


def test_a_case_linked_into_an_expected_root_inside_the_inputs_root_is_refused(
    make_project, runner, tmp_path
):
    # The expected root sits inside the inputs root, so a case dir inside it is under the
    # inputs root too; that must not excuse it. (_answers/extra is no case's answers.)
    sol, task = _answers_inside_the_inputs_root(make_project, tmp_path)
    (task / "cases" / "_answers" / "extra").mkdir()
    _relink(task / "cases" / "c1", "_answers/extra")
    res = runner.invoke(app, ["run", "--no-environment"])
    expected = (task / "cases" / "_answers").resolve()
    _assert_refused(res, sol, "c1", expected / "extra", f"sit inside {ROOT}", expected)


def test_a_case_linked_into_answers_with_inputs_at_the_task_root_is_refused(make_project, runner, tmp_path):
    sol = make_project(
        cmd=MARKS_THAT_IT_RAN,
        cases=["c1", "c2"],
        expected={c: {"answer.txt": f"secret-{c}"} for c in ("c1", "c2")},
    )
    task = tmp_path / "task"
    (task / "inputs" / "c2").rename(task / "c2")
    shutil.rmtree(task / "inputs")
    (task / "c1").symlink_to("expected/c2")
    _set_dirs(task, inputs="./", expected="expected/")
    res = runner.invoke(app, ["run", "--no-environment"])
    held = (task / "expected" / "c2").resolve()
    _assert_refused(res, sol, "c1", held, "are the answers of case 'c2'", held)
    assert _squash("c2: inputs") not in _squash(res.output)


# --- tp run: one case's inputs against another case's answers -----------------------


def _answers(directory: Path, case: str) -> None:
    """Put ``case``'s answer file in ``directory`` (its answers dir, wherever that falls)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "answer.txt").write_text(f"secret-{case}")


def test_a_case_whose_inputs_are_another_case_s_answers_by_its_id_is_refused(make_project, runner, tmp_path):
    # expected "./" makes case "inputs/c2"'s answers dir ./inputs/c2 -- which is case c2's inputs.
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c2", "inputs/c2"])
    task = tmp_path / "task"
    _answers(task / "c2", "c2")
    _answers(task / "inputs" / "c2", "inputs/c2")
    _set_dirs(task, inputs="inputs/", expected="./")
    res = runner.invoke(app, ["run", "--no-environment"])
    held = (task / "inputs" / "c2").resolve()
    _assert_refused(res, sol, "c2", held, "are the answers of case 'inputs/c2'", held)
    assert _squash("inputs/c2: inputs") not in _squash(res.output)


def test_the_same_by_id_under_a_nested_inputs_dir_is_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c2", "in/c2"])
    task = tmp_path / "task"
    (task / "data").mkdir()
    (task / "inputs").rename(task / "data" / "in")
    _answers(task / "data" / "c2", "c2")
    _answers(task / "data" / "in" / "c2", "in/c2")
    _set_dirs(task, inputs="data/in/", expected="data/")
    res = runner.invoke(app, ["run", "--no-environment"])
    held = (task / "data" / "in" / "c2").resolve()
    _assert_refused(res, sol, "c2", held, "are the answers of case 'in/c2'", held)


def test_a_case_whose_inputs_hold_another_case_s_answers_by_its_id_is_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c2", "inputs/c2/key"])
    task = tmp_path / "task"
    _answers(task / "c2", "c2")
    _answers(task / "inputs" / "c2" / "key", "inputs/c2/key")
    _set_dirs(task, inputs="inputs/", expected="./")
    res = runner.invoke(app, ["run", "--no-environment"])
    inputs = (task / "inputs" / "c2").resolve()
    _assert_refused(res, sol, "c2", inputs, "contain the answers of case 'inputs/c2/key'", inputs / "key")


def test_inputs_inside_the_answers_of_a_case_not_selected_to_run_are_refused(make_project, runner, tmp_path):
    # The inputs root links into c2's answers dir; only c1 is selected, but c2's answers
    # are on disk all the same.
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c1", "c2"], tags={"c1": ["smoke"]})
    task = tmp_path / "task"
    _answers(task / "data" / "c1", "c1")
    _answers(task / "data" / "c2", "c2")
    for case in ("c1", "c2"):
        (task / "inputs" / case).rename(task / "data" / "c2" / case)
    (task / "inputs").rmdir()
    (task / "data" / "in").symlink_to("c2")
    _set_dirs(task, inputs="data/in/", expected="data/")
    res = runner.invoke(app, ["run", "-t", "smoke", "--no-environment"])
    held = (task / "data" / "c2").resolve()
    _assert_refused(res, sol, "c1", held / "c1", "sit inside the answers of case 'c2'", held)


def test_an_answers_dir_linked_to_another_case_s_inputs_is_refused(make_project, runner, tmp_path):
    sol = make_project(
        cmd=MARKS_THAT_IT_RAN, cases=["c1", "c2"], expected={"c1": {"answer.txt": "secret-c1"}}
    )
    task = tmp_path / "task"
    (task / "expected" / "c2").symlink_to("../inputs/c1")
    res = runner.invoke(app, ["run", "--no-environment"])
    inputs = (task / "inputs" / "c1").resolve()
    _assert_refused(res, sol, "c1", inputs, "are the answers of case 'c2'", inputs)
    assert _squash("c2: inputs") not in _squash(res.output)


# --- tp run: not refused ----------------------------------------------------------


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


def test_answers_at_the_task_root_beside_inputs_run_and_score(make_project, runner, tmp_path):
    # dirs.expected "./" puts the expected root around inputs/; a case dir under inputs/
    # sits inside it without holding any answers.
    make_project(
        cmd="sh -c 'cat'", stdin="input.txt", inputs={"c1": {"input.txt": "hello"}}, judge_src=JUDGE_SCORE
    )
    task = tmp_path / "task"
    (task / "c1").mkdir()
    (task / "c1" / "answer.txt").write_text("hello")
    _set_dirs(task, inputs="inputs/", expected="./")
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["cases_results"][0]["metrics"] == {"score": 1.0}


def test_a_clean_task_with_its_inputs_at_the_task_root_runs_and_scores(make_project, runner, tmp_path):
    # dirs.inputs "./" puts the inputs root around expected/; a case dir beside expected/
    # holds no answers.
    make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
    )
    task = tmp_path / "task"
    (task / "inputs" / "c1").rename(task / "c1")
    (task / "inputs").rmdir()
    _set_dirs(task, inputs="./", expected="expected/")
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["cases_results"][0]["metrics"] == {"score": 1.0}


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
    assert f"  c2: inputs {answers} are {OWN} {answers}" in str(e.value)
    assert not _solution_ran(sol)
    assert not (tmp_path / "run").exists()


def test_the_runner_runs_every_case_it_is_handed_as_an_iterator(make_project, tmp_path):
    make_project(cmd="sh -c 'echo hi'", cases=["c1", "c2"])
    tr = _task_runner(tmp_path)
    results, _, _ = tr.run(iter(tr.traptask_config.cases))
    assert [r.case_id for r in results] == ["c1", "c2"]


def test_the_error_the_runner_raises_is_the_one_the_loader_exports():
    from trap import errors
    from trap.loader import errors as loader_errors

    assert errors.ConfigError is loader_errors.ConfigError is ConfigError


# --- answer_overlaps ------------------------------------------------------------


def _relation(overlap: runner_task.AnswerOverlap) -> tuple[Path, str, str, Path]:
    return overlap.inputs, overlap.relation, overlap.target, overlap.answers


def test_inputs_inside_the_expected_root_overlap_even_away_from_their_own_answers(tmp_path):
    expected = tmp_path / "expected"
    (expected / "c2").mkdir(parents=True)
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "c1").symlink_to("../expected/c2")
    [overlap] = runner_task.answer_overlaps(inputs, expected, ["c1"])
    assert overlap.case_id == "c1"
    assert _relation(overlap) == ((expected / "c2").resolve(), "sit inside", ROOT, expected.resolve())


def test_inputs_holding_the_expected_root_overlap_it(tmp_path):
    # c1's own answers live outside the task, so only the expected root is inside the inputs.
    task = tmp_path / "task"
    (task / "expected").mkdir(parents=True)
    (tmp_path / "elsewhere" / "c1").mkdir(parents=True)
    (task / "expected" / "c1").symlink_to(tmp_path / "elsewhere" / "c1")
    (task / "inputs").mkdir()
    (task / "inputs" / "c1").symlink_to("..")
    [overlap] = runner_task.answer_overlaps(task / "inputs", task / "expected", ["c1"])
    assert _relation(overlap) == (task.resolve(), "contain", ROOT, (task / "expected").resolve())


def test_inputs_that_are_the_expected_root_overlap_it(tmp_path):
    # c1's own answers live outside the task, so the inputs match only the expected root.
    task = tmp_path / "task"
    (task / "expected").mkdir(parents=True)
    (tmp_path / "elsewhere" / "c1").mkdir(parents=True)
    (task / "expected" / "c1").symlink_to(tmp_path / "elsewhere" / "c1")
    (task / "inputs").mkdir()
    (task / "inputs" / "c1").symlink_to("../expected")
    [overlap] = runner_task.answer_overlaps(task / "inputs", task / "expected", ["c1"])
    expected = (task / "expected").resolve()
    assert _relation(overlap) == (expected, "are", ROOT, expected)


def test_inputs_holding_their_own_answers_overlap_them(tmp_path):
    (tmp_path / "expected" / "c1").mkdir(parents=True)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "c1").symlink_to("..")
    [overlap] = runner_task.answer_overlaps(tmp_path / "inputs", tmp_path / "expected", ["c1"])
    answers = (tmp_path / "expected" / "c1").resolve()
    assert _relation(overlap) == (tmp_path.resolve(), "contain", OWN, answers)


def test_inputs_inside_their_own_answers_overlap_them(tmp_path):
    shared = tmp_path / "cases" / "c1"
    (shared / "question").mkdir(parents=True)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "expected").mkdir()
    (tmp_path / "inputs" / "c1").symlink_to("../cases/c1/question")
    (tmp_path / "expected" / "c1").symlink_to("../cases/c1")
    [overlap] = runner_task.answer_overlaps(tmp_path / "inputs", tmp_path / "expected", ["c1"])
    assert _relation(overlap) == ((shared / "question").resolve(), "sit inside", OWN, shared.resolve())


def test_overlap_is_decided_by_path_components_not_string_prefixes(tmp_path):
    # The expected root <tmp>/x/a is a string prefix of the inputs <tmp>/x/ab/c1, not a parent.
    (tmp_path / "x" / "a" / "c1").mkdir(parents=True)
    (tmp_path / "x" / "ab" / "c1").mkdir(parents=True)
    assert runner_task.answer_overlaps(tmp_path / "x" / "ab", tmp_path / "x" / "a", ["c1"]) == ()


def test_a_missing_expected_dir_is_no_overlap(tmp_path):
    (tmp_path / "inputs" / "c1").mkdir(parents=True)
    assert runner_task.answer_overlaps(tmp_path / "inputs", tmp_path / "expected", ["c1"]) == ()


def test_a_case_link_that_loops_is_no_overlap(tmp_path):
    (tmp_path / "expected" / "c1").mkdir(parents=True)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "c1").symlink_to("c1")
    assert runner_task.answer_overlaps(tmp_path / "inputs", tmp_path / "expected", ["c1"]) == ()


def test_an_unreadable_expected_dir_is_still_compared_by_path(tmp_path):
    expected = tmp_path / "expected"
    for case in ("c1", "c2"):
        (expected / case).mkdir(parents=True)
        (tmp_path / "inputs" / case).mkdir(parents=True)
    _relink(tmp_path / "inputs" / "c1", "../expected/c1")
    expected.chmod(0o000)
    try:
        overlaps = runner_task.answer_overlaps(tmp_path / "inputs", expected, ["c1", "c2"])
    finally:
        unlock(tmp_path)
    assert [o.case_id for o in overlaps] == ["c1"]


def test_unreadable_dirs_on_both_sides_match_nothing(tmp_path):
    # Neither side can be stat'ed, so neither has an identity; two unknowns are not one dir.
    (tmp_path / "inputs").mkdir()
    (tmp_path / "expected").mkdir()
    (tmp_path / "inputs" / "c1").symlink_to("c1")
    (tmp_path / "expected" / "c2").symlink_to("c2")
    overlaps = runner_task.answer_overlaps(
        tmp_path / "inputs", tmp_path / "expected", ["c1"], defined=["c1", "c2", "c3"]
    )
    assert overlaps == ()


def test_the_first_case_in_task_order_is_named_when_several_answers_are_held(tmp_path):
    shared = tmp_path / "shared"
    for case in ("c2", "c3"):
        (shared / case).mkdir(parents=True)
    (tmp_path / "inputs").mkdir()
    (tmp_path / "expected").mkdir()
    (tmp_path / "inputs" / "c1").symlink_to("../shared")
    for case in ("c3", "c2"):
        (tmp_path / "expected" / case).symlink_to(f"../shared/{case}")
    [overlap] = runner_task.answer_overlaps(
        tmp_path / "inputs", tmp_path / "expected", ["c1"], defined=["c1", "c3", "c2"]
    )
    assert _relation(overlap) == (
        shared.resolve(),
        "contain",
        "the answers of case 'c3'",
        (shared / "c3").resolve(),
    )

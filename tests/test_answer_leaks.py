"""A task that could hand a solution the answers is refused before any case runs.

A solution is handed its case's inputs directory. Nothing in it may be a symlink, from the
inputs root down; no answers directory may lie inside it or around it; and case ids stay
inside their directories. The tests build those layouts with real links."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from trap.cli import app
from trap.loader import ConfigError, TrapLoader, TraptaskLoader
from trap.models import DirsConfig, TraptaskCase, TraptaskConfig
from trap.runner import TaskRunner, refuse_answer_leaks

from .conftest import JUDGE_SCORE, unlock

#: A solution that leaves ran.txt in its cwd (the solution dir) if it ever starts.
MARKS_THAT_IT_RAN = "sh -c 'touch ran.txt'"
HEADER = "these cases' inputs could hand every solution the answers"


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


def _assert_refused(res, sol: Path, *lines: str) -> None:
    assert res.exit_code == 2, res.output
    out = _squash(res.output)
    assert _squash(HEADER) in out
    for line in lines:
        assert _squash(line) in out, line
    assert not _solution_ran(sol)


def _task(tmp_path: Path, cases: tuple[str, ...] = ("c1",)) -> Path:
    """A clean task: inputs/<id>/question.txt and expected/<id>/answer.txt for each case."""
    task = (tmp_path / "task").resolve()
    for case in cases:
        (task / "inputs" / case).mkdir(parents=True)
        (task / "inputs" / case / "question.txt").write_text("q")
        (task / "expected" / case).mkdir(parents=True)
        (task / "expected" / case / "answer.txt").write_text(f"secret-{case}")
    return task


def _refusal(
    task: Path, cases: list[str], *, run: list[str] | None = None, inputs="inputs/", expected="expected/"
) -> str:
    """What trap says refusing ``task`` when it runs ``run`` (every case by default) — ""
    when it lets the task run."""
    config = TraptaskConfig(
        cases=tuple(TraptaskCase(id=c) for c in cases), dirs=DirsConfig(inputs=inputs, expected=expected)
    )
    try:
        refuse_answer_leaks(task, config, [c for c in config.cases if run is None or c.id in run])
    except ConfigError as e:
        return str(e)
    return ""


# --- tp run: refused, before the solution runs --------------------------------------


def _link_to_the_answer(make_project, tmp_path: Path) -> tuple[Path, Path]:
    """A project whose case c1 holds a link to its answer; returns (solution, task)."""
    sol = make_project(cmd=MARKS_THAT_IT_RAN, expected={"c1": {"answer.txt": "secret"}})
    task = (tmp_path / "task").resolve()
    (task / "inputs" / "c1" / "reference.txt").symlink_to("../../expected/c1/answer.txt")
    return sol, task


def test_a_link_inside_a_case_is_refused(make_project, runner, tmp_path):
    sol, task = _link_to_the_answer(make_project, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    link = task / "inputs" / "c1" / "reference.txt"
    _assert_refused(res, sol, f"refusing to run task {task}", f"c1: inputs {link} is a symlink")
    assert "secret" not in res.output


def test_answers_linked_to_a_case_s_inputs_are_refused_even_for_a_skipped_case(
    make_project, runner, tmp_path
):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c1", "c2"], skip=("c2",))
    task = (tmp_path / "task").resolve()
    (task / "expected").mkdir()
    (task / "expected" / "c2").symlink_to("../inputs/c1")
    res = runner.invoke(app, ["run", "--no-environment"])
    inputs = task / "inputs" / "c1"
    _assert_refused(res, sol, f"c1: inputs {inputs} hold the answers of case 'c2' {inputs}")


def test_answers_around_a_case_s_inputs_are_refused(make_project, runner, tmp_path):
    sol = make_project(cmd=MARKS_THAT_IT_RAN)
    task = (tmp_path / "task").resolve()
    (task / "expected").mkdir()
    (task / "expected" / "c1").symlink_to("../inputs")
    res = runner.invoke(app, ["run", "--no-environment"])
    inputs = task / "inputs"
    _assert_refused(res, sol, f"c1: inputs {inputs / 'c1'} sit inside the answers of case 'c1' {inputs}")


def test_a_refused_run_asks_nothing_and_opens_nothing_on_the_site(
    make_project, runner, tmp_path, monkeypatch
):
    called: list[str] = []
    monkeypatch.setattr("trap.cli._confirm_unanchored", lambda provenance, **_kw: called.append("prompt"))
    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kw: called.append("live session"))
    monkeypatch.setattr("trap.cli.start_site_grading", lambda **_kw: called.append("site grading"))
    sol, _ = _link_to_the_answer(make_project, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert called == [], "the run prompted, or reached the site, before it was refused"
    _assert_refused(res, sol)


def test_a_case_id_that_reads_as_markup_is_printed_as_written(make_project, runner, tmp_path):
    # A case id is the task author's text; "[/x]" would otherwise be a closing markup tag.
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c[/x]"], expected={"c[/x]": {"answer.txt": "secret"}})
    task = (tmp_path / "task").resolve()
    _relink(task / "inputs" / "c[", "../expected/c[")
    res = runner.invoke(app, ["run", "--no-environment"])
    assert "Traceback" not in res.output
    _assert_refused(res, sol, f"c[/x]: inputs {task / 'inputs' / 'c['} is a symlink")


# --- tp run: layouts that hand over no answers run ------------------------------------


def _answers_at_the_task_root(task: Path) -> None:
    (task / "expected" / "c1").rename(task / "c1")
    config = json.loads((task / "traptask.yaml").read_text())
    (task / "traptask.yaml").write_text(json.dumps({**config, "dirs": {"expected": "./"}}))


def _inputs_stored_elsewhere(task: Path) -> None:
    (task / "inputs").rename(task.parent / "stored")
    (task / "inputs").symlink_to(task.parent / "stored")


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(lambda task: None, id="the default layout"),
        pytest.param(_answers_at_the_task_root, id="answers at the task root"),
        pytest.param(_inputs_stored_elsewhere, id="an inputs root linked to a clean folder"),
    ],
)
def test_a_task_that_hands_over_no_answers_runs_and_scores(
    make_project, runner, tmp_path, layout: Callable[[Path], None]
):
    make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
    )
    layout(tmp_path / "task")
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["cases_results"][0]["metrics"] == {"score": 1.0}


# --- no symlink in what a solution is handed ------------------------------------------


@pytest.mark.parametrize(
    ("make_link", "link"),
    [
        pytest.param(lambda t: (t / "inputs/c1/data").symlink_to("../../expected/c1"), "c1/data", id="a dir"),
        pytest.param(
            lambda t: (t / "inputs/c1/gone.txt").symlink_to("nowhere"), "c1/gone.txt", id="dangling"
        ),
        pytest.param(lambda t: (t / "inputs/c1/alias").symlink_to("question.txt"), "c1/alias", id="in-case"),
        pytest.param(lambda t: _relink(t / "inputs/c1", "../expected/c1"), "c1", id="the case dir"),
        pytest.param(
            lambda t: (t / "cases").mkdir() or _relink(t / "inputs/c1", "../cases"),
            "c1",
            id="to a clean folder",
        ),
    ],
)
def test_a_symlink_in_a_case_s_inputs_is_refused(tmp_path, make_link, link: str):
    task = _task(tmp_path)
    make_link(task)
    assert f"\n  c1: inputs {task / 'inputs' / link} is a symlink" in _refusal(task, ["c1"])


def test_a_case_reached_through_a_linked_directory_is_refused(tmp_path):
    task = _task(tmp_path, ("c1",))
    (task / "inputs").rename(task / "stored")
    (task / "inputs").mkdir()
    (task / "inputs" / "grp").symlink_to("../stored")
    assert f"grp/c1: inputs {task / 'inputs' / 'grp'} is a symlink" in _refusal(task, ["grp/c1"])


def test_a_directory_the_check_cannot_read_is_refused(tmp_path):
    task = _task(tmp_path)
    (task / "inputs" / "c1" / "locked").mkdir(mode=0o000)
    try:
        message = _refusal(task, ["c1"])
    finally:
        unlock(tmp_path)
    assert f"c1: inputs {task / 'inputs' / 'c1' / 'locked'} cannot be read" in message


def test_only_the_cases_about_to_run_are_walked(tmp_path):
    task = _task(tmp_path, ("c1", "c2"))
    (task / "inputs" / "c2" / "reference.txt").symlink_to("../../expected/c2/answer.txt")
    assert _refusal(task, ["c1", "c2"], run=["c1"]) == ""


# --- no answers inside what a solution is handed --------------------------------------


@pytest.mark.parametrize("spelling", ["inputs/", "Inputs/"])
def test_dirs_naming_one_directory_are_refused(tmp_path, spelling: str):
    task = _task(tmp_path)
    if not (task / spelling).exists():
        pytest.skip("the tmp filesystem tells letter cases apart")
    message = _refusal(task, ["c1"], inputs=spelling, expected="inputs/")
    assert (
        f"c1: inputs {task / spelling / 'c1'} hold the answers of case 'c1' {task / 'inputs' / 'c1'}"
        in message
    )


def test_ids_that_make_one_case_s_inputs_another_s_answers_are_refused(tmp_path):
    # With the answers at the task root, case "inputs/c2" keeps its answers in inputs/c2.
    task = _task(tmp_path, ("c2",))
    shutil.rmtree(task / "expected")
    inputs = task / "inputs" / "c2"
    message = _refusal(task, ["c2", "inputs/c2"], expected="./")
    assert f"c2: inputs {inputs} hold the answers of case 'inputs/c2' {inputs}" in message
    assert "inputs/c2: inputs" not in message


def test_an_expected_root_inside_a_case_s_inputs_is_refused(tmp_path):
    task = _task(tmp_path)
    (task / "inputs" / "c1" / "answers").mkdir()
    message = _refusal(task, ["c1"], expected="inputs/c1/answers/")
    answers = task / "inputs" / "c1" / "answers"
    assert f"c1: inputs {task / 'inputs' / 'c1'} hold the expected root {answers}" in message


def test_the_first_case_in_task_order_is_named_when_one_case_holds_several_answers(tmp_path):
    task = _task(tmp_path, ("c1", "c2", "c3"))
    for case in ("c2", "c3"):
        _relink(task / "expected" / case, f"../inputs/c1/{case}")
        (task / "inputs" / "c1" / case).mkdir()
    message = _refusal(task, ["c1", "c3", "c2"])
    assert "c1: inputs" in message and "hold the answers of case 'c3'" in message
    assert "case 'c2'" not in message


# --- no answers directory around a case's inputs; ids stay inside ---------------------


def test_a_case_inside_another_case_s_answers_is_refused(tmp_path):
    # The inputs root is a link into c2's answers; only c1 runs.
    task = _task(tmp_path, ("c1", "c2"))
    (task / "inputs").rename(task / "expected" / "c2" / "in")
    (task / "inputs").symlink_to("expected/c2/in")
    message = _refusal(task, ["c1", "c2"], run=["c1"])
    answers = task / "expected" / "c2"
    assert f"c1: inputs {answers / 'in' / 'c1'} sit inside the answers of case 'c2' {answers}" in message


@pytest.mark.parametrize("case", ["../x", "/x", ".", "a/../c1"])
def test_an_id_that_does_not_name_a_directory_inside_the_roots_is_refused(tmp_path, case: str):
    # A ".." that stays inside is refused too: resolving it can leave the directory walked.
    task = _task(tmp_path)
    line = f"{case}: id does not name a directory inside {task / 'inputs'} and {task / 'expected'}"
    assert line in _refusal(task, ["c1", case])


def test_every_offending_case_is_named_up_to_a_cap(tmp_path):
    cases = tuple(f"c{n}" for n in range(1, 9))
    task = _task(tmp_path, cases)
    for case in cases[1:]:
        (task / "inputs" / case / "alias").symlink_to("question.txt")
    lines = _refusal(task, list(cases)).splitlines()[1:]
    assert [line.split(":")[0] for line in lines[:-1]] == ["  c2", "  c3", "  c4", "  c5", "  c6"]
    assert lines[-1] == "  and 2 more"


# --- layouts that hand over no answers run --------------------------------------------


def _answers_in_the_inputs_root(task: Path) -> dict[str, str]:
    (task / "expected").rename(task / "inputs" / "_answers")
    return {"expected": "inputs/_answers/"}


def _inputs_at_the_task_root(task: Path) -> dict[str, str]:
    (task / "inputs" / "c1").rename(task / "c1")
    return {"inputs": "./"}


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(lambda task: shutil.rmtree(task / "expected") or {}, id="no expected dir"),
        pytest.param(lambda task: shutil.rmtree(task / "inputs" / "c1") or {}, id="a case without inputs"),
        pytest.param(_answers_in_the_inputs_root, id="answers beside the cases in the inputs root"),
        pytest.param(_inputs_at_the_task_root, id="inputs at the task root"),
    ],
)
def test_a_layout_that_hands_over_no_answers_runs(tmp_path, layout: Callable[[Path], dict[str, str]]):
    task = _task(tmp_path)
    assert _refusal(task, ["c1"], **layout(task)) == ""


# --- TaskRunner.run -------------------------------------------------------------------


def _task_runner(tmp_path: Path) -> TaskRunner:
    tl = TrapLoader.from_solution(None)
    ttl = TraptaskLoader.from_task_binding(tl.resolve_task(None), tl.trap_dir)
    return TaskRunner(tl.config, tl.trap_dir, ttl.traptask_dir, ttl.traptask, tmp_path / "run", False)


def test_the_runner_refuses_the_whole_run_before_its_first_case(make_project, tmp_path):
    # c1 is clean and would run first; the run is refused as a whole because of c2.
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c1", "c2"])
    task = (tmp_path / "task").resolve()
    (task / "inputs" / "c2" / "alias").symlink_to("input.txt")
    tr = _task_runner(tmp_path)
    with pytest.raises(ConfigError) as e:
        tr.run(iter(tr.traptask_config.cases))
    assert f"refusing to run task {task}" in str(e.value)
    assert f"  c2: inputs {task / 'inputs' / 'c2' / 'alias'} is a symlink" in str(e.value)
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

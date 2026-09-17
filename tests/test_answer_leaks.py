"""A task that could hand a solution the answers is refused before any case runs.

A solution is handed its case's inputs directory. No directory on the way to it from the
inputs root may be a symlink, and a link inside it must be a link to a regular file under
the inputs root, outside every answers directory; no answers directory may lie inside it,
and no case's answers directory around it; and case ids stay inside their directories. The
tests build those layouts with real links."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import unicodedata
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from trap.cli import app
from trap.loader import ConfigError, TrapLoader, TraptaskLoader
from trap.models import DirsConfig, TraptaskCase, TraptaskConfig
from trap.runner import TaskRunner, refuse_answer_leaks

from .conftest import JUDGE_SCORE, PY, case_capture, unlock

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


# --- tp run: a case's file links to shared inputs (dabstep's layout) -----------------

SHARED = "day,count\n1,42\n"

#: A plain solution that prints its case's data.csv, found through the manifest.
PRINTS_DATA_CSV = shlex.join(
    [
        PY,
        "-c",
        "import json, os, pathlib; "
        "inputs = pathlib.Path(json.loads(os.environ['TRAP_MANIFEST'])['inputs_dir']); "
        "print((inputs / 'data.csv').read_text(), end='')",
    ]
)


def _shared_inputs(make_project, cmd: str, tmp_path: Path) -> tuple[Path, Path]:
    """dabstep's layout: cases c1 and c2 each hold question.txt and data.csv, a link to the
    one real copy in inputs/context/ (not a case); no expected dir. Returns (solution, task)."""
    sol = make_project(
        cmd=cmd, cases=["c1", "c2"], inputs={c: {"question.txt": f"q {c}"} for c in ("c1", "c2")}
    )
    task = (tmp_path / "task").resolve()
    (task / "inputs" / "context").mkdir()
    (task / "inputs" / "context" / "data.csv").write_text(SHARED)
    for case in ("c1", "c2"):
        (task / "inputs" / case / "data.csv").symlink_to("../context/data.csv")
    return sol, task


def test_file_links_to_shared_inputs_run_and_the_solution_reads_them(make_project, runner, tmp_path):
    sol, _ = _shared_inputs(make_project, PRINTS_DATA_CSV, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 0, res.output
    for case in ("c1", "c2"):
        out, meta = case_capture(sol, case)
        assert (out, meta["exit_code"]) == (SHARED, 0)


def test_a_shape_hands_its_program_a_file_link_to_shared_inputs_as_a_regular_file(
    make_project, runner, tmp_path
):
    # The program answers only when data.csv in its work dir is a regular file, not a link.
    template = "sh -c 'test -f data.csv && test ! -L data.csv && cat data.csv'"
    cmd = shlex.join([PY, "-m", "trap.shapes.command", "--template", template, "--deadline", "30"])
    sol, _ = _shared_inputs(make_project, cmd, tmp_path)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 0, res.output
    for case in ("c1", "c2"):
        out, meta = case_capture(sol, case)
        assert (out, meta["exit_code"]) == (SHARED, 0)


def _to_the_answer(task: Path) -> str:
    (task / "expected" / "c1").mkdir(parents=True)
    (task / "expected" / "c1" / "answer.txt").write_text("secret")
    (task / "inputs" / "c1" / "reference.txt").symlink_to("../../expected/c1/answer.txt")
    return "c1/reference.txt"


def _through_a_link_out_of_the_inputs_root(task: Path) -> str:
    # Spelled inside inputs/context/, but esc/ there is a link out to expected/.
    (task / "expected" / "c1").mkdir(parents=True)
    (task / "expected" / "c1" / "answer.txt").write_text("secret")
    (task / "inputs" / "context" / "esc").symlink_to("../../expected")
    (task / "inputs" / "c1" / "notes.csv").symlink_to("../context/esc/c1/answer.txt")
    return "c1/notes.csv"


def _to_a_file_outside_the_task(task: Path) -> str:
    (task.parent / "outside.csv").write_text("elsewhere")
    (task / "inputs" / "c1" / "outside.csv").symlink_to(task.parent / "outside.csv")
    return "c1/outside.csv"


NOT_A_FILE_INSIDE = "is a symlink that does not resolve to a file inside {inputs}"


@pytest.mark.parametrize(
    ("make_link", "why"),
    [
        pytest.param(_to_the_answer, NOT_A_FILE_INSIDE, id="a file link to the answer"),
        pytest.param(_through_a_link_out_of_the_inputs_root, NOT_A_FILE_INSIDE, id="through a link out"),
        pytest.param(_to_a_file_outside_the_task, NOT_A_FILE_INSIDE, id="an absolute link out of the task"),
        pytest.param(
            lambda t: (t / "inputs/c1/data").symlink_to("../context") or "c1/data",
            NOT_A_FILE_INSIDE,
            id="a link to a directory",
        ),
        pytest.param(
            lambda t: (t / "inputs/c1/gone.csv").symlink_to("../context/gone.csv") or "c1/gone.csv",
            NOT_A_FILE_INSIDE,
            id="dangling",
        ),
        pytest.param(
            lambda t: (t / "inputs/c1/loop.csv").symlink_to("loop.csv") or "c1/loop.csv",
            NOT_A_FILE_INSIDE,
            id="a loop",
        ),
        pytest.param(
            lambda t: _relink(t / "inputs/c1", "context") or "c1", "is a symlink", id="the case dir"
        ),
    ],
)
def test_any_other_link_in_a_task_with_shared_inputs_is_refused(
    make_project, runner, tmp_path, make_link: Callable[[Path], str], why: str
):
    sol, task = _shared_inputs(make_project, MARKS_THAT_IT_RAN, tmp_path)
    link = task / "inputs" / make_link(task)
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, f"c1: inputs {link} {why.format(inputs=task / 'inputs')}")
    assert "secret" not in res.output and "elsewhere" not in res.output


def test_a_file_link_into_answers_kept_inside_the_inputs_root_is_refused(make_project, runner, tmp_path):
    sol, task = _shared_inputs(make_project, MARKS_THAT_IT_RAN, tmp_path)
    (task / "inputs").rename(task / "cases")
    for case in ("c1", "c2"):
        (task / "cases" / "_answers" / case).mkdir(parents=True)
        (task / "cases" / "_answers" / case / "answer.txt").write_text(f"secret-{case}")
    config = json.loads((task / "traptask.yaml").read_text())
    dirs = {"inputs": "cases/", "expected": "cases/_answers/"}
    (task / "traptask.yaml").write_text(json.dumps({**config, "dirs": dirs}))
    (task / "cases" / "c1" / "reference.txt").symlink_to("../_answers/c2/answer.txt")
    res = runner.invoke(app, ["run", "--no-environment"])
    link, answers = task / "cases" / "c1" / "reference.txt", task / "cases" / "_answers" / "c2"
    _assert_refused(res, sol, f"c1: inputs {link} is a symlink into the answers of case 'c2' {answers}")
    assert "secret" not in res.output


def test_a_nested_case_reached_through_a_linked_directory_in_the_inputs_root_is_refused(
    make_project, runner, tmp_path
):
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["grp/c1"])
    task = (tmp_path / "task").resolve()
    (task / "inputs" / "grp").rename(task / "inputs" / "context")
    (task / "inputs" / "grp").symlink_to("context")
    res = runner.invoke(app, ["run", "--no-environment"])
    _assert_refused(res, sol, f"grp/c1: inputs {task / 'inputs' / 'grp'} is a symlink")


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


# --- no link in what a solution is handed, but one to a file in the inputs ------------


@pytest.mark.parametrize(
    ("make_link", "link"),
    [
        pytest.param(lambda t: (t / "inputs/c1/data").symlink_to("../../expected/c1"), "c1/data", id="a dir"),
        pytest.param(
            lambda t: (t / "inputs/c1/answer.txt").symlink_to("../../expected/c1/answer.txt"),
            "c1/answer.txt",
            id="a file outside the inputs root",
        ),
        pytest.param(
            lambda t: (t / "inputs/c1/answer.txt").symlink_to("../../Expected/c1/answer.txt"),
            "c1/answer.txt",
            id="another letter case of a file outside",
        ),
        pytest.param(
            lambda t: (t / "inputs/c1/gone.txt").symlink_to("nowhere"), "c1/gone.txt", id="dangling"
        ),
        pytest.param(lambda t: (t / "inputs/c1/loop").symlink_to("loop"), "c1/loop", id="a loop"),
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


def test_an_entry_that_vanishes_during_the_walk_is_refused(tmp_path, monkeypatch):
    task = _task(tmp_path)
    walk = os.walk

    def walk_with_a_vanished_entry(top, **kwargs):
        for dirpath, dirnames, filenames in walk(top, **kwargs):
            yield dirpath, dirnames, [*filenames, "vanished.txt"]

    monkeypatch.setattr(os, "walk", walk_with_a_vanished_entry)
    message = _refusal(task, ["c1"])
    assert f"c1: inputs {task / 'inputs' / 'c1' / 'vanished.txt'} cannot be read" in message


def test_only_the_cases_about_to_run_are_walked(tmp_path):
    task = _task(tmp_path, ("c1", "c2"))
    (task / "inputs" / "c2" / "reference.txt").symlink_to("../../expected/c2/answer.txt")
    assert _refusal(task, ["c1", "c2"], run=["c1"]) == ""


# --- the roots, resolved as the runner resolves them ----------------------------------


@pytest.fixture(params=["gone", "locked"])
def detour(request, tmp_path: Path) -> Iterator[str]:
    """A folder for a ``..`` in a root to pass through: one that isn't there, or one no one
    may search. The runner resolves such a ``..`` by text, where the OS fails on it."""
    if request.param == "locked":
        (tmp_path / "task" / "locked").mkdir(parents=True, mode=0o000)
    yield request.param
    unlock(tmp_path)


def test_a_dotdot_in_dirs_inputs_is_resolved_as_the_runner_resolves_it(tmp_path, detour: str):
    task = _task(tmp_path)
    link = task / "inputs" / "c1" / "reference.txt"
    link.symlink_to("../../expected/c1/answer.txt")
    assert f"c1: inputs {link} is a symlink" in _refusal(task, ["c1"], inputs=f"{detour}/../inputs/")


def test_a_dotdot_in_dirs_expected_is_resolved_as_the_runner_resolves_it(tmp_path, detour: str):
    task = _task(tmp_path)
    answers = task / "inputs" / "c1" / "ans"
    answers.mkdir()
    message = _refusal(task, ["c1"], expected=f"{detour}/../inputs/c1/ans/")
    assert f"c1: inputs {task / 'inputs' / 'c1'} hold the expected root {answers}" in message


def test_an_inputs_root_linked_through_a_dotdot_is_resolved_as_the_runner_resolves_it(tmp_path, detour: str):
    task = _task(tmp_path)
    (task / "inputs").rename(task / "stored")
    (task / "inputs").symlink_to(f"{detour}/../stored")
    link = task / "stored" / "c1" / "reference.txt"
    link.symlink_to("../../expected/c1/answer.txt")
    assert f"c1: inputs {link} is a symlink" in _refusal(task, ["c1"])


# --- no answers inside what a solution is handed --------------------------------------


NFC, NFD = (unicodedata.normalize(form, "réponses") for form in ("NFC", "NFD"))


def _expected_in_another_unicode_form(task: Path) -> dict[str, str]:
    (task / "expected").rename(task / NFC)
    return {"inputs": f"{NFD}/", "expected": f"{NFC}/"}


def _inputs_root_linked_to_expected(task: Path) -> dict[str, str]:
    shutil.rmtree(task / "inputs")
    (task / "inputs").symlink_to("expected")
    return {}


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(lambda task: {"inputs": "expected/"}, id="one name"),
        pytest.param(lambda task: {"inputs": "Expected/"}, id="another letter case"),
        pytest.param(_expected_in_another_unicode_form, id="another Unicode form"),
        pytest.param(_inputs_root_linked_to_expected, id="an inputs root linked to it"),
    ],
)
def test_dirs_naming_one_directory_are_refused(tmp_path, layout: Callable[[Path], dict[str, str]]):
    task = _task(tmp_path)
    dirs = layout(task)
    inputs = task / dirs.get("inputs", "inputs/")
    if not inputs.exists():
        pytest.skip("the tmp filesystem tells these two spellings apart")
    line = f"c1: inputs {os.path.realpath(inputs / 'c1')} hold the answers of case 'c1'"
    assert line in _refusal(task, ["c1"], **dirs)


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


@pytest.mark.parametrize(
    ("target", "whose"),
    [
        pytest.param(
            "../_answers/c2/answer.txt", "the answers of case 'c2' {answers}/c2", id="a case's answers"
        ),
        pytest.param("../_answers/notes.txt", "the expected root {answers}", id="the expected root"),
        pytest.param(
            "../_answers/c2/../c1/answer.txt", "the answers of case 'c1' {answers}/c1", id="spelled round"
        ),
    ],
)
def test_a_file_link_into_answers_inside_the_inputs_root_is_refused(tmp_path, target: str, whose: str):
    task = _task(tmp_path, ("c1", "c2"))
    answers = task / "inputs" / "_answers"
    (task / "expected").rename(answers)
    (answers / "notes.txt").write_text("secret notes")
    link = task / "inputs" / "c1" / "reference.txt"
    link.symlink_to(target)
    message = _refusal(task, ["c1", "c2"], expected="inputs/_answers/")
    assert f"c1: inputs {link} is a symlink into {whose.format(answers=answers)}" in message
    assert "secret" not in message


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
        (task / "inputs" / case / "gone").symlink_to("nowhere")
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


def _shared_file(task: Path) -> None:
    (task / "inputs" / "context").mkdir()
    (task / "inputs" / "context" / "data.csv").write_text("shared")
    (task / "inputs" / "c1" / "data.csv").symlink_to("../context/data.csv")


def _a_chain_of_links_to_a_shared_file(task: Path) -> dict[str, str]:
    _shared_file(task)
    (task / "inputs" / "context" / "latest.csv").symlink_to("data.csv")
    (task / "inputs" / "c1" / "latest.csv").symlink_to(task / "inputs" / "context" / "latest.csv")
    return {}


def _a_shared_file_with_the_answers_at_the_task_root(task: Path) -> dict[str, str]:
    # The expected root is the task root, around the inputs root: only the directories
    # under the inputs root are compared with the answers.
    for case in ("c1", "c2"):
        (task / "expected" / case).rename(task / case)
    _shared_file(task)
    return {"expected": "./"}


def _a_shared_file_in_an_inputs_root_linked_elsewhere(task: Path) -> dict[str, str]:
    _inputs_stored_elsewhere(task)
    _shared_file(task)
    (task / "inputs" / "c1" / "absolute.csv").symlink_to(task / "inputs" / "context" / "data.csv")
    return {}


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(lambda task: shutil.rmtree(task / "expected") or {}, id="no expected dir"),
        pytest.param(lambda task: shutil.rmtree(task / "inputs" / "c1") or {}, id="a case without inputs"),
        pytest.param(_answers_in_the_inputs_root, id="answers beside the cases in the inputs root"),
        pytest.param(_inputs_at_the_task_root, id="inputs at the task root"),
        pytest.param(lambda task: _shared_file(task) or {}, id="a file link to a shared file"),
        pytest.param(
            lambda task: (task / "inputs/c1/alias").symlink_to("question.txt") or {},
            id="a file link to a file in the same case",
        ),
        pytest.param(
            lambda task: (task / "inputs/c1/other.txt").symlink_to("../c2/question.txt") or {},
            id="a file link to another case's input",
        ),
        pytest.param(_a_chain_of_links_to_a_shared_file, id="a chain of links to a shared file"),
        pytest.param(_a_shared_file_with_the_answers_at_the_task_root, id="answers at the task root"),
        pytest.param(_a_shared_file_in_an_inputs_root_linked_elsewhere, id="an inputs root linked elsewhere"),
    ],
)
def test_a_layout_that_hands_over_no_answers_runs(tmp_path, layout: Callable[[Path], dict[str, str]]):
    task = _task(tmp_path, ("c1", "c2"))
    assert _refusal(task, ["c1", "c2"], **layout(task)) == ""


# --- TaskRunner.run -------------------------------------------------------------------


def _task_runner(tmp_path: Path) -> TaskRunner:
    tl = TrapLoader.from_solution(None)
    ttl = TraptaskLoader.from_task_binding(tl.resolve_task(None), tl.trap_dir)
    return TaskRunner(tl.config, tl.trap_dir, ttl.traptask_dir, ttl.traptask, tmp_path / "run", False)


def test_the_runner_refuses_the_whole_run_before_its_first_case(make_project, tmp_path):
    # c1 is clean and would run first; the run is refused as a whole because of c2.
    sol = make_project(cmd=MARKS_THAT_IT_RAN, cases=["c1", "c2"])
    task = (tmp_path / "task").resolve()
    (task / "inputs" / "c2" / "gone").symlink_to("nowhere")
    tr = _task_runner(tmp_path)
    with pytest.raises(ConfigError) as e:
        tr.run(iter(tr.traptask_config.cases))
    assert f"refusing to run task {task}" in str(e.value)
    assert f"  c2: inputs {task / 'inputs' / 'c2' / 'gone'} is a symlink" in str(e.value)
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

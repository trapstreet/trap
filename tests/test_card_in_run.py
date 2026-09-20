"""Every built-in shape says, on stderr, exactly what it ran."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from trap.models.card import SolutionCard
from trap.runner.layout import CaseLayout
from trap.shapes._case import CARD_PREFIX

from .conftest import PY, case_capture
from .test_shapes_direct import OPENAI_OK, _vendor


def card_from_stderr(text: str) -> SolutionCard:
    lines = [line for line in text.splitlines() if line.startswith(CARD_PREFIX)]
    assert len(lines) == 1, f"expected exactly one card line, got {len(lines)}"
    return SolutionCard.model_validate(json.loads(lines[0][len(CARD_PREFIX) :]))


def test_the_command_shape_cards_its_template(make_project, runner, tmp_path):
    from trap.cli import app

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    stderr = (sorted((sol / ".trap").rglob("stderr"))[0]).read_text()
    card = card_from_stderr(stderr)
    assert (card.shape, card.shape_version, card.timeout) == ("cmd", 1, 30.0)
    assert card.cmd == f"{PY} {{repo}}/tool.py" and card.model is None
    assert isinstance(card.timeout, int)


def test_the_direct_shape_cards_the_provider_and_model(make_project, runner, monkeypatch, tmp_path):
    from trap.cli import app

    srv, url = _vendor(OPENAI_OK)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        cmd = shlex.join([PY, "-m", "trap.shapes.direct", "--model", "gpt-test", "--deadline", "30"])
        sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "Q?"}})
        res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    finally:
        srv.shutdown()
    assert res.exit_code == 0, res.output
    out, meta = case_capture(sol)
    assert (out.strip(), meta["exit_code"]) == ("hi there", 0)
    stderr = next((Path(sol) / ".trap").rglob("c1/solution/stderr")).read_text()
    card = card_from_stderr(stderr)
    assert (card.shape, card.provider, card.model, card.timeout) == ("model", "openai", "gpt-test", 30.0)
    assert isinstance(card.timeout, int)


# --- print_card must never cost a case its answer and exit code over an unprintable ----
# --- label -- an argv- or agent-sourced lone UTF-16 surrogate is replaced, not raised ---


def test_print_card_replaces_a_lone_surrogate_instead_of_raising(capsys):
    from trap.shapes._case import print_card

    card = SolutionCard(shape="cmd", shape_version=1, cmd="echo \udcff", timeout=30)
    print_card(card)  # must not raise
    got = card_from_stderr(capsys.readouterr().err)
    assert got.cmd == "echo ?"  # str.encode(..., errors="replace") uses "?", not U+FFFD


def test_print_card_still_works_normally_when_nothing_needs_replacing(capsys):
    from trap.shapes._case import print_card

    card = SolutionCard(shape="model", shape_version=1, provider="anthropic", model="claude-sonnet-5")
    print_card(card)
    got = card_from_stderr(capsys.readouterr().err)
    assert (got.provider, got.model) == ("anthropic", "claude-sonnet-5")


def test_a_shape_run_records_its_card_and_digest_in_the_report(make_project, runner, tmp_path):
    from trap.cli import app
    from trap.models.card import card_digest

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0

    report = json.loads(next((sol / ".trap").rglob("report.json")).read_text())
    adapter = report["provenance"]["solution"]["adapter"]
    assert adapter["shape"] == "cmd" and adapter["cmd"].endswith("tool.py")
    assert report["provenance"]["solution"]["adapter_digest"] == card_digest(
        SolutionCard.model_validate(adapter)
    )


def test_a_plain_solution_records_no_card(make_project, runner):
    from trap.cli import app

    sol = make_project(cmd="sh -c 'echo hi'", inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    solution = json.loads(next((sol / ".trap").rglob("report.json")).read_text())["provenance"]["solution"]
    assert solution["adapter"] is None and solution["adapter_digest"] is None


# --- card_from_run: reading the card back out of a case's captured stderr ----------
# --- unit-level, so each branch (a bad line, an unreadable case, which case wins) --
# --- is exercised directly rather than through a full `tp run` each time. ----------


def _stderr_path(run_dir: Path, case_id: str) -> Path:
    path = CaseLayout.for_case(run_dir, case_id).solution_capture.stderr
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_card_from_run_returns_none_when_a_cases_stderr_is_unreadable(tmp_path):
    from trap.cli._card import card_from_run

    # case "a" never ran (no solution/stderr at all) -- reading it raises OSError,
    # which must be swallowed rather than failing the run.
    assert card_from_run(tmp_path / "run", ["a"]) is None


def test_card_from_run_uses_the_first_case_that_printed_a_card(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    _stderr_path(run_dir, "a").write_text("nothing carded here\n")
    card = SolutionCard(shape="cmd", shape_version=1, cmd="from-b")
    _stderr_path(run_dir, "b").write_text(CARD_PREFIX + card.model_dump_json() + "\n")

    got = card_from_run(run_dir, ["a", "b"])
    assert got is not None and got.cmd == "from-b"


def test_card_from_run_the_last_card_line_in_one_stderr_wins(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    first = SolutionCard(shape="cmd", shape_version=1, cmd="first")
    second = SolutionCard(shape="cmd", shape_version=1, cmd="second")
    _stderr_path(run_dir, "a").write_text(
        CARD_PREFIX + first.model_dump_json() + "\n" + CARD_PREFIX + second.model_dump_json() + "\n"
    )

    got = card_from_run(run_dir, ["a"])
    assert got is not None and got.cmd == "second"


def test_card_from_run_returns_none_for_a_line_that_is_not_json(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + "not json{\n")

    assert card_from_run(run_dir, ["a"]) is None


def test_card_from_run_returns_none_for_json_that_is_not_a_card(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    # Valid JSON, but missing the required `shape`/`shape_version` fields.
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + json.dumps({"cmd": "x"}) + "\n")

    assert card_from_run(run_dir, ["a"]) is None


def test_card_from_run_leaves_a_non_git_skill_directory_unchanged(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    card = SolutionCard(shape="acp", shape_version=1, skill=str(skill_dir))
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + card.model_dump_json() + "\n")

    got = card_from_run(run_dir, ["a"])
    assert got is not None and got.skill == str(skill_dir)


def test_card_from_run_resolves_a_git_backed_skill_to_repo_at_sha(tmp_path):
    from trap.cli._card import card_from_run

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["remote", "add", "origin", "https://github.com/o/r"],
    ):
        subprocess.run(["git", *args], cwd=skill_dir, check=True, capture_output=True, text=True)
    (skill_dir / "SKILL.md").write_text("---\nname: s\n---\n")
    subprocess.run(["git", "add", "-A"], cwd=skill_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=skill_dir, check=True, capture_output=True, text=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=skill_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    run_dir = tmp_path / "run"
    card = SolutionCard(shape="acp", shape_version=1, skill=str(skill_dir))
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + card.model_dump_json() + "\n")

    got = card_from_run(run_dir, ["a"])
    assert got is not None and got.skill == f"https://github.com/o/r@{sha}"

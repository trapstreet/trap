"""Every built-in shape says, on stderr, exactly what it ran."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from trap.live.context import SKILLS_UNSUPPORTED
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


def test_the_command_template_never_reaches_the_run_context():
    from trap.live.context import build_context
    from trap.models.provenance import GitProvenance, Provenance
    from trap.models.trap_yaml import Profile

    card = SolutionCard(
        shape="cmd", shape_version=1, cmd="python main.py --key $OPENAI_API_KEY {prompt}", setup="uv sync"
    )
    patch = build_context(
        profile=Profile(),
        provenance=Provenance(solution=GitProvenance()),
        environment=None,
        trap_version="1.2.3",
        card=card,
    )
    wire = json.dumps(patch)
    assert "OPENAI_API_KEY" not in wire and "main.py" not in wire and "uv sync" not in wire


# --- live sync end to end: the merge point (`card` folded into provenance) and the ---
# --- send point (`tracker.describe(final)`) are wired together only in cli/__init__ --
# --- -- test_the_command_template_never_reaches_the_run_context above only exercises -
# --- a direct build_context() call, so it cannot catch a leak introduced at either ---
# --- of those two call sites, only inside build_context() itself. ---------------------


def test_live_sync_carries_the_cards_labels_but_never_its_command_or_setup(
    make_project, runner, monkeypatch, tmp_path
):
    from tests.test_live import _FakeTracker, _use_fake_tracker
    from trap.cli import app

    tracker = _FakeTracker()
    _use_fake_tracker(monkeypatch, tracker)

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = (
        f"{PY} -m trap.shapes.command --repo {tool} "
        f"--template '{PY} {{repo}}/tool.py --leak-marker-cmd-9f3a' "
        "--setup 'echo leak-marker-setup-2b7c' --deadline 30"
    )
    make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    result = runner.invoke(app, ["run", "--task", "t", "--no-environment", "--live"])
    assert result.exit_code == 0, result.output

    # Two describe() calls: the opening one (before the card is known) and the
    # closing one (after `card_from_run` read it back and folded it into
    # provenance) -- the card can only ever reach the second. Checked across all
    # three places a card can show up, not just `identity.name`: a refactor that
    # leaked it into the opening `model.config` or `skills.installed` instead
    # would pass a narrower, single-key assertion.
    opening, final = tracker.described
    assert "name" not in opening["identity"]
    assert "config" not in opening.get("model", {})
    assert opening["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}

    wire = json.dumps(final)
    for secret in ("leak-marker-cmd-9f3a", "leak-marker-setup-2b7c", "tool.py", "adapter"):
        assert secret not in wire, secret
    # The labels the card *is* meant to contribute are there: a `cmd`-shaped
    # card with no name has nothing safe to call itself but its shape.
    assert final["identity"]["name"] == "cmd"
    assert final["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}


# --- card_from_run: reading the card back out of a case's captured stderr ----------
# --- unit-level, so each branch (a bad line, an unreadable case, which case wins) --
# --- is exercised directly rather than through a full `tp run` each time. ----------


def _stderr_path(run_dir: Path, case_id: str) -> Path:
    path = CaseLayout.for_case(run_dir, case_id).solution_capture.stderr
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_card_from_run_silently_tries_the_next_case_when_one_never_ran(tmp_path, capsys):
    from trap.cli._card import card_from_run

    # case "a" never ran (no solution/stderr at all) -- reading it raises
    # FileNotFoundError, which must be swallowed, quietly, rather than failing the
    # run: this is the ordinary "not a built-in shape" / "case didn't get this far"
    # case, not a problem worth a log line.
    assert card_from_run(tmp_path / "run", ["a"]) is None
    assert capsys.readouterr().err == ""


def test_card_from_run_logs_and_skips_a_stderr_it_cannot_read_for_another_reason(
    tmp_path, monkeypatch, capsys
):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    stderr = _stderr_path(run_dir, "a")
    stderr.write_text("irrelevant\n")

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", denied)

    # Unlike a missing case (silently tried next), a real read failure -- a
    # PermissionError, say -- must not look identical to "this solution prints no
    # card": it is named on stderr, distinctly, before the run moves on.
    assert card_from_run(run_dir, ["a"]) is None
    err = capsys.readouterr().err
    assert err.startswith("[trap]")
    assert str(stderr) in err and "Permission denied" in err


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


# --- tp submit shows, before publishing, exactly what a carded submit makes public ---


def _carded_run(make_project, runner, tmp_path):
    """A finished run whose report has a card and an anchored solution repo."""
    from trap.cli import app

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    report_path = next((sol / ".trap").rglob("report.json"))
    report = json.loads(report_path.read_text())
    report["provenance"]["solution"] |= {
        "repo": "https://github.com/o/sol",
        "commit": "a" * 40,
        "issue": None,
    }
    report_path.write_text(json.dumps(report))
    return sol


def _submit(monkeypatch, runner):
    """Run `tp submit --yes`; return what was uploaded and what the user was told.
    `--yes` skips the interactive prompt only -- the credential-store stand-in is the
    same one-line monkeypatch the existing submit tests in tests/test_cli.py use."""
    from trap.cli import app

    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    sent: dict[str, object] = {}
    monkeypatch.setattr(
        "trap.auth.client.ApiClient.submit",
        lambda self, path: sent.update(payload=json.loads(Path(path).read_text())) or {"run": {"id": "r1"}},
    )
    result = runner.invoke(app, ["submit", "--task", "t", "--yes"])
    assert result.exit_code == 0, result.output
    return sent["payload"]["provenance"]["solution"], result.output


def test_a_carded_submit_uploads_the_card_and_keeps_the_repo(make_project, runner, tmp_path, monkeypatch):
    """The card is additive: it rides alongside the anchor, it does not replace it."""
    _carded_run(make_project, runner, tmp_path)
    solution, _ = _submit(monkeypatch, runner)
    assert solution["adapter"]["shape"] == "cmd" and solution["adapter_digest"]
    assert solution["repo"] == "https://github.com/o/sol" and solution["commit"] == "a" * 40


def test_the_confirmation_shows_the_command_template_verbatim(make_project, runner, tmp_path, monkeypatch):
    """§5.3: an explicit `tp submit`'s confirmation shows verbatim what is about to
    become public."""
    _carded_run(make_project, runner, tmp_path)
    _, output = _submit(monkeypatch, runner)
    assert f"{PY} {{repo}}/tool.py" in output  # the exact cmd, not paraphrased or cut
    assert "public" in output.lower()


def test_the_confirmation_shows_nothing_new_without_a_card(make_project, runner, monkeypatch):
    from trap.cli import app

    make_project(cmd="sh -c 'echo hi'", inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    _, output = _submit(monkeypatch, runner)
    assert "public" not in output.lower()


def test_confirm_card_shows_the_template_labelled_as_public(capsys):
    import trap.cli as climod

    card = SolutionCard(
        shape="cmd", shape_version=1, cmd="python main.py {prompt}", setup="pip install -r r.txt"
    )
    climod._confirm_card(card)
    err = capsys.readouterr().err
    assert "python main.py {prompt}" in err
    assert "pip install -r r.txt" in err
    assert "public" in err.lower()


def test_confirm_card_survives_a_bracket_in_the_command_unescaped_on_the_way_out(capsys):
    """rich markup uses `[...]`, so a literal `[` in the template must round-trip through
    `rich.markup.escape` and back out unchanged -- a lossy escape would silently break
    the one guarantee this whole confirmation exists for (verbatim, not paraphrased)."""
    import trap.cli as climod

    climod._confirm_card(SolutionCard(shape="cmd", shape_version=1, cmd="sed 's/[a-z]//' {prompt}"))
    assert "sed 's/[a-z]//' {prompt}" in capsys.readouterr().err


def test_confirm_card_shows_the_setup_line_even_without_a_command(capsys):
    import trap.cli as climod

    climod._confirm_card(SolutionCard(shape="cmd", shape_version=1, setup="pip install -r r.txt"))
    err = capsys.readouterr().err
    assert "pip install -r r.txt" in err
    assert "cmd:" not in err


def test_confirm_card_silent_for_a_card_with_nothing_to_preview(capsys):
    """An ACP or model-direct card carries no `cmd`/`setup` -- nothing becomes public
    that the intent table has not already shown, so this stays quiet."""
    import trap.cli as climod

    climod._confirm_card(SolutionCard(shape="acp", shape_version=1, agent="a@1", model="m"))
    climod._confirm_card(None)
    assert capsys.readouterr().err == ""


def test_confirm_submit_shows_the_template_even_when_yes_skips_the_prompt(
    make_project, runner, tmp_path, monkeypatch, capsys
):
    """--yes means the user pre-consented to skipping the *prompt* -- it must not also
    swallow what the submit makes public, since that's the only record a
    non-interactive run leaves behind."""
    import trap.cli as climod
    from trap.models import Provenance, ReportData

    report = ReportData(
        provenance=Provenance(),
        cases_results=(),
        grader_metrics=None,
        started_at_utc="2026-01-01T00:00:00",
        finished_at_utc="2026-01-01T00:00:01",
    )
    report.provenance.solution.adapter = SolutionCard(shape="cmd", shape_version=1, cmd="x {prompt}")
    climod._confirm_submit(report, "ts-1", "http://s", yes=True, allow_unanchored=False)
    assert "x {prompt}" in capsys.readouterr().err


# --- tp run with no trap.yaml: the flags are the solution -------------------------


def _task_only(tmp_path, monkeypatch, question="what is 2+2?"):
    """A task on disk and nothing else -- the shape of a task someone just cloned."""
    task = tmp_path / "task"
    (task / "inputs" / "c1").mkdir(parents=True)
    (task / "inputs" / "c1" / "question.txt").write_text(question)
    (task / "traptask.yaml").write_text(json.dumps({"cases": [{"id": "c1"}]}))
    here = tmp_path / "elsewhere"
    here.mkdir()
    monkeypatch.chdir(here)
    monkeypatch.setenv("TRAP_ALLOW_UNANCHORED", "1")
    return task, here


def test_a_cmd_template_runs_a_task_with_no_trap_yaml_anywhere(tmp_path, monkeypatch, runner):
    """The whole point: a task, a one-line command, no config file to write."""
    from trap.cli import app

    task, here = _task_only(tmp_path, monkeypatch)

    result = runner.invoke(
        app, ["run", str(task), "--cmd", f"{PY} -c 'print(4)'", "--no-live", "--no-environment"]
    )

    assert result.exit_code == 0, result.output
    assert not (here / "trap.yaml").exists()
    report = json.loads(next((here / ".trap").rglob("report.json")).read_text())
    assert [c["case_id"] for c in report["cases_results"]] == ["c1"]


def test_the_flags_run_this_tp_not_whichever_one_is_on_path():
    """A synthesised `tp shape ...` would be resolved through PATH, so a different trap
    build installed there would quietly become the measuring apparatus -- a different
    shape version, different generation defaults, different scores, and no sign of it in
    the report. The interpreter that is running now is named instead."""
    import sys

    from trap.cli import _config_from_flags

    for kwargs in (
        {"cmd": "echo hi", "agent": None, "model": None},
        {"cmd": None, "agent": None, "model": "claude-sonnet-5"},
        {"cmd": None, "agent": "claude-acp", "model": "haiku"},
    ):
        config = _config_from_flags(task_source="../task", **kwargs)
        assert config is not None
        argv = shlex.split(config.cmd)
        assert argv[0] == sys.executable, config.cmd
        assert argv[1] == "-m" and argv[2].startswith("trap.shapes."), config.cmd


def test_an_agent_alone_says_which_model_and_how_to_find_one(tmp_path, monkeypatch, runner):
    from trap.cli import app

    task, _ = _task_only(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", str(task), "--agent", "claude-acp"])
    assert result.exit_code == 2
    assert "--model" in result.output and "--describe" in result.output


def test_an_unknown_agent_names_the_ones_tp_can_start(tmp_path, monkeypatch, runner):
    from trap.cli import app

    task, _ = _task_only(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", str(task), "--agent", "gemini-cli", "--model", "m"])
    assert result.exit_code == 2
    assert "gemini-cli" in result.output and "claude-acp" in result.output


def test_a_template_cannot_also_be_an_agent(tmp_path, monkeypatch, runner):
    from trap.cli import app

    task, _ = _task_only(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", str(task), "--cmd", "echo hi", "--model", "m"])
    assert result.exit_code == 2
    assert "cannot be combined" in result.output


def test_the_flags_without_a_task_say_the_task_is_the_argument(tmp_path, monkeypatch, runner):
    from trap.cli import app

    _task_only(tmp_path, monkeypatch)
    result = runner.invoke(app, ["run", "--model", "claude-sonnet-5"])
    assert result.exit_code == 2
    assert "the task is required" in result.output


def test_an_agent_and_model_build_the_acp_shape_with_the_pinned_agent_command():
    from trap.cli import _config_from_flags
    from trap.shapes.acp.hints import agent_command

    config = _config_from_flags("claude-acp", "haiku", None, task_source="../task")
    assert config is not None
    argv = shlex.split(config.cmd)
    assert argv[2] == "trap.shapes.acp"
    assert argv[argv.index("--agent-cmd") + 1] == agent_command("claude-acp")
    assert argv[argv.index("--model") + 1] == "haiku"


def test_a_model_on_its_own_builds_the_direct_shape():
    from trap.cli import _config_from_flags

    config = _config_from_flags(None, "claude-sonnet-5", None, task_source="../task")
    assert config is not None
    assert shlex.split(config.cmd)[2] == "trap.shapes.direct"


def test_no_flags_reads_trap_yaml_as_before():
    from trap.cli import _config_from_flags

    assert _config_from_flags(None, None, None, task_source=None) is None

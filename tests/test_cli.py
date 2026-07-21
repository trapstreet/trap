"""End-to-end CLI tests — these drive the whole run → judge → grade → report
pipeline through the Typer app, covering loader / runner / report / models in one go."""

from __future__ import annotations

import json
from pathlib import Path

from trap.cli import app

from .conftest import GRADER_PASS, JUDGE_SCORE, PY


def _passing(make_project, **kw):
    return make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        cases=["c1"],
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
        grader_src=GRADER_PASS,
        **kw,
    )


def test_version(runner):
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert res.output.startswith("tp ")


def test_run_full_pipeline(make_project, runner):
    _passing(make_project)
    res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert "1 case" in res.output
    assert '"passed": true' in res.output  # grader output rendered verbatim


def test_run_default_task_and_environment(make_project, runner):
    _passing(make_project)
    res = runner.invoke(app, ["run"])  # no --task → first task; environment on
    assert res.exit_code == 0, res.output


def test_run_json_output(make_project, runner):
    _passing(make_project)
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert data["cases_results"][0]["case_id"] == "c1"
    assert data["grader_metrics"]["passed"] is True


def test_report_carries_solution_name(make_project, runner):
    _passing(make_project, extra_solution={"name": "my-sol"})
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["solution_name"] == "my-sol"


def test_run_wrong_answer_scores_zero_but_exits_0(make_project, runner):
    make_project(
        cmd="sh -c 'echo nope'",
        cases=["c1"],
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
        grader_src=GRADER_PASS,
    )
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 0  # facts, not a verdict
    assert '"passed": false' in res.output


def test_run_tag_filter_and_skip(make_project, runner):
    make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        cases=["c1", "c2", "c3"],
        inputs={c: {"input.txt": "x"} for c in ("c1", "c2", "c3")},
        tags={"c1": ["smoke"]},
        skip=("c3",),
    )
    res = runner.invoke(app, ["run", "-t", "smoke", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert "c1" in res.output and "c2" not in res.output


def test_run_output_only_autodiscovered(make_project, runner, tmp_path):
    # no traptask.yaml → cases discovered from inputs/
    make_project(cmd="sh -c 'echo hi'", cases=["a", "b"])
    (tmp_path / "task" / "traptask.yaml").unlink()
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 0, res.output
    assert "2 cases" in res.output


def test_run_solution_timeout_is_isolated(make_project, runner):
    make_project(cmd="sh -c 'sleep 5'", cases=["c1"], timeout=1)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 0, res.output  # run completes despite the hang
    assert "124" in res.output  # exit column shows the timeout sentinel


def test_run_broken_judge_and_grader_isolated(make_project, runner):
    make_project(
        cmd="sh -c 'echo hi'",
        cases=["c1"],
        judge={"cmd": "sh -c 'exit 3'"},
        grader={"cmd": "sh -c 'echo not-json'"},
    )
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert res.exit_code == 0, res.output
    data = json.loads(res.stdout)
    assert "exited with status 3" in data["cases_results"][0]["metrics"]["error"]
    assert "invalid JSON" in data["grader_metrics"]["error"]


def test_run_fail_fast(make_project, runner):
    make_project(cmd="sh -c 'exit 1'", cases=["c1", "c2"])
    res = runner.invoke(app, ["run", "--fail-fast", "--no-environment"])
    assert res.exit_code == 0, res.output


# --- error paths → clean message, no traceback -------------------------------


def test_run_missing_trap_yaml(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["run"])
    assert res.exit_code == 2
    assert "no trap.yaml" in res.output


def test_run_invalid_config(runner, tmp_path, monkeypatch):
    (tmp_path / "trap.yaml").write_text(json.dumps({"tasks": {"t": {"source": "x"}}}))  # no cmd
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["run"])
    assert res.exit_code == 2
    assert "invalid trap.yaml" in res.output


def test_run_unknown_task_alias_lists_available(make_project, runner):
    _passing(make_project)
    res = runner.invoke(app, ["run", "--task", "nope"])
    assert res.exit_code == 2
    assert "available: t" in res.output


def test_run_malformed_yaml(runner, tmp_path, monkeypatch):
    (tmp_path / "trap.yaml").write_text("cmd: 'unterminated\n  bad: [")
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["run"])
    assert res.exit_code == 2
    assert "invalid YAML" in res.output


# --- remote-source confirmation gate -----------------------------------------


def test_run_remote_refused_without_tty(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    res = runner.invoke(app, ["run", "git+https://example.invalid/x.git"])
    assert res.exit_code == 2
    assert "needs confirmation" in res.output


def test_confirm_remote_trust_bypasses():
    import trap.cli as climod

    climod._confirm_remote("git+url", trust=True)  # returns, no prompt


def test_confirm_remote_declined_raises(monkeypatch):
    import pytest

    import trap.cli as climod

    monkeypatch.setattr(climod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(climod.typer, "confirm", lambda *a, **k: False)
    with pytest.raises(climod.typer.Exit) as e:
        climod._confirm_remote("git+url", trust=False)
    assert e.value.exit_code == 1


def test_confirm_remote_accepted_returns(monkeypatch):
    import trap.cli as climod

    monkeypatch.setattr(climod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(climod.typer, "confirm", lambda *a, **k: True)
    climod._confirm_remote("git+url", trust=False)  # returns, no raise


def test_run_remote_trusted_proceeds_to_clone(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # file:// to a nonexistent path fails fast at clone (no network), proving the gate
    # was bypassed and the loader was reached.
    res = runner.invoke(app, ["run", "git+file:///nonexistent-trap-repo", "--trust-remote"])
    assert res.exit_code == 2
    assert "git clone failed" in res.output


def test_run_remote_trusted_via_env(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRAP_TRUST_REMOTE", "1")
    res = runner.invoke(app, ["run", "git+file:///nonexistent-trap-repo"])
    assert res.exit_code == 2
    assert "git clone failed" in res.output


# --- report ------------------------------------------------------------------


def test_report_after_run(make_project, runner):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    res = runner.invoke(app, ["report"])
    assert res.exit_code == 0, res.output
    assert "1 case" in res.output


def test_report_missing_run(make_project, runner):
    _passing(make_project)
    res = runner.invoke(app, ["report"])
    assert res.exit_code == 2
    assert "no completed runs" in res.output


# --- submit ------------------------------------------------------------------


def test_submit_not_logged_in(make_project, runner, monkeypatch):
    _passing(make_project)
    monkeypatch.delenv("TRAPSTREET_API_KEY", raising=False)
    # point auth store at an empty location so no stored token is found
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", Path("/nonexistent/auth.json"))
    res = runner.invoke(app, ["submit"])
    assert res.exit_code == 2
    assert "not logged in" in res.output


def test_init_stub(runner):
    res = runner.invoke(app, ["init"])
    assert res.exit_code == 0


# --- unanchored-provenance gate (leaderboard hides such runs) ------------------


def test_run_warns_unanchored_with_reason(make_project, runner):
    _passing(make_project)  # tmp_path scaffold → neither side is a git repo
    res = runner.invoke(app, ["run", "--no-environment"])  # fixture pre-authorises the gate
    assert res.exit_code == 0, res.output
    assert "solution has no git provenance (not a git repo)" in res.stderr
    assert "task has no git provenance (not a git repo)" in res.stderr
    assert "leaderboard hides" in res.stderr


def test_run_json_stdout_stays_clean_despite_warning(make_project, runner):
    _passing(make_project)
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    json.loads(res.stdout)  # warning went to stderr, not into the JSON
    assert "leaderboard hides" in res.stderr


def test_run_unanchored_refused_without_tty(make_project, runner, monkeypatch):
    _passing(make_project)
    monkeypatch.delenv("TRAP_ALLOW_UNANCHORED", raising=False)
    res = runner.invoke(app, ["run", "--no-environment"])
    assert res.exit_code == 2
    assert "needs confirmation" in res.output
    assert "leaderboard hides" in res.stderr  # warning shown before refusing


def test_run_unanchored_allowed_via_flag(make_project, runner, monkeypatch):
    _passing(make_project)
    monkeypatch.delenv("TRAP_ALLOW_UNANCHORED", raising=False)
    res = runner.invoke(app, ["run", "--allow-unanchored", "--no-environment"])
    assert res.exit_code == 0, res.output


def test_submit_repeats_gate_with_stored_reason(make_project, runner, monkeypatch):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    monkeypatch.setattr("trap.auth.client.ApiClient.submit", lambda self, path: {"run": {"passed": True}})
    res = runner.invoke(app, ["submit", "--task", "t"])  # fixture pre-authorises the gate
    assert res.exit_code == 0, res.output
    # the reason was saved in the report's `issue` field, so submit names it too
    assert "solution has no git provenance (not a git repo)" in res.stderr
    assert "leaderboard hides" in res.stderr


def test_submit_unanchored_refused_without_tty(make_project, runner, monkeypatch):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    monkeypatch.delenv("TRAP_ALLOW_UNANCHORED", raising=False)
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 2
    assert "needs confirmation" in res.output


def test_confirm_unanchored_silent_when_anchored(capsys):
    import trap.cli as climod
    from trap.models import GitProvenance, Provenance

    anchored = GitProvenance(repo="https://x/r", commit="a" * 40)
    climod._confirm_unanchored(Provenance(solution=anchored, task=anchored), allow=False)
    assert capsys.readouterr().err == ""


def test_confirm_unanchored_generic_without_issue(capsys):
    import trap.cli as climod
    from trap.models import Provenance

    climod._confirm_unanchored(Provenance(), allow=True)  # report predating the `issue` field
    err = capsys.readouterr().err
    assert "solution has no git provenance" in err
    assert "solution has no git provenance (" not in err  # no reason suffix


def test_confirm_unanchored_declined_raises(monkeypatch):
    import pytest

    import trap.cli as climod
    from trap.models import Provenance

    monkeypatch.setattr(climod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(climod.typer, "confirm", lambda *a, **k: False)
    with pytest.raises(climod.typer.Exit) as e:
        climod._confirm_unanchored(Provenance(), allow=False)
    assert e.value.exit_code == 1


def test_confirm_unanchored_accepted_returns(monkeypatch):
    import trap.cli as climod
    from trap.models import Provenance

    monkeypatch.setattr(climod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(climod.typer, "confirm", lambda *a, **k: True)
    climod._confirm_unanchored(Provenance(), allow=False)  # returns, no raise


def test_python_interpreter_available():
    assert Path(PY).exists()

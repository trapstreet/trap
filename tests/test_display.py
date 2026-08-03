from __future__ import annotations

from trap.display.report import RichRenderer
from trap.display.submit import SubmitRenderer
from trap.models import CaseResult, GitProvenance, Profile, Provenance, ReportData


def _report(**kw) -> ReportData:
    base = {
        "cases_results": (),
        "grader_metrics": None,
        "started_at_utc": "2026-01-01T00:00:00",
        "finished_at_utc": "2026-01-01T00:00:01",
    }
    return ReportData(**{**base, **kw})


def test_render_submit_success(capsys):
    SubmitRenderer().result({"run": {"id": "r1"}, "view_url": "http://x/runs/r1"})
    out = capsys.readouterr().out
    assert "submitted" in out and "r1" in out and "http://x/runs/r1" in out


def test_render_submit_lean_response(capsys):
    # missing keys still render — success was already decided by the HTTP status
    SubmitRenderer().result({})
    out = capsys.readouterr().out
    assert "submitted" in out and "?" in out


def test_render_submit_result_echoes_local_recap(capsys):
    # with the local report, success echoes what was uploaded (solution · run · tally)
    data = _report(
        solution_name="my-sol",
        cases_results=(CaseResult(case_id="c1", metrics=None, judge_exit_code=0),),
    )
    SubmitRenderer().result({"run": {"id": "r1"}}, report_data=data, run_id="ts-1")
    out = capsys.readouterr().out
    assert "uploaded" in out and "my-sol" in out and "ts-1" in out and "1 case" in out


def test_render_submit_intent_anchored(capsys):
    anchored = GitProvenance(repo="https://x/r", commit="a" * 40, subdirectory="sub")
    data = _report(
        solution_name="my-sol",
        profile=Profile(model=("haiku",)),
        provenance=Provenance(solution=anchored, task=anchored),
        cases_results=(
            CaseResult(case_id="c1", metrics=None, judge_exit_code=0),
            CaseResult(case_id="c2", exit_code=1, metrics=None, judge_exit_code=1),
        ),
    )
    SubmitRenderer().intent(data, "ts-1", "https://trapstreet.run")
    out = capsys.readouterr().out
    assert "my-sol" in out and "haiku" in out
    assert "ts-1" in out and "trapstreet.run" in out
    assert "2 cases" in out and "1/2 judged" in out and "1 non-zero exit" in out
    assert "https://x/r@aaaaaaaa" in out and "/sub" in out  # anchor shown, commit truncated


def test_render_submit_intent_unanchored_and_server_assigned(capsys):
    # no solution_name → server-assigned placeholder; unanchored → ✗ with reason
    data = _report(
        provenance=Provenance(solution=GitProvenance(issue="not a git repo")),
    )
    SubmitRenderer().intent(data, "ts-2", "http://local")
    out = capsys.readouterr().out
    assert "server-assigned" in out
    assert "unanchored" in out and "not a git repo" in out


def test_render_cost_unknown_distinct_from_zero():
    # unknown (unpriced model) must read differently from a measured zero
    assert RichRenderer._render_cost(None) == "[dim]?[/dim]"
    assert RichRenderer._render_cost(0.0) == "[dim]—[/dim]"

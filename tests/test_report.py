from __future__ import annotations

import json

from rich.console import Console

from trap.display.report import JsonRenderer, RichRenderer
from trap.models import CaseCost, CaseResult, ModelCost, ReportData


def _data(cases, grader=None):
    return ReportData(
        cases_results=tuple(cases), grader_metrics=grader, started_at_utc="x", finished_at_utc="y"
    )


def _render(data) -> str:
    console = Console(record=True, width=240)
    RichRenderer(console).render(data)
    return console.export_text()


def _cost(usd):
    return CaseCost(
        by_model=[
            ModelCost(
                provider="openai", model="gpt", prompt_tokens=28, completion_tokens=14, cost_usd=usd, calls=1
            )
        ]
    )


def test_rich_renders_every_cell_type():
    cases = [
        CaseResult(
            case_id="ok",
            exit_code=0,
            duration=0.05,
            metrics={
                "passed": True,
                "score": 1.0,
                "ratio": 0.5,
                "zero": 0.0,
                "note": "[brackets]",
                "none": None,
            },
            cost=_cost(0.000007),
        ),
        CaseResult(case_id="big", exit_code=0, duration=0.02, metrics={"passed": False}, cost=_cost(0.5)),
        CaseResult(case_id="free", exit_code=0, duration=0.02, metrics=None, cost=_cost(0.0)),
        CaseResult(case_id="bad", exit_code=2, duration=0.01, metrics=None),
    ]
    out = _render(_data(cases, grader={"passed": True, "score": 0.83}))
    assert "ok" in out
    assert "100%" in out and "50%" in out  # float buckets
    assert "✓" in out and "✗" in out  # bool cells
    assert "grader" in out and "0.83" in out  # grader rendered verbatim
    assert "4 case" in out and "1 non-zero exit" in out


def test_rich_no_cases():
    assert "0 cases" in _render(_data([]))


def test_json_renderer(capsys):
    JsonRenderer().render(_data([CaseResult(case_id="c", metrics=None)]))
    out = json.loads(capsys.readouterr().out)
    assert out["cases_results"][0]["case_id"] == "c"

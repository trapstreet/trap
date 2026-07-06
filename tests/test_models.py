from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trap.models import (
    CaseCost,
    CaseResult,
    GraderConfig,
    JudgeConfig,
    ModelCost,
    Profile,
    Provenance,
    ReportData,
    TrapConfig,
    TraptaskConfig,
)


def test_profile_scalar_and_list_and_none():
    assert Profile(model="gpt-4o", framework=["a", "b"]).model == ("gpt-4o",)
    assert Profile(framework=["a", "b"]).framework == ("a", "b")
    assert Profile(model=None).model == ()
    assert Profile().framework == ()


def test_trapconfig_requires_cmd():
    with pytest.raises(ValidationError):
        TrapConfig(tasks={"t": {"source": "x"}})


def test_trapconfig_defaults():
    c = TrapConfig(cmd="x", tasks={"t": {"source": "y"}})
    assert c.timeout == 600
    assert c.manifest_envvar == "TRAP_MANIFEST"


def test_casecost_aggregates_and_empty():
    c = CaseCost(
        by_model=[
            ModelCost(
                provider="openai", model="gpt", prompt_tokens=10, completion_tokens=5, cost_usd=0.1, calls=1
            ),
            ModelCost(provider="openai", prompt_tokens=2, completion_tokens=1, cost_usd=0.2, calls=1),
        ]
    )
    assert (c.prompt_tokens, c.completion_tokens, c.calls) == (12, 6, 2)
    assert round(c.cost_usd, 2) == 0.3
    empty = CaseCost()
    assert empty.prompt_tokens == 0 and empty.cost_usd == 0.0


def test_judge_grader_role_defaults():
    assert JudgeConfig(cmd="x").timeout == 300
    assert GraderConfig(cmd="x").timeout == 120
    assert JudgeConfig(cmd="x").manifest_envvar == "TRAPTASK_MANIFEST"


def test_report_from_run_roundtrip():
    cfg = TrapConfig(cmd="x", tasks={"t": {"source": "y"}}, profile=Profile(model="m"))
    cr = CaseResult(case_id="c1", metrics={"score": 1.0}, exit_code=0, duration=0.1)
    data = ReportData.from_run(
        trap_config=cfg,
        cases_results=(cr,),
        grader_metrics={"passed": True},
        started_at_utc=datetime.now(UTC),
        finished_at_utc=datetime.now(UTC),
        provenance=Provenance(),
    )
    back = ReportData.model_validate_json(data.model_dump_json())
    assert back.cases_results[0].metrics == {"score": 1.0}
    assert back.profile.model == ("m",)
    assert back.grader_metrics == {"passed": True}


def test_traptask_minimal():
    t = TraptaskConfig(cases=({"id": "a"},))
    assert t.cases[0].id == "a"
    assert t.judge is None and t.grader is None

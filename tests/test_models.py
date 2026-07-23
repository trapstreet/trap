from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from trap.models import (
    CaseCost,
    CaseResult,
    Diagnosis,
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


# -- Diagnosis (post-run measurement health) ----------------------------------

# (metrics, judge_exit_code) — pass/fail is the exit code alone; metrics never decides.
SCORED = ({"score": 1.0}, 0)  # exit 0 → passed
NULL_OK = (None, 0)  # exit 0, judge printed `null` → still passed
NON_JSON = (None, 125)  # exit 0 but output wasn't JSON → failed (sentinel)
CRASHED = (None, 3)  # non-zero exit → failed
NO_JUDGE = (None, None)  # no judge ran for this case


def _report(cases, *, grader_metrics=None, grader_exit_code=None):
    return ReportData(
        cases_results=tuple(
            CaseResult(case_id=f"c{i}", metrics=m, judge_exit_code=code) for i, (m, code) in enumerate(cases)
        ),
        grader_metrics=grader_metrics,
        grader_exit_code=grader_exit_code,
        started_at_utc="t0",
        finished_at_utc="t1",
    )


def test_diagnosis_pass_fail_is_the_exit_code_not_the_output():
    # exit 0 passes with no metrics (a `null` verdict); a non-zero exit fails even with
    # metrics present — the output never enters the decision.
    d = Diagnosis.from_report_data(_report([NULL_OK, ({"score": 1.0}, 3)]))
    assert d.partial_judge_failure and d.exit_code == 0
    assert len(d.judge_failures) == 1 and d.judge_failures[0].judge_exit_code == 3


def test_diagnosis_judge_broken_when_all_cases_fail():
    d = Diagnosis.from_report_data(_report([NON_JSON, CRASHED]))
    assert d.judge_broken and d.measurement_broken and d.exit_code == 3
    assert d.total_cases == 2 and len(d.judge_failures) == 2 and not d.partial_judge_failure


def test_diagnosis_partial_judge_failure_exits_0():
    d = Diagnosis.from_report_data(_report([CRASHED, SCORED]))
    assert not d.judge_broken and d.partial_judge_failure
    assert not d.measurement_broken and d.exit_code == 0


def test_diagnosis_clean_run():
    d = Diagnosis.from_report_data(_report([SCORED, NULL_OK], grader_exit_code=0))
    assert not d.measurement_broken and d.exit_code == 0
    assert not d.judge_failures and not d.partial_judge_failure


def test_diagnosis_no_judge_never_broken():
    # no judge ran (exit code None) → nothing to fail
    d = Diagnosis.from_report_data(_report([NO_JUDGE, NO_JUDGE]))
    assert not d.judge_broken and d.exit_code == 0


def test_diagnosis_empty_run_is_not_broken():
    d = Diagnosis.from_report_data(_report([]))
    assert not d.judge_broken and d.total_cases == 0


def test_diagnosis_grader_broken_on_nonzero_exit():
    d = Diagnosis.from_report_data(_report([SCORED], grader_exit_code=7))
    assert d.grader_broken and d.measurement_broken and d.exit_code == 3


def test_diagnosis_grader_broken_on_non_json():
    # grader exited 0 but its output wasn't JSON → the 125 sentinel → broke
    d = Diagnosis.from_report_data(_report([SCORED], grader_exit_code=125))
    assert d.grader_broken and d.exit_code == 3


def test_diagnosis_grader_pass_ignores_output():
    # grader exited 0 → passed, even with metrics None (it printed `null`)
    d = Diagnosis.from_report_data(_report([SCORED], grader_metrics=None, grader_exit_code=0))
    assert not d.grader_broken and d.exit_code == 0


def test_diagnosis_grader_not_run_is_not_broken():
    d = Diagnosis.from_report_data(_report([SCORED], grader_exit_code=None))
    assert not d.grader_broken and d.exit_code == 0

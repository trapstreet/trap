# Runtime result models produced by judge and grader subprocesses.
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from trap.models.cost import CaseCost


class CaseResult(BaseModel):
    case_id: str
    exit_code: int = 0  # the solution subprocess's exit code
    duration: float = 0.0  # seconds
    # any JSON-serializable value; trap does not interpret this, grader does
    metrics: Any
    cost: CaseCost | None = None
    # The judge subprocess's exit code — the sole pass/fail signal for scoring, or None
    # when no judge ran. 0 = passed (the judge produced valid JSON, whatever that JSON
    # was); 124 = timed out; 125 = it exited 0 but its stdout wasn't JSON; any other
    # non-zero value is the judge's own exit. ``metrics`` is the parsed verdict (any valid
    # JSON, or None) and never affects pass/fail.
    judge_exit_code: int | None = None

# Runtime result models produced by judge and grader subprocesses.
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from trap.models.cost import CaseCost


class CaseResult(BaseModel):
    case_id: str
    exit_code: int = 0
    duration: float = 0.0  # seconds
    # any JSON-serializable value; trap does not interpret this, grader does
    metrics: Any
    cost: CaseCost | None = None

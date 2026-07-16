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


# Marker key the runner writes into `metrics` when a judge/grader broke (timeout,
# non-zero exit, invalid JSON) and its error was folded in instead of raised.
# Part of the report.json schema: consumers distinguish "the measuring apparatus
# failed" from "the actor's verdict mentions an error" by this key, not by shape.
INFRA_ERROR_KEY = "infra"


def is_infra_error(metrics: Any) -> bool:
    """True when ``metrics`` is a folded infra error, not an actor-produced verdict."""
    return (
        isinstance(metrics, dict)
        and metrics.get(INFRA_ERROR_KEY) is True
        and isinstance(metrics.get("error"), str)
    )

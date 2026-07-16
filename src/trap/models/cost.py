from __future__ import annotations

import math

from pydantic import BaseModel, computed_field, field_validator


class ModelCost(BaseModel):
    provider: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # None = unknown (unpriced model). Never coerce unknown to 0 — an unknown
    # cost is not a zero cost. NaN (the calculator's in-memory marker) is
    # normalised to None here so the JSON round-trip (NaN → null → load) that
    # `tp submit` depends on can't crash.
    cost_usd: float | None = None
    calls: int = 0

    @field_validator("cost_usd")
    @classmethod
    def _nan_is_unknown(cls, v: float | None) -> float | None:
        if v is not None and math.isnan(v):
            return None
        return v


class CaseCost(BaseModel):
    by_model: list[ModelCost] = []

    @computed_field
    @property
    def prompt_tokens(self) -> int:
        return sum(u.prompt_tokens for u in self.by_model)

    @computed_field
    @property
    def completion_tokens(self) -> int:
        return sum(u.completion_tokens for u in self.by_model)

    @computed_field
    @property
    def cost_usd(self) -> float | None:
        # any unknown component makes the total unknown — a partial sum
        # presented as the total would understate real spend
        costs = [u.cost_usd for u in self.by_model]
        if any(c is None for c in costs):
            return None
        return sum(c for c in costs if c is not None)

    @computed_field
    @property
    def calls(self) -> int:
        return sum(u.calls for u in self.by_model)

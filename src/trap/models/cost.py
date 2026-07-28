from __future__ import annotations

from pydantic import BaseModel, computed_field


def combine_costs(*costs: float | None) -> float | None:
    """Combine ``cost_usd`` values, treating unknown (``None``) as contagious.

    A total that quietly dropped an unknown component would understate real
    spend, so any unknown makes the whole sum unknown. With no arguments the
    sum is ``0.0`` (an empty run cost nothing, not an unknown one).

    Shared by both fold levels — the proxy folding a call into a per-model
    bucket, and :attr:`CaseCost.cost_usd` folding the buckets into a case total
    — so it belongs to neither model in particular; it is the cost module's rule.
    """
    total = 0.0
    for cost in costs:
        if cost is None:
            return None
        total += cost
    return total


class ModelCost(BaseModel):
    provider: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # None = unknown (unpriced model). An unknown cost is not a zero cost, so it
    # is never coerced to 0.0; it serialises to JSON null and loads straight back
    # — the round-trip `tp submit` depends on to re-read a report it just wrote.
    cost_usd: float | None = None
    calls: int = 0


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
        return combine_costs(*(u.cost_usd for u in self.by_model))

    @computed_field
    @property
    def calls(self) -> int:
        return sum(u.calls for u in self.by_model)

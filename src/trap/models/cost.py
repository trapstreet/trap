from __future__ import annotations

from pydantic import BaseModel, ConfigDict, computed_field


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


class CallUsage(BaseModel):
    """One API call's token usage, normalised across vendors to the same four disjoint
    counts as :class:`ModelCost`, plus the 1-hour slice of the cache writes (Anthropic
    prices a 1-hour write apart from a 5-minute one). Internal to the cost proxy — the
    parser produces it, the price table prices it, and it is folded into a ModelCost;
    it never reaches the report, so it is deliberately not exported from ``models``."""

    model_config = ConfigDict(frozen=True)

    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_write_1h_tokens: int = 0  # the part of cache_write_tokens written with a 1-hour TTL

    @property
    def is_empty(self) -> bool:
        return not (
            self.prompt_tokens or self.completion_tokens or self.cache_read_tokens or self.cache_write_tokens
        )


class ModelCost(BaseModel):
    """One (provider, model) bucket of a case's LLM calls.

    The four token counts are DISJOINT, normalised across vendors: ``prompt_tokens`` is
    the uncached input only, so the input a bucket sent is ``prompt_tokens +
    cache_read_tokens + cache_write_tokens``. That is Anthropic's own split; vendors that
    report cached tokens as a subset of the prompt (OpenAI, DeepSeek, OpenRouter) have the
    cached part taken out of ``prompt_tokens``. The site's run context splits usage the
    same way (``input`` / ``cache_read`` / ``cache_creation``), and its server-side
    repricing multiplies the input count by the full input rate — which is only right for
    uncached input.
    """

    provider: str
    model: str | None = None
    prompt_tokens: int = 0  # uncached input
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    # None = unknown (unpriced model). An unknown cost is not a zero cost, so it
    # is never coerced to 0.0; it serialises to JSON null and loads straight back
    # — the round-trip `tp submit` depends on to re-read a report it just wrote.
    cost_usd: float | None = None
    # The part of cost_usd spent on cache reads and writes: already inside cost_usd, not
    # on top of it. None when cost_usd is None, and on a report written before cache
    # accounting existed (the cache spend was never measured, so it is unknown, not 0).
    cache_cost_usd: float | None = None
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
    def cache_read_tokens(self) -> int:
        return sum(u.cache_read_tokens for u in self.by_model)

    @computed_field
    @property
    def cache_write_tokens(self) -> int:
        return sum(u.cache_write_tokens for u in self.by_model)

    @computed_field
    @property
    def cost_usd(self) -> float | None:
        return combine_costs(*(u.cost_usd for u in self.by_model))

    @computed_field
    @property
    def cache_cost_usd(self) -> float | None:
        return combine_costs(*(u.cache_cost_usd for u in self.by_model))

    @computed_field
    @property
    def calls(self) -> int:
        return sum(u.calls for u in self.by_model)

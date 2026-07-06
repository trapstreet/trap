from __future__ import annotations

from pydantic import BaseModel, computed_field


class ModelCost(BaseModel):
    provider: str
    model: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
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
    def cost_usd(self) -> float:
        return sum(u.cost_usd for u in self.by_model)

    @computed_field
    @property
    def calls(self) -> int:
        return sum(u.calls for u in self.by_model)

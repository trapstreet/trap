"""Cost calculation for LLM token usage.

Isolated so this logic can be moved server-side without touching the proxy infrastructure.
The only public symbol is :func:`calculate_call_cost`.
"""

from __future__ import annotations

from tokencost import TOKEN_COSTS as _TOKEN_COSTS  # type: ignore[import-untyped]
from tokencost import calculate_cost_by_tokens as _calc_cost  # type: ignore[import-untyped]
from tokencost import register_model_pattern as _register_pattern  # type: ignore[import-untyped]


def _register_anthropic_version_patterns() -> None:
    # Anthropic now returns version-based names (e.g. "claude-sonnet-4-6") but tokencost only
    # has date-based entries (e.g. "claude-sonnet-4-20250514"). Register wildcard patterns so
    # calculate_cost_by_tokens can resolve them via _normalize_model_for_pricing.
    for pattern, canonical in [
        ("claude-sonnet-4-*", "claude-sonnet-4-20250514"),
        ("claude-opus-4-*", "claude-opus-4-20250514"),
    ]:
        entry = _TOKEN_COSTS.get(canonical)
        if entry:  # pragma: no branch - both canonicals ship in tokencost
            _register_pattern(
                pattern,
                float(entry["input_cost_per_token"]) * 1000,
                float(entry["output_cost_per_token"]) * 1000,
            )


_register_anthropic_version_patterns()


def calculate_call_cost(prompt_tokens: int, completion_tokens: int, model: str) -> float:
    """Return the USD cost for one API call. Returns 0.0 if the model is not in the pricing table."""
    try:
        return float(
            _calc_cost(prompt_tokens, model, "input") + _calc_cost(completion_tokens, model, "output")
        )
    except KeyError:
        return 0.0

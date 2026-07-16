"""Cost calculation for LLM token usage.

Isolated so this logic can be moved server-side without touching the proxy infrastructure.
The only public symbol is :func:`calculate_call_cost`.

Price data is served by the trapstreet site (GET /api/pricing) and cached locally — see
:mod:`trap.cost.pricing` for the source chain. The bundled table below is only the
last-resort fallback (fresh install, offline, no cache); it exists because the previous
third-party dependency (tokencost) lagged behind provider releases, silently pricing
current models at 0 or at a stale predecessor's rate. A NaN fallback keeps the failure
mode honest — an unknown cost is reported as unknown, never as a wrong number.
"""

from __future__ import annotations

import math
from functools import lru_cache

from trap.cost.pricing import PriceRow, get_price_rows

# USD per million tokens (input, output), keyed by model-id prefix so one entry covers
# both a version alias ("claude-haiku-4-5") and the dated full id the API reports
# ("claude-haiku-4-5-20251001"). First match wins: keep more-specific prefixes
# ("gpt-5.5-pro") ahead of the prefixes they extend ("gpt-5.5").
_PRICES_PER_MTOK: list[PriceRow] = [
    # Anthropic
    ("claude-fable-5", 10.0, 50.0),
    ("claude-mythos-5", 10.0, 50.0),
    # Introductory pricing through 2026-08-31; reverts to 3.0 / 15.0 after.
    ("claude-sonnet-5", 2.0, 10.0),
    ("claude-opus-4-8", 5.0, 25.0),
    ("claude-opus-4-7", 5.0, 25.0),
    ("claude-opus-4-6", 5.0, 25.0),
    ("claude-opus-4-5", 5.0, 25.0),
    ("claude-opus-4-1", 15.0, 75.0),
    ("claude-sonnet-4-6", 3.0, 15.0),
    ("claude-sonnet-4-5", 3.0, 15.0),
    ("claude-haiku-4-5", 1.0, 5.0),
    # OpenAI
    ("gpt-5.6-sol", 5.0, 30.0),
    ("gpt-5.6-terra", 2.5, 15.0),
    ("gpt-5.6-luna", 1.0, 6.0),
    ("gpt-5.5-pro", 30.0, 180.0),
    ("gpt-5.5", 5.0, 30.0),
    ("gpt-5.4-pro", 30.0, 180.0),
    ("gpt-5.4-mini", 0.75, 4.5),
    ("gpt-5.4-nano", 0.2, 1.25),
    ("gpt-5.4", 2.5, 15.0),
]


@lru_cache(maxsize=1)
def _price_rows() -> tuple[PriceRow, ...]:
    # resolved once per process (a run makes many calls; the table doesn't move under it)
    return tuple(get_price_rows(_PRICES_PER_MTOK))


def calculate_call_cost(prompt_tokens: int, completion_tokens: int, model: str | None) -> float:
    """Return the USD cost for one API call.

    Returns NaN when the model is unknown or absent from the pricing table — an
    unknown cost is not a zero cost (pydantic serialises the NaN as JSON null)."""
    if model is None:
        return math.nan
    name = model.lower()
    for prefix, input_per_mtok, output_per_mtok in _price_rows():
        if name.startswith(prefix):
            return (prompt_tokens * input_per_mtok + completion_tokens * output_per_mtok) / 1_000_000
    return math.nan

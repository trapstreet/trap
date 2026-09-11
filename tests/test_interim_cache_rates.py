"""The INTERIM cache-rate fallback (:mod:`trap.cost.interim_cache_rates`): each vendor's
documented cache multiples, used only for a rate the served price row lacks. Delete this
file with the module once ``GET /api/pricing`` serves cache rates."""

from __future__ import annotations

import pytest

from trap.cost.pricing import PriceRow, PriceTable
from trap.models.cost import CallUsage

M = 1_000_000  # one Mtok, so a row's per-Mtok rates read straight off the result


def _usage(model: str, read: int = 0, write: int = 0, write_1h: int = 0) -> CallUsage:
    return CallUsage(
        model=model,
        prompt_tokens=M,
        completion_tokens=M,
        cache_read_tokens=read,
        cache_write_tokens=write,
        cache_write_1h_tokens=write_1h,
    )


def _cache_usd(model: str, input_per_mtok: float, **counts: int) -> float | None:
    """The cache part of one call's cost, on a served row that carries no cache rates."""
    table = PriceTable(
        unit="usd_per_mtok",
        prices=[PriceRow(model_prefix=model, input_per_mtok=input_per_mtok, output_per_mtok=8.0)],
    )
    cost = table.cost_of(_usage(model, **counts))
    return None if cost is None else cost.cache_usd


def test_anthropic_multiples():
    # https://platform.claude.com/docs/en/about-claude/pricing — cache hits 0.1x base input,
    # 5-minute writes 1.25x, 1-hour writes 2x. Opus 5 at $5 in: $0.50 / $6.25 / $10.
    cache = _cache_usd("claude-opus-5", 5.0, read=M, write=M, write_1h=M // 4)
    assert cache == pytest.approx(0.50 + 0.75 * 6.25 + 0.25 * 10.0)


def test_fable_and_mythos_5_1_read_the_cache_at_a_quarter_of_the_usual_rate():
    # same page: "Cache hits and refreshes on Claude Fable 5.1 and Claude Mythos 5.1 are priced
    # at 0.025x the base input price" ($10 in → $0.25); writes keep 1.25x
    for model in ("claude-fable-5-1", "claude-mythos-5-1"):
        assert _cache_usd(model, 10.0, read=M, write=M) == pytest.approx(0.25 + 12.5), model
    assert _cache_usd("claude-fable-5", 10.0, read=M) == pytest.approx(1.0)  # 0.1x on the non-.1 model


def test_deepseek_hit_price():
    # https://api-docs.deepseek.com/quick_start/pricing (from 2026-09-10): a cache hit costs
    # 0.006 vs a 0.30 miss on deepseek-flash (v4-flash is billed as flash), 0.044 vs 1.32 on
    # deepseek-v4-pro — the same ratio peak and off-peak. A miss is plain input: no write fee.
    for model, miss, hit in [
        ("deepseek-flash", 0.30, 0.006),
        ("deepseek-v4-flash", 0.30, 0.006),
        ("deepseek-v4-pro", 1.32, 0.044),
    ]:
        assert _cache_usd(model, miss, read=M) == pytest.approx(hit), model


@pytest.mark.parametrize(
    ("model", "read", "write"),
    [
        # https://developers.openai.com/api/docs/pricing (cached ÷ input) and the prompt-caching
        # guide: "For GPT-5.6 and later, cache writes cost 1.25x the standard, uncached input-token
        # rate... subsequent reads cost only 0.1x"; earlier models have "No additional cache-write
        # charge", so a write there is plain input.
        ("gpt-6-astra", 0.1, 1.25),
        ("gpt-5.6-sol", 0.1, 1.25),
        ("gpt-5.6", 0.1, 1.25),
        ("gpt-5.5", 0.1, 1.0),
        ("gpt-5.4-mini", 0.1, 1.0),
        ("gpt-5.1-codex-max", 0.1, 1.0),
        ("gpt-5-nano", 0.1, 1.0),
        ("gpt-4.1-mini", 0.25, 1.0),
        ("gpt-4o", 0.5, 1.0),
        ("gpt-4o-mini", 0.5, 1.0),
        ("o1", 0.5, 1.0),
        ("o3-mini", 0.5, 1.0),
        ("o3", 0.25, 1.0),
        ("o4-mini", 0.25, 1.0),
        # "GPT-5.5 Pro does not offer a cached input discount": a cached token costs full input
        ("gpt-5.5-pro", 1.0, 1.0),
        ("gpt-5-pro", 1.0, 1.0),
        ("o1-pro", 1.0, 1.0),
        ("o3-pro", 1.0, 1.0),
        # https://platform.kimi.ai/docs/pricing/chat — cache hit vs miss: kimi-k3 0.30 vs 3.00,
        # kimi-k2.7-code(-highspeed) 0.19 vs 0.95 (0.38 vs 1.90), kimi-k2.6 0.16 vs 0.95; no write fee
        ("kimi-k3", 0.1, 1.0),
        ("kimi-k2.7-code-highspeed", 0.2, 1.0),
        ("kimi-k2.7-code", 0.2, 1.0),
        ("kimi-k2.6", 0.16 / 0.95, 1.0),
    ],
)
def test_openai_and_kimi_multiples(model, read, write):
    assert _cache_usd(model, 2.0, read=M, write=M) == pytest.approx(2.0 * (read + write)), model


@pytest.mark.parametrize("model", ["gpt-realtime-2.1", "codex-mini-latest", "mistral-large-2512"])
def test_families_without_one_documented_cache_rate_have_no_fallback(model):
    # gpt-realtime prices cached audio (0.0125x) and text (0.1x) apart, and the proxy cannot
    # tell them apart; Mistral's -90% is documented, but its native ids share no prefix scheme
    assert _cache_usd(model, 2.0, read=1) is None


@pytest.mark.parametrize(
    "model", ["anthropic/claude-sonnet-4.6", "~anthropic/claude-opus-latest", "openai/gpt-5.5"]
)
def test_routed_ids_have_no_fallback(model):
    # what a route charges only the served table knows — even on a maker's own route
    assert _cache_usd(model, 3.0, read=M) is None

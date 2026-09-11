"""Price-table source chain: fresh cache → server → stale cache → bundled.

All hermetic — the server is mocked at the httpx level and the cache lives in tmp_path
(the autouse ``hermetic_pricing`` fixture in conftest already points TRAP_PRICING_CACHE
there and stubs out the fetch seam; tests that exercise fetching restore the real one)."""

from __future__ import annotations

import json
import time

import pytest

from trap.cost import pricing
from trap.cost.pricing import PriceCatalogue, PriceRow, PriceTable
from trap.models.cost import CallUsage

# import-time reference — grabbed before the autouse fixture stubs the seam
_REAL_FETCH = PriceCatalogue._fetch

WIRE = {
    "version": 1,
    "updated_at": "2026-07-17",
    "unit": "usd_per_mtok",
    "prices": [
        {
            "provider": "openai",
            "model_prefix": "gpt-3.5-turbo",
            "input_per_mtok": 0.5,
            "output_per_mtok": 1.5,
        },
    ],
}


def _tuples(rows: list[PriceRow]) -> list[tuple[str, float, float]]:
    return [(r.model_prefix, r.input_per_mtok, r.output_per_mtok) for r in rows]


def _write_cache(tmp_path, fetched_at: float) -> None:
    (tmp_path / "pricing-cache.json").write_text(json.dumps({**WIRE, "fetched_at": fetched_at}))


def _mock_fetch(monkeypatch, payload=None, fail=False):
    class _Resp:
        def raise_for_status(self) -> None:
            if fail:
                raise pricing.httpx.HTTPError("boom")

        def json(self):
            return payload

    def _get(url: str, timeout: float):
        if fail:
            raise pricing.httpx.ConnectError("down")
        return _Resp()

    # restore the real fetch (conftest stubs it for everyone else), mock the wire
    monkeypatch.setattr(PriceCatalogue, "_fetch", _REAL_FETCH)
    monkeypatch.setattr(pricing.httpx, "get", _get)


def test_fresh_cache_wins_without_fetch(tmp_path, monkeypatch):
    _write_cache(tmp_path, time.time())

    def _no_fetch(*a, **k):  # pragma: no cover - failing loudly is the assertion
        raise AssertionError("must not hit the network when the cache is fresh")

    monkeypatch.setattr(PriceCatalogue, "_fetch", _REAL_FETCH)
    monkeypatch.setattr(pricing.httpx, "get", _no_fetch)
    assert _tuples(PriceCatalogue.resolve().prices) == [("gpt-3.5-turbo", 0.5, 1.5)]


def test_fetch_refreshes_and_writes_cache(tmp_path, monkeypatch):
    _mock_fetch(monkeypatch, payload=WIRE)
    assert _tuples(PriceCatalogue.resolve().prices) == [("gpt-3.5-turbo", 0.5, 1.5)]
    cached = json.loads((tmp_path / "pricing-cache.json").read_text())
    assert cached["fetched_at"] > 0
    # the parsed projection is cached, not the raw wire — extra fields (provider,
    # version, updated_at) are dropped
    assert cached["prices"] == [
        {"model_prefix": "gpt-3.5-turbo", "input_per_mtok": 0.5, "output_per_mtok": 1.5}
    ]


def test_stale_cache_beats_bundled_when_offline(tmp_path, monkeypatch):
    _write_cache(tmp_path, fetched_at=time.time() - 7 * 24 * 3600)  # a week old
    _mock_fetch(monkeypatch, fail=True)
    # stale cache is newer than the wheel
    assert _tuples(PriceCatalogue.resolve().prices) == [("gpt-3.5-turbo", 0.5, 1.5)]


def test_default_when_no_cache_and_offline(monkeypatch):
    _mock_fetch(monkeypatch, fail=True)
    prefixes = [r.model_prefix for r in PriceCatalogue.resolve().prices]
    assert "claude-opus-4-8" in prefixes and "gpt-5.4-nano" in prefixes


@pytest.mark.parametrize(
    "payload",
    [
        {"unit": "usd_per_token", "prices": WIRE["prices"]},  # wrong unit
        {"unit": "usd_per_mtok", "prices": []},  # empty
        {"unit": "usd_per_mtok", "prices": [{"model_prefix": "x"}]},  # missing rates
        "not-a-dict",
    ],
)
def test_malformed_wire_falls_through_to_default(monkeypatch, payload):
    _mock_fetch(monkeypatch, payload=payload)
    # a malformed server payload is ignored; the default table serves the run
    assert "claude-opus-4-8" in [r.model_prefix for r in PriceCatalogue.resolve().prices]


def test_default_table_is_shipped_and_valid():
    # the wheel resource parses and covers the known models the calculator tests rely on
    prefixes = [r.model_prefix for r in PriceCatalogue._default().prices]
    assert "gpt-5.5-pro" in prefixes  # more-specific prefix precedes "gpt-5.5"
    assert prefixes.index("gpt-5.5-pro") < prefixes.index("gpt-5.5")


def test_served_prefix_is_lowercased(monkeypatch):
    upper = {
        "unit": "usd_per_mtok",
        "prices": [{"model_prefix": "GPT-4o", "input_per_mtok": 1.0, "output_per_mtok": 2.0}],
    }
    _mock_fetch(monkeypatch, payload=upper)
    assert PriceCatalogue.resolve().prices[0].model_prefix == "gpt-4o"


def test_default_cache_path_without_override(monkeypatch):
    monkeypatch.delenv("TRAP_PRICING_CACHE", raising=False)
    assert PriceCatalogue._cache_path().parts[-3:] == (".config", "trapstreet", "pricing.json")


def test_corrupt_cache_falls_through(tmp_path):
    # cache file parses as JSON but isn't the wire shape → unusable → default
    (tmp_path / "pricing-cache.json").write_text(json.dumps({"unit": "usd_per_token", "prices": []}))
    assert "claude-opus-4-8" in [r.model_prefix for r in PriceCatalogue.resolve().prices]


def test_cache_write_failure_still_returns_fetched(tmp_path, monkeypatch):
    # cache path's parent is a FILE → mkdir raises OSError → swallowed, rows still served
    blocker = tmp_path / "blocker"
    blocker.write_text("")
    monkeypatch.setenv("TRAP_PRICING_CACHE", str(blocker / "pricing.json"))
    _mock_fetch(monkeypatch, payload=WIRE)
    assert _tuples(PriceCatalogue.resolve().prices) == [("gpt-3.5-turbo", 0.5, 1.5)]


def test_cost_of_uses_served_prices_end_to_end(tmp_path):
    # a model absent from the default table becomes priceable once served
    _write_cache(tmp_path, time.time())
    cost = PriceCatalogue.resolve().cost_of(_usage("gpt-3.5-turbo-0125", prompt=M, completion=M))
    assert cost is not None and cost.usd == pytest.approx(0.5 + 1.5)


# -- cache pricing ------------------------------------------------------------------

M = 1_000_000  # one Mtok, so a row's per-Mtok rates read straight off the result


def _usage(model: str | None, **counts: int) -> CallUsage:
    names = {"prompt": "prompt_tokens", "completion": "completion_tokens", "read": "cache_read_tokens"}
    names |= {"write": "cache_write_tokens", "write_1h": "cache_write_1h_tokens"}
    return CallUsage(model=model, **{names[k]: v for k, v in counts.items()})


def _table(*rows: dict) -> PriceTable:
    return PriceTable(unit="usd_per_mtok", prices=[PriceRow(**row) for row in rows])


def test_cache_rate_columns_are_optional_on_the_wire(monkeypatch):
    # a row may carry cache rates; one without them (every table served so far) still parses
    payload = {
        "unit": "usd_per_mtok",
        "prices": [
            {"model_prefix": "a", "input_per_mtok": 1, "output_per_mtok": 2, "cache_read_per_mtok": 0.1},
            {"model_prefix": "b", "input_per_mtok": 1, "output_per_mtok": 2, "cache_write_per_mtok": 1.5},
            {"model_prefix": "c", "input_per_mtok": 1, "output_per_mtok": 2},
        ],
    }
    _mock_fetch(monkeypatch, payload=payload)
    rows = PriceCatalogue.resolve().prices
    assert [(r.cache_read_per_mtok, r.cache_write_per_mtok) for r in rows] == [
        (0.1, None),
        (None, 1.5),
        (None, None),
    ]


def test_cache_rate_columns_survive_the_local_cache(tmp_path, monkeypatch):
    served = {
        "unit": "usd_per_mtok",
        "prices": [
            {"model_prefix": "a", "input_per_mtok": 1, "output_per_mtok": 2, "cache_read_per_mtok": 0.1},
            {"model_prefix": "b", "input_per_mtok": 1, "output_per_mtok": 2},
        ],
    }
    _mock_fetch(monkeypatch, payload=served)
    PriceCatalogue.resolve()
    cached = json.loads((tmp_path / "pricing-cache.json").read_text())["prices"]
    # a rate the server gave is kept; one it did not is left out, not written as null
    assert cached[0]["cache_read_per_mtok"] == 0.1
    assert "cache_read_per_mtok" not in cached[1] and "cache_write_per_mtok" not in cached[1]


def test_cost_of_prices_cache_tokens_at_the_rows_own_rates():
    table = _table(
        {
            "model_prefix": "mystery-model",
            "input_per_mtok": 2.0,
            "output_per_mtok": 8.0,
            "cache_read_per_mtok": 0.3,
            "cache_write_per_mtok": 2.7,
        }
    )
    cost = table.cost_of(_usage("mystery-model", prompt=M, completion=M, read=M, write=M))
    assert cost is not None
    assert cost.usd == pytest.approx(2.0 + 8.0 + 0.3 + 2.7)
    assert cost.cache_usd == pytest.approx(0.3 + 2.7)


def test_cost_of_without_cache_tokens_is_the_plain_arithmetic():
    # no cached tokens → no cache rate needed, even for a vendor with no cache price on file
    table = _table({"model_prefix": "mystery-model", "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    cost = table.cost_of(_usage("mystery-model", prompt=3 * M, completion=M))
    assert cost is not None
    assert (cost.usd, cost.cache_usd) == (pytest.approx(14.0), 0.0)


@pytest.mark.parametrize("counts", [{"read": 1}, {"write": 1}])
def test_cost_of_cache_tokens_at_an_unknown_rate_is_unknown(counts):
    # a priced model whose cache rate neither the row nor a documented vendor multiplier
    # gives: the call's cost is unknown (None) — never priced at the full input rate,
    # never at zero. Both would be a wrong number.
    table = _table({"model_prefix": "mystery-model", "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    assert table.cost_of(_usage("mystery-model", prompt=M, **counts)) is None


def test_anthropic_rows_without_cache_rates_use_the_documented_multipliers():
    # https://platform.claude.com/docs/en/about-claude/pricing — cache hits 0.1x base input,
    # 5-minute writes 1.25x, 1-hour writes 2x. Opus 5 at $5 in: $0.50 / $6.25 / $10.
    table = _table({"model_prefix": "claude-opus-5", "input_per_mtok": 5.0, "output_per_mtok": 25.0})
    usage = _usage("claude-opus-5", prompt=M, completion=M, read=M, write=M, write_1h=M // 4)
    cost = table.cost_of(usage)
    assert cost is not None
    assert cost.cache_usd == pytest.approx(0.50 + 0.75 * 6.25 + 0.25 * 10.0)
    assert cost.usd == pytest.approx(5.0 + 25.0 + cost.cache_usd)


def test_fable_and_mythos_5_1_read_the_cache_at_a_quarter_of_the_usual_rate():
    # same page: "Cache hits and refreshes on Claude Fable 5.1 and Claude Mythos 5.1 are priced
    # at 0.025x the base input price" ($10 in → $0.25); writes keep 1.25x / 2x
    table = _table(
        {"model_prefix": "claude-fable-5", "input_per_mtok": 10.0, "output_per_mtok": 50.0},
        {"model_prefix": "anthropic/claude-fable-5", "input_per_mtok": 10.0, "output_per_mtok": 50.0},
    )
    for model in ("claude-fable-5-1", "anthropic/claude-fable-5.1"):
        cost = table.cost_of(_usage(model, read=M, write=M))
        assert cost is not None and cost.cache_usd == pytest.approx(0.25 + 12.5), model
    fable_5 = table.cost_of(_usage("claude-fable-5", read=M))
    assert fable_5 is not None and fable_5.cache_usd == pytest.approx(1.0)  # 0.1x on the non-.1 model


def test_deepseek_rows_without_cache_rates_use_the_documented_hit_price():
    # https://api-docs.deepseek.com/quick_start/pricing (from 2026-09-10): a cache hit costs
    # 0.006 vs a 0.30 miss on deepseek-flash (v4-flash is billed as flash), 0.044 vs 1.32 on
    # deepseek-v4-pro — the same ratio peak and off-peak. A miss is plain input: no write fee.
    table = _table(
        {"model_prefix": "deepseek-flash", "input_per_mtok": 0.30, "output_per_mtok": 1.20},
        {"model_prefix": "deepseek-v4-flash", "input_per_mtok": 0.30, "output_per_mtok": 1.20},
        {"model_prefix": "deepseek-v4-pro", "input_per_mtok": 1.32, "output_per_mtok": 3.96},
    )
    for model, hit in [("deepseek-flash", 0.006), ("deepseek-v4-flash", 0.006), ("deepseek-v4-pro", 0.044)]:
        cost = table.cost_of(_usage(model, prompt=M, read=M))
        assert cost is not None and cost.cache_usd == pytest.approx(hit), model


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
        ("openai/gpt-5.5", 0.1, 1.0),  # OpenRouter's first-party route
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
def test_openai_and_kimi_rows_without_cache_rates_use_the_documented_multipliers(model, read, write):
    table = _table({"model_prefix": model, "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    cost = table.cost_of(_usage(model, read=M, write=M))
    assert cost is not None and cost.cache_usd == pytest.approx(2.0 * (read + write)), model


@pytest.mark.parametrize("model", ["gpt-realtime-2.1", "codex-mini-latest", "mistral-large-2512"])
def test_families_without_one_documented_cache_rate_are_unknown(model):
    # gpt-realtime prices cached audio (0.0125x) and text (0.1x) apart, and the proxy cannot
    # tell them apart; Mistral's -90% is documented, but its native ids share no prefix scheme
    table = _table({"model_prefix": model, "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    assert table.cost_of(_usage(model, prompt=M, read=1)) is None


def test_a_rows_own_cache_rate_beats_the_vendor_multiplier():
    row = {"model_prefix": "claude-opus-5", "input_per_mtok": 5.0, "output_per_mtok": 25.0}
    table = _table(row | {"cache_read_per_mtok": 0.4})
    cost = table.cost_of(_usage("claude-opus-5", read=M, write=M))
    assert cost is not None and cost.cache_usd == pytest.approx(0.4 + 6.25)  # served read, 1.25x write


def test_routed_ids_take_vendor_rates_only_from_first_party_routes():
    # OpenRouter serves anthropic/ and openai/ models only at their makers' cache rates, so
    # those routed ids take the vendor multipliers ...
    table = _table(
        {"model_prefix": "anthropic/claude-sonnet-4.6", "input_per_mtok": 3.0, "output_per_mtok": 15.0},
        {"model_prefix": "~anthropic/claude-opus-latest", "input_per_mtok": 5.0, "output_per_mtok": 25.0},
        {"model_prefix": "deepseek/deepseek-v4-flash", "input_per_mtok": 0.07, "output_per_mtok": 0.14},
    )
    sonnet = table.cost_of(_usage("anthropic/claude-sonnet-4.6", read=M))
    latest = table.cost_of(_usage("~anthropic/claude-opus-latest", read=M))
    assert sonnet is not None and sonnet.cache_usd == pytest.approx(0.3)
    assert latest is not None and latest.cache_usd == pytest.approx(0.5)
    # ... but an open-weight model is resold by many hosts at their own cache prices
    # (OpenRouter's deepseek/deepseek-v3.2 reads at 0.5x, not DeepSeek's), so a routed
    # deepseek/ id is not priced at DeepSeek's native ratio: its cache hits are unknown
    assert table.cost_of(_usage("deepseek/deepseek-v4-flash", prompt=M, read=M)) is None


def test_cost_of_unknown_model_is_none():
    table = _table({"model_prefix": "mystery-model", "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    assert table.cost_of(_usage("other-model", prompt=1)) is None
    assert table.cost_of(_usage(None, prompt=1)) is None

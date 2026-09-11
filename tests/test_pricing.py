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
            {"model_prefix": "c", "input_per_mtok": 1, "output_per_mtok": 2, "cache_write_1h_per_mtok": 2.0},
            {"model_prefix": "d", "input_per_mtok": 1, "output_per_mtok": 2},
        ],
    }
    _mock_fetch(monkeypatch, payload=payload)
    rows = PriceCatalogue.resolve().prices
    assert [(r.cache_read_per_mtok, r.cache_write_per_mtok, r.cache_write_1h_per_mtok) for r in rows] == [
        (0.1, None, None),
        (None, 1.5, None),
        (None, None, 2.0),
        (None, None, None),
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


def test_a_served_1h_write_rate_prices_the_1h_slice_of_the_writes():
    # Anthropic prices a 1-hour cache write apart from a 5-minute one, and so can the table
    table = _table(
        {
            "model_prefix": "mystery-model",
            "input_per_mtok": 2.0,
            "output_per_mtok": 8.0,
            "cache_write_per_mtok": 2.5,
            "cache_write_1h_per_mtok": 4.0,
        }
    )
    cost = table.cost_of(_usage("mystery-model", write=M, write_1h=M // 4))
    assert cost is not None and cost.cache_usd == pytest.approx(0.75 * 2.5 + 0.25 * 4.0)


def test_each_cache_rate_is_the_served_one_else_the_interim_multiple():
    # per rate, not per row: a Claude row that serves read and write but not yet the 1-hour
    # rate (the likely first shape the site ships) still prices a 1-hour write, at the
    # interim 2x; the two rates it does serve beat the interim 0.1x / 1.25x
    row = {"model_prefix": "claude-opus-5", "input_per_mtok": 5.0, "output_per_mtok": 25.0}
    table = _table(row | {"cache_read_per_mtok": 0.4, "cache_write_per_mtok": 6.0})
    cost = table.cost_of(_usage("claude-opus-5", read=M, write=M, write_1h=M // 2))
    assert cost is not None
    assert cost.cache_usd == pytest.approx(0.4 + 0.5 * 6.0 + 0.5 * 10.0)


def test_routed_ids_price_their_cache_from_the_served_table_only():
    # a routed id ("vendor/model", OpenRouter's shape) pays what the route charges, which only
    # the served table knows: with served rates it is priced ...
    routed = {"model_prefix": "anthropic/claude-sonnet-4.6", "input_per_mtok": 3.0, "output_per_mtok": 15.0}
    served = _table(routed | {"cache_read_per_mtok": 0.3, "cache_write_per_mtok": 3.75})
    cost = served.cost_of(_usage("anthropic/claude-sonnet-4.6", read=M, write=M))
    assert cost is not None and cost.cache_usd == pytest.approx(0.3 + 3.75)
    # ... and without them its cache tokens are unknown: no interim fallback for a route
    assert _table(routed).cost_of(_usage("anthropic/claude-sonnet-4.6", prompt=M, read=M)) is None


def test_cost_of_unknown_model_is_none():
    table = _table({"model_prefix": "mystery-model", "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    assert table.cost_of(_usage("other-model", prompt=1)) is None
    assert table.cost_of(_usage(None, prompt=1)) is None

"""Price-table source chain: fresh cache → server → stale cache → bundled.

All hermetic — the server is mocked at the httpx level and the cache lives in tmp_path
(the autouse ``hermetic_pricing`` fixture in conftest already points TRAP_PRICING_CACHE
there and stubs out the fetch seam; tests that exercise fetching restore the real one)."""

from __future__ import annotations

import json
import time

import pytest

from trap.cost import pricing
from trap.cost.pricing import PriceCatalogue, PriceRow

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
    cost = PriceCatalogue.resolve().cost_of(1_000_000, 1_000_000, "gpt-3.5-turbo-0125")
    assert cost == pytest.approx(0.5 + 1.5)

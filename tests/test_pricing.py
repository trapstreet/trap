"""Price-table source chain: fresh cache → server → stale cache → bundled.

All hermetic — the server is mocked at httpx level, the cache lives in tmp_path
(the autouse fixture in conftest already points TRAP_PRICING_CACHE there and
stubs out the fetch seam; tests that exercise fetching restore the real one)."""

from __future__ import annotations

import json
import time

import pytest

from trap.cost import pricing
from trap.cost.calculator import calculate_call_cost

# import-time reference — grabbed before the autouse fixture stubs the seam
_REAL_FETCH = pricing._fetch_and_cache

BUNDLED: list[pricing.PriceRow] = [("bundled-model", 1.0, 2.0)]

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
    monkeypatch.setattr(pricing, "_fetch_and_cache", _REAL_FETCH)
    monkeypatch.setattr(pricing.httpx, "get", _get)


def test_fresh_cache_wins_without_fetch(tmp_path, monkeypatch):
    _write_cache(tmp_path, time.time())

    def _no_fetch(*a, **k):  # pragma: no cover - failing loudly is the assertion
        raise AssertionError("must not hit the network when the cache is fresh")

    monkeypatch.setattr(pricing, "_fetch_and_cache", _REAL_FETCH)
    monkeypatch.setattr(pricing.httpx, "get", _no_fetch)
    rows = pricing.get_price_rows(BUNDLED)
    assert rows[0][0] == "gpt-3.5-turbo"


def test_fetch_refreshes_and_writes_cache(tmp_path, monkeypatch):
    _mock_fetch(monkeypatch, payload=WIRE)
    rows = pricing.get_price_rows(BUNDLED)
    assert rows == [("gpt-3.5-turbo", 0.5, 1.5)]
    cached = json.loads((tmp_path / "pricing-cache.json").read_text())
    assert cached["fetched_at"] > 0 and cached["prices"] == WIRE["prices"]


def test_stale_cache_beats_bundled_when_offline(tmp_path, monkeypatch):
    _write_cache(tmp_path, fetched_at=time.time() - 7 * 24 * 3600)  # a week old
    _mock_fetch(monkeypatch, fail=True)
    rows = pricing.get_price_rows(BUNDLED)
    assert rows[0][0] == "gpt-3.5-turbo"  # stale cache is newer than the wheel


def test_bundled_when_no_cache_and_offline(monkeypatch):
    _mock_fetch(monkeypatch, fail=True)
    assert pricing.get_price_rows(BUNDLED) == BUNDLED


@pytest.mark.parametrize(
    "payload",
    [
        {"unit": "usd_per_token", "prices": WIRE["prices"]},  # wrong unit
        {"unit": "usd_per_mtok", "prices": []},  # empty
        {"unit": "usd_per_mtok", "prices": [{"model_prefix": "x"}]},  # missing rates
        "not-a-dict",
    ],
)
def test_malformed_wire_falls_through(monkeypatch, payload):
    _mock_fetch(monkeypatch, payload=payload)
    assert pricing.get_price_rows(BUNDLED) == BUNDLED


def test_calculator_uses_served_prices_end_to_end(tmp_path, monkeypatch):
    # a model absent from the bundled table becomes priceable once served
    _write_cache(tmp_path, time.time())
    cost = calculate_call_cost(1_000_000, 1_000_000, "gpt-3.5-turbo-0125")
    assert cost == pytest.approx(0.5 + 1.5)

"""Price-table sourcing: server → local cache → bundled fallback.

The trapstreet site is the source of truth for model prices (GET /api/pricing);
a price update is a data change there, not a CLI release. This module keeps the
CLI fully offline-capable: the fetch is best-effort with a short timeout, a
stale local cache beats the bundled table (it is newer than the wheel), and any
failure falls through silently — pricing must never break or slow down a run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

DEFAULT_SERVER = "https://trapstreet.run"
_CACHE_TTL_SECONDS = 24 * 3600
_FETCH_TIMEOUT_SECONDS = 3.0

# (model-id prefix, USD per Mtok input, USD per Mtok output) — first match wins.
PriceRow = tuple[str, float, float]


def _cache_path() -> Path:
    override = os.environ.get("TRAP_PRICING_CACHE")
    if override:
        return Path(override)
    return Path.home() / ".config" / "trapstreet" / "pricing.json"


def _server_url() -> str:
    return (os.environ.get("TRAPSTREET_URL") or DEFAULT_SERVER).rstrip("/")


def _rows_from_wire(payload: Any) -> list[PriceRow] | None:
    """Parse the /api/pricing wire shape; None on anything unexpected.

    Order is preserved — first-prefix-match-wins is part of the contract."""
    try:
        if payload.get("unit") != "usd_per_mtok":
            return None
        rows = [
            (str(p["model_prefix"]).lower(), float(p["input_per_mtok"]), float(p["output_per_mtok"]))
            for p in payload["prices"]
        ]
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return rows or None


def _read_cache() -> tuple[list[PriceRow], float] | None:
    """Return (rows, fetched_at) from the local cache, or None if unusable."""
    try:
        raw = json.loads(_cache_path().read_text())
        rows = _rows_from_wire(raw)
        if rows is None:
            return None
        return rows, float(raw.get("fetched_at", 0.0))
    except (OSError, ValueError):
        return None


def _fetch_and_cache() -> list[PriceRow] | None:
    """Best-effort fetch from the server; cache on success, None on any failure."""
    try:
        resp = httpx.get(f"{_server_url()}/api/pricing", timeout=_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    rows = _rows_from_wire(payload)
    if rows is None:
        return None
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**payload, "fetched_at": time.time()}, indent=2))
    except OSError:
        pass  # a cache-write failure must not break pricing
    return rows


def get_price_rows(bundled: list[PriceRow]) -> list[PriceRow]:
    """Resolve the price table: fresh cache → server → stale cache → bundled."""
    cached = _read_cache()
    if cached is not None and time.time() - cached[1] < _CACHE_TTL_SECONDS:
        return cached[0]
    fetched = _fetch_and_cache()
    if fetched is not None:
        return fetched
    if cached is not None:
        return cached[0]
    return bundled

"""Price-table sourcing: server → local cache → bundled fallback.

The trapstreet site is the source of truth for model prices (GET /api/pricing);
a price update is a data change there, not a CLI release. This module keeps the
CLI fully offline-capable: the fetch is best-effort with a short timeout, a
stale local cache beats the default table (it is newer than the wheel), and any
failure falls through silently — pricing must never break or slow down a run.

The three sources share one shape (:class:`PriceTable`) and one parse path: the
server response, the on-disk cache, and the shipped ``default_prices.json`` are
byte-for-byte the same JSON contract. ``default_prices.json`` is a captured
snapshot of ``GET /api/pricing`` — regenerate it by re-fetching the endpoint, not
by hand — so the offline default is exactly what the server most recently served.

Prices are served rather than pulled from a third-party table (the previous
dependency, tokencost, lagged behind provider releases and silently priced current
models at 0 or a stale predecessor's rate): a price update is a data change on the
server, and :meth:`PriceTable.cost_of` reports an unknown model as ``None``, never
as a wrong number.

Cache rates are served the same way: ``cache_read_per_mtok``, ``cache_write_per_mtok`` and
``cache_write_1h_per_mtok`` on a row. A rate a row does not serve yet is filled from the
INTERIM fallback in :mod:`trap.cost.interim_cache_rates` (a stopgap, deleted once the site
serves them), and cached tokens neither can price make the call's cost unknown rather than
guessed. This module parses and computes; it holds no prices of its own.
"""

from __future__ import annotations

import json
import os
import time
from importlib.resources import files
from pathlib import Path
from typing import Literal, NamedTuple

import httpx
from pydantic import BaseModel, ValidationError, field_validator

from trap.cost.interim_cache_rates import CacheMultipliers, interim_multiples
from trap.models.cost import CallUsage


class CallCost(NamedTuple):
    """What one call cost, in USD. ``cache_usd`` is the part of ``usd`` spent on cache
    reads and writes — included in ``usd``, not on top of it."""

    usd: float
    cache_usd: float


class PriceRow(BaseModel):
    """One priced model-id prefix. Extra fields on the wire (``provider``, ``note``, …)
    are ignored — only the prefix and its rates are used. The cache rates are optional:
    a table served before the site priced cache prices without them, and an older client
    ignores them."""

    model_prefix: str
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float | None = None
    cache_write_per_mtok: float | None = None  # a standard (Anthropic: 5-minute) cache write
    cache_write_1h_per_mtok: float | None = None  # a 1-hour cache write (Anthropic)

    @field_validator("model_prefix")
    @classmethod
    def _normalise(cls, value: str) -> str:
        # matched against a lowercased model id, so store the prefix lowercased
        return value.lower()

    def cache_rates(
        self, fallback: CacheMultipliers | None
    ) -> tuple[float | None, float | None, float | None]:
        """(read, write, 1-hour write) in USD per Mtok. Each is the rate the row serves, else
        the interim ``fallback`` multiple of the row's input rate, else None (unknown)."""
        base = self.input_per_mtok

        def rate(served: float | None, multiple: float | None) -> float | None:
            if served is not None:
                return served
            return None if multiple is None else multiple * base

        return (
            rate(self.cache_read_per_mtok, fallback and fallback.read),
            rate(self.cache_write_per_mtok, fallback and fallback.write),
            rate(self.cache_write_1h_per_mtok, fallback and fallback.write_1h),
        )


class PriceTable(BaseModel):
    """The /api/pricing wire shape, shared by the server response, the on-disk cache, and
    the bundled fallback. Order is preserved — first-prefix-match-wins is part of the
    contract, so more-specific prefixes must precede the ones they extend. ``fetched_at`` is
    stamped locally when cached; absent elsewhere (0.0, i.e. always stale)."""

    # Guards the unit baked into cost_of's ``/ 1_000_000``: a payload in any other
    # unit is rejected (→ fallthrough) rather than misread by a factor of 1000+.
    unit: Literal["usd_per_mtok"]
    prices: list[PriceRow]
    # epoch seconds (``time.time()``), for direct TTL arithmetic in :meth:`is_fresh`; a
    # machine field, unlike the human-readable ``updated_at`` on the wire. 0.0 = always stale.
    fetched_at: float = 0.0

    @classmethod
    def parse(cls, payload: object) -> PriceTable | None:
        """Validate an untrusted payload (server or cache); None on anything unexpected —
        a wrong unit, missing rates, an empty table, or a non-object all fall through."""
        try:
            table = cls.model_validate(payload)
        except ValidationError:
            return None
        return table if table.prices else None

    def is_fresh(self, ttl_seconds: float) -> bool:
        return time.time() - self.fetched_at < ttl_seconds

    def cost_of(self, usage: CallUsage) -> CallCost | None:
        """What one API call cost against this table, or ``None`` when that is unknown —
        the model is absent or unpriced, or it has cached tokens at a rate neither its row
        nor its vendor's documented multipliers give. An unknown cost is not a zero cost,
        and is reported as JSON null rather than a misleading number. First matching prefix
        wins (order is load-bearing; see :meth:`PriceCatalogue._default`)."""
        if usage.model is None:
            return None
        name = usage.model.lower()
        row = next((row for row in self.prices if name.startswith(row.model_prefix)), None)
        if row is None:
            return None
        read, write, write_1h = row.cache_rates(interim_multiples(name))
        cache = 0.0
        for tokens, rate in (
            (usage.cache_read_tokens, read),
            (usage.cache_write_tokens - usage.cache_write_1h_tokens, write),
            (usage.cache_write_1h_tokens, write_1h),
        ):
            if not tokens:
                continue
            if rate is None:
                return None
            cache += tokens * rate
        plain = usage.prompt_tokens * row.input_per_mtok + usage.completion_tokens * row.output_per_mtok
        return CallCost(usd=(plain + cache) / 1_000_000, cache_usd=cache / 1_000_000)


class PriceCatalogue:
    """Picks the price table from a source chain that never blocks or breaks a run:
    fresh cache → server fetch → stale cache → default fallback. Stateless — every method
    is a classmethod and :meth:`resolve` returns a :class:`PriceTable` the caller holds
    onto (the proxy resolves once per run, then prices every call against that table)."""

    # Kept local rather than imported from ``trap.auth``: the package graph runs
    # cost → models only, never cost → auth (see CODE_MAP.md).
    DEFAULT_SERVER = "https://trapstreet.run"
    CACHE_TTL_SECONDS = 24 * 3600
    FETCH_TIMEOUT_SECONDS = 3.0
    DEFAULT_RESOURCE = "default_prices.json"

    @classmethod
    def resolve(cls) -> PriceTable:
        """Resolve the table: fresh cache → server fetch → stale cache → default fallback."""
        cached = cls._read_cache()
        if cached is not None and cached.is_fresh(cls.CACHE_TTL_SECONDS):
            return cached
        fetched = cls._fetch()
        if fetched is not None:
            return fetched
        if cached is not None:
            return cached
        return cls._default()

    @classmethod
    def _default(cls) -> PriceTable:
        """The last-resort table shipped in the wheel (a captured ``/api/pricing`` snapshot).
        It is our own data, not untrusted input, so a missing or malformed resource is a
        build bug that must surface loudly.

        Row order is load-bearing and preserved verbatim: lookup is first-prefix-match-wins
        (see :meth:`PriceTable.cost_of`), so a more-specific prefix must appear before the
        one it extends — e.g. ``gpt-5.5-pro`` before ``gpt-5.5``, or the ``-pro`` model would
        silently match its parent's cheaper rate. The endpoint already serves rows
        specific-first; keep that order when regenerating and do NOT re-sort the file (a
        plain alphabetical sort inverts every such pair)."""
        raw = files(__package__).joinpath(cls.DEFAULT_RESOURCE).read_text()
        return PriceTable.model_validate(json.loads(raw))

    @classmethod
    def _fetch(cls) -> PriceTable | None:
        """Best-effort fetch from the server; cache on success, None on any failure."""
        try:
            resp = httpx.get(f"{cls._server_url()}/api/pricing", timeout=cls.FETCH_TIMEOUT_SECONDS)
            resp.raise_for_status()
            table = PriceTable.parse(resp.json())
        except (httpx.HTTPError, ValueError):
            return None
        if table is None:
            return None
        table.fetched_at = time.time()
        cls._write_cache(table)
        return table

    @classmethod
    def _read_cache(cls) -> PriceTable | None:
        """The locally cached table, or None if it is absent, unreadable, or stale-shaped."""
        try:
            raw = json.loads(cls._cache_path().read_text())
        except (OSError, ValueError):
            return None
        return PriceTable.parse(raw)

    @classmethod
    def _write_cache(cls, table: PriceTable) -> None:
        """Best-effort — a cache-write failure must not break pricing."""
        try:
            path = cls._cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # exclude_none: a cache rate the server did not give is left out, not written as null
            path.write_text(table.model_dump_json(indent=2, exclude_none=True))
        except OSError:
            pass

    @classmethod
    def _cache_path(cls) -> Path:
        override = os.environ.get("TRAP_PRICING_CACHE")
        return Path(override) if override else Path.home() / ".config" / "trapstreet" / "pricing.json"

    @classmethod
    def _server_url(cls) -> str:
        return (os.environ.get("TRAPSTREET_URL") or cls.DEFAULT_SERVER).rstrip("/")

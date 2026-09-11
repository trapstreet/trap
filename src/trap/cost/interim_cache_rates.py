"""INTERIM cache-rate fallback — delete this module once ``GET /api/pricing`` serves cache rates.

The site's price table is the one source of cache prices, as of every other price: a row's
``cache_read_per_mtok`` / ``cache_write_per_mtok`` / ``cache_write_1h_per_mtok``. Until the
site serves them, a rate a row lacks is taken here, as the vendor's documented multiple of
that row's own input rate. This is vendor pricing copied into the CLI, and it goes stale
when a vendor reprices — which is why it is a stopgap, not a second source:

- a rate the row serves always wins; this only fills a missing one;
- native model ids only — a routed id ("vendor/model", OpenRouter's shape) gets nothing
  here, since what a route charges is only known to the served table;
- a family absent here has no known cache price, and cached tokens on it price as unknown.

To retire it: once the site serves the three columns, delete this file and
``tests/test_interim_cache_rates.py``, and drop the ``fallback`` argument from
:meth:`trap.cost.pricing.PriceRow.cache_rates`.
"""

from __future__ import annotations

from typing import NamedTuple


class CacheMultipliers(NamedTuple):
    """A vendor's cache prices as multiples of the model's own input rate."""

    read: float
    write: float  # a standard cache write (Anthropic: the 5-minute TTL)
    write_1h: float  # a 1-hour cache write; only Anthropic reports these, elsewhere == write


def _reads_at(read: float, write: float = 1.0) -> CacheMultipliers:
    """A vendor with no 1-hour TTL (every one but Anthropic); ``write=1.0`` = no write fee."""
    return CacheMultipliers(read=read, write=write, write_1h=write)


_ANTHROPIC = CacheMultipliers(read=0.1, write=1.25, write_1h=2.0)
_ANTHROPIC_5_1 = CacheMultipliers(read=0.025, write=1.25, write_1h=2.0)

# Documented multiples as of 2026-09-11, per model family. First prefix wins, so a more
# specific family precedes the one it extends.
_MULTIPLES: tuple[tuple[str, CacheMultipliers], ...] = (
    # https://platform.claude.com/docs/en/about-claude/pricing — "5m cache writes: 1.25x base
    # input price · 1h cache writes: 2x · Cache hits & refreshes: 0.1x (0.025x on Claude Fable
    # 5.1 and Claude Mythos 5.1)".
    ("claude-fable-5-1", _ANTHROPIC_5_1),
    ("claude-mythos-5-1", _ANTHROPIC_5_1),
    ("claude-", _ANTHROPIC),
    # https://developers.openai.com/api/docs/pricing (cached ÷ input rate) and the prompt-caching
    # guide: "For GPT-5.6 and later, cache writes cost 1.25x the standard, uncached input-token
    # rate... subsequent reads cost only 0.1x"; every earlier model has "No additional
    # cache-write charge". The -pro models have no cached price at all ("GPT-5.5 Pro does not
    # offer a cached input discount"), so a cached token there is plain input. gpt-realtime is
    # left out: it prices cached audio (0.0125x) and text (0.1x) apart.
    ("gpt-6", _reads_at(0.1, write=1.25)),
    ("gpt-5.6", _reads_at(0.1, write=1.25)),
    ("gpt-5-pro", _reads_at(1.0)),
    ("gpt-5.2-pro", _reads_at(1.0)),
    ("gpt-5.4-pro", _reads_at(1.0)),
    ("gpt-5.5-pro", _reads_at(1.0)),
    ("gpt-5", _reads_at(0.1)),
    ("gpt-4.1", _reads_at(0.25)),
    ("gpt-4o", _reads_at(0.5)),
    ("o1-pro", _reads_at(1.0)),
    ("o3-pro", _reads_at(1.0)),
    ("o1", _reads_at(0.5)),
    ("o3-mini", _reads_at(0.5)),
    ("o3", _reads_at(0.25)),
    ("o4-mini", _reads_at(0.25)),
    # https://api-docs.deepseek.com/quick_start/pricing (effective 2026-09-10), USD/Mtok input
    # cache hit vs miss: deepseek-flash 0.006 vs 0.30, deepseek-v4-pro 0.044 vs 1.32 (off-peak
    # halves both, keeping the ratio); deepseek-v4-flash is "billed at the Flash price". A miss
    # is plain input — DeepSeek charges no cache write.
    ("deepseek-v4-pro", _reads_at(0.044 / 1.32)),
    ("deepseek-v4-flash", _reads_at(0.006 / 0.30)),
    ("deepseek-flash", _reads_at(0.006 / 0.30)),
    # https://platform.kimi.ai/docs/pricing/chat, USD/Mtok cache hit vs miss: kimi-k3 0.30 vs
    # 3.00, kimi-k2.7-code 0.19 vs 0.95 (-highspeed 0.38 vs 1.90), kimi-k2.6 0.16 vs 0.95; no
    # cache write or storage fee.
    ("kimi-k3", _reads_at(0.1)),
    ("kimi-k2.7-code", _reads_at(0.2)),
    ("kimi-k2.6", _reads_at(0.16 / 0.95)),
)


def interim_multiples(model: str) -> CacheMultipliers | None:
    """The documented multiples for a native ``model`` id (lowercased), or None — always
    None for a routed ``vendor/model`` id."""
    if "/" in model:
        return None
    return next((multiples for prefix, multiples in _MULTIPLES if model.startswith(prefix)), None)

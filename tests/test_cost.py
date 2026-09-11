from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from trap.cost.pricing import PriceCatalogue
from trap.cost.providers import _CONFIGS, active_provider_configs
from trap.cost.proxy import CostProxy
from trap.models.cost import CallUsage, CaseCost


def _usage(model: str | None, prompt: int = 0, completion: int = 0, **cache: int) -> CallUsage:
    return CallUsage(model=model, prompt_tokens=prompt, completion_tokens=completion, **cache)


def test_calculator_unknown_is_none():
    # unknown cost is not zero cost — unpriced models report None (JSON null)
    assert PriceCatalogue.resolve().cost_of(_usage("totally-unknown-model-xyz", 100, 50)) is None
    assert PriceCatalogue.resolve().cost_of(_usage(None, 100, 50)) is None


def test_calculator_prices():
    # prefixes resolve both version aliases and dated full ids to the exact
    # published prices (USD for 1M input + 1M output tokens)
    for model, expected in [
        ("claude-fable-5", 60.0),
        ("claude-sonnet-5", 12.0),  # introductory pricing through 2026-08-31
        ("claude-opus-4-8", 30.0),
        ("claude-opus-4-6", 30.0),
        ("claude-opus-4-1-20250805", 90.0),
        ("claude-sonnet-4-6", 18.0),
        ("claude-haiku-4-5-20251001", 6.0),
        ("gpt-5.5", 35.0),
        ("gpt-5.5-pro", 210.0),  # longer prefix wins over its parent "gpt-5.5"
        ("gpt-5.4-mini", 5.25),
    ]:
        cost = PriceCatalogue.resolve().cost_of(_usage(model, 1_000_000, 1_000_000))
        assert cost is not None and cost.usd == pytest.approx(expected), model
        assert cost.cache_usd == 0.0


KEYED = ["MISTRAL_API_KEY", "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY"]


def test_registry_active(monkeypatch):
    for key in KEYED:
        monkeypatch.delenv(key, raising=False)
    keyed = {"mistral", "moonshot", "deepseek", "deepseek-search", "openrouter"}
    assert not keyed & set(active_provider_configs())
    for key in KEYED:
        monkeypatch.setenv(key, "k")
    active = active_provider_configs()
    assert keyed <= set(active)  # key set
    assert "anthropic" in active and "openai" in active  # always_intercept


def test_openrouter_redirects_both_base_url_vars(monkeypatch):
    # litellm (and so Aider) reads OPENROUTER_API_BASE; OpenRouter's TypeScript SDK reads
    # OPENROUTER_BASE_URL. Both point at the one OpenRouter port.
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    proxy = CostProxy()
    proxy.start()  # stop() waits on the serve loops, so they must be running
    try:
        overrides = proxy.env_overrides
        assert overrides["OPENROUTER_API_BASE"] == overrides["OPENROUTER_BASE_URL"]
        assert overrides["OPENROUTER_API_BASE"].startswith("http://127.0.0.1:")
    finally:
        proxy.stop()


@pytest.mark.parametrize(
    ("env", "upstream"),
    [
        ({}, "https://openrouter.ai/api/v1"),
        ({"OPENROUTER_API_BASE": "https://litellm.example/v1"}, "https://litellm.example/v1"),
        ({"OPENROUTER_BASE_URL": "https://sdk.example/v1"}, "https://sdk.example/v1"),
        # both set: the first var in the registry's order wins
        (
            {"OPENROUTER_BASE_URL": "https://sdk.example/v1", "OPENROUTER_API_BASE": "https://x/v1"},
            "https://sdk.example/v1",
        ),
    ],
)
def test_a_user_set_base_url_is_the_upstream(monkeypatch, env, upstream):
    for name in ("OPENROUTER_BASE_URL", "OPENROUTER_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert _CONFIGS["openrouter"].resolve_upstream() == upstream


def _fake_upstream(usage_body: bytes, content_type: str = "application/json"):
    """A loopback 'vendor' that answers every POST with ``usage_body`` and records the
    path each request arrived on (``srv.paths``)."""
    paths: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            paths.append(self.path)
            self.send_response(200)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(usage_body)))
            self.end_headers()
            self.wfile.write(usage_body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    srv.paths = paths  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _through_proxy(monkeypatch, env: dict[str, str], base_env: str, path: str) -> CaseCost:
    """Set ``env``, start a CostProxy, POST once to ``path`` on ``base_env``'s port, stop."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    proxy = CostProxy()
    proxy.start()
    try:
        resp = httpx.post(
            f"{proxy.env_overrides[base_env]}{path}",
            content=b"{}",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 200
    finally:
        cost = proxy.stop()
    return cost


def test_proxy_forwards_and_accounts(monkeypatch):
    body = json.dumps({"model": "gpt", "usage": {"prompt_tokens": 11, "completion_tokens": 7}}).encode()
    srv, upstream = _fake_upstream(body)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", upstream)  # proxy captures this as upstream
        proxy = CostProxy()
        proxy.start()
        url = proxy.env_overrides["OPENAI_BASE_URL"]
        resp = httpx.post(
            f"{url}/v1/chat/completions", content=b"{}", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 200
        cost = proxy.stop()
    finally:
        srv.shutdown()
    openai = next(m for m in cost.by_model if m.provider == "openai")
    assert (openai.prompt_tokens, openai.completion_tokens, openai.calls) == (11, 7, 1)


def test_proxy_intercepts_and_prices_moonshot(monkeypatch):
    # end to end: a Moonshot (OpenAI-compatible) call is intercepted via its own port,
    # and kimi-k3 prices from the served table ($3/$15 per Mtok) — no OPENAI_* hijack.
    body = json.dumps({"model": "kimi-k3", "usage": {"prompt_tokens": 11, "completion_tokens": 7}}).encode()
    srv, upstream = _fake_upstream(body)
    try:
        monkeypatch.setenv("MOONSHOT_API_KEY", "k")
        monkeypatch.setenv("MOONSHOT_API_BASE", upstream)  # proxy captures this as upstream
        proxy = CostProxy()
        proxy.start()
        url = proxy.env_overrides["MOONSHOT_API_BASE"]
        resp = httpx.post(
            f"{url}/chat/completions", content=b"{}", headers={"content-type": "application/json"}
        )
        assert resp.status_code == 200
        cost = proxy.stop()
    finally:
        srv.shutdown()
    kimi = next(m for m in cost.by_model if m.provider == "moonshot")
    assert (kimi.model, kimi.prompt_tokens, kimi.completion_tokens) == ("kimi-k3", 11, 7)
    assert kimi.cost_usd == pytest.approx((11 * 3 + 7 * 15) / 1_000_000)


def test_accumulate_unknown_model_stays_unknown():
    # Regression: an unpriced model's None call cost lands as None (unknown) in
    # the bucket and stays None across calls — never 0, never a partial sum.
    proxy = CostProxy()
    proxy._accumulate("openai", _usage("unpriced-model-xyz", 60, 39))  # absent from the table
    proxy._accumulate("openai", _usage("unpriced-model-xyz", 10, 5, cache_read_tokens=3))
    (entry,) = proxy._cost_buckets.values()
    assert entry.cost_usd is None and entry.cache_cost_usd is None
    assert (entry.calls, entry.prompt_tokens, entry.cache_read_tokens) == (2, 70, 3)


def test_accumulate_priced_model_sums():
    # a priced model is unaffected: known + known accumulates arithmetically
    proxy = CostProxy()
    proxy._accumulate("openai", _usage("gpt-5.4-nano", 100, 50))
    priced = proxy._cost_buckets[("openai", "gpt-5.4-nano")]
    assert priced.cost_usd is not None and priced.cost_usd > 0
    assert priced.cache_cost_usd == 0.0  # measured, and nothing was cached
    single = priced.cost_usd
    proxy._accumulate("openai", _usage("gpt-5.4-nano", 100, 50))
    assert priced.cost_usd == pytest.approx(single * 2)


def _serve_table(tmp_path, *rows: dict) -> None:
    """Make ``rows`` the run's price table: a fresh cache in the per-test cache path
    (the autouse hermetic_pricing fixture points TRAP_PRICING_CACHE at tmp_path)."""
    table = {"unit": "usd_per_mtok", "prices": list(rows), "fetched_at": time.time()}
    (tmp_path / "pricing-cache.json").write_text(json.dumps(table))


def test_accumulate_folds_cache_tokens_and_cache_spend(tmp_path):
    _serve_table(
        tmp_path,
        {
            "model_prefix": "mystery-model",
            "input_per_mtok": 2.0,
            "output_per_mtok": 8.0,
            "cache_read_per_mtok": 0.5,
            "cache_write_per_mtok": 3.0,
        },
    )
    proxy = CostProxy()
    proxy._accumulate("openai", _usage("mystery-model", 1_000_000, 0, cache_read_tokens=2_000_000))
    proxy._accumulate("openai", _usage("mystery-model", 0, 1_000_000, cache_write_tokens=1_000_000))
    (entry,) = proxy._cost_buckets.values()
    assert (entry.prompt_tokens, entry.completion_tokens) == (1_000_000, 1_000_000)
    assert (entry.cache_read_tokens, entry.cache_write_tokens, entry.calls) == (2_000_000, 1_000_000, 2)
    assert entry.cache_cost_usd == pytest.approx(2 * 0.5 + 3.0)
    assert entry.cost_usd == pytest.approx(2.0 + 8.0 + 2 * 0.5 + 3.0)  # cache spend is inside the total


def test_accumulate_cache_hits_at_an_unknown_rate_poison_the_bucket(tmp_path):
    # a priced model, but its cache reads have no price anywhere: the bucket's cost goes
    # unknown from that call on, rather than showing the plain-token part as the total
    _serve_table(tmp_path, {"model_prefix": "mystery-model", "input_per_mtok": 2.0, "output_per_mtok": 8.0})
    proxy = CostProxy()
    proxy._accumulate("openai", _usage("mystery-model", 100, 50))
    proxy._accumulate("openai", _usage("mystery-model", 100, 50, cache_read_tokens=900))
    (entry,) = proxy._cost_buckets.values()
    assert entry.cost_usd is None and entry.cache_cost_usd is None
    assert (entry.cache_read_tokens, entry.calls) == (900, 2)


def test_accumulate_ignores_a_call_that_reported_no_tokens():
    proxy = CostProxy()
    proxy._accumulate("openai", CallUsage(model="gpt-5.5"))
    assert proxy._cost_buckets == {}
    # a cache-only call is still a call: all of its input came from the cache
    proxy._accumulate("anthropic", CallUsage(model="claude-opus-5", cache_read_tokens=10))
    assert proxy._cost_buckets[("anthropic", "claude-opus-5")].calls == 1


# -- end to end through a fake upstream --------------------------------------------


def _sse_body(*events: dict | str) -> bytes:
    lines = [e if isinstance(e, str) else f"data: {json.dumps(e)}" for e in events]
    return ("\n\n".join(lines) + "\n\n").encode()


SSE = "text/event-stream"


def test_new_providers_default_upstreams_match_what_their_clients_append():
    # the DeepSeek harness POSTs `${DEEPSEEK_BASE_URL}/chat/completions` (no /v1) from a default
    # of https://api.deepseek.com; its web search POSTs `${DEEPSEEK_SEARCH_BASE_URL}/messages`
    # from https://api.deepseek.com/anthropic/v1; OpenRouter clients' bases carry /api/v1
    assert _CONFIGS["deepseek"].upstream == "https://api.deepseek.com"
    assert _CONFIGS["deepseek-search"].upstream == "https://api.deepseek.com/anthropic/v1"
    assert _CONFIGS["openrouter"].upstream == "https://openrouter.ai/api/v1"


def test_proxy_meters_the_deepseek_harness_stream(monkeypatch, tmp_path):
    # the harness streams OpenAI-format chat completions with include_usage; DeepSeek puts the
    # usage on the last chunk, hits reported twice (cached_tokens == prompt_cache_hit_tokens)
    _serve_table(
        tmp_path, {"model_prefix": "deepseek-flash", "input_per_mtok": 0.30, "output_per_mtok": 1.20}
    )
    usage = {"completion_tokens": 1000, "prompt_tokens": 1_001_000, "total_tokens": 1_002_000}
    usage |= {"prompt_tokens_details": {"cached_tokens": 1_000_000}}
    usage |= {"prompt_cache_hit_tokens": 1_000_000, "prompt_cache_miss_tokens": 1000}
    body = _sse_body(
        {"choices": [{"delta": {"content": "hi"}, "index": 0}], "model": "deepseek-flash", "usage": None},
        {
            "choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
            "model": "deepseek-flash",
            "usage": usage,
        },
        "data: [DONE]",
    )
    srv, upstream = _fake_upstream(body, SSE)
    try:
        env = {"DEEPSEEK_API_KEY": "k", "DEEPSEEK_BASE_URL": upstream}
        cost = _through_proxy(monkeypatch, env, "DEEPSEEK_BASE_URL", "/chat/completions")
    finally:
        srv.shutdown()
    assert srv.paths == ["/chat/completions"]
    (entry,) = cost.by_model
    assert (entry.provider, entry.model) == ("deepseek", "deepseek-flash")
    assert (entry.prompt_tokens, entry.cache_read_tokens, entry.cache_write_tokens) == (1000, 1_000_000, 0)
    assert entry.completion_tokens == 1000
    assert entry.cache_cost_usd == pytest.approx(0.006)  # 1M hits at DeepSeek's documented 0.006/Mtok
    assert entry.cost_usd == pytest.approx((1000 * 0.30 + 1000 * 1.20) / 1_000_000 + 0.006)


def test_proxy_meters_the_deepseek_harness_web_search(monkeypatch):
    # each web_search is one more billed call, a non-streaming Anthropic-format POST to
    # `${DEEPSEEK_SEARCH_BASE_URL}/messages` that DEEPSEEK_BASE_URL does not reach
    usage = {"input_tokens": 3000, "output_tokens": 200}
    body = json.dumps({"type": "message", "model": "deepseek-v4-flash", "usage": usage}).encode()
    srv, upstream = _fake_upstream(body)
    try:
        env = {"DEEPSEEK_API_KEY": "k", "DEEPSEEK_SEARCH_BASE_URL": upstream}
        cost = _through_proxy(monkeypatch, env, "DEEPSEEK_SEARCH_BASE_URL", "/messages")
    finally:
        srv.shutdown()
    assert srv.paths == ["/messages"]
    (entry,) = cost.by_model
    # reported under the provider it bills, not under the registry's port name
    assert (entry.provider, entry.model, entry.prompt_tokens, entry.completion_tokens) == (
        "deepseek",
        "deepseek-v4-flash",
        3000,
        200,
    )


def test_proxy_meters_openrouter_through_litellms_base_var(monkeypatch, tmp_path):
    # a litellm-based solution (Aider) is redirected via OPENROUTER_API_BASE; OpenRouter's
    # stream opens with keep-alive comments, and its usage chunk still carries a choice
    _serve_table(
        tmp_path,
        {"model_prefix": "anthropic/claude-sonnet-4.6", "input_per_mtok": 3.0, "output_per_mtok": 15.0},
    )
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)
    usage = {"prompt_tokens": 1_010_000, "completion_tokens": 1000, "total_tokens": 1_011_000, "cost": 1.0}
    usage |= {"prompt_tokens_details": {"cached_tokens": 800_000, "cache_write_tokens": 200_000}}
    final = {
        "model": "anthropic/claude-sonnet-4.6",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    body = _sse_body(": OPENROUTER PROCESSING", final | {"usage": usage}, "data: [DONE]")
    srv, upstream = _fake_upstream(body, SSE)
    try:
        env = {"OPENROUTER_API_KEY": "k", "OPENROUTER_API_BASE": upstream}
        cost = _through_proxy(monkeypatch, env, "OPENROUTER_API_BASE", "/chat/completions")
    finally:
        srv.shutdown()
    assert srv.paths == ["/chat/completions"]
    (entry,) = cost.by_model
    assert (entry.provider, entry.model) == ("openrouter", "anthropic/claude-sonnet-4.6")
    assert (entry.prompt_tokens, entry.cache_read_tokens, entry.cache_write_tokens) == (
        10_000,
        800_000,
        200_000,
    )
    # a first-party route: Anthropic's 0.1x read ($0.30) and 1.25x write ($3.75) of the $3 input rate
    assert entry.cache_cost_usd == pytest.approx(0.8 * 0.30 + 0.2 * 3.75)
    assert cost.cache_cost_usd == entry.cache_cost_usd and cost.cache_read_tokens == 800_000


def test_claude_code_on_a_third_party_anthropic_endpoint_is_metered(monkeypatch, tmp_path):
    # the anthropic provider takes a user-set ANTHROPIC_BASE_URL as its upstream, so Claude Code
    # pointed at DeepSeek's Anthropic-compatible endpoint (DeepSeek's Claude Code guide sets
    # ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic) is metered like Anthropic itself:
    # the path prefix is kept, and the call is priced at the MODEL's vendor rates, not the
    # port's. (DeepSeek does not document which usage fields /anthropic returns; this is
    # Anthropic's own shape.)
    _serve_table(
        tmp_path, {"model_prefix": "deepseek-flash", "input_per_mtok": 0.30, "output_per_mtok": 1.20}
    )
    start = {"input_tokens": 1000, "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 0}
    body = _sse_body(
        {
            "type": "message_start",
            "message": {"model": "deepseek-flash", "usage": start | {"output_tokens": 1}},
        },
        {"type": "message_delta", "usage": {"output_tokens": 1000}},
        {"type": "message_stop"},
    )
    srv, upstream = _fake_upstream(body, SSE)
    try:
        cost = _through_proxy(
            monkeypatch, {"ANTHROPIC_BASE_URL": f"{upstream}/anthropic"}, "ANTHROPIC_BASE_URL", "/v1/messages"
        )
    finally:
        srv.shutdown()
    assert srv.paths == ["/anthropic/v1/messages"]
    (entry,) = cost.by_model
    assert (entry.provider, entry.model) == ("anthropic", "deepseek-flash")
    assert (entry.prompt_tokens, entry.cache_read_tokens, entry.completion_tokens) == (1000, 1_000_000, 1000)
    assert entry.cache_cost_usd == pytest.approx(0.006)  # DeepSeek's hit price, not Anthropic's 0.1x


def test_proxy_meters_a_responses_api_stream(monkeypatch, tmp_path):
    # the Responses API nests usage in the Response its response.completed event carries —
    # a stream the proxy used to meter as zero tokens
    _serve_table(tmp_path, {"model_prefix": "gpt-5.6-sol", "input_per_mtok": 4.0, "output_per_mtok": 24.0})
    usage = {"input_tokens": 2_000_000, "output_tokens": 100_000}
    usage |= {"input_tokens_details": {"cached_tokens": 1_000_000, "cache_write_tokens": 500_000}}
    body = _sse_body(
        {"type": "response.created", "response": {"model": "gpt-5.6-sol", "usage": None}},
        {"type": "response.output_text.delta", "delta": "hi"},
        {"type": "response.completed", "response": {"model": "gpt-5.6-sol", "usage": usage}},
    )
    srv, upstream = _fake_upstream(body, SSE)
    try:
        cost = _through_proxy(monkeypatch, {"OPENAI_BASE_URL": upstream}, "OPENAI_BASE_URL", "/responses")
    finally:
        srv.shutdown()
    (entry,) = cost.by_model
    assert (entry.prompt_tokens, entry.cache_read_tokens, entry.cache_write_tokens) == (
        500_000,
        1_000_000,
        500_000,
    )
    # GPT-5.6: reads 0.1x ($0.40), writes 1.25x ($5.00) of the $4 input rate
    assert entry.cache_cost_usd == pytest.approx(0.40 + 0.5 * 5.00)
    assert entry.cost_usd == pytest.approx(0.5 * 4.0 + 0.1 * 24.0 + entry.cache_cost_usd)

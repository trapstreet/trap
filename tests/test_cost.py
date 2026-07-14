from __future__ import annotations

import json
import math
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from trap.cost.calculator import calculate_call_cost
from trap.cost.providers import _ProtocolStyle, active_provider_configs
from trap.cost.proxy import CostProxy

ANTH = _ProtocolStyle.ANTHROPIC_COMPATIBLE
OAI = _ProtocolStyle.OPENAI_COMPATIBLE


def test_calculator_unknown_is_nan():
    # unknown cost is not zero cost — unpriced models report NaN (JSON null)
    assert math.isnan(calculate_call_cost(100, 50, "totally-unknown-model-xyz"))
    assert math.isnan(calculate_call_cost(100, 50, None))


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
        assert calculate_call_cost(1_000_000, 1_000_000, model) == pytest.approx(expected), model


def test_style_parse_json():
    body = json.dumps({"model": "m", "usage": {"input_tokens": 10, "output_tokens": 5}}).encode()
    assert ANTH.parse("application/json", body) == (10, 5, "m")
    body2 = json.dumps({"model": "g", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}).encode()
    assert OAI.parse("application/json", body2) == (3, 2, "g")


def test_style_parse_bad_json():
    assert OAI.parse("application/json", b"notjson") == (0, 0, None)


def test_style_parse_anthropic_sse():
    sse = (
        'data: {"type":"message_start","message":{"model":"m","usage":{"input_tokens":7}}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n'
        "data: notjson\n\n"
        "ignored line\n"
    )
    assert ANTH.parse("text/event-stream", sse.encode()) == (7, 4, "m")


def test_style_parse_openai_sse():
    sse = 'data: {"model":"g","usage":{"prompt_tokens":5,"completion_tokens":3}}\n\ndata: [DONE]\n\n'
    assert OAI.parse("text/event-stream", sse.encode()) == (5, 3, "g")


def test_style_sse_no_usage():
    assert OAI.parse("text/event-stream", b"event: ping\ndata: {}\ndata: bad\n") == (0, 0, None)
    assert ANTH.parse("text/event-stream", b"junk\n") == (0, 0, None)


def test_anthropic_sse_other_event_type():
    sse = (
        'data: {"type":"ping"}\n\n'
        'data: {"type":"message_start","message":{"model":"m","usage":{"input_tokens":1}}}\n'
    )
    assert ANTH.parse("text/event-stream", sse.encode()) == (1, 0, "m")


def test_registry_active(monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    assert "mistral" not in active_provider_configs()
    monkeypatch.setenv("MISTRAL_API_KEY", "k")
    active = active_provider_configs()
    assert "mistral" in active  # key set
    assert "anthropic" in active and "openai" in active  # always_intercept


def _fake_upstream(usage_body: bytes):
    class H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(usage_body)))
            self.end_headers()
            self.wfile.write(usage_body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


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

from __future__ import annotations

import http.server
import socketserver
import threading
from functools import cached_property
from typing import TYPE_CHECKING

import httpx

from trap.cost.calculator import calculate_call_cost as _calc_cost
from trap.cost.providers import active_provider_configs
from trap.models.cost import CaseCost, ModelCost

if TYPE_CHECKING:
    from trap.cost.providers import _ProviderConfig

# Three-class structure imposed by the socketserver framework:
#
#   CostProxy  →(creates one per active provider)→  _ProxyServer
#                                                        │  .proxy = cost_proxy   (back-ref for bookkeeping)
#                                                        └─→(framework)→  _ProxyHandler
#                                                                             │  .server = proxy_server
#
# Each _ProxyServer is bound to its own port and knows its provider, protocol
# style, and upstream URL — no per-request provider detection needed.
# _ProxyHandler is ephemeral (one per request); _ProxyServer and CostProxy
# live for the duration of a case run.


class CostProxy:
    """HTTP reverse proxy that intercepts LLM API calls to track token usage and cost per case."""

    def __init__(self) -> None:
        self._cost_buckets: dict[tuple[str, str | None], ModelCost] = {}
        self._lock = threading.Lock()
        # Servers are created here to bind ports immediately; threads start in start().
        self._servers: dict[str, _ProxyServer] = {
            name: _ProxyServer(self, name, cfg) for name, cfg in active_provider_configs().items()
        }

    @cached_property
    def env_overrides(self) -> dict[str, str]:
        """Env vars pointing each provider SDK at its proxy port."""
        return {
            server.base_env: f"http://127.0.0.1:{server.server_address[1]}"
            for server in self._servers.values()
        }

    def start(self) -> None:
        """Start serving threads for all provider proxy servers."""
        for server in self._servers.values():
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def stop(self) -> CaseCost:
        """Shut down all proxy servers and return accumulated cost data."""
        for server in self._servers.values():
            server.shutdown()
        with self._lock:
            self._servers.clear()
            return CaseCost(by_model=list(self._cost_buckets.values()))

    def _accumulate(
        self, provider: str, prompt_tokens: int, completion_tokens: int, model: str | None
    ) -> None:
        if not (prompt_tokens or completion_tokens):
            return
        call_cost = _calc_cost(prompt_tokens, completion_tokens, model) if model else 0.0
        with self._lock:
            key = (provider, model)
            entry = self._cost_buckets.get(key)
            if entry is None:
                self._cost_buckets[key] = ModelCost(
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost_usd=call_cost,
                    calls=1,
                )
            else:
                entry.prompt_tokens += prompt_tokens
                entry.completion_tokens += completion_tokens
                entry.cost_usd += call_cost
                entry.calls += 1


class _ProxyServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False

    def __init__(self, proxy: CostProxy, provider: str, cfg: _ProviderConfig) -> None:
        self.proxy = proxy
        self.provider = provider
        self.base_env = cfg.base_env
        self.style = cfg.style
        # Capture upstream before env_overrides() redirects base_env to the proxy itself.
        self.upstream = cfg.resolve_upstream()
        super().__init__(("127.0.0.1", 0), _ProxyHandler)


class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    server: _ProxyServer  # type: ignore[assignment]

    _STRIP_REQUEST_HEADERS = frozenset(
        {
            "host",
            "content-length",
            "accept-encoding",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
        }
    )
    _STRIP_RESPONSE_HEADERS = frozenset(
        {
            "content-length",
            "content-encoding",
            "transfer-encoding",
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "upgrade",
        }
    )

    def do_POST(self) -> None:
        self._forward()

    def do_GET(self) -> None:
        self._forward()

    def _forward(self) -> None:
        proxy = self.server.proxy
        body = self._read_body()
        status_code, content_type, chunks = self._relay_to_upstream(self.server.upstream, body)
        if 0 < status_code < 400:
            prompt_tokens, completion_tokens, model = self.server.style.parse(content_type, b"".join(chunks))
            proxy._accumulate(self.server.provider, prompt_tokens, completion_tokens, model)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _relay_to_upstream(self, upstream: str, body: bytes) -> tuple[int, str, list[bytes]]:
        target_url = upstream.rstrip("/") + self.path
        forward_headers = {
            k: v for k, v in self.headers.items() if k.lower() not in self._STRIP_REQUEST_HEADERS
        }
        status_code = 0
        content_type = ""
        chunks: list[bytes] = []
        try:
            with httpx.Client(timeout=300.0) as client:
                with client.stream(self.command, target_url, content=body, headers=forward_headers) as resp:
                    status_code = resp.status_code
                    content_type = resp.headers.get("content-type", "")
                    self.send_response(status_code)
                    for h_name, h_val in resp.headers.multi_items():
                        if h_name.lower() not in self._STRIP_RESPONSE_HEADERS:
                            self.send_header(h_name, h_val)
                    self.end_headers()
                    for chunk in resp.iter_bytes():
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client hangup
                            break
                        chunks.append(chunk)
        except httpx.HTTPError as exc:
            if not status_code:  # pragma: no branch - the set-status case needs a mid-stream failure
                self.send_error(502, str(exc))
        return status_code, content_type, chunks

    def log_message(self, format: str, *args: object) -> None:
        # Suppress the default per-request stderr logging from BaseHTTPRequestHandler.
        pass

from __future__ import annotations

import http.server
import socketserver
import threading
import urllib.parse
import webbrowser
from typing import Any

from trap.auth.store import Credential


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    _SUCCESS_HTML = (
        b"<!doctype html><meta charset=utf-8>"
        b"<title>logged in</title>"
        b"<style>body{font-family:ui-monospace,monospace;"
        b"background:#0a0a0a;color:#ededed;padding:3em;}"
        b"h1{color:#ff5f1f}</style>"
        b"<h1>logged in</h1>"
        b"<p>You can close this tab.</p>"
    )

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        api_key = (params.get("api_key") or [None])[0]
        if not api_key:
            self._respond(400, "text/plain", b"missing api_key in query string")
            return

        account = (params.get("account") or [None])[0]
        srv: OAuthCallbackServer = self.server  # type: ignore[assignment]
        srv._receive(Credential(server=srv._server_url, api_key=api_key, account=account))
        self._respond(200, "text/html; charset=utf-8", self._SUCCESS_HTML)


class OAuthCallbackServer(socketserver.TCPServer):
    """Local HTTP server that receives the OAuth callback with api_key."""

    def __init__(self, server_url: str) -> None:
        super().__init__(("127.0.0.1", 0), _CallbackHandler)
        self._server_url = server_url.rstrip("/")
        self._auth_data: Credential | None = None
        self._received = threading.Event()

    def _receive(self, data: Credential) -> None:
        self._auth_data = data
        self._received.set()

    @property
    def auth_data(self) -> Credential | None:
        return self._auth_data

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def auth_url(self) -> str:
        return f"{self._server_url}/cli/authorize?return=http://localhost:{self.port}/callback"

    def run(self, timeout: int) -> bool:
        """Start server, open browser, wait for callback, shut down. Returns True if api_key received."""
        threading.Thread(target=self.serve_forever, daemon=True).start()
        try:
            webbrowser.open(self.auth_url)
        except Exception:
            pass
        received = self._received.wait(timeout)
        self.shutdown()
        self.server_close()
        return received

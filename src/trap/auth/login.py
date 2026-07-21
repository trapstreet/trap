from __future__ import annotations

from trap.auth.oauth import OAuthCallbackServer
from trap.auth.store import DEFAULT_SERVER, Credential


class TokenProvider:
    def __init__(self, server: str, token: str) -> None:
        self._server = server
        self._token = token

    @property
    def pre_message(self) -> str:
        return "authenticating with api_key"

    def acquire(self) -> Credential:
        if not self._token:
            raise ValueError("empty api_key")
        return Credential(server=self._server, api_key=self._token)


class BrowserProvider:
    def __init__(self, server: str, timeout: int) -> None:
        if server != DEFAULT_SERVER:
            raise ValueError(
                f"browser login is only supported on {DEFAULT_SERVER}. Use --with-token for custom servers."
            )
        self._cb = OAuthCallbackServer(server)
        self._timeout = timeout

    @property
    def pre_message(self) -> str:
        return f"opening [link={self._cb.auth_url}]{self._cb.auth_url}[/link]"

    def acquire(self) -> Credential:
        if not self._cb.run(self._timeout):
            raise TimeoutError(f"timed out after {self._timeout}s waiting for browser approval")
        assert self._cb.auth_data is not None
        return self._cb.auth_data

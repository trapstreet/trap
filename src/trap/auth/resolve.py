from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel

from trap.auth.store import DEFAULT_SERVER, AuthData

Source = Literal["env", "stored", "default"]


class AuthMismatchError(Exception):
    """The effective token was issued for a different server than the effective one —
    sending it would leak a credential across environments and 401 anyway."""


class ResolvedAuth(BaseModel):
    """The server/token pair a command will actually use, after env overrides.

    Both `tp submit` and `tp auth status` resolve through here so status always
    reports exactly what submit would do. Each value carries its source; a stored
    token also carries the server it was issued for, so pairing can be checked.
    """

    server: str
    server_source: Source
    api_key: str | None
    api_key_source: Literal["env", "stored"] | None
    issued_for: str | None = None  # server the stored token belongs to

    @property
    def is_mismatched(self) -> bool:
        # An env-supplied token is taken as intended for the effective server; only
        # a stored token is pinned to the server it was issued for.
        if self.api_key_source != "stored" or self.issued_for is None:
            return False
        return _norm(self.issued_for) != _norm(self.server)

    def ensure_paired(self) -> None:
        if self.is_mismatched:
            raise AuthMismatchError(
                f"stored token was issued for {self.issued_for}, not {self.server}. "
                f"Set TRAPSTREET_API_KEY, or run `tp auth login --server {self.server}`."
            )


def effective_server(server_override: str | None = None) -> str:
    """The server a command targets before profile lookup: flag > env > default.

    Callers pass this to AuthStore.load() so the profile they hand to
    resolve_auth() is the one issued for the server actually in effect.
    """
    return server_override or _env("TRAPSTREET_URL") or DEFAULT_SERVER


def resolve_auth(stored: AuthData | None, server_override: str | None = None) -> ResolvedAuth:
    """Resolve the effective server and token: override/env > stored > default (server only).

    `server_override` is an explicitly selected server (a --server flag); it outranks
    the env var and is labelled "env" alongside it — both mean "explicitly selected".
    """
    env_server = server_override or _env("TRAPSTREET_URL")
    env_key = _env("TRAPSTREET_API_KEY")

    if env_server:
        server, server_source = env_server, "env"
    elif stored:
        server, server_source = stored.server, "stored"
    else:
        server, server_source = DEFAULT_SERVER, "default"

    if env_key:
        api_key, api_key_source = env_key, "env"
    elif stored:
        api_key, api_key_source = stored.api_key, "stored"
    else:
        api_key, api_key_source = None, None

    return ResolvedAuth(
        server=server,
        server_source=server_source,
        api_key=api_key,
        api_key_source=api_key_source,
        issued_for=stored.server if stored else None,
    )


def _env(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _norm(url: str) -> str:
    return url.rstrip("/")

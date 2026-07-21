from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel

from trap.auth.store import DEFAULT_SERVER, CredentialStore

ServerSource = Literal["env", "stored", "default"]
KeySource = Literal["env", "stored"]


class ResolvedAuth(BaseModel):
    """The server/token pair a command will actually use, after env overrides.

    Both ``tp submit`` and ``tp auth status`` build one of these, so status always
    reports exactly what submit would send. Each value records where it came from.

    Cross-server safety is structural, not a runtime check: the token can only come
    from the effective server's own credential (:meth:`CredentialStore.load` never hands back
    another server's), so a stored credential is never sent where it wasn't issued.
    """

    server: str
    server_source: ServerSource
    api_key: str | None
    api_key_source: KeySource | None

    @classmethod
    def resolve(cls, store: CredentialStore, server_override: str | None = None) -> ResolvedAuth:
        """Resolve the effective credential from the environment and ``store``.

        Server: ``server_override`` (a --server flag) > ``TRAPSTREET_URL`` > default.
        Token: ``TRAPSTREET_API_KEY`` > the stored credential for that server > none.
        An explicit override and the env var both mean "explicitly selected", so both
        are labelled ``env``.
        """
        selected = server_override or _env("TRAPSTREET_URL")
        credential = store.load(selected or DEFAULT_SERVER)

        if selected:
            server, server_source = selected, "env"
        elif credential:
            server, server_source = credential.server, "stored"
        else:
            server, server_source = DEFAULT_SERVER, "default"

        if env_key := _env("TRAPSTREET_API_KEY"):
            return cls(server=server, server_source=server_source, api_key=env_key, api_key_source="env")
        if credential:
            return cls(
                server=server,
                server_source=server_source,
                api_key=credential.api_key,
                api_key_source="stored",
            )
        return cls(server=server, server_source=server_source, api_key=None, api_key_source=None)


def _env(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None

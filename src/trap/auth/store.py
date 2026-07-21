from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

DEFAULT_SERVER = "https://trapstreet.run"


def _normalize(server: str) -> str:
    return server.rstrip("/")


class AuthData(BaseModel):
    """A stored credential: the api_key paired with the server it was issued for,
    plus the account name the server echoed back at pairing time (display only —
    the server derives identity from the token, never from this).

    `account` was called `solution` in the v1 era, when api_keys belonged to a
    leaderboard solution rather than a user. No compat: pre-rename files and
    servers aren't read — a stale profile just means one `tp auth login`."""

    server: str
    api_key: str
    account: str | None = None


class AuthStore:
    """Credential store: one JSON file holding a profile per server URL, so logging
    in to one server never clobbers another's pairing.

    File shape: {"version": 2, "profiles": {"<server>": {"api_key": …, "account": …}}}.
    Anything else (including the pre-profile flat shape) reads as empty — no
    migration; a stale file costs one `tp auth login`. chmod 0600 on every write.
    """

    PATH = Path.home() / ".config" / "trapstreet" / "auth.json"

    def load(self, server: str | None = None) -> AuthData | None:
        """Profile for `server` (default: DEFAULT_SERVER), or None if absent/corrupt."""
        key = _normalize(server or DEFAULT_SERVER)
        profile = self._read_profiles().get(key)
        if profile is None:
            return None
        try:
            return AuthData.model_validate({**profile, "server": key})
        except ValidationError:
            return None

    def save(self, data: AuthData) -> Path:
        profiles = {
            **self._read_profiles(),
            _normalize(data.server): data.model_dump(exclude={"server"}, exclude_none=True),
        }
        return self._write(profiles)

    def delete(self, server: str | None = None) -> bool:
        """Remove one server's profile; True if it existed. Last profile → file removed."""
        profiles = dict(self._read_profiles())
        if profiles.pop(_normalize(server or DEFAULT_SERVER), None) is None:
            return False
        if profiles:
            self._write(profiles)
        else:
            self.PATH.unlink(missing_ok=True)
        return True

    def servers(self) -> list[str]:
        """All servers with a stored profile, sorted."""
        return sorted(self._read_profiles())

    def _read_profiles(self) -> dict[str, dict[str, Any]]:
        try:
            raw = _json.loads(self.PATH.read_text())
        except (OSError, _json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        profiles = raw.get("profiles")
        return profiles if isinstance(profiles, dict) else {}

    def _write(self, profiles: dict[str, dict[str, Any]]) -> Path:
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        self.PATH.write_text(_json.dumps({"version": 2, "profiles": profiles}, indent=2) + "\n")
        self.PATH.chmod(0o600)
        return self.PATH

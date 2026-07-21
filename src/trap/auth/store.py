from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

DEFAULT_SERVER = "https://trapstreet.run"


def _normalize(server: str) -> str:
    return server.rstrip("/")


class AuthData(BaseModel):
    """A stored credential: the api_key paired with the server it was issued for,
    plus the account name the server echoed back at pairing time (display only —
    the server derives identity from the token, never from this).

    `account` was called `solution` in the v1 era, when api_keys belonged to a
    leaderboard solution rather than a user. Old files and callback params still
    say `solution`; accepted on read, rewritten as `account` on save."""

    model_config = ConfigDict(populate_by_name=True)

    server: str
    api_key: str
    account: str | None = Field(default=None, validation_alias=AliasChoices("account", "solution"))


class AuthStore:
    """Credential store: one JSON file holding a profile per server URL, so logging
    in to one server never clobbers another's pairing.

    File shape: {"version": 2, "profiles": {"<server>": {"api_key": …, "solution": …}}}.
    Pre-profile files (a single flat AuthData object) are rewritten in the keyed
    shape on first read. The file is chmod 0600 on every write.
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
        if isinstance(profiles, dict):
            return profiles
        # pre-profile shape: the whole file is one flat AuthData object
        try:
            legacy = AuthData.model_validate(raw)
        except ValidationError:
            return {}
        migrated = {_normalize(legacy.server): legacy.model_dump(exclude={"server"}, exclude_none=True)}
        try:
            self._write(migrated)  # upgrade in place so every later reader sees one shape
        except OSError:
            pass  # read-only location: still serve the parsed profile this run
        return migrated

    def _write(self, profiles: dict[str, dict[str, Any]]) -> Path:
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        self.PATH.write_text(_json.dumps({"version": 2, "profiles": profiles}, indent=2) + "\n")
        self.PATH.chmod(0o600)
        return self.PATH

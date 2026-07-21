from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

DEFAULT_SERVER = "https://trapstreet.run"


class Credential(BaseModel):
    """A stored credential: the api_key paired with the server it was issued for, plus
    the account name the server echoed back at login (display only)."""

    server: str
    api_key: str
    solution: str | None = None


class CredentialStoreError(Exception):
    """The credential file exists but can't be read or parsed — distinct from an absent
    file, which simply means logged out."""


class CredentialStore:
    """One JSON file holding one credential per server URL, so logging in to one
    server never displaces another's pairing.

    On disk, keyed by server URL (pretty-printed, ``indent=2``)::

        {
          "version": 2,
          "credentials": {
            "https://trapstreet.run": {
              "api_key": "tp_live_a1b2c3d4",
              "solution": "alice"
            },
            "https://abc.def": {
              "api_key": "tp_live_e5f6g7h8"
            }
          }
        }

    ``solution`` is optional (the account name the server echoes at login). A legacy
    flat file (a single unkeyed Credential object) is migrated to the keyed shape on
    first read. Every write re-applies mode 0600.
    """

    PATH = Path.home() / ".config" / "trapstreet" / "auth.json"
    _SCHEMA_VERSION = 2

    def load(self, server: str = DEFAULT_SERVER) -> Credential | None:
        """The credential stored for ``server``, or None when none is stored for it. A
        credential is never borrowed across servers — asking for a server you never paired
        returns None, not some other server's token. Raises ``CredentialStoreError`` if the
        file exists but can't be parsed."""
        credential = self._credentials().get(self._server_key(server))
        if credential is None:
            return None
        try:
            return Credential(server=self._server_key(server), **credential)
        except ValidationError:
            return None

    def servers(self) -> list[str]:
        """Every server with a stored credential, sorted."""
        return sorted(self._credentials().keys())

    def save(self, data: Credential) -> Path:
        """Store ``data`` under its server, leaving every other server's credential intact."""
        credentials = self._credentials()
        credentials[self._server_key(data.server)] = data.model_dump(exclude={"server"}, exclude_none=True)
        return self._write(credentials)

    def delete(self, server: str = DEFAULT_SERVER) -> bool:
        """Remove ``server``'s credential; return whether it existed. Removing the last
        credential removes the file."""
        credentials = self._credentials()
        if credentials.pop(self._server_key(server), None) is None:
            return False
        if credentials:
            self._write(credentials)
        else:
            self.PATH.unlink(missing_ok=True)
        return True

    def _credentials(self) -> dict[str, dict[str, Any]]:
        """The keyed credentials on disk. An absent file reads as empty — simply logged
        out — but a file that exists yet can't be read or parsed raises
        ``CredentialStoreError`` instead of masquerading as logged-out. A legacy flat
        file is migrated in place on first read."""
        try:
            raw_text = self.PATH.read_text()
        except FileNotFoundError:
            return {}
        except OSError as e:
            raise CredentialStoreError(f"cannot read the credential store at {self.PATH}: {e}") from e
        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise CredentialStoreError(
                f"the credential store at {self.PATH} is not valid JSON ({e}); delete it and log in again."
            ) from e
        if isinstance(raw, dict) and isinstance(raw.get("credentials"), dict):
            return raw["credentials"]
        return self._migrate_legacy(raw)

    def _migrate_legacy(self, raw: Any) -> dict[str, dict[str, Any]]:
        """Migrate a legacy flat Credential file to the keyed shape, or raise
        ``CredentialStoreError`` when ``raw`` is neither the keyed shape nor a legacy
        Credential. The rewrite is best-effort — a read-only location still serves the
        parsed credential for this run."""
        try:
            legacy = Credential.model_validate(raw)
        except ValidationError as e:
            raise CredentialStoreError(
                f"the credential store at {self.PATH} has an unrecognised format; delete it and log in again."
            ) from e
        stored = legacy.model_dump(exclude={"server"}, exclude_none=True)
        credentials = {self._server_key(legacy.server): stored}
        try:
            self._write(credentials)
        except OSError:
            pass
        return credentials

    def _write(self, credentials: dict[str, dict[str, Any]]) -> Path:
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": self._SCHEMA_VERSION, "credentials": credentials}
        self.PATH.write_text(json.dumps(payload, indent=2) + "\n")
        self.PATH.chmod(0o600)
        return self.PATH

    @staticmethod
    def _server_key(server: str) -> str:
        """Canonical key for a server URL, so a trailing slash never splits one server
        into two credentials."""
        return server.rstrip("/")

from __future__ import annotations

import json as _json
from pathlib import Path

from pydantic import BaseModel, ValidationError

DEFAULT_SERVER = "https://trapstreet.run"


class AuthData(BaseModel):
    server: str
    api_key: str
    solution: str | None = None


class AuthStore:
    PATH = Path.home() / ".config" / "trapstreet" / "auth.json"

    def load(self) -> AuthData | None:
        if not self.exists:
            return None
        try:
            return AuthData.model_validate(_json.loads(self.PATH.read_text()))
        except (OSError, _json.JSONDecodeError, ValidationError):
            return None

    def save(self, data: AuthData) -> Path:
        self.PATH.parent.mkdir(parents=True, exist_ok=True)
        self.PATH.write_text(data.model_dump_json(exclude_none=True, indent=2) + "\n")
        self.PATH.chmod(0o600)
        return self.PATH

    @property
    def exists(self) -> bool:
        return self.PATH.exists()

    def delete(self) -> None:
        self.PATH.unlink()

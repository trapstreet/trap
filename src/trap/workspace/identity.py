"""SolutionIdentity — the identity a solution's runs are stored under."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from trap.git_ops import ParsedGitUrl

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class SolutionIdentity:
    """The identity a solution's runs are stored under: ``<readable>-<hash8>``.

    ``readable`` is the solution's basename (never encodes path separators,
    never leaks ``..``); ``ident`` is the full identity the hash disambiguates
    same-named solutions by — the resolved absolute path, or the normalised
    URL + subdirectory for a remote. Aliases of one solution (``./x``, ``x``,
    an absolute path, a symlinked path) all derive the same key."""

    readable: str
    ident: str

    @classmethod
    def from_spec(cls, solution: str | None) -> SolutionIdentity:
        """Derive the key from a ``--solution`` spec (local path or git+ URL)."""
        if solution is not None and ParsedGitUrl.looks_remote(solution):
            return cls._from_remote(ParsedGitUrl.from_full_url(solution))
        return cls._from_local(solution)

    @classmethod
    def _from_remote(cls, parsed: ParsedGitUrl) -> SolutionIdentity:
        # The URL is the stable identity — the clone dir can be moved (--clone-to)
        # without changing which solution this is.
        return cls(readable=parsed.dir_basename, ident=parsed.normalised_dir_url)

    @classmethod
    def _from_local(cls, solution: str | None) -> SolutionIdentity:
        resolved = (Path.cwd() / (solution or ".")).resolve()
        return cls(readable=resolved.name, ident=str(resolved))

    @property
    def dirname(self) -> str:
        """The key as it appears on disk: the directory name under ``runs/``."""
        return f"{self._safe_readable}-{self._digest}"

    @property
    def _safe_readable(self) -> str:
        """`readable` reduced to dirname-safe characters, with a non-empty fallback."""
        return _UNSAFE.sub("-", self.readable).strip("-.") or "solution"

    @property
    def _digest(self) -> str:
        return hashlib.sha256(self.ident.encode()).hexdigest()[:8]

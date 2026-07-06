from __future__ import annotations

import re
from dataclasses import dataclass

from trap.git_ops.base import GitOpsError


@dataclass(frozen=True)
class ParsedGitUrl:
    """A git URL split into its parts, for cloning a task/solution repo.

    A user writes one string (in trap.yaml or `--solution`); this pulls it apart
    into the URL to clone, an optional pinned rev, and an optional sub-path.

        git+https://github.com/org/repo@v1.0#subdirectory=tasks/a
        └──────── repo ───────┘ └rev┘ └──── subdirectory ────┘
    """

    repo: str  # the underlying git URL (git+ prefix stripped, no @rev, no #fragment)
    rev: str | None  # branch / tag / SHA; None = default branch
    subdirectory: str | None  # sub-path inside the repo; None = root

    @staticmethod
    def looks_remote(s: str) -> bool:
        """True if `s` is a git remote URL (vs a local path).

        A `scheme://…` URL (git+https, https, ssh, git, file) or scp shorthand
        `user@host:path`. Local paths (./x, ../task, /abs, foo) have neither.
        """
        return "://" in s or bool(re.match(r"^[^/@]+@[^/:]+:", s))

    @classmethod
    def from_full_url(cls, url: str) -> ParsedGitUrl:
        """Parse a git URL into (repo, rev, subdirectory).

        Accepts any of these base forms — the `git+` prefix is optional:

            git+https://github.com/org/repo
            https://github.com/org/repo.git
            ssh://git@github.com/org/repo
            git@github.com:org/repo.git          (scp shorthand)

        (Any `scheme://` works, so `file:///path/to/repo` also clones a local
        repo — handy for tests/pinning, distinct from an in-place local path.)

        Two optional suffixes work on every form:

            @<rev>             pin a branch / tag / commit
            #subdirectory=<p>  sub-path inside the repo holding the trap config

        Example:

            git+https://github.com/org/repo@v1.0#subdirectory=tasks/a
            → repo="https://github.com/org/repo", rev="v1.0", subdirectory="tasks/a"

        Raises GitOpsError if `url` is not a remote URL (see `looks_remote`).
        """
        if not cls.looks_remote(url):
            raise GitOpsError(f"not a git URL: {url!r}")
        rest = url[4:] if url.startswith("git+") else url

        subdirectory: str | None = None
        if "#" in rest:
            rest, fragment = rest.split("#", 1)
            for part in fragment.split("&"):
                if part.startswith("subdirectory="):
                    subdirectory = part[len("subdirectory=") :]

        rev: str | None = None
        # only look for @rev in the last path segment to avoid cutting user@host
        if "@" in rest.split("/", 3)[-1]:
            idx = rest.rfind("@")
            rest, rev = rest[:idx], rest[idx + 1 :]

        return cls(repo=rest, rev=rev, subdirectory=subdirectory)

    @property
    def basename(self) -> str:
        """https://github.com/org/my-task.git  →  my-task"""
        name = self.repo.rstrip("/").rsplit("/", 1)[-1]
        return re.sub(r"\.git$", "", name)

    @property
    def normalised_url(self) -> str:
        """`repo` as a canonical clickable https URL.

        git@github.com:user/repo.git   → https://github.com/user/repo
        ssh://git@gitlab.com/u/r       → https://gitlab.com/u/r
        https://github.com/u/r.git     → https://github.com/u/r
        """
        m = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", self.repo)  # scp git@host:path
        if m:
            return f"https://{m.group(1)}/{m.group(2)}"
        m = re.match(r"^ssh://git@([^/]+)/(.+?)(?:\.git)?$", self.repo)  # ssh:// form
        if m:
            return f"https://{m.group(1)}/{m.group(2)}"
        return re.sub(r"\.git$", "", self.repo)  # https/http: drop trailing .git

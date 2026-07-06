from __future__ import annotations

from pathlib import Path

import git

from trap.git_ops.url import ParsedGitUrl
from trap.models.provenance import GitProvenance


class LocalRepo:
    """An existing on-disk git checkout — read-only inspection.

    Distinct from `RemoteRepo` (which clones a declared URL into a root): this wraps
    a `git.Repo` already on disk — whether trap cloned it or the user pointed at
    a local solution. Both clone-sync validation and report provenance go through
    here, so the "open repo + read origin/commit/dirty" logic lives in one place.
    """

    def __init__(self, repo: git.Repo) -> None:
        self.repo = repo

    @classmethod
    def open(cls, path: Path, *, search_parent: bool = False) -> LocalRepo | None:
        """Open the git checkout at `path`, or None if it isn't a git repo."""
        try:
            return cls(git.Repo(path, search_parent_directories=search_parent))
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return None

    @property
    def origin_normalised_url(self) -> str | None:
        """`origin` remote as a canonical https URL, or None if there's no origin."""
        try:
            return ParsedGitUrl.from_full_url(self.repo.remotes.origin.url).normalised_url
        except AttributeError:
            return None

    def provenance(self) -> GitProvenance:
        """{repo, commit} for a clean checkout with an origin, else empty.

        Empty for a dirty tree (tracked-file changes), a remote-less repo, or any
        git error — the run isn't reproducible from remote+commit alone, so we
        claim nothing. Best-effort: a probe failure degrades to empty rather than
        aborting an otherwise-complete run. Untracked files (run outputs under
        .trap/, .venv, …) don't count as dirty.
        """
        try:
            url = self.origin_normalised_url
            if url is None or not self.repo.head.is_valid() or self.repo.is_dirty():
                return GitProvenance()
            return GitProvenance(repo=url, commit=self.repo.head.commit.hexsha)
        except Exception:
            return GitProvenance()

    @classmethod
    def provenance_of(cls, path: Path) -> GitProvenance:
        """Provenance ({repo, commit}) of the checkout at `path`, empty if not a git repo."""
        local_repo = cls.open(path, search_parent=True)
        return local_repo.provenance() if local_repo else GitProvenance()

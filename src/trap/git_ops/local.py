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

    def __init__(self, repo: git.Repo, path: Path) -> None:
        self.repo = repo
        # The path this checkout was opened at (may sit below the repo root when
        # opened with search_parent).
        self.path = path

    @classmethod
    def open(cls, path: Path, *, search_parent: bool = False) -> LocalRepo | None:
        """Open the git checkout at `path`, or None if it isn't a git repo."""
        try:
            return cls(git.Repo(path, search_parent_directories=search_parent), path)
        except (git.InvalidGitRepositoryError, git.NoSuchPathError):
            return None

    @property
    def origin_normalised_url(self) -> str | None:
        """`origin` remote as a canonical https URL, or None if there's no origin."""
        try:
            return ParsedGitUrl.from_full_url(self.repo.remotes.origin.url).normalised_url
        except AttributeError:
            return None

    def provenance_issue(self) -> str | None:
        """Why this checkout cannot be anchored to {repo, commit}, or None if it can.

        The run isn't reproducible from remote+commit alone when the repo has no
        origin, no commit, or uncommitted tracked-file changes. Best-effort: a probe
        failure degrades to a reason rather than raising. Untracked files (run
        outputs under .trap/, .venv, …) don't count as dirty.
        """
        try:
            if self.origin_normalised_url is None:
                return "no origin remote"
            if not self.repo.head.is_valid():
                return "no commit to anchor to"
            if self.repo.is_dirty():
                return "uncommitted changes"
            return None
        except Exception:
            return "git probe failed"

    @property
    def subdirectory(self) -> str | None:
        """Opened path relative to the repo root, None at the root (or in a bare repo)."""
        root = self.repo.working_tree_dir
        if root is None:
            return None
        rel = self.path.resolve().relative_to(Path(root).resolve())
        return rel.as_posix() if rel != Path(".") else None

    def provenance(self) -> GitProvenance:
        """{repo, commit, subdirectory} for a clean checkout with an origin, else empty
        with `issue` naming why — we claim nothing about a run that isn't reproducible."""
        issue = self.provenance_issue()
        if issue is not None:
            return GitProvenance(issue=issue)
        return GitProvenance(
            repo=self.origin_normalised_url,
            commit=self.repo.head.commit.hexsha,
            subdirectory=self.subdirectory,
        )

    @classmethod
    def provenance_of(cls, path: Path) -> GitProvenance:
        """Provenance ({repo, commit}) of the checkout at `path`."""
        local_repo = cls.open(path, search_parent=True)
        return local_repo.provenance() if local_repo else GitProvenance(issue="not a git repo")

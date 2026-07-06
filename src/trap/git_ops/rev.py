from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import git

from trap.git_ops.base import GitOpsError, ProgressCallback

# Each rev kind owns its full lifecycle — fresh clone and reconcile-existing —
# so the behaviour for one kind reads top-to-bottom in one place rather than
# being split across the clone and sync paths.


class RevStrategy(ABC):
    @classmethod
    def for_rev(cls, rev: str | None) -> RevStrategy:
        """Pick the strategy for a rev string (knowable without network)."""
        if rev is None:
            return DefaultBranch()
        if re.fullmatch(r"[0-9a-f]{7,40}", rev):
            return PinnedSha(rev)
        return NamedRef(rev)

    @abstractmethod
    def clone(self, repo_url: str, dest: Path) -> None:
        """Fresh clone of repo_url into dest, positioned at the desired rev."""

    @abstractmethod
    def reconcile(self, repo: git.Repo, root: Path, progress_func: ProgressCallback) -> bool:
        """Verify/update an existing clone (remote already validated).

        Returns True if local code changed (caller then auto-runs setup_cmd).
        """


class DefaultBranch(RevStrategy):
    """No rev pinned — clone default branch; never auto-update an existing clone."""

    def clone(self, repo_url: str, dest: Path) -> None:
        git.Repo.clone_from(repo_url, dest)

    def reconcile(self, repo: git.Repo, root: Path, progress_func: ProgressCallback) -> bool:
        return False


@dataclass(frozen=True)
class PinnedSha(RevStrategy):
    """Immutable SHA — clone then checkout; verify HEAD offline, never fetch."""

    sha: str

    def clone(self, repo_url: str, dest: Path) -> None:
        repo = git.Repo.clone_from(repo_url, dest)
        repo.git.checkout(self.sha)

    def reconcile(self, repo: git.Repo, root: Path, progress_func: ProgressCallback) -> bool:
        head_sha = repo.head.commit.hexsha
        if not head_sha.startswith(self.sha):
            raise GitOpsError(
                f"rev mismatch at {root}:\n  declared: {self.sha}\n  HEAD:     {head_sha[: len(self.sha)]}"
            )
        return False


@dataclass(frozen=True)
class NamedRef(RevStrategy):
    """Tag or branch — distinguished only after fetch: tags are immutable (error
    on drift), branches fast-forward when behind."""

    ref: str

    def clone(self, repo_url: str, dest: Path) -> None:
        git.Repo.clone_from(repo_url, dest, branch=self.ref, single_branch=True)

    def reconcile(self, repo: git.Repo, root: Path, progress_func: ProgressCallback) -> bool:
        head_sha = repo.head.commit.hexsha

        if progress_func:
            progress_func(f"fetching {root.name}...")
        try:
            repo.remotes.origin.fetch()
        except git.GitCommandError as exc:
            raise GitOpsError(f"git fetch failed:\n{exc.stderr.strip()}") from exc

        declared_sha = self._resolve_sha(repo)
        if declared_sha == head_sha:
            return False  # already up to date

        is_branch = any(r.remote_head == self.ref for r in repo.remotes.origin.refs)
        if not is_branch:
            raise GitOpsError(  # pragma: no cover - only a force-moved remote tag reaches here
                f"rev mismatch at {root}:\n"
                f"  declared tag: {self.ref} ({declared_sha[:8]})\n"
                f"  HEAD:         {head_sha[:8]}"
            )

        if progress_func:
            progress_func(f"updating {root.name} to {self.ref} ({declared_sha[:8]})...")
        try:
            repo.git.pull("--ff-only", "origin", self.ref)
        except git.GitCommandError as exc:
            raise GitOpsError(
                f"cannot fast-forward {root} to origin/{self.ref}:\n"
                f"{exc.stderr.strip()}\n"
                f"Local branch has diverged. Delete {root} and re-run."
            ) from exc
        return True

    def _resolve_sha(self, repo: git.Repo) -> str:
        """Resolve the ref to a full commit SHA (call after fetch)."""
        for candidate in [f"origin/{self.ref}", self.ref]:
            try:
                return repo.commit(candidate).hexsha
            except git.BadName:  # pragma: no cover - defensive: a resolvable ref was just fetched
                continue
        raise GitOpsError(f"cannot resolve rev {self.ref!r}")  # pragma: no cover

from __future__ import annotations

from pathlib import Path

import git

from trap.git_ops.base import GitOpsError, ProgressCallback
from trap.git_ops.local import LocalRepo
from trap.git_ops.rev import RevStrategy
from trap.git_ops.url import ParsedGitUrl


class RemoteRepo:
    """A parsed git+ URL bound to a local clone directory (`root`).

    `ensure()` clones it (fresh) or reconciles an existing clone. The caller
    decides where to clone — `root` is the resolved directory, RemoteRepo holds no
    default or base-dir policy. Takes a `ParsedGitUrl` (callers usually need its
    `basename` to build `root` anyway, so parse once and pass it in).
    """

    def __init__(self, parsed: ParsedGitUrl, root: Path) -> None:
        self.parsed = parsed
        self.strategy = RevStrategy.for_rev(parsed.rev)
        self.root = root

    @property
    def local_dir(self) -> Path:
        """Clone root, descended into the requested subdirectory if any."""
        if self.parsed.subdirectory:
            return self.root / self.parsed.subdirectory
        return self.root

    def ensure(self, progress_func: ProgressCallback = None) -> bool:
        """Clone (fresh) or sync (existing) the repo into `root`.

        Returns True when code changed (fresh clone OR branch fast-forwarded) —
        callers auto-run setup_cmd then.  Read `local_dir` for the resolved path.
        """
        return self._sync(progress_func) if self.root.exists() else self._clone(progress_func)

    def _clone(self, progress_func: ProgressCallback) -> bool:
        """Fresh clone — common scaffolding here; rev-specific clone via the strategy."""
        if progress_func:
            progress_func(f"cloning {self.parsed.repo} → {self.root}")
        try:
            self.strategy.clone(self.parsed.repo, self.root)
        except git.GitCommandError as exc:
            raise GitOpsError(f"git clone failed:\n{exc.stderr.strip()}") from exc
        return True

    def _sync(self, progress_func: ProgressCallback) -> bool:
        """Existing clone — validate remote here; rev-specific update via the strategy."""
        local_repo = LocalRepo.open(self.root)
        if local_repo is None:
            raise GitOpsError(f"{self.root} is not a git repository")

        actual = local_repo.origin_normalised_url
        expected = self.parsed.normalised_url
        if actual != expected:
            raise GitOpsError(f"repo mismatch at {self.root}:\n  declared: {expected}\n  found:    {actual}")

        return self.strategy.reconcile(local_repo.repo, self.root, progress_func)

"""Workspace — one `.trap/` store and the queries it answers.

``latest`` is a *derived* value — the newest timestamp-named run directory that
holds a report, detected at every use. Nothing on disk records which run is
latest, so there is no pointer to go stale.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from trap.models import ReportData


class Workspace:
    """One `.trap/` store, scoped to one (solution, task) pair.

    A CLI invocation resolves its solution key and task alias once, so both are
    fixed at construction time — they address one run *series*. Which run inside
    the series to touch is a per-call choice, so ``run`` stays a parameter."""

    DEFAULT_DIRNAME = ".trap"
    RUNS_DIR = "runs"
    REPOS_DIR = "repos"
    REPORT_FILENAME = "report.json"

    def __init__(self, root: Path, solution_key: str, task_alias: str) -> None:
        self.root = root
        self.solution_key = solution_key
        self.task_alias = task_alias

    @classmethod
    def clone_cache_dir(cls, root: Path, repo_basename: str) -> Path:
        """Where a remote task clones when no ``clone_to`` is given: the hidden
        ``repos/`` cache inside the workspace at ``root`` — the same root that
        holds ``runs/``, so the whole store lives in one place."""
        return root / cls.REPOS_DIR / repo_basename

    @property
    def runs_root(self) -> Path:
        return self.root / self.RUNS_DIR

    @property
    def solution_dir(self) -> Path:
        """This solution's namespace inside the store."""
        return self.runs_root / self.solution_key

    @property
    def solution_task_alias_dir(self) -> Path:
        """Where this (solution, task) pair's runs live."""
        return self.solution_dir / self.task_alias

    def latest_run(self) -> str | None:
        """The newest completed run for this (solution, task), or None when there is none.

        Derived from the directory names at every call — stray symlinks and
        non-run entries are ignored."""
        task_dir = self.solution_task_alias_dir
        if not task_dir.is_dir():
            return None
        runs = (path.name for path in task_dir.iterdir() if self._is_completed_run(path))
        return max(runs, default=None)

    def resolved_run(self, run: str) -> str:
        """``"latest"`` resolves to the newest completed run; anything else names
        a run directly (whether its report exists is ``assert_exists``'s concern).
        Raises FileNotFoundError when there is no completed run to resolve
        ``"latest"`` to."""
        if run != "latest":
            return run
        latest = self.latest_run()
        if latest is None:
            raise FileNotFoundError(f"no completed runs in {self.solution_task_alias_dir}")
        return latest

    def run_dir(self, run: str) -> Path:
        """One run's directory; ``run="latest"`` is resolved at every use."""
        return self.solution_task_alias_dir / self.resolved_run(run)

    def report_json_path(self, run: str) -> Path:
        return self.run_dir(run) / self.REPORT_FILENAME

    @classmethod
    def _is_completed_run(cls, path: Path) -> bool:
        """A run counts when its directory is named by an ISO timestamp and holds
        a report — half-written directories from a crashed run never win."""
        if path.is_symlink() or not path.is_dir():
            return False
        try:
            datetime.fromisoformat(path.name)
        except ValueError:
            return False
        return (path / cls.REPORT_FILENAME).is_file()

    def solution_keys(self) -> list[str]:
        """Solution keys with runs in this workspace — the self-help list for a miss."""
        if not self.runs_root.is_dir():
            return []
        return sorted(path.name for path in self.runs_root.iterdir() if path.is_dir())

    def save_as_report(self, run: str, data: ReportData) -> None:
        """Write ``run``'s report; ``run`` must name a concrete run, never ``"latest"``."""
        self.run_dir(run).mkdir(parents=True, exist_ok=True)
        self.report_json_path(run).write_text(data.model_dump_json(indent=2))

    def assert_exists(self, run: str) -> None:
        if not self.report_json_path(run).exists():
            raise FileNotFoundError(f"no report found in {self.run_dir(run)}")

    def load(self, run: str) -> ReportData:
        self.assert_exists(run)
        return ReportData.model_validate_json(self.report_json_path(run).read_text())

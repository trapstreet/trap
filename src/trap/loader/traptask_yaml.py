# Loads traptask.yaml (task author's config) into TraptaskLoader.
from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from trap.loader.errors import ConfigError
from trap.models import TaskBinding, TraptaskCase, TraptaskConfig
from trap.workspace import Workspace


class TraptaskLoader:
    """Loads traptask.yaml (task author's config) and resolves runtime paths."""

    def __init__(self, traptask_yaml_path: Path) -> None:
        self.traptask_dir: Path = traptask_yaml_path.resolve().parent
        if traptask_yaml_path.exists():
            try:
                data = yaml.safe_load(traptask_yaml_path.read_text())
            except yaml.YAMLError as e:
                raise ConfigError(f"invalid YAML in {traptask_yaml_path}: {e}") from e
            try:
                self.traptask = TraptaskConfig.model_validate(data)
            except ValidationError as e:
                raise ConfigError(f"invalid traptask.yaml ({traptask_yaml_path}):\n{e}") from e
        else:
            self.traptask = self._discover(self.traptask_dir)

    @staticmethod
    def _discover(traptask_dir: Path) -> TraptaskConfig:
        """Auto-build TraptaskConfig by scanning inputs/ when traptask.yaml is absent."""
        inputs_dir = traptask_dir / "inputs"
        if not inputs_dir.is_dir():
            raise ConfigError(f"no traptask.yaml and no inputs/ directory in {traptask_dir}")
        case_ids = sorted(p.name for p in inputs_dir.iterdir() if p.is_dir())
        if not case_ids:
            raise ConfigError(f"inputs/ in {traptask_dir} has no case subdirectories")
        return TraptaskConfig(cases=tuple(TraptaskCase(id=case_id) for case_id in case_ids))

    @classmethod
    def from_task_binding(
        cls,
        task_binding: TaskBinding,
        trap_dir: Path,
        setup: bool = False,
        workspace_root: Path = Path(Workspace.DEFAULT_DIRNAME),
    ) -> TraptaskLoader:
        """Resolve traptask.yaml from a TaskBinding's source and the trap.yaml directory.

        Mirrors `TrapLoader.from_solution`: `source` is a local path or a git+ URL.
        A URL clones into `clone_to` (resolved against `trap_dir`, since it is the
        solution author's config) or, when omitted, the workspace's hidden
        `<workspace_root>/repos/<repo>-<hash>` cache (keyed on repo URL + rev, so
        two revs of one repo don't collide) — the same root that holds run
        artifacts; a local path uses it in place and rejects `clone_to`.
        Raises GitOpsError on a bad spec (caller maps it to a CLI error).

        The task's `setup_cmd` (declared in its traptask.yaml, so it travels with the
        task version) prepares the checkout. It auto-runs when a remote pull brought
        new code, and otherwise only when `setup` is set (the `tp run --setup-task`
        escape hatch covering pinned/up-to-date clones and local sources).
        """
        from trap.git_ops import GitOpsError, ParsedGitUrl, RemoteRepo

        if ParsedGitUrl.looks_remote(task_binding.source):
            parsed = ParsedGitUrl.from_full_url(task_binding.source)
            if task_binding.clone_to is not None:
                dest = trap_dir / task_binding.clone_to
            else:
                dest = Workspace.clone_cache_dir(workspace_root, parsed.clone_cache_dirname)
            remote_repo = RemoteRepo(parsed, dest.resolve())
            is_local_changed = remote_repo.ensure()
            traptask_dir = remote_repo.local_dir
        else:
            if task_binding.clone_to is not None:
                raise GitOpsError("clone_to only applies to a remote (git URL) source")
            is_local_changed = False
            traptask_dir = (trap_dir / task_binding.source).resolve()
            _refuse_source_outside_the_solution(task_binding.source, trap_dir, traptask_dir)
        loader = cls(traptask_dir / "traptask.yaml")
        if (is_local_changed or setup) and loader.traptask.setup_cmd:
            # raises subprocess.CalledProcessError on non-zero exit
            subprocess.run(loader.traptask.setup_cmd, shell=True, cwd=loader.traptask_dir, check=True)
        return loader

    @property
    def cases(self) -> tuple[TraptaskCase, ...]:
        """Return all non-skipped cases."""
        return tuple(c for c in self.traptask.cases if not c.skip)

    def cases_with_tags(self, tags: Iterable[str] | None = None) -> tuple[TraptaskCase, ...]:
        """Return non-skipped cases matching any of the specified tags, or all cases if tags is empty/None."""
        if not (tag_set := set(tags or ())):
            return self.cases
        return tuple(c for c in self.cases if not tag_set.isdisjoint(c.tags))


def _refuse_source_outside_the_solution(source: str, trap_dir: Path, traptask_dir: Path) -> None:
    """Name the real cause when a relative `source` points out of the solution's repo.

    A solution whose task lives at `../task` only works where its author kept it: beside
    a sibling checkout. Clone that repo on its own -- which is what happens to every
    solution someone finds and wants to run -- and the path resolves to a sibling of the
    clone that was never there. The failure is real either way; without this the message
    names a directory the user never chose and reads as "the task is missing" rather than
    "this solution was not written to be run anywhere else".

    Only raised when the target is absent, so a working layout is never touched, and only
    when the solution *is* a git checkout -- a loose directory has no boundary to escape,
    and guessing one would turn an ordinary typo into a lecture.
    """
    from trap.git_ops import LocalRepo

    if (traptask_dir / "traptask.yaml").exists() or (traptask_dir / "inputs").is_dir():
        return
    repo = LocalRepo.open(trap_dir, search_parent=True)
    root = repo.repo.working_tree_dir if repo is not None else None
    if root is None or traptask_dir.is_relative_to(Path(root).resolve()):
        return
    raise ConfigError(
        f"task source {source!r} resolves to {traptask_dir}, outside the solution's "
        f"repository ({root}) -- and nothing is there.\n"
        f"  This solution is not self-contained: it only runs beside a checkout of its "
        f"task that its author kept next to it.\n"
        f"  A solution meant to be run from a clone points at the task by URL instead, "
        f"e.g. source: git+https://github.com/owner/task-repo"
    )

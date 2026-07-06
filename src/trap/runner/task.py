from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from trap.models import CaseResult, TrapConfig, TraptaskCase, TraptaskConfig
from trap.runner.grader import GraderRunner
from trap.runner.judge import JudgeRunner
from trap.runner.layout import CaseLayout
from trap.runner.solution import SolutionRunner


class TaskRunner:
    def __init__(
        self,
        trap_config: TrapConfig,
        trap_dir: Path,
        traptask_dir: Path,
        traptask_config: TraptaskConfig,
        run_dir: Path,
        cost_enabled: bool = True,
    ) -> None:
        self.trap_config = trap_config
        self.trap_dir = trap_dir
        self.traptask_config = traptask_config
        self.traptask_dir = traptask_dir
        self.run_dir = run_dir
        self.cost_enabled = cost_enabled

    @cached_property
    def task_inputs_dir(self) -> Path:
        """The task's inputs/ dir (traptask_dir / dirs.inputs), resolved once on first use."""
        return (self.traptask_dir / self.traptask_config.dirs.inputs).resolve()

    @cached_property
    def task_expected_dir(self) -> Path:
        """The task's expected/ dir (traptask_dir / dirs.expected), resolved once on first use."""
        return (self.traptask_dir / self.traptask_config.dirs.expected).resolve()

    def _update_latest(self) -> None:
        latest = self.run_dir.parent / "latest"
        if latest.is_symlink():
            latest.unlink()
        elif latest.exists():
            # Path exists but isn't a symlink — likely an interrupted prior run
            # or a sync tool that materialized the symlink as a real directory.
            # Move it aside (non-destructive) so subsequent runs self-heal.
            suffix = datetime.now().isoformat(timespec="microseconds")
            latest.rename(latest.with_name(f"latest.broken.{suffix}"))
        latest.symlink_to(self.run_dir.name)

    def _iter_cases(
        self,
        cases: Iterable[TraptaskCase],
        *,
        fail_fast: bool = False,
        on_case_start: Callable[[str], None] | None = None,
        on_case_done: Callable[[CaseResult], None] | None = None,
    ) -> Iterator[CaseResult]:
        # TODO: parallelize case runs, but judge cases sequentially in the same order as case runs
        for case in cases:
            if on_case_start is not None:
                on_case_start(case.id)
            layout = CaseLayout.for_case(self.run_dir, case.id)
            case_result = SolutionRunner(self, case.id, layout).run()
            if self.traptask_config.judge is not None:
                # A broken judge is folded into an error metric by the runner (never
                # crashes the run); we just attach whatever it returns.
                metrics = JudgeRunner(self, case.id, layout).run()
                case_result = case_result.model_copy(update={"metrics": metrics})
            if on_case_done is not None:
                on_case_done(case_result)
            yield case_result
            if fail_fast and case_result.exit_code != 0:
                break

    def run(
        self,
        cases: Iterable[TraptaskCase],
        *,
        fail_fast: bool = False,
        on_case_start: Callable[[str], None] | None = None,
        on_case_done: Callable[[CaseResult], None] | None = None,
    ) -> tuple[tuple[CaseResult, ...], Any]:

        case_results = tuple(
            self._iter_cases(
                cases, fail_fast=fail_fast, on_case_start=on_case_start, on_case_done=on_case_done
            )
        )

        grader_metrics = None
        if self.traptask_config.grader is not None:
            # A broken grader is folded into an error metric by the runner (the run
            # still completes and the report still saves).
            grader_metrics = GraderRunner(self, case_results).run()

        self._update_latest()
        return case_results, grader_metrics

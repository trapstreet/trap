from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from trap.models import CaseResult, GraderConfig
from trap.runner.capture import Capture
from trap.runner.proc import CapturedSubprocess

if TYPE_CHECKING:
    from trap.runner.task import TaskRunner


class GraderRunner:
    def __init__(self, runner: TaskRunner, case_results: tuple[CaseResult, ...]) -> None:
        assert runner.traptask_config.grader is not None
        self.grader: GraderConfig = runner.traptask_config.grader
        self.runner = runner
        self.cases = case_results
        # Run-level grader gets its own `grader/` directory next to report.json.
        self.grader_dir = runner.run_dir / "grader"
        self.capture = Capture.from_dir(self.grader_dir)

    @property
    def _manifest(self) -> str:
        return json.dumps([c.model_dump() for c in self.cases])

    def run(self) -> Any:
        return CapturedSubprocess(
            self.grader.cmd,
            manifest_envvar=self.grader.manifest_envvar,
            timeout=self.grader.timeout,
            cwd=self.runner.traptask_dir,
            manifest=self._manifest,
            capture=self.capture,
        ).run_metrics_or_error(runner_name="grader")

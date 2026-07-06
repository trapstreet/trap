from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from trap.models import JudgeConfig
from trap.runner.proc import CapturedSubprocess

if TYPE_CHECKING:
    from trap.runner.layout import CaseLayout
    from trap.runner.task import TaskRunner


class JudgeRunner:
    def __init__(self, runner: TaskRunner, case_id: str, layout: CaseLayout) -> None:
        self.runner = runner
        self.case_id = case_id
        self.case_inputs_dir = runner.task_inputs_dir / case_id  # task-repo side
        self.case_expected_dir = runner.task_expected_dir / case_id  # task-repo side
        self.layout = layout  # workspace side

        assert runner.traptask_config.judge is not None
        self.judge: JudgeConfig = runner.traptask_config.judge

    @property
    def _manifest(self) -> str:
        expected_dir = self.case_expected_dir
        solution_capture = self.layout.solution_capture
        return json.dumps(
            {
                "inputs_dir": str(self.case_inputs_dir.resolve()),
                "expected_dir": str(expected_dir.resolve()) if expected_dir.exists() else None,
                "outputs_dir": str(self.layout.outputs_dir.resolve()),
                "run": {
                    "stdout": str(solution_capture.stdout.resolve()),
                    "stderr": str(solution_capture.stderr.resolve()),
                    "meta": str(solution_capture.meta.resolve()),
                },
            }
        )

    def run(self) -> Any:
        return CapturedSubprocess(
            self.judge.cmd,
            manifest_envvar=self.judge.manifest_envvar,
            timeout=self.judge.timeout,
            cwd=self.runner.traptask_dir,
            manifest=self._manifest,
            capture=self.layout.judge_capture,
        ).run_metrics_or_error(runner_name="judge")

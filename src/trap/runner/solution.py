from __future__ import annotations

import json
from typing import TYPE_CHECKING

from trap.cost import CostProxy
from trap.models import CaseResult
from trap.models.cost import CaseCost
from trap.runner.proc import CapturedSubprocess

if TYPE_CHECKING:
    from trap.runner.layout import CaseLayout
    from trap.runner.task import TaskRunner


class SolutionRunner:
    def __init__(self, runner: TaskRunner, case_id: str, layout: CaseLayout) -> None:
        self.runner = runner
        self.case_id = case_id
        self.case_inputs_dir = runner.task_inputs_dir / case_id  # task-repo side
        self.layout = layout  # workspace side

    @property
    def _stdin(self) -> str:
        stdin = self.runner.trap_config.stdin
        if stdin:
            return (self.case_inputs_dir / stdin).read_text()
        return ""

    @property
    def _manifest(self) -> str:
        return json.dumps(
            {
                "inputs_dir": str(self.case_inputs_dir.resolve()),
                "outputs_dir": str(self.layout.outputs_dir.resolve()),
            }
        )

    def run(self) -> CaseResult:
        self.layout.outputs_dir.mkdir(parents=True, exist_ok=True)

        proxy: CostProxy | None = None
        proxy_env: dict[str, str] = {}
        if self.runner.cost_enabled:
            try:
                proxy = CostProxy()
                proxy.start()
                proxy_env = proxy.env_overrides
            except Exception:
                pass

        # A timeout is folded into the result (exit 124, partial output) rather than
        # raised — for the solution it counts as "did not complete", never a crash.
        config = self.runner.trap_config
        proc = CapturedSubprocess(
            config.cmd,
            manifest_envvar=config.manifest_envvar,
            timeout=config.timeout,
            cwd=self.runner.trap_dir,
            manifest=self._manifest,
            capture=self.layout.solution_capture,
            stdin=self._stdin,
            env_extra=proxy_env,
        )
        case_cost: CaseCost | None = None
        try:
            result = proc.run()
        finally:
            if proxy is not None:
                partial = proxy.stop()
                if partial.calls > 0:
                    case_cost = partial

        return CaseResult(
            case_id=self.case_id,
            exit_code=result.exit_code,
            duration=result.duration,
            metrics=None,
            cost=case_cost,
        )

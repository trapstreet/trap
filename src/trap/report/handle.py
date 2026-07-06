from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from trap.models import CaseResult, Environment, Provenance, ReportData, TrapConfig

_REPORT_FILENAME = "report.json"


class ReportHandle:
    def __init__(self, workspace: Path, task_alias: str, run: str) -> None:
        self._workspace = workspace
        self._task_alias = task_alias
        self._run = run

    @property
    def run_dir(self) -> Path:
        return self._workspace / self._task_alias / self._run

    @property
    def report_json_path(self) -> Path:
        return self.run_dir / _REPORT_FILENAME

    def save(
        self,
        trap_config: TrapConfig,
        case_results: tuple[CaseResult, ...],
        started_at_utc: datetime,
        finished_at_utc: datetime,
        provenance: Provenance,
        grader_metrics: Any = None,
        environment: Environment | None = None,
    ) -> ReportData:
        data = ReportData.from_run(
            trap_config=trap_config,
            provenance=provenance,
            cases_results=case_results,
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            grader_metrics=grader_metrics,
            environment=environment,
        )
        self.report_json_path.write_text(data.model_dump_json(indent=2))
        return data

    def assert_exists(self) -> None:
        if not self.report_json_path.exists():
            raise FileNotFoundError(f"no report found in {self.run_dir}")

    def load(self) -> ReportData:
        self.assert_exists()
        return ReportData.model_validate_json(self.report_json_path.read_text())

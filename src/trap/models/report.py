# Wire format for what `tp` writes to .trap/<task>/<ts>/report.json
# and POSTs to the trapstreet `/api/submit` endpoint.
#
# Reference: trapstreet/docs/scoring-and-metrics.md "Upload protocol".
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from trap import __version__
from trap.models.environment import Environment
from trap.models.provenance import Provenance
from trap.models.results import CaseResult
from trap.models.trap_yaml import Profile, TrapConfig


class ReportData(BaseModel):
    """Top-level upload protocol envelope."""

    # The (repo, commit) of both checkouts — the minimal seed to reproduce the run.
    provenance: Provenance = Field(default_factory=Provenance)

    cases_results: tuple[CaseResult, ...]
    # Raw run-level grader output; None when no grader is configured.
    grader_metrics: Any
    started_at_utc: str
    finished_at_utc: str

    # Engine identity (model/framework). Self-reported from trap.yaml today, but the
    # website consumes it from the report — never from trap.yaml directly — precisely
    # because the source may change: a future version is expected to derive this by
    # observing actual usage (e.g. the cost proxy) rather than trusting self-report.
    # Routing it through the report keeps that swap invisible to downstream consumers.
    profile: Profile = Field(default_factory=Profile)
    # The trap build that produced this report (hatch-vcs version).
    trap_version: str = __version__
    # Host machine environment captured at run time; None when --no-environment.
    environment: Environment | None = None

    @classmethod
    def from_run(
        cls,
        trap_config: TrapConfig,
        cases_results: tuple[CaseResult, ...],
        grader_metrics: Any,
        started_at_utc: datetime,
        finished_at_utc: datetime,
        provenance: Provenance,
        environment: Environment | None = None,
    ) -> ReportData:
        return cls(
            provenance=provenance,
            cases_results=cases_results,
            grader_metrics=grader_metrics,
            started_at_utc=started_at_utc.isoformat(timespec="seconds"),
            finished_at_utc=finished_at_utc.isoformat(timespec="seconds"),
            profile=trap_config.profile,
            environment=environment,
        )

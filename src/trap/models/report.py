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


class SiteGrading(BaseModel):
    """Where this run's answers were sent to be judged by the site. Present only when
    `tp run` opened a server-graded run for an admitted evaluation revision; the local
    judge's scores in this report are then a preview, and the site's own verdicts live
    at ``url``."""

    run_id: str
    url: str


class ReportData(BaseModel):
    """Top-level upload protocol envelope."""

    # The (repo, commit) of both checkouts — the minimal seed to reproduce the run.
    provenance: Provenance = Field(default_factory=Provenance)

    cases_results: tuple[CaseResult, ...]
    # Raw run-level grader output — any valid JSON the grader printed, or None. It never
    # affects pass/fail; that is grader_exit_code alone.
    grader_metrics: Any
    # The grader subprocess's exit code — the sole pass/fail signal for aggregation, or
    # None when no grader ran. Parallels CaseResult.judge_exit_code: 0 = passed, 124 =
    # timed out, 125 = exited 0 but its output wasn't JSON, other non-zero = the grader's
    # own exit.
    grader_exit_code: int | None = None
    started_at_utc: str
    finished_at_utc: str

    # Leaderboard identity the solution author chose in trap.yaml (`name`); None →
    # the server auto-assigns one.
    solution_name: str | None = None
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
    # The live-sync session this run mirrored its progress to, when it had one — the id
    # minted by `tp run` and frozen in the run's sidecar. It is what lets the website
    # attach an uploaded report to the private session that watched the same execution,
    # instead of guessing from timestamps. None whenever no session was created (sync
    # off, no CLI token, an unwritable workspace) and absent from reports written by
    # older CLIs, both of which stay valid and upload unchanged.
    client_run_id: str | None = None
    # The site-graded run this run's answers were submitted to, when there was one
    # (see SiteGrading). None when site grading was off, unavailable, or the task has
    # no admitted evaluation revision; absent from older reports.
    site_grading: SiteGrading | None = None

    @classmethod
    def from_run(
        cls,
        trap_config: TrapConfig,
        cases_results: tuple[CaseResult, ...],
        grader_metrics: Any,
        started_at_utc: datetime,
        finished_at_utc: datetime,
        provenance: Provenance,
        grader_exit_code: int | None = None,
        environment: Environment | None = None,
        client_run_id: str | None = None,
        site_grading: SiteGrading | None = None,
    ) -> ReportData:
        return cls(
            provenance=provenance,
            cases_results=cases_results,
            grader_metrics=grader_metrics,
            grader_exit_code=grader_exit_code,
            started_at_utc=started_at_utc.isoformat(timespec="seconds"),
            finished_at_utc=finished_at_utc.isoformat(timespec="seconds"),
            solution_name=trap_config.name,
            profile=trap_config.profile,
            environment=environment,
            client_run_id=client_run_id,
            site_grading=site_grading,
        )

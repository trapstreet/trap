# Post-run health check: did the measuring apparatus (judge/grader) hold up?
from __future__ import annotations

from pydantic import BaseModel

from trap.models.report import ReportData
from trap.models.results import CaseResult


class Diagnosis(BaseModel):
    """Whether a finished run's *measurement* held up, separate from what it measured.

    trap reports facts, not a verdict: per-case exit codes and scores never set the CLI
    exit code. But a judge that returned no verdict on *every* case, or a grader that
    returned none, means the scores are missing — not zero — so ``tp run`` exits 3 to stop
    scripts from reading an unscored run as one that completed. A judge that failed on only
    *some* cases is still a completed run (exit 0), but worth a loud warning.

    Pass/fail is the exit code *alone* — the output (metrics) is never consulted. The
    judge/grader author is free to print anything; only how the process exits (and trap's
    125 sentinel for "exited 0 but the output wasn't JSON") decides. Derived from the
    report alone: the recorded exit codes already say which actors ran and how they ended.
    """

    judge_failures: tuple[CaseResult, ...]
    total_cases: int
    grader_broken: bool

    @classmethod
    def from_report_data(cls, report: ReportData) -> Diagnosis:
        def broke(exit_code: int | None) -> bool:
            # Pass/fail is the exit code alone; the output never enters it.
            #   None → the actor never ran (no judge/grader configured); not a failure.
            #   0    → passed (it produced valid JSON — any JSON, even `null`).
            #   else → failed: a non-zero exit, a timeout (124), or a clean exit 0 whose
            #          stdout wasn't JSON (the 125 sentinel).
            return exit_code is not None and exit_code != 0

        judge_failures = tuple(r for r in report.cases_results if broke(r.judge_exit_code))
        return cls(
            judge_failures=judge_failures,
            total_cases=len(report.cases_results),
            grader_broken=broke(report.grader_exit_code),
        )

    @property
    def judge_broken(self) -> bool:
        """A judge ran on every case and returned a verdict on none."""
        return bool(self.judge_failures) and len(self.judge_failures) == self.total_cases

    @property
    def partial_judge_failure(self) -> bool:
        """Some cases scored, some didn't — a completed run, but say so loudly."""
        return bool(self.judge_failures) and not self.judge_broken

    @property
    def measurement_broken(self) -> bool:
        """The scores are missing, not zero — the run must not read as a clean pass."""
        return self.judge_broken or self.grader_broken

    @property
    def exit_code(self) -> int:
        return 3 if self.measurement_broken else 0

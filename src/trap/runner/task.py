from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from trap.loader.errors import ConfigError
from trap.models import CaseResult, TrapConfig, TraptaskCase, TraptaskConfig
from trap.runner.grader import GraderRunner
from trap.runner.judge import JudgeRunner
from trap.runner.layout import CaseLayout
from trap.runner.solution import SolutionRunner


@dataclass(frozen=True)
class AnswerOverlap:
    """A case whose inputs, with symlinks followed, overlap the answers at ``answers``."""

    case_id: str
    inputs: Path
    answers: Path


def _nested(a: Path, b: Path) -> bool:
    """Whether ``a`` and ``b`` are one path or one sits inside the other — compared by
    path component, so ``/x/ab`` is not inside ``/x/a``."""
    return a.is_relative_to(b) or b.is_relative_to(a)


def answer_overlaps(
    inputs_root: Path, expected_root: Path, case_ids: Iterable[str]
) -> tuple[AnswerOverlap, ...]:
    """The cases whose inputs directory, resolved, is, contains, or sits inside its own
    resolved answers directory or the resolved expected root (where every other case's
    answers are). Nothing has to exist — a missing path is resolved as far as it goes — so
    a task that keeps its answers off the machine is compared like any other."""
    expected = expected_root.resolve()
    overlaps: list[AnswerOverlap] = []
    for case_id in case_ids:
        inputs = (inputs_root / case_id).resolve()
        answers = (expected_root / case_id).resolve()
        if _nested(inputs, answers):
            overlaps.append(AnswerOverlap(case_id, inputs, answers))
        elif _nested(inputs, expected):
            overlaps.append(AnswerOverlap(case_id, inputs, expected))
    return tuple(overlaps)


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

    def refuse_answer_overlap(self, cases: Iterable[TraptaskCase]) -> None:
        """Raise ConfigError when any of ``cases`` has inputs that overlap its answers.

        A solution is handed its case's inputs as a resolved path, so a case directory
        that — through a symlink or through ``dirs`` — is, holds, or sits inside expected
        answers hands every solution the answers, and nothing downstream can tell it from
        an ordinary directory. The error names each such case with both resolved paths."""
        overlaps = answer_overlaps(self.task_inputs_dir, self.task_expected_dir, (c.id for c in cases))
        if overlaps:
            lines = "".join(
                f"\n  {o.case_id}: inputs {o.inputs} overlap answers {o.answers}" for o in overlaps
            )
            raise ConfigError(
                f"refusing to run task {self.traptask_dir}: every solution would be handed the "
                "answers, because these cases' inputs are, contain, or sit inside expected "
                f"answers once symlinks are followed:{lines}"
            )

    def _iter_cases(
        self,
        cases: Iterable[TraptaskCase],
        *,
        fail_fast: bool = False,
        on_case_start: Callable[[str], None] | None = None,
        on_case_done: Callable[[CaseResult], None] | None = None,
        on_judge_start: Callable[[str], None] | None = None,
        on_judge_done: Callable[[str, Any, int], None] | None = None,
    ) -> Iterator[CaseResult]:
        # TODO: parallelize case runs, but judge cases sequentially in the same order as case runs
        for case in cases:
            if on_case_start is not None:
                on_case_start(case.id)
            layout = CaseLayout.for_case(self.run_dir, case.id)
            case_result = SolutionRunner(self, case.id, layout).run()
            if self.traptask_config.judge is not None:
                # The judge never crashes the run: a broken one returns None metrics and
                # its exit code, both attached to the case for the report to record.
                if on_judge_start is not None:
                    on_judge_start(case.id)
                metrics, judge_exit_code = JudgeRunner(self, case.id, layout).run()
                if on_judge_done is not None:
                    on_judge_done(case.id, metrics, judge_exit_code)
                case_result = case_result.model_copy(
                    update={"metrics": metrics, "judge_exit_code": judge_exit_code}
                )
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
        on_judge_start: Callable[[str], None] | None = None,
        on_judge_done: Callable[[str, Any, int], None] | None = None,
        on_grader_start: Callable[[], None] | None = None,
        on_grader_done: Callable[[Any, int], None] | None = None,
    ) -> tuple[tuple[CaseResult, ...], Any, int | None]:
        """Run every case, then the grader. The callbacks are stage observers --
        ``on_judge_*`` fire around each case's judge, ``on_grader_*`` around the
        run's grader -- and receive only what the report will record: the
        actor's parsed metrics and exit code.

        Raises ConfigError, before any case starts, for a task whose case inputs
        overlap its answers (see ``refuse_answer_overlap``)."""

        cases = tuple(cases)
        self.refuse_answer_overlap(cases)
        case_results = tuple(
            self._iter_cases(
                cases,
                fail_fast=fail_fast,
                on_case_start=on_case_start,
                on_case_done=on_case_done,
                on_judge_start=on_judge_start,
                on_judge_done=on_judge_done,
            )
        )

        grader_metrics = None
        grader_exit_code = None
        if self.traptask_config.grader is not None:
            # The grader never crashes the run: a broken one returns None metrics and its
            # exit code (the run still completes and the report still saves).
            if on_grader_start is not None:
                on_grader_start()
            grader_metrics, grader_exit_code = GraderRunner(self, case_results).run()
            if on_grader_done is not None:
                on_grader_done(grader_metrics, grader_exit_code)

        return case_results, grader_metrics, grader_exit_code

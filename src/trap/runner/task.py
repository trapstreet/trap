from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from trap.errors import ConfigError
from trap.models import CaseResult, TrapConfig, TraptaskCase, TraptaskConfig
from trap.runner.grader import GraderRunner
from trap.runner.judge import JudgeRunner
from trap.runner.layout import CaseLayout
from trap.runner.solution import SolutionRunner

OWN_ANSWERS = "its own answers"
EXPECTED_ROOT = "the expected root"


@dataclass(frozen=True)
class AnswerOverlap:
    """One case whose inputs overlap answers, read as ``inputs`` ``relation`` ``target``
    ``answers`` — "inputs /t/expected/c1 are its own answers /t/expected/c1". ``relation``
    is "are", "contain" or "sit inside"; ``target`` says whether ``answers`` is the case's
    own answers directory or the expected root that holds every case's."""

    case_id: str
    inputs: Path
    relation: str
    target: str
    answers: Path


def _identity(path: Path) -> tuple[int, int] | None:
    """The directory ``path`` names on disk, as (device, inode) — or None when it can't be
    read (missing, a link loop, a parent without search permission), which matches nothing."""
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _same(a: Path, b: Path) -> bool:
    """One directory: the same path, or two spellings of it on disk — letter case or
    Unicode form on a filesystem that ignores them, a firmlink."""
    return a == b or ((key := _identity(a)) is not None and key == _identity(b))


def _inside(child: Path, parent: Path) -> bool:
    """``child`` is ``parent`` or sits inside it: by path component (``/x/ab`` is not
    inside ``/x/a``), or because ``child`` or one of its parents is ``parent`` on disk."""
    if child.is_relative_to(parent):
        return True
    key = _identity(parent)
    return key is not None and any(_identity(p) == key for p in (child, *child.parents))


def _relation(inputs: Path, answers: Path) -> str | None:
    """How a case's inputs overlap an answers directory, or None when they don't."""
    if _same(inputs, answers):
        return "are"
    if _inside(answers, inputs):
        return "contain"
    if _inside(inputs, answers):
        return "sit inside"
    return None


def _root_relation(inputs: Path, expected: Path, inputs_root: Path) -> str | None:
    """How a case's inputs overlap the expected root. Sitting inside it counts only away
    from the inputs root: an expected root around the inputs root (``dirs.expected: ./``)
    holds every case's inputs without their holding anyone's answers."""
    relation = _relation(inputs, expected)
    if relation == "sit inside" and _inside(inputs, inputs_root):
        return None
    return relation


def answer_overlaps(
    inputs_root: Path, expected_root: Path, case_ids: Iterable[str]
) -> tuple[AnswerOverlap, ...]:
    """The cases whose resolved inputs directory is, contains, or sits inside its own
    resolved answers directory; contains the resolved expected root; or sits inside it
    outside the inputs root, where other cases' answers are.

    Directories are compared by path and by identity on disk, so two spellings of one
    directory match. Nothing has to exist: a missing path is resolved as far as it goes
    and compared by path alone, so a task that keeps its answers off the machine is
    compared like any other."""
    inputs_dir = inputs_root.resolve()
    expected = expected_root.resolve()
    overlaps: list[AnswerOverlap] = []
    for case_id in case_ids:
        inputs = (inputs_root / case_id).resolve()
        answers = (expected_root / case_id).resolve()
        if own := _relation(inputs, answers):
            overlaps.append(AnswerOverlap(case_id, inputs, own, OWN_ANSWERS, answers))
        elif root := _root_relation(inputs, expected, inputs_dir):
            overlaps.append(AnswerOverlap(case_id, inputs, root, EXPECTED_ROOT, expected))
    return tuple(overlaps)


def refuse_answer_overlap(
    traptask_dir: Path, traptask_config: TraptaskConfig, cases: Iterable[TraptaskCase]
) -> None:
    """Raise ConfigError when any of ``cases`` has inputs that overlap answers.

    A solution is handed its case's inputs as a resolved path, so a case directory that —
    through a symlink, through ``dirs``, or as another spelling of one directory — is,
    holds, or sits inside answers hands every solution the answers, and nothing
    downstream can tell it from an ordinary directory. The error names the task and, for
    each such case, how its inputs overlap which answers."""
    dirs = traptask_config.dirs
    overlaps = answer_overlaps(
        traptask_dir / dirs.inputs, traptask_dir / dirs.expected, (c.id for c in cases)
    )
    if overlaps:
        lines = "".join(
            f"\n  {o.case_id}: inputs {o.inputs} {o.relation} {o.target} {o.answers}" for o in overlaps
        )
        raise ConfigError(
            f"refusing to run task {traptask_dir}: every solution would be handed the answers, "
            f"because these cases' inputs overlap expected answers:{lines}"
        )


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
        refuse_answer_overlap(self.traptask_dir, self.traptask_config, cases)
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

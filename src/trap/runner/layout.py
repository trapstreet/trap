from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trap.runner.capture import Capture


def task_root(traptask_dir: Path, relative: str) -> Path:
    """A task directory named in traptask.yaml's ``dirs``, resolved: the root each case's
    inputs and answers are handed on from, to the solution, the judge and the leak check
    alike. Resolving takes a ``..`` after a folder that is missing or can't be searched
    by its text, where the OS would fail on it, so all three must start from this path."""
    return (traptask_dir / relative).resolve()


@dataclass(frozen=True)
class CaseLayout:
    """Where one case's run artefacts live in the .trap workspace.

    Structure: ``{run_dir}/{case_id}/{solution,judge}/...``. This is the single
    source of truth for the per-case directory layout, so the runners derive
    their paths from here instead of each re-joining the same strings.
    """

    case_dir: Path

    @classmethod
    def for_case(cls, run_dir: Path, case_id: str) -> CaseLayout:
        return cls(run_dir / case_id)

    @property
    def outputs_dir(self) -> Path:
        """Solution-written files only — the manifest's ``outputs_dir``."""
        return self.case_dir / "solution" / "outputs"

    @property
    def solution_capture(self) -> Capture:
        """The solution run's stdout/stderr/meta — the manifest's ``run``."""
        return Capture.from_dir(self.case_dir / "solution")

    @property
    def judge_capture(self) -> Capture:
        return Capture.from_dir(self.case_dir / "judge")

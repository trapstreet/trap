from __future__ import annotations

from types import TracebackType
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column

from trap.models import CaseResult, TraptaskCase


class CaseProgress:
    """Context manager that shows a Rich progress bar while cases run.

    Pass ``console=None`` (the default) for a silent no-op.
    """

    def __init__(self, cases: tuple[TraptaskCase, ...], *, console: Console | None = None) -> None:
        self._n = len(cases)
        # Always build the Progress; disable=True (console=None) makes every call a
        # no-op — Rich renders nothing rather than falling back to its stdout console.
        self._progress = Progress(
            SpinnerColumn(style="dark_orange"),
            TextColumn("[bold]{task.description}", table_column=Column(width=30, no_wrap=True)),
            BarColumn(complete_style="dark_orange", finished_style="bright_yellow"),
            TaskProgressColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
            disable=console is None,
        )
        self._task_id: Any = None

    def __enter__(self) -> CaseProgress:
        self._progress.start()
        self._task_id = self._progress.add_task("starting...", total=self._n)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._progress.stop()

    def on_case_start(self, case_id: str) -> None:
        self._progress.update(self._task_id, description=f"running  [bold]{case_id}[/bold]")

    def on_case_done(self, _: CaseResult) -> None:
        self._progress.advance(self._task_id)

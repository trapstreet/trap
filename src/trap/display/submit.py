from __future__ import annotations

from rich.console import Console
from rich.table import Table

from trap.models import GitProvenance, ReportData


class SubmitRenderer:
    """Renders `tp submit`'s terminal output: the pre-upload intent table and the
    post-upload result. Mirrors RichRenderer in display/report.py — a console fixed at
    construction, static helpers for cell/line formatting, and one method per view."""

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    @staticmethod
    def _case_tally(data: ReportData) -> str:
        """A neutral one-line execution tally — facts, not a verdict (mirrors
        RichRenderer._build_summary in display/report.py). Counts cases, judge passes
        (judge_exit_code == 0), and non-zero solution exits; adds a grader token when a
        grader ran."""
        results = data.cases_results
        n_total = len(results)
        parts = [f"{n_total} case{'s' if n_total != 1 else ''}"]
        if n_judged := sum(1 for r in results if r.judge_exit_code is not None):
            n_passed = sum(1 for r in results if r.judge_exit_code == 0)
            parts.append(f"{n_passed}/{n_judged} judged ✓")
        if n_errored := sum(1 for r in results if r.exit_code != 0):
            parts.append(f"{n_errored} non-zero exit")
        if data.grader_exit_code is not None:
            parts.append("grader ✓" if data.grader_exit_code == 0 else "grader ✗")
        return " · ".join(parts)

    @staticmethod
    def _anchor_cell(side: GitProvenance) -> str:
        """One checkout's anchor status: ✓ repo@commit[/subdir] when pinned to a remote,
        else ✗ unanchored (with the recorded reason, if any)."""
        if not side.repo:
            reason = f" ({side.issue})" if side.issue else ""
            return f"[yellow]✗ unanchored{reason}[/yellow]"
        commit = (side.commit or "")[:8] or "?"
        subdir = f"/{side.subdirectory}" if side.subdirectory else ""
        return f"[green]✓[/green] {side.repo}@{commit}{subdir}"

    def intent(self, data: ReportData, run_id: str, server: str) -> None:
        """Echo what a `tp submit` is about to publish — solution / run / result /
        anchor, all read from the local report — so the user (or a CI log) sees the
        payload before it leaves the machine."""
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", justify="right")
        grid.add_column()

        name = data.solution_name or "[dim](server-assigned)[/dim]"
        engine = ", ".join((*data.profile.model, *data.profile.framework))
        if engine:
            name += f"  [dim]({engine})[/dim]"
        grid.add_row("solution", name)
        grid.add_row("run", f"[bold]{run_id}[/bold] → {server}")
        grid.add_row("result", self._case_tally(data))
        grid.add_row("anchor", self._anchor_cell(data.provenance.solution) + "  [dim]solution[/dim]")
        grid.add_row("", self._anchor_cell(data.provenance.task) + "  [dim]task[/dim]")

        self.console.print("[bold]about to submit[/bold]")
        self.console.print(grid)

    def result(
        self, resp_data: dict, *, report_data: ReportData | None = None, run_id: str | None = None
    ) -> None:
        """Render a successful POST /api/submit response: {run: {id}, view_url}.

        Success is decided by the HTTP status alone (ApiClient.submit raises on
        non-2xx), so this only confirms the upload and points at the run page —
        scores and case detail live there, not in the response. When the local report
        is passed, a compact recap of the uploaded content is echoed too, so the user
        sees what was published without depending on the server response contract."""
        run_obj = resp_data.get("run") or {}
        self.console.print("[green]✓ submitted[/green]")
        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column()
        table.add_row("run_id", f"[bold]{run_obj.get('id', '?')}[/bold]")
        if view_url := resp_data.get("view_url"):
            table.add_row("url", f"[link={view_url}]{view_url}[/link]")
        if report_data is not None:
            recap = report_data.solution_name or "(server-assigned)"
            if run_id is not None:
                recap += f" · run {run_id}"
            recap += f" · {self._case_tally(report_data)}"
            table.add_row("uploaded", f"[dim]{recap}[/dim]")
        self.console.print(table)

from __future__ import annotations

import json

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from trap.models import CaseResult, ReportData
from trap.report.base import BaseRenderer


class RichRenderer(BaseRenderer):
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    @staticmethod
    def _get_metrics_keys(data: ReportData) -> set[str]:
        return {key for result in data.cases_results if result.metrics for key in result.metrics}

    @staticmethod
    def _render_metric_cell(value: object) -> str:
        if value is None:
            return "[dim]—[/dim]"
        if isinstance(value, bool):
            return "[bold green]✓[/bold green]" if value else "[bold red]✗[/bold red]"
        if isinstance(value, float) and 0.0 <= value <= 1.0:
            pct = value * 100
            if pct >= 100:
                return f"[bold green]{pct:.0f}%[/bold green]"
            if pct > 0:
                return f"[bold yellow]{pct:.0f}%[/bold yellow]"
            return f"[bold red]{pct:.0f}%[/bold red]"
        # Bracket characters in user-supplied content (judge metric values,
        # grader outputs, etc.) would otherwise be interpreted as Rich
        # markup and crash with MarkupError on mismatched tags. e.g. a judge
        # emitting a regex pattern like "[/\\-]" raises:
        #     MarkupError: closing tag '[/\\-]' doesn't match any open tag
        # Escape so brackets render as literal text.
        return escape(str(value))

    @staticmethod
    def _render_exit(result: CaseResult) -> str:
        # The raw exit code — not a verdict. "did it run" (this) is a separate axis from
        # "is it correct" (the judge's score columns); 124 is the timeout sentinel.
        if result.exit_code == 0:
            return "[dim]0[/dim]"
        return f"[red]{result.exit_code}[/red]"

    @staticmethod
    def _render_cost(usd: float) -> str:
        if usd <= 0:
            return "[dim]—[/dim]"
        if usd < 0.001:
            return f"[dim]${usd:.5f}[/dim]"
        return f"[dim]${usd:.4f}[/dim]"

    def _build_table(self, data: ReportData) -> Table:
        metrics_keys = self._get_metrics_keys(data)
        has_cost = any(c.cost is not None for c in data.cases_results)
        table = Table(box=box.ROUNDED, show_header=True, header_style="bold", expand=True)
        table.add_column("case")
        table.add_column("exit", justify="right")
        table.add_column("time", justify="right", style="dim")
        for key in metrics_keys:
            table.add_column(f"# {escape(key)}", justify="right", header_style="bold cyan")
        if has_cost:
            table.add_column("prompt_tok", justify="right", style="dim")
            table.add_column("compl_tok", justify="right", style="dim")
            table.add_column("cost", justify="right")
        for result in data.cases_results:
            row = [escape(result.case_id), self._render_exit(result), f"{result.duration:.3f}s"]
            for key in metrics_keys:
                value = result.metrics.get(key) if result.metrics else None
                row.append(self._render_metric_cell(value))
            if has_cost:
                if result.cost:
                    row.append(str(result.cost.prompt_tokens))
                    row.append(str(result.cost.completion_tokens))
                    row.append(self._render_cost(result.cost.cost_usd))
                else:
                    row.extend(["[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]"])
            table.add_row(*row)
        return table

    def _build_summary(self, data: ReportData) -> Panel:
        # Neutral execution tally — trap reports facts, not a verdict. "non-zero exit"
        # just counts cases whose solution did not exit 0 (correctness is the judge's
        # score, not this). The grader's own output, if any, is shown verbatim below.
        results = data.cases_results
        n_total = len(results)
        n_errored = sum(1 for r in results if r.exit_code != 0)
        parts = [f"{n_total} case{'s' if n_total != 1 else ''}"]
        if n_errored:
            parts.append(f"[red]{n_errored} non-zero exit[/red]")

        rows: list[tuple[str, Text | str]] = [
            ("cases", Text.from_markup(" · ".join(parts) or "[dim]no cases[/dim]")),
        ]
        if data.grader_metrics is not None:
            rows.append(("grader", escape(json.dumps(data.grader_metrics, ensure_ascii=False))))

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", justify="right")
        grid.add_column()
        for key, value in rows:
            grid.add_row(key, value)

        return Panel(grid, title="Summary", expand=True)

    def render(self, data: ReportData) -> None:
        self.console.print(self._build_summary(data))
        self.console.print(self._build_table(data))

from __future__ import annotations

from rich.console import Console
from rich.table import Table

console = Console()


def render_submit_result(resp_data: dict) -> None:
    run_obj = resp_data.get("run") or {}
    passed = run_obj.get("passed")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("status", "[green]✓ passed[/green]" if passed else "[red]✗ failed[/red]")
    table.add_row("run", f"[bold]{run_obj.get('id', '?')}[/bold]")
    table.add_row("score", f"[cyan]{run_obj.get('total_score')}[/cyan]")
    if view_url := resp_data.get("view_url"):
        table.add_row("url", f"[link={view_url}]{view_url}[/link]")
    console.print(table)

from __future__ import annotations

from rich.console import Console
from rich.table import Table

console = Console()


def render_submit_result(resp_data: dict) -> None:
    """Render a successful POST /api/submit response: {run: {id}, view_url}.

    Success is decided by the HTTP status alone (ApiClient.submit raises on
    non-2xx), so this only confirms the upload and points at the run page —
    scores and case detail live there, not in the response."""
    run_obj = resp_data.get("run") or {}
    console.print("[green]✓ submitted[/green]")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("run_id", f"[bold]{run_obj.get('id', '?')}[/bold]")
    if view_url := resp_data.get("view_url"):
        table.add_row("url", f"[link={view_url}]{view_url}[/link]")
    console.print(table)

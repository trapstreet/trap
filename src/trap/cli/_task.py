from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer

from trap.auth import (
    DEFAULT_SERVER,
    ApiClient,
    ApiError,
    CredentialStore,
    CredentialStoreError,
    ResolvedAuth,
)
from trap.cli._console import _die, console

task_app = typer.Typer(help="Manage tasks on trapstreet.")


class RankingMetric(StrEnum):
    score = "score"
    cost = "cost"
    latency = "latency"
    pass_rate = "pass_rate"
    none = "none"


@task_app.command("register")
def task_register(
    ref: Annotated[
        str,
        typer.Argument(
            help="The task's public repo, owner/repo or owner/repo/path. Its default branch's HEAD is pinned."
        ),
    ],
    task_id: Annotated[str, typer.Option("--id", help="Task id: the slug in /tasks/<id>.")],
    title: Annotated[str, typer.Option("--title", help="Human-readable title.")],
    tags: Annotated[list[str], typer.Option("--tag", help="A tag; repeat for more. At least one.")],
    summary: Annotated[str | None, typer.Option("--summary", help="One line for the task list.")] = None,
    metric: Annotated[
        RankingMetric, typer.Option("--metric", help="How the board ranks.")
    ] = RankingMetric.score,
    private: Annotated[
        bool, typer.Option("--private", help="Register it private (default: public).")
    ] = False,
    server: Annotated[
        str | None,
        typer.Option("--server", envvar="TRAPSTREET_URL", help="Trapstreet server URL."),
    ] = None,
) -> None:
    """Register a task on trapstreet with your CLI token. Maintainers only; everyone else uses the website."""
    try:
        resolved = ResolvedAuth.resolve(CredentialStore(), server)
    except CredentialStoreError as e:
        raise _die(e) from None
    if resolved.api_key is None:
        hint = "" if resolved.server == DEFAULT_SERVER else f" --server {resolved.server}"
        raise _die(
            f"not logged in to {resolved.server}. Run [bold]tp auth login{hint}[/bold] "
            "or set [bold]TRAPSTREET_API_KEY[/bold]."
        )

    body: dict[str, object] = {
        "id": task_id,
        "title": title,
        "traptask_ref": ref,
        "tags": tags,
        "ranking_metric": metric.value,
        "visibility": "private" if private else "public",
    }
    if summary is not None:
        body["summary"] = summary

    try:
        task = ApiClient(resolved.server, resolved.api_key).register_task(body)["task"]
    except ApiError as e:
        raise _die(e) from None

    url = f"{resolved.server.rstrip('/')}/tasks/{task['id']}"
    console.print(f"registered {url}", soft_wrap=True, markup=False, highlight=False)
    commit = (task.get("latest") or {}).get("commit_sha")
    if commit:
        console.print(f"pinned at {commit[:12]}", markup=False, highlight=False)
    console.print(
        f"Needs a private judge? Bind its grading pack at {url}/grading",
        soft_wrap=True,
        markup=False,
        highlight=False,
    )

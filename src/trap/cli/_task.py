from __future__ import annotations

from enum import StrEnum
from typing import Annotated

import typer
from rich.markup import escape

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


def _field(value: object, key: str) -> object:
    """``value[key]`` when ``value`` is an object, else None: a reply's shape is not a promise."""
    return value.get(key) if isinstance(value, dict) else None


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
    """Register a task on trapstreet with your CLI token. Authorized accounts only; others use the website."""
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
        # Sent, not left to the site: it defaults to desc, the website's form picks asc
        # for these two, and a direction can't be edited once the task exists.
        "ranking_direction": "asc" if metric in (RankingMetric.cost, RankingMetric.latency) else "desc",
        "visibility": "private" if private else "public",
    }
    if summary is not None:
        body["summary"] = summary

    try:
        reply = ApiClient(resolved.server, resolved.api_key).register_task(body)
    except ApiError as e:
        # The site's words, which may hold brackets: printed, not parsed as markup.
        raise _die(escape(str(e))) from None

    # The task exists from here on: whatever the reply looks like, report it rather than
    # crash, or an agent reads a traceback as failure, retries, and meets a 409.
    task = _field(reply, "task")
    registered = _field(task, "id")
    url = f"{resolved.server.rstrip('/')}/tasks/{registered if isinstance(registered, str) else task_id}"
    console.print(f"registered {url}", soft_wrap=True, markup=False, highlight=False)
    commit = _field(_field(task, "latest"), "commit_sha")
    if isinstance(commit, str):
        console.print(f"pinned at {commit[:12]}", markup=False, highlight=False)
    console.print(
        f"Needs a private judge? Bind its grading pack at {url}/grading",
        soft_wrap=True,
        markup=False,
        highlight=False,
    )

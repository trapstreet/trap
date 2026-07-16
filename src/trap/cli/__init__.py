from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from trap import __version__
from trap.auth import (
    DEFAULT_SERVER,
    ApiClient,
    ApiError,
    AuthMismatchError,
    AuthStore,
    effective_server,
    resolve_auth,
)
from trap.cli._auth import auth_app
from trap.cli._console import _die, _env_truthy, console, err_console
from trap.display import CaseProgress, render_submit_result
from trap.environment import EnvironmentDetector
from trap.git_ops import GitOpsError, LocalRepo, ParsedGitUrl
from trap.loader import ConfigError, TrapLoader, TraptaskLoader
from trap.models import Provenance, is_infra_error
from trap.report import OutputFormat, ReportHandle, renderer_factory
from trap.runner import TaskRunner

app = typer.Typer(help="AI prompt / agent / workflow / testing framework.")
app.add_typer(auth_app, name="auth")


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"tp {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            help="Show the trap version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """AI prompt / agent / workflow / testing framework."""


def _confirm_remote(url: str, *, trust: bool) -> None:
    """Gate trap's auto-download-and-run of a remote (git+ URL) source — the one place
    trap fetches and executes code the user may not have seen. Returns to proceed;
    raises to abort. Bypassed by --trust-remote / TRAP_TRUST_REMOTE (for CI / repeated
    runs); refuses (rather than running silently) when there's no TTY to confirm at."""
    if trust:
        return
    err_console.print(
        "[yellow]⚠  about to download and RUN code from a remote source:[/yellow]\n"
        f"     [bold]{url}[/bold]\n"
        "   trap will execute its setup command, the solution, and any judge/grader\n"
        "   scripts — arbitrary code from this repo runs on your machine."
    )
    if not sys.stdin.isatty():
        raise _die(
            "remote source needs confirmation; pass --trust-remote "
            "(or set TRAP_TRUST_REMOTE=1) to run it non-interactively"
        )
    if not typer.confirm("Continue?", default=False):
        raise typer.Exit(code=1)


def _confirm_unanchored(provenance: Provenance, *, allow: bool) -> None:
    """Gate run/submit when a side has no anchored git provenance.

    trapstreet accepts such uploads but its leaderboard hides runs it cannot pin to
    a commit — make the user acknowledge that up front instead of discovering it on
    the site. The reason travels in the provenance's `issue` field, so `tp submit`
    (which reads the saved report rather than re-probing the checkouts) names it
    too. `allow` (--allow-unanchored / TRAP_ALLOW_UNANCHORED, for CI) keeps the
    warning but skips the prompt; with no TTY and no `allow` it refuses.
    """
    sides = {"solution": provenance.solution, "task": provenance.task}
    missing = {name: side.issue for name, side in sides.items() if not side.repo}
    if not missing:
        return
    for name, reason in missing.items():
        suffix = f" ({reason})" if reason else ""
        err_console.print(f"[yellow]⚠  {name} has no git provenance{suffix}[/yellow]")
    err_console.print(
        "[yellow]   trapstreet accepts the submission, but the leaderboard hides runs that "
        "aren't anchored to a commit on a remote — run from a clean, committed checkout "
        "with an origin remote to make it rankable.[/yellow]"
    )
    if allow:
        return
    if not sys.stdin.isatty():
        raise _die(
            "unanchored provenance needs confirmation; pass --allow-unanchored "
            "(or set TRAP_ALLOW_UNANCHORED=1) to proceed non-interactively"
        )
    if not typer.confirm("Continue anyway?", default=False):
        raise typer.Exit(code=1)


@app.command()
def run(
    task: Annotated[str | None, typer.Argument()] = None,
    solution: Annotated[
        str | None,
        typer.Option("--solution", help="Solution to run: a local path or a git+ URL (default: cwd)."),
    ] = None,
    clone_to: Annotated[
        Path | None,
        typer.Option("--clone-to", help="Where to clone a git+ URL --solution (default: ./<repo>)."),
    ] = None,
    trust_remote: Annotated[
        bool,
        typer.Option(
            "--trust-remote",
            help="Skip the confirmation before downloading and running a remote "
            "(git+ URL) solution/task. Also settable via TRAP_TRUST_REMOTE=1.",
        ),
    ] = False,
    allow_unanchored: Annotated[
        bool,
        typer.Option(
            "--allow-unanchored",
            help="Skip the confirmation when the run has no git provenance (the "
            "leaderboard hides such runs). Also settable via TRAP_ALLOW_UNANCHORED=1.",
        ),
    ] = False,
    tags: Annotated[list[str] | None, typer.Option("--tag", "-t")] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o")] = OutputFormat.rich,
    fail_fast: Annotated[bool, typer.Option("--fail-fast")] = False,
    setup_solution: Annotated[
        bool,
        typer.Option(
            "--setup-solution",
            help="Force-run the solution's setup_cmd even when no remote pull brought new code.",
        ),
    ] = False,
    setup_task: Annotated[
        bool,
        typer.Option(
            "--setup-task",
            help="Force-run the task's setup_cmd even when no remote pull brought new code.",
        ),
    ] = False,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path(".trap"),
    environment: Annotated[
        bool,
        typer.Option(
            "--environment/--no-environment",
            help="Collect host machine environment info (CPU/RAM/OS/Python) into the report.",
        ),
    ] = True,
    cost: Annotated[
        bool,
        typer.Option(
            "--cost/--no-cost",
            help="Track LLM token usage and spend via the proxy (auto-detects providers from env).",
        ),
    ] = True,
) -> None:
    """Run a task against a solution.

    --solution is the solution to run: a local path, or a git+ URL to clone
    into ./<repo> (or --clone-to). Omit it to use the trap.yaml in the cwd.
    """
    # Gate trap's auto-download-and-run of any remote source before it happens — once
    # for a remote --solution, once for a remote task source (resolved from trap.yaml).
    trust = trust_remote or _env_truthy("TRAP_TRUST_REMOTE")
    if solution is not None and ParsedGitUrl.looks_remote(solution):
        _confirm_remote(solution, trust=trust)
    try:
        trap_yaml_loader = TrapLoader.from_solution(
            solution,
            clone_to,
            allow_remote=True,
            setup=setup_solution,
            progress_func=(
                (lambda m: console.print(f"[dim]{m}[/dim]")) if output == OutputFormat.rich else None
            ),
        )
        task_binding = trap_yaml_loader.resolve_task(task)
        if ParsedGitUrl.looks_remote(task_binding.source):
            _confirm_remote(task_binding.source, trust=trust)
        traptask_yaml_loader = TraptaskLoader.from_task(
            task_binding, trap_yaml_loader.trap_dir, setup=setup_task
        )
    except (GitOpsError, ConfigError, subprocess.CalledProcessError) as e:
        raise _die(e) from None

    # Record git provenance (repo + commit) of both checkouts — solution and task —
    # so the run is reproducible; an unanchored side carries an `issue` naming why.
    # Probed once, before any case runs: an unanchored checkout produces a report the
    # leaderboard will hide, so the user must acknowledge (or pre-authorise) that.
    provenance = Provenance(
        solution=LocalRepo.provenance_of(trap_yaml_loader.trap_dir),
        task=LocalRepo.provenance_of(traptask_yaml_loader.traptask_dir),
    )
    _confirm_unanchored(provenance, allow=allow_unanchored or _env_truthy("TRAP_ALLOW_UNANCHORED"))

    active_cases = traptask_yaml_loader.cases_with_tags(tags or [])

    started_at_local = datetime.now()
    ts = started_at_local.isoformat(timespec="seconds")
    report_handle = ReportHandle(workspace.resolve(), task_binding.alias, ts)

    runner = TaskRunner(
        trap_config=trap_yaml_loader.config,
        trap_dir=trap_yaml_loader.trap_dir,
        traptask_config=traptask_yaml_loader.traptask,
        traptask_dir=traptask_yaml_loader.traptask_dir,
        run_dir=report_handle.run_dir,
        cost_enabled=cost,
    )
    prog_console = console if output == OutputFormat.rich else None
    with CaseProgress(active_cases, console=prog_console) as prog:
        case_results, grader_metrics = runner.run(
            active_cases,
            fail_fast=fail_fast,
            on_case_start=prog.on_case_start,
            on_case_done=prog.on_case_done,
        )
    finished_at_utc = datetime.now(UTC)

    # Capture the host machine environment (CPU/RAM/OS/Python) unless disabled.
    # Detection is best-effort and must never abort a completed run.
    environment_info = None
    if environment:
        try:
            environment_info = EnvironmentDetector().detect()
        except Exception:
            environment_info = None

    report_data = report_handle.save(
        case_results=case_results,
        trap_config=trap_yaml_loader.config,
        started_at_utc=started_at_local.astimezone(UTC),
        finished_at_utc=finished_at_utc,
        grader_metrics=grader_metrics,
        provenance=provenance,
        environment=environment_info,
    )
    renderer_factory(output).render(report_data)
    # trap reports facts, not a verdict: per-case exit codes and scores never set the
    # exit code (read the grader / report.json to gate CI); trap-level failures (bad
    # config, git errors) exit 2 via _die. But a broken measuring apparatus is not a
    # fact about the solution: when the judge errored on *every* case, or the grader
    # errored, the scores are missing — not zero — so exit 3 to keep scripts from
    # reading an unscored run as a completed one.
    judge_failed = [r for r in case_results if is_infra_error(r.metrics)]
    judge_broken = (
        traptask_yaml_loader.traptask.judge is not None
        and bool(case_results)
        and len(judge_failed) == len(case_results)
    )
    grader_broken = traptask_yaml_loader.traptask.grader is not None and (is_infra_error(grader_metrics))
    if judge_broken:
        first = case_results[0]
        err_console.print(
            f"[red]error[/red]: judge failed on all {len(case_results)} cases — "
            f"scores are missing, not zero. First error: {first.metrics['error']}\n"
            f"  judge stderr: {report_handle.run_dir / first.case_id / 'judge' / 'stderr'}"
        )
    elif judge_failed:
        # Some cases scored, some didn't: still a completed run (exit unchanged),
        # but say loudly that those scores are missing, not zero.
        err_console.print(
            f"[yellow]warning[/yellow]: judge failed on {len(judge_failed)} of "
            f"{len(case_results)} cases ({', '.join(r.case_id for r in judge_failed[:5])}"
            f"{', …' if len(judge_failed) > 5 else ''}) — their scores are missing, not zero."
        )
    if grader_broken:
        err_console.print(
            f"[red]error[/red]: grader failed: {grader_metrics['error']}\n"
            f"  grader stderr: {report_handle.run_dir / 'grader' / 'stderr'}"
        )
    if judge_broken or grader_broken:
        raise typer.Exit(code=3)


@app.command()
def report(
    task: Annotated[str | None, typer.Argument()] = None,
    run: Annotated[str, typer.Argument()] = "latest",
    solution: Annotated[
        str | None,
        typer.Option("--solution", help="Local solution path holding trap.yaml (default: cwd)."),
    ] = None,
    output: Annotated[OutputFormat, typer.Option("--output", "-o")] = OutputFormat.rich,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path(".trap"),
) -> None:
    """Display a report for a task (defaults to latest run)."""
    try:
        task_alias = TrapLoader.from_solution(solution).resolve_task(task).alias
    except (GitOpsError, ConfigError) as e:
        raise _die(e) from None
    handle = ReportHandle(workspace.resolve(), task_alias, run)
    try:
        report_data = handle.load()
    except FileNotFoundError:
        raise _die(f"no report at {handle.report_json_path}; run `tp run` first") from None
    renderer_factory(output).render(report_data)


@app.command()
def submit(
    task: Annotated[
        str | None,
        typer.Argument(
            help="Task name (defaults to first task in trap.yaml). "
            "Used as both the local run dir and the trapstreet task_id.",
        ),
    ] = None,
    solution: Annotated[
        str | None,
        typer.Option("--solution", help="Local solution path holding trap.yaml (default: cwd)."),
    ] = None,
    workspace: Annotated[Path, typer.Option("--workspace", "-w")] = Path(".trap"),
    run: Annotated[str, typer.Option("--run", "-r", help="Which run to upload.")] = "latest",
    allow_unanchored: Annotated[
        bool,
        typer.Option(
            "--allow-unanchored",
            help="Skip the confirmation when the run has no git provenance (the "
            "leaderboard hides such runs). Also settable via TRAP_ALLOW_UNANCHORED=1.",
        ),
    ] = False,
) -> None:
    """Upload a report.json to trapstreet.

    Reads from the .trap/<task>/<run>/report.json workspace that `tp run`
    populated.
    """
    target = effective_server()
    resolved = resolve_auth(AuthStore().load(target))  # the profile issued for the target server
    if not resolved.api_key:
        hint = "" if target == DEFAULT_SERVER else f" --server {target}"
        raise _die(
            f"not logged in to {target}. Run [bold]tp auth login{hint}[/bold] "
            "or set [bold]TRAPSTREET_API_KEY[/bold]."
        )
    try:
        resolved.ensure_paired()
    except AuthMismatchError as e:
        raise _die(e) from None

    try:
        task_alias = TrapLoader.from_solution(solution).resolve_task(task).alias
    except (GitOpsError, ConfigError) as e:
        raise _die(e) from None
    report_handle = ReportHandle(workspace.resolve(), task_alias, run)
    try:
        report_data = report_handle.load()
    except FileNotFoundError:
        raise _die(f"no report at {report_handle.report_json_path}. Run [bold]tp run[/bold] first.") from None
    # Repeat the unanchored-provenance gate at upload time — the report records
    # what `tp run` saw, so the checkouts aren't re-probed here.
    _confirm_unanchored(
        report_data.provenance, allow=allow_unanchored or _env_truthy("TRAP_ALLOW_UNANCHORED")
    )
    report_path = report_handle.report_json_path

    client = ApiClient(resolved.server, resolved.api_key)
    try:
        resp_data = client.submit(report_path)
    except ApiError as e:
        raise _die(e) from None
    render_submit_result(resp_data)


# Hidden until the scaffold is implemented — registered but not advertised in `--help`.
@app.command(hidden=True)
def init() -> None:
    """Not implemented yet — write trap.yaml by hand (see the trap.yaml reference)."""
    console.print(
        "[yellow]tp init isn't implemented yet.[/yellow] Write trap.yaml by hand — "
        "see https://github.com/trapstreet/trap/blob/main/docs/reference/trap-yaml.md"
    )
    raise typer.Exit(code=2)

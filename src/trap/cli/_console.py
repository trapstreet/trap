"""Shared CLI console objects and small helpers used across command modules."""

from __future__ import annotations

import os

import typer
from rich.console import Console

console = Console()
# Warnings go to stderr so `-o json` stdout stays machine-parseable.
err_console = Console(stderr=True)


def _die(msg: object) -> typer.Exit:
    """Print an error and return an Exit(2) to raise."""
    console.print(f"[red]error[/red]: {msg}")
    return typer.Exit(code=2)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")

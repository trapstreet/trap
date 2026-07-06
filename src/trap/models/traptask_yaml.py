# Models for traptask.yaml (task author's config).
from __future__ import annotations

from pydantic import BaseModel


class SubprocessConfig(BaseModel):
    # How to invoke the judge/grader subprocess: the command (run via shlex.split,
    # cwd = traptask.yaml's directory) plus the env var carrying its manifest.
    cmd: str
    manifest_envvar: str = "TRAPTASK_MANIFEST"
    # Per-invocation wall-clock ceiling (seconds): a safety net against a hung or
    # runaway actor, not a time budget. On timeout the actor is killed and recorded
    # as an error on the run (exit 124) — it never crashes the run. Task-author owned,
    # so it is identical for every solution that runs this task version. The subclasses
    # set the per-actor default.
    timeout: int


class JudgeConfig(SubprocessConfig):
    # Runs once per case and may itself call an LLM (LLM-as-judge) → generous default.
    timeout: int = 300


class GraderConfig(SubprocessConfig):
    # Only aggregates the per-case results → fast, so a tighter default.
    timeout: int = 120


class DirsConfig(BaseModel):
    # paths relative to traptask.yaml; outputs dir is a runtime tmpdir, not declared here
    inputs: str = "inputs/"
    expected: str = "expected/"


class TraptaskCase(BaseModel):
    id: str
    description: str = ""  # free-form author note; trap neither consumes nor displays it
    tags: tuple[str, ...] = ()
    skip: bool = False


class TraptaskConfig(BaseModel):
    # Field order mirrors the canonical traptask.yaml layout.
    # Optional human-readable title for the task; task-author owned. Read from the task
    # repo (via the report's provenance.task), so it stays consistent across every
    # solution that runs this task version.
    name: str | None = None
    # Prepares the checkout (e.g. `uv sync`); task-author owned so every solution gets
    # an identical env. Auto-runs on a remote clone/update; else `tp run --setup-task`.
    setup_cmd: str | None = None
    dirs: DirsConfig = DirsConfig()
    # Advisory contract: filenames (and/or `stdout`/`stderr`) the solution writes.
    # Never enforced — the judge is the sole arbiter. Omit for dynamic outputs.
    declared_outputs: tuple[str, ...] = ()
    cases: tuple[TraptaskCase, ...]
    judge: JudgeConfig | None = None  # None → skip per-case scoring
    grader: GraderConfig | None = None  # None → skip overall aggregation

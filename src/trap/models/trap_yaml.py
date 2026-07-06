# Models for trap.yaml (solution author's config).
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Profile(BaseModel):
    # Self-reported engine identity, surfaced in the report. Both fields are
    # multi-valued (a run may use several models/frameworks); a scalar is
    # accepted as a convenience and normalised to a single-element list.
    model: tuple[str, ...] = ()
    framework: tuple[str, ...] = ()

    @field_validator("model", "framework", mode="before")
    @classmethod
    def _as_list(cls, v: Any) -> Any:
        if v is None:
            return ()
        if isinstance(v, str):
            return (v,)
        return v


class TaskBinding(BaseModel):
    # A task binding: which task this solution is run against. The map key is the
    # binding's `alias` — the solution author's handle for this task, which also
    # serves as the local run-dir name and the trapstreet task_id on submit. The
    # value is just where the task lives. Solution-invariant settings sit at the
    # top level of trap.yaml (see TrapConfig).
    alias: str = ""
    # required: local task dir XOR git+ URL (relative to trap.yaml), same polymorphic
    # form as --solution. A git+ URL is cloned into clone_to.
    source: str
    # clone target for a git+ source (relative to trap.yaml, or absolute);
    # omitted → hidden cache .trap/repos/<repo>. Only valid when source is a URL.
    clone_to: Path | None = None


class TrapConfig(BaseModel):
    # trap.yaml is one solution's file: its invariant settings sit at the top
    # level (flat, 1:1 with the YAML), and `tasks` is the collection of task
    # bindings it is run against.
    # Field order mirrors the canonical trap.yaml layout.
    # optional leaderboard identity; None → server auto-assigns a serial name
    name: str | None = None
    # self-reported engine identity (model/framework); plumbed to report.json `profile`
    profile: Profile = Field(default_factory=Profile)
    # Prepares the solution checkout once after clone (e.g. `uv sync`, `npm install`).
    # Solution-author owned. Auto-runs on a remote clone/update; else `tp run --setup-solution`.
    setup_cmd: str | None = None
    stdin: str | None = None  # optional: filename in inputs/{case_id}/ piped to the solution's stdin
    cmd: str  # run via shlex.split, cwd = the trap.yaml directory
    # env var carrying the run manifest (inputs_dir / outputs_dir); override if needed
    manifest_envvar: str = "TRAP_MANIFEST"
    # per-case wall-clock ceiling (seconds). A safety net against hangs/runaways, not a
    # fair budget — duration is recorded faithfully, so set it generously; a timed-out
    # case = "did not complete". Solution-author owned.
    timeout: int = 600
    # free-form escape hatch for author notes; tolerated but never written to the report
    extra: dict[str, Any] = {}
    tasks: dict[str, TaskBinding]

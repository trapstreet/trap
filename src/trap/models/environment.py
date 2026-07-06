from __future__ import annotations

from pydantic import BaseModel, Field


class Cpu(BaseModel):
    """CPU identity. `model` is the human brand string (py-cpuinfo `brand_raw`),
    which psutil does not expose."""

    model: str | None = None
    cores_physical: int | None = None
    cores_logical: int | None = None


class Environment(BaseModel):
    """Machine runtime environment captured at `tp run` — a fastfetch-like subset
    relevant to comparing runs across hosts. Every field is optional so a failed
    probe degrades to None rather than aborting the run."""

    os: str | None = None
    kernel: str | None = None
    arch: str | None = None
    cpu: Cpu = Field(default_factory=Cpu)
    memory_total_bytes: int | None = None

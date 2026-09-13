"""Exception types raised by more than one package. A leaf: imports nothing from trap."""

from __future__ import annotations


class ConfigError(Exception):
    """A trap.yaml / traptask.yaml that cannot be loaded — missing file, malformed
    YAML, failed schema validation, an unknown task alias, or no tasks/cases — or a task
    the runner refuses because it could hand a solution the answers. Carries a
    user-facing message; the CLI maps it to a clean error (exit 2, no traceback)."""

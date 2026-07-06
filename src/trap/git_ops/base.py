from __future__ import annotations

from collections.abc import Callable

# Progress callback used across clone/fetch/update steps; None disables progress output.
ProgressCallback = Callable[[str], None] | None


class GitOpsError(Exception):
    pass

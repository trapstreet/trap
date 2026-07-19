"""Workspace addressing: where a solution's runs live inside `.trap/`.

Layout: ``<workspace>/runs/<solution-key>/<task_alias>/<run>/``.

Two objects own this package's concerns: :class:`SolutionIdentity`
(``identity.py``) derives which solution a run belongs to and projects it to a
directory name (its ``dirname`` is the solution key), and :class:`Workspace`
(``store.py``) answers queries and does report IO against one `.trap/` store,
scoped to one (solution, task) pair fixed at construction. The key
carries the
run's *identity* inside the store, so ``latest`` is scoped per (solution, task)
by construction — a cwd mismatch between ``tp run`` and ``tp submit`` degrades
to a loud "not found" instead of silently reading another solution's report.
Runs live under the ``runs/`` namespace so run storage never collides with
sibling namespaces like ``repos/`` (the clone cache).
"""

from __future__ import annotations

from trap.workspace.identity import SolutionIdentity
from trap.workspace.store import Workspace

__all__ = ["SolutionIdentity", "Workspace"]

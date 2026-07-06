# trap — Coding Agent Reference

`trap` is a non-invasive CLI testing framework for AI workflows: it runs the **solution**
(the program under test) as a black-box subprocess and evaluates its outputs (stdout and/or
files). It knows nothing about how the solution is implemented. Solution and task repos are
fully decoupled — they share only an IO contract.

This file holds only what an agent editing the code must keep invariant: the **architecture
constraints**, the **ownership model**, and **pointers** to the reference docs. Everything
else — the IO contract, YAML fields, workspace layout, cost tracking — lives in `docs/`, the
single source of truth. Don't restate docs/ content here; two hand-synced copies drift.

## Architecture constraints

Pipeline: `loader → runner → judge → reporter`. Modules communicate **only** through pydantic
models (serialisable data); there is no shared mutable state. These rules keep a future Rust
rewrite cheap — do not violate them:

1. **Module boundaries** — loader / runner / judge / reporter talk only via pydantic models.
2. **Serialisable data only** — pass JSON-serialisable models across boundaries; no
   Python-only runtime objects.
3. **Stateless runner** — `runner` is a pure function: same inputs always produce same outputs.
4. **Stable JSON schema** — the `--json` / `report.json` output is a public contract; changes
   must stay backwards-compatible.
5. **No dynamic Python features** in cross-module interfaces (no metaclasses, no dynamic
   attributes) — a plain field maps cleanly to a Rust struct field.

`TaskRunner._iter_cases()` is deliberately a separate generator (not inlined into `run()`) to
preserve a seam for future async / threaded case execution — the for-loop body can be swapped
for `asyncio.gather` or a thread-pool map without restructuring `run()`.

Likely first Rust targets: `runner`, `reporter`, the CLI entry point. `judge/custom` stays
Python (it executes user-written Python).

## Ownership model

Solution and task are owned by different people, live in different repos, and meet only at the
IO contract.

- **Task author** owns `traptask.yaml`, `judge.py`, `grader.py`, `inputs/`, `expected/` — what
  the solution must do and how it is scored.
- **Solution author** owns `trap.yaml` and the solution code — how the solution runs.

Two things are **task-author owned by design**, so they are identical for every solution run
against a given task version (this is what keeps runs reproducible and comparable):

- the task `setup_cmd` (prepares the task checkout), and
- the `judge` / `grader` `timeout`s (safety nets against a hung/runaway actor, not budgets).

Their solution-side mirrors — `trap.yaml`'s `setup_cmd` and per-case `timeout` — are
**solution-author owned**: each solution only prepares and limits itself, so they don't affect
cross-solution comparability. The two setups are independent; `--setup-solution` and
`--setup-task` force each side separately.

## Reference docs (the source of truth)

| Topic | File |
|---|---|
| IO contract (`TRAP_MANIFEST` / `TRAPTASK_MANIFEST`, judge & grader) | `docs/reference/io-contract.md` |
| `trap.yaml` fields (solution side) | `docs/reference/trap-yaml.md` |
| `traptask.yaml` fields (task side) | `docs/reference/traptask-yaml.md` |
| `.trap/` workspace layout | `docs/reference/workspace.md` |
| CLI commands & flags | `docs/reference/cli.md` |
| Cost tracking (proxy, providers, pricing, internals) | `docs/guides/cost-tracking.md` |
| Package graph & core data models | `docs/code-map.md` |
| Writing a solution / a task | `docs/guides/writing-solution.md` · `docs/guides/writing-task.md` |

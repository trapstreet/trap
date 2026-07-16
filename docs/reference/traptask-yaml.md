# traptask.yaml reference

The task author's config, in the task directory alongside `inputs/`, `expected/`, and any
judge/grader scripts. **Entirely optional**: if absent, trap scans `inputs/` and treats
each subdirectory as a case in output-only mode (no judge/grader).

## Example

```yaml
name: Reference task          # optional title (task-author owned)

dirs:
  inputs: inputs/             # default
  expected: expected/         # default

cases:
  - id: case_one
    description: optional label
    tags: [smoke]
  - id: case_two
    skip: true

judge:                        # optional: per-case scoring
  cmd: uv run python judge.py
  manifest_envvar: TRAPTASK_MANIFEST   # default
  timeout: 300                # optional: per-case hang ceiling (s)

grader:                       # optional: overall aggregation
  cmd: uv run python grader.py
  timeout: 120                # optional: hang ceiling (s)

setup_cmd: uv sync            # optional: prepare the checkout (e.g. install judge deps)
```

## Fields

- **`name`** — optional title. Task-author owned; consumers read it from the task repo via the run's `provenance.task`, so it stays identical across solutions.
- **`dirs.inputs` / `dirs.expected`** — case input / expected dirs, relative to `traptask.yaml` (defaults `inputs/`, `expected/`).
- **`cases[]`** — `id` (required; matches an `inputs/<id>/` dir), `description` (free-form author note; trap doesn't show it), `tags` (filter with `tp run -t`), `skip` (bool — a skipped case is not run and never appears in the report).
- **`judge`** / **`grader`** — each optional; a subprocess with `cmd` (shlex-split, cwd = task dir), `manifest_envvar` (default `TRAPTASK_MANIFEST`), and `timeout`. Omit `judge` → cases run unscored; omit `grader` → no aggregation. Each gets `TRAPTASK_MANIFEST` and prints JSON — see the [IO contract](io-contract.md).
- **`setup_cmd`** — shell command to prepare the checkout (cwd = task dir). Task-author owned, so every solution on this task commit gets the same env. Auto-runs when a remote pull brings new code; force on a pinned/local source with `tp run --setup-task`.
- **`declared_outputs`** — optional, **advisory** list of what a solution produces: output filenames and/or the tokens `stdout` / `stderr` for the standard streams. A published contract for solution authors; trap never enforces it (the judge is the sole arbiter). Omit for dynamic outputs.

## judge / grader timeout

Per-subprocess wall-clock ceiling (seconds) — a **safety net against a hung/runaway
actor**, not a budget. On timeout trap kills it and records `{"error": ..., "infra": true}`
on that case's `metrics` (or `grader_metrics`), exactly as for a non-zero exit or non-JSON
output, so one stuck actor never crashes the run; its `meta.json` shows exit `124`. The
`infra: true` marker is how report consumers tell a trap-folded failure from an actor's
own verdict that happens to carry an `error` field. Defaults
differ by role — `judge` `300` (per case, may call an LLM), `grader` `120` (aggregation
only). Both are task-author owned, identical for every solution on this task version.
Raise `judge.timeout` for a slow LLM-as-judge.

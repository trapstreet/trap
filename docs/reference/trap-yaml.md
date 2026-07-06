# trap.yaml reference

The solution author's config, next to the solution code. Invariant settings sit at the
top level; `tasks:` is the collection of task bindings the solution runs against.

## Example

```yaml
name: claude-sonnet-baseline   # optional leaderboard identity
profile:                       # optional engine identity → report.json
  model: claude-sonnet-4       # model(s) used; scalar or list
  framework: claude-code       # what drove the model; scalar or list
setup_cmd: uv sync             # optional: prepare the checkout once after clone
stdin: input.json              # optional: pipe this input file to the solution's stdin
cmd: uv run python solution.py # required: how to invoke the solution
manifest_envvar: TRAP_MANIFEST # optional: override the manifest env var name
timeout: 600                   # optional: per-case hang ceiling (s)
extra: { notes: anything }     # optional: author notes; never written to the report

tasks:                         # task bindings, keyed by alias
  test:
    source: ../task            # required: local path or git+ URL (cloned)
    # clone_to: .trap/repos/task   # optional clone target for a git+ URL
```

## Solution fields (top level)

- **`cmd`** (required) — shell command to invoke the solution; shlex-split, cwd = the `trap.yaml` dir.
- **`setup_cmd`** — prepares the checkout once (e.g. `uv sync`). Solution-author owned. Auto-runs on a remote clone/update; force with `tp run --setup-solution`.
- **`stdin`** — filename in `inputs_dir` piped to the solution's stdin.
- **`timeout`** — per-case wall-clock ceiling (seconds, default 600). A safety net against hangs, **not** a budget — real `duration` is recorded; a timed-out case = exit 124.
- **`manifest_envvar`** — name of the env var carrying the run manifest (default `TRAP_MANIFEST`).
- **`profile`** — self-reported identity written to the report; **strict**, only `model` and `framework` (each scalar or list).
- **`extra`** — free-form dict for author notes; tolerated but never written to the report.
- **`name`** — optional leaderboard identity for `tp submit` (else the server assigns one).
- **cost** — not a field; a CLI toggle (`tp run --no-cost`). See [cost tracking](../guides/cost-tracking.md).

## `tasks` block

A map of **alias → binding**. The alias is your handle (`tp run <alias>`), the local
run-dir name, and the trapstreet task id on submit; with one task, `tp run` uses it
automatically. Each binding:

- **`source`** (required) — where the task lives, relative to `trap.yaml`: a local path **or** a git+ URL (cloned).
- **`clone_to`** — clone target for a git+ `source` (default: hidden cache `.trap/repos/<repo>`). Only valid for a URL.

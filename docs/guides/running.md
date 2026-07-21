# Running & reporting

## tp run

Run from the directory containing `trap.yaml`:

```bash
tp run                # solution in cwd, first task in trap.yaml
tp run ./variant      # another solution by path
tp run --task test    # a named task
tp run -t smoke       # only cases tagged `smoke` (repeatable)
tp run --output json  # machine-readable instead of the rich table
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | solution to run: a local path or a git+ URL (cloned) |
| `--task` | first task | which task (the `tasks:` alias) to run |
| `--workspace / -w` | `.trap` | where to write run artifacts |
| `--output / -o` | `rich` | renderer: `rich` or `json` |
| `--clone-to` | `./<repo>` | where to clone a git+ URL `SOLUTION` |
| `--trust-remote` | `false` | skip the confirmation before running a remote source (see below) |
| `--tag / -t` | (none) | filter cases by tag; repeatable |
| `--fail-fast` | `false` | stop after the first case whose solution exits non-zero |
| `--setup-solution` / `--setup-task` | `false` | force the solution's / task's `setup_cmd` |
| `--cost / --no-cost` | on | track LLM tokens/spend via the proxy |
| `--environment / --no-environment` | on | record host CPU/RAM/OS/Python in the report |

### Remote sources

A remote `git+<url>` solution (or a task `source:` that is a git+ URL) makes trap
**download and run code you may not have seen** — its `setup_cmd`, the solution, and any
judge/grader. trap asks for confirmation first; pre-authorise with `--trust-remote` or
`TRAP_TRUST_REMOTE=1`. With no TTY and no authorisation it refuses rather than running
silently. Local sources are never gated.

### Exit codes

trap reports facts, not a verdict — a completed run exits `0` regardless of per-case
exit codes or scores. Gate CI on the grader output / `report.json`.

| Code | Condition |
|---|---|
| `0` | the run completed |
| `2` | trap-level failure — bad config, git error, declined remote, etc. |

## tp report

Re-render a stored run without re-executing the solution:

```bash
tp report                                       # latest run of the first task
tp report --task test --run 2026-05-09T14:30:00 # a specific run by timestamp
```

Takes the same `SOLUTION` argument and `--task`, `--workspace`, `--output` flags as
`tp run`. Artifacts live under `.trap/runs/<solution-key>/<task>/<timestamp>/`; full
layout: [workspace reference](../reference/workspace.md).

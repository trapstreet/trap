# Quick start

Runs the `examples/echo/` example end to end (~5 min). The echo solution reads JSON from
stdin and prints its `message` field; the task scores that output.

## Install

trap needs [uv](https://docs.astral.sh/uv/getting-started/installation/). Then:

```bash
uv tool install trap-cli       # from PyPI
# or latest main:
uv tool install "git+https://github.com/trapstreet/trap.git"
```

The command is `tp` (check with `tp --help`).

## Solution side

`examples/echo/solution/echo.py` reads stdin, prints `message`, exits 1 if it's missing.
Its `trap.yaml`:

```yaml
cmd: uv run python echo.py
stdin: input.json          # pipe inputs/<case>/input.json into stdin
tasks:
  test: { source: ../task }
```

## Task side

`examples/echo/task/traptask.yaml`:

```yaml
cases:
  - id: contains_basic
    tags: [smoke]
  - id: exit_code_failure
  - id: skipped_example
    skip: true

judge:  { cmd: uv run python judge.py }
grader: { cmd: uv run python grader.py }
```

`skip: true` marks a case as skipped: it is not run and never appears in the report
(so `skipped_example` above is defined but produces no row).

Each case has an `inputs/<id>/` directory. The judge reads the solution's output and
prints a score; the grader aggregates all cases — see the
[IO contract](reference/io-contract.md).

## Run it

From `examples/echo/solution/`:

```bash
tp run                # default task
tp run --task test    # a named task
tp run -t smoke       # only `smoke`-tagged cases
tp run --output json  # machine-readable
```

Artifacts go to `.trap/runs/<solution-key>/<task>/<timestamp>/`. Re-display a
stored run without re-executing it:

```bash
tp report                                       # latest
tp report --task test --run 2026-05-09T14:30:00 # a specific run
```

## Next

[Writing a solution](guides/writing-solution.md) ·
[Writing a task](guides/writing-task.md) ·
[Running & reporting](guides/running.md)

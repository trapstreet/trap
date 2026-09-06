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
| `--live / --no-live` | on | mirror progress to your paired trapstreet account (see below) |
| `--server` | the paired one | which trapstreet server to mirror to |

### Live progress sync

When the CLI is paired with a trapstreet account (`tp auth login`), `tp run` mirrors the
run's progress to that account as it happens and prints the page to watch it on:

```
live · https://trapstreet.run/runs/r-2f8c…
```

That page is **private to you**. Sync publishes nothing: it does not upload the report, does
not enter a leaderboard, and does not make the run visible to anyone else. Publishing is
still `tp submit`, and still only when you ask for it.

What crosses the wire is a fixed list of progress facts — case *ordinals* (`3 of 12`), a
pass / fail / error verdict, scores, durations, cost, and the run's exit code. Case names,
inputs, expected answers, your solution's output, stdout, file paths, environment variables
and command lines never do.

Sync is off when there is no stored token — an unpaired CLI runs exactly as before and says
nothing about it. Turn it off explicitly with `--no-live`, or for every run in a shell with
`TRAP_NO_LIVE=1`.

**It cannot affect the run.** No network, an expired token, a full disk or a bug in sync
leaves the solution, judge, grader, `report.json` and the exit code exactly as they would
have been with `--no-live`. Progress that could not be sent is written to the run's outbox
(see [workspace](../reference/workspace.md)) and reported in one line at the end:

```
live sync: 7 progress event(s) not delivered — they stay in this run's outbox
```

Send them later with [`tp sync`](#tp-sync). The CLI leaves no background service running,
so nothing is delivered after `tp run` exits until you ask for it.

### Remote sources

A remote `git+<url>` solution (or a task `source:` that is a git+ URL) makes trap
**download and run code you may not have seen** — its `setup_cmd`, the solution, and any
judge/grader. trap asks for confirmation first; pre-authorise with `--trust-remote` or
`TRAP_TRUST_REMOTE=1`. With no TTY and no authorisation it refuses rather than running
silently. Local sources are never gated.

### Exit codes

Solution exit codes and scores do not set `tp run`'s exit code. To gate CI on the
solution's result, read the grader output / `report.json`. Failures in the judge or
grader can set exit `3`, because the run is missing scores.

| Code | Condition |
|---|---|
| `0` | the run completed; neither every-case judge failure nor grader failure occurred |
| `2` | trap-level failure — bad config, git error, declined remote, etc. |
| `3` | the judge failed on every case, or the grader failed |

A judge or grader fails if it exits non-zero, times out (`124`), or exits `0`
without valid JSON output (`125`). A judge failure on only some cases does not
by itself change `tp run`'s exit code; those cases have missing scores, not zero scores. The
report is saved before exit `3`, so it remains available for diagnosis. See the
[CLI reference](../reference/cli.md#tp-run) for the full contract.

## tp sync

Deliver a tracked run's queued progress after the fact — the network came back, or the run
finished offline:

```bash
tp sync                                       # latest run of the first task
tp sync --task test --run 2026-05-09T14:30:00 # a specific run
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | local solution path holding `trap.yaml` |
| `--task` | first task | task alias (the `tasks:` key) |
| `--run / -r` | `latest` | which run to sync |
| `--workspace / -w` | `.trap` | directory containing run artifacts |
| `--server` | the run's own | must match the server the run was tracked against |

The queue belongs to the account and server it was created under. `tp sync` checks the
current credential against that frozen identity first: a rotated token for the same person
continues normally, a different account is refused and the events stay on disk. A `--server`
that disagrees with the run's own is refused too — a queue cannot move servers.

`--task` names the alias in your `trap.yaml`, the same one you ran with. It is the solution
author's own label and need not match the website's task id, so use the same alias for
`tp run`, `tp sync`, `tp report` and `tp submit`.

Nothing to send, a run that was never tracked, and no network are all ordinary outcomes: they
report what happened and exit `0`. Only a trap-level problem — bad arguments, an unreadable
workspace, an account that may not have this queue — exits `2`. Sync never changes a run's
own `0` / `2` / `3` exit code, never rewrites its report, and never publishes.

If part of the queue was lost (a cleaned-up or truncated outbox), the server can never
acknowledge past the hole. `tp sync` does not pretend otherwise: it sends a **checkpoint**
— the run's execution status and how many cases finished, rebuilt from what survived — and
the site then shows the run's history as incomplete rather than as complete-but-wrong.

## tp report

Re-render a stored run without re-executing the solution:

```bash
tp report                                       # latest run of the first task
tp report --task test --run 2026-05-09T14:30:00 # a specific run by timestamp
```

Takes the same `SOLUTION` argument and `--task`, `--workspace`, `--output` flags as
`tp run`. Artifacts live under `.trap/runs/<solution-key>/<task>/<timestamp>/`; full
layout: [workspace reference](../reference/workspace.md).

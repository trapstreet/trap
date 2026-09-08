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
| `--site-grading / --no-site-grading` | on | for an admitted evaluation, let the site judge each answer (see below) |
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

A run that **starts offline** is not lost either. The sender keeps trying to open the run's
session on the site for as long as the run lasts (backing off to at most 30 seconds between
attempts), sends nothing until that succeeds, and then delivers everything from the outbox in
order. If the network never comes back, `tp sync` opens the session first and does the same.
Progress a network drop loses **mid-run** is re-sent from the outbox too, oldest first, on
the sender's next wake (at most every ten seconds), so the site's view catches up while the
run is still going; whatever is still undelivered when `tp run` exits is reported once and
left for `tp sync`.

While a case is running and nothing else has happened for ten seconds, the sender sends a
heartbeat so a long case reads as "still running" on the site rather than as lost contact.

**What the site is told about the run.** Beside progress, `tp run` describes the run once
when it opens and once when it ends, so the run page can say what it was made of: tp as the
launcher (and the agent that launched tp, if it names itself with `TRAP_AGENT`), the model
and framework `trap.yaml` declares, the machine (the same OS / CPU / RAM / Python block as
`report.json`), the solution's and task's commits, and at the end how long the solver took
and what the cost proxy counted per model. Skills and tools are reported as *unsupported* —
tp does not see inside the solver — and `--no-environment` / `--no-cost` report their part
as *disabled*. A part tp did not report is shown as **not reported, never as zero**. None of
it names a case, and none of it is part of a score. See [the reference](../reference/cli.md#tp-run).

**Whose run it is.** `tp auth login` verifies the token with the server and stores the
account it belongs to; every run freezes that account into its sidecar before the first
case, without a network call. `tp sync` only ever delivers a run to the account it was
frozen to — under any later token of that account. A run tracked before the pairing was
verified has no frozen owner, and `tp sync` refuses to hand it to whoever is logged in now
unless you say so with `--claim`.

### Site grading

Some tasks are **admitted evaluations** on trapstreet: the site holds the reference answers
and runs the task's own judge over what you submit. For those, a paired `tp run` hands each
case's answer — the solver's stdout, nothing else — to the site as the case finishes, and
prints where the site's verdicts will appear:

```
graded on site · https://trapstreet.run/runs/rs_…
```

The local judge still runs, and its scores are still what `report.json` and the terminal
show; treat them as a **preview**. The site's verdicts are the ones that count for the
evaluation, and they appear on that page as each case is graded (the numbers arrive
together once every case is). The report records the graded run under `site_grading`.

This is fail-open. If the site cannot be reached when the run starts, or the task is not an
admitted evaluation, the run is judged locally and nothing is submitted — then or later. Once
the graded run is open, every answer is recorded on disk before it is sent and retried in the
background if the site does not take it, so a dropped request costs nothing; the site answers
a receipt per case, and the run ends with one line saying where the answers stand:

```
site grading: 3 of 4 answer(s) submitted; 1 skipped by the site (c2: SOLVER_ERRORED) — the site's run stays unfinished
```

Anything still unconfirmed when `tp run` exits stays in the run's answers outbox for
[`tp sync`](#tp-sync), which resends it to the same graded run. None of this changes the
run's exit code. Turn it off for a run with `--no-site-grading`, or everywhere with
`TRAP_NO_SITE_GRADING=1`.

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

Deliver a tracked run's queued progress — and, for a run graded on the site, the answers the
site never confirmed — after the fact: the network came back, or the run finished offline:

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
| `--claim` | `false` | adopt a run that froze no account into the one you are logged in as |

The queue belongs to the account and server it was created under. `tp sync` checks the
current credential against that frozen identity first: a rotated token for the same person
continues normally, a different account is refused and the events stay on disk. A `--server`
that disagrees with the run's own is refused too — a queue cannot move servers.

A run whose sidecar froze **no** account — tracked before `tp auth login` (or `tp auth
status`) had verified the pairing — is refused too, because whoever is logged in now is not
necessarily who ran it. Pass `--claim` to adopt it explicitly; the run then freezes your
verified account and behaves like any other from then on.

`tp sync` does the same three things the run's own sender does, in the same order: verify
the frozen identity, make sure the run's session exists on the site (opening it if the run
never reached the server), then deliver the queue. Nothing is sent before the session exists.

`--task` names the alias in your `trap.yaml`, the same one you ran with. It is the solution
author's own label and need not match the website's task id, so use the same alias for
`tp run`, `tp sync`, `tp report` and `tp submit`.

A run graded on the site gets the same treatment for its answers: `tp sync` re-reads each
unconfirmed answer from the run directory, posts it to the same graded run, and prints a
second line for that half — what was submitted, and anything the site skipped or rejected.

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

# CLI reference

The installed command is `tp`.

## tp run

Run a task against a solution (the positional argument; defaults to the `trap.yaml` in the cwd).

```
tp run [SOLUTION] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | solution to run: a local path or a git+ URL (cloned) |
| `--task` | first task | task alias (the `tasks:` key) to run |
| `--workspace / -w` | `.trap` | directory for run artifacts |
| `--output / -o` | `rich` | renderer: `rich` or `json` |
| `--clone-to` | `./<repo>` | where to clone a git+ URL `SOLUTION` |
| `--trust-remote` | `false` | skip the confirmation before running a remote source (see below) |
| `--allow-unanchored` | `false` | skip the confirmation for a run with no git provenance (see below) |
| `--tag / -t` | (none) | filter cases by tag; repeatable |
| `--fail-fast` | `false` | stop after the first case whose solution exits non-zero |
| `--setup-solution` / `--setup-task` | `false` | force the solution's / task's `setup_cmd` |
| `--cost / --no-cost` | on | track LLM tokens/spend via the proxy |
| `--environment / --no-environment` | on | record host CPU/RAM/OS/Python in the report |
| `--live / --no-live` | on | mirror progress to the paired trapstreet account (see below) |
| `--server` | the paired one | which trapstreet server to mirror progress to; also `TRAPSTREET_URL` |

**Live progress sync.** With a stored token (`tp auth login`), `tp run` mirrors progress to
that account while the run happens and prints the private page for it. It **publishes
nothing** — no report upload, no leaderboard entry, nothing visible to anyone else; that is
`tp submit`'s job alone. Only progress facts leave the machine: case ordinals (never case
names), a `passed` / `failed` / `error` verdict, scores, durations, cost and the exit code —
never inputs, expected answers, solution output, stdout, paths, environment or command
lines. Sync is off when no token is stored, off with `--no-live`, and off everywhere with
`TRAP_NO_LIVE=1`.

Sync can never change the run: with no network, a rejected token, a full disk or a bug in
sync, the solution, judge, grader, `report.json` and the exit code are identical to a
`--no-live` run. Undelivered progress stays in the run's outbox and is reported in one line
at the end; [`tp sync`](#tp-sync) delivers it later. No background service survives `tp run`,
so nothing is sent after it exits until you ask.

When a run had a live session, its `report.json` carries the session's `client_run_id`, which
is how a later `tp submit` lands on the same run page instead of creating a second one.
Reports from runs without a session — and from older CLIs — simply have no such field and
upload unchanged.

**Remote sources.** A remote `git+<url>` solution (or a task whose `source` is a git+
URL) makes trap **download and run code you may not have seen** — its `setup_cmd`, the
solution, and any judge/grader. trap asks for confirmation `[y/N]` first; pre-authorise
with `--trust-remote` or `TRAP_TRUST_REMOTE=1`. With no TTY and no authorisation it
refuses rather than running silently. Local sources are never gated.

**Unanchored runs.** trap records the git provenance (`{repo, commit}`) of the solution and
task checkouts. When either side can't be anchored — not a git repo, no origin remote, no
commit yet, or uncommitted changes — trapstreet still accepts the upload, but the
leaderboard **hides** the run. `tp run` therefore warns and asks for confirmation `[y/N]`
first; pre-authorise with `--allow-unanchored` or `TRAP_ALLOW_UNANCHORED=1` (the warning
still prints). With no TTY and no authorisation it refuses. `tp submit` folds this warning
into its own pre-submit confirmation (see below).

**Exit codes.** trap reports facts, not a verdict — a completed run exits `0` regardless
of per-case exit codes or scores (gate CI on the grader output / `report.json`). `2`
means a trap-level failure: bad/missing config, git error, declined remote, etc. `3`
means the measuring apparatus broke: the judge failed on **every** case, or the grader
failed — the scores are missing, not zero. An actor fails purely by its exit code
(recorded as `judge_exit_code` per case, `grader_exit_code` at the top level): non-zero, a
timeout (`124`), or a clean exit `0` whose stdout wasn't JSON (`125`). Exit `0` is a pass
whatever it printed — the output never decides. A judge failure on only *some* cases stays
exit `0`.

## tp sync

Send a tracked run's queued progress to trapstreet after the fact. Requires auth for the
server the run was tracked against.

```
tp sync [SOLUTION] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | local solution path holding `trap.yaml` |
| `--task` | first task | task alias (the `tasks:` key in this solution's `trap.yaml`) |
| `--run / -r` | `latest` | which run to sync |
| `--workspace / -w` | `.trap` | directory containing run artifacts |
| `--server` | the run's own | the server the run was tracked against; a disagreement is refused |

`tp sync` publishes nothing and changes nothing about the run — not its `report.json`, not
its `0` / `2` / `3` exit code. It only delivers progress the network never took.

**Identity.** A run's outbox is frozen to the account and server that created it. `tp sync`
resolves the credential for *that* server (not `TRAPSTREET_URL`, not the default), fetches
the account it belongs to, and compares. A rotated token for the same account continues; a
different account is refused and the events stay on disk for their owner. `--server` exists
to state the expected target, not to redirect a queue: a value that disagrees with the run's
own is refused.

**Exit codes.** `0` for everything that leaves the user informed and the data intact —
delivered, nothing to deliver, a run that was never tracked, or no network (the events stay
queued; run it again later). `2` only for a trap-level failure: bad/missing config, an
unknown run, an unreadable workspace, no credential for that server, a token the server
rejects, or a queue belonging to another account. A rejected token stops there — no other
stored credential and no other server is tried.

**Gap recovery.** If events the server still needs are gone (a cleaned-up or corrupted
outbox), its contiguous acknowledgement can never move past the hole, and re-sending cannot
help. `tp sync` then posts a **checkpoint** instead: the run's execution status and how many
of how many cases finished, rebuilt from the events that survived and the saved report. The
server opens a new producer generation, and the run's history is marked incomplete on the
site. The checkpoint reports local execution state only — it cannot overwrite a report, a
final status, or any score the server already holds. A run whose session id never reached
disk cannot be synced at all: a fresh id would be a different run, so trap says the run was
never tracked rather than inventing one.

**Task identity.** As for `tp submit`: `--task` names the local alias from `trap.yaml`, which
is chosen by the solution author and is **not** the website's task id.

## tp report

Re-render a stored run without re-executing the solution.

```
tp report [SOLUTION] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | local solution path holding `trap.yaml` |
| `--task` | first task | task alias |
| `--run / -r` | `latest` | timestamp directory name, or `latest` |
| `--workspace / -w` | `.trap` | directory containing run artifacts |
| `--output / -o` | `rich` | renderer: `rich` or `json` |

## tp submit

Upload a run's `report.json` to trapstreet. Requires auth (`tp auth login` or
`TRAPSTREET_API_KEY`).

The current CLI runs the solution, judge, and grader locally with `tp run`.
`tp submit` uploads that saved report; it does not ask the website to run the
judge or grader.

```
tp submit [SOLUTION] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | local solution path holding `trap.yaml` |
| `--task` | first task | task alias (the `tasks:` key in this solution's `trap.yaml`) |
| `--run / -r` | `latest` | which run to upload |
| `--workspace / -w` | `.trap` | directory containing run artifacts |
| `--yes / -y` | `false` | skip the pre-submit confirmation and publish (for CI / scripts) |
| `--allow-unanchored` | `false` | skip the pre-submit confirmation (like `--yes`) and acknowledge that a run with no git provenance is hidden from the leaderboard; also `TRAP_ALLOW_UNANCHORED=1` |

**Task identity.** `--task` selects the local task binding and saved run. The alias
is chosen by the solution author and need not match the website's task ID. The
website identifies the task version from the report's
`provenance.task.{repo, commit, subdirectory}`, so use the same local alias for
`tp run`, `tp report`, and `tp submit`.

**Pre-submit confirmation.** A submit is an irreversible external publish, so trap echoes
what it is about to upload — solution, run id → server, a neutral result tally, and the
anchor status of each checkout, all read from the local `report.json` — then asks `Submit
to <server>? [y/N]`. The intent table prints even when the prompt is skipped, so a CI log
still records the payload. Any of `--yes`, `--allow-unanchored`, or
`TRAP_ALLOW_UNANCHORED=1` skips the prompt; with no TTY and none of them, submit refuses.
When a checkout is unanchored the leaderboard warning (see `tp run`) prints here too.

**Server/token resolution.** The target server is `TRAPSTREET_URL` env >
`https://trapstreet.run`. The token is `TRAPSTREET_API_KEY` env > the stored credential *for
that server* (`tp auth login --server <url>`). Tokens are stored per server and never
borrowed across servers, so pointing `TRAPSTREET_URL` at a server you haven't paired makes
`tp submit` report logged-out rather than send another server's credential. `tp auth status`
reports the same resolved pair.

## tp auth

```
tp auth login  [--server URL] [--with-token] [--timeout SECONDS]
tp auth logout [--server URL]
tp auth status [--server URL] [--verify / --no-verify]
```

`login` opens a browser for OAuth by default (only on `https://trapstreet.run`); pass
`--with-token` to read an API key from stdin instead (for CI / custom servers). Tokens are
stored one credential per server in `~/.config/trapstreet/auth.json` (mode 600), keyed by server
URL — logging in to one server never displaces another's. Legacy single-token files are migrated
to the keyed shape automatically on first read. All three commands default to
`https://trapstreet.run`; `--server` (or `TRAPSTREET_URL`) selects another credential.

One credential per server also decides what `tp sync` may do: it resolves the token for the
server a run's queue was created against, and refuses rather than reaching for another
server's credential when there is none.

`status` shows the server and token **in effect** — after env overrides, each annotated with
its source (`env` / `stored` / `default`) — exactly what `tp submit` would use. Targeting a
server with no stored credential reports logged-out for it (exit 1). Unless `--no-verify`, it
then pings the server to check the token.

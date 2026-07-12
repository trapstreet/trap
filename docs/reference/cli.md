# CLI reference

The installed command is `tp`.

## tp run

Run a task against a solution (from the directory holding `trap.yaml`, or use `--solution`).

```
tp run [TASK] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `TASK` (positional) | first task | task alias (the `tasks:` key) to run |
| `--solution` | cwd | solution to run: a local path or a git+ URL (cloned) |
| `--clone-to` | `./<repo>` | where to clone a git+ URL `--solution` |
| `--trust-remote` | `false` | skip the confirmation before running a remote source (see below) |
| `--allow-unanchored` | `false` | skip the confirmation for a run with no git provenance (see below) |
| `--tag / -t` | (none) | filter cases by tag; repeatable |
| `--output / -o` | `rich` | renderer: `rich` or `json` |
| `--fail-fast` | `false` | stop after the first case whose solution exits non-zero |
| `--setup-solution` / `--setup-task` | `false` | force the solution's / task's `setup_cmd` |
| `--cost / --no-cost` | on | track LLM tokens/spend via the proxy |
| `--environment / --no-environment` | on | record host CPU/RAM/OS/Python in the report |
| `--workspace / -w` | `.trap` | directory for run artifacts |

**Remote sources.** A remote `--solution git+<url>` (or a task whose `source` is a git+
URL) makes trap **download and run code you may not have seen** — its `setup_cmd`, the
solution, and any judge/grader. trap asks for confirmation `[y/N]` first; pre-authorise
with `--trust-remote` or `TRAP_TRUST_REMOTE=1`. With no TTY and no authorisation it
refuses rather than running silently. Local sources are never gated.

**Unanchored runs.** trap records the git provenance (`{repo, commit}`) of the solution and
task checkouts. When either side can't be anchored — not a git repo, no origin remote, no
commit yet, or uncommitted changes — trapstreet still accepts the upload, but the
leaderboard **hides** the run. `tp run` and `tp submit` therefore warn and ask for
confirmation `[y/N]` first; pre-authorise with `--allow-unanchored` or
`TRAP_ALLOW_UNANCHORED=1` (the warning still prints). With no TTY and no authorisation they
refuse.

**Exit codes.** trap reports facts, not a verdict — a completed run exits `0` regardless
of per-case exit codes or scores (gate CI on the grader output / `report.json`). `2`
means a trap-level failure: bad/missing config, git error, declined remote, etc.

## tp report

Re-render a stored run without re-executing the solution.

```
tp report [TASK] [RUN] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `TASK` (positional) | first task | task alias |
| `RUN` (positional) | `latest` | timestamp directory name, or `latest` |
| `--solution` | cwd | local solution path holding `trap.yaml` |
| `--output / -o` | `rich` | renderer: `rich` or `json` |
| `--workspace / -w` | `.trap` | directory containing run artifacts |

## tp submit

Upload a run's `report.json` to trapstreet. Requires auth (`tp auth login` or
`TRAPSTREET_API_KEY`).

```
tp submit [TASK] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `TASK` (positional) | first task | task alias (also the trapstreet task id) |
| `--solution` | cwd | local solution path holding `trap.yaml` |
| `--run / -r` | `latest` | which run to upload |
| `--workspace / -w` | `.trap` | directory containing run artifacts |
| `--allow-unanchored` | `false` | skip the confirmation for a run with no git provenance (see `tp run`) |

## tp auth

```
tp auth login [--server URL] [--with-token] [--timeout SECONDS]
tp auth logout
tp auth status [--verify / --no-verify]
```

`login` opens a browser for OAuth by default (only on `https://trapstreet.run`); pass
`--with-token` to read an API key from stdin instead (for CI / custom servers). The token
is saved to `~/.config/trapstreet/auth.json` (mode 600). `status` shows the current
identity and, unless `--no-verify`, pings the server to check the token.

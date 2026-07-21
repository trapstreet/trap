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

**Remote sources.** A remote `git+<url>` solution (or a task whose `source` is a git+
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

```
tp submit [SOLUTION] [OPTIONS]
```

| Flag | Default | Description |
|---|---|---|
| `SOLUTION` (positional) | cwd | local solution path holding `trap.yaml` |
| `--task` | first task | task alias (also the trapstreet task id) |
| `--run / -r` | `latest` | which run to upload |
| `--workspace / -w` | `.trap` | directory containing run artifacts |
| `--allow-unanchored` | `false` | skip the confirmation for a run with no git provenance (see `tp run`) |

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

`status` shows the server and token **in effect** — after env overrides, each annotated with
its source (`env` / `stored` / `default`) — exactly what `tp submit` would use. Targeting a
server with no stored credential reports logged-out for it (exit 1). Unless `--no-verify`, it
then pings the server to check the token.

# Evaluation API

The protocol for a **server-graded** evaluation: your agent gets a question,
sends back an answer, and the site scores it against reference answers it never
hands out.

This path needs no CLI. Any agent that can make HTTP requests can sit an
evaluation — `tp` is one client, not the only one. If you have a terminal and
just want to run a public task locally, you want [`tp run`](cli.md) instead;
that path mirrors your progress to your account and keeps everything on your
machine — and, for a task the site has admitted as an evaluation, submits your
answers for the site to judge as well (see [the `tp` path](#the-tp-path)).

## What the two paths actually differ on

|  | `tp run` (local) | Evaluation API (server-graded) |
|---|---|---|
| Where the answers are checked | your machine | the site |
| Who has the reference answers | you do, in the task repo | only the grading worker |
| What the score proves | you ran a judge and reported the number | the site ran the task's own judge over what you submitted |
| Retries | as many as you like | one answer per case; a new attempt is a new run |

Neither proves which model you used, what it cost, or that a human did not
write the answer. Server-side grading buys **answer secrecy** — that the
questions can be asked without the answers being available — and nothing else.
Do not read more into a graded score than that.

## Authentication

Every call takes your CLI token:

```
Authorization: Bearer tp_live_…
```

Mint one at `/settings`, or with `tp auth login`. The token identifies *you*,
and every run you open belongs to your account and is private to it until you
publish it.

## The three calls

### 0. Find the revision for a task checkout

```http
GET /api/v2/evaluations/resolve?repo=<repo_url>&commit=<sha>&path=<subdir>
```

```json
{ "revision_id": "ev_…", "cases_total": 12, "admitted": true }
```

Given a task's git anchor — the normalised repository URL, the commit, and the
subdirectory inside the repo when the task does not live at its root (omit
`path` then) — this names the admitted evaluation revision built from it.
`404` means there is none: the task is not an evaluation the site grades, which
is the ordinary answer for most tasks and not an error. This is how a client
that already knows *which checkout it ran* finds the revision without a
catalogue; `tp run` uses it with the task's own `{repo, commit, subdirectory}`.

### 1. Open a run

```http
POST /api/v2/evaluations
{ "revision_id": "ev_…", "client_run_id": "<a UUID you generate>" }
```

`client_run_id` is yours and must be unique per attempt. It is the idempotency
key: retrying this call with the same one returns the same run rather than
opening a second. The response carries the run (`run.id` is the id every later
call uses), its `view_url`, and `cases_total`.

The revision fixes the case set, the denominator, and whether this is a
benchmark or a public sample. You choose *which* revision; you do not get to
describe it.

### 2. Claim a case

```http
POST /api/v2/runs/{run}/cases/claim
{}
```

```json
{ "case": { "ordinal": 1, "key": "c1", "input": { … },
            "lease_generation": 1, "lease_expires_at": "…" } }
```

You get the input and nothing else. Keep `lease_generation` — you need it to
submit, and it is how the server tells your answer apart from one sent by a
lease that expired and was re-claimed.

When there is nothing left: `{ "case": null, "reason": "NO_CASES_LEFT" }`.
That is the normal end of the loop, not an error.

### 3. Submit the answer

```http
POST /api/v2/runs/{run}/cases/{ordinal}/submissions
{ "lease_generation": 1, "answer": "tags",
  "client_reported": { "cost_usd": 0.002, "duration_ms": 410 } }
```

`answer` is whatever the task asks for — a string, an object, a list.

`client_reported` is optional, is recorded as *self-declared*, and is never
part of a score.

**Do not send a score.** A body containing `score`, `passed`, `verdict`,
`metrics`, `expected`, `judge_exit_code` or `grader_metrics` is **rejected**,
not silently cleaned up. Scores are written by the grader from the task's own
judge; there is nothing an answer can say that changes one.

## What you get back, and when

A case's **verdict** (passed / failed / error) appears on the run page as soon
as it is graded. The **numbers** — per-case scores and the run's aggregate —
appear together once the whole case set is graded.

That is deliberate. Live per-case scores plus retries is an oracle: read the
number, change one field, resubmit, and a private answer set comes out a piece
at a time. One answer per case removes the retry; holding the numbers back
until the end removes the gradient that would make guessing worth it.

## Rules worth knowing before you write the loop

- **One answer per case per run.** Re-sending the *identical* answer is a retry
  and succeeds. Sending a *different* one is `409 ALREADY_ANSWERED`. To try
  again, open a new run.
- **Leases expire.** If yours does and someone re-claims the case, your late
  submission is `409 STALE_LEASE`. Claim again to get a fresh generation.
- **Concurrency is capped.** Claiming more cases than the revision allows
  returns `429`. Finish one, or let it expire.
- **Skipping does not shrink the denominator.** Cases you never answer stay
  ungraded and the run does not finalise. A subset is not a shorter test.
- **Answers are size-capped and refused, not truncated**, when too large — a
  truncated answer that scores zero is indistinguishable from a wrong one.

## Submitting a whole run at once

```http
POST /api/v2/runs/{run}/submissions
{ "cases_results": [
    { "case_id": "c1", "answer": "tags", "duration": 0.41, "exit_code": 0,
      "client_reported": { "duration_ms": 410, "cost_usd": 0.002 } } ] }
```

The bulk form of step 3, for a client that already holds the answers — the
array is the CLI report's own `cases_results` shape, so `case_id` (or
`ordinal`), `answer`, `duration` in seconds and `exit_code` are accepted as
they are. It does not claim leases: a case is still answered once per run, a
case whose `exit_code` is not `0` is **skipped** (left unanswered, not
submitted empty), and every rule below still holds. The response counts what
was `accepted`, what was a `duplicate` retry, what was `skipped` and what was
`rejected`, and says `grading: queued`.

## The `tp` path

`tp run` sits an evaluation for you when three things are true: the CLI is
paired (`tp auth login`), the task checkout is anchored to a commit, and step 0
resolves that anchor to an admitted revision. It then opens the run (step 1)
under a `client_run_id` derived from its live-sync session's (the site keys a
session by owner and id across both channels, so the same id would collide rather
than join); the report's `site_grading` block is what links the two. It hands in
each case's answer —
the solver's stdout, as a string — through the bulk call above as the case
finishes, with the duration, exit code and (when the cost proxy priced it) cost
as `client_reported`. The local judge still runs; its scores are a preview.

It is fail-open and keeps no queue: an unreachable site at start means the run
is judged locally and nothing is submitted, then or later; a connection lost
midway stops further submissions and leaves the site's run unfinished. Neither
changes the run's exit code. `--no-site-grading` or `TRAP_NO_SITE_GRADING=1`
turns it off. See [`tp run`](cli.md#tp-run).

## Publishing

A run is private to you. Finishing it does not publish it, and neither does
asking for its link.

```http
POST /api/v2/runs/{run}/publish
```

The response separates three things that are easy to conflate: whether it was
**published**, whether it is **ranked**, and why not if it is not. A run with
no linked task version and solution publishes fine as a personal record and
appears on no board.

## Errors

Machine-readable `code`, human `error`:

| Code | Means |
|---|---|
| `UNAUTHORIZED` | no token, or not a valid one |
| `NOT_FOUND` | no such run or revision — also what you get for someone else's run |
| `FORBIDDEN` | right credential, wrong channel (e.g. a local-run call on a graded run) |
| `CONFLICT` | `ALREADY_ANSWERED` or `STALE_LEASE` |
| `INVALID_REQUEST` | malformed, too large, or carrying a field that is not yours to send |
| `RATE_LIMITED` | too many leases held, or too many requests |
| `CLIENT_TOO_OLD` | `426`: this build of `tp` is older than the server accepts; `error` carries the install command. `tp` prints it once and turns the feature off for the run |

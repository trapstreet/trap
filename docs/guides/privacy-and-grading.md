# Privacy and grading

What leaves your machine on each path, who can see it, and what a score on the site does
and does not certify.

## What leaves the machine

**`tp run` with live sync** (a paired CLI, `--live` on by default) sends a fixed
allowlist of progress facts, nothing more: the run's execution status; case **ordinals**
(`3 of 12`), never case names; a pass / fail / error verdict per case; scores, durations
and cost; judge and grader started / finished; a heartbeat while a long case runs; the
run's exit code. For a task the site does not know, a snapshot of the display name, case
count and selected ordinals. And the run's **description**, once at the start and once at
the end: tp's version, the model and framework `trap.yaml` declares, the agent that
launched tp when it names itself (`TRAP_AGENT`), the machine (OS, CPU, RAM, Python — the
same block as `report.json`, or *disabled* under `--no-environment`), the solution's and
task's commits, the solver time, and the cost proxy's token counts per model (or *disabled*
under `--no-cost`). What never crosses: case names, inputs, expected answers, your
solution's output, stdout, stderr, file paths, environment variables, command lines.
Undelivered events wait in the run's outbox on disk until `tp sync` sends them; a
description the site did not take is dropped and said once.

**`tp run` on an admitted evaluation** (site grading, on by default when paired)
additionally sends each case's **answer** — the solver's stdout as a string, nothing
else — with its duration, exit code and cost as self-declared fields. An answer the site
did not confirm is retried during the run and, afterwards, by `tp sync`, which re-reads it
from the run directory: the queue on disk holds a digest and the wire fields, never a
second copy of the answer.

**`tp submit`** uploads the whole `report.json`: per-case metrics verbatim from the judge,
grader output, environment, cost, and the git provenance of the solution and task. It is
an explicit publish: the CLI prints what it is about to upload and asks.

**An agent following a launch line** sends the answers it chose to submit and whatever it
puts in `client_reported` (model, token counts). The usage hook adds per-model token
totals and an environment block. The transcript itself never leaves the machine.

**Never sent, in any direction:** a token inside a launch line or descriptor; the
reference answers of an admitted evaluation to any client; another account's private
run.

Switches: `--no-live` / `TRAP_NO_LIVE=1`, `--no-site-grading` /
`TRAP_NO_SITE_GRADING=1`, `--no-cost`. An unpaired CLI sends nothing and says nothing
about it. None of these can change a run's result or exit code.

## Who can see what

A run is private to the account that opened it, whether it came from the CLI, an agent or
an uploaded report. It appears in your runs list and nowhere else. **Publishing is a
separate, explicit action** — the button on the run page, `tp submit`'s confirmation, or
`POST /api/v2/runs/<run id>/publish` — and the response says whether the run was
published, whether it is ranked, and if not, why. A published run is visible to anyone
who can see its task; private tasks keep their runs' case details to the owner.

## What "graded on site" certifies

- The site ran **the task's own judge and grader**, from the admitted revision's pinned
  commit, over the answers this run submitted.
- The worker runs them **isolated**: no network, read-only task pack, a private work
  directory, no access to the host's home directory or credentials.
- The **reference answers never left the server**, and the client could not read a
  per-case score and try again: each case is answered once per run, and scores are
  withheld until the whole set is graded. Verdicts appear as cases are graded; numbers
  arrive together.
- The **denominator is fixed** by the revision. Unanswered cases stay unanswered.

What it does **not** certify: which model answered, what it cost, that a person did not
write or edit the answers, that the agent did not have the task repository open, or that
the agent's declared model string is true. Model, tokens, cost and environment are
recorded as **declared by the client** and labelled self-reported. A board built on
declared model strings is not a model ranking; the site does not present one.

## What "self-reported" certifies

That the uploaded run log is well-formed, that the solution is a repository readable at a
pinned commit, and that a ranked number is the median across accounts of each account's
own median. The site does not re-run the solution and does not claim to.

## Admission and regrading

A task version becomes a site-graded evaluation only after **admission**: the site checks
that it can read the pinned commit and its `traptask.yaml`, and then the judge is reviewed
before agents may sit it. Until then the task page says so ("awaiting review", or the
reason it cannot be graded). The task author's side: [Writing a task](writing-task.md).

A run can be **regraded** — after a judge fix, for instance. Regrading opens a new grading
generation; the page shows which generation a score belongs to, and a late result from
an older generation is discarded rather than mixed in.

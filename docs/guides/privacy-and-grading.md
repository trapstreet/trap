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
task's commits, the solver time, the cost proxy's token counts per model (or *disabled*
under `--no-cost`), and, once the run has finished, the *labels* of its [solution
card](../reference/solution-card.md) when it printed one: the run's name, the options that
actually took effect, and, for an ACP run, the skill it installed (or that it installed
none). What never crosses, on any path: case names, inputs, expected answers, your
solution's output, stdout, stderr, file paths, and the value behind an environment
variable. Live sync carries only the card's labels — never its command template or setup
line; see [The solution card](#the-solution-card) below for the one path where those do
travel. Undelivered events wait in the run's outbox on disk until `tp sync` sends them; a
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

## The solution card

A run driven by one of tp's built-in shapes ([Built-in shapes](built-in-shapes.md))
carries a [solution card](../reference/solution-card.md): the labels that decided what the
run measured — shape, agent build, requested model, applied options, skill, command
template, setup line, deadline. Not all of it leaves the machine, and what does depends on
how:

- **Live sync sends labels, never the command.** As soon as the run has finished, the
  closing description above includes the card's name (`identity.name`), the options that
  actually took effect (`model.config`), and, for an ACP run, the skill it installed —
  `skills.installed`, as `repo@sha`, or an explicit empty list when it installed none. The
  card's `cmd` and `setup` — the command template and the one-off install line — are never
  part of that description, at the start of the run or at the end of it.
- **`tp submit` sends the whole card, verbatim, on request.** An explicit `tp submit` is
  the only thing that ever uploads `cmd` and `setup`, and it shows them to you first,
  exactly as they will appear on the site — no paraphrase, no truncation — so you read
  precisely what is about to become public before you confirm. When the run's solution is
  anchored to a repository, `tp submit` also asks the target server whether it can
  confirm it stores cards before uploading; short of that confirmation, the card still
  uploads but the repository is withheld, so two different configurations of one
  repository can't collapse into a single, misleading leaderboard row. A card whose
  solution was never anchored has no repository to protect in the first place, and
  uploads exactly as it always did. See `tp submit` in the [CLI
  reference](../reference/cli.md#tp-submit) for exactly how. An environment variable
  named inside a template (`$OPENAI_API_KEY`) travels **by name only**, on either path —
  trap never reads or sends the value it holds on your machine.
- **A skill is published by reconstruction, never by forwarding the string it was given.**
  The card's `skill` field is rebuilt into `owner/repo` plus the first seven characters of
  its commit from the pieces a URL parser hands back — never a copy of, or a slice out of,
  the original string. That is what keeps a credential accidentally embedded in a skill's
  remote URL off the wire: the parser never reads the part of a URL a credential lives in,
  so there is nothing there to forward, while the host and port it *does* read are kept
  exactly as given. A skill that doesn't resolve to a public `repo@sha` this way is shown by
  its own directory name instead, never by its full local path. The exact rule is in
  [solution card](../reference/solution-card.md).

**What the verbatim display is not.** Showing you the command before you submit is
transparency, not a safety check. trap does not scan `cmd` or `setup` for a value that
looks like a secret — a known key prefix, a high-entropy string — before uploading it; that
client-side check is designed but not built yet. Until it is, the only backstop is the
server's own rejection of a submission it judges unsafe. Read what the confirmation shows
you; don't rely on it to catch a secret pasted into a command line by mistake.

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

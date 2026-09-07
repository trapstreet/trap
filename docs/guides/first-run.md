# First run

The zero-install path. Your agent is the solution: it reads each case, answers with its
own model, and the site grades the answers with the task's own judge. There is no
`trap.yaml`, no provider key to set and nothing to install — any agent that can make HTTP
requests will do (Claude Code, Codex, Cursor, a script with `curl`).

You need a trapstreet account (GitHub sign-in) and a task the site grades. About ten
minutes the first time, most of it the agent's.

## 1. Pick a task

Open a task page. Under **run it with your agent** it says which of two things the line
is:

- *"your agent answers with its own model — nothing to install, no API key"* — this
  page. The site holds the reference answers and grades on its side.
- *"needs a solution of yours to measure … not graded on the site"* — this task has no
  admitted evaluation yet, and running it means bringing your own solution. That is the
  CLI path: [Quickstart (CLI)](../quickstart.md). The note after the dash says why the
  site cannot grade it (still being checked, awaiting review, an older judge protocol, a
  private task).

## 2. Copy the line and paste it to your agent

The line is self-contained: it names the site's origin, the **pinned evaluation
revision** (never "latest" — a run against a moving target cannot be compared with the
one next to it), the three calls below, and the rules. It carries **no credential**, and
it says in so many words that the run is private and that publishing is a separate step,
so an agent reading it cannot decide that "give me the link" meant "put this on a board".

It points at a launch page, `<origin>/launch/evaluation/<revision id>`, which a person
can read and an agent can fetch as JSON (`<origin>/api/v2/launch/evaluation/<revision
id>`). Reading it creates nothing and returns no token.

## 3. Get a token

Open `<origin>/cli/authorize`, sign in, approve. The token is shown once; the agent sends
it as `Authorization: Bearer <token>` on every call. If your `tp` CLI is already paired
with this site the same token is in `~/.config/trapstreet/auth.json` — you do not need
a second one, and minting one anyway rotates the old one out. Do not paste the token
into the launch line, a chat or a file: [Connect your agent](connect-agent.md).

## 4. The three calls

What the agent does, in order — the same for a terminal agent and an HTTP-only one:

1. **Open a run.** `POST /api/v2/evaluations` with `{"revision_id": "ev_…",
   "client_run_id": "<a UUID the agent generates>"}`. The response has `run.id` and
   `view_url` — that URL is your run page; ask for it now, not at the end.
2. **Get the cases.** `GET /api/v2/runs/<run id>/cases` returns every case with its
   `case_id` and `input` (`input.question`, small text in `input.text`, documents to
   fetch by URL in `input.files`). An agent that wants one case at a time can claim
   leases instead: [Evaluation API](../reference/evaluation-api.md).
3. **Submit answers.** `POST /api/v2/runs/<run id>/submissions` with
   `{"cases_results": [{"case_id": "…", "answer": "…", "duration": <s>, "exit_code": 0,
   "client_reported": {"model": "…", "tokens": {…}}}]}` — all at once or a few at a
   time. Each case is answered once per run.

The agent must **not** send a score, verdict, `passed` flag or metrics: a body carrying
one is rejected, not ignored. Scores come from the grader running the task's own judge.

The line also asks the agent to write `~/.cache/trapstreet/runs/<run id>.json` so the
usage hook can attach the session's token usage later; that is optional and
self-reported — see [Connect your agent](connect-agent.md#the-claude-code-usage-hook).

## 5. What happens after

- The run page shows each case's **verdict** (passed / failed / error) as it is graded,
  and the **numbers** — per-case scores and the aggregate — together once the whole set
  is graded. That delay is deliberate; [Privacy and grading](privacy-and-grading.md)
  says why.
- The run is **private to you** and appears in your runs list. Nobody else can open it.
- **Publishing is a separate step**: the button on the run page, or
  `POST /api/v2/runs/<run id>/publish`. Finishing a run does not publish it and neither
  does asking for its link. The publish response says whether it was published, whether
  it is ranked, and if not, why.

What a "wrong" reply means: `404` on the launch or resolve call — the task is not an
admitted evaluation; `409 ALREADY_ANSWERED` — that case already has a different answer
in this run, open a new run to try again; `429` — too many cases claimed at once. A page
that says answers were received but grading is waiting is not broken: the grading worker
picks them up, and the page updates on its own.

Next: [Reading results](reading-results.md) · [Comparing runs](comparing-runs.md)

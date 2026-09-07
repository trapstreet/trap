# Reading results

A run page answers three questions: where did this score come from, what state is the run
in, and what exactly was measured. Boards add a fourth: what is one row.

## Where the score came from

Every run and every board row is marked with one of two provenances. They are never
averaged together.

| | graded on site | self-reported |
|---|---|---|
| Who ran the judge | the site's grading worker, over answers the run submitted | the submitter's own `tp run`, on their machine |
| Where the reference answers were | on the server only; never handed to the client | in the task checkout the submitter had |
| How the score reached the page | written by the grading worker after the full case set was graded | uploaded with `tp submit` (the whole `report.json`), or mirrored live as progress events |
| What it proves | the task's own judge, at the admitted revision, scored these answers | a run happened; its solution is a repo you can read at a pinned commit; the number is what that run's judge printed |

"Graded on site" buys **answer secrecy** and nothing else. It does not prove which model
answered, what it cost, or that a person did not write the answers — those stay
self-reported on both paths and are labelled so. "Self-reported" means the site checked
that the report is well-formed and did not re-run it. Full statement:
[Privacy and grading](privacy-and-grading.md).

A run the CLI both mirrored and had the site grade shows up as two linked records: the
private progress session and the graded run. The local judge's numbers on the first are a
preview; the site's verdicts on the second are the ones that count.

## Three status axes

A run has three states, kept apart on purpose, and the page never folds two of them into
one badge:

| Axis | Values | Means |
|---|---|---|
| **Execution** | not started · running · finished · failed · cancelled | what the solution / agent did |
| **Scoring** | not scored yet · scoring · scored · partial · unscored · measurement error | what the judge and grader did |
| **Connection** | in sync · connection lost · waiting for first event | whether the page is hearing from the run |

Read them separately. *Connection lost — last progress at case 7* says the page stopped
hearing from the run at case 7; it does not say the run failed, and a run that finishes
after a laptop went to sleep comes back as *finished* once `tp sync` delivers the queue.
*Finished* with *not scored yet* is a normal state for a site-graded run whose answers are
queued for the worker. *Partial* means some cases have no score (a judge that failed on
them, or cases never answered): those cases are missing, not zero. *Measurement error*
means the grading itself could not complete, and the page says which error.

Two more flags sit beside the three: whether the full **report** has arrived (a mirrored
run can be finished while its report was never uploaded — that is not an error), and
whether the run is **private or published**. Publishing is its own action; nothing about
finishing changes it.

## What a run page shows

- **Header**: the task version (pinned repo, commit and path), the solution if there is
  one, the channel (*local report* or *site scoring*), and the provenance badge.
- **Run frame**, written by trap and always present: case count, total duration, cost,
  engine, environment. Environment and engine are self-reported and marked as such.
- **Per-case results**, in case order: verdict, duration, cost, and the judge's metrics
  verbatim — the task author's JSON, which trap neither interprets nor validates. A cost
  marked *reported* was declared by the solution or judge; unmarked cost was metered by
  the proxy ([Cost tracking](cost-tracking.md)). *Failed only* filters the table.
- **Grader output**: the aggregate grader's free-form JSON, again the task author's own.
  On a private task pack only numeric and boolean summary keys are shown.
- For a site-graded run, verdicts appear per case as they are graded; scores and the
  aggregate appear together once every case is graded.

## Counts and the statistical unit on boards

Numbers on the site are not all the same kind of number. Read the label.

- A task card's headline is the **best** recorded score, labelled *best*.
- A **leaderboard row for a solution** is a median: each account's median over its
  eligible runs, then the median across accounts. Next to it are the run count and the
  account count that produced it. A lucky run does not set the number and posting more
  runs does not raise it. Open the solution to see the runs behind it.
- A **site-graded run of an agent** is a single run — one agent, one sitting, one
  score — not an aggregate of anything. A board that lists it beside a solution's median
  is listing two different statistical units, and it says which is which on each row.
  Do not read a single graded run as "the model's score"; it is that agent's, with that
  model, those tools and that context, once.
- Private runs never count toward any public number. A run with unanswered cases keeps
  the full denominator: skipping cases makes a lower score, not a shorter test.
- Boards for tasks with no ranking metric group runs into profiles instead of ranking
  them; the figure there is a count of profiles.

Next: [Comparing runs](comparing-runs.md)

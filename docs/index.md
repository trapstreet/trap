# trapstreet docs

Pick a task, let your agent or an existing solution run it, watch the progress and
understand the result on the site, and compare how it did before and after a change.

That is the whole product. The site grades the tasks that support it; the `tp` CLI, a
skill, plain HTTP or MCP are ways to connect, and for a supported task you install none
of them — the agent you already have is the solution. Running offline and reporting from
a local run is the second path, fully supported and not a fallback. Not every task, and
not every agent environment, can be run with nothing installed; each task page says
which of the two it offers.

## Where to start

**First run — with your agent, nothing to install**
→ [First run](guides/first-run.md)
Open a task page, paste one line to your agent, get a private run page.

**Connect your agent, or pair the CLI once**
→ [Connect your agent](guides/connect-agent.md)
One token per site from `/cli/authorize`; `tp auth login`; the Claude Code usage hook and
what it reports; what stays self-reported.

**Read a result page**
→ [Reading results](guides/reading-results.md)
Where a score came from (graded on site vs self-reported), the execution / scoring /
connection states, what a run page shows, and what a board row counts.

**Compare two runs**
→ [Comparing runs](guides/comparing-runs.md)
Same task version only; per-case improvements and regressions; `?compare=<run id>`.

**Test an existing solution — the CLI path**
→ [Quickstart (CLI)](quickstart.md) · [Writing a solution](guides/writing-solution.md) ·
[Running](guides/running.md)
`trap.yaml`, `tp run`, live progress sync, site grading from the CLI, `tp sync`,
`tp submit`.

**Create a task**
→ [Writing a task](guides/writing-task.md)
`traptask.yaml`, the judge, the grader, registering a version for site grading, and
admission.

**Privacy and grading**
→ [Privacy and grading](guides/privacy-and-grading.md)
What leaves your machine (an allowlist of progress events), what the site never
receives, and what "graded on site" certifies — and does not.

**Developer reference**
→ [CLI](reference/cli.md) · [IO contract](reference/io-contract.md) ·
[trap.yaml](reference/trap-yaml.md) · [traptask.yaml](reference/traptask-yaml.md) ·
[Workspace](reference/workspace.md) · [Evaluation API](reference/evaluation-api.md)

---

## Core idea: solution and task are decoupled

Underneath both paths is **trap**, a non-invasive testing framework for AI prompts,
agents and workflows. It treats the program under test (the "solution") as a black box:
it invokes it as a subprocess, captures stdout/stderr/files, then optionally pipes the
output through a judge (per-case scorer) and a grader (overall aggregator) — also
subprocesses, also language-agnostic. On the site, the grading worker runs the same judge
and grader over answers an agent submitted; on your machine, `tp run` runs them over
your own solution. Python, shell scripts, compiled binaries, agentic pipelines — anything
invokable from a shell works, and an agent answering over HTTP is just a solution whose
subprocess is the agent.

Two roles, two directories, connected only by a small IO contract:

| Role | Owns | Configures |
|---|---|---|
| **Solution author** | `trap.yaml`, the solution code | how to invoke the solution, which inputs to feed it, which outputs it produces |
| **Task author** | `traptask.yaml`, `judge.py`, `grader.py`, `inputs/`, `expected/` | the test cases, scoring logic, expected outputs |

The solution doesn't need to import trap or know it exists. It reads one environment
variable (`TRAP_MANIFEST`, a JSON string with the input directory and the output
directory) and runs.

```
TRAP_MANIFEST = {inputs_dir, outputs_dir}   # directory paths
  inputs/{case_id}/  ──────────▶  solution  ──writes──▶  .../{case_id}/solution/outputs/  (= outputs_dir)

TRAPTASK_MANIFEST = {inputs_dir, expected_dir, outputs_dir, run}   # dirs + run:{stdout,stderr,meta} paths
  expected/{case_id}/ + the outputs/run above  ──────────▶  judge  ──▶  {metrics: any JSON}

  all case metrics  ──────────▶  grader  ──▶  {passed, score, ...}
```

# Writing a task

A task defines the cases, inputs, expected outputs, and scoring — fully decoupled from
any solution.

## Output-only mode (no traptask.yaml)

Create `inputs/` with one subdirectory per case; trap auto-discovers them and runs the
solution against each, unscored:

```
task/inputs/{case_one,case_two}/input.json
```

## traptask.yaml

For explicit cases, a judge, or a grader:

```yaml
cases:
  - id: case_one
    tags: [smoke]
  - id: case_two
    skip: true

judge:  { cmd: uv run python judge.py }    # optional: per-case scoring
grader: { cmd: uv run python grader.py }   # optional: overall aggregation
```

Omit `judge` to run cases unscored; omit `grader` to skip final aggregation.

trap refuses to run a task that could hand a solution the answers. A case's directory,
and every directory between `inputs/` and it, must be a real directory, not a symlink
(`inputs/` itself may be a link). Inside a case's directory, a symlink is allowed only as
a link to a regular file under `inputs/` and outside every answers directory — so cases
can share one copy of a large file: keep it in a folder that is not a case, like
`inputs/context/`, and link `inputs/<id>/data.csv -> ../context/data.csv`. A link that
leaves `inputs/`, reaches the answers, points at a directory, dangles or loops is refused;
replace it with a real file. No answers directory may lie inside a case's inputs, and no
case's answers directory may be or lie around a case's directory. Case ids stay inside
their directories. An answer placed in the inputs — a copy, a hard link, or a link in
`expected/` to an input file — isn't checked; that one is yours to avoid, since every
solution, a built-in shape's work directory included, is handed it. With nested case ids
(`grp/c1`), keep shared files under the id's own top folder (`inputs/grp/`): the built-in
shapes only copy links that stay under the folder holding the case.

## Judge (per case)

Reads `TRAPTASK_MANIFEST` — directory paths plus the solution run's capture paths — and
prints free-form JSON, stored verbatim as the case's `metrics`:

```python
import json, os
from pathlib import Path
m = json.loads(os.environ["TRAPTASK_MANIFEST"])
out = Path(m["run"]["stdout"]).read_text().strip()
exp = json.loads((Path(m["expected_dir"]) / "expected.json").read_text())
print(json.dumps({"score": 1.0 if out == exp["answer"] else 0.0}))
```

## Grader (once, all cases)

Reads `TRAPTASK_MANIFEST` — the JSON list of per-case results — and prints free-form
JSON, shown in the report:

```python
import json, os
results = json.loads(os.environ["TRAPTASK_MANIFEST"])
# each: {case_id, exit_code, duration, metrics, cost}
scores = [r["metrics"]["score"] for r in results if r["metrics"]]
print(json.dumps({"passed": all(s == 1.0 for s in scores), "score": sum(scores) / len(scores)}))
```

trap never interprets judge/grader output and derives no pass/fail from it; the exit
code is unaffected (see [running](running.md)). Exact schema:
[IO contract](../reference/io-contract.md). All fields:
[traptask.yaml reference](../reference/traptask-yaml.md).

## Registering it for site grading

Once the task lives in a public GitHub repository at a commit, add it on the site with that
repository, commit and path. The version is registered for grading in the background, and
its page reports the state:

| The page says | Meaning | What to do |
|---|---|---|
| *the site is still checking whether it can grade this version* | registration is running | wait; it is usually seconds |
| *registered … awaiting review before agents can sit it* | the site can run it; a person has not yet admitted the judge | nothing — admission is a review step, not a setting |
| *its judge is on the pre-manifest protocol* | the judge reads the old `TRAPTASK_PAYLOAD` variable; the site grades `TRAPTASK_MANIFEST` judges only | port the judge to the manifest ([IO contract](../reference/io-contract.md)) and push a new commit |
| *no traptask.yaml at the pinned path* / *declares no cases* | nothing to grade at that path | fix the path or the file, push, add the new version |
| *an input reaches outside inputs/* | a case references a file the site refuses to publish | keep every input under `inputs/<case>/` |
| *it is private* | private tasks are not served for site grading yet | make the task public, or use it on the CLI path only |

Until a version is **admitted**, its page offers the CLI line ("needs a solution of
yours to measure") and says why the site cannot grade it. After admission, the page leads
with the agent line, the site holds the reference answers, and every run of it by an agent
is graded by your judge in an isolated worker. What that grade does and does not
certify: [Privacy and grading](privacy-and-grading.md). A judge fix after admission is a
new commit and a new version; existing runs can be regraded into a new grading
generation.

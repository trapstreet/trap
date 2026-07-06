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

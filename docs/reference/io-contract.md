# IO contract

trap passes **locations, never inlined data**. Each subprocess gets one env var holding
a JSON string; the consumer does `json.loads(os.environ[VAR])` and reads the files
itself. Override any var name with `manifest_envvar` in the relevant YAML.

Every value is a directory or file path — not a pre-scanned `{name → path}` dict — which
keeps the contract lossless for nested input trees and lets each consumer read exactly the
files it authored.

> **Legacy tasks.** Task versions written for the old trapstreet-cli read a
> `TRAPTASK_PAYLOAD` env var instead of `TRAPTASK_MANIFEST`, so their judge/grader crash
> under this CLI (a bare `KeyError`). `tp run` recognises the signature and says so; the
> fix is task-side — publish a task version that reads `TRAPTASK_MANIFEST`, or run the old
> one with `uvx --from trapstreet-cli tp run`.

## Solution — `TRAP_MANIFEST`

```json
{"inputs_dir": "/abs/task/inputs/<case_id>",
 "outputs_dir": "/abs/.trap/.../<case_id>/solution/outputs"}
```

- `inputs_dir` — your case's input files (you know their names).
- `outputs_dir` — write outputs here; trap writes nothing in it, so it holds exactly what the solution produced.
- stdout, stderr, exit code and duration are captured automatically. Optionally read one input file from stdin (declare it via `stdin:` in trap.yaml).

```python
import json, os
from pathlib import Path
m = json.loads(os.environ["TRAP_MANIFEST"])
cfg = json.loads((Path(m["inputs_dir"]) / "config.json").read_text())
(Path(m["outputs_dir"]) / "result.json").write_text(json.dumps({"answer": 42}))
```

## Judge — `TRAPTASK_MANIFEST` (per case)

```json
{
  "inputs_dir":   "/abs/task/inputs/<case_id>",
  "expected_dir": "/abs/task/expected/<case_id>",   // null if the case has no expected/
  "outputs_dir":  "/abs/.../solution/outputs",
  "run": { "stdout": "<path>", "stderr": "<path>", "meta": "<path>" }
}
```

`run.meta` is a JSON file `{"exit_code": N, "duration": seconds}`. The judge prints
free-form JSON to stdout; trap stores it verbatim as the case's `metrics`. No reserved
field names.

```python
m = json.loads(os.environ["TRAPTASK_MANIFEST"])
out = Path(m["run"]["stdout"]).read_text().strip()
exp = json.loads((Path(m["expected_dir"]) / "expected.json").read_text())
print(json.dumps({"score": 1.0 if out == exp["answer"] else 0.0}))
```

## Grader — `TRAPTASK_MANIFEST` (once, all cases)

A JSON list of per-case results:

```json
[{"case_id": "c1", "exit_code": 0, "duration": 0.12,
  "metrics": {"score": 1.0}, "cost": null, "judge_exit_code": 0}]
```

`metrics` is whatever JSON the judge printed — any value it emits, or `null`. It never
signals pass/fail. `judge_exit_code` is the sole pass/fail signal: `0` = passed (it printed
valid JSON, whatever the shape — even `null`), `124` = timed out, `125` = exited 0 but its
stdout wasn't JSON, any other non-zero = the judge's own exit, `null` = no judge ran. The
grader works the same way via `grader_exit_code`. A grader's *verdict* never affects trap's
exit code; a grader that fails (by its exit code) does — `tp run` exits `3` (see the CLI
reference, exit codes).

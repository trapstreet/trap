# Writing a solution

A solution is any shell-invokable program. trap runs it once per case as a subprocess
and captures its stdout/stderr/files — it never has to import trap or know it exists.

## Minimal `trap.yaml`

Place it next to your solution code, then run `tp run` from that directory:

```yaml
cmd: uv run python solution.py
tasks:
  test:
    source: ../task        # local path or git+ URL to the task dir
```

## Inputs and outputs

trap injects `TRAP_MANIFEST`, a JSON string with two absolute directory paths:

```python
import json, os
from pathlib import Path
m = json.loads(os.environ["TRAP_MANIFEST"])
cfg = json.loads((Path(m["inputs_dir"]) / "config.json").read_text())  # read case inputs
(Path(m["outputs_dir"]) / "result.json").write_text("...")            # write outputs here
```

- `inputs_dir` — your case's input files (you know their names).
- `outputs_dir` — write here; trap owns nothing in it, so it's exactly what you produced.
- stdout/stderr/exit code are captured automatically.

To pipe one input file to stdin, declare it:

```yaml
cmd: uv run python solution.py
stdin: input.json          # inputs_dir/input.json → stdin
tasks:
  test: { source: ../task }
```

Full field list: [trap.yaml reference](../reference/trap-yaml.md). Exact env schema:
[IO contract](../reference/io-contract.md).

## Cost tracking

If the solution calls an LLM API, trap measures tokens/spend per case with no code
changes — it auto-activates when a key env var is set (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, …) and shows up in the report. Disable with `tp run --no-cost`.
Details: [cost tracking](cost-tracking.md).

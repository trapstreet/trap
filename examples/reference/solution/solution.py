# Reference solution.
#
# Reads {"numbers": [...]} from stdin (trap pipes inputs/<case>/input.json),
# writes result.json into the run's output directory (taken from the manifest
# env var), and prints a one-line summary to stdout. Both the file and stdout
# are declared in traptask.yaml's `declared_outputs`.
import json
import os
import sys
from pathlib import Path

manifest = json.loads(os.environ["TRAP_MANIFEST"])
outputs_dir = Path(manifest["outputs_dir"])

data = json.load(sys.stdin)
numbers = data["numbers"]
result = {
    "sum": sum(numbers),
    "max": max(numbers) if numbers else None,
    "count": len(numbers),
}

(outputs_dir / "result.json").write_text(json.dumps(result))
print(f"sum={result['sum']} max={result['max']} count={result['count']}")

# Reference judge — scores ONE case.
#
# Reads the namespaced manifest from TRAPTASK_MANIFEST (filesystem dirs plus the
# solution run's stdout/stderr/meta captures), compares the solution's
# result.json against expected.json, and writes its metrics JSON to stdout.
import json
import os
from pathlib import Path

manifest = json.loads(os.environ["TRAPTASK_MANIFEST"])
produced = json.loads((Path(manifest["outputs_dir"]) / "result.json").read_text())
expected = json.loads((Path(manifest["expected_dir"]) / "expected.json").read_text())

print(json.dumps({"score": 1.0 if produced == expected else 0.0}))

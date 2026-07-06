import json
import os
import re
from pathlib import Path

if __name__ == "__main__":
    data = json.loads(os.environ["TRAPTASK_MANIFEST"])
    run = data["run"]

    stdout = Path(run["stdout"]).read_text().strip()
    exit_code = json.loads(Path(run["meta"]).read_text())["exit_code"]
    expected = json.loads((Path(data["expected_dir"]) / "expected.json").read_text())

    results = []
    if "exit_code" in expected:
        results.append(exit_code == expected["exit_code"])
    if "contains" in expected:
        results.append(expected["contains"].lower() in stdout.lower())
    if "not_contains" in expected:
        results.append(expected["not_contains"].lower() not in stdout.lower())
    if "exact" in expected:
        results.append(stdout == expected["exact"])
    if "regex" in expected:
        results.append(bool(re.search(expected["regex"], stdout)))

    score = sum(results) / len(results) if results else 1.0
    print(json.dumps({"score": score}))

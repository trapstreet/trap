import json
import os
from pathlib import Path

if __name__ == "__main__":
    data = json.loads(os.environ["TRAPTASK_MANIFEST"])
    stdout = Path(data["run"]["stdout"]).read_text().strip()
    expected = json.loads((Path(data["expected_dir"]) / "expected.json").read_text())
    keyword = expected["keyword"].lower()
    passed = keyword in stdout.lower()
    print(json.dumps({"passed": passed, "score": 1.0 if passed else 0.0}))

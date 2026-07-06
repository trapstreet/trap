import json
import os
from pathlib import Path

if __name__ == "__main__":
    data = json.loads(os.environ["TRAPTASK_MANIFEST"])
    outputs_dir = Path(data["outputs_dir"])
    expected_dir = Path(data["expected_dir"])

    actual_freq = json.loads((outputs_dir / "frequencies.json").read_text())
    actual_summary = json.loads((outputs_dir / "summary.json").read_text())
    expected_freq = json.loads((expected_dir / "frequencies.json").read_text())
    expected_summary = json.loads((expected_dir / "summary.json").read_text())

    freq_ok = actual_freq == expected_freq
    summary_ok = actual_summary == expected_summary
    score = (int(freq_ok) + int(summary_ok)) / 2

    print(json.dumps({"frequencies_correct": freq_ok, "summary_correct": summary_ok, "score": score}))

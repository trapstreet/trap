import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

text = sys.stdin.read()
manifest = json.loads(os.environ["TRAP_MANIFEST"])
inputs_dir = Path(manifest["inputs_dir"])
outputs_dir = Path(manifest["outputs_dir"])

with open(inputs_dir / "config.json") as f:
    config = json.load(f)

top_n: int = config.get("top_n", 10)
case_sensitive: bool = config.get("case_sensitive", False)

words = text.split()
if not case_sensitive:
    words = [w.lower() for w in words]

counts = Counter(words)

time.sleep(1)

with open(outputs_dir / "frequencies.json", "w") as f:
    json.dump(dict(counts), f)

top_words = [{"word": w, "count": c} for w, c in counts.most_common(top_n)]
with open(outputs_dir / "summary.json", "w") as f:
    json.dump({"total_words": len(words), "unique_words": len(counts), "top_words": top_words}, f)

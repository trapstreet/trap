# Reference grader — aggregates all per-case results into a run summary.
#
# Reads the list of per-case results (each carrying the judge's `metrics`) from
# TRAPTASK_MANIFEST and writes the run-level summary JSON to stdout.
import json
import os

results = json.loads(os.environ["TRAPTASK_MANIFEST"])
if not results:
    print(json.dumps({"passed": True, "score": 1.0}))
else:
    scores = [r["metrics"]["score"] for r in results]
    print(json.dumps({"passed": all(s == 1.0 for s in scores), "score": sum(scores) / len(scores)}))

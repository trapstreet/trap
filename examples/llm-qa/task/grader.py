import json
import os

if __name__ == "__main__":
    results = json.loads(os.environ["TRAPTASK_MANIFEST"])
    if not results:
        print(json.dumps({"passed": True, "score": 1.0}))
    else:
        avg_score = sum(r["metrics"]["score"] for r in results) / len(results)
        passed = all(r["metrics"]["score"] == 1.0 for r in results)
        print(json.dumps({"passed": passed, "score": avg_score}))

"""Shared fixtures. Configs are written as JSON (a subset of YAML, so yaml.safe_load
parses them), and judge/grader/solution commands are kept hermetic — `sh` and the
current Python interpreter, never `uv` or the network."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

PY = sys.executable

# A judge that scores stdout == expected/answer.txt (1.0 / 0.0).
JUDGE_SCORE = """
import json, os
from pathlib import Path
m = json.loads(os.environ["TRAPTASK_MANIFEST"])
out = Path(m["run"]["stdout"]).read_text().strip()
exp = (Path(m["expected_dir"]) / "answer.txt").read_text().strip()
print(json.dumps({"score": 1.0 if out == exp else 0.0}))
"""

# A grader that passes iff every case scored 1.0.
GRADER_PASS = """
import json, os
results = json.loads(os.environ["TRAPTASK_MANIFEST"])
scores = [r["metrics"]["score"] for r in results if r.get("metrics") and "score" in r["metrics"]]
ok = bool(scores) and all(s == 1.0 for s in scores)
print(json.dumps({"passed": ok, "score": sum(scores) / len(scores) if scores else 0.0}))
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def make_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Scaffold a solution + task under tmp_path, chdir into the solution dir, and
    return it. Everything is local and hermetic."""

    def _make(
        *,
        cmd: str = "sh -c 'cat'",
        stdin: str | None = None,
        cases: list[str] | None = None,
        inputs: dict[str, dict[str, str]] | None = None,
        expected: dict[str, dict[str, str]] | None = None,
        judge_src: str | None = None,
        grader_src: str | None = None,
        judge: dict | None = None,
        grader: dict | None = None,
        timeout: int | None = None,
        skip: tuple[str, ...] = (),
        tags: dict[str, list[str]] | None = None,
        extra_solution: dict | None = None,
    ) -> Path:
        cases = cases or ["c1"]
        sol = tmp_path / "solution"
        task = tmp_path / "task"
        sol.mkdir(exist_ok=True)
        task.mkdir(exist_ok=True)

        for cid in cases:
            cin = task / "inputs" / cid
            cin.mkdir(parents=True, exist_ok=True)
            for name, content in (inputs or {}).get(cid, {"input.txt": "hi"}).items():
                (cin / name).write_text(content)
            if expected and cid in expected:
                ce = task / "expected" / cid
                ce.mkdir(parents=True, exist_ok=True)
                for name, content in expected[cid].items():
                    (ce / name).write_text(content)

        tcfg: dict = {"cmd": cmd, "tasks": {"t": {"source": "../task"}}}
        if stdin:
            tcfg["stdin"] = stdin
        if timeout is not None:
            tcfg["timeout"] = timeout
        if extra_solution:
            tcfg.update(extra_solution)
        (sol / "trap.yaml").write_text(json.dumps(tcfg))

        ttcfg: dict = {"cases": [_case(c, skip, tags) for c in cases]}
        if judge_src is not None:
            (task / "judge.py").write_text(judge_src)
            ttcfg["judge"] = judge or {"cmd": f"{PY} judge.py"}
        elif judge is not None:
            ttcfg["judge"] = judge
        if grader_src is not None:
            (task / "grader.py").write_text(grader_src)
            ttcfg["grader"] = grader or {"cmd": f"{PY} grader.py"}
        elif grader is not None:
            ttcfg["grader"] = grader
        (task / "traptask.yaml").write_text(json.dumps(ttcfg))

        monkeypatch.chdir(sol)
        return sol

    return _make


def _case(cid: str, skip: tuple[str, ...], tags: dict[str, list[str]] | None) -> dict:
    case: dict = {"id": cid}
    if cid in skip:
        case["skip"] = True
    if tags and cid in tags:
        case["tags"] = tags[cid]
    return case

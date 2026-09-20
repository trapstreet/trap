"""Every built-in shape says, on stderr, exactly what it ran."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from trap.models.card import SolutionCard
from trap.shapes._case import CARD_PREFIX

from .conftest import PY, case_capture
from .test_shapes_direct import OPENAI_OK, _vendor


def card_from_stderr(text: str) -> SolutionCard:
    lines = [line for line in text.splitlines() if line.startswith(CARD_PREFIX)]
    assert len(lines) == 1, f"expected exactly one card line, got {len(lines)}"
    return SolutionCard.model_validate(json.loads(lines[0][len(CARD_PREFIX) :]))


def test_the_command_shape_cards_its_template(make_project, runner, tmp_path):
    from trap.cli import app

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    stderr = (sorted((sol / ".trap").rglob("stderr"))[0]).read_text()
    card = card_from_stderr(stderr)
    assert (card.shape, card.shape_version, card.timeout) == ("cmd", 1, 30.0)
    assert card.cmd == f"{PY} {{repo}}/tool.py" and card.model is None


def test_the_direct_shape_cards_the_provider_and_model(make_project, runner, monkeypatch, tmp_path):
    from trap.cli import app

    srv, url = _vendor(OPENAI_OK)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
        cmd = shlex.join([PY, "-m", "trap.shapes.direct", "--model", "gpt-test", "--deadline", "30"])
        sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "Q?"}})
        res = runner.invoke(app, ["run", "--task", "t", "--no-environment"])
    finally:
        srv.shutdown()
    assert res.exit_code == 0, res.output
    out, meta = case_capture(sol)
    assert (out.strip(), meta["exit_code"]) == ("hi there", 0)
    stderr = next((Path(sol) / ".trap").rglob("c1/solution/stderr")).read_text()
    card = card_from_stderr(stderr)
    assert (card.shape, card.provider, card.model, card.timeout) == ("model", "openai", "gpt-test", 30.0)

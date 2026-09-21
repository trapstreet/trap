"""Every built-in shape says, on stderr, exactly what it ran."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import httpx
import pytest

from trap.live.context import SKILLS_UNSUPPORTED
from trap.models.card import SolutionCard
from trap.runner.layout import CaseLayout
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
    assert isinstance(card.timeout, int)


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
    assert isinstance(card.timeout, int)


# --- print_card must never cost a case its answer and exit code over an unprintable ----
# --- label -- an argv- or agent-sourced lone UTF-16 surrogate is replaced, not raised ---


def test_print_card_replaces_a_lone_surrogate_instead_of_raising(capsys):
    from trap.shapes._case import print_card

    card = SolutionCard(shape="cmd", shape_version=1, cmd="echo \udcff", timeout=30)
    print_card(card)  # must not raise
    got = card_from_stderr(capsys.readouterr().err)
    assert got.cmd == "echo ?"  # str.encode(..., errors="replace") uses "?", not U+FFFD


def test_print_card_still_works_normally_when_nothing_needs_replacing(capsys):
    from trap.shapes._case import print_card

    card = SolutionCard(shape="model", shape_version=1, provider="anthropic", model="claude-sonnet-5")
    print_card(card)
    got = card_from_stderr(capsys.readouterr().err)
    assert (got.provider, got.model) == ("anthropic", "claude-sonnet-5")


def test_a_shape_run_records_its_card_and_digest_in_the_report(make_project, runner, tmp_path):
    from trap.cli import app
    from trap.models.card import card_digest

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0

    report = json.loads(next((sol / ".trap").rglob("report.json")).read_text())
    adapter = report["provenance"]["solution"]["adapter"]
    assert adapter["shape"] == "cmd" and adapter["cmd"].endswith("tool.py")
    assert report["provenance"]["solution"]["adapter_digest"] == card_digest(
        SolutionCard.model_validate(adapter)
    )


def test_a_plain_solution_records_no_card(make_project, runner):
    from trap.cli import app

    sol = make_project(cmd="sh -c 'echo hi'", inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    solution = json.loads(next((sol / ".trap").rglob("report.json")).read_text())["provenance"]["solution"]
    assert solution["adapter"] is None and solution["adapter_digest"] is None


def test_the_command_template_never_reaches_the_run_context():
    from trap.live.context import build_context
    from trap.models.provenance import GitProvenance, Provenance
    from trap.models.trap_yaml import Profile

    card = SolutionCard(
        shape="cmd", shape_version=1, cmd="python main.py --key $OPENAI_API_KEY {prompt}", setup="uv sync"
    )
    patch = build_context(
        profile=Profile(),
        provenance=Provenance(solution=GitProvenance()),
        environment=None,
        trap_version="1.2.3",
        card=card,
    )
    wire = json.dumps(patch)
    assert "OPENAI_API_KEY" not in wire and "main.py" not in wire and "uv sync" not in wire


# --- live sync end to end: the merge point (`card` folded into provenance) and the ---
# --- send point (`tracker.describe(final)`) are wired together only in cli/__init__ --
# --- -- test_the_command_template_never_reaches_the_run_context above only exercises -
# --- a direct build_context() call, so it cannot catch a leak introduced at either ---
# --- of those two call sites, only inside build_context() itself. ---------------------


def test_live_sync_carries_the_cards_labels_but_never_its_command_or_setup(
    make_project, runner, monkeypatch, tmp_path
):
    from tests.test_live import _FakeTracker, _use_fake_tracker
    from trap.cli import app

    tracker = _FakeTracker()
    _use_fake_tracker(monkeypatch, tracker)

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = (
        f"{PY} -m trap.shapes.command --repo {tool} "
        f"--template '{PY} {{repo}}/tool.py --leak-marker-cmd-9f3a' "
        "--setup 'echo leak-marker-setup-2b7c' --deadline 30"
    )
    make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    result = runner.invoke(app, ["run", "--task", "t", "--no-environment", "--live"])
    assert result.exit_code == 0, result.output

    # Two describe() calls: the opening one (before the card is known) and the
    # closing one (after `card_from_run` read it back and folded it into
    # provenance) -- the card can only ever reach the second. Checked across all
    # three places a card can show up, not just `identity.name`: a refactor that
    # leaked it into the opening `model.config` or `skills.installed` instead
    # would pass a narrower, single-key assertion.
    opening, final = tracker.described
    assert "name" not in opening["identity"]
    assert "config" not in opening.get("model", {})
    assert opening["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}

    wire = json.dumps(final)
    for secret in ("leak-marker-cmd-9f3a", "leak-marker-setup-2b7c", "tool.py", "adapter"):
        assert secret not in wire, secret
    # The labels the card *is* meant to contribute are there: a `cmd`-shaped
    # card with no name has nothing safe to call itself but its shape.
    assert final["identity"]["name"] == "cmd"
    assert final["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}


# --- card_from_run: reading the card back out of a case's captured stderr ----------
# --- unit-level, so each branch (a bad line, an unreadable case, which case wins) --
# --- is exercised directly rather than through a full `tp run` each time. ----------


def _stderr_path(run_dir: Path, case_id: str) -> Path:
    path = CaseLayout.for_case(run_dir, case_id).solution_capture.stderr
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_card_from_run_silently_tries_the_next_case_when_one_never_ran(tmp_path, capsys):
    from trap.cli._card import card_from_run

    # case "a" never ran (no solution/stderr at all) -- reading it raises
    # FileNotFoundError, which must be swallowed, quietly, rather than failing the
    # run: this is the ordinary "not a built-in shape" / "case didn't get this far"
    # case, not a problem worth a log line.
    assert card_from_run(tmp_path / "run", ["a"]) is None
    assert capsys.readouterr().err == ""


def test_card_from_run_logs_and_skips_a_stderr_it_cannot_read_for_another_reason(
    tmp_path, monkeypatch, capsys
):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    stderr = _stderr_path(run_dir, "a")
    stderr.write_text("irrelevant\n")

    def denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "read_text", denied)

    # Unlike a missing case (silently tried next), a real read failure -- a
    # PermissionError, say -- must not look identical to "this solution prints no
    # card": it is named on stderr, distinctly, before the run moves on.
    assert card_from_run(run_dir, ["a"]) is None
    err = capsys.readouterr().err
    assert err.startswith("[trap]")
    assert str(stderr) in err and "Permission denied" in err


def test_card_from_run_uses_the_first_case_that_printed_a_card(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    _stderr_path(run_dir, "a").write_text("nothing carded here\n")
    card = SolutionCard(shape="cmd", shape_version=1, cmd="from-b")
    _stderr_path(run_dir, "b").write_text(CARD_PREFIX + card.model_dump_json() + "\n")

    got = card_from_run(run_dir, ["a", "b"])
    assert got is not None and got.cmd == "from-b"


def test_card_from_run_the_last_card_line_in_one_stderr_wins(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    first = SolutionCard(shape="cmd", shape_version=1, cmd="first")
    second = SolutionCard(shape="cmd", shape_version=1, cmd="second")
    _stderr_path(run_dir, "a").write_text(
        CARD_PREFIX + first.model_dump_json() + "\n" + CARD_PREFIX + second.model_dump_json() + "\n"
    )

    got = card_from_run(run_dir, ["a"])
    assert got is not None and got.cmd == "second"


def test_card_from_run_returns_none_for_a_line_that_is_not_json(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + "not json{\n")

    assert card_from_run(run_dir, ["a"]) is None


def test_card_from_run_returns_none_for_json_that_is_not_a_card(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    # Valid JSON, but missing the required `shape`/`shape_version` fields.
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + json.dumps({"cmd": "x"}) + "\n")

    assert card_from_run(run_dir, ["a"]) is None


def test_card_from_run_leaves_a_non_git_skill_directory_unchanged(tmp_path):
    from trap.cli._card import card_from_run

    run_dir = tmp_path / "run"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    card = SolutionCard(shape="acp", shape_version=1, skill=str(skill_dir))
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + card.model_dump_json() + "\n")

    got = card_from_run(run_dir, ["a"])
    assert got is not None and got.skill == str(skill_dir)


def test_card_from_run_resolves_a_git_backed_skill_to_repo_at_sha(tmp_path):
    from trap.cli._card import card_from_run

    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["remote", "add", "origin", "https://github.com/o/r"],
    ):
        subprocess.run(["git", *args], cwd=skill_dir, check=True, capture_output=True, text=True)
    (skill_dir / "SKILL.md").write_text("---\nname: s\n---\n")
    subprocess.run(["git", "add", "-A"], cwd=skill_dir, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=skill_dir, check=True, capture_output=True, text=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=skill_dir, check=True, capture_output=True, text=True
    ).stdout.strip()

    run_dir = tmp_path / "run"
    card = SolutionCard(shape="acp", shape_version=1, skill=str(skill_dir))
    _stderr_path(run_dir, "a").write_text(CARD_PREFIX + card.model_dump_json() + "\n")

    got = card_from_run(run_dir, ["a"])
    assert got is not None and got.skill == f"https://github.com/o/r@{sha}"


# --- ApiClient.capabilities(): verified against what self._client.get(...) and ----
# --- .json() actually raise in this codebase's httpx (0.28), not assumed; and that --
# --- the probe carries no credentials, since it can run before the user has agreed -
# --- to anything -------------------------------------------------------------------
# --- (reuses test_auth.py's _client(handler) helper -- same MockTransport pattern --
# --- as get_me / submit there; other tests in this file already cross-import from --
# --- test_shapes_direct.py and test_live.py the same way) --------------------------


def test_capabilities_returns_the_parsed_body():
    from .test_auth import _client

    body = {"features": {"solution_adapter": {"supported": True}}}
    assert _client(lambda r: httpx.Response(200, json=body)).capabilities() == body


def test_capabilities_is_empty_when_the_body_is_not_a_dict():
    from .test_auth import _client

    assert _client(lambda r: httpx.Response(200, json=[1, 2])).capabilities() == {}


def test_capabilities_is_empty_on_an_http_error_status():
    from .test_auth import _client

    assert _client(lambda r: httpx.Response(500)).capabilities() == {}


def test_capabilities_is_empty_when_the_server_is_unreachable():
    from .test_auth import _client

    def boom(r):
        raise httpx.ConnectError("down")

    assert _client(boom).capabilities() == {}


def test_capabilities_is_empty_when_the_body_is_not_json():
    from .test_auth import _client

    assert _client(lambda r: httpx.Response(200, text="not json")).capabilities() == {}


def test_capabilities_sends_no_credentials():
    """This probe can fire before the user has agreed to submit anything (it informs
    the confirmation itself), so it must not identify them to the server first -- even
    though every other ApiClient call, including this same client's get_me/submit,
    stays authenticated. Asserted against what the transport actually received, not
    against the request object `capabilities()` built, so a future refactor back onto
    the shorter `self._client.get(...)` (which would silently re-attach the client's
    default `authorization` header) fails this test rather than passing unnoticed."""
    from .test_auth import _client

    seen: dict[str, object] = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"features": {}})

    client = _client(handler)
    assert client.capabilities() == {"features": {}}  # the body still parses normally
    assert "authorization" not in seen["headers"]


def test_capabilities_uses_a_short_timeout_not_the_clients_default():
    """This probe runs before the pre-submit confirmation prints, so an unresponsive
    or blackholing server must not leave the user waiting through the client's
    ordinary 30s default before they can even decline. The underlying client is built
    with that 30s default here (matching ApiClient's own default) specifically so an
    observed 5s can only be explained by capabilities()'s own per-request override,
    not by happening to match some other default."""
    from trap.auth.client import ApiClient

    seen: dict[str, object] = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"features": {}})

    client = ApiClient("https://srv", "key", timeout=30)
    client.__dict__["_client"] = httpx.Client(
        base_url="https://srv",
        transport=httpx.MockTransport(handler),
        headers={"authorization": "Bearer key"},
        timeout=30,
    )
    assert client.capabilities() == {"features": {}}
    assert seen["timeout"] == {"connect": 5, "read": 5, "write": 5, "pool": 5}


# --- _server_stores_cards: only an explicit, correctly-typed True is "yes" ---------
# --- -- everything else is conservative, and none of it may raise -----------------


@pytest.mark.parametrize(
    "capabilities",
    [
        {},  # no "features" key at all
        {"features": None},
        {"features": "yes"},
        {"features": []},
        {"features": {}},  # no "solution_adapter" key
        {"features": {"solution_adapter": None}},
        {"features": {"solution_adapter": "supported"}},
        {"features": {"solution_adapter": []}},
        {"features": {"solution_adapter": {}}},  # no "supported" key
        {"features": {"solution_adapter": {"supported": False}}},
        {"features": {"solution_adapter": {"supported": "false"}}},  # truthy string
        {"features": {"solution_adapter": {"supported": "true"}}},  # also just a string
        {"features": {"solution_adapter": {"supported": 1}}},
        {"features": {"solution_adapter": {"supported": 0}}},
        {"features": {"solution_adapter": {"supported": None}}},
    ],
    ids=lambda c: json.dumps(c),
)
def test_server_stores_cards_is_conservative_for_everything_but_a_literal_true(capabilities):
    import trap.cli as climod

    assert climod._server_stores_cards(capabilities) is False


def test_server_stores_cards_true_only_for_the_exact_confirmed_shape():
    import trap.cli as climod

    confirmed = {"features": {"solution_adapter": {"supported": True}}}
    assert climod._server_stores_cards(confirmed) is True


# --- tp submit asks the server whether it stores cards -----------------------------
# --- and shows, before publishing, exactly what a carded submit makes public -------


def _carded_run(make_project, runner, tmp_path):
    """A finished run whose report has a card and an anchored solution repo."""
    from trap.cli import app

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    sol = make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    report_path = next((sol / ".trap").rglob("report.json"))
    report = json.loads(report_path.read_text())
    report["provenance"]["solution"] |= {
        "repo": "https://github.com/o/sol",
        "commit": "a" * 40,
        "issue": None,
    }
    report_path.write_text(json.dumps(report))
    return sol


def _submit_with(monkeypatch, runner, capabilities):
    """Run `tp submit --yes` against a server with these capabilities; return what was
    uploaded and what the user was told. `--yes` skips the interactive prompt only --
    the credential-store stand-in is the same one-line monkeypatch the existing submit
    tests in tests/test_cli.py (328-410) use (`TRAPSTREET_API_KEY` set, no dedicated
    fixture exists there to reuse)."""
    from trap.cli import app

    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    sent: dict[str, object] = {}
    monkeypatch.setattr("trap.auth.client.ApiClient.capabilities", lambda self: capabilities)
    monkeypatch.setattr(
        "trap.auth.client.ApiClient.submit",
        lambda self, path: sent.update(payload=json.loads(Path(path).read_text())) or {"run": {"id": "r1"}},
    )
    result = runner.invoke(app, ["submit", "--task", "t", "--yes"])
    assert result.exit_code == 0, result.output
    return sent["payload"]["provenance"]["solution"], result.output


def test_submitting_a_card_to_a_server_that_cannot_store_it_drops_the_repo(
    make_project, runner, tmp_path, monkeypatch
):
    _carded_run(make_project, runner, tmp_path)
    solution, output = _submit_with(monkeypatch, runner, {"features": {}})
    assert solution["adapter"]["shape"] == "cmd" and solution["adapter_digest"]
    assert solution["repo"] is None and solution["commit"] is None
    # "did not confirm it stores", not the stronger "does not store": a capabilities
    # probe that failed outright reads identically to genuine non-support, so the
    # uploaded issue text -- what a third party may read on the site -- must not claim
    # more than is actually known.
    assert "did not confirm" in (solution["issue"] or "")
    assert "not ranked" in output


def test_a_server_that_stores_cards_gets_the_repo_and_the_card(make_project, runner, tmp_path, monkeypatch):
    _carded_run(make_project, runner, tmp_path)
    solution, _ = _submit_with(monkeypatch, runner, {"features": {"solution_adapter": {"supported": True}}})
    assert solution["repo"] == "https://github.com/o/sol"
    assert solution["adapter"]["shape"] == "cmd" and solution["adapter_digest"]


def test_a_run_without_a_card_is_submitted_as_before(make_project, runner, monkeypatch):
    from trap.cli import app

    make_project(cmd="sh -c 'echo hi'", inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    solution, output = _submit_with(monkeypatch, runner, {"features": {}})
    assert solution["adapter"] is None and "did not confirm" not in output


def test_a_carded_but_already_unanchored_run_never_asks_the_server(
    make_project, runner, tmp_path, monkeypatch
):
    """The gate only matters when there is a repo to withhold. A card recorded on a
    solution that was never anchored to begin with (the ordinary `make_project`
    scaffold: not a git repo) has no repo for the gate to touch, so `capabilities()`
    must not even be called -- this is the `adapter is not None and ... .repo` branch
    where `.repo` is falsy."""
    from trap.cli import app

    tool = tmp_path / "tool"
    tool.mkdir()
    (tool / "tool.py").write_text("print('hi')\n")
    cmd = f"{PY} -m trap.shapes.command --repo {tool} --template '{PY} {{repo}}/tool.py' --deadline 30"
    make_project(cmd=cmd, inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0

    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    calls: list[None] = []
    monkeypatch.setattr(
        "trap.auth.client.ApiClient.capabilities", lambda self: calls.append(None) or {"features": {}}
    )
    monkeypatch.setattr("trap.auth.client.ApiClient.submit", lambda self, path: {"run": {"id": "r1"}})

    result = runner.invoke(app, ["submit", "--task", "t", "--yes"])
    assert result.exit_code == 0, result.output
    assert calls == []  # never asked -- there was nothing this gate could withhold
    assert "did not confirm" not in result.output


def test_the_confirmation_shows_the_command_template_verbatim(make_project, runner, tmp_path, monkeypatch):
    """§5.3: an explicit `tp submit`'s confirmation shows verbatim what is about to
    become public. A server that stores cards is used here so the repo-withheld
    message doesn't also appear -- this test is only about the template line."""
    _carded_run(make_project, runner, tmp_path)
    _, output = _submit_with(monkeypatch, runner, {"features": {"solution_adapter": {"supported": True}}})
    assert f"{PY} {{repo}}/tool.py" in output  # the exact cmd, not paraphrased or cut
    assert "public" in output.lower()


def test_the_confirmation_shows_nothing_new_without_a_card(make_project, runner, monkeypatch):
    from trap.cli import app

    make_project(cmd="sh -c 'echo hi'", inputs={"c1": {"question.txt": "q"}})
    assert runner.invoke(app, ["run", "--task", "t", "--no-environment"]).exit_code == 0
    _, output = _submit_with(monkeypatch, runner, {"features": {}})
    assert "public" not in output.lower()
    assert "did not confirm" not in output


def test_confirm_card_shows_the_template_labelled_as_public(capsys):
    import trap.cli as climod

    card = SolutionCard(
        shape="cmd", shape_version=1, cmd="python main.py {prompt}", setup="pip install -r r.txt"
    )
    climod._confirm_card(card, withhold_repo=False)
    err = capsys.readouterr().err
    assert "python main.py {prompt}" in err
    assert "pip install -r r.txt" in err
    assert "public" in err.lower()


def test_confirm_card_survives_a_bracket_in_the_command_unescaped_on_the_way_out(capsys):
    """rich markup uses `[...]`, so a literal `[` in the template must round-trip through
    `rich.markup.escape` and back out unchanged -- a lossy escape would silently break
    the one guarantee this whole confirmation exists for (verbatim, not paraphrased)."""
    import trap.cli as climod

    card = SolutionCard(shape="cmd", shape_version=1, cmd="sed 's/[a-z]//' {prompt}")
    climod._confirm_card(card, withhold_repo=False)
    err = capsys.readouterr().err
    assert "sed 's/[a-z]//' {prompt}" in err


def test_confirm_card_shows_the_setup_line_even_without_a_command(capsys):
    import trap.cli as climod

    card = SolutionCard(shape="cmd", shape_version=1, setup="pip install -r r.txt")
    climod._confirm_card(card, withhold_repo=False)
    err = capsys.readouterr().err
    assert "pip install -r r.txt" in err
    assert "cmd:" not in err


def test_confirm_card_states_the_reason_even_when_the_card_has_no_command(capsys):
    """An ACP or model-direct card carries no `cmd`/`setup` -- there's nothing to
    preview, but the repo-withheld fact still belongs here."""
    import trap.cli as climod

    card = SolutionCard(shape="acp", shape_version=1, agent="a@1", model="m")
    climod._confirm_card(card, withhold_repo=True)
    err = capsys.readouterr().err
    assert "not ranked" in err
    assert "cmd:" not in err and "setup:" not in err


def test_confirm_card_silent_without_a_card_or_a_reason(capsys):
    import trap.cli as climod

    climod._confirm_card(None, withhold_repo=False)
    assert capsys.readouterr().err == ""


def test_confirm_submit_prints_the_withheld_reason_even_when_yes_skips_the_prompt(capsys):
    """--yes means the user pre-consented to skipping the *prompt* -- it must not also
    swallow the *reason* a repo was withheld, since that's the only record a
    non-interactive run leaves behind."""
    import trap.cli as climod
    from trap.models import Provenance, ReportData

    report = ReportData(
        provenance=Provenance(),
        cases_results=(),
        grader_metrics=None,
        started_at_utc="2026-01-01T00:00:00",
        finished_at_utc="2026-01-01T00:00:01",
    )
    climod._confirm_submit(report, "ts-1", "http://s", yes=True, allow_unanchored=False, withhold_repo=True)
    assert "not ranked" in capsys.readouterr().err

"""Cross-language contract tests against the trapstreet web source.

The server enforces an allowlist on everything the CLI sends: event types and
their payload keys (``lib/runs/events.ts``), checkpoint snapshot fields and
execution statuses (``lib/runs/checkpoint.ts``). A mismatch is silent in
production -- a 400 the tracker shrugs off -- so it is caught here instead, by
reading those allowlists straight out of the TypeScript source and checking
every value the CLI can emit against them.

The web checkout is located through ``TRAPSTREET_WEB_SRC`` (the ``apps/web/src``
directory) or, failing that, a sibling checkout of the platform monorepo next to
this repository. Without one the module skips: the contract is asserted wherever
both sides are present, and never guessed where they are not.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from trap.live.identity import LiveSession
from trap.live.outbox import Outbox
from trap.live.tracker import EMITTED_EVENT_TYPES, LiveTracker
from trap.models.cost import CaseCost, ModelCost
from trap.models.results import CaseResult

_EVENTS_TS = Path("lib/runs/events.ts")
_CHECKPOINT_TS = Path("lib/runs/checkpoint.ts")


def _web_src() -> Path | None:
    configured = os.environ.get("TRAPSTREET_WEB_SRC", "").strip()
    if configured:
        return Path(configured)
    workspace = Path(__file__).resolve().parents[2]
    for events in sorted(workspace.glob("*/apps/web/src/lib/runs/events.ts")):
        return events.parents[2]
    return None


@pytest.fixture(scope="module")
def web_src() -> Path:
    found = _web_src()
    if found is None or not (found / _EVENTS_TS).is_file():
        pytest.skip("no trapstreet web checkout (set TRAPSTREET_WEB_SRC to apps/web/src)")
    return found


def _string_list(source: str, name: str) -> set[str]:
    """``export const NAME = [ "a", "b" ]`` (or ``const NAME: T[] = [...]``) as a set."""
    match = re.search(rf"const {name}\b[^=]*=\s*\[([^\]]*)\]", source)
    assert match, f"{name} not found in the web source"
    return set(re.findall(r'"(\w+)"', match.group(1)))


def _event_schema(source: str) -> dict[str, set[str]]:
    """``EVENT_SCHEMA`` as {event type: allowed payload keys}."""
    start = source.index("EVENT_SCHEMA")
    block = source[start : source.index("\n};", start)]
    schema: dict[str, set[str]] = {}
    for match in re.finditer(r"(\w+):\s*\{([^}]*)\}", block):
        schema[match.group(1)] = set(re.findall(r"(\w+):\s*\w+", match.group(2)))
    assert schema, "EVENT_SCHEMA not parsed from the web source"
    return schema


@pytest.fixture(scope="module")
def events_ts(web_src: Path) -> str:
    return (web_src / _EVENTS_TS).read_text()


@pytest.fixture(scope="module")
def checkpoint_ts(web_src: Path) -> str:
    return (web_src / _CHECKPOINT_TS).read_text()


# -- events ----------------------------------------------------------------------


def test_every_event_type_the_cli_emits_is_on_the_web_allowlist(events_ts: str):
    assert EMITTED_EVENT_TYPES <= _string_list(events_ts, "EVENT_TYPES")


def test_the_emitted_set_matches_what_the_tracker_source_actually_emits():
    """The constant the contract is checked against cannot drift from the code."""
    import trap.live.tracker as tracker_module

    source = Path(tracker_module.__file__).read_text()
    literal = set(re.findall(r'_emit\(\s*"(\w+)"', source))
    assert literal == EMITTED_EVENT_TYPES


class _Recorder:
    server = "https://srv"

    def __init__(self) -> None:
        self.events: list[dict] = []

    def whoami(self) -> str:
        return "usr_a"

    def ensure_session(self, client_run_id, **_kwargs):
        return {"run": {"id": "rs_1", "producer_generation": 1}}

    def send_events(self, _run_ref, events):
        self.events.extend(events)
        return {"ack_seq": max(e["client_seq"] for e in events)}

    def close(self) -> None:
        return


def _full_lifecycle(tmp_path: Path) -> list[dict]:
    """Every event the tracker can produce, with every optional field populated."""
    client = _Recorder()
    outbox = Outbox(tmp_path)
    outbox.prepare()
    tracker = LiveTracker(
        client=client,  # type: ignore[arg-type]
        session=LiveSession(client_run_id="r-1", server="https://srv"),
        outbox=outbox,
        run_dir=tmp_path,
        case_ids=["c1", "c2"],
    )
    tracker.start()
    tracker.on_case_start("c1")
    tracker.on_judge_started("c1")
    tracker.on_judge_finished("c1", exit_code=0, score=1.0)
    tracker.on_case_done(
        CaseResult(
            case_id="c1",
            metrics={"score": 1.0},
            judge_exit_code=0,
            duration=1.5,
            cost=CaseCost(by_model=[ModelCost(provider="anthropic", cost_usd=0.25)]),
        )
    )
    tracker.on_case_start("c2")
    tracker.on_case_done(CaseResult(case_id="c2", metrics={"score": 0.0}, judge_exit_code=0))
    tracker.on_case_done(CaseResult(case_id="c2", metrics=None, judge_exit_code=124))
    tracker._emit("heartbeat", {"ordinal": 2})
    tracker.on_grader_started()
    tracker.on_grader_finished(exit_code=0, score=0.5)
    tracker.on_run_finished(exit_code=0, cases_done=2, score=0.5, cost_usd=0.25)
    tracker.on_run_failed("judge_error", cases_done=1)
    tracker.on_run_cancelled(cases_done=1)
    tracker.close()
    return client.events


def test_every_payload_key_the_cli_sends_is_allowed_for_its_type(tmp_path: Path, events_ts: str):
    schema = _event_schema(events_ts)
    sent = _full_lifecycle(tmp_path)
    assert {e["type"] for e in sent} == EMITTED_EVENT_TYPES
    for event in sent:
        extra = set(event["payload"]) - schema[event["type"]]
        assert not extra, f"{event['type']} carries keys the web refuses: {sorted(extra)}"


def test_verdicts_and_error_codes_are_members_of_the_web_enums(tmp_path: Path, events_ts: str):
    verdicts = _string_list(events_ts, "VERDICTS")
    error_codes = _string_list(events_ts, "ERROR_CODES")
    for event in _full_lifecycle(tmp_path):
        payload = event["payload"]
        if "verdict" in payload:
            assert payload["verdict"] in verdicts
        if "error_code" in payload:
            assert payload["error_code"] in error_codes
    # The three verdicts the CLI can derive, and the one error code it emits.
    assert {"passed", "failed", "error"} <= verdicts
    assert "interrupted" in error_codes


# -- checkpoint --------------------------------------------------------------------


def test_every_exec_status_the_cli_can_claim_is_accepted(checkpoint_ts: str):
    from trap.live.sync import _TERMINAL_STATUS, EXEC_STATUSES

    accepted = _string_list(checkpoint_ts, "EXEC_VALUES")
    assert set(EXEC_STATUSES) == accepted
    assert set(_TERMINAL_STATUS.values()) <= accepted
    assert {"running", "finished"} <= accepted  # the two _exec_status falls back to


def test_the_checkpoint_snapshot_carries_only_allowed_fields(tmp_path: Path, checkpoint_ts: str):
    from trap.live.sync import _snapshot

    match = re.search(r"new Set\(\[([^\]]*)\]\)", checkpoint_ts)
    assert match, "the snapshot allowlist was not found in checkpoint.ts"
    allowed = set(re.findall(r'"(\w+)"', match.group(1)))
    assert set(_snapshot(tmp_path, [])) <= allowed


# -- site grading ------------------------------------------------------------------


def _interface_fields(source: str, name: str) -> set[str]:
    """The field names of ``export interface NAME { ... }``."""
    start = source.index(f"interface {name}")
    block = source[start : source.index("\n}", start)]
    return set(re.findall(r"^\s*(\w+)\??:", block, re.MULTILINE))


def test_a_submission_carries_only_fields_the_bulk_route_reads(web_src: Path):
    from trap.live.grading import SiteGrader

    grading_ts = (web_src / "lib/runs/grading.ts").read_text()
    cost = CaseCost(by_model=[ModelCost(provider="p", cost_usd=1.0)])
    result = CaseResult(case_id="c1", duration=1.0, metrics=None, cost=cost)
    submission = SiteGrader._submission(result, "answer")
    assert set(submission) <= _interface_fields(grading_ts, "BulkAnswer")
    assert set(submission["client_reported"]) <= _interface_fields(grading_ts, "ClientReported")


def _quoted_union(source: str, name: str) -> set[str] | None:
    """The string members of ``type NAME = | "a" | "b"`` (or ``NAME: | "a" ...``),
    read across lines; None when the name is absent."""
    match = re.search(rf"{name}\s*[=:]\s*((?:\|?\s*\"\w+\"\s*)+)", source)
    return set(re.findall(r'"(\w+)"', match.group(1))) if match else None


def test_every_reason_the_cli_can_quote_is_one_the_bulk_route_gives(web_src: Path):
    from trap.live.answers import KNOWN_REASONS

    grading_ts = (web_src / "lib/runs/grading.ts").read_text()
    # The per-case submit errors: the multi-line ``error:`` union of SubmitResult.
    start = grading_ts.index("SubmitResult")
    errors = _quoted_union(grading_ts[start:], "error")
    assert errors, "SubmitResult's error union was not found"
    skips = set(re.findall(r'reason: "(\w+)"', grading_ts))
    assert skips, "the bulk route's skip reasons were not found"
    # WRONG_CHANNEL fails the whole request (a 403), never one case.
    assert (errors - {"WRONG_CHANNEL"}) | skips == KNOWN_REASONS


def test_the_receipt_statuses_are_the_webs(web_src: Path):
    from trap.live.answers import RECEIPT_STATUSES

    grading_ts = (web_src / "lib/runs/grading.ts").read_text()
    statuses = _quoted_union(grading_ts, "BulkReceiptStatus")
    if statuses is None:
        pytest.skip("the web checkout has no per-case receipt yet (counts only)")
    assert statuses == RECEIPT_STATUSES


def test_the_evaluation_routes_exist_where_the_cli_calls_them(web_src: Path):
    api = web_src / "app/api/v2"
    assert (api / "evaluations/route.ts").is_file()
    assert (api / "runs/[id]/submissions/route.ts").is_file()
    # The resolve endpoint is being added by the web side; its shape is pinned
    # in test_grading.py and this line turns red the day it lands elsewhere.
    resolve = api / "evaluations/resolve/route.ts"
    if resolve.is_file():
        source = resolve.read_text()
        for name in ("repo", "commit", "path", "revision_id", "cases_total", "admitted"):
            assert name in source, f"resolve route no longer mentions {name}"


# -- the CLI floor the web enforces ------------------------------------------


_TRAP_VERSION_TS = Path("lib/runs/trap-version.ts")


def _version_key(release: tuple[int, int, int], dev: int | None) -> tuple[int, int, int, int, int]:
    """PEP 440 order for the two shapes hatch-vcs emits here: ``X.Y.Z`` and
    ``X.Y.Z.devN`` -- a dev build sorts below its release, dev distance ascending."""
    return (*release, 0 if dev is not None else 1, dev or 0)


def _parse_floor(text: str) -> tuple[int, int, int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:\.dev(\d+))?", text)
    assert match, f"MIN_TRAP_VERSION {text!r} is not a shape hatch-vcs produces"
    dev = match.group(4)
    return _version_key(
        (int(match.group(1)), int(match.group(2)), int(match.group(3))), int(dev) if dev else None
    )


def _checkout_version() -> tuple[int, int, int, int, int] | None:
    """What hatch-vcs would call this checkout, from the same ``git describe`` it
    runs -- not ``trap.__version__``, which is baked at install time and goes stale."""
    repo = Path(__file__).resolve().parents[1]
    try:
        described = subprocess.run(
            ["git", "describe", "--tags", "--long", "--match", "v*", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)-(\d+)-g[0-9a-f]+", described)
    if match is None:
        return None
    major, minor, patch, distance = (int(group) for group in match.groups())
    if distance == 0:
        return _version_key((major, minor, patch), None)
    return _version_key((major, minor, patch + 1), distance)  # hatch-vcs guesses the next patch


def test_the_web_floor_is_not_ahead_of_this_checkout(web_src: Path):
    """The day someone bumps the web's minimum past what main produces, every
    paired ``tp run`` starts being refused; this is where that fails first."""
    source_path = web_src / _TRAP_VERSION_TS
    if not source_path.is_file():
        pytest.skip("the web checkout has no CLI floor yet")
    match = re.search(r'MIN_TRAP_VERSION\s*=\s*"([^"]+)"', source_path.read_text())
    assert match, "MIN_TRAP_VERSION not found in trap-version.ts"
    checkout = _checkout_version()
    if checkout is None:
        pytest.skip("no git tags to describe this checkout with")
    assert checkout >= _parse_floor(match.group(1))

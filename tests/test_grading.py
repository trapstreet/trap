"""Tests for site grading from `tp run`: answers submitted, the site judges.

The load-bearing property is the same as live sync's: nothing here may change
what the run does. Every failure -- no revision, no network, a lost connection
after the third case, an answer file that cannot be read -- ends with the
grader quietly off, one line of notice, and the run's report and exit code
exactly as a locally judged run would have them.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from trap import __version__
from trap.live.client import LiveApiError, LiveClient
from trap.live.grading import SiteGrader, site_grading_disabled_by_env, start_site_grading
from trap.models.cost import CaseCost, ModelCost
from trap.models.provenance import GitProvenance
from trap.models.results import CaseResult

ANCHORED = GitProvenance(repo="https://github.com/org/task", commit="abc123", subdirectory="tasks/a")
TP_RUNTIME = {"orchestrator": "tp", "executor": "tp", "trap_version": __version__}
_ADMITTED = {"revision_id": "ev_1", "cases_total": 2, "admitted": True}


class _Site:
    """A LiveClient stand-in: scripted answers to the three grading calls."""

    def __init__(
        self,
        *,
        resolve: object | None = None,
        opened: object | None = None,
        submits: list[object] | None = None,
    ) -> None:
        self.server = "https://srv"
        self._resolve = resolve if resolve is not None else _ADMITTED
        self._opened = (
            opened
            if opened is not None
            else {"run": {"id": "rs_9", "channel": "platform_grading"}, "view_url": "https://srv/runs/rs_9"}
        )
        self._submits = list(submits or [])
        self.resolved: list[dict] = []
        self.opens: list[dict] = []
        self.submissions: list[tuple[str, list[dict]]] = []
        self.closed = False

    def resolve_evaluation(self, **kwargs):
        self.resolved.append(kwargs)
        if isinstance(self._resolve, LiveApiError):
            raise self._resolve
        return self._resolve

    def open_evaluation(self, **kwargs):
        self.opens.append(kwargs)
        if isinstance(self._opened, LiveApiError):
            raise self._opened
        return self._opened

    def submit_answers(self, run_id, cases_results):
        self.submissions.append((run_id, cases_results))
        if self._submits:
            answer = self._submits.pop(0)
            if isinstance(answer, LiveApiError):
                raise answer
        return {"accepted": 1}

    def close(self) -> None:
        self.closed = True


class _Store:
    def __init__(self, key: str | None = "k") -> None:
        self._key = key

    def load(self, server):
        from trap.auth.store import Credential

        return None if self._key is None else Credential(server=server, api_key=self._key, user_id="usr_a")


def _start(monkeypatch, site: _Site | None = None, *, task=ANCHORED, store=None, **kwargs):
    monkeypatch.delenv("TRAP_NO_SITE_GRADING", raising=False)
    site = site or _Site()
    monkeypatch.setattr("trap.live.grading.CredentialStore", lambda: store or _Store())
    monkeypatch.setattr("trap.live.grading.LiveClient", lambda *_a, **_k: site)
    answers = {"c1": "forty-two", "c2": "x"}
    grader = start_site_grading(
        task=task, cases_total=2, answer_of=lambda case_id: answers[case_id], **kwargs
    )
    return grader, site


# -- when grading is on -------------------------------------------------------


def test_an_admitted_task_opens_a_graded_run_under_an_id_derived_from_the_live_one(monkeypatch):
    # Not the SAME id: the site keys sessions by (owner, client_run_id) across
    # both channels, so reusing the live id would collide with the private
    # progress session instead of joining it.
    grader, site = _start(monkeypatch, client_run_id="r-1")
    assert grader is not None and grader.opened
    assert grader.url == "https://srv/runs/rs_9"
    assert grader.notice is None
    assert site.resolved == [{"repo": ANCHORED.repo, "commit": "abc123", "path": "tasks/a"}]
    assert site.opens == [{"revision_id": "ev_1", "client_run_id": "r-1-site", "runtime": TP_RUNTIME}]
    summary = grader.summary()
    assert summary is not None and summary.model_dump() == {"run_id": "rs_9", "url": "https://srv/runs/rs_9"}


def test_without_a_live_session_a_fresh_id_is_minted(monkeypatch):
    _grader, site = _start(monkeypatch)
    assert site.opens[0]["client_run_id"].startswith("r-")


def test_each_answer_is_the_solvers_stdout_in_the_reports_own_shape(monkeypatch):
    grader, site = _start(monkeypatch)
    assert grader is not None
    grader.on_case_done(
        CaseResult(
            case_id="c1",
            exit_code=0,
            duration=1.25,
            metrics={"score": 1.0, "expected": "SENTINEL"},
            cost=CaseCost(by_model=[ModelCost(provider="anthropic", cost_usd=0.02)]),
        )
    )
    grader.on_case_done(CaseResult(case_id="c2", exit_code=1, duration=0.5, metrics=None))
    assert [run_id for run_id, _ in site.submissions] == ["rs_9", "rs_9"]
    first, second = (batch[0] for _, batch in site.submissions)
    assert first == {
        "case_id": "c1",
        "answer": "forty-two",
        "duration": 1.25,
        "exit_code": 0,
        "client_reported": {"duration_ms": 1250, "cost_usd": 0.02},
    }
    # An errored solver's case is still reported -- the site skips it itself
    # and keeps the case visibly unanswered -- and an unknown cost is absent.
    assert second["exit_code"] == 1 and second["client_reported"] == {"duration_ms": 500}
    # Nothing the judge said crosses: no score, no metrics, no expected answer.
    assert "SENTINEL" not in json.dumps(site.submissions) and "score" not in json.dumps(site.submissions)


def test_a_view_url_the_site_omits_is_derived(monkeypatch):
    grader, _site = _start(monkeypatch, _Site(opened={"run": {"id": "rs_9"}}))
    assert grader is not None and grader.url == "https://srv/runs/rs_9"


def test_a_case_count_that_differs_from_the_sites_is_said_once(monkeypatch):
    grader, _site = _start(monkeypatch, _Site(resolve={**_ADMITTED, "cases_total": 5}))
    assert grader is not None and grader.opened
    assert grader.notice is not None and "5 case(s)" in grader.notice and "covers 2" in grader.notice


def test_close_releases_the_client(monkeypatch):
    grader, site = _start(monkeypatch)
    assert grader is not None
    grader.close()
    assert site.closed is True


# -- the quiet paths: None, and nothing said ------------------------------------


def test_the_flag_turns_it_off(monkeypatch):
    grader, site = _start(monkeypatch, enabled=False)
    assert grader is None and site.resolved == []


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_env_switch_turns_it_off(monkeypatch, value):
    monkeypatch.setenv("TRAP_NO_SITE_GRADING", value)
    assert site_grading_disabled_by_env() is True
    monkeypatch.setattr("trap.live.grading.CredentialStore", lambda: _Store())
    assert start_site_grading(task=ANCHORED, cases_total=1, answer_of=str) is None


def test_other_env_values_leave_it_on(monkeypatch):
    monkeypatch.setenv("TRAP_NO_SITE_GRADING", "0")
    assert site_grading_disabled_by_env() is False


@pytest.mark.parametrize(
    "task",
    [GitProvenance(), GitProvenance(repo="https://github.com/org/task"), GitProvenance(commit="abc")],
)
def test_an_unanchored_task_cannot_be_graded(monkeypatch, task):
    grader, site = _start(monkeypatch, task=task)
    assert grader is None and site.resolved == []


def test_an_unpaired_cli_grades_nothing(monkeypatch):
    grader, _site = _start(monkeypatch, store=_Store(key=None))
    assert grader is None


def test_an_unreadable_credential_file_is_not_a_run_problem(monkeypatch):
    from trap.auth.store import CredentialStoreError

    class _Broken:
        def load(self, _server):
            raise CredentialStoreError("bad file")

    grader, _site = _start(monkeypatch, store=_Broken())
    assert grader is None


def test_a_task_the_site_has_not_admitted_is_the_ordinary_case(monkeypatch):
    grader, site = _start(monkeypatch, _Site(resolve=LiveApiError("http 404", status=404)))
    assert grader is None
    assert site.opens == [] and site.closed is True


@pytest.mark.parametrize(
    "resolve",
    [
        {"revision_id": "ev_1", "cases_total": 2, "admitted": False},
        {"cases_total": 2, "admitted": True},
        {"revision_id": 7, "admitted": True},
    ],
)
def test_a_revision_that_is_not_admitted_or_not_named_is_not_used(monkeypatch, resolve):
    grader, site = _start(monkeypatch, _Site(resolve=resolve))
    assert grader is None and site.opens == []


# -- fail-open: a grader that never opened, and says why --------------------------


def test_an_unreachable_site_at_start_turns_grading_off_for_the_run(monkeypatch):
    grader, site = _start(monkeypatch, _Site(resolve=LiveApiError("unreachable")))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "could not resolve" in grader.notice
    assert "judged locally only" in grader.notice
    assert grader.summary() is None and grader.url is None
    # Nothing is submitted, and nothing is queued for later.
    grader.on_case_done(CaseResult(case_id="c1", metrics=None))
    assert site.submissions == []


def test_a_run_the_site_will_not_open_is_reported(monkeypatch):
    grader, _site = _start(monkeypatch, _Site(opened=LiveApiError("http 503", status=503)))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "could not open a graded run" in grader.notice


TOO_OLD = LiveApiError(
    "http 426",
    status=426,
    payload={"code": "CLIENT_TOO_OLD", "error": "Install the pinned tp build: uv tool install --force X"},
)


def test_opening_a_graded_run_reports_the_tp_version(monkeypatch):
    _grader, site = _start(monkeypatch)
    assert site.opens[0]["runtime"] == TP_RUNTIME
    assert site.opens[0]["runtime"]["trap_version"] == __version__


def test_a_server_that_refuses_this_build_is_quoted_once_and_grading_is_off(monkeypatch):
    grader, site = _start(monkeypatch, _Site(opened=TOO_OLD))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "Install the pinned tp build" in grader.notice
    assert "http 426" not in grader.notice
    grader.on_case_done(CaseResult(case_id="c1", metrics=None))
    assert site.submissions == []


def test_a_refusal_at_resolve_time_is_quoted_too(monkeypatch):
    grader, _site = _start(monkeypatch, _Site(resolve=TOO_OLD))
    assert grader is not None and grader.notice is not None
    assert "Install the pinned tp build" in grader.notice


def test_a_refusal_without_words_still_says_what_it_is(monkeypatch):
    grader, _site = _start(monkeypatch, _Site(opened=LiveApiError("http 426", status=426)))
    assert grader is not None and grader.notice is not None
    assert "this server needs a newer tp" in grader.notice


def test_an_answer_without_a_run_id_is_reported(monkeypatch):
    grader, _site = _start(monkeypatch, _Site(opened={"view_url": "https://srv/runs/x"}))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "without a run id" in grader.notice


def test_an_id_the_site_holds_as_a_local_session_is_not_submitted_to(monkeypatch):
    # The live session and the graded run share one client_run_id. If the site
    # answers with the local session instead, every submission would be refused;
    # better to say so once and grade locally.
    grader, site = _start(monkeypatch, _Site(opened={"run": {"id": "rs_1", "channel": "local_report"}}))
    assert grader is not None and not grader.opened
    assert grader.notice is not None and "non-graded session" in grader.notice
    grader.on_case_done(CaseResult(case_id="c1", metrics=None))
    assert site.submissions == []


# -- fail-open: losing the site midway ---------------------------------------------


def test_losing_the_site_midway_stops_submitting_and_says_how_far_it_got(monkeypatch):
    grader, site = _start(monkeypatch, _Site(submits=[{}, LiveApiError("unreachable")]))
    assert grader is not None
    for case_id in ("c1", "c2", "c1"):
        grader.on_case_done(CaseResult(case_id=case_id, metrics=None))
    assert len(site.submissions) == 2  # the third was never attempted
    assert grader.notice is not None and "after 1 answer(s)" in grader.notice
    # The run still exists on the site, so the report still points at it.
    assert grader.summary() is not None


def test_an_answer_that_cannot_be_read_stops_submitting(monkeypatch):
    site = _Site()
    monkeypatch.setattr("trap.live.grading.CredentialStore", lambda: _Store())
    monkeypatch.setattr("trap.live.grading.LiveClient", lambda *_a, **_k: site)

    def unreadable(_case_id: str) -> str:
        raise OSError("no stdout")

    grader = start_site_grading(task=ANCHORED, cases_total=2, answer_of=unreadable)
    assert grader is not None
    grader.on_case_done(CaseResult(case_id="c1", metrics=None))
    assert site.submissions == []
    assert grader.notice is not None and "could not read the answer for case 1" in grader.notice


def test_only_the_first_notice_is_kept():
    grader = SiteGrader(client=_Site(), answer_of=str, run_id="rs_9", url="u", notice="first")  # type: ignore[arg-type]
    grader._stop("second")
    assert grader.notice == "first"


# -- the HTTP calls ----------------------------------------------------------------


def _live_client(handler) -> LiveClient:
    client = LiveClient("https://srv", "key")
    client.__dict__["_client"] = httpx.Client(base_url="https://srv", transport=httpx.MockTransport(handler))
    return client


def test_resolve_is_a_get_with_the_task_locator():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, query=dict(request.url.params))
        return httpx.Response(200, json=_ADMITTED)

    result = _live_client(handler).resolve_evaluation(repo="https://github.com/o/r", commit="abc", path="t/a")
    assert result["revision_id"] == "ev_1"
    assert seen == {
        "method": "GET",
        "path": "/api/v2/evaluations/resolve",
        "query": {"repo": "https://github.com/o/r", "commit": "abc", "path": "t/a"},
    }


def test_resolve_omits_the_path_for_a_task_at_the_repo_root():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={})

    _live_client(handler).resolve_evaluation(repo="https://github.com/o/r", commit="abc", path=None)
    assert seen["query"] == {"repo": "https://github.com/o/r", "commit": "abc"}


def test_open_posts_the_revision_and_the_client_run_id():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, body=json.loads(request.content))
        return httpx.Response(201, json={"run": {"id": "rs_9"}, "view_url": "https://srv/runs/rs_9"})

    result = _live_client(handler).open_evaluation(
        revision_id="ev_1", client_run_id="r-1", runtime=TP_RUNTIME
    )
    assert result["run"]["id"] == "rs_9"
    assert seen == {
        "method": "POST",
        "path": "/api/v2/evaluations",
        "body": {"revision_id": "ev_1", "client_run_id": "r-1", "runtime": TP_RUNTIME},
    }


def test_open_without_a_runtime_sends_an_empty_block():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"run": {"id": "rs_9"}})

    _live_client(handler).open_evaluation(revision_id="ev_1", client_run_id="r-1")
    assert seen["body"] == {"revision_id": "ev_1", "client_run_id": "r-1", "runtime": {}}


def test_submit_posts_cases_results_to_the_runs_submissions():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(method=request.method, path=request.url.path, body=json.loads(request.content))
        return httpx.Response(200, json={"accepted": 1, "grading": "queued"})

    result = _live_client(handler).submit_answers("rs_9", [{"case_id": "c1", "answer": "x"}])
    assert result["grading"] == "queued"
    assert seen == {
        "method": "POST",
        "path": "/api/v2/runs/rs_9/submissions",
        "body": {"cases_results": [{"case_id": "c1", "answer": "x"}]},
    }


# -- tp run --------------------------------------------------------------------------


class _FakeGrader:
    def __init__(self, *, opened: bool = True, notice: str | None = None) -> None:
        self.opened = opened
        self.url = "https://srv/runs/rs_9" if opened else None
        self.notice = notice
        self.cases: list[str] = []
        self.closed = False
        self.started_with: dict[str, object] = {}

    def on_case_done(self, result) -> None:
        self.cases.append(result.case_id)

    def summary(self):
        from trap.models.report import SiteGrading

        return SiteGrading(run_id="rs_9", url=self.url) if self.opened else None

    def close(self) -> None:
        self.closed = True


def _use_fake_grader(monkeypatch, grader: _FakeGrader) -> None:
    def start(**kwargs):
        grader.started_with = kwargs
        return grader

    monkeypatch.setattr("trap.cli.start_site_grading", start)


def _run_dir_of(project: Path) -> Path:
    return next((project / ".trap" / "runs").glob("*/t/*"))


def test_tp_run_grades_on_site_and_records_where(make_project, runner, monkeypatch):
    from tests.test_live import _FakeTracker, _use_fake_tracker
    from trap.cli import app

    grader = _FakeGrader()
    _use_fake_grader(monkeypatch, grader)
    _use_fake_tracker(monkeypatch, _FakeTracker())
    project = make_project(cmd="sh -c 'cat'", stdin="input.txt", cases=["c1", "c2"])
    result = runner.invoke(app, ["run", "--no-environment"])
    assert result.exit_code == 0, result.output
    assert "graded on site · https://srv/runs/rs_9" in result.output
    assert grader.cases == ["c1", "c2"] and grader.closed is True
    # Opened under the live session's id, with the run's case count, and able
    # to read each case's answer from the workspace.
    assert grader.started_with["client_run_id"] == "r-1"
    assert grader.started_with["cases_total"] == 2
    answer_of = grader.started_with["answer_of"]
    assert callable(answer_of) and answer_of("c1") == "hi"
    report = json.loads((_run_dir_of(project) / "report.json").read_text())
    assert report["site_grading"] == {"run_id": "rs_9", "url": "https://srv/runs/rs_9"}


def test_a_grader_that_never_opened_prints_nothing_but_its_notice(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    _use_fake_grader(monkeypatch, _FakeGrader(opened=False, notice="site grading off for this run: x"))
    project = make_project(cmd="sh -c 'cat'", cases=["c1"])
    result = runner.invoke(app, ["run", "--no-environment"])
    assert result.exit_code == 0, result.output
    assert "graded on site" not in result.output
    assert "site grading off for this run: x" in result.output
    report = json.loads((_run_dir_of(project) / "report.json").read_text())
    assert report["site_grading"] is None


def test_no_site_grading_passes_the_flag_through(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    grader = _FakeGrader()
    _use_fake_grader(monkeypatch, grader)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment", "--no-site-grading"]).exit_code == 0
    assert grader.started_with["enabled"] is False
    assert grader.started_with["client_run_id"] is None  # no live session: none to share


def test_ctrl_c_closes_the_grader(make_project, runner, monkeypatch):
    from trap.cli import app
    from trap.runner import TaskRunner

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    grader = _FakeGrader()
    _use_fake_grader(monkeypatch, grader)

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(TaskRunner, "run", interrupted)
    make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code != 0
    assert grader.closed is True


def test_a_run_with_no_grader_records_none(make_project, runner, monkeypatch):
    from trap.cli import app

    monkeypatch.setattr("trap.cli.start_tracking", lambda **_kwargs: None)
    monkeypatch.setattr("trap.cli.start_site_grading", lambda **_kwargs: None)
    project = make_project(cmd="sh -c 'cat'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    assert json.loads((_run_dir_of(project) / "report.json").read_text())["site_grading"] is None

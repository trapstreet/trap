"""`tp task register`: what a maintainer's agent sends, and what it is told back."""

from __future__ import annotations

from pathlib import Path

import pytest

from trap.auth import ApiError
from trap.cli import app

REF = "trapstreet/open-benchmarks/bird/tasks/bird-small"
ARGS = ["task", "register", REF, "--id", "bird-small", "--title", "BIRD small", "--tag", "database"]
RESPONSE = {"task": {"id": "bird-small", "latest": {"commit_sha": "a" * 40}}}


@pytest.fixture
def register(monkeypatch):
    calls: list[dict] = []

    def fake(self, body):
        calls.append(body)
        return RESPONSE

    monkeypatch.setattr("trap.auth.ApiClient.register_task", fake)
    monkeypatch.setenv("TRAPSTREET_URL", "https://uat.example")
    monkeypatch.setenv("TRAPSTREET_API_KEY", "key")
    return calls


def test_sends_the_ref_id_title_tags_and_defaults(runner, register):
    res = runner.invoke(app, [*ARGS, "--tag", "sql"])
    assert res.exit_code == 0, res.output
    assert register == [
        {
            "id": "bird-small",
            "title": "BIRD small",
            "traptask_ref": REF,
            "tags": ["database", "sql"],
            "ranking_metric": "score",
            "ranking_direction": "desc",
            "visibility": "public",
        }
    ]


def test_private_and_metric_and_summary(runner, register):
    res = runner.invoke(app, [*ARGS, "--private", "--metric", "pass_rate", "--summary", "One line."])
    assert res.exit_code == 0, res.output
    assert register[0]["visibility"] == "private"
    assert register[0]["ranking_metric"] == "pass_rate"
    assert register[0]["summary"] == "One line."


def test_a_tag_is_required(runner, register):
    res = runner.invoke(app, ["task", "register", REF, "--id", "bird-small", "--title", "BIRD small"])
    assert res.exit_code != 0
    assert register == []


def test_not_logged_in(runner, monkeypatch, register):
    monkeypatch.delenv("TRAPSTREET_API_KEY", raising=False)
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", Path("/nonexistent/auth.json"))
    res = runner.invoke(app, ARGS)
    assert res.exit_code == 2
    assert "not logged in" in res.output
    assert register == []


def test_success_names_the_task_its_commit_and_the_grading_page(runner, register):
    res = runner.invoke(app, ARGS)
    assert "https://uat.example/tasks/bird-small" in res.output
    assert "a" * 12 in res.output
    # A task with a private judge is not gradable until a person binds its pack.
    assert "https://uat.example/tasks/bird-small/grading" in res.output


def test_a_refusal_shows_the_sites_reason(runner, monkeypatch, register):
    def refuse(self, body):
        raise ApiError("http 403: limited to maintainers", status=403)

    monkeypatch.setattr("trap.auth.ApiClient.register_task", refuse)
    res = runner.invoke(app, ARGS)
    assert res.exit_code == 2
    assert "limited to maintainers" in res.output


def test_help_renders(runner):
    # The help text passes through rich markup: "owner/repo[/path]" crashed it.
    res = runner.invoke(app, ["task", "register", "--help"])
    assert res.exit_code == 0, res.output
    assert "owner/repo" in res.output


@pytest.mark.parametrize(
    ("metric", "direction"),
    [("score", "desc"), ("pass_rate", "desc"), ("cost", "asc"), ("latency", "asc")],
)
def test_direction_follows_the_metric(runner, register, metric, direction):
    # The site defaults to desc and a direction can't be edited after registering:
    # without this, a cost board would rank the most expensive run first for good.
    res = runner.invoke(app, [*ARGS, "--metric", metric])
    assert res.exit_code == 0, res.output
    assert register[0]["ranking_direction"] == direction


def test_a_taken_id_shows_the_sites_reason(runner, monkeypatch, register):
    def taken(self, body):
        raise ApiError("http 409: task bird-small already exists", status=409)

    monkeypatch.setattr("trap.auth.ApiClient.register_task", taken)
    res = runner.invoke(app, ARGS)
    assert res.exit_code == 2
    assert "already exists" in res.output


def test_a_reason_with_brackets_is_printed_not_parsed(runner, monkeypatch, register):
    def refuse(self, body):
        raise ApiError("http 400: bad ref owner/repo[/path]", status=400)

    monkeypatch.setattr("trap.auth.ApiClient.register_task", refuse)
    res = runner.invoke(app, ARGS)
    assert res.exit_code == 2
    assert "owner/repo[/path]" in res.output


def test_server_flag_never_borrows_another_servers_stored_token(runner, monkeypatch, register, tmp_path):
    monkeypatch.delenv("TRAPSTREET_API_KEY", raising=False)
    monkeypatch.delenv("TRAPSTREET_URL", raising=False)
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", tmp_path / "auth.json")
    login = runner.invoke(
        app, ["auth", "login", "--server", "https://a.example", "--with-token"], input="a-key\n"
    )
    assert login.exit_code == 0, login.output
    res = runner.invoke(app, [*ARGS, "--server", "https://b.example"])
    assert res.exit_code == 2
    assert "not logged in" in res.output
    assert register == []


def test_a_success_without_a_task_body_still_reports_the_requested_id(runner, monkeypatch, register):
    # The task exists by now: a traceback here would send an agent into a retry and a 409.
    monkeypatch.setattr("trap.auth.ApiClient.register_task", lambda self, body: {})
    res = runner.invoke(app, ARGS)
    assert res.exit_code == 0, res.output
    assert "https://uat.example/tasks/bird-small" in res.output


def test_an_unreadable_credential_store_is_reported_not_raised(runner, monkeypatch, tmp_path):
    monkeypatch.delenv("TRAPSTREET_API_KEY", raising=False)
    store = tmp_path / "auth.json"
    store.write_text("not json")
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", store)
    res = runner.invoke(app, ARGS)
    assert res.exit_code == 2
    assert "error" in res.output

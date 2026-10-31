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

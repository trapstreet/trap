"""CLI tests for `tp auth` and `tp submit` (the auth-touching commands)."""

from __future__ import annotations

from trap.cli import app

from .conftest import GRADER_PASS, JUDGE_SCORE


def _store_at(monkeypatch, tmp_path):
    monkeypatch.setattr("trap.auth.store.AuthStore.PATH", tmp_path / "auth.json")


def test_auth_login_with_token(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "login", "--with-token"], input="mytoken\n")
    assert res.exit_code == 0, res.output
    assert "logged in" in res.output
    assert (tmp_path / "auth.json").exists()


def test_auth_login_browser_custom_server_rejected(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "login", "--server", "https://other.example"])
    assert res.exit_code == 2
    assert "browser login is only supported" in res.output


def test_auth_logout(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text('{"server": "https://s", "api_key": "k"}')
    res = runner.invoke(app, ["auth", "logout"])
    assert res.exit_code == 0 and "removed" in res.output
    res2 = runner.invoke(app, ["auth", "logout"])
    assert "already logged out" in res2.output


def test_auth_status_not_logged_in(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 1
    assert "not logged in" in res.output


def test_auth_status_no_verify(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text('{"server": "https://s", "api_key": "k", "solution": "sol"}')
    res = runner.invoke(app, ["auth", "status", "--no-verify"])
    assert res.exit_code == 0
    assert "sol" in res.output


def test_auth_status_verify(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text('{"server": "https://s", "api_key": "k"}')
    monkeypatch.setattr("trap.auth.client.ApiClient.get_me", lambda self: {"user": {"name": "Alice"}})
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 0
    assert "Alice" in res.output and "valid" in res.output


# -- submit -------------------------------------------------------------------


def _passing(make_project, **kw):
    return make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        cases=["c1"],
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
        grader_src=GRADER_PASS,
        **kw,
    )


def test_submit_success(make_project, runner, tmp_path, monkeypatch):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    _store_at(monkeypatch, tmp_path)
    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    monkeypatch.setattr(
        "trap.auth.client.ApiClient.submit",
        lambda self, path: {"run": {"id": "r1"}, "view_url": "http://x/runs/r1"},
    )
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 0, res.output
    assert "submitted" in res.output and "r1" in res.output

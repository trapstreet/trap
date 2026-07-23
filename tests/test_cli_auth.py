"""CLI tests for `tp auth` and `tp submit` (the auth-touching commands)."""

from __future__ import annotations

import json

from trap.auth import DEFAULT_SERVER, Credential, CredentialStore
from trap.cli import app

from .conftest import GRADER_PASS, JUDGE_SCORE

UAT = "https://uat.trapstreet.run"


def _store_at(monkeypatch, tmp_path):
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", tmp_path / "auth.json")


# -- login --------------------------------------------------------------------


def test_auth_login_with_token(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "login", "--with-token"], input="mytoken\n")
    assert res.exit_code == 0, res.output
    assert "logged in" in res.output
    stored = CredentialStore().load()  # no --server → the default credential
    assert stored is not None and stored.api_key == "mytoken"


def test_auth_login_second_server_keeps_first(runner, tmp_path, monkeypatch):
    """Logging in to UAT must not clobber the prod pairing (per-server credentials)."""
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="prod-key"))
    res = runner.invoke(app, ["auth", "login", "--server", UAT, "--with-token"], input="uat-key\n")
    assert res.exit_code == 0, res.output
    prod, uat = CredentialStore().load(), CredentialStore().load(UAT)
    assert prod is not None and prod.api_key == "prod-key"
    assert uat is not None and uat.api_key == "uat-key"


def test_auth_login_default_ignores_other_servers_credential(runner, tmp_path, monkeypatch):
    """No --server → the default server, even when only uat is stored (a credential is
    never borrowed as the login target)."""
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=UAT, api_key="uat-key"))
    res = runner.invoke(app, ["auth", "login", "--with-token"], input="prod-key\n")
    assert res.exit_code == 0, res.output
    prod = CredentialStore().load()
    assert prod is not None and prod.api_key == "prod-key"


def test_auth_login_browser_custom_server_rejected(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "login", "--server", "https://other.example"])
    assert res.exit_code == 2
    assert "browser login is only supported" in res.output


# -- logout -------------------------------------------------------------------


def test_auth_logout(runner, tmp_path, monkeypatch):
    """Default logout targets the default server; a legacy single-object file counts."""
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text(json.dumps({"server": DEFAULT_SERVER, "api_key": "k"}))
    res = runner.invoke(app, ["auth", "logout"])
    assert res.exit_code == 0 and "removed" in res.output
    res2 = runner.invoke(app, ["auth", "logout"])
    assert "already logged out" in res2.output


def test_auth_logout_is_server_scoped(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="p"))
    CredentialStore().save(Credential(server=UAT, api_key="u"))
    res = runner.invoke(app, ["auth", "logout", "--server", UAT])
    assert res.exit_code == 0, res.output
    assert "removed" in res.output and UAT in res.output
    assert CredentialStore().load(UAT) is None
    prod = CredentialStore().load()
    assert prod is not None and prod.api_key == "p"  # the prod pairing survives


# -- status -------------------------------------------------------------------


def test_auth_status_not_logged_in(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 1
    assert "not logged in" in res.output


def test_auth_status_no_verify(runner, tmp_path, monkeypatch):
    """--no-verify shows the resolved server + token source with no network call."""
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text(json.dumps({"server": DEFAULT_SERVER, "api_key": "k"}))
    res = runner.invoke(app, ["auth", "status", "--no-verify"])
    assert res.exit_code == 0
    assert "(stored)" in res.output and DEFAULT_SERVER in res.output


def test_auth_status_verify(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text(json.dumps({"server": DEFAULT_SERVER, "api_key": "k"}))
    monkeypatch.setattr("trap.auth.client.ApiClient.get_me", lambda self: {"user": {"name": "Alice"}})
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 0
    assert "Alice" in res.output and "valid" in res.output


def test_auth_status_server_flag_selects_credential(runner, tmp_path, monkeypatch):
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="p"))
    CredentialStore().save(Credential(server=UAT, api_key="u"))
    res = runner.invoke(app, ["auth", "status", "--no-verify", "--server", UAT])
    assert res.exit_code == 0, res.output
    # --server picked the uat credential (its server + a stored token)
    assert "https://uat.trapstreet.run" in res.output and "(stored)" in res.output


def test_auth_status_env_server_without_credential_says_login(runner, tmp_path, monkeypatch):
    """TRAPSTREET_URL points at a server we never paired: the prod token can't be
    borrowed for it — status reports logged-out for that server."""
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="prod-key"))
    monkeypatch.setenv("TRAPSTREET_URL", UAT)
    res = runner.invoke(app, ["auth", "status", "--no-verify"])
    assert res.exit_code == 1
    assert "not logged in" in res.output and "uat.trapstreet.run" in res.output
    assert "--server" in res.output  # the hint names the fix
    assert DEFAULT_SERVER in res.output  # existing pairings are listed


def test_auth_status_env_key_only(runner, tmp_path, monkeypatch):
    """TRAPSTREET_API_KEY alone counts as logged in — status shows and verifies it."""
    _store_at(monkeypatch, tmp_path)  # no auth.json
    monkeypatch.setenv("TRAPSTREET_API_KEY", "env-key")
    seen = {}

    def fake_get_me(self):
        seen["server"], seen["key"] = self._server, self._api_key
        return {"user": {"name": "Bob"}}

    monkeypatch.setattr("trap.auth.client.ApiClient.get_me", fake_get_me)
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 0, res.output
    assert "Bob" in res.output and "(env)" in res.output
    assert seen == {"server": DEFAULT_SERVER, "key": "env-key"}


def test_auth_status_invalid_token_exits_1(runner, tmp_path, monkeypatch):
    from trap.auth import ApiError

    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="stale"))

    def failing_get_me(self):
        raise ApiError("http 401: unauthorized")

    monkeypatch.setattr("trap.auth.client.ApiClient.get_me", failing_get_me)
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 1
    assert "401" in res.output


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


def test_submit_stored_token_never_sent_to_unpaired_server(runner, tmp_path, monkeypatch):
    """TRAPSTREET_URL points at a server we never logged in to; the prod credential must
    not be borrowed for it — submit refuses before any API call."""
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="prod-key"))
    monkeypatch.setenv("TRAPSTREET_URL", UAT)

    def no_submit(self, path):  # any API call here means the token leaked
        raise AssertionError("submit must not be called for an unpaired server")

    monkeypatch.setattr("trap.auth.client.ApiClient.submit", no_submit)
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 2, res.output
    assert "not logged in" in res.output and "uat.trapstreet.run" in res.output


def test_submit_uses_credential_matching_env_server(make_project, runner, tmp_path, monkeypatch):
    """TRAPSTREET_URL selects the uat credential — its key, not prod's, is sent."""
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="prod-key"))
    CredentialStore().save(Credential(server=UAT, api_key="uat-key"))
    monkeypatch.setenv("TRAPSTREET_URL", UAT)
    seen = {}

    def fake_submit(self, path):
        seen["server"], seen["key"] = self._server, self._api_key
        return {"run": {"id": "r1"}, "view_url": "http://x/runs/r1"}

    monkeypatch.setattr("trap.auth.client.ApiClient.submit", fake_submit)
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 0, res.output
    assert seen == {"server": UAT, "key": "uat-key"}


def test_submit_env_key_pairs_with_env_server(make_project, runner, tmp_path, monkeypatch):
    """Both server and key from env: submit hits the env server with the env key,
    ignoring stored credentials entirely."""
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    _store_at(monkeypatch, tmp_path)
    CredentialStore().save(Credential(server=DEFAULT_SERVER, api_key="prod-key"))
    monkeypatch.setenv("TRAPSTREET_URL", UAT)
    monkeypatch.setenv("TRAPSTREET_API_KEY", "uat-key")
    seen = {}

    def fake_submit(self, path):
        seen["server"], seen["key"] = self._server, self._api_key
        return {"run": {"id": "r1"}, "view_url": "http://x/runs/r1"}

    monkeypatch.setattr("trap.auth.client.ApiClient.submit", fake_submit)
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 0, res.output
    assert seen == {"server": UAT, "key": "uat-key"}


# -- corrupt store (present but unparseable → clean error, not logged-out) ------


def _corrupt_store(monkeypatch, tmp_path):
    _store_at(monkeypatch, tmp_path)
    (tmp_path / "auth.json").write_text("not json{{{")


def test_auth_status_corrupt_store_errors(runner, tmp_path, monkeypatch):
    _corrupt_store(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "status", "--no-verify"])
    assert res.exit_code == 2
    assert "credential store" in res.output


def test_auth_login_corrupt_store_errors(runner, tmp_path, monkeypatch):
    _corrupt_store(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "login", "--with-token"], input="tok\n")
    assert res.exit_code == 2
    assert "credential store" in res.output


def test_auth_logout_corrupt_store_errors(runner, tmp_path, monkeypatch):
    _corrupt_store(monkeypatch, tmp_path)
    res = runner.invoke(app, ["auth", "logout"])
    assert res.exit_code == 2
    assert "credential store" in res.output


def test_submit_corrupt_store_errors(make_project, runner, tmp_path, monkeypatch):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    _corrupt_store(monkeypatch, tmp_path)
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 2
    assert "credential store" in res.output

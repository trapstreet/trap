from __future__ import annotations

import json

import httpx
import pytest

from trap.auth.client import ApiClient, ApiError
from trap.auth.login import BrowserProvider, TokenProvider
from trap.auth.oauth import OAuthCallbackServer
from trap.auth.resolve import ResolvedAuth
from trap.auth.store import DEFAULT_SERVER, Credential, CredentialStore, CredentialStoreError

UAT = "https://uat.trapstreet.run"

# -- store (one credential per server) -------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch) -> CredentialStore:
    monkeypatch.setattr(CredentialStore, "PATH", tmp_path / "auth.json")
    return CredentialStore()


def test_store_roundtrip_default_server(store):
    assert store.load() is None
    path = store.save(Credential(server=DEFAULT_SERVER, api_key="k", account="alice"))
    assert oct(path.stat().st_mode)[-3:] == "600"
    loaded = store.load()  # no arg → the default-server credential
    assert loaded is not None and loaded.api_key == "k" and loaded.server == DEFAULT_SERVER
    assert loaded.account is None  # the account name is never persisted
    assert "alice" not in path.read_text()  # …and never written to disk


def test_store_credentials_are_independent(store):
    store.save(Credential(server=DEFAULT_SERVER, api_key="prod-key"))
    store.save(Credential(server=UAT, api_key="uat-key"))
    assert store.load().api_key == "prod-key"  # a uat login must not clobber prod
    assert store.load(UAT).api_key == "uat-key"
    assert store.servers() == [DEFAULT_SERVER, UAT]


def test_store_save_updates_existing_credential(store):
    store.save(Credential(server=DEFAULT_SERVER, api_key="old"))
    store.save(Credential(server=DEFAULT_SERVER, api_key="new"))
    assert store.load().api_key == "new"
    assert store.servers() == [DEFAULT_SERVER]


def test_store_server_key_ignores_trailing_slash(store):
    store.save(Credential(server=DEFAULT_SERVER + "/", api_key="k"))
    assert store.load(DEFAULT_SERVER).api_key == "k"
    assert store.load(DEFAULT_SERVER + "/").api_key == "k"
    assert store.servers() == [DEFAULT_SERVER]


def test_store_delete_is_per_server(store, tmp_path):
    store.save(Credential(server=DEFAULT_SERVER, api_key="p"))
    store.save(Credential(server=UAT, api_key="u"))
    assert store.delete(UAT) is True
    assert store.load(UAT) is None
    assert store.load().api_key == "p"  # the other credential is untouched
    assert store.delete(UAT) is False  # already gone
    assert store.delete() is True  # last credential → file removed
    assert not (tmp_path / "auth.json").exists()


def test_store_migrates_legacy_single_object_file(store, tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"server": UAT, "api_key": "legacy", "account": "a"}))
    assert store.load() is None  # the legacy credential was uat, not the default server
    loaded = store.load(UAT)
    assert loaded is not None and loaded.api_key == "legacy"
    assert loaded.account is None  # migration drops the un-persisted account name
    migrated = json.loads((tmp_path / "auth.json").read_text())
    assert migrated["credentials"][UAT] == {"api_key": "legacy"}  # keyed, api_key only
    assert oct((tmp_path / "auth.json").stat().st_mode)[-3:] == "600"


def test_store_legacy_migration_survives_readonly_file(store, tmp_path, monkeypatch):
    # the in-place upgrade is best-effort: if the rewrite fails, the parsed credential is
    # still served for this run.
    (tmp_path / "auth.json").write_text(json.dumps({"server": UAT, "api_key": "legacy"}))

    def failing_write(self, credentials):
        raise OSError("read-only")

    monkeypatch.setattr(CredentialStore, "_write", failing_write)
    loaded = store.load(UAT)
    assert loaded is not None and loaded.api_key == "legacy"


def test_store_corrupt_json_raises(store, tmp_path):
    # a present-but-broken file is corruption, not logged-out — raise, don't return {}.
    (tmp_path / "auth.json").write_text("not json{{{")
    with pytest.raises(CredentialStoreError, match="not valid JSON"):
        store.load()


def test_store_non_dict_json_raises(store, tmp_path):
    (tmp_path / "auth.json").write_text("[1, 2]")  # valid JSON, wrong shape
    with pytest.raises(CredentialStoreError, match="unrecognised format"):
        store.servers()


def test_store_unrecognised_object_raises(store, tmp_path):
    # a dict that is neither the keyed shape nor a legacy Credential
    (tmp_path / "auth.json").write_text(json.dumps({"foo": 1}))
    with pytest.raises(CredentialStoreError, match="unrecognised format"):
        store.load()


def test_store_unreadable_file_raises(store, tmp_path, monkeypatch):
    # a present-but-unreadable file (permissions / IO) is an error, not logged-out.
    (tmp_path / "auth.json").write_text("{}")

    def denied(self, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("pathlib.Path.read_text", denied)
    with pytest.raises(CredentialStoreError, match="cannot read"):
        store.load()


def test_store_invalid_credential_returns_none(store, tmp_path):
    # keyed shape, but this one credential is missing api_key: the file is fine (other
    # servers still load), only this entry is unusable — so load(), not the file, is None.
    (tmp_path / "auth.json").write_text(
        json.dumps({"version": 2, "credentials": {DEFAULT_SERVER: {"account": "a"}}})
    )
    assert store.load() is None
    assert store.servers() == [DEFAULT_SERVER]  # listed, just not loadable


# -- resolve (env overrides + server/token routing) ---------------------------

STORED = Credential(server=DEFAULT_SERVER, api_key="prod-key")


def _store_with(credentials, tmp_path, monkeypatch) -> CredentialStore:
    monkeypatch.setattr(CredentialStore, "PATH", tmp_path / "auth.json")
    s = CredentialStore()
    for data in credentials:
        s.save(data)
    return s


def test_resolve_stored_only(tmp_path, monkeypatch):
    s = _store_with([STORED], tmp_path, monkeypatch)
    r = ResolvedAuth.resolve(s)
    assert (r.server, r.server_source) == (DEFAULT_SERVER, "stored")
    assert (r.api_key, r.api_key_source) == ("prod-key", "stored")


def test_resolve_nothing_defaults(tmp_path, monkeypatch):
    s = _store_with([], tmp_path, monkeypatch)
    r = ResolvedAuth.resolve(s)
    assert (r.server, r.server_source) == (DEFAULT_SERVER, "default")
    assert (r.api_key, r.api_key_source) == (None, None)


def test_resolve_env_overrides_both(tmp_path, monkeypatch):
    s = _store_with([STORED], tmp_path, monkeypatch)
    monkeypatch.setenv("TRAPSTREET_URL", UAT)
    monkeypatch.setenv("TRAPSTREET_API_KEY", "uat-key")
    r = ResolvedAuth.resolve(s)
    assert (r.server, r.server_source) == (UAT, "env")
    assert (r.api_key, r.api_key_source) == ("uat-key", "env")


def test_resolve_env_server_without_credential_is_logged_out(tmp_path, monkeypatch):
    # the prod token can't be borrowed for a server we never paired.
    s = _store_with([STORED], tmp_path, monkeypatch)
    monkeypatch.setenv("TRAPSTREET_URL", UAT)
    r = ResolvedAuth.resolve(s)
    assert (r.server, r.server_source) == (UAT, "env")
    assert (r.api_key, r.api_key_source) == (None, None)


def test_resolve_override_selects_credential(tmp_path, monkeypatch):
    s = _store_with([STORED, Credential(server=UAT, api_key="uat-key")], tmp_path, monkeypatch)
    r = ResolvedAuth.resolve(s, server_override=UAT)
    assert (r.server, r.server_source) == (UAT, "env")
    assert (r.api_key, r.api_key_source) == ("uat-key", "stored")


def test_resolve_env_key_only(tmp_path, monkeypatch):
    s = _store_with([], tmp_path, monkeypatch)
    monkeypatch.setenv("TRAPSTREET_API_KEY", "env-key")
    r = ResolvedAuth.resolve(s)
    assert (r.server, r.server_source) == (DEFAULT_SERVER, "default")
    assert (r.api_key, r.api_key_source) == ("env-key", "env")


def test_resolve_blank_env_is_ignored(tmp_path, monkeypatch):
    s = _store_with([STORED], tmp_path, monkeypatch)
    monkeypatch.setenv("TRAPSTREET_URL", "  ")
    monkeypatch.setenv("TRAPSTREET_API_KEY", "")
    r = ResolvedAuth.resolve(s)
    assert (r.server_source, r.api_key_source) == ("stored", "stored")


# -- client (httpx MockTransport) --------------------------------------------


def _client(handler):
    c = ApiClient("https://srv", "key")
    c.__dict__["_client"] = httpx.Client(
        base_url="https://srv",
        transport=httpx.MockTransport(handler),
        headers={"authorization": "Bearer key"},
    )
    return c


def test_get_me_ok():
    assert (
        _client(lambda r: httpx.Response(200, json={"user": {"name": "x"}})).get_me()["user"]["name"] == "x"
    )


def test_get_me_401():
    with pytest.raises(ApiError, match="invalid"):
        _client(lambda r: httpx.Response(401)).get_me()


def test_get_me_server_error():
    with pytest.raises(ApiError, match="server error"):
        _client(lambda r: httpx.Response(500)).get_me()


def test_get_me_unreachable():
    def boom(r):
        raise httpx.ConnectError("down")

    with pytest.raises(ApiError, match="unreachable"):
        _client(boom).get_me()


def test_submit_ok(tmp_path):
    rp = tmp_path / "r.json"
    rp.write_text("{}")
    out = _client(lambda r: httpx.Response(200, json={"run": {"id": "1"}})).submit(rp)
    assert out["run"]["id"] == "1"


def test_submit_http_error(tmp_path):
    rp = tmp_path / "r.json"
    rp.write_text("{}")
    with pytest.raises(ApiError, match="http 400: bad"):
        _client(lambda r: httpx.Response(400, text="bad")).submit(rp)


def test_submit_http_error_json_message(tmp_path):
    # the server's {error, code} body renders as its message, not raw JSON
    rp = tmp_path / "r.json"
    rp.write_text("{}")
    with pytest.raises(ApiError, match="http 404: task not registered"):
        _client(
            lambda r: httpx.Response(404, json={"error": "task not registered", "code": "not_found"})
        ).submit(rp)


def test_submit_http_error_json_without_message(tmp_path):
    # JSON body with no usable error field falls back to the raw text
    rp = tmp_path / "r.json"
    rp.write_text("{}")
    with pytest.raises(ApiError, match=r'http 500: \{"detail":"x"\}'):
        _client(lambda r: httpx.Response(500, json={"detail": "x"})).submit(rp)


def test_submit_conn_error(tmp_path):
    rp = tmp_path / "r.json"
    rp.write_text("{}")

    def boom(r):
        raise httpx.ConnectError("x")

    with pytest.raises(ApiError, match="connection error"):
        _client(boom).submit(rp)


# -- login providers ----------------------------------------------------------


def test_token_provider():
    p = TokenProvider("https://s", "tok")
    assert p.acquire().api_key == "tok"
    assert "api_key" in p.pre_message
    with pytest.raises(ValueError):
        TokenProvider("https://s", "").acquire()


def test_browser_provider_custom_server_rejected():
    with pytest.raises(ValueError):
        BrowserProvider("https://other", 1)


def test_browser_provider_success(monkeypatch):
    p = BrowserProvider(DEFAULT_SERVER, 1)
    monkeypatch.setattr(p._cb, "run", lambda t: True)
    p._cb._auth_data = Credential(server=DEFAULT_SERVER, api_key="k")
    assert p.acquire().api_key == "k"
    assert "opening" in p.pre_message


def test_browser_provider_timeout(monkeypatch):
    p = BrowserProvider(DEFAULT_SERVER, 1)
    monkeypatch.setattr(p._cb, "run", lambda t: False)
    with pytest.raises(TimeoutError):
        p.acquire()


# -- oauth callback server ----------------------------------------------------


def test_oauth_callback_success(monkeypatch):
    srv = OAuthCallbackServer("https://trapstreet.run/")

    def fake_open(url):
        httpx.get(f"http://127.0.0.1:{srv.port}/callback?api_key=K&account=A")

    monkeypatch.setattr("trap.auth.oauth.webbrowser.open", fake_open)
    assert "/cli/authorize" in srv.auth_url
    assert srv.run(timeout=5) is True
    assert srv.auth_data is not None and srv.auth_data.api_key == "K" and srv.auth_data.account == "A"


def test_oauth_missing_api_key_and_timeout(monkeypatch):
    srv = OAuthCallbackServer("https://x")

    def fake_open(url):
        raise RuntimeError("no browser")  # exercises the swallowed-open branch

    monkeypatch.setattr("trap.auth.oauth.webbrowser.open", fake_open)
    assert srv.run(timeout=1) is False  # nothing ever hits the callback


def test_oauth_rejects_missing_key(monkeypatch):
    srv = OAuthCallbackServer("https://x")

    def fake_open(url):
        resp = httpx.get(f"http://127.0.0.1:{srv.port}/callback")  # no api_key
        assert resp.status_code == 400

    monkeypatch.setattr("trap.auth.oauth.webbrowser.open", fake_open)
    assert srv.run(timeout=1) is False

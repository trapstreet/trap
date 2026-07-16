from __future__ import annotations

import json

import httpx
import pytest

from trap.auth.client import ApiClient, ApiError
from trap.auth.login import BrowserProvider, TokenProvider
from trap.auth.oauth import OAuthCallbackServer
from trap.auth.resolve import AuthMismatchError, resolve_auth
from trap.auth.store import DEFAULT_SERVER, AuthData, AuthStore

# -- store (one profile per server) --------------------------------------------

UAT = "https://uat.trapstreet.run"


@pytest.fixture
def store(tmp_path, monkeypatch) -> AuthStore:
    monkeypatch.setattr(AuthStore, "PATH", tmp_path / "auth.json")
    return AuthStore()


def test_store_roundtrip_default_server(store):
    assert store.load() is None
    path = store.save(AuthData(server=DEFAULT_SERVER, api_key="k", solution="sol"))
    assert oct(path.stat().st_mode)[-3:] == "600"
    loaded = store.load()  # no arg → DEFAULT_SERVER profile
    assert loaded is not None and loaded.api_key == "k" and loaded.solution == "sol"
    assert loaded.server == DEFAULT_SERVER


def test_store_profiles_are_independent(store):
    store.save(AuthData(server=DEFAULT_SERVER, api_key="prod-key"))
    store.save(AuthData(server=UAT, api_key="uat-key"))
    assert store.load().api_key == "prod-key"  # uat login must not clobber prod
    assert store.load(UAT).api_key == "uat-key"
    assert store.servers() == [DEFAULT_SERVER, UAT]


def test_store_save_updates_existing_profile(store):
    store.save(AuthData(server=DEFAULT_SERVER, api_key="old"))
    store.save(AuthData(server=DEFAULT_SERVER, api_key="new"))
    assert store.load().api_key == "new"
    assert store.servers() == [DEFAULT_SERVER]


def test_store_server_url_normalized(store):
    store.save(AuthData(server=DEFAULT_SERVER + "/", api_key="k"))
    assert store.load(DEFAULT_SERVER).api_key == "k"
    assert store.load(DEFAULT_SERVER + "/").api_key == "k"
    assert store.servers() == [DEFAULT_SERVER]


def test_store_delete_per_server(store, tmp_path):
    store.save(AuthData(server=DEFAULT_SERVER, api_key="p"))
    store.save(AuthData(server=UAT, api_key="u"))
    assert store.delete(UAT) is True
    assert store.load(UAT) is None
    assert store.load().api_key == "p"  # other profile untouched
    assert store.delete(UAT) is False  # already gone
    assert store.delete() is True  # last profile removed → file removed
    assert not (tmp_path / "auth.json").exists()


def test_store_migrates_legacy_single_object_file(store, tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({"server": UAT, "api_key": "legacy", "solution": "sol"}))
    assert store.load() is None  # legacy profile was uat, not prod
    loaded = store.load(UAT)
    assert loaded is not None and loaded.api_key == "legacy" and loaded.solution == "sol"
    migrated = json.loads((tmp_path / "auth.json").read_text())
    assert UAT in migrated["profiles"]  # file rewritten in the keyed format
    assert oct((tmp_path / "auth.json").stat().st_mode)[-3:] == "600"


def test_store_corrupt_returns_none(store, tmp_path):
    (tmp_path / "auth.json").write_text("not json{{{")
    assert store.load() is None
    assert store.servers() == []
    assert store.delete() is False


def test_store_non_dict_json_returns_none(store, tmp_path):
    (tmp_path / "auth.json").write_text("[1, 2]")  # valid JSON, wrong shape
    assert store.load() is None
    assert store.servers() == []


def test_store_unrecognized_object_returns_none(store, tmp_path):
    # a dict that is neither the keyed shape nor a legacy AuthData
    (tmp_path / "auth.json").write_text(json.dumps({"foo": 1}))
    assert store.load() is None
    assert store.servers() == []


def test_store_invalid_profile_returns_none(store, tmp_path):
    # keyed shape, but the profile itself is missing api_key
    (tmp_path / "auth.json").write_text(
        json.dumps({"version": 2, "profiles": {DEFAULT_SERVER: {"solution": "sol"}}})
    )
    assert store.load() is None
    assert store.servers() == [DEFAULT_SERVER]  # listed, just not loadable


def test_store_legacy_migration_survives_readonly_file(store, tmp_path, monkeypatch):
    # the in-place upgrade is best-effort: if the rewrite fails, the parsed
    # profile is still served for this run
    (tmp_path / "auth.json").write_text(json.dumps({"server": UAT, "api_key": "legacy"}))

    def failing_write(self, profiles):
        raise OSError("read-only")

    monkeypatch.setattr(AuthStore, "_write", failing_write)
    loaded = store.load(UAT)
    assert loaded is not None and loaded.api_key == "legacy"


# -- resolve (env overrides + server↔token pairing) ---------------------------

STORED = AuthData(server="https://trapstreet.run", api_key="prod-key")


def test_resolve_stored_only():
    r = resolve_auth(STORED)
    assert (r.server, r.server_source) == ("https://trapstreet.run", "stored")
    assert (r.api_key, r.api_key_source) == ("prod-key", "stored")
    r.ensure_paired()  # stored token on its own server — never a mismatch


def test_resolve_nothing_defaults():
    r = resolve_auth(None)
    assert (r.server, r.server_source) == (DEFAULT_SERVER, "default")
    assert (r.api_key, r.api_key_source) == (None, None)
    r.ensure_paired()  # no token — nothing to mispair


def test_resolve_env_overrides_both(monkeypatch):
    monkeypatch.setenv("TRAPSTREET_URL", "https://uat.trapstreet.run")
    monkeypatch.setenv("TRAPSTREET_API_KEY", "uat-key")
    r = resolve_auth(STORED)
    assert (r.server, r.server_source) == ("https://uat.trapstreet.run", "env")
    assert (r.api_key, r.api_key_source) == ("uat-key", "env")
    r.ensure_paired()  # explicit env token is taken as intended for the env server


def test_resolve_mismatch_stored_token_env_server(monkeypatch):
    monkeypatch.setenv("TRAPSTREET_URL", "https://uat.trapstreet.run")
    r = resolve_auth(STORED)
    assert (r.api_key, r.api_key_source) == ("prod-key", "stored")
    with pytest.raises(AuthMismatchError, match=r"https://trapstreet\.run"):
        r.ensure_paired()


def test_resolve_mismatch_message_names_the_fix(monkeypatch):
    monkeypatch.setenv("TRAPSTREET_URL", "https://uat.trapstreet.run")
    with pytest.raises(AuthMismatchError) as exc:
        resolve_auth(STORED).ensure_paired()
    msg = str(exc.value)
    assert "TRAPSTREET_API_KEY" in msg and "tp auth login" in msg
    assert "https://uat.trapstreet.run" in msg


def test_resolve_trailing_slash_is_not_a_mismatch(monkeypatch):
    monkeypatch.setenv("TRAPSTREET_URL", "https://trapstreet.run/")
    resolve_auth(STORED).ensure_paired()


def test_resolve_blank_env_ignored(monkeypatch):
    monkeypatch.setenv("TRAPSTREET_URL", "  ")
    monkeypatch.setenv("TRAPSTREET_API_KEY", "")
    r = resolve_auth(STORED)
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
    p._cb._auth_data = AuthData(server=DEFAULT_SERVER, api_key="k")
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
        httpx.get(f"http://127.0.0.1:{srv.port}/callback?api_key=K&solution=S")

    monkeypatch.setattr("trap.auth.oauth.webbrowser.open", fake_open)
    assert "/cli/authorize" in srv.auth_url
    assert srv.run(timeout=5) is True
    assert srv.auth_data is not None and srv.auth_data.api_key == "K" and srv.auth_data.solution == "S"


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

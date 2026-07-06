from __future__ import annotations

import httpx
import pytest

from trap.auth.client import ApiClient, ApiError
from trap.auth.login import BrowserProvider, TokenProvider
from trap.auth.oauth import OAuthCallbackServer
from trap.auth.store import DEFAULT_SERVER, AuthData, AuthStore

# -- store --------------------------------------------------------------------


def test_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthStore, "PATH", tmp_path / "auth.json")
    store = AuthStore()
    assert store.load() is None and not store.exists
    path = store.save(AuthData(server="https://s", api_key="k", solution="sol"))
    assert oct(path.stat().st_mode)[-3:] == "600"
    loaded = store.load()
    assert loaded is not None and loaded.api_key == "k" and loaded.solution == "sol"
    store.delete()
    assert not store.exists


def test_store_corrupt_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(AuthStore, "PATH", tmp_path / "auth.json")
    (tmp_path / "auth.json").write_text("not json{{{")
    assert AuthStore().load() is None


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
    with pytest.raises(ApiError, match="http 400"):
        _client(lambda r: httpx.Response(400, text="bad")).submit(rp)


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

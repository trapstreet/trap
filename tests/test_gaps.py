"""Targeted tests for the last hard-to-reach branches."""

from __future__ import annotations

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from trap.auth.client import ApiError
from trap.cli import app
from trap.cost.proxy import CostProxy
from trap.git_ops import LocalRepo, ParsedGitUrl, RemoteRepo
from trap.loader import TrapLoader, TraptaskLoader
from trap.models.cost import CaseCost, ModelCost
from trap.runner import TaskRunner
from trap.workspace import Workspace

from .conftest import GRADER_PASS, JUDGE_SCORE


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_solution(path: Path) -> Path:
    """A git repo holding a runnable trap.yaml (local task copied in)."""
    path.mkdir(parents=True)
    # setup_cmd on both sides exercises the post-clone setup branches in the loaders.
    (path / "trap.yaml").write_text(
        json.dumps({"cmd": "sh -c 'echo hi'", "setup_cmd": "true", "tasks": {"t": {"source": "task"}}})
    )
    (path / "task" / "inputs" / "c1").mkdir(parents=True)
    (path / "task" / "inputs" / "c1" / "input.txt").write_text("x")
    (path / "task" / "traptask.yaml").write_text(json.dumps({"cases": [{"id": "c1"}], "setup_cmd": "true"}))
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "c1")
    return path


# -- solution.py cost branches ------------------------------------------------


def test_run_no_cost(make_project, runner):
    make_project(cmd="sh -c 'echo hi'", cases=["c1"])
    assert runner.invoke(app, ["run", "--no-cost", "--no-environment"]).exit_code == 0


def test_run_cost_proxy_construction_swallowed(make_project, runner, monkeypatch):
    make_project(cmd="sh -c 'echo hi'", cases=["c1"])

    class Boom:
        def __init__(self):
            raise RuntimeError("no proxy")

    monkeypatch.setattr("trap.runner.solution.CostProxy", Boom)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0


def test_run_cost_accounted(make_project, runner, monkeypatch):
    make_project(cmd="sh -c 'echo hi'", cases=["c1"])

    class Fake:
        def start(self):
            pass

        @property
        def env_overrides(self):
            return {}

        def stop(self):
            return CaseCost(
                by_model=[
                    ModelCost(
                        provider="openai",
                        model="m",
                        prompt_tokens=5,
                        completion_tokens=3,
                        cost_usd=0.01,
                        calls=1,
                    )
                ]
            )

    monkeypatch.setattr("trap.runner.solution.CostProxy", Fake)
    res = runner.invoke(app, ["run", "-o", "json", "--no-environment"])
    assert json.loads(res.stdout)["cases_results"][0]["cost"]["calls"] == 1


# -- cli run / report / submit edges ------------------------------------------


def test_run_remote_task_source(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "trap.yaml").write_text(
        json.dumps({"cmd": "x", "tasks": {"t": {"source": "git+file:///nope-task"}}})
    )
    res = runner.invoke(app, ["run", "--trust-remote"])
    assert res.exit_code == 2
    assert "git clone failed" in res.output


def test_run_environment_detect_failure(make_project, runner, monkeypatch):
    make_project(cmd="sh -c 'echo hi'", cases=["c1"])

    def boom(self):
        raise RuntimeError("probe down")

    monkeypatch.setattr("trap.cli.EnvironmentDetector.detect", boom)
    assert runner.invoke(app, ["run"]).exit_code == 0  # environment on, swallowed


def test_report_bad_config(runner, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "trap.yaml").write_text("cmd: x\ntasks: {")
    assert runner.invoke(app, ["report"]).exit_code == 2


def test_submit_bad_config(runner, tmp_path, monkeypatch):
    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "trap.yaml").write_text("cmd: x\ntasks: {")
    assert runner.invoke(app, ["submit"]).exit_code == 2


def _passing(make_project, **kw):
    return make_project(
        cmd="sh -c 'cat'",
        stdin="input.txt",
        inputs={"c1": {"input.txt": "hello"}},
        expected={"c1": {"answer.txt": "hello"}},
        judge_src=JUDGE_SCORE,
        grader_src=GRADER_PASS,
        **kw,
    )


def test_submit_no_run(make_project, runner, monkeypatch):
    make_project(cmd="sh -c 'echo hi'", cases=["c1"])
    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 2 and "no completed runs" in res.output


def test_submit_api_error(make_project, runner, monkeypatch):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    monkeypatch.setenv("TRAPSTREET_API_KEY", "k")

    def boom(self, path):
        raise ApiError("server exploded")

    monkeypatch.setattr("trap.auth.client.ApiClient.submit", boom)
    res = runner.invoke(app, ["submit", "--task", "t"])
    assert res.exit_code == 2 and "server exploded" in res.output


def test_submit_uses_stored_credentials(make_project, runner, monkeypatch, tmp_path):
    _passing(make_project)
    assert runner.invoke(app, ["run", "--no-environment"]).exit_code == 0
    monkeypatch.delenv("TRAPSTREET_API_KEY", raising=False)
    monkeypatch.delenv("TRAPSTREET_URL", raising=False)
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", tmp_path / "auth.json")
    # a legacy single-object file: migrated on read, then resolved as the default profile
    (tmp_path / "auth.json").write_text(
        json.dumps({"server": "https://trapstreet.run", "api_key": "stored-key"})
    )
    captured = {}

    def fake_submit(self, path):
        captured["server"], captured["key"] = self._server, self._api_key
        return {"run": {"passed": True}}

    monkeypatch.setattr("trap.auth.client.ApiClient.submit", fake_submit)
    assert runner.invoke(app, ["submit", "--task", "t"]).exit_code == 0
    assert captured == {"server": "https://trapstreet.run", "key": "stored-key"}


# -- TaskRunner: no callbacks; fail-fast ---------------------------------------


def _run_dir(make_project, run_name: str, cmd: str = "sh -c 'echo hi'", cases=("c1",)):
    sol = make_project(cmd=cmd, cases=list(cases))
    run_dir = Workspace((sol / ".trap").resolve(), "sol-key", "t").run_dir(run_name)
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _task_runner(make_project, run_name: str):
    run_dir = _run_dir(make_project, run_name)
    tl = TrapLoader.from_solution(None)
    task = tl.resolve_task(None)
    ttl = TraptaskLoader.from_task_binding(task, tl.trap_dir)
    return TaskRunner(
        trap_config=tl.config,
        trap_dir=tl.trap_dir,
        traptask_config=ttl.traptask,
        traptask_dir=ttl.traptask_dir,
        run_dir=run_dir,
        cost_enabled=False,
    )


def test_taskrunner_without_callbacks(make_project):
    tr = _task_runner(make_project, "ts1")
    results, _ = tr.run(tr.traptask_config.cases)
    assert len(results) == 1


def test_taskrunner_fail_fast(make_project):
    sol = make_project(cmd="sh -c 'exit 1'", cases=["c1", "c2"])
    tl = TrapLoader.from_solution(None)
    task = tl.resolve_task(None)
    ttl = TraptaskLoader.from_task_binding(task, tl.trap_dir)
    run_dir = Workspace((sol / ".trap").resolve(), "sol-key", task.alias).run_dir("ff")
    run_dir.mkdir(parents=True)
    tr = TaskRunner(tl.config, tl.trap_dir, ttl.traptask_dir, ttl.traptask, run_dir, False)
    results, _ = tr.run(ttl.traptask.cases, fail_fast=True)
    assert len(results) == 1  # stopped after the first non-zero exit


# -- cost proxy: accumulate / GET / upstream failure --------------------------


def _fake_upstream(usage_body: bytes):
    class H(BaseHTTPRequestHandler):
        def _send(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(usage_body)))
            self.end_headers()
            self.wfile.write(usage_body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            self.rfile.read(n)
            self._send()

        def do_GET(self):
            self._send()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_proxy_accumulates_and_get(monkeypatch):
    body = json.dumps({"model": "gpt", "usage": {"prompt_tokens": 4, "completion_tokens": 2}}).encode()
    srv, upstream = _fake_upstream(body)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", upstream)
        proxy = CostProxy()
        proxy.start()
        url = proxy.env_overrides["OPENAI_BASE_URL"]
        httpx.post(f"{url}/v1/x", content=b"{}", headers={"content-type": "application/json"})
        httpx.post(
            f"{url}/v1/x", content=b"{}", headers={"content-type": "application/json"}
        )  # same model → accumulate
        httpx.get(f"{url}/v1/models")  # GET path
        cost = proxy.stop()
    finally:
        srv.shutdown()
    openai = next(m for m in cost.by_model if m.provider == "openai")
    assert openai.calls == 3 and openai.prompt_tokens == 12


def test_proxy_skips_zero_usage(monkeypatch):
    body = json.dumps({"model": "gpt", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}).encode()
    srv, upstream = _fake_upstream(body)
    try:
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        monkeypatch.setenv("OPENAI_BASE_URL", upstream)
        proxy = CostProxy()
        proxy.start()
        httpx.post(f"{proxy.env_overrides['OPENAI_BASE_URL']}/v1/x", content=b"{}")
        cost = proxy.stop()
    finally:
        srv.shutdown()
    assert all(m.provider != "openai" for m in cost.by_model)  # zero usage → no bucket


def test_proxy_upstream_down_returns_502(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:1")  # nothing listening
    proxy = CostProxy()
    proxy.start()
    url = proxy.env_overrides["OPENAI_BASE_URL"]
    resp = httpx.post(f"{url}/v1/x", content=b"{}")
    proxy.stop()
    assert resp.status_code == 502


# -- loader remote branches ---------------------------------------------------


def test_from_solution_remote_clones(tmp_path, monkeypatch):
    src = _git_solution(tmp_path / "remote-sol")
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    loader = TrapLoader.from_solution(f"git+file://{src}", allow_remote=True)
    assert loader.config.cmd == "sh -c 'echo hi'"


def test_from_task_remote_clones(tmp_path):
    src = _git_solution(tmp_path / "remote-task")
    from trap.models import TaskBinding

    loader = TraptaskLoader.from_task_binding(
        TaskBinding(alias="t", source=f"git+file://{src}#subdirectory=task"),
        tmp_path / "trapdir",
        workspace_root=tmp_path / "ws",
    )
    assert loader.traptask.cases[0].id == "c1"
    # the clone cache lives inside the workspace root, beside runs/
    assert (tmp_path / "ws" / "repos" / "remote-task").is_dir()
    # clone_to is solution-author config: it anchors to the trap.yaml dir instead
    with_clone_to = TraptaskLoader.from_task_binding(
        TaskBinding(alias="t", source=f"git+file://{src}#subdirectory=task", clone_to=Path("vendored")),
        tmp_path / "trapdir",
        workspace_root=tmp_path / "ws",
    )
    assert with_clone_to.traptask_dir == tmp_path / "trapdir" / "vendored" / "task"


# -- git_ops stragglers -------------------------------------------------------


def test_remote_local_dir_no_subdirectory(tmp_path):
    rr = RemoteRepo(ParsedGitUrl.from_full_url("git+https://x/r"), tmp_path / "root")
    assert rr.local_dir == tmp_path / "root"


def test_provenance_swallows_probe_error(tmp_path, monkeypatch):
    path = tmp_path / "src"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "f").write_text("x")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "c")
    _git(path, "remote", "add", "origin", "https://github.com/o/r.git")
    lr = LocalRepo.open(path)
    monkeypatch.setattr(type(lr.repo), "is_dirty", lambda self, **k: (_ for _ in ()).throw(RuntimeError()))
    assert not lr.provenance().repo


def test_auth_login_browser_default(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", tmp_path / "auth.json")
    from trap.auth.store import DEFAULT_SERVER, Credential

    monkeypatch.setattr(
        "trap.cli._auth.BrowserProvider.acquire",
        lambda self: Credential(server=DEFAULT_SERVER, api_key="k", solution="s"),
    )
    res = runner.invoke(app, ["auth", "login"])
    assert res.exit_code == 0 and "logged in" in res.output


def test_auth_login_empty_token_errors(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", tmp_path / "auth.json")
    res = runner.invoke(app, ["auth", "login", "--with-token"], input="\n")
    assert res.exit_code == 2


def test_auth_status_verify_error(runner, tmp_path, monkeypatch):
    monkeypatch.setattr("trap.auth.store.CredentialStore.PATH", tmp_path / "auth.json")
    (tmp_path / "auth.json").write_text(json.dumps({"server": "https://s", "api_key": "k"}))

    def boom(self):
        raise ApiError("token is invalid")

    monkeypatch.setattr("trap.auth.client.ApiClient.get_me", boom)
    res = runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 1


def test_client_builds_real_session():
    from trap.auth.client import ApiClient

    assert isinstance(ApiClient("https://srv/", "k")._client, httpx.Client)

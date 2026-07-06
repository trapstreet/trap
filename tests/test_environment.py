from __future__ import annotations

import trap.environment.detector as d
from trap.environment import EnvironmentDetector
from trap.models import Environment


def _raise(*a, **k):
    raise RuntimeError("boom")


def test_detect_happy_path():
    env = EnvironmentDetector().detect()
    assert isinstance(env, Environment)
    Environment.model_validate_json(env.model_dump_json())


def test_safe_swallows_probe_failure(monkeypatch):
    monkeypatch.setattr(d.cpuinfo, "get_cpu_info", _raise)
    assert EnvironmentDetector()._get_cpu_model() is None


def test_get_os_linux(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Linux")
    monkeypatch.setattr(d.platform, "freedesktop_os_release", lambda: {"PRETTY_NAME": "TestOS"})
    assert EnvironmentDetector()._get_os() == "TestOS"


def test_get_os_other(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Plan9")
    assert EnvironmentDetector()._get_os() == "Plan9"


def test_get_os_darwin_without_version(monkeypatch):
    monkeypatch.setattr(d.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(d.platform, "mac_ver", lambda: ("", "", ""))
    assert EnvironmentDetector()._get_os() == "macOS"

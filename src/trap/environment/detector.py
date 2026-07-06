# Best-effort detection of the host's runtime environment, recorded in the report
# so runs are comparable across machines. Each probe is decorated with `@_safe`, so
# a failing call degrades that field to None and never aborts the run.
from __future__ import annotations

import functools
import platform
from collections.abc import Callable

import cpuinfo
import psutil

from trap.models.environment import Cpu, Environment


class EnvironmentDetector:
    """Probes the host machine for a fastfetch-like subset of its environment."""

    @staticmethod
    def _safe[**P, T](probe: Callable[P, T]) -> Callable[P, T | None]:
        """Decorate a probe so any failure is swallowed to None — one bad field
        can't sink the whole run."""

        @functools.wraps(probe)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T | None:
            try:
                return probe(*args, **kwargs)
            except Exception:
                return None

        return wrapper

    def detect(self) -> Environment:
        return Environment(
            os=self._get_os(),
            kernel=self._get_kernel(),
            arch=self._get_arch(),
            cpu=Cpu(
                model=self._get_cpu_model(),
                cores_physical=self._get_cpu_cores_physical(),
                cores_logical=self._get_cpu_cores_logical(),
            ),
            memory_total_bytes=self._get_memory_total_bytes(),
        )

    # -- probes: stdlib `platform` ------------------------------------------

    @_safe
    def _get_os(self) -> str | None:
        if platform.system() == "Darwin":
            ver = platform.mac_ver()[0]
            return f"macOS {ver}" if ver else "macOS"
        if platform.system() == "Linux":
            return platform.freedesktop_os_release().get("PRETTY_NAME") or platform.system()
        return platform.system() or None

    @_safe
    def _get_kernel(self) -> str | None:
        return f"{platform.system()} {platform.release()}".strip() or None

    @_safe
    def _get_arch(self) -> str | None:
        return platform.machine() or None

    # -- probes: py-cpuinfo / psutil ----------------------------------------

    @_safe
    def _get_cpu_model(self) -> str | None:
        return cpuinfo.get_cpu_info().get("brand_raw") or None

    @_safe
    def _get_cpu_cores_physical(self) -> int | None:
        return psutil.cpu_count(logical=False)

    @_safe
    def _get_cpu_cores_logical(self) -> int | None:
        return psutil.cpu_count(logical=True)

    @_safe
    def _get_memory_total_bytes(self) -> int | None:
        return psutil.virtual_memory().total

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trap.runner.capture import Capture


@dataclass(frozen=True)
class ProcResult:
    """The normalised outcome of one run. A timeout is folded in here as
    ``exit_code == CapturedSubprocess.TIMEOUT_EXIT_CODE`` (124) with whatever partial
    output we got, so callers never see a raw TimeoutExpired."""

    stdout: str
    stderr: str
    exit_code: int
    duration: float


class CapturedSubprocess:
    """A subprocess trap runs and whose streams it captures to the workspace — the
    solution, a judge, or a grader. On top of ``subprocess.run`` it owns the shared
    lifecycle: assemble the env, run the command (folding a timeout into exit 124 +
    partial output), time it, and persist the capture.

    Two consume methods, neither of which raises (failure is always in-band) — they
    differ because the solution and the judge/grader play opposite roles:

      - ``run()`` (solution) returns the raw ProcResult. The solution is the *subject
        under test*, so a non-zero exit or a timeout is a legitimate outcome to measure
        (a case may even expect exit 1) — not an error. It is handed back verbatim as
        data for the judge to evaluate.
      - ``run_metrics_or_error()`` (judge/grader) returns the actor's parsed JSON
        metrics. A judge/grader is the *measuring apparatus*: if it times out, exits
        non-zero, or emits non-JSON it produced no verdict at all, so that is folded
        into an ``{"error": ...}`` metric (isolated, so one broken judge or grader never
        crashes the run or loses the report).
    """

    # conventional exit code for "a subprocess was killed for exceeding its timeout"
    TIMEOUT_EXIT_CODE = 124

    def __init__(
        self,
        cmd: str,
        *,
        manifest_envvar: str,
        timeout: int,
        cwd: Path,
        manifest: str,
        capture: Capture,
        stdin: str | None = None,
        env_extra: dict[str, str] | None = None,
    ) -> None:
        self.cmd = cmd
        self.manifest_envvar = manifest_envvar
        self.timeout = timeout
        self.cwd = cwd
        self.manifest = manifest
        self.capture = capture
        self.stdin = stdin
        self.env_extra = env_extra or {}

    def run(self) -> ProcResult:
        """Run and capture, returning the raw ProcResult. A timeout is folded in (exit
        124) but never raised — for the solution a failed/timed-out run is a measured
        outcome, so the caller reads ``exit_code`` rather than catching."""
        env = {**os.environ, self.manifest_envvar: self.manifest, **self.env_extra}
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                shlex.split(self.cmd),
                input=self.stdin,
                capture_output=True,
                text=True,
                cwd=self.cwd,
                timeout=self.timeout,
                env=env,
            )
            stdout, stderr, exit_code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            stdout = self._as_text(e.stdout)
            stderr = self._as_text(e.stderr) + f"\n[trap] timed out after {self.timeout}s"
            exit_code = self.TIMEOUT_EXIT_CODE

        duration = time.monotonic() - t0
        self.capture.write(stdout, stderr, {"exit_code": exit_code, "duration": duration})
        return ProcResult(stdout, stderr, exit_code, duration)

    def run_metrics_or_error(self, *, runner_name: str) -> Any:
        """Run the actor and return its parsed JSON metrics. A broken actor — timeout,
        non-zero exit, or non-JSON output — is folded into an ``{"error": ...}`` metric
        rather than raised, so one bad judge/grader never crashes the run or loses the
        report (full detail is in the actor's stderr capture). ``runner_name`` ("judge" /
        "grader") just labels the error."""
        result = self.run()
        if result.exit_code == self.TIMEOUT_EXIT_CODE:
            return {"error": f"{runner_name} timed out after {self.timeout}s"}
        if result.exit_code != 0:
            return {"error": f"{runner_name} exited with status {result.exit_code}"}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            return {"error": f"{runner_name} produced invalid JSON: {e}"}

    @staticmethod
    def _as_text(out: object) -> str:
        """Partial stdout/stderr off a TimeoutExpired may be a bytes-like buffer (or
        absent) even with text=True; normalise whatever we got to a str."""
        if isinstance(out, str):
            return out
        if isinstance(out, (bytes, bytearray, memoryview)):
            return bytes(out).decode(errors="replace")
        return ""

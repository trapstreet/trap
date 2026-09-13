"""What every shape does around its one case: a work directory holding a copy of the
case's inputs, an environment with the task checkout scrubbed out of it, a deadline, and
the exit codes a shape reports through.

A shape is a solution under trap's IO contract (docs/reference/io-contract.md): the
runner starts it with ``TRAP_MANIFEST`` set, keeps its stdout as the answer and its exit
code as the case's."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from types import FrameType
from typing import NoReturn


class ShapeExit(IntEnum):
    """How a shape ends. TIMEOUT is the runner's own 124, so a shape that stopped at its
    deadline reads like a solution the runner killed; the rest stay clear of 124-127
    (timeout, judge-without-JSON, and the shell's two)."""

    OK = 0
    REFUSAL = 20
    MAX_TOKENS = 21
    MAX_TURNS = 22
    AGENT_ERROR = 23
    CONFIG_ERROR = 24
    TIMEOUT = 124


class ShapeError(Exception):
    """The shape cannot produce an answer; ``code`` is what it exits with."""

    def __init__(self, code: ShapeExit, message: str) -> None:
        super().__init__(message)
        self.code = code


def fail(error: ShapeError) -> int:
    """Say why on stderr (the runner keeps it with the case) and return the exit code."""
    print(f"[trap] {error}", file=sys.stderr)
    return int(error.code)


def print_answer(text: str) -> None:
    """Print a case's answer, replacing whatever character stdout cannot encode instead
    of raising. A reply is trap's to relay, not to sanitize — but a lone UTF-16 surrogate
    (what ``json.loads`` turns a JSON escape like ``"\\ud800"`` into) has no encoding on a
    real stream, and one such character must not cost the case its answer and its exit
    code both."""
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


class Deadline:
    """The case's wall-clock budget. A shape stops itself before the runner's timeout so it
    can take its children down too: the runner kills only its direct child, and an agent
    left behind keeps running and spending."""

    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self._end - time.monotonic())


#: Files macOS Finder and Windows Explorer write into any folder they show.
OS_JUNK = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})

#: A refusal names at most this many links before falling back to "and N more" — a case
#: gone wrong in bulk doesn't need every path spelled out to be diagnosable.
MAX_NAMED_SYMLINKS = 5


def _symlinks_under(root: Path) -> list[str]:
    """Every symlink under ``root`` — a file, a directory, or one that resolves to
    nothing — as a sorted POSIX path relative to ``root``. Walked with
    ``followlinks=False`` so a symlinked directory is reported and never descended
    into; nothing a link points at is ever read to produce this list."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            entry = Path(dirpath, name)
            if entry.is_symlink():
                found.append(entry.relative_to(root).as_posix())
    return sorted(found)


def _refuse_symlinks(paths: Sequence[str]) -> NoReturn:
    """A case whose inputs hold any symlink is refused outright, wherever it points:
    even a link that resolves inside the case's own inputs is one a task author could
    just as easily have pointed at ``expected/`` instead, and a shape has no way to
    tell the two apart from here — so none are ever copied through."""
    shown = list(paths[:MAX_NAMED_SYMLINKS])
    remaining = len(paths) - len(shown)
    if remaining > 0:
        shown.append(f"and {remaining} more")
    raise ShapeError(
        ShapeExit.CONFIG_ERROR,
        "this case's inputs contain symlinks — a shape never copies them, because a link "
        f"can reach the answers: {', '.join(shown)}",
    )


@dataclass
class CaseSandbox:
    """The case's inputs, copied into a fresh directory outside the task checkout and
    outside ``.trap/``. Task prompts say the files are "in the current directory"; a work
    directory beside ``expected/`` would be one ``ls ..`` from the answers."""

    inputs_dir: Path
    prompt_file: str
    workdir: Path

    @classmethod
    def open(cls, *, manifest_envvar: str, prompt_file: str, environ: Mapping[str, str]) -> CaseSandbox:
        raw = environ.get(manifest_envvar)
        if not raw:
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"${manifest_envvar} is not set — run this shape under `tp run`"
            )
        try:
            inputs_dir = Path(json.loads(raw)["inputs_dir"])
        except (ValueError, KeyError, TypeError) as e:
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"${manifest_envvar} is not a trap manifest ({e})"
            ) from None
        if inputs_dir.is_symlink():
            # Only a hand-made manifest reaches this — the runner always resolves
            # inputs_dir first — but nothing stops one from pointing straight at
            # expected/, and copytree below would silently walk through it: caught
            # here, before a work directory even exists to clean up. inputs_dir has
            # no path relative to itself to name, so this gets its own message
            # rather than _refuse_symlinks', which names paths under it.
            raise ShapeError(
                ShapeExit.CONFIG_ERROR,
                f"this case's inputs ({inputs_dir}) is itself a symlink — a shape never copies "
                "inputs through a link, because it can reach the answers",
            )
        if not (inputs_dir / prompt_file).is_file():
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"this case has no {prompt_file} (looked in {inputs_dir})"
            )
        workdir = Path(tempfile.mkdtemp(prefix="trap-case-")).resolve()
        try:
            # symlinks=True so a link is recreated as a link, never dereferenced into
            # the answer it names; the walk below then decides from what actually
            # landed in the work directory, which is what the child can see.
            shutil.copytree(inputs_dir, workdir, symlinks=True, dirs_exist_ok=True)
        except OSError as e:  # shutil.Error too: an unreadable file, one that vanishes mid-copy, ...
            shutil.rmtree(workdir, ignore_errors=True)
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"cannot copy this case's inputs from {inputs_dir}: {e}"
            ) from None
        # The copy mirrors inputs_dir's layout exactly, so a path found here is the
        # same path relative to inputs_dir — this is what a shape is about to hand a
        # program or agent, named the way the case author would recognise it.
        if links := _symlinks_under(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
            _refuse_symlinks(links)
        return cls(inputs_dir=inputs_dir, prompt_file=prompt_file, workdir=workdir)

    @property
    def prompt_path(self) -> Path:
        return self.workdir / self.prompt_file

    @property
    def question(self) -> str:
        """The question, read as UTF-8 whatever the locale says — a task's files are."""
        try:
            return self.prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"cannot read {self.prompt_file} as UTF-8 text: {e}"
            ) from None

    def extra_inputs(self) -> list[str]:
        """The case's input files other than the prompt, relative to its directory. The
        files a file browser leaves behind (OS_JUNK) are not inputs: a task folder
        opened once in Finder must not read as a case with files."""
        prompt = Path(self.prompt_file)
        return sorted(
            p.relative_to(self.inputs_dir).as_posix()
            for p in self.inputs_dir.rglob("*")
            if p.is_file() and p.name not in OS_JUNK and p.relative_to(self.inputs_dir) != prompt
        )

    def close(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


#: Never scrubbed by value: a child cannot start without them, and a broad ``--scrub`` (a
#: repo root that also holds a venv) must not take PATH with it. HOME LOGNAME PATH SHELL
#: TERM USER is the ACP/MCP SDKs' default inherited set; TMPDIR and LANG are added here.
ESSENTIAL_ENV = frozenset({"PATH", "HOME", "TMPDIR", "LANG", "SHELL", "TERM", "USER", "LOGNAME"})


def scrubbed_env(
    environ: Mapping[str, str], *, manifest_envvar: str, prefixes: Sequence[Path]
) -> dict[str, str]:
    """``environ`` minus the manifest and every variable whose value names one of
    ``prefixes`` (ESSENTIAL_ENV excepted). The manifest points at inputs/ and the answers
    sit beside it: a session that inherited it once read expected/ in one step
    (session_memory_recall's README). Matched by value too, because a task can rename the
    manifest variable."""
    names = {"TRAP_MANIFEST", manifest_envvar}
    needles = [str(Path(p).resolve()) for p in prefixes]
    return {
        k: v
        for k, v in environ.items()
        if k not in names and (k in ESSENTIAL_ENV or not any(n in v for n in needles))
    }


class ShapeParser(argparse.ArgumentParser):
    """An ``argparse.ArgumentParser`` for a shape's own CLI. Bad arguments are a config
    error like any other the shape can hit — a missing ``--agent-cmd`` should exit 24 like
    every other config problem, not argparse's own 2, so the runner and a human reading
    exit codes see one config-error code everywhere."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(int(ShapeExit.CONFIG_ERROR), f"[trap] {self.prog}: {message}\n")


def add_case_args(parser: argparse.ArgumentParser) -> None:
    """The arguments every shape takes about its case."""
    parser.add_argument("--prompt-file", default="question.txt", help="the case file holding the question")
    parser.add_argument(
        "--manifest-envvar", default="TRAP_MANIFEST", help="trap.yaml's manifest_envvar, if it changes it"
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=570.0,
        help="seconds this shape allows itself per case; keep it below trap.yaml's timeout",
    )
    parser.add_argument(
        "--scrub",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="also drop env vars whose value names PATH (e.g. the task checkout); repeatable",
    )


def open_case(
    args: argparse.Namespace, environ: Mapping[str, str] | None = None
) -> tuple[CaseSandbox, dict[str, str]]:
    """The case's sandbox and the environment its child may see."""
    environ = os.environ if environ is None else environ
    sandbox = CaseSandbox.open(
        manifest_envvar=args.manifest_envvar, prompt_file=args.prompt_file, environ=environ
    )
    env = scrubbed_env(
        environ, manifest_envvar=args.manifest_envvar, prefixes=[sandbox.inputs_dir.parent, *args.scrub]
    )
    return sandbox, env


def kill_now(pgid: int) -> None:
    """SIGKILL the process group ``pgid`` at once: one system call and no waiting, so it
    is fit for a signal handler. A group already gone needs nothing — nor one left with
    only unreaped children, which macOS refuses to signal (EPERM)."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_group(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Take down ``proc`` and everything it started — it was spawned as a session leader,
    so its pid is the group id. TERM, a moment to exit, then KILL whatever is left."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):  # gone, or only unreaped (see kill_now)
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


@contextmanager
def kill_on_interrupt(pgid: int) -> Iterator[None]:
    """While the body runs, SIGINT or SIGTERM SIGKILLs process group ``pgid`` right away
    and then ends the shape with ``SystemExit(128 + signal)``.

    Right away, in the handler, because nothing else would: the group runs in its own
    session, so the terminal's Ctrl-C never reaches it, and under ``tp run`` the runner
    SIGKILLs the shape itself 0.25 s after a Ctrl-C — too soon for kill_group's grace
    period. SystemExit, because it still runs every ``finally`` on the way out (the work
    directory's removal among them) and ends the process with the status a shell reports
    for the signal, without a traceback.

    Only the main thread can own signal handlers; anywhere else this does nothing. The
    handlers it replaced come back when the body ends, however it ends."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def on_interrupt(signum: int, frame: FrameType | None) -> NoReturn:
        kill_now(pgid)
        raise SystemExit(128 + signum)

    previous = {sig: signal.signal(sig, on_interrupt) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            # None: the old handler was not set from Python and cannot be put back as it was.
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def run_group(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None, deadline: Deadline
) -> tuple[str, str, int]:
    """Run ``argv`` in its own process group and collect its output. At the deadline the
    group is killed and the exit code is TIMEOUT, with whatever output there was.

    Output that is not valid text is decoded with the bad bytes replaced, not refused: a
    program's stray byte must not cost the case its answer, and passing raw bytes on would
    only move the decoding failure into the runner, which reads this shape's stdout as text."""
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    with kill_on_interrupt(proc.pid):
        try:
            out, err = proc.communicate(input=stdin, timeout=deadline.remaining())
        except subprocess.TimeoutExpired:
            kill_group(proc)
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return (
                out or "",
                (err or "") + "\n[trap] deadline reached; process group killed\n",
                int(ShapeExit.TIMEOUT),
            )
        except BaseException:
            # An interrupt, or anything else unwinding through here: no one will read the
            # output, so the group goes at once rather than after kill_group's grace.
            kill_now(proc.pid)
            proc.wait()
            raise
    return out, err, proc.returncode

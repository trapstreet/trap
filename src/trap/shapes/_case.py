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
from typing import Any, NoReturn


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


def _secure_and_verify(dst: Path) -> list[str]:
    """Walk an already-copied tree, giving the owner read/write/execute on every real
    directory found — on top of whatever ``copystat`` gave it from the inputs, so a
    read-only input directory (0555, say) does not stop the copy being written into or
    later removed — and collecting any symlink still there.

    ``copy_tree_without_symlinks``'s ``ignore`` callback should have kept every link out
    of ``dst`` in the first place, so finding one here means it slipped past that check
    somehow; ``symlinks=True`` stays a backstop for exactly that case, and a symlink
    found is reported, never chmod'd through (that would reach through it to whatever it
    names).

    Walked with ``followlinks=False`` and an ``onerror`` that re-raises, so a directory
    the walk cannot read (however that happened) surfaces as an error rather than being
    silently skipped — the default `os.walk` swallows a `listdir` failure and simply
    does not recurse into it, which would let a copy this function cannot fully inspect
    be reported as symlink-free.

    Returns every symlink found, as POSIX paths relative to ``dst``."""
    links: list[str] = []

    def _raise(err: OSError) -> NoReturn:
        raise err

    for dirpath, dirnames, filenames in os.walk(dst, followlinks=False, onerror=_raise):
        for name in dirnames:
            entry = os.path.join(dirpath, name)
            if os.path.islink(entry):
                links.append(Path(entry).relative_to(dst).as_posix())
            else:
                os.chmod(entry, os.stat(entry).st_mode | 0o700)
        for name in filenames:
            entry = os.path.join(dirpath, name)
            if os.path.islink(entry):
                links.append(Path(entry).relative_to(dst).as_posix())
    return links


def copy_tree_without_symlinks(src: Path, dst: Path) -> list[str]:
    """Copy every file and directory under ``src`` into ``dst``, never creating a
    symlink there — used for a case's inputs and for ``--skill``.

    The ``copytree`` ``ignore`` callback records each entry ``os.path.islink`` reports
    and drops it from the copy, whatever it names: even one that resolves inside ``src``
    itself is left out, because nothing a link points at is ever read to decide. This
    also closes a theoretical write-through: on a case-insensitive work directory, a
    link ``DATA -> X`` alongside an input directory ``data/`` would otherwise make
    copytree write *into* ``X`` when it reached the second name. ``symlinks=True`` stays
    as a backstop, so a link that lands anyway is recreated as a link, never
    dereferenced — see ``_secure_and_verify``, which checks for exactly that.

    Every directory copytree creates — ``dst`` itself included — gets the owner's
    read/write/execute bits added on top of whatever ``copystat`` gave it from ``src``.

    Returns every symlink found — file, directory, or one that resolves to nothing — as
    POSIX paths relative to ``src`` (``dst`` mirrors ``src``'s layout, so the two name
    the same paths), sorted. Raises ``OSError`` if the copy, or verifying it, fails."""
    links: list[str] = []

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        skip = set()
        for name in names:
            if os.path.islink(os.path.join(dirpath, name)):
                skip.add(name)
                links.append(Path(dirpath, name).relative_to(src).as_posix())
        return skip

    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, ignore=_ignore)
    os.chmod(dst, os.stat(dst).st_mode | 0o700)
    links.extend(_secure_and_verify(dst))
    return sorted(set(links))


def refuse_symlinks(clause: str, paths: Sequence[str]) -> NoReturn:
    """A case or a skill whose copy would include a symlink is refused outright,
    wherever it points: even a link that resolves inside its own source is one a task
    author could just as easily have pointed at ``expected/`` instead, and a shape has
    no way to tell the two apart from here — so none are ever copied through.

    ``clause`` names what held them (e.g. "this case's inputs contain symlinks");
    ``paths`` are named relative to that source, capped at MAX_NAMED_SYMLINKS — a case
    gone wrong in bulk doesn't need every path spelled out to be diagnosable — with the
    rest only counted, and file contents are never shown."""
    shown = list(paths[:MAX_NAMED_SYMLINKS])
    remaining = len(paths) - len(shown)
    if remaining > 0:
        shown.append(f"and {remaining} more")
    raise ShapeError(
        ShapeExit.CONFIG_ERROR,
        f"{clause} — a shape never copies them, because a link can reach the answers: {', '.join(shown)}",
    )


def _reclaim_and_retry(func: Any, path: str, exc: BaseException) -> None:
    """``remove_tree``'s ``onexc`` handler: a directory copied read-only from the
    inputs, or one a child made read-only while the case ran, blocks ``func`` on its
    parent's write bit. Reclaim it there and on ``path`` itself, then retry once. A
    path already gone — nothing left to remove — needs no retry."""
    if not os.path.lexists(path):
        return
    parent = os.path.dirname(path)
    os.chmod(parent, os.stat(parent).st_mode | 0o700)
    os.chmod(path, os.stat(path).st_mode | 0o700)
    func(path)


def remove_tree(path: Path) -> None:
    """Remove ``path`` and everything under it, reclaiming the owner's write bit
    wherever it is missing instead of leaving a ``trap-case-*`` directory (or a
    partially installed skill) behind. A path already gone is not an error."""
    shutil.rmtree(path, onexc=_reclaim_and_retry)


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
            # The runner always hands a shape a resolved inputs_dir — a case directory
            # that is itself a link is followed by the runner before a shape ever sees
            # it — so only a hand-made manifest reaches this check. Nothing stops one
            # from pointing straight at expected/, though, and copytree below would
            # silently walk through it: caught here, before a work directory even
            # exists to clean up. inputs_dir has no path relative to itself to name, so
            # this gets its own message rather than refuse_symlinks', which names
            # paths under it.
            raise ShapeError(
                ShapeExit.CONFIG_ERROR,
                f"this case's inputs ({inputs_dir}) is itself a symlink — a shape never copies "
                "inputs through a link, because it can reach the answers",
            )
        prompt_path_rel = Path(prompt_file)
        if prompt_path_rel.is_absolute() or ".." in prompt_path_rel.parts:
            # inputs_dir / prompt_file silently escapes inputs_dir for either shape
            # (an absolute right-hand side replaces the left entirely; ".." walks back
            # out) — refused here so the existence check below and the later read from
            # the work directory can never disagree about which file they mean.
            raise ShapeError(
                ShapeExit.CONFIG_ERROR,
                f"--prompt-file must be a plain relative path inside the case, got {prompt_file!r}",
            )
        if not (inputs_dir / prompt_file).is_file():
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"this case has no {prompt_file} (looked in {inputs_dir})"
            )
        workdir = Path(tempfile.mkdtemp(prefix="trap-case-")).resolve()
        try:
            links = copy_tree_without_symlinks(inputs_dir, workdir)
        except OSError as e:  # shutil.Error too: an unreadable file, one that vanishes mid-copy, ...
            remove_tree(workdir)
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"cannot copy this case's inputs from {inputs_dir}: {e}"
            ) from None
        # The work directory is this case's own, not shared with any other — private to
        # whoever runs `tp run`, however the inputs themselves were shared on disk.
        os.chmod(workdir, 0o700)
        # The copy mirrors inputs_dir's layout exactly, so a path found here is the
        # same path relative to inputs_dir — this is what a shape is about to hand a
        # program or agent, named the way the case author would recognise it.
        if links:
            remove_tree(workdir)
            refuse_symlinks("this case's inputs contain symlinks", links)
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
        remove_tree(self.workdir)


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

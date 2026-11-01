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
import stat
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

from trap.models.card import SolutionCard, canonical_card_json


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


#: One line on stderr, machine-readable: `tp run` reads it back after the case and
#: records it; a person reading the log sees the same facts, prefixed so they read past it.
CARD_PREFIX = "[trap] card "


def _desurrogate(value: object) -> object:
    """``value`` with any lone UTF-16 surrogate character (see ``print_answer``)
    replaced -- recursing into a dict's values, since ``SolutionCard.options`` is one.
    Anything else (an int, ``None``, ...) is returned as-is."""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, dict):
        return {_desurrogate(k): _desurrogate(v) for k, v in value.items()}
    return value


def print_card(card: SolutionCard) -> None:
    """Say, on stderr, what this run actually was. The shape is the only party that knows
    -- which agent build answered the handshake, which options the model accepted, which
    template was expanded -- so it states those labels as one line for `tp run` to record.

    A label built from argv (a non-UTF-8 byte in --template, --setup or a --skill path,
    decoded with surrogateescape) or from an agent's own report (agentInfo.name) can
    carry a lone UTF-16 surrogate the same way a case's answer can -- canonical_card_json's
    own ``.encode("utf-8")`` raises on one, and the card must not cost the case its answer
    and exit code over one unprintable label (print_card runs before the answer is
    printed). A card is trap's to relay, not to sanitize, so the replacement is tried only
    once the plain form fails -- trap.models.card is shared with a future Rust/TS
    implementation and is never touched here."""
    try:
        body = canonical_card_json(card)
    except UnicodeEncodeError:
        clean = SolutionCard.model_validate({k: _desurrogate(v) for k, v in card.model_dump().items()})
        body = canonical_card_json(clean)
    print(CARD_PREFIX + body.decode(), file=sys.stderr)


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


def _linked_file(link: str, root: str | None) -> str | None:
    """The regular file ``link`` resolves to when that file lies under ``root``; None when
    there is no ``root``, or the link resolves to anything else — a directory, nothing, a
    loop, a file outside ``root``."""
    if root is None:
        return None
    target = os.path.realpath(link)
    try:
        regular = stat.S_ISREG(os.lstat(target).st_mode)
    except OSError:
        return None
    return target if regular and Path(target).is_relative_to(root) else None


def _copy_linked_file(target: str, dest: Path) -> None:
    """Write ``target``'s contents and mode to ``dest`` as a new regular file. ``dest``
    must not exist yet, so a name the copy already holds (another spelling of it, on a
    case-insensitive work directory) fails the copy instead of being written through."""
    with open(target, "rb") as source, open(dest, "xb") as copy:
        shutil.copyfileobj(source, copy)
    shutil.copystat(target, dest)


def copy_tree_without_symlinks(src: Path, dst: Path, *, file_links_under: Path | None = None) -> list[str]:
    """Copy every file and directory under ``src`` into ``dst``, never creating a
    symlink there — used for a case's inputs and for ``--skill``.

    The ``copytree`` ``ignore`` callback drops each entry ``os.path.islink`` reports from
    the copy. Given ``file_links_under`` (a case's inputs are), a link that resolves to a
    regular file under that directory — one file many cases share — is then written into
    ``dst`` as a regular file holding that file's contents. Every other link is recorded,
    whatever it names: without ``file_links_under`` (``--skill``) even one that resolves
    inside ``src`` itself. Dropping links from copytree also closes a theoretical
    write-through: on a case-insensitive work directory, a link ``DATA -> X`` alongside an
    input directory ``data/`` would otherwise make copytree write *into* ``X`` when it
    reached the second name; a linked file is written only to a name not yet taken.
    ``symlinks=True`` stays as a backstop, so a link that lands anyway is recreated as a
    link, never dereferenced — see ``_secure_and_verify``, which checks for exactly that,
    again after any linked file is written.

    ``dst`` is private to the owner (exactly 0700) from the moment copytree returns,
    before anything walks the copy: copystat has just given it ``src``'s mode, which may
    be open to everyone. Every directory copytree created below it gets the owner's
    read/write/execute bits added on top of whatever ``copystat`` gave it from ``src``,
    before a linked file is written into it.

    Returns every symlink recorded or found — file, directory, or one that resolves to
    nothing — as POSIX paths relative to ``src`` (``dst`` mirrors ``src``'s layout, so the
    two name the same paths), sorted; linked files are written only when there is none.
    Raises ``OSError`` if the copy, or verifying it, fails."""
    links: list[str] = []
    files: list[tuple[str, str]] = []
    root = None if file_links_under is None else os.path.realpath(file_links_under)

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        skip = set()
        for name in names:
            entry = os.path.join(dirpath, name)
            if os.path.islink(entry):
                skip.add(name)
                path = Path(entry).relative_to(src).as_posix()
                if (target := _linked_file(entry, root)) is None:
                    links.append(path)
                else:
                    files.append((target, path))
        return skip

    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True, ignore=_ignore)
    os.chmod(dst, 0o700)
    links.extend(_secure_and_verify(dst))
    if files and not links:
        for target, path in files:
            _copy_linked_file(target, dst / path)
        links.extend(_secure_and_verify(dst))
    return sorted(set(links))


def refuse_symlinks(clause: str, paths: Sequence[str]) -> NoReturn:
    """A case or a skill whose copy would include a symlink is refused outright. In a
    case's inputs a link to a file under the directory that holds the case is copied as a
    plain file; any other link there, and any link at all in a skill, is one a task author
    could just as easily have pointed at ``expected/``, and a shape has no way to tell the
    two apart from here — so none are ever copied through.

    ``clause`` names what held them (e.g. "this skill contains symlinks");
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


def _open_up_directories(root: str) -> None:
    """Add the owner's read/write/execute to ``root`` and to every real directory under
    it, each one before it is listed — so a directory left 000, 444 or 555 can be listed
    and emptied. Walked top-down with ``lstat``: a symlink, and anything reached through
    one, is never chmod'd, and nothing above ``root`` is ever touched. Files keep their
    modes: removing one needs only its directory's write bit. A directory that cannot be
    opened up is skipped, left for the removal that follows to report."""
    pending = [root]
    while pending:
        path = pending.pop()
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                os.chmod(path, stat.S_IMODE(mode) | 0o700)
                with os.scandir(path) as entries:
                    pending.extend(e.path for e in entries if e.is_dir(follow_symlinks=False))
        except OSError:
            continue


def remove_tree(path: Path, *, what: str = "the work dir") -> None:
    """Remove ``path`` and everything under it as far as possible, and never raise: a
    shape prints its answer after removing the work dir, and a cleanup that fails must
    not cost the case its answer and exit code, nor turn a refusal into some other
    error. Whatever is left is named in one ``[trap] could not remove …`` line on stderr,
    naming the exact path of the first failure — the full path, not just the bare entry
    name a descriptor-based unlink reports for one nested under ``path``; a path already
    gone needs nothing.

    "Left behind" is decided by ``os.lstat(root)``, not ``os.path.lexists``: lexists
    reports False on *any* lstat error, not only "gone", so a root whose parent went
    unsearchable mid-run (a program's own ``chmod 000 ..``, say) would read as removed
    and the warning would never fire even though the directory is still sitting there.
    Only ``FileNotFoundError`` means gone; every other lstat error means "maybe left
    behind" and is reported too, with that error standing in as the reason when nothing
    more specific was recorded.

    Runs only once nothing else can write under ``path``: a shape removes its work dir
    only after ``run_group`` — which kills the child's whole process group before
    returning on every path, a normal exit included, not only the deadline or an
    interrupt — or the ACP connection's ``close()`` has done the same, so no process of
    the case is left to swap a directory for a link between the ``lstat`` that vets it
    and the ``chmod`` that follows."""
    root = os.fspath(path)
    _open_up_directories(root)
    errors: list[tuple[str, BaseException]] = []
    try:
        # Each failure is recorded and skipped, never retried — ignore_errors=True, but
        # keeping (path, reason) for every one: shutil hands onexc the full path it
        # failed on even for an operation whose own exception names only the bare entry.
        shutil.rmtree(root, onexc=lambda func, failed, exc: errors.append((failed, exc)))
    except Exception as e:  # rmtree hands every OSError to onexc; nothing else may escape either
        errors.append((root, e))
    try:
        os.lstat(root)
    except FileNotFoundError:
        return
    except OSError as e:
        if not errors:
            errors.append((root, e))
    if errors:
        failing_path, exc = errors[0]
        reason: object = exc if failing_path == root else f"{failing_path}: {exc}"
    else:
        reason = "reason unknown"
    print(f"[trap] could not remove {what} {root}: {reason}", file=sys.stderr)


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
            # The runner refuses a task whose case directory is a link or is reached
            # through one, and hands a shape a resolved inputs_dir, so only a hand-made
            # manifest reaches this check. Nothing stops one from pointing straight at
            # expected/, though, and copytree below would silently walk through it:
            # caught here, before a work directory even exists to clean up. inputs_dir
            # has no path relative to itself to name, so this gets its own message
            # rather than refuse_symlinks', which names paths under it.
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
        # A link to a file shared by many cases is copied as that file. The inputs root and
        # the answers are the runner's to know, and it has already refused a link that
        # reaches the answers; the directory holding the case is the widest bound a shape
        # can see, and the one a hand-made manifest is held to.
        shared = Path(os.path.realpath(inputs_dir.parent))
        workdir = Path(tempfile.mkdtemp(prefix="trap-case-")).resolve()
        try:
            # Leaves the work directory private (0700) to whoever runs `tp run` — this
            # case's own, however the inputs themselves were shared on disk.
            links = copy_tree_without_symlinks(inputs_dir, workdir, file_links_under=shared)
        except OSError as e:  # shutil.Error too: an unreadable file, one that vanishes mid-copy, ...
            remove_tree(workdir)
            raise ShapeError(
                ShapeExit.CONFIG_ERROR, f"cannot copy this case's inputs from {inputs_dir}: {e}"
            ) from None
        # The copy mirrors inputs_dir's layout exactly, so a path found here is the
        # same path relative to inputs_dir — this is what a shape is about to hand a
        # program or agent, named the way the case author would recognise it.
        if links:
            remove_tree(workdir)
            refuse_symlinks(
                f"this case's inputs contain symlinks other than links to a file in {shared}", links
            )
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

    Whichever way this returns, the whole group is dead first: on a normal exit,
    anything ``argv`` backgrounded and left running (a stray ``sleep &``, say) is
    SIGKILLed along with it, so nothing of this case outlives its shape into cleanup.

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
        # argv exited on its own, but its process group may not have: a backgrounded
        # grandchild (output redirected away, say) doesn't make proc.communicate() wait
        # for it. Killed here, before the work dir removal that follows in every shape,
        # so nothing of this case is still running to race that removal.
        kill_now(proc.pid)
    return out, err, proc.returncode

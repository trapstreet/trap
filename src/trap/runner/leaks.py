"""Refuse a task that could hand a solution the answers, before any case runs.

A solution is handed its case's inputs directory, resolved, and nothing downstream can tell
a link or an answers directory in it from an ordinary file. So: nothing a solution is
handed may be a symlink, from the inputs root down to and inside each running case's
inputs (the inputs root itself may be one); no answers directory — the expected root's or
any defined case's — may lie inside a running case's inputs, and no case's answers
directory may lie around a running case's directory; and case ids stay inside their
directories. Directories are compared as (device, inode), which sees through a different
letter case or Unicode spelling of one directory, and through a firmlink. A hard link or a
copy of an answer inside the inputs is an ordinary file and is the task author's to avoid."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from functools import cache
from pathlib import Path, PurePath
from typing import NoReturn

from trap.errors import ConfigError
from trap.models import TraptaskCase, TraptaskConfig

#: A refusal names at most this many cases and counts the rest.
MAX_NAMED_CASES = 5

Identity = tuple[int, int]


def _identity(path: Path) -> Identity | None:
    """What ``path`` names on disk, as (device, inode), or None when it can't be stat'ed."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


def _is_link(path: str | Path) -> bool:
    """By lstat, which raises for a path it can't look at, rather than reporting no link."""
    return stat.S_ISLNK(os.lstat(path).st_mode)


def _raise(error: OSError) -> NoReturn:
    raise error


def _walk_inputs(inputs_root: Path, case: str, held: dict[Identity, str]) -> str | None:
    """Why ``case``'s inputs can't be handed over — a symlink, or something that can't be
    read — or None, recording every directory in them in ``held``. A case with no inputs
    directory hands nothing over."""
    path = inputs_root
    try:
        for part in PurePath(case).parts:
            path = path / part
            if _is_link(path):
                return f"inputs {path} is a symlink"
        for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
            st = os.stat(dirpath)
            held.setdefault((st.st_dev, st.st_ino), case)
            for name in sorted(dirnames + filenames):
                if _is_link(entry := os.path.join(dirpath, name)):
                    return f"inputs {entry} is a symlink"
    except FileNotFoundError:
        return None
    except OSError as e:
        return f"inputs {e.filename} cannot be read ({e.strerror})"
    return None


def refuse_answer_leaks(
    traptask_dir: Path, traptask_config: TraptaskConfig, cases: Iterable[TraptaskCase]
) -> None:
    """Raise ConfigError when the task could hand a solution running ``cases`` the answers
    (see the module docstring), naming each offending case with the first reason found:
    its id, a link or unreadable directory in its inputs, answers inside its inputs, then
    answers around its directory. Answers are those of every case the task defines."""
    inputs_root = traptask_dir / traptask_config.dirs.inputs
    expected_root = traptask_dir / traptask_config.dirs.expected
    running = list(dict.fromkeys(c.id for c in cases))
    defined = list(dict.fromkeys([*(c.id for c in traptask_config.cases), *running]))
    problems: dict[str, str] = {}
    for case in defined:
        parts = PurePath(case).parts
        if not parts or PurePath(case).is_absolute() or ".." in parts:
            problems[case] = f"id does not name a directory inside {inputs_root} and {expected_root}"
    defined = [c for c in defined if c not in problems]
    running = [c for c in running if c not in problems]
    held: dict[Identity, str] = {}
    for case in running:
        if problem := _walk_inputs(inputs_root, case, held):
            problems[case] = problem
    identity = cache(_identity)
    real = {c: Path(os.path.realpath(inputs_root / c)) for c in running}
    answers = [(f"the answers of case {c!r}", expected_root / c) for c in defined]
    for whose, path in [("the expected root", expected_root), *answers]:
        if identity(path) is not None:
            resolved = Path(os.path.realpath(path))
            for k in (identity(p) for p in (resolved, *resolved.parents)):
                if k in held:
                    problems.setdefault(held[k], f"inputs {real[held[k]]} hold {whose} {resolved}")
                    break
    around: dict[Identity, str] = {}
    for case in (c for c in running if c not in problems):
        for k in (identity(p) for p in (real[case], *real[case].parents)):
            if k is not None:
                around.setdefault(k, case)
    for whose, path in answers:
        if (k := identity(path)) in around:
            resolved = os.path.realpath(path)
            problems.setdefault(around[k], f"inputs {real[around[k]]} sit inside {whose} {resolved}")
    if problems:
        lines = [f"\n  {case}: {problem}" for case, problem in problems.items()]
        if len(lines) > MAX_NAMED_CASES:
            lines[MAX_NAMED_CASES:] = [f"\n  and {len(lines) - MAX_NAMED_CASES} more"]
        raise ConfigError(
            f"refusing to run task {traptask_dir}: these cases' inputs could hand every solution "
            f"the answers:{''.join(lines)}"
        )

"""Refuse a task that could hand a solution the answers, before any case runs.

A solution is handed its case's inputs directory, resolved, and nothing downstream can tell
a link or an answers directory in it from an ordinary file. So: no directory from the
inputs root down to each running case's inputs may be a symlink (the inputs root itself
may be one), and a symlink inside them must resolve to a regular file under the inputs
root, outside every answers directory — a file shared by many cases, say; no answers
directory — the expected root or any defined case's — may lie inside a running case's
inputs, and no case's answers directory may be or lie around a running case's directory;
and case ids stay inside their directories. Both roots are resolved exactly as the runner
resolves them (``task_root``), and directories are compared as (device, inode), which sees
through a different letter case or Unicode spelling of one directory, and through a
firmlink. An answer placed in the inputs — a copy, a hard link, or an answers-side link to
an input file — isn't checked, and is the task author's to avoid."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from functools import cache
from pathlib import Path, PurePath
from typing import NoReturn

from trap.errors import ConfigError
from trap.models import TraptaskCase, TraptaskConfig
from trap.runner.layout import task_root

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


def _link_problem(link: str, inputs_root: Path, answers: dict[Identity, str]) -> str | None:
    """Why the symlink ``link`` inside a case can't be handed over, or None when it resolves
    to a regular file under ``inputs_root`` and no directory from that file up to the root
    is one of the ``answers``."""
    target = Path(os.path.realpath(link))
    try:
        regular = stat.S_ISREG(os.lstat(target).st_mode)
    except OSError:
        regular = False
    if not regular or not target.is_relative_to(inputs_root):
        return f"inputs {link} is a symlink that does not resolve to a file inside {inputs_root}"
    below = target.relative_to(inputs_root)
    for path in (inputs_root / p for p in (below, *below.parents)):
        if (k := _identity(path)) in answers:
            return f"inputs {link} is a symlink into {answers[k]} {path}"
    return None


def _walk_inputs(
    inputs_root: Path, case: str, held: dict[Identity, str], answers: dict[Identity, str]
) -> str | None:
    """Why ``case``'s inputs can't be handed over — a symlink on the way to them, one in
    them that ``_link_problem`` refuses, or something that can't be read — or None,
    recording every directory in them in ``held``. A case whose folder isn't there hands
    nothing over; anything else the walk can't read refuses."""
    path = inputs_root
    try:
        for part in PurePath(case).parts:
            path = path / part
            try:
                if _is_link(path):
                    return f"inputs {path} is a symlink"
            except FileNotFoundError:
                return None
        for dirpath, dirnames, filenames in os.walk(path, onerror=_raise):
            st = os.stat(dirpath)
            held.setdefault((st.st_dev, st.st_ino), case)
            for name in sorted(dirnames + filenames):
                entry = os.path.join(dirpath, name)
                if _is_link(entry) and (problem := _link_problem(entry, inputs_root, answers)):
                    return problem
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
    inputs_root = task_root(traptask_dir, traptask_config.dirs.inputs)
    expected_root = task_root(traptask_dir, traptask_config.dirs.expected)
    running = list(dict.fromkeys(c.id for c in cases))
    defined = list(dict.fromkeys([*(c.id for c in traptask_config.cases), *running]))
    problems: dict[str, str] = {}
    for case in defined:
        parts = PurePath(case).parts
        if not parts or PurePath(case).is_absolute() or ".." in parts:
            problems[case] = f"id does not name a directory inside {inputs_root} and {expected_root}"
    defined = [c for c in defined if c not in problems]
    running = [c for c in running if c not in problems]
    identity = cache(_identity)
    answers = [(f"the answers of case {c!r}", expected_root / c) for c in defined]
    every_answers = [("the expected root", expected_root), *answers]
    named = {k: whose for whose, path in every_answers if (k := identity(path)) is not None}
    held: dict[Identity, str] = {}
    for case in running:
        if problem := _walk_inputs(inputs_root, case, held, named):
            problems[case] = problem
    real = {c: Path(os.path.realpath(inputs_root / c)) for c in running}
    for whose, path in every_answers:
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

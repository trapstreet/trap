"""``tp shape cmd``: a one-line command template around someone else's program.

Programs take their input in no standard way (an argument, stdin, a file path), so the
line says it once: ``{prompt}`` is the question's text, ``{prompt_file}`` the path to the
question in the case's work directory, ``{repo}`` the program's checkout. With neither
prompt placeholder the question goes to stdin. The program runs in the case's work
directory with the scrubbed environment; what it prints is the answer and its exit code
is the case's.

    cmd: tp shape cmd --repo . --template "python {repo}/main.py {prompt}"
"""

from __future__ import annotations

import shlex
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from trap.models.card import SolutionCard
from trap.shapes._case import (
    Deadline,
    ShapeError,
    ShapeExit,
    ShapeParser,
    add_case_args,
    fail,
    open_case,
    print_card,
    run_group,
)

#: Behaviour version of this shape: how the template is expanded and what the answer
#: rule is. A change to either is a new card, not a quiet re-scoring of an old one.
CMD_SHAPE_VERSION = 1


def expand(
    template: str, *, question: str, prompt_path: Path, repo: Path | None
) -> tuple[list[str], str | None]:
    """The argv for one case and the stdin to feed it (None when the template takes the
    question itself). Split first, substitute after: the question becomes one argument
    whatever spaces or quotes it holds, and no shell ever reads it. The question goes in
    last, so braces inside it are left alone."""
    try:
        tokens = shlex.split(template)
    except ValueError as e:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"cannot parse the command template: {e}") from None
    if not tokens:
        raise ShapeError(ShapeExit.CONFIG_ERROR, "the command template is empty")
    if repo is None and any("{repo}" in t for t in tokens):
        raise ShapeError(ShapeExit.CONFIG_ERROR, "the template uses {repo} but no --repo was given")
    takes_prompt = any("{prompt}" in t or "{prompt_file}" in t for t in tokens)
    repo_s = str(repo) if repo is not None else ""
    argv = [
        t.replace("{repo}", repo_s).replace("{prompt_file}", str(prompt_path)).replace("{prompt}", question)
        for t in tokens
    ]
    return argv, (None if takes_prompt else question)


def _setup_argv(setup: str | None, repo: Path | None) -> tuple[list[str], Path] | None:
    """``--setup``'s argv and the ``--repo`` it runs in, or ``None`` when no ``--setup``
    was given. Parsed and validated up front, alongside the template, so a broken
    ``--setup`` fails before the card is printed — same as a broken template."""
    if not setup:
        return None
    if repo is None:
        raise ShapeError(ShapeExit.CONFIG_ERROR, "--setup requires --repo")
    try:
        argv = shlex.split(setup)
    except ValueError as e:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"cannot parse --setup: {e}") from None
    if not argv:
        raise ShapeError(ShapeExit.CONFIG_ERROR, "--setup is empty")
    return argv, repo


def _run_setup(argv: list[str], repo: Path, *, env: Mapping[str, str], deadline: Deadline) -> None:
    """Run ``--setup`` once, in ``--repo``, before the case's own command — same deadline
    and env. A setup that cannot even start, or that exits non-zero, means the program was
    never in a state to answer: a config problem, the same as a template naming a program
    that is not there. ``run_group`` never raises on its own deadline — it kills the group
    and hands back ``ShapeExit.TIMEOUT`` as an ordinary exit code — so that case is
    checked first: a setup that overruns the deadline is the deadline (124), on the same
    contract as the case's own command, never folded into the generic non-zero-exit
    config error (24)."""
    try:
        out, err, code = run_group(argv, cwd=repo, env=env, stdin=None, deadline=deadline)
    except FileNotFoundError:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"--setup command not found: {argv[0]}") from None
    except OSError as e:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"cannot start --setup {argv[0]}: {e.strerror}") from None
    if code == ShapeExit.TIMEOUT:
        raise ShapeError(ShapeExit.TIMEOUT, "--setup reached the deadline")
    if code != 0:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"--setup exited {code}: {(err or out)[-500:]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = ShapeParser(prog="tp shape cmd", description="Run a command template for one case.")
    parser.add_argument(
        "--template", required=True, help="the command line, e.g. 'python {repo}/main.py {prompt}'"
    )
    parser.add_argument("--repo", type=Path, help="the program's checkout, substituted for {repo}")
    parser.add_argument("--setup", help="one-off install command, run once in --repo before the case")
    add_case_args(parser)
    args = parser.parse_args(argv)
    deadline = Deadline(args.deadline)
    try:
        sandbox, env = open_case(args)
    except ShapeError as e:
        return fail(e)
    try:
        repo = args.repo.resolve() if args.repo else None
        command, stdin = expand(
            args.template, question=sandbox.question, prompt_path=sandbox.prompt_path, repo=repo
        )
        setup = _setup_argv(args.setup, repo)
        print_card(
            SolutionCard(
                shape="cmd",
                shape_version=CMD_SHAPE_VERSION,
                cmd=args.template,
                setup=args.setup,
                timeout=round(args.deadline),
            )
        )
        if setup is not None:
            _run_setup(*setup, env=env, deadline=deadline)
        try:
            out, err, code = run_group(command, cwd=sandbox.workdir, env=env, stdin=stdin, deadline=deadline)
        except FileNotFoundError:
            raise ShapeError(ShapeExit.CONFIG_ERROR, f"command not found: {command[0]}") from None
        except OSError as e:  # no execute bit, a bad shebang, anything else exec refused
            raise ShapeError(ShapeExit.CONFIG_ERROR, f"cannot start {command[0]}: {e.strerror}") from None
    except ShapeError as e:
        return fail(e)
    finally:
        sandbox.close()
    sys.stdout.write(out)
    sys.stderr.write(err)
    # A program killed by signal N has returncode -N; report it the way a shell does,
    # 128 + N — an exit status is 0-255, and -9 would reach the runner as 247.
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main())

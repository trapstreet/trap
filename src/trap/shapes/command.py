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
from collections.abc import Sequence
from pathlib import Path

from trap.shapes._case import (
    Deadline,
    ShapeError,
    ShapeExit,
    ShapeParser,
    add_case_args,
    fail,
    open_case,
    run_group,
)


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = ShapeParser(prog="tp shape cmd", description="Run a command template for one case.")
    parser.add_argument(
        "--template", required=True, help="the command line, e.g. 'python {repo}/main.py {prompt}'"
    )
    parser.add_argument("--repo", type=Path, help="the program's checkout, substituted for {repo}")
    add_case_args(parser)
    args = parser.parse_args(argv)
    deadline = Deadline(args.deadline)
    try:
        sandbox, env = open_case(args)
    except ShapeError as e:
        return fail(e)
    try:
        command, stdin = expand(
            args.template,
            question=sandbox.question,
            prompt_path=sandbox.prompt_path,
            repo=args.repo.resolve() if args.repo else None,
        )
        try:
            out, err, code = run_group(command, cwd=sandbox.workdir, env=env, stdin=stdin, deadline=deadline)
        except FileNotFoundError:
            raise ShapeError(ShapeExit.CONFIG_ERROR, f"command not found: {command[0]}") from None
    except ShapeError as e:
        return fail(e)
    finally:
        sandbox.close()
    sys.stdout.write(out)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

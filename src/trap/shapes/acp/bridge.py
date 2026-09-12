"""``tp shape acp``: one case, one prompt, through an agent that speaks ACP.

    cmd: tp shape acp --agent-id claude-acp --model haiku --scrub ../task
         --agent-cmd "npx -y @agentclientprotocol/claude-agent-acp@0.76.0"

Prints the agent's last message and exits with the turn's ShapeExit code; the rest of
the conversation — the config the case ran with, the agent's earlier messages and tool
calls, permissions, self-reported usage — goes to stderr. ``--describe`` prints the
agent's config options instead: the values ``--model`` and ``--option`` accept."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from trap.shapes._case import (
    Deadline,
    ShapeError,
    ShapeExit,
    ShapeParser,
    add_case_args,
    fail,
    open_case,
    scrubbed_env,
)
from trap.shapes.acp.connection import AcpError
from trap.shapes.acp.hints import extra_env, install_skill, session_meta
from trap.shapes.acp.session import describe_agent, run_case


def _parser() -> ShapeParser:
    parser = ShapeParser(prog="tp shape acp", description="Drive an ACP agent for one case.")
    parser.add_argument(
        "--agent-cmd", required=True, help="how to start the agent, e.g. 'npx -y <package>@<version>'"
    )
    parser.add_argument("--agent-id", help="the agent's ACP registry id (claude-acp, codex-acp, ...)")
    parser.add_argument("--model", help="a value the agent lists for its model option (see --describe)")
    parser.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="ID=VALUE",
        help="set another config option, e.g. effort=low",
    )
    parser.add_argument(
        "--skill", type=Path, help="a skill directory (with SKILL.md) to install for the case"
    )
    parser.add_argument(
        "--describe", action="store_true", help="print the agent's config options as JSON and exit"
    )
    add_case_args(parser)
    return parser


def _options(pairs: Sequence[str]) -> dict[str, str]:
    options: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ShapeError(ShapeExit.CONFIG_ERROR, f"--option wants ID=VALUE, got {pair!r}")
        options[key] = value
    return options


def _agent_start_failed(error: OSError) -> int:
    """Starting the agent process failed outright — not found, no execute bit, a bad
    shebang, or anything else the OS refused. One config error, wherever the agent is
    launched: from the case run and from ``--describe`` alike."""
    return fail(ShapeError(ShapeExit.CONFIG_ERROR, f"cannot start the agent: {error}"))


def _describe(agent: list[str], agent_id: str | None, env: Mapping[str, str]) -> int:
    cwd = Path(tempfile.mkdtemp(prefix="trap-describe-")).resolve()
    try:
        options = describe_agent(agent, env=env, cwd=cwd, meta=session_meta(agent_id))
    except (AcpError, TimeoutError, KeyError, TypeError) as e:  # TypeError: a reply not shaped like ACP
        return fail(ShapeError(ShapeExit.AGENT_ERROR, f"could not open a session: {e}"))
    except OSError as e:
        return _agent_start_failed(e)
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    print(json.dumps(options, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        agent = shlex.split(args.agent_cmd)
        if not agent:
            raise ValueError("empty")
    except ValueError as e:
        return fail(ShapeError(ShapeExit.CONFIG_ERROR, f"cannot parse --agent-cmd: {e}"))
    if args.describe:
        # Never the full os.environ: --describe opens a real session with the agent, so
        # whatever the case's env scrub would have hidden from it must stay hidden here too.
        described_env = scrubbed_env(os.environ, manifest_envvar=args.manifest_envvar, prefixes=args.scrub)
        return _describe(agent, args.agent_id, described_env)
    if not args.model:
        return fail(
            ShapeError(ShapeExit.CONFIG_ERROR, "--model is required; --describe lists this agent's values")
        )
    deadline = Deadline(args.deadline)
    try:
        options = _options(args.option)
        sandbox, env = open_case(args)
    except ShapeError as e:
        return fail(e)
    try:
        if args.skill is not None:
            install_skill(args.agent_id, args.skill, sandbox.workdir)
        outcome = run_case(
            agent,
            env={**env, **extra_env(args.agent_id, env)},
            workdir=sandbox.workdir,
            question=sandbox.question,
            model=args.model,
            options=options,
            meta=session_meta(args.agent_id),
            deadline=deadline,
        )
    except ShapeError as e:
        return fail(e)
    except OSError as e:
        return _agent_start_failed(e)
    finally:
        sandbox.close()
    for note in outcome.notes:
        print(f"[trap] {note}", file=sys.stderr)
    if outcome.answer:
        print(outcome.answer)
    return int(outcome.exit_code)

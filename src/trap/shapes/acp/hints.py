"""What tp knows about particular agents — only what the protocol cannot say.

Keyed by the agent's ACP registry id (``--agent-id``). An agent with no entry here runs
on the protocol alone. Verified by the 2026-09-12 probe unless a docstring says
otherwise."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from trap.shapes._case import ShapeError, ShapeExit, copy_tree_without_symlinks, refuse_symlinks, remove_tree

CLAUDE = "claude-acp"
CODEX = "codex-acp"

#: How to start each agent tp can launch without being told -- what `tp run --agent <id>`
#: resolves to. Every entry pins an exact version, never a tag: which model an alias like
#: `sonnet` means is decided by the agent's build, so a moving version would silently
#: re-score every run made with it. Upgrading tp may move a pin, and that is visible
#: rather than silent -- the resolved `package@version` is what lands in the run's card,
#: so two tp builds that disagree produce two different card digests, which is correct:
#: they are two different solutions.
AGENT_COMMANDS: Mapping[str, str] = {
    CLAUDE: "npx -y @agentclientprotocol/claude-agent-acp@0.76.0",
    CODEX: "npx -y @agentclientprotocol/codex-acp@1.11.0",
}


class UnknownAgent(ValueError):
    """No launch command is known for this agent id. Raised on the `tp run` path, not
    inside a case, so it carries no shape exit code -- the CLI turns it into a config
    failure the way it does any other bad argument."""


def agent_command(agent_id: str) -> str:
    """The command that starts `agent_id`, or raise naming the ids that do work.

    Only agents tp has actually verified are listed. An agent that speaks ACP but is not
    here can still be run the explicit way -- `tp shape acp --agent-cmd ...` in a
    trap.yaml -- which is also the only way to pin a version tp does not carry.
    """
    try:
        return AGENT_COMMANDS[agent_id]
    except KeyError:
        known = ", ".join(sorted(AGENT_COMMANDS))
        raise UnknownAgent(
            f"no launch command known for agent {agent_id!r}; tp can start: {known}.\n"
            f"  Any other ACP agent runs through a trap.yaml with "
            f'`cmd: tp shape acp --agent-id {agent_id} --agent-cmd "<how to start it>" ...`'
        ) from None


def session_meta(agent_id: str | None) -> dict[str, Any] | None:
    """``_meta`` for ``session/new``. claude-agent-acp loads the runner's own CLAUDE.md,
    skills, hooks and plugins by default (settingSources user/project/local) — the same
    card would be a different agent on every machine — and a settings.json ``env`` can
    override ANTHROPIC_BASE_URL around the cost proxy. "project" alone loads only the case
    directory, which is also where an installed skill lives."""
    if agent_id == CLAUDE:
        return {"claudeCode": {"options": {"settingSources": ["project"]}}}
    return None


def extra_env(
    agent_id: str | None, env: Mapping[str, str], *, codex_auth: Path | None = None
) -> dict[str, str]:
    """Environment an agent needs on top of the case's. codex-acp ignores OPENAI_BASE_URL;
    CODEX_CONFIG carries the cost proxy instead — but only under an API-key login. Under a
    ChatGPT login the redirect sends the ChatGPT token to api.openai.com and every turn
    fails with 401 (probe). API-key mode itself is not yet verified."""
    base = env.get("OPENAI_BASE_URL")
    if agent_id != CODEX or not base:
        return {}
    auth_file = codex_auth or Path(env.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"
    mode = _codex_auth_mode(auth_file)
    if mode == "chatgpt" or (mode is None and not env.get("OPENAI_API_KEY")):
        return {}
    return {"CODEX_CONFIG": json.dumps({"openai_base_url": base})}


def _codex_auth_mode(path: Path) -> str | None:
    try:
        mode = json.loads(path.read_text()).get("auth_mode")
    except (OSError, ValueError, AttributeError):
        return None
    return mode if isinstance(mode, str) else None


def install_skill(agent_id: str | None, skill_dir: Path, workdir: Path) -> None:
    """Put a skill where the agent loads it from. Claude Code only for now: project skills
    live in ``.claude/skills/<name>/`` and load under settingSources ["project"] (probe).

    Copied the way a case's inputs are, but with no link allowed at all, not even one to
    a file: no symlink in the skill is ever created in the work directory, and a skill
    holding one is refused rather than silently missing whatever it named. Any other
    failure to install — the directory is missing, a file could not be read — is a config
    error naming the skill directory, not the agent (starting the agent is a separate
    failure, reported separately)."""
    if agent_id != CLAUDE:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"installing a skill is supported for {CLAUDE} only")
    if not skill_dir.is_dir():
        raise ShapeError(
            ShapeExit.CONFIG_ERROR, f"cannot install the skill from {skill_dir}: no such directory"
        )
    if not (skill_dir / "SKILL.md").is_file():
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"no SKILL.md in {skill_dir}")
    dest = workdir / ".claude" / "skills" / skill_dir.resolve().name
    try:
        links = copy_tree_without_symlinks(skill_dir, dest)
    except OSError as e:
        remove_tree(dest, what="the partly installed skill")
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"cannot install the skill from {skill_dir}: {e}") from None
    if links:
        remove_tree(dest, what="the partly installed skill")
        refuse_symlinks("this skill contains symlinks", links)

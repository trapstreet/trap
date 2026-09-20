"""The solution card: the labels that decide what a run measured.

Two runs of the same repository at the same commit are the same solution only when
they were driven the same way -- same agent build, same model, same options, same
skill, same command. The card is those labels, and its digest is the content address
the site stores beside the repo and commit, so "this skill on sonnet" and "this skill
on haiku" are two rows instead of one row that hides half the story.

The rules here are shared with the server, which recomputes the digest from what it
received and refuses a submission whose declared digest disagrees. Both sides run
``tests/data/solution_card_vectors.json``; the format is written down in
``docs/reference/solution-card.md``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

#: Every card field except ``name``: renaming a card must not change its identity.
DIGEST_FIELDS = (
    "agent",
    "cmd",
    "model",
    "options",
    "provider",
    "setup",
    "shape",
    "shape_version",
    "skill",
    "timeout",
)


class SolutionCard(BaseModel):
    """How a solution was driven. Labels only -- never code, never a secret's value."""

    shape: Literal["acp", "model", "cmd"]
    #: Behaviour version of the shape that produced this card: the answer rule, the
    #: generation defaults and what gets copied all move the score, so a change to any
    #: of them is a new card rather than a quiet re-scoring of an old one.
    shape_version: int
    #: ACP: the agent's full package@version, as resolved when it started.
    agent: str | None = None
    #: The model that was *asked* for. What a harness used underneath (Claude Code
    #: reaches for Haiku on its own) is reported per model in the run's cost, not here.
    model: str | None = None
    #: Model-direct: which vendor API the request went to.
    provider: str | None = None
    #: Agent options that actually took effect, id -> value. An option the chosen model
    #: does not offer is skipped when the case runs, so it never lands here.
    options: dict[str, str] = Field(default_factory=dict)
    #: The skill that was installed, as ``repo@sha`` when the CLI could resolve one.
    skill: str | None = None
    #: The command template, and the one-off install line a program needed.
    cmd: str | None = None
    setup: str | None = None
    #: The per-case deadline the shape ran under, in seconds.
    timeout: float | None = None
    #: Display only, outside the digest: `solutions.title` on the site.
    name: str | None = None


def _digest_payload(card: SolutionCard) -> dict[str, Any]:
    """The digest-bearing fields that carry something. A field left unset and a field
    set to nothing are the same card, so adding an optional field later cannot change
    the digest of cards that do not use it."""
    payload: dict[str, Any] = {}
    for field in DIGEST_FIELDS:
        value = getattr(card, field)
        if value is None or value == {} or value == "":
            continue
        payload[field] = value
    return payload


def canonical_card_json(card: SolutionCard) -> bytes:
    """The bytes both sides hash: keys sorted, no padding, UTF-8, text left as text."""
    return json.dumps(
        _digest_payload(card), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def card_digest(card: SolutionCard) -> str:
    """The content address: sha256 of the canonical JSON, lowercase hex."""
    return hashlib.sha256(canonical_card_json(card)).hexdigest()


def card_label(card: SolutionCard) -> str:
    """The row name a site-graded run gets: what was driven, by what, with what."""
    if card.name:
        return card.name
    parts = [card.agent or card.provider or card.cmd or card.shape]
    if card.model:
        parts.append(card.model)
    if card.skill:
        repo, _, commit = card.skill.partition("@")
        short = repo.rsplit("/", 2)[-2:] if "/" in repo else [repo]
        parts.append("/".join(short) + (f"@{commit[:7]}" if commit else ""))
    return " · ".join(parts)

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
    #: The per-case deadline the shape ran under, in whole seconds. Every number in a
    #: card is an integer -- JSON has one numeric type in JavaScript but two in Python,
    #: so a float that happens to be whole (``570.0``) renders differently from the int
    #: ``570`` in each language's default JSON encoder, and the digest must not depend
    #: on which one wrote it. A shape's deadline may be fractional; the card records
    #: ``round(deadline)``.
    timeout: int | None = None
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
        # Belt and braces: every numeric field is typed `int`, so ordinary validated
        # construction never hands us a float here -- but a card built with
        # `model_construct` (validation skipped) can, and a future numeric field could
        # too. An integral float and its int are the same number; only Python's JSON
        # encoder tells them apart (`570.0` vs `570`), so fold it to int before that
        # difference can reach the digest. A genuinely fractional value is left alone.
        if isinstance(value, float) and value.is_integer():
            value = int(value)
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
    """The row name a site-graded run gets: what was driven, by what, with what.

    Safe to publish as it stands -- never a command template, and never a
    filesystem path:

    - ``cmd``/``setup`` are not in the fallback chain at all, so a bare
      ``cmd``-shaped card with no name, agent or provider is named by its shape
      alone (``"cmd"``), never by its command line.
    - a skill is named ``owner/repo@sha7`` only when it actually reads as
      ``repo@sha`` (`_resolved_skill`); a skill that never resolved to a git
      remote -- the routine outcome for a local directory that is not itself a
      git checkout, per ``SolutionCard.skill``'s own docstring -- is named by
      its own last path segment alone (`_last_segment`), never by the
      directories that hold it (which routinely include a real username).

    This is what lets a second caller (a ``tp submit`` preview, ``tp inspect``,
    a console line) call this function and inherit both guarantees for free,
    rather than re-deriving them. `live.context`'s ``skills.installed`` uses the
    same two helpers, so the name shown there for a given skill and the name
    shown here for the same skill never disagree.
    """
    if card.name:
        return card.name
    parts = [card.agent or card.provider or card.shape]
    if card.model:
        parts.append(card.model)
    if card.skill:
        resolved = _resolved_skill(card.skill)
        if resolved is not None:
            repo, commit = resolved
            short = repo.rsplit("/", 2)[-2:] if "/" in repo else [repo]
            parts.append("/".join(short) + f"@{commit[:7]}")
        else:
            parts.append(_last_segment(card.skill))
    return " · ".join(parts)


def _resolved_skill(skill: str) -> tuple[str, str] | None:
    """``(repo, commit)`` when ``skill`` reads as ``repo@sha``; ``None`` for
    anything else -- most often a local directory that never resolved to a git
    remote. The one test that decides whether a skill is named by its full
    reference or only its last path segment, shared by `card_label` and
    `live.context`'s ``skills.installed`` so the two can never disagree about
    which skills are "resolved".

    Two separate ``if`` statements, deliberately, rather than one compound
    ``if sep and commit``: a skill with an "@" but nothing after it
    (``"repo@"``) is its own input class, distinct from "no @ at all" -- and a
    single compound condition can reach 100% branch coverage without a test
    ever landing on that combination, since branch coverage tracks the whole
    expression's outcome, not which half of it produced a False.
    """
    repo, sep, commit = skill.partition("@")
    if not sep:
        return None
    if not commit:
        return None
    return repo, commit


def _last_segment(path: str) -> str:
    """The last "/"-delimited piece of a string, ignoring a trailing "/" --
    shared between the label a card shows and the skill reference the run
    context reports, so a skill that could not be resolved to a repo is named
    identically on both surfaces."""
    return path.rstrip("/").rsplit("/", 1)[-1]

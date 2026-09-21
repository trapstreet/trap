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
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

#: What actually distinguishes a `repo@sha` reference from an ordinary path that
#: happens to contain an "@" (an npm-scoped package directory, a username shaped
#: like an email address): the tail must read as a git commit sha. Lowercase hex,
#: 7 to 64 characters -- the same bounds git itself accepts for an abbreviated-to-
#: full sha. A separator's mere presence is not enough; round 2 of review found
#: that `"@my-org/my-skill"` and `"eve@work/my-skill"` both satisfied "has an @
#: with something on both sides" and were published with a real username in them.
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")

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
    - a skill is named ``owner/repo@sha7`` only when `_resolved_skill` calls it
      a *publishable* reference; anything else -- a plain directory, an "@"
      that turned out not to be a commit separator at all (an npm-scoped
      package directory, a username shaped like an email address), or a git
      remote that is itself a local path -- is named by its own last path
      segment alone (`_unresolved_skill_name`), never by the directories that
      hold it (which routinely include a real username).

    This is what lets a second caller (a ``tp submit`` preview, ``tp inspect``,
    a console line) call this function and inherit both guarantees for free,
    rather than re-deriving them. `live.context`'s ``skills.installed`` uses the
    same helpers, so the name shown there for a given skill and the name shown
    here for the same skill never disagree.
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
            parts.append(_unresolved_skill_name(card.skill))
    return " · ".join(parts)


def _skill_parts(skill: str) -> tuple[str, str] | None:
    """``(repo, commit)`` when the tail of ``skill`` -- split at its LAST "@",
    never the first, so a repo URL that itself contains one (``git@host:a/b``)
    is not split in the wrong place -- reads as a git commit sha (`_COMMIT_RE`).
    ``None`` for anything else: most often an ordinary filesystem path with no
    "@" in it at all, or one whose "@" belongs to the path itself (an npm-scoped
    package directory, an email-shaped username) rather than to a commit.

    This says only "the tail looks like a commit". Whether the repo half is
    something safe to *publish* is a separate question `_resolved_skill` asks
    next -- kept apart so `_unresolved_skill_name` can still tell a local path
    that merely isn't publishable (``/repo/path@<sha>``, still names its own
    directory) from one whose "@" was never a commit reference in the first
    place (``/repo/path@notasha``, names its own last segment as a whole).
    """
    repo, sep, commit = skill.rpartition("@")
    if not sep:
        return None
    if not _COMMIT_RE.fullmatch(commit):
        return None
    if not repo:
        return None
    return repo, commit


def _resolved_skill(skill: str) -> tuple[str, str] | None:
    """``(repo, commit)`` when ``skill`` is a *publishable* ``repo@sha``
    reference: everything `_skill_parts` requires, plus the repo must not
    itself be a filesystem path. A git remote CAN legitimately be a local path
    (``git clone /Users/alice/repos/foo``), and such a "remote" is exactly as
    unpublishable as an unresolved directory -- rejecting it here is the
    behaviour that is wanted, not a compromise a future reader should "fix"
    back.

    Shared by `card_label` and `live.context`'s ``skills.installed`` so the two
    can never disagree about which skills are "resolved".
    """
    parts = _skill_parts(skill)
    if parts is None:
        return None
    repo, commit = parts
    if repo.startswith("/"):
        return None
    return repo, commit


def _unresolved_skill_name(skill: str) -> str:
    """The name shown for a skill `_resolved_skill` would not vouch for. When
    ``skill`` still parses as ``<directory>@<sha>`` (a local path used as a git
    remote, rejected only because it is a path), the sha is not the skill's own
    name -- its directory's last segment is, exactly as if no sha had ever been
    appended. Anything else that looks unresolved -- no "@" at all, or a tail
    that is not a plausible commit -- is an ordinary filesystem path with
    nothing to strip, so its own last segment is used whole."""
    parts = _skill_parts(skill)
    if parts is not None:
        return _last_segment(parts[0])
    return _last_segment(skill)


def _last_segment(path: str) -> str:
    """The last "/"-delimited piece of a string, ignoring a trailing "/" --
    shared between the label a card shows and the skill reference the run
    context reports, so a skill that could not be resolved to a repo is named
    identically on both surfaces."""
    return path.rstrip("/").rsplit("/", 1)[-1]

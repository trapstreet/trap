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

`_parse_skill`, `_skill_leaf` and `_authority` are underscored because nothing
outside this repository should call them, but they are part of this module's
contract with `trap.live.context`, which shares them so that ``identity.name``
and ``skills.installed`` can never disagree about whether a skill resolved.
Rename them with that caller in hand.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

#: A git commit sha: lowercase hex, 7 to 64 characters -- the same bounds git
#: itself accepts for an abbreviated-to-full sha.
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")

#: C0 controls and DEL. No real forge allows one of these in a remote name;
#: cheap and defensive rather than urgent -- but no published string may
#: contain one regardless.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

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

    Safe to publish as it stands: never a command template, never a path,
    never a secret, on the same reconstruct-only rule `_parse_skill` states
    and holds to. A bare ``cmd``-shaped card with no name, agent or provider
    is named by its shape alone (``"cmd"``) -- ``cmd``/``setup`` are not in
    the fallback chain at all. A skill is named ``owner/repo@sha7`` only when
    `_parse_skill` reads it as a full reference, using only ``owner``,
    ``repo`` and a slice of ``commit`` from what it returns (``scheme`` and
    ``authority`` are for `live.context`'s ``skills.installed`` instead);
    anything `_parse_skill` would not parse is named by `_skill_leaf`'s own
    last path segment. `live.context`'s ``skills.installed`` parses the same
    way, so the two never disagree about one skill's name.
    """
    if card.name:
        return card.name
    parts = [card.agent or card.provider or card.shape]
    if card.model:
        parts.append(card.model)
    if card.skill:
        parsed = _parse_skill(card.skill)
        if parsed is not None:
            _scheme, _authority, owner, repo, commit = parsed
            parts.append(f"{owner}/{repo}@{commit[:7]}")
        else:
            parts.append(_skill_leaf(card.skill))
    return " · ".join(parts)


def _parse_skill(skill: str) -> tuple[str, str, str, str, str] | None:
    """``(scheme, authority, owner, repo, commit)`` when ``skill`` is a
    fully specified, publishable reference; ``None`` for anything else, in
    which case `_skill_leaf` names it instead.

    A reference is an http(s) URL naming exactly one owner and one repo,
    followed by ``@<sha>`` (lowercase hex, 7 to 64 characters). ``scheme``
    is ``"http"`` or ``"https"``, whichever the input used; ``authority`` is
    the host, re-bracketed if it is an IPv6 literal, with the port appended
    when the input had one (`_authority`). Every value here is read out of
    ``urlsplit``'s own parsed fields and glued into the published form
    unchanged -- never sliced from the original string, and never from
    ``.netloc`` (right shape, wrong contents). This is why userinfo can
    never reach the wire (nothing here ever reads ``.username``/
    ``.password``) while the scheme and the port *do* reach it, unchanged:
    the published values are meant to be the endpoint the input actually
    named, and a credential is not part of that endpoint while a port is.
    Query strings and fragments are parsed and equally never read.

    ``None`` covers: a plain filesystem path (POSIX, Windows, or UNC --
    none parses as a URL); any scheme but ``http``/``https`` (a scp-style or
    ``ssh://`` remote is normalised to http(s) before it ever reaches a
    card; anything still not http(s) has no business becoming one); an "@"
    that belongs to the path itself, not a commit (an npm-scoped package
    directory, an email-shaped username); a git remote that is itself a
    local path; a URL whose port is not a plain integer in range, or whose
    bracketed host is not a valid IPv6 literal (``urlsplit`` itself raises
    for either, not just for ``.port``/``.hostname`` -- caught below, so the
    parse never crashes its caller); anything but exactly two path segments;
    ``.``/``..`` as either segment (no forge allows them, and any
    URL-normalising re-parse, including the site's, collapses them); a repo
    segment that is nothing but a stripped ``.git`` suffix; or a control
    character anywhere in what would be published. That last check is one
    guard over the whole assembled result, not a per-field ``if`` -- a
    future field added to the tuple is covered by construction, not by
    remembering to add a fourth check.

    Splits on the LAST "@", never the first, so a URL that itself contains
    one (real HTTP Basic credentials, or ``user@host``-shaped syntax) is not
    split at the wrong point. Leading/trailing whitespace is stripped before
    any of this, so it cannot turn a valid reference into an unresolved one
    by riding along on the end of the commit.
    """
    skill = skill.strip()
    repo_url, sep, commit = skill.rpartition("@")
    if not sep:
        return None
    if not _COMMIT_RE.fullmatch(commit):
        return None
    try:
        parsed = urlsplit(repo_url)
        scheme = parsed.scheme
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if scheme not in ("http", "https"):
        return None
    if not host:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2:
        return None
    owner, repo = segments
    if owner in (".", ".."):
        return None
    repo = re.sub(r"\.git$", "", repo)
    if not repo or repo in (".", ".."):
        return None
    published = (scheme, _authority(host, port), owner, repo, commit)
    if any(_CONTROL_RE.search(part) for part in published):
        return None
    return published


def _authority(host: str, port: int | None) -> str:
    """``host[:port]``, with an IPv6 literal re-bracketed -- see
    `_parse_skill` for why both are read and kept rather than left to
    whatever ``.hostname`` alone would give (nothing)."""
    wrapped = f"[{host}]" if ":" in host else host
    return f"{wrapped}:{port}" if port is not None else wrapped


def _skill_leaf(skill: str) -> str:
    """The one thing about ``skill`` that is always safe to publish when it
    did not parse into a full reference (`_parse_skill` returned ``None``):
    its own leaf, whichever kind of path it happens to be written as.
    Splits on both "/" and "\\\\" -- nothing here runs on Windows, but a
    string this module did not build is not this module's to trust the
    shape of. When ``skill`` still ends in something that reads as a commit
    sha (`_parse_skill` rejected it for some other reason, most often that
    the part before the "@" is a local path rather than a URL), that tail is
    stripped before the leaf is taken; otherwise the leaf comes from the
    whole string. Control characters are stripped from the result, and a
    leaf left empty by that stripping is named the literal ``"skill"`` --
    ``SkillRef`` requires a ``name``, and a card that installed a skill must
    not report installing none.
    """
    repo_candidate, sep, commit = skill.rpartition("@")
    base = repo_candidate if sep and _COMMIT_RE.fullmatch(commit) else skill
    pieces = [piece for piece in re.split(r"[/\\]", base) if piece]
    leaf = pieces[-1] if pieces else base
    leaf = _CONTROL_RE.sub("", leaf)
    return leaf or "skill"

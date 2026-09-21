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
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

#: A git commit sha: lowercase hex, 7 to 64 characters -- the same bounds git
#: itself accepts for an abbreviated-to-full sha.
_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")

#: C0 controls and DEL. No real forge allows one of these in a remote name;
#: cheap and defensive rather than urgent -- but no published string may
#: contain one regardless (round 4 of review).
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

    Safe to publish as it stands. Not because every dangerous shape of input
    has been thought of and rejected -- three rounds of review found leaks in
    exactly that approach, in a username, an npm-scoped path, a credential
    embedded in a remote URL, a Windows drive letter, each one caught only
    after the fact -- but because **no value here is a string this module
    received; every value is reconstructed from parsed components**:

    - ``cmd``/``setup`` are not in the fallback chain at all, so a bare
      ``cmd``-shaped card with no name, agent or provider is named by its shape
      alone (``"cmd"``): built from a fixed literal, never from ``card.cmd``.
    - a skill is named ``owner/repo@sha7`` only when `_parse_skill` reads it as
      a full reference and hands back its four parts (authority, owner, repo,
      commit) -- and even then, only ``owner``, ``repo`` and a slice of
      ``commit`` are used; ``authority`` (host, and a port when the input had
      one) is used by `live.context`'s ``skills.installed``, not here, and
      everything `_parse_skill` chose not to extract -- a URL's userinfo, a
      query string, a fragment, anything before or after the parsed pieces --
      simply never reaches a variable either function reads. Anything
      `_parse_skill` would not parse -- a plain directory, an "@" that was not
      a commit separator, a git remote that is itself a local path, a Windows
      or UNC path, an owner or repo segment with a control character in it --
      is named by `_skill_leaf`, which returns exactly one thing: the input's
      own last path segment, control characters stripped. There is no third
      path through this code that copies more of ``skill`` than that.

    This is what lets a second caller (a ``tp submit`` preview, ``tp inspect``,
    a console line) call this function and inherit the guarantee for free,
    rather than re-deriving it -- and what makes the guarantee something a
    reader can check by looking at which variables this function's return
    value is built from, not by trying to enumerate every shape ``skill``
    must never be. `live.context`'s ``skills.installed`` parses the same way,
    so the name shown there for a given skill and the name shown here for the
    same skill never disagree.
    """
    if card.name:
        return card.name
    parts = [card.agent or card.provider or card.shape]
    if card.model:
        parts.append(card.model)
    if card.skill:
        parsed = _parse_skill(card.skill)
        if parsed is not None:
            _authority, owner, repo, commit = parsed
            parts.append(f"{owner}/{repo}@{commit[:7]}")
        else:
            parts.append(_skill_leaf(card.skill))
    return " · ".join(parts)


def _parse_skill(skill: str) -> tuple[str, str, str, str] | None:
    """``(authority, owner, repo, commit)`` when ``skill`` is a fully
    specified, publishable reference -- an http(s) URL naming exactly one
    owner and one repo, followed by ``@<sha>`` where the sha is lowercase
    hex, 7 to 64 characters. ``authority`` is the host `_authority` rebuilds
    -- re-bracketed if it is an IPv6 literal, with the port appended when the
    input had one -- never the host alone: round 4 of review found that
    reading ``.hostname`` and never ``.port`` published a URL pointing at a
    different endpoint than the input named (a self-hosted remote on a
    non-default port is reachable through real code, not a crafted string --
    see `_authority`), and that ``.hostname`` alone drops the brackets an
    IPv6 literal needs to remain a valid URL.

    ``None`` for anything else: a plain filesystem path (POSIX, Windows, or
    UNC -- none of them parses as a URL at all); an "@" that belongs to the
    path itself rather than to a commit (an npm-scoped package directory, an
    email-shaped username); a git remote that is itself a local path
    (rejected for the same reason a bare directory is: it has no host to
    publish); a URL whose port is not a plain non-negative integer in range
    (``urlsplit`` raises rather than returning ``None`` for one of those, so
    this catches the exception); a URL with anything other than exactly two
    path segments (a GitLab-style subgroup, say -- there is no attempt to
    guess which segment is "the" owner or repo, only exactly two is
    accepted); a repo segment that is nothing but a ``.git`` suffix once
    that suffix is stripped; or an owner or repo segment with a control
    character in it (cheap and defensive -- no real forge allows one, but no
    published string may contain one regardless).

    Splits on the LAST "@", never the first, so a URL that itself contains
    one for an unrelated reason (a scp-style remote is normalised to an
    http(s) form before it ever reaches a card, but nothing stops a future
    caller from handing this a URL with real HTTP Basic credentials in it)
    is not split at the wrong point.

    The four values returned here are the only things about ``skill`` that
    ever reach the wire, and they are read out of ``urlsplit``'s own parsed
    fields -- ``.scheme``, ``.hostname``, ``.port``, ``.path``'s segments --
    never by slicing the original string, and never from ``.netloc`` (which
    has the right shape but carries userinfo verbatim). In particular, this
    never reads ``.username``/``.password``: a URL's userinfo is parsed by
    ``urlsplit`` into those two attributes precisely so that code which
    never asks for them structurally cannot leak what they hold, whatever it
    is. A query string or a fragment, if the input had one, is parsed into
    ``.query``/``.fragment`` and is equally never read.
    """
    repo_url, sep, commit = skill.rpartition("@")
    if not sep:
        return None
    if not _COMMIT_RE.fullmatch(commit):
        return None
    parsed = urlsplit(repo_url)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.hostname
    if not host:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2:
        return None
    owner, repo = segments
    repo = re.sub(r"\.git$", "", repo)
    if not repo:
        return None
    if _CONTROL_RE.search(owner):
        return None
    if _CONTROL_RE.search(repo):
        return None
    return _authority(host, port), owner, repo, commit


def _authority(host: str, port: int | None) -> str:
    """``host[:port]``, with an IPv6 literal re-bracketed. ``urlsplit``'s
    ``.hostname`` strips both the brackets a ``[::1]``-style host needs to
    remain a valid URL authority and the port, so both must be added back by
    hand rather than trusted to still be attached to whatever ``.hostname``
    returns -- ``.netloc`` has the right shape already, but carries userinfo
    along with it, which is the one thing that must never be reconstructed.
    """
    wrapped = f"[{host}]" if ":" in host else host
    return f"{wrapped}:{port}" if port is not None else wrapped


def _skill_leaf(skill: str) -> str:
    """The one thing about ``skill`` that is always safe to publish when it did
    not parse into a full reference (`_parse_skill` returned ``None``): its own
    leaf, whichever kind of path it happens to be written as. Splits on both
    "/" and "\\\\" -- nothing here runs on Windows, but nothing stops ``skill``
    from being written as a Windows or UNC path regardless (round 3 of review:
    a POSIX-only ``startswith("/")`` guard and a "/"-only split each let a
    drive letter or a share name straight through), and a string this module
    did not build is not this module's to trust the shape of.

    When ``skill`` still ends in something that reads as a commit sha --
    `_parse_skill` rejected it for some other reason, most often that the part
    before the "@" is a local path rather than a URL -- that tail is not part
    of the skill's own name, so it is stripped before the leaf is taken.
    Anything else (no "@" at all, or a tail that plainly is not a commit) has
    no sha to strip, so the leaf comes from the whole string.

    Control characters (round 4) are stripped from the result, not merely
    left in: a card that installed a skill must still say so, so a leaf that
    is nothing *but* control characters is named the literal ``"skill"``
    rather than an empty string -- ``SkillRef`` requires a ``name``, and an
    empty one would read as indistinguishable from "no skill".
    """
    repo_candidate, sep, commit = skill.rpartition("@")
    base = repo_candidate if sep and _COMMIT_RE.fullmatch(commit) else skill
    pieces = [piece for piece in re.split(r"[/\\]", base) if piece]
    leaf = pieces[-1] if pieces else base
    leaf = _CONTROL_RE.sub("", leaf)
    return leaf or "skill"

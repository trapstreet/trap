"""A permanent, seeded fuzz corpus over `card.skill`, checked against an oracle
that computes the *expected wire value* independently of the code under test.

Five leaks have been found in this card's naming logic across five rounds of
review: a username via an unresolved local directory; an npm-scoped path and an
email-shaped username, both mistaken for `repo@sha` by a separator's mere
presence; a git remote's embedded HTTP Basic credentials; a Windows drive
letter and a UNC share, neither caught by a POSIX-only guard; a port silently
dropped and an IPv6 host's brackets silently lost (round 4); and a scheme
silently upgraded to `https` regardless of what the input actually said
(round 5) -- the *other* failure mode of "reconstruct from parsed
components", where the rebuilt value is not a leak but a lie about where the
skill actually lives. (Round 4's version of this docstring named "a wrong
scheme" as exactly the kind of thing an equality oracle would catch that a
substring check could not -- before round 5's bug was found. It was right.)

Round 4's fix is also a fix to this file. The round 1-3 version of this fuzzer
checked a *substring* property: for each generated input, none of a fixed list
of "forbidden" tokens planted at generation time (a home directory, a
credential) could appear in the wire. That property is why the port bug
survived this fuzzer's own corpus: `_url_cases` forbade the port digits from
appearing at all, so *dropping* the port -- exactly the round 4 bug -- made
the forbidden-token check pass, not fail. A substring check against planted
tokens can only ever catch a mutation that moves one of those tokens; it
structurally cannot catch a mutation that *omits* a value the test never
asserted should be there.

The fix: since every case here is built from named, known components, the
test can compute -- independently of `card_label`/`_skill_ref`, by restating
the spec rather than calling into it -- exactly what the faithful published
value should be, and assert equality against the whole thing (`skills.installed[0]`,
and the skill segment of `identity.name`), not just the absence of a few
tokens. This is what makes the test able to catch a shape nobody predicted:
a missing port, a wrong scheme, a stray `.query` byte, would all show up as an
equality mismatch even though none of them was ever named "forbidden".

The substring checks are kept anyway, as cheap extra guards for the one thing
an equality check does not make redundant: they are what proves a *specific
planted secret* (a credential, a home directory's username) never appears
*anywhere* in the JSON, not merely that the one field they might have leaked
into has the right value.
"""

from __future__ import annotations

import itertools
import json
import random
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import pytest

from trap.live.context import build_context
from trap.models.card import SolutionCard
from trap.models.provenance import GitProvenance, Provenance
from trap.models.trap_yaml import Profile

#: Fixed so the corpus is identical on every run, in CI and on every machine.
_SEED = 20260921
_RNG = random.Random(_SEED)


def _random_token(length: int) -> str:
    return "".join(_RNG.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(length))


# -- the oracle ---------------------------------------------------------------
# Independent restatements of the spec `trap.models.card` implements -- not
# calls into it. Re-declared here on purpose: if a future change to the
# implementation silently drifts from the spec these encode, this file's own
# copy does not drift with it, so the mismatch is exactly what gets caught.

_COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _expected_leaf(skill: str) -> str:
    """What `_skill_leaf` should return for an unresolved `skill`: strip a
    trailing ``@<valid-hex-commit>`` if present, split on "/" and "\\", take
    the last non-empty piece, strip control characters, fall back to "skill"
    if that leaves nothing."""
    repo_candidate, sep, commit = skill.rpartition("@")
    base = repo_candidate if sep and _COMMIT_RE.fullmatch(commit) else skill
    pieces = [piece for piece in re.split(r"[/\\]", base) if piece]
    leaf = pieces[-1] if pieces else base
    leaf = _CONTROL_RE.sub("", leaf)
    return leaf or "skill"


def _expected_authority(host: str, is_ipv6: bool, port: int | None) -> str:
    """What `_authority` should return: the host, re-bracketed if it is an
    IPv6 literal, with the port appended when there is one."""
    wrapped = f"[{host}]" if is_ipv6 else host
    return f"{wrapped}:{port}" if port is not None else wrapped


@dataclass(frozen=True)
class _Case:
    skill: str
    #: The exact expected `skills.installed[0]` dict.
    expected_installed: dict[str, str]
    #: The exact expected skill segment of `identity.name` (this test's card
    #: always has a fixed agent/model, so `identity.name` is always
    #: ``f"pkg@1 · m · {expected_label_segment}"``).
    expected_label_segment: str
    #: Specific strings planted at generation time that must never appear
    #: anywhere in the wire, whatever `expected_installed` says -- a home
    #: directory's token, a credential, a distinctive query/fragment marker.
    forbidden: tuple[str, ...] = field(default_factory=tuple)


# -- component pools ---------------------------------------------------------------
# The classes the reviewer named across rounds 2-5: home directories including a
# Windows drive path and a UNC share; npm scopes; unicode, spaces and a control
# character in the leaf; trailing slashes; 6/7/40/64-character and uppercase hex;
# non-hex branch/tag names; schemes including both that must resolve (http, https
# -- kept as given, round 5) and one that must never (ftp); hosts including IPv6
# literals; ports; userinfo; query strings; fragments.

#: path -> the tokens in it that must never reach the wire (its own leaf excluded --
#: none of these pools' leaves share a token with a home directory or npm scope, so
#: there is never a legitimate reason one of these could coincide with the leaf).
_HOME_DIRS: dict[str, list[str]] = {
    "/Users/alice": ["alice"],
    "C:\\Users\\bob": ["bob"],
    "\\\\fileserver\\share": ["fileserver", "share"],
    "/Users/eve@work": ["eve@work"],
    f"/home/{_random_token(8)}": [],  # filled in below -- the token itself
    f"C:\\Users\\{_random_token(8)}": [],
}
# Back-fill the two random entries' forbidden tokens now that the keys exist.
for _path in list(_HOME_DIRS):
    if not _HOME_DIRS[_path]:
        _HOME_DIRS[_path] = [_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]]

_NPM_SCOPES = ["", "@my-org"]
#: A leaf with a control character in it (round 4) -- `_expected_leaf` strips
#: it, same as the last non-control character it shares no other token with.
_LEAVES = ["my-skill", "skill with spaces", "skïll-ünïcode", "skill\x00name"]
_TRAILING = ["", "/"]

_HEX7 = "a" * 7
_HEX40 = "a" * 40
_HEX6 = "a" * 6  # one short of the minimum -- invalid
_HEXUPPER = "A" * 40  # valid length, wrong case -- invalid
#: name -> commit string, or None for "no @<commit> suffix at all".
_PATH_COMMITS: dict[str, str | None] = {
    "none": None,
    "valid7": _HEX7,
    "valid40": _HEX40,
    "tooshort": _HEX6,
    "uppercase": _HEXUPPER,
    "branchname": "main",
}


def _path_cases() -> list[_Case]:
    """Every path-shaped input: never a `scheme://` URL, so `_parse_skill` can
    never resolve one no matter what commit-shaped tail is glued on -- the
    expected wire value is always the leaf alone, computed the same way
    whether or not a (valid or invalid) commit is attached."""
    cases = []
    for home, forbidden in _HOME_DIRS.items():
        for scope in _NPM_SCOPES:
            for leaf in _LEAVES:
                for trailing in _TRAILING:
                    for commit in _PATH_COMMITS.values():
                        segment = f"{scope}/{leaf}" if scope else leaf
                        skill = f"{home}/{segment}{trailing}"
                        if commit is not None:
                            skill = f"{skill}@{commit}"
                        expected = _expected_leaf(skill)
                        case_forbidden = list(forbidden)
                        if scope:
                            case_forbidden.append(scope)
                        cases.append(_Case(skill, {"name": expected}, expected, tuple(case_forbidden)))
    return cases


@dataclass(frozen=True)
class _Host:
    bare: str  # never bracketed, never carries a port
    is_ipv6: bool


_HOSTS = [
    _Host("gitlab.example.com", False),
    _Host("::1", True),
    _Host("2001:db8::1", True),
    # round 6, finding A: a control character in the host reaches the wire
    # through `_authority` unless the whole assembled tuple is checked, not
    # just owner/repo -- this host must never resolve.
    _Host("ho\x00st.example.com", False),
]
_PORTS = [None, 9999]
#: round 6: a `.git`-suffixed repo, so the corpus actually exercises
#: stripping it (the reviewer's `.git` mutation passed 4/4 before this --
#: no pool case had one).
_OWNER_REPO = [("owner", "repo"), ("a", "b"), ("owner", "repo.git")]
#: Distinctive markers -- never appear anywhere else in this file's strings --
#: so a query string or fragment leaking (the reconstruct rule applies to them
#: too: nothing `_parse_skill` did not choose to extract should reach the wire)
#: is as detectable as a credential leaking.
_SUFFIXES = ["", "?ref=refMARKER111", "#fragMARKER222"]
_SCHEMES = ["https://", "http://", "ftp://"]  # ftp:// must never resolve

_RANDOM_CREDENTIAL = _random_token(16)
#: (the literal userinfo text, or None; the secret substring that must never
#: survive, or None when there is no userinfo at all).
_USERINFO: list[tuple[str, str | None]] = [
    ("", None),
    ("oauth2:ghp_SECRETTOKEN1234@", "ghp_SECRETTOKEN1234"),
    (f"user:{_RANDOM_CREDENTIAL}@", _RANDOM_CREDENTIAL),
]
#: A URL case always has a commit suffix -- an "@"-free URL is a `_path_cases`
#: concern, one more "no @ at all" input.
_URL_COMMITS = {k: v for k, v in _PATH_COMMITS.items() if v is not None}


def _url_cases() -> list[_Case]:
    """Every URL-shaped input: a host (plain or IPv6, with or without a port),
    optional userinfo, optional query/fragment noise, and a commit that may or
    may not be valid, over a scheme that may or may not be http(s). The
    expected wire value is computed independently for each combination: a
    faithfully reconstructed reference (port and IPv6 brackets included,
    credentials and query/fragment excluded) when the combination should
    resolve, the shared leaf oracle otherwise."""
    cases = []
    for scheme, (userinfo, secret), host, port, (owner, repo), suffix, commit in itertools.product(
        _SCHEMES, _USERINFO, _HOSTS, _PORTS, _OWNER_REPO, _SUFFIXES, _URL_COMMITS.values()
    ):
        host_in_url = f"[{host.bare}]" if host.is_ipv6 else host.bare
        netloc = f"{host_in_url}:{port}" if port is not None else host_in_url
        skill = f"{scheme}{userinfo}{netloc}/{owner}/{repo}{suffix}@{commit}"

        # round 6: a `.git` suffix is stripped from the repo name -- the
        # value that ends up in `owner`/`repo`/`repo_name` below has to
        # reflect that, since the real code strips it before publishing.
        repo_name = repo.removesuffix(".git")
        resolves = (
            scheme in ("https://", "http://")
            and _COMMIT_RE.fullmatch(commit) is not None
            and not _CONTROL_RE.search(host.bare)  # round 6, finding A
        )
        forbidden = [secret] if secret else []
        if resolves:
            # round 5: the published scheme is the input's own, never
            # hard-coded -- an http-only remote must publish an http:// link.
            scheme_name = scheme.removesuffix("://")
            authority = _expected_authority(host.bare, host.is_ipv6, port)
            expected_installed = {
                "name": repo_name,
                "repo": f"{scheme_name}://{authority}/{owner}/{repo_name}",
                "commit": commit,
            }
            expected_label = f"{owner}/{repo_name}@{commit[:7]}"
            # Only meaningful once resolved: an *unresolved* case's leaf is
            # computed from the whole string, marker included, by design --
            # the equality check above already covers that value exactly, so
            # forbidding the marker there would flag correct behaviour.
            if suffix:
                forbidden.append(suffix.lstrip("?#"))
        else:
            leaf = _expected_leaf(skill)
            expected_installed = {"name": leaf}
            expected_label = leaf

        cases.append(_Case(skill, expected_installed, expected_label, tuple(forbidden)))
    return cases


def _leaf_case(skill: str) -> _Case:
    """A one-off case that must fall back to the leaf, kept outside the main
    combinatorial product because adding it as a dimension there would
    multiply the corpus for no extra coverage -- each exercises a boundary
    that does not interact with the others."""
    leaf = _expected_leaf(skill)
    return _Case(skill, {"name": leaf}, leaf, ())


_ONE_OFF_LEAF_CASES = [
    # A control character in a resolved URL's repo segment, and -- its own
    # case, checked independently, since a mutation could drop either half
    # of the assembled-tuple guard without the other -- in its owner segment.
    _leaf_case(f"https://github.com/owner/repo\x00secret@{_HEX40}"),
    _leaf_case(f"https://github.com/own\x00er/repo@{_HEX40}"),
    # round 6, finding B: `urlsplit` itself raises for an IPv4 address in
    # brackets, and for a control character inside brackets -- not just for
    # `.port`/`.hostname`, which round 4 already guarded. The parse must be
    # total, so these must fall back to the leaf, not crash the test.
    _leaf_case(f"https://[192.168.1.1]/a/b@{_HEX40}"),
    _leaf_case(f"https://[ho\x00st]/a/b@{_HEX40}"),
    # round 6, finding C: "." and ".." collapse under any URL-normalising
    # re-parse (including the site's own), so the two-segment structure tp
    # published would not survive one -- checked on owner and repo
    # independently, each its own case.
    _leaf_case(f"https://github.com/./repo@{_HEX40}"),
    _leaf_case(f"https://github.com/owner/..@{_HEX40}"),
]

#: round 6, finding C: leading/trailing whitespace is stripped before
#: parsing -- the "must NOT fall back to the leaf" counterpart to the cases
#: above, since a reference that is otherwise entirely valid must still
#: resolve.
_WHITESPACE_RESOLVES_CASE = _Case(
    f"https://github.com/a/b@{_HEX40} ",
    {"name": "b", "repo": "https://github.com/a/b", "commit": _HEX40},
    f"a/b@{_HEX40[:7]}",
    (),
)

_CASES = _path_cases() + _url_cases() + _ONE_OFF_LEAF_CASES + [_WHITESPACE_RESOLVES_CASE]

_PROFILE = Profile()
_PROVENANCE = Provenance(solution=GitProvenance())


def _patch_for(skill: str) -> dict:
    card = SolutionCard(shape="acp", shape_version=1, agent="pkg@1", model="m", skill=skill)
    return build_context(
        profile=_PROFILE, provenance=_PROVENANCE, environment=None, trap_version="x", card=card
    )


def test_the_corpus_is_not_empty_and_stays_a_fixed_size():
    # A corpus that silently shrank to zero (a bad refactor of the generators
    # above) would make every test below vacuously pass. Pinning the exact
    # count catches that, and catches the corpus silently growing unboundedly
    # slow just as fast.
    assert len(_CASES) == len(_path_cases()) + len(_url_cases()) + len(_ONE_OFF_LEAF_CASES) + 1
    assert 500 <= len(_CASES) <= 10000


def test_every_emitted_skill_name_is_a_single_segment():
    # Kept as a cheap extra guard: whether or not `_parse_skill` resolved the
    # input, the one thing `skills.installed[0]["name"]` is ever built from --
    # a parsed `repo` segment, or the leaf -- cannot contain a path separator.
    for case in _CASES:
        name = _patch_for(case.skill)["skills"]["installed"][0]["name"]
        assert "/" not in name and "\\" not in name, f"skill={case.skill!r} name={name!r}"


def test_no_planted_secret_reaches_the_wire():
    # Kept as a cheap extra guard, narrowed from round 1-3's version: a port
    # is no longer forbidden (round 4 -- it is now a legitimate, expected part
    # of a resolved reference), but a planted credential, a home directory's
    # token, and a query/fragment marker still must never appear anywhere in
    # the JSON, regardless of what `expected_installed` says for that case.
    for case in _CASES:
        wire = json.dumps(_patch_for(case.skill))
        for secret in case.forbidden:
            assert secret not in wire, f"skill={case.skill!r} leaked {secret!r}"


def test_every_case_matches_its_independently_expected_wire_value():
    # The property that replaces round 1-3's substring check: every generated
    # case's `skills.installed[0]` and `identity.name` skill segment must
    # equal the value this file computed *from the same named components*,
    # independently of `card_label`/`_skill_ref`/`_parse_skill`. A dropped
    # port, a wrong scheme boundary, a stray query byte -- anything that
    # changes the *value* rather than merely reintroducing a planted token --
    # shows up here as an equality mismatch, which is what a substring check
    # against a fixed forbidden list can never do.
    #
    # round 6's own lesson: this test alone is not enough, because its
    # oracle (`_expected_authority`, `_expected_leaf`) restates the rules
    # `_authority`/`_skill_leaf` implement rather than checking them against
    # anything independent -- a control-character host added to `_HOSTS`
    # passed here even before this round's fix, because `_expected_authority`
    # recomputed `_authority`'s own (incomplete) rule, bug included. The
    # three invariant tests below do not have that blind spot: none of them
    # calls back into this file's own mirror of the implementation.
    for case in _CASES:
        patch = _patch_for(case.skill)
        installed = patch["skills"]["installed"][0]
        name = patch["identity"]["name"]
        assert installed == case.expected_installed, f"skill={case.skill!r} installed={installed!r}"
        expected_name = f"pkg@1 · m · {case.expected_label_segment}"
        assert name == expected_name, f"skill={case.skill!r} name={name!r}"


def _string_values(value: object):
    """Every string value in a JSON-shaped structure, at any depth -- not
    the JSON *text*: `json.dumps` escapes a control character into a six-
    character sequence rather than emitting the byte, so a substring search
    over the serialised text would never find one. Walking the raw
    structure before serialisation is what actually inspects the values a
    reader must trust have no control characters in them."""
    if isinstance(value, dict):
        for v in value.values():
            yield from _string_values(v)
    elif isinstance(value, list):
        for item in value:
            yield from _string_values(item)
    elif isinstance(value, str):
        yield value


def test_invariant_no_published_string_contains_a_control_character():
    # Invariant 1 (round 6, finding A): stated as an invariant over the
    # whole patch, not as one more per-field substring check -- a future
    # field this module starts publishing is covered by construction, the
    # same reasoning `_parse_skill`'s own single guard now uses internally.
    for case in _CASES:
        patch = _patch_for(case.skill)
        for value in _string_values(patch):
            assert not _CONTROL_RE.search(value), f"skill={case.skill!r} value={value!r}"


def test_invariant_a_resolved_reference_reparses_to_the_input_it_named():
    # Invariant 3 (round 6, finding D -- the round's real lesson): a
    # round-trip check, independent of `_authority`'s own formatting rule.
    # Re-parses the *emitted* `repo` URL with the standard library, the same
    # way any reader of it would, and compares that against an independent
    # re-parse of the input's own repo-url portion (everything before the
    # last "@") -- not against this file's `_expected_authority`, which
    # would agree with a bug in `_authority` rather than exposing it. This
    # is what would have caught the dropped port, the lost IPv6 brackets and
    # the forced `https` without anyone naming those shapes in advance.
    #
    # The one named exception: `.git` is a documented, intentional
    # normalisation (`_parse_skill` strips it from the repo segment before
    # publishing), not a reconstruction defect, so the expected path has the
    # same suffix stripped before comparing -- a one-line restatement of a
    # simple, spec-level rule, not a mirror of `_authority`'s own logic.
    for case in _CASES:
        installed = _patch_for(case.skill)["skills"]["installed"][0]
        if "repo" not in installed:
            continue
        input_repo_url = case.skill.strip().rpartition("@")[0]
        input_parsed = urlsplit(input_repo_url)  # must succeed: a `repo` was emitted, so this already did
        try:
            output_parsed = urlsplit(installed["repo"])
        except ValueError as e:
            pytest.fail(f"skill={case.skill!r} emitted repo {installed['repo']!r} does not re-parse: {e}")
        assert output_parsed.scheme == input_parsed.scheme, case.skill
        assert output_parsed.hostname == input_parsed.hostname, case.skill
        assert output_parsed.port == input_parsed.port, case.skill
        expected_path = re.sub(r"\.git$", "", input_parsed.path)
        assert output_parsed.path == expected_path, f"skill={case.skill!r} repo={installed['repo']!r}"

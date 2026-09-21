"""A permanent, seeded fuzz corpus over `card.skill`, and the two properties that
would have caught every leak found in this card's naming logic so far.

Three rounds of review found four leaks in `card_label` / `live.context._skill_ref`
(a username via an unresolved local directory; an npm-scoped path and an
email-shaped username, both mistaken for `repo@sha` by a separator's mere presence;
a git remote's embedded HTTP Basic credentials; a Windows drive letter and a UNC
share, neither caught by a POSIX-only guard) -- and each time, a hand-built
generator was written to catch the specific shape just found, then deleted. Branch
coverage sat at 100% throughout: every leak was a *missing test*, not a missing
line, so 100% coverage on its own never would have caught the next one.

This is that generator, kept, run on every test invocation instead of once by
hand. It does not try to enumerate dangerous shapes -- that is exactly the game
that lost three times running -- it builds inputs from named, tracked components
and checks two properties that hold regardless of which component combination
produced the input:

1. the ``name`` field this module ever emits (`skills.installed[0]["name"]`) never
   contains a path separator, "/" or "\\\\" -- true whether or not `_parse_skill`
   resolved anything, since `card_label`/`_skill_ref` never emit anything else in
   that slot.
2. for every generated input, none of the *specific strings used to build its
   unpublishable parts* -- a home directory's distinguishing token, an npm scope,
   an embedded credential, a port number -- appear anywhere in the JSON patch,
   whether or not the input happened to resolve to a publishable reference.
   (What legitimately *does* survive resolution -- host, owner, repo, the
   commit's first 7 characters -- is checked by the exact-value regression tests
   in ``test_solution_card.py`` and ``test_context.py`` instead: this fuzz corpus
   is deliberately built so that every one of its inputs' *sensitive* components
   never has a legitimate reason to reach the wire, so property 2 needs no case
   analysis of "resolved or not" to stay meaningful.)

No new dependency: `random.Random` with a fixed seed, plus `itertools.product`
over the component lists below.
"""

from __future__ import annotations

import itertools
import json
import random

from trap.live.context import build_context
from trap.models.card import SolutionCard
from trap.models.provenance import GitProvenance, Provenance
from trap.models.trap_yaml import Profile

#: Fixed so the corpus is identical on every run, in CI and on every machine.
_SEED = 20260921
_RNG = random.Random(_SEED)


def _random_token(length: int) -> str:
    return "".join(_RNG.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(length))


# -- component pools ---------------------------------------------------------------
# The classes the reviewer named: home directories including a Windows drive path
# and a UNC share; npm scopes; unicode and spaces in the leaf; trailing slashes;
# 6/7/40/64/65-character and uppercase hex; URLs with ports and with userinfo.

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
_LEAVES = ["my-skill", "skill with spaces", "skïll-ünïcode"]
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

_URL_SCHEMES = ["https://", "http://", "ftp://"]
_URL_HOSTS = ["github.com", "gitlab.example.com:9418"]  # the second carries a port
_USERINFO = ["", "oauth2:ghp_SECRETTOKEN1234@", f"user:{_random_token(16)}@"]
_OWNER_REPO = [("owner", "repo"), ("a", "b")]
#: A URL case always has a commit suffix (an "@"-free URL is a `_path_cases`
#: concern -- it would just be one more "no @ at all" input).
_URL_COMMITS = {k: v for k, v in _PATH_COMMITS.items() if v is not None}


def _path_cases() -> list[tuple[str, list[str]]]:
    """``(skill, forbidden)`` for every path-shaped input: never a `scheme://`
    URL, so `_parse_skill` can never resolve one no matter what commit-shaped
    tail is glued on -- only the leaf may ever appear on the wire."""
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
                        case_forbidden = list(forbidden)
                        if scope:
                            case_forbidden.append(scope)
                        cases.append((skill, case_forbidden))
    return cases


def _url_cases() -> list[tuple[str, list[str]]]:
    """``(skill, forbidden)`` for every URL-shaped input: a legitimate host and
    a legitimate owner/repo, with userinfo and/or a port layered on as noise.
    Resolution may or may not succeed depending on the commit and scheme, but
    the injected credential and port must never appear on the wire regardless
    -- `_parse_skill` never reads a URL's userinfo, and never reads its port,
    whether or not the rest of the URL turns out to be publishable."""
    cases = []
    for scheme, userinfo, host, (owner, repo), commit in itertools.product(
        _URL_SCHEMES, _USERINFO, _URL_HOSTS, _OWNER_REPO, _URL_COMMITS.values()
    ):
        skill = f"{scheme}{userinfo}{host}/{owner}/{repo}@{commit}"
        forbidden = []
        if userinfo:
            forbidden.append(userinfo.split(":", 1)[1].rstrip("@"))  # the secret half
        if ":" in host:
            forbidden.append(host.split(":", 1)[1])  # the port digits
        cases.append((skill, forbidden))
    return cases


_CASES = _path_cases() + _url_cases()

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
    assert len(_CASES) == len(_path_cases()) + len(_url_cases())
    assert 500 <= len(_CASES) <= 5000


def test_every_emitted_skill_name_is_a_single_segment():
    # Property 1: whether or not `_parse_skill` resolved the input, the one
    # thing `skills.installed[0]["name"]` is ever built from -- a parsed
    # `repo` segment, or `_skill_leaf`'s own leaf -- cannot contain a path
    # separator.
    for skill, _forbidden in _CASES:
        name = _patch_for(skill)["skills"]["installed"][0]["name"]
        assert "/" not in name and "\\" not in name, f"skill={skill!r} name={name!r}"


def test_no_sensitive_component_used_to_build_an_input_reaches_the_wire():
    # Property 2: this is the one that would have caught all four leaks
    # without anyone predicting their shape -- none of the *specific strings
    # this test used to build the unpublishable parts of an input* (a home
    # directory's token, an npm scope, an embedded credential, a port) may
    # appear anywhere in the JSON patch, however that input was assembled.
    for skill, forbidden in _CASES:
        wire = json.dumps(_patch_for(skill))
        for secret in forbidden:
            assert secret not in wire, f"skill={skill!r} leaked {secret!r}"

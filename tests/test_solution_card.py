"""The solution card: what was measured, and the content address that names it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trap.models.card import (
    DIGEST_FIELDS,
    SolutionCard,
    _parse_skill,
    _skill_leaf,
    canonical_card_json,
    card_digest,
    card_label,
)

VECTORS = Path(__file__).parent / "data" / "solution_card_vectors.json"


def test_canonical_json_sorts_keys_and_drops_what_is_not_set():
    card = SolutionCard(shape="acp", shape_version=1, model="sonnet", agent="pkg@1.2.3")
    assert (
        canonical_card_json(card) == b'{"agent":"pkg@1.2.3","model":"sonnet","shape":"acp","shape_version":1}'
    )


def test_the_name_is_display_only_and_never_reaches_the_digest():
    plain = SolutionCard(shape="cmd", shape_version=1, cmd="python main.py {prompt}")
    named = plain.model_copy(update={"name": "someone's tool"})
    assert card_digest(named) == card_digest(plain)
    assert "name" not in DIGEST_FIELDS


def test_two_cards_that_differ_in_one_label_differ_in_digest():
    sonnet = SolutionCard(shape="acp", shape_version=1, agent="pkg@1.2.3", model="sonnet")
    haiku = sonnet.model_copy(update={"model": "haiku"})
    assert card_digest(sonnet) != card_digest(haiku)


def test_an_empty_options_map_is_the_same_card_as_no_options():
    with_empty = SolutionCard(shape="acp", shape_version=1, model="haiku", options={})
    without = SolutionCard(shape="acp", shape_version=1, model="haiku")
    assert card_digest(with_empty) == card_digest(without)


def test_options_order_does_not_change_the_digest():
    one = SolutionCard(shape="acp", shape_version=1, options={"effort": "low", "mode": "default"})
    other = SolutionCard(shape="acp", shape_version=1, options={"mode": "default", "effort": "low"})
    assert card_digest(one) == card_digest(other)


def test_non_ascii_stays_as_itself_rather_than_an_escape():
    card = SolutionCard(shape="cmd", shape_version=1, cmd="python main.py 题目 {prompt}")
    assert "题目".encode() in canonical_card_json(card)


def test_the_label_names_the_agent_the_model_and_the_skill():
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="claude-agent-acp@0.76.0",
        model="sonnet",
        skill="https://github.com/a/b@0123456789abcdef0123456789abcdef01234567",
    )
    assert card_label(card) == "claude-agent-acp@0.76.0 · sonnet · a/b@0123456"


def test_the_label_is_just_the_name_when_one_is_set():
    card = SolutionCard(shape="cmd", shape_version=1, cmd="python main.py", name="acme-solver")
    assert card_label(card) == "acme-solver"


def test_the_label_ignores_cmd_and_falls_back_to_the_bare_shape():
    # `cmd` is not in the fallback chain at all (deliberate contract change, round 1
    # of review): a bare `cmd`-shaped card with no name, agent or provider is named
    # by its shape alone, never by its command line.
    card = SolutionCard(shape="cmd", shape_version=1, cmd="python main.py")
    assert card_label(card) == "cmd"


def test_the_label_shows_only_the_last_segment_when_the_skill_has_no_pinned_commit():
    # No "@" at all means the skill did not parse into a reference -- the same
    # test `_parse_skill` uses -- so only the last path segment is shown, never
    # the repo path in full (deliberate contract change, round 1 of review).
    card = SolutionCard(shape="model", shape_version=1, model="sonnet", skill="https://github.com/a/b")
    assert card_label(card) == "model · sonnet · b"


def test_the_label_never_leaks_an_unresolved_skills_directory():
    # The critical case: a local skill directory that never resolved to a git remote
    # (the routine outcome for a skill that is not itself a git checkout -- see
    # `card.skill`'s own docstring) must never put its parent directories -- routinely
    # a real username -- into a label that gets published.
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill="/Users/alice/checkout/skills/my-skill",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "alice" not in label and "/" not in label


def test_the_label_never_leaks_the_command_or_a_token_reference():
    # card_label has exactly one caller today (live/context.py's identity.name), but
    # its own docstring claims to be safe to publish as-is -- so a future second
    # caller (a `tp submit` preview, `tp inspect`, a console line) inherits that
    # guarantee for free, rather than having to re-derive it.
    card = SolutionCard(
        shape="cmd",
        shape_version=1,
        cmd="python /Users/alice/project/main.py --key $OPENAI_API_KEY {prompt}",
        setup="uv sync",
    )
    label = card_label(card)
    assert label == "cmd"
    assert "alice" not in label and "OPENAI_API_KEY" not in label and "main.py" not in label


def test_the_label_never_leaks_a_scoped_package_path_that_merely_contains_an_at_sign():
    # round 2: an npm-scoped skill directory (`@my-org/...`) has an "@" with
    # something on both sides of it, same as a real `repo@sha` -- but it is not
    # a commit separator at all. `_parse_skill` must tell the two apart by
    # what the tail actually looks like, not by the mere presence of a "@".
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill="/Users/alice/.cache/node_modules/@my-org/my-skill",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "alice" not in label and "/" not in label


def test_the_label_never_leaks_an_email_shaped_username_in_a_path():
    # round 2: same failure mode, different reason the "@" is there -- a
    # username that happens to look like an email address.
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill="/Users/eve@work/my-skill",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "eve" not in label and "/" not in label


def test_the_label_names_a_local_path_remote_by_its_own_directory_not_its_sha():
    # A git remote CAN legitimately be a local filesystem path
    # (`git clone /Users/alice/repos/foo`), and such a "remote" is exactly as
    # unpublishable as any other local directory -- rejecting it is the
    # behaviour that is wanted, not a compromise a future reader should "fix"
    # back. Its own directory name ("foo"), not the sha glued onto it, is what
    # is safe to show.
    sha = "a" * 40
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill=f"/Users/alice/repos/foo@{sha}",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · foo"
    assert "alice" not in label and "/" not in label


@pytest.mark.parametrize("commit", ["main", "v1.2.0"])
def test_a_non_hex_commit_is_treated_as_unresolved(commit):
    # "main" and "v1.2.0" are the kind of thing that sits after an "@" in
    # ordinary text without being a commit sha at all -- neither is lowercase
    # hex, so `_parse_skill` must not treat either as one.
    assert _parse_skill(f"repo@{commit}") is None


def test_a_skill_with_an_empty_repo_before_the_at_sign_is_unresolved():
    # round 3: `_parse_skill` no longer checks "repo non-empty" as its own
    # statement -- an empty repo half fails to parse as an http(s) URL at all
    # (no scheme), so this input class is now covered by the scheme check
    # instead. Kept as its own test since it is still a distinct input, even
    # though it now exercises a different branch than round 2's version did.
    assert _parse_skill("@1234567890") is None


def test_a_non_http_scheme_is_treated_as_unresolved():
    # round 3: only http(s) is ever reconstructed. `git@host:owner/repo`-style
    # remotes are normalised to `https://host/owner/repo` before they ever
    # reach a card (`ParsedGitUrl.normalised_url`), so nothing legitimate is
    # lost by refusing every other scheme.
    assert _parse_skill("ftp://github.com/owner/repo@" + "a" * 40) is None


def test_a_url_with_no_host_is_treated_as_unresolved():
    # round 3: `https:///owner/repo` -- a scheme with an empty authority --
    # has no host to publish at all.
    assert _parse_skill("https:///owner/repo@" + "a" * 40) is None


@pytest.mark.parametrize("path", ["https://github.com/owner", "https://github.com/owner/repo/extra"])
def test_a_url_without_exactly_owner_and_repo_is_treated_as_unresolved(path):
    # round 3: one segment (no repo) and three segments (a GitLab-style
    # subgroup, say) are both refused -- `_parse_skill` only ever names a
    # skill by exactly (host, owner, repo), not by trying to guess which of
    # several segments is the "real" owner or repo.
    assert _parse_skill(f"{path}@{'a' * 40}") is None


def test_a_repo_segment_that_is_only_dot_git_is_treated_as_unresolved():
    # round 3: `.git` is stripped from the repo segment (the same suffix
    # `ParsedGitUrl.normalised_url` strips) -- and a segment that was nothing
    # but that suffix leaves an empty repo name, which is exactly as
    # unpublishable as one that was never there.
    assert _parse_skill("https://github.com/owner/.git@" + "a" * 40) is None


def test_a_dot_git_suffix_on_a_real_repo_name_is_stripped_not_rejected():
    # The positive counterpart to the test above: `.git` is stripped, not
    # treated as poison -- a repo segment that is MORE than just the suffix
    # still resolves normally.
    commit = "a" * 40
    assert _parse_skill(f"https://github.com/a/b.git@{commit}") == ("https", "github.com", "a", "b", commit)


def test_a_non_default_port_is_kept_in_the_parsed_authority():
    # round 4, the severe one: `_parse_skill` read `.hostname` and never
    # `.port`, so a self-hosted remote on a non-standard port -- an ordinary
    # internal GitLab or Gitea, reachable through real code via
    # `ParsedGitUrl.normalised_url`, which returns the http(s) remote verbatim
    # including its port -- published a URL pointing at the default port
    # instead of the one the input actually named. The credential being
    # correctly absent does not make the published endpoint correct.
    commit = "a" * 40
    parsed = _parse_skill(f"https://user:pass@gitlab.internal.example.com:9999/owner/repo@{commit}")
    assert parsed == ("https", "gitlab.internal.example.com:9999", "owner", "repo", commit)


def test_an_ipv6_host_keeps_its_brackets():
    # round 4: `urlsplit(...).hostname` strips the `[...]` an IPv6 literal
    # needs to remain a valid URL authority -- `.hostname` for
    # `https://[::1]/a/b` is the bare `::1`, which `new URL()` (and a Python
    # re-parse) reads as a bare string with colons, not an IPv6 address.
    # The brackets must be added back, not merely left wherever `.hostname`
    # happened to put them (nowhere).
    commit = "a" * 40
    assert _parse_skill(f"https://[::1]/a/b@{commit}") == ("https", "[::1]", "a", "b", commit)


def test_an_ipv6_host_with_a_port_keeps_both():
    commit = "a" * 40
    assert _parse_skill(f"https://[2001:db8::1]:8443/a/b@{commit}") == (
        "https",
        "[2001:db8::1]:8443",
        "a",
        "b",
        commit,
    )


def test_the_published_scheme_matches_the_input_instead_of_being_hardcoded():
    # round 5, closing the exception named in round 4's report: `_skill_ref`
    # used to build every reconstructed URL as `https://...` regardless of
    # whether the input was `http://` -- the same defect class as the
    # dropped port, a reconstruction that points somewhere the input did
    # not. An `http`-only internal remote must publish an `http://` link,
    # not one the server may not answer.
    commit = "a" * 40
    assert _parse_skill(f"http://gitlab.internal.example.com/owner/repo@{commit}") == (
        "http",
        "gitlab.internal.example.com",
        "owner",
        "repo",
        commit,
    )


def test_reconstructing_from_netloc_would_reopen_the_credential_leak():
    # Documents *why* the authority is built from `.hostname` + `.port`
    # rather than from `.netloc` (which already has the brackets and the port
    # in the right place, and would be the "obvious" shortcut): `.netloc`
    # also carries userinfo verbatim when the input had any.
    from urllib.parse import urlsplit

    parsed = urlsplit("https://oauth2:ghp_SECRETTOKEN1234@github.com:9999/owner/repo")
    assert "ghp_SECRETTOKEN1234" in parsed.netloc
    assert "ghp_SECRETTOKEN1234" not in (parsed.hostname or "")


def test_a_malformed_port_is_treated_as_unresolved():
    # `urlsplit(...).port` raises ValueError for a non-numeric or out-of-range
    # port rather than returning None -- a caller that let that propagate
    # would turn a bad port into a crash instead of "this did not resolve".
    assert _parse_skill("https://host:not-a-port/owner/repo@" + "a" * 40) is None


def test_a_control_character_in_the_repo_segment_is_treated_as_unresolved():
    # round 4, cheap and defensive (no real forge allows this in a remote
    # name): a NUL inside an otherwise well-formed segment must not reach
    # `identity.name` or `skills.installed` verbatim.
    assert _parse_skill("https://github.com/owner/repo\x00secret@" + "a" * 40) is None


def test_a_control_character_in_the_owner_segment_is_treated_as_unresolved():
    # Its own test, its own statement in `_parse_skill` -- owner and repo are
    # checked one at a time, not folded into one `or`, so a control character
    # in *either* half is independently exercised.
    assert _parse_skill("https://github.com/own\x00er/repo@" + "a" * 40) is None


def test_the_label_strips_a_control_character_from_an_unresolved_leaf():
    card = SolutionCard(
        shape="acp", shape_version=1, agent="pkg@1", model="sonnet", skill="/Users/alice/my-sk\x00ill"
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "\x00" not in label


def test_a_leaf_that_is_only_control_characters_is_named_literally_skill():
    # A card that installed a skill must not report that it installed none --
    # `SkillRef` requires a `name`, so an empty string is not schema-legal,
    # and would be indistinguishable from "no skill" besides.
    assert _skill_leaf("/Users/alice/\x00\x01\x7f") == "skill"


def test_the_label_never_leaks_credentials_embedded_in_a_skills_remote_url():
    # round 3, the severe one: a git remote with HTTP Basic credentials baked
    # into it is a routine real pattern. `urlsplit` reads them into
    # `.username`/`.password`; `_parse_skill` never looks at either, so they
    # cannot reach the label no matter what they contain -- not because they
    # are detected and stripped, but because they are never read out in the
    # first place.
    commit = "0" * 40
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill=f"https://oauth2:ghp_SECRETTOKEN1234@github.com/owner/repo@{commit}",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · owner/repo@0000000"
    assert "ghp_SECRETTOKEN1234" not in label and "oauth2" not in label


def test_the_label_never_leaks_a_windows_drive_path():
    # round 3: `repo.startswith("/")` (round 2's guard) is POSIX-only --
    # `C:\Users\bob\my-skill` starts with "C", not "/", and would have sailed
    # through it. Reconstruct-don't-forward makes this moot: a bare drive
    # path is not an http(s) URL, full stop, regardless of what character it
    # happens to start with.
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill="C:\\Users\\bob\\my-skill",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "bob" not in label and "\\" not in label and "/" not in label


def test_the_label_never_leaks_a_windows_local_path_remote():
    # The Windows form of round 2's local-path-remote case: a drive path with
    # a commit-shaped tail glued on. Still not an http(s) URL, so the sha is
    # stripped before the leaf is taken, same as the POSIX form.
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill="C:\\Users\\bob\\my-skill@" + "a" * 7,
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "bob" not in label and "\\" not in label and "/" not in label


def test_the_label_never_leaks_a_unc_path():
    # round 3: a UNC share (`\\\\fileserver\\share\\...`) has no leading "/"
    # either, and `_last_segment` (round 1/2) split only on "/" -- neither
    # guard would have caught this on its own.
    card = SolutionCard(
        shape="acp",
        shape_version=1,
        agent="pkg@1",
        model="sonnet",
        skill="\\\\fileserver\\share\\my-skill",
    )
    label = card_label(card)
    assert label == "pkg@1 · sonnet · my-skill"
    assert "fileserver" not in label and "share" not in label and "\\" not in label


def test_the_label_falls_back_to_the_provider_when_there_is_no_agent():
    card = SolutionCard(shape="model", shape_version=1, provider="anthropic", model="claude-sonnet-5")
    assert card_label(card) == "anthropic · claude-sonnet-5"


def test_the_label_falls_back_to_the_bare_shape_when_nothing_else_is_set():
    card = SolutionCard(shape="acp", shape_version=1)
    assert card_label(card) == "acp"


def test_the_label_has_no_repo_segment_without_a_host_and_owner():
    # Deliberate contract change (round 3 of review): a card whose skill is
    # `<name>@<sha>` with no scheme, no host and no owner does not parse into
    # (host, owner, repo, commit) at all -- there is no URL to reconstruct --
    # so it is named by its own leaf alone, the sha dropped entirely, rather
    # than the round-1/2 shape `justarepo@abc1234`.
    card = SolutionCard(shape="model", shape_version=1, model="sonnet", skill="justarepo@abc1234567")
    assert card_label(card) == "model · sonnet · justarepo"


def test_an_explicit_empty_string_is_the_same_card_as_the_field_left_unset():
    empty = SolutionCard(shape="cmd", shape_version=1, cmd="")
    unset = SolutionCard(shape="cmd", shape_version=1)
    assert card_digest(empty) == card_digest(unset)


def test_an_integral_float_in_a_digest_field_digests_like_the_int():
    # model_construct bypasses field validation -- the one path that can still hand
    # _digest_payload a raw float even though `timeout` is typed `int`, e.g. a card
    # rebuilt from a foreign/older caller's already-parsed JSON. The digest must not
    # care: json.dumps(570.0) != json.dumps(570), so without normalisation these two
    # cards would silently mint different digests for the same card.
    as_float = SolutionCard.model_construct(shape="cmd", shape_version=1, cmd="x", timeout=570.0)
    as_int = SolutionCard.model_construct(shape="cmd", shape_version=1, cmd="x", timeout=570)
    assert card_digest(as_float) == card_digest(as_int)


@pytest.mark.parametrize("vector", json.loads(VECTORS.read_text())["vectors"], ids=lambda v: v["id"])
def test_the_published_vectors_hold(vector):
    card = SolutionCard.model_validate(vector["card"])
    assert canonical_card_json(card).decode() == vector["canonical_json"]
    assert card_digest(card) == vector["digest"]

"""The solution card: what was measured, and the content address that names it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trap.models.card import (
    DIGEST_FIELDS,
    SolutionCard,
    _resolved_skill,
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
    # No "@" at all means the skill did not resolve to `repo@sha` -- the same test
    # `_resolved_skill` uses -- so only the last path segment is shown, never the
    # repo path in full (deliberate contract change, round 1 of review).
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
    # a commit separator at all. `_resolved_skill` must tell the two apart by
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
    # hex, so `_resolved_skill` must not treat either as one.
    assert _resolved_skill(f"repo@{commit}") is None


def test_a_skill_with_an_empty_repo_before_the_at_sign_is_unresolved():
    # The third condition the ruling names alongside "has a separator" and "the
    # commit looks like hex": the repo half must also be non-empty. Its own
    # statement, its own test -- a single `sep and repo` combined with the
    # other checks could reach 100% branch coverage without this input ever
    # running, the same lesson as round 1's `sep and commit`.
    assert _resolved_skill("@1234567890") is None


def test_the_label_falls_back_to_the_provider_when_there_is_no_agent():
    card = SolutionCard(shape="model", shape_version=1, provider="anthropic", model="claude-sonnet-5")
    assert card_label(card) == "anthropic · claude-sonnet-5"


def test_the_label_falls_back_to_the_bare_shape_when_nothing_else_is_set():
    card = SolutionCard(shape="acp", shape_version=1)
    assert card_label(card) == "acp"


def test_the_label_handles_a_skill_repo_with_no_slash():
    card = SolutionCard(shape="model", shape_version=1, model="sonnet", skill="justarepo@abc1234567")
    assert card_label(card) == "model · sonnet · justarepo@abc1234"


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

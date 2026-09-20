"""The solution card: what was measured, and the content address that names it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trap.models.card import (
    DIGEST_FIELDS,
    SolutionCard,
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


def test_the_label_falls_back_to_cmd_alone_without_model_or_skill():
    card = SolutionCard(shape="cmd", shape_version=1, cmd="python main.py")
    assert card_label(card) == "python main.py"


def test_the_label_shows_a_skill_with_no_pinned_commit():
    card = SolutionCard(shape="model", shape_version=1, model="sonnet", skill="https://github.com/a/b")
    assert card_label(card) == "model · sonnet · a/b"


@pytest.mark.parametrize("vector", json.loads(VECTORS.read_text())["vectors"], ids=lambda v: v["id"])
def test_the_published_vectors_hold(vector):
    card = SolutionCard.model_validate(vector["card"])
    assert canonical_card_json(card).decode() == vector["canonical_json"]
    assert card_digest(card) == vector["digest"]

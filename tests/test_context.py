"""Tests for the run description tp sends (:mod:`trap.live.context`).

Two properties carry the weight. The patch is built field by field, so nothing
that belongs to a case -- its id, its answer, its stdout -- can get in; the
tests hand it cases with conspicuous ids and answers and grep the wire for
them. And every group is either data or an honest status: a group tp cannot
see is ``unsupported``, one the user switched off is ``disabled``, and one
with nothing to say is absent -- never an empty object the site would record
as collected.
"""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime

import pytest

from trap.live.context import (
    EMITTED_GROUPS,
    SKILLS_UNSUPPORTED,
    TOOLS_UNSUPPORTED,
    agent_from_env,
    build_context,
)
from trap.models.card import SolutionCard, card_digest
from trap.models.cost import CaseCost, ModelCost
from trap.models.environment import Cpu, Environment
from trap.models.provenance import GitProvenance, Provenance
from trap.models.results import CaseResult
from trap.models.trap_yaml import Profile

#: The platform's words and the owner's private columns: refused by the site
#: at any depth. The contract test reads the live list from the web source;
#: this copy keeps the property checked where the patch is built.
REJECTED_KEYS = frozenset(
    {
        "score",
        "passed",
        "verdict",
        "metrics",
        "platform_score",
        "platform_verdict",
        "self_score",
        "self_cost_usd",
        "exec_status",
        "score_status",
        "report_status",
        "cases_done",
        "cases_total",
        "visibility",
        "owner_id",
        "channel",
        "final_run_id",
    }
)

PROFILE = Profile(model=["gpt-5", "claude-4"], framework=["langgraph"])
PROVENANCE = Provenance(
    solution=GitProvenance(repo="https://github.com/o/sol", commit="a" * 40, subdirectory="s"),
    task=GitProvenance(issue="uncommitted changes"),
)
ENVIRONMENT = Environment(
    os="macOS 15.1",
    kernel="Darwin 24.1.0",
    arch="arm64",
    cpu=Cpu(model="Apple M3", cores_physical=8, cores_logical=8),
    memory_total_bytes=17179869184,
)
STARTED = datetime(2026, 9, 8, 10, 0, 0, tzinfo=UTC)
FINISHED = datetime(2026, 9, 8, 10, 0, 12, 500000, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 8, 10, 0, 13, tzinfo=UTC)


def _cost(provider: str, model: str | None, prompt: int, completion: int, usd: float | None, calls: int = 1):
    return ModelCost(
        provider=provider,
        model=model,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=usd,
        calls=calls,
    )


#: Cases with everything a leak would show: telling ids, an answer in the
#: metrics, a path, and costs across two providers, one bucket unpriced.
CASES = (
    CaseResult(
        case_id="secret-case-alpha",
        duration=1.25,
        metrics={"score": 1.0, "answer": "the-answer-text", "stdout": "/tmp/run/secret-case-alpha/stdout"},
        cost=CaseCost(
            by_model=[_cost("openai", "gpt-5", 100, 20, 0.01), _cost("anthropic", "claude-4", 50, 5, 0.002)]
        ),
    ),
    CaseResult(
        case_id="secret-case-beta",
        duration=0.75,
        metrics=None,
        cost=CaseCost(
            by_model=[_cost("openai", "gpt-5", 10, 2, 0.001, calls=2), _cost("openai", None, 7, 3, None)]
        ),
    ),
    CaseResult(case_id="secret-case-gamma", duration=0.5, metrics=None),
)


def _opening(**kwargs):
    defaults = {
        "profile": PROFILE,
        "provenance": PROVENANCE,
        "environment": ENVIRONMENT,
        "trap_version": "1.2.3",
        "observed_at": OBSERVED,
    }
    return build_context(**{**defaults, **kwargs})


def _final(**kwargs):
    return _opening(cases=CASES, started_at=STARTED, finished_at=FINISHED, **kwargs)


def _keys_at_any_depth(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {key for child in value.values() for key in _keys_at_any_depth(child)}
    if isinstance(value, list):
        return {key for item in value for key in _keys_at_any_depth(item)}
    return set()


# -- the envelope ---------------------------------------------------------------


def test_the_envelope_names_tp_as_the_source():
    patch = _opening()
    assert patch["schema_version"] == 1
    assert patch["source"] == "tp"
    assert patch["collector"] == "tp/1.2.3"
    assert patch["observed_at"] == "2026-09-08T10:00:13+00:00"


def test_observed_at_defaults_to_now_in_utc():
    before = datetime.now(UTC).replace(microsecond=0)
    observed = datetime.fromisoformat(
        build_context(profile=Profile(), provenance=Provenance(), environment=None, trap_version="x")[
            "observed_at"
        ]
    )
    assert observed.tzinfo is not None and observed >= before


def test_every_group_the_module_can_emit_is_declared():
    envelope = {"schema_version", "source", "collector", "observed_at"}
    assert set(_final()) - envelope == EMITTED_GROUPS
    assert set(_opening()) - envelope < EMITTED_GROUPS


# -- identity and model ---------------------------------------------------------


def test_tp_is_both_launcher_and_executor_and_frameworks_sit_under_identity():
    assert _opening()["identity"] == {
        "launcher": {"name": "tp", "version": "1.2.3"},
        "executor": {"name": "tp", "version": "1.2.3"},
        "framework": [{"name": "langgraph"}],
    }


def test_the_launching_agent_is_named_when_it_says_so():
    identity = _opening(agent={"name": "claude-code", "version": "2.1"})["identity"]
    assert identity["agent"] == {"name": "claude-code", "version": "2.1"}


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({}, None),
        ({"TRAP_AGENT": "  "}, None),
        ({"TRAP_AGENT": "claude-code"}, {"name": "claude-code"}),
        ({"TRAP_AGENT": "claude-code", "TRAP_AGENT_VERSION": ""}, {"name": "claude-code"}),
        (
            {"TRAP_AGENT": " claude-code ", "TRAP_AGENT_VERSION": "2.1"},
            {"name": "claude-code", "version": "2.1"},
        ),
    ],
)
def test_the_agent_is_read_from_the_environment(environ, expected):
    assert agent_from_env(environ) == expected


def test_declared_models_come_from_trap_yaml_and_say_so():
    assert _opening()["model"] == {
        "declared": [
            {"model": "gpt-5", "role": "solver", "source": "trap.yaml"},
            {"model": "claude-4", "role": "solver", "source": "trap.yaml"},
        ]
    }


def test_a_solution_that_declares_nothing_has_no_model_group():
    patch = build_context(profile=Profile(), provenance=Provenance(), environment=None, trap_version="x")
    assert "model" not in patch  # not reported, rather than reported empty
    assert "framework" not in patch["identity"]


# -- reproducibility and environment ---------------------------------------------


def test_provenance_sits_under_reproducibility_with_the_tp_build():
    assert _opening()["reproducibility"] == {
        "solution": {"repo": "https://github.com/o/sol", "commit": "a" * 40, "subdirectory": "s"},
        "task": {"issue": "uncommitted changes"},
        "trap_version": "1.2.3",
    }


def test_an_empty_provenance_side_is_left_unsaid():
    patch = build_context(profile=Profile(), provenance=Provenance(), environment=None, trap_version="x")
    assert patch["reproducibility"] == {"trap_version": "x"}


def test_a_carded_solutions_command_and_setup_never_reach_reproducibility():
    # The card rides on `GitProvenance.adapter`/`.adapter_digest` for the report, but
    # live sync only ever gets labels and the digest -- never the command template
    # itself, which routinely names paths and environment-variable names (R12).
    card = SolutionCard(
        shape="cmd",
        shape_version=1,
        cmd="python main.py --key $OPENAI_API_KEY {prompt}",
        setup="uv sync",
    )
    provenance = Provenance(
        solution=GitProvenance(
            repo="https://github.com/o/sol",
            commit="a" * 40,
            adapter=card,
            adapter_digest=card_digest(card),
        ),
        task=GitProvenance(issue="uncommitted changes"),
    )
    patch = build_context(profile=PROFILE, provenance=provenance, environment=None, trap_version="1.2.3")
    wire = json.dumps(patch)
    for secret in ("main.py", "OPENAI_API_KEY", "uv sync", "adapter"):
        assert secret not in wire, secret
    assert patch["reproducibility"]["solution"] == {"repo": "https://github.com/o/sol", "commit": "a" * 40}


def test_the_environment_keeps_the_detectors_shape_and_adds_the_interpreter():
    assert _opening()["environment"] == {
        "os": "macOS 15.1",
        "kernel": "Darwin 24.1.0",
        "arch": "arm64",
        "cpu": {"model": "Apple M3", "cores_physical": 8, "cores_logical": 8},
        "memory_total_bytes": 17179869184,
        "runtime": {"python": platform.python_version()},
    }


def test_failed_probes_are_left_out_rather_than_sent_as_null():
    environment = _opening(environment=Environment())["environment"]
    assert environment == {"runtime": {"python": platform.python_version()}}


def test_no_environment_is_reported_as_disabled_not_missing():
    patch = _opening(environment_enabled=False)
    assert patch["environment"] == {"status": "disabled", "reason": "--no-environment"}


def test_a_detection_that_failed_says_nothing_about_the_environment():
    assert "environment" not in _opening(environment=None)


def test_skills_and_tools_are_unsupported_with_a_reason():
    patch = _opening()
    assert patch["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}
    assert patch["tools"] == {"status": "unsupported", "reason": TOOLS_UNSUPPORTED}


# -- timing and usage: the closing description ----------------------------------


def test_the_opening_description_has_no_timing_or_usage():
    patch = _opening()
    assert "timing" not in patch and "usage" not in patch


def test_timing_sums_the_solver_and_measures_the_wall():
    assert _final()["timing"] == {
        "solver_ms": 2500,
        "wall_ms": 12500,
        "started_at": "2026-09-08T10:00:00+00:00",
        "finished_at": "2026-09-08T10:00:12+00:00",
    }


def test_timing_without_the_run_clock_has_only_solver_time():
    assert _opening(cases=CASES)["timing"] == {"solver_ms": 2500}


def test_a_wall_clock_that_went_backwards_is_not_a_negative_duration():
    timing = _opening(cases=CASES, started_at=FINISHED, finished_at=STARTED)["timing"]
    assert timing["wall_ms"] == 0


def test_usage_is_folded_per_provider_and_model_over_the_run():
    by_model = _final()["usage"]["by_model"]
    assert [(entry["provider"], entry["model"]) for entry in by_model] == [
        ("openai", "gpt-5"),
        ("anthropic", "claude-4"),
        ("openai", "unknown"),
    ]
    gpt, claude, unknown = by_model
    assert gpt == {
        "model": "gpt-5",
        "provider": "openai",
        "source": "tp-cost-proxy",
        "input": 110,
        "output": 22,
        "cache_read": 0,
        "cache_creation": 0,
        "calls": 3,
        "cost_usd_reported": pytest.approx(0.011),
    }
    assert claude["cost_usd_reported"] == pytest.approx(0.002) and claude["calls"] == 1
    # The bucket whose response named no model still spent the tokens.
    assert (unknown["input"], unknown["output"]) == (7, 3)


def test_cache_tokens_are_described_in_the_sites_own_split():
    # The site's usage entry is Anthropic's split -- `input` excludes the cache, which has its
    # own `cache_read` / `cache_creation` -- the same four disjoint counts the proxy records.
    def cached(prompt: int, read: int, write: int, usd: float) -> ModelCost:
        return ModelCost(
            provider="anthropic",
            model="claude-opus-5",
            prompt_tokens=prompt,
            completion_tokens=10,
            cache_read_tokens=read,
            cache_write_tokens=write,
            cost_usd=usd,
            cache_cost_usd=usd / 2,
            calls=1,
        )

    cases = [
        CaseResult(case_id="a", metrics=None, cost=CaseCost(by_model=[cached(5, 900, 100, 0.2)])),
        CaseResult(case_id="b", metrics=None, cost=CaseCost(by_model=[cached(3, 1000, 0, 0.1)])),
    ]
    (entry,) = _opening(cases=cases)["usage"]["by_model"]
    assert (entry["input"], entry["cache_read"], entry["cache_creation"], entry["output"]) == (
        8,
        1900,
        100,
        20,
    )
    assert entry["cost_usd_reported"] == pytest.approx(0.3)  # the whole spend, cache included


def test_an_unpriced_call_leaves_the_buckets_cost_unsaid():
    priced_and_not = CaseResult(
        case_id="c",
        metrics=None,
        cost=CaseCost(by_model=[_cost("openai", "gpt-5", 1, 1, 0.5), _cost("openai", "gpt-5", 1, 1, None)]),
    )
    (entry,) = _opening(cases=[priced_and_not])["usage"]["by_model"]
    assert "cost_usd_reported" not in entry  # unknown is not zero
    assert (entry["input"], entry["output"], entry["calls"]) == (2, 2, 2)


def test_no_cost_is_reported_as_disabled_in_both_descriptions():
    disabled = {"status": "disabled", "reason": "--no-cost"}
    assert _opening(cost_enabled=False)["usage"] == disabled
    assert _final(cost_enabled=False)["usage"] == disabled


def test_a_run_the_proxy_saw_nothing_of_says_nothing_about_usage():
    unpriced = [CaseResult(case_id="c", metrics=None), CaseResult(case_id="d", metrics=None, cost=CaseCost())]
    assert "usage" not in _opening(cases=unpriced)


# -- what never leaves -----------------------------------------------------------


def test_nothing_that_belongs_to_a_case_is_on_the_wire():
    wire = json.dumps(_final(agent={"name": "claude-code"}))
    for secret in ("secret-case", "alpha", "beta", "gamma", "the-answer-text", "/tmp/", "stdout"):
        assert secret not in wire, secret


def test_no_key_the_site_refuses_appears_at_any_depth():
    for patch in (_opening(agent={"name": "a", "version": "1"}), _final(), _final(cost_enabled=False)):
        assert not (_keys_at_any_depth(patch) & REJECTED_KEYS)


# -- the solution card: labels only, never the command that drove the run -------

SKILL = "https://github.com/a/b@0123456789abcdef0123456789abcdef01234567"
ACP_CARD = SolutionCard(
    shape="acp",
    shape_version=1,
    agent="claude-agent-acp@0.76.0",
    model="sonnet",
    options={"effort": "low"},
    skill=SKILL,
)


def _context(**kwargs):
    return build_context(
        profile=PROFILE, provenance=PROVENANCE, environment=None, trap_version="1.2.3", **kwargs
    )


def test_a_card_names_the_run_and_states_its_options_and_skill():
    patch = _context(card=ACP_CARD)
    assert patch["identity"]["name"] == "claude-agent-acp@0.76.0 · sonnet · a/b@0123456"
    assert patch["model"]["config"] == {"effort": "low"}
    assert patch["skills"] == {
        "installed": [
            {
                "name": "b",
                "repo": "https://github.com/a/b",
                "commit": "0123456789abcdef0123456789abcdef01234567",
            }
        ]
    }


def test_an_acp_run_without_a_skill_says_so_rather_than_unsupported():
    patch = _context(card=ACP_CARD.model_copy(update={"skill": None}))
    assert patch["skills"] == {"installed": []}


def test_a_card_that_is_not_acp_leaves_skills_unsupported():
    patch = _context(card=SolutionCard(shape="model", shape_version=1, provider="anthropic", model="m"))
    assert patch["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}


def test_without_a_card_the_context_is_what_it_was():
    patch = _context()
    assert "name" not in patch["identity"]
    assert patch["skills"] == {"status": "unsupported", "reason": SKILLS_UNSUPPORTED}
    assert "config" not in patch["model"]


def test_a_long_label_is_cut_to_what_the_site_stores():
    patch = _context(card=ACP_CARD.model_copy(update={"name": "x" * 200}))
    assert len(patch["identity"]["name"]) == 120


def test_a_skill_not_resolved_to_repo_at_sha_is_named_by_its_directory():
    # `card_from_run` leaves a non-git skill directory exactly as it found it --
    # no "@", so there is no commit to report and the last path segment is all
    # that is left to call it.
    card = ACP_CARD.model_copy(update={"skill": "/Users/x/checkout/skills/my-skill"})
    patch = _context(card=card)
    assert patch["skills"] == {"installed": [{"name": "my-skill"}]}


def test_a_skill_with_an_at_sign_but_no_commit_is_still_unresolved():
    # A third input class distinct from "no @ at all" (above) and "repo@sha"
    # (test_a_card_names_the_run_...): `_resolved_skill` treats an "@" with
    # nothing after it as unresolved too. Written as its own test rather than
    # folded into the two above, because a single compound `if sep and commit`
    # can reach 100% branch coverage without this combination ever running --
    # coverage.py's branch tracking sees the whole expression's outcome, not
    # which half of it produced a False.
    card = ACP_CARD.model_copy(update={"skill": "some/repo@"})
    patch = _context(card=card)
    assert patch["skills"] == {"installed": [{"name": "repo@"}]}


def test_identity_name_never_leaks_an_unresolved_skills_directory():
    # The critical case (round 1 of review): an ACP card's skill is a local
    # directory that never resolved to a git remote -- a routine outcome, not an
    # edge case -- so `identity.name` must show neither the parent directories
    # (routinely a real username) nor any "/" from the path at all.
    card = ACP_CARD.model_copy(update={"skill": "/Users/alice/checkout/skills/my-skill"})
    patch = _context(card=card)
    name = patch["identity"]["name"]
    assert "alice" not in name
    assert "/" not in name


def test_skill_ref_and_card_label_agree_on_an_unresolved_skill():
    # The two surfaces that both name a skill -- `skills.installed` (built from
    # `_skill_ref`) and `identity.name` (built from `card_label`) -- must never
    # disagree about what an unresolved skill is called. Calling both directly on
    # the same string is what actually proves agreement, rather than each being
    # separately correct by coincidence.
    from trap.live.context import _skill_ref
    from trap.models.card import card_label

    skill = "/Users/alice/checkout/skills/my-skill"
    card = SolutionCard(shape="acp", shape_version=1, skill=skill)
    ref = _skill_ref(skill)
    label = card_label(card)
    assert ref == {"name": "my-skill"}
    assert label == "acp · my-skill"
    assert label.endswith(ref["name"])
    assert "alice" not in label


def test_a_card_without_options_adds_no_model_config():
    # The card that is not ACP (above) also carries no options -- covers the
    # branch where a card exists but `card.options` is empty, distinctly from
    # "no card at all".
    patch = _context(card=SolutionCard(shape="model", shape_version=1, provider="anthropic", model="m"))
    assert "config" not in patch["model"]


def test_a_carded_run_with_no_declared_model_still_gets_its_config():
    # `patch.setdefault("model", {})` must create the group itself when
    # `profile.model` is empty -- the ACP_CARD tests above only exercise the
    # branch where `model` already exists from a `trap.yaml` declaration.
    patch = build_context(
        profile=Profile(), provenance=PROVENANCE, environment=None, trap_version="1.2.3", card=ACP_CARD
    )
    assert patch["model"] == {"config": {"effort": "low"}}


def test_the_command_template_never_reaches_the_run_context():
    # Unlike test_a_carded_solutions_command_and_setup_never_reach_reproducibility
    # above (which only checks the `reproducibility` group), this walks the whole
    # patch: a `cmd`-shaped card with no name has nothing safe to call itself but
    # its shape, because `card_label`'s own fallback chain (agent, provider, cmd,
    # shape) would otherwise hand the command line straight to `identity.name`.
    card = SolutionCard(
        shape="cmd", shape_version=1, cmd="python main.py --key $OPENAI_API_KEY {prompt}", setup="uv sync"
    )
    patch = _context(card=card)
    wire = json.dumps(patch)
    assert "OPENAI_API_KEY" not in wire and "main.py" not in wire and "uv sync" not in wire
    assert patch["identity"]["name"] == "cmd"

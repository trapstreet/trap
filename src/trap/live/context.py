"""What tp says a run was made of: the RunContext patch (§20.4).

Beside a run's score the site keeps a description of it -- who ran it, on what
machine, with which model, from which commits, and what it cost -- as a
merge-only record that several reporters may add to without an order between
them. tp describes a run twice: once when it opens (identity, the declared
model, the environment, the provenance) and once when it ends (timing, usage).
A later patch adds to the record; it never replaces it, and an absent group is
shown by the site as *not reported*, never as zero.

Like the tracker, this module is a translation rather than a filter. Every
field is built by name from a value this module computed, so nothing that
belongs to a case -- its id, its name, its answer, its stdout, a path -- can
get in by being attached to the wrong object. A group tp cannot see is said to
be ``unsupported`` with the reason: tp runs a solver process and does not watch
inside it, so ``tools`` is always this way, and so is ``skills`` for a solution
that is not a built-in shape. A solution card (§5.1), when there is one, can
name the run itself (``identity.name``), the options that took effect
(``model.config``), and whether an ACP run installed a skill (``skills``) --
labels only, never the card's own command line (R12; see ``build_context``'s
``card`` parameter). The label itself is never a filesystem path either: a
skill that never resolved to a git remote is named by its own last path
segment, not the directories that hold it (`card_label`, `trap.models.card`,
carries this guarantee -- ``identity.name`` is a plain, unguarded call to it).
A group the user switched off is ``disabled`` with the flag that did it. The
site shows all three as what they are.
"""

from __future__ import annotations

import platform
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from trap.models.card import SolutionCard, _last_segment, _resolved_skill, card_label
from trap.models.cost import ModelCost, combine_costs
from trap.models.environment import Environment
from trap.models.provenance import Provenance
from trap.models.results import CaseResult
from trap.models.trap_yaml import Profile

#: The shape this module speaks. The server refuses any other with a 400.
SCHEMA_VERSION = 1

#: Who is describing the run. Every model claim and usage entry names its own
#: source too, because a harness hook may add observed models to the same run.
SOURCE = "tp"
DECLARED_SOURCE = "trap.yaml"
USAGE_SOURCE = "tp-cost-proxy"

#: A usage bucket whose response named no model still spent the tokens.
UNKNOWN_MODEL = "unknown"

SKILLS_UNSUPPORTED = "tp runs a solver process; skills are a harness concept"
TOOLS_UNSUPPORTED = "tp does not observe the solver's tool calls"

#: The environment variables an agent that launches tp may set to name itself.
AGENT_ENV = "TRAP_AGENT"
AGENT_VERSION_ENV = "TRAP_AGENT_VERSION"

#: Every top-level group this module can emit. The contract test checks each
#: one against the server's ``CONTEXT_GROUPS``, and walks a fully populated
#: patch for any key the server would refuse.
EMITTED_GROUPS = frozenset(
    {
        "identity",
        "model",
        "environment",
        "reproducibility",
        "skills",
        "tools",
        "timing",
        "usage",
    }
)


def agent_from_env(environ: Mapping[str, str]) -> dict[str, str] | None:
    """The agent that launched tp, when it said so through the environment.

    ``TRAP_AGENT`` names it; ``TRAP_AGENT_VERSION`` is optional. Blank counts
    as unset. tp cannot verify either -- they are recorded as declared.
    """
    name = environ.get(AGENT_ENV, "").strip()
    if not name:
        return None
    agent = {"name": name}
    version = environ.get(AGENT_VERSION_ENV, "").strip()
    if version:
        agent["version"] = version
    return agent


def build_context(
    *,
    profile: Profile,
    provenance: Provenance,
    environment: Environment | None,
    trap_version: str,
    agent: Mapping[str, str] | None = None,
    cases: Sequence[CaseResult] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    cost_enabled: bool = True,
    environment_enabled: bool = True,
    observed_at: datetime | None = None,
    card: SolutionCard | None = None,
) -> dict[str, Any]:
    """The patch describing one run, ready to send.

    Without ``cases`` it is the opening description: what is known before the
    first case runs. With them it is the closing one, which adds how long the
    solver took and what the cost proxy saw. ``environment`` is the detector's
    result (None when detection failed), and the two ``*_enabled`` flags are
    the run's ``--no-environment`` / ``--no-cost`` switches, which are reported
    as *disabled* rather than left unsaid.

    ``card`` is the solution card (§5.1) a built-in shape printed, when there
    was one -- known only once the run has finished, so only the closing call
    ever has one. It contributes *labels* only: the run's name, the options
    that took effect, and an ACP skill's ``repo@sha``. Its command template and
    setup line never appear here (R12) -- that is the one thing an explicit
    ``tp submit`` is for.
    """
    tp = {"name": "tp", "version": trap_version}
    patch: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "collector": f"tp/{trap_version}",
        "observed_at": _iso(observed_at or datetime.now(UTC)),
        "identity": _identity(profile, tp, agent, card),
    }
    if profile.model:
        patch["model"] = {"declared": [_declared(model) for model in profile.model]}
    if card is not None and card.options:
        patch.setdefault("model", {})["config"] = dict(sorted(card.options.items()))
    if not environment_enabled:
        patch["environment"] = _disabled("--no-environment")
    elif environment is not None:
        patch["environment"] = _environment(environment)
    patch["reproducibility"] = _reproducibility(provenance, trap_version)
    patch["skills"] = _skills(card)
    patch["tools"] = _unsupported(TOOLS_UNSUPPORTED)
    if cases is not None:
        patch["timing"] = _timing(cases, started_at, finished_at)
    if not cost_enabled:
        patch["usage"] = _disabled("--no-cost")
    elif cases is not None and (by_model := _usage_by_model(cases)):
        patch["usage"] = {"by_model": by_model}
    return patch


def _identity(
    profile: Profile, tp: dict[str, str], agent: Mapping[str, str] | None, card: SolutionCard | None
) -> dict[str, Any]:
    """tp launched the solver and tp ran it; the frameworks are what trap.yaml
    declared, and the agent is whatever launched tp, if it said.

    A card names the run too, when there is one -- a plain call, with no
    stripping or guarding at this layer. `card_label` (`trap.models.card`) is
    safe to publish as it stands: never a command template, never a filesystem
    path. (An earlier version of this function stripped ``cmd``/``setup`` from
    the card here instead, before handing it to `card_label` -- a guard in the
    wrong place, since it protected only this one caller and left `card_label`
    itself unsafe for the next one. The guard now lives in `card_label`, the
    only place that can make it hold for every caller.)
    """
    identity: dict[str, Any] = {"launcher": tp, "executor": tp}
    if profile.framework:
        identity["framework"] = [{"name": name} for name in profile.framework]
    if agent:
        identity["agent"] = dict(agent)
    if card is not None:
        identity["name"] = card_label(card)[:120]
    return identity


def _declared(model: str) -> dict[str, str]:
    """A model trap.yaml says the solver uses. Declared, not observed: the site
    keeps the two apart, and a harness hook may add what it actually saw."""
    return {"model": model, "role": "solver", "source": DECLARED_SOURCE}


def _environment(environment: Environment) -> dict[str, Any]:
    """The detector's nested shape, minus the probes that failed, plus the
    interpreter tp itself runs under."""
    dumped = environment.model_dump(exclude_none=True)
    group: dict[str, Any] = {key: value for key, value in dumped.items() if value != {}}
    group["runtime"] = {"python": platform.python_version()}
    return group


def _reproducibility(provenance: Provenance, trap_version: str) -> dict[str, Any]:
    """Both checkouts as the report records them -- {repo, commit, subdirectory},
    or the ``issue`` that kept a side from being anchored -- and the tp build.

    ``GitProvenance.adapter``/``.adapter_digest`` (the solution card and its digest)
    are deliberately left out here: this group travels on every live-sync patch, but
    the card's own fields -- a command template, a setup line -- routinely name paths
    and environment-variable names, and R12 makes that upload happen only on an
    explicit `tp submit`, after the user is shown what becomes public. A later group
    may carry the card's *labels* on purpose; this one never carries the card itself.
    """
    group: dict[str, Any] = {}
    for side in ("solution", "task"):
        ref = getattr(provenance, side).model_dump(exclude_none=True, exclude={"adapter", "adapter_digest"})
        if ref:
            group[side] = ref
    group["trap_version"] = trap_version
    return group


def _timing(
    cases: Sequence[CaseResult], started_at: datetime | None, finished_at: datetime | None
) -> dict[str, Any]:
    """Solver time is the sum of the cases' own durations; wall time is the
    run's, when both ends are known."""
    timing: dict[str, Any] = {"solver_ms": round(sum(case.duration for case in cases) * 1000)}
    if started_at is not None:
        timing["started_at"] = _iso(started_at)
    if finished_at is not None:
        timing["finished_at"] = _iso(finished_at)
    if started_at is not None and finished_at is not None:
        timing["wall_ms"] = max(0, round((finished_at - started_at).total_seconds() * 1000))
    return timing


def _usage_by_model(cases: Sequence[CaseResult]) -> list[dict[str, Any]]:
    """What the cost proxy saw, folded over the run per (provider, model)."""
    grouped: dict[tuple[str, str], list[ModelCost]] = {}
    for case in cases:
        for cost in case.cost.by_model if case.cost is not None else ():
            grouped.setdefault((cost.provider, cost.model or UNKNOWN_MODEL), []).append(cost)
    return [_usage_entry(provider, model, costs) for (provider, model), costs in grouped.items()]


def _usage_entry(provider: str, model: str, costs: Sequence[ModelCost]) -> dict[str, Any]:
    """One bucket. The reported cost is the proxy's own pricing, and it is only
    a sum when every call in the bucket was priced -- an unknown is not a zero,
    so one unpriced call leaves the bucket's cost unsaid (the site prices the
    tokens itself either way). The token counts are the site's own split, which is
    the proxy's: ``input`` is the uncached input, the cache has its own two counts."""
    entry: dict[str, Any] = {
        "model": model,
        "provider": provider,
        "source": USAGE_SOURCE,
        "input": sum(cost.prompt_tokens for cost in costs),
        "output": sum(cost.completion_tokens for cost in costs),
        "cache_read": sum(cost.cache_read_tokens for cost in costs),
        "cache_creation": sum(cost.cache_write_tokens for cost in costs),
        "calls": sum(cost.calls for cost in costs),
    }
    reported = combine_costs(*(cost.cost_usd for cost in costs))
    if reported is not None:
        entry["cost_usd_reported"] = reported
    return entry


def _skills(card: SolutionCard | None) -> dict[str, Any]:
    """What the card says was installed. Only an ACP card can install a skill, so
    only the card knows whether one skill is installed, none was, or the shape --
    ``model`` or ``cmd`` -- has no such concept at all. Without a card this is
    exactly as unreachable as it always was."""
    if card is None:
        return _unsupported(SKILLS_UNSUPPORTED)
    if card.skill:
        return {"installed": [_skill_ref(card.skill)]}
    if card.shape == "acp":
        return {"installed": []}
    return _unsupported(SKILLS_UNSUPPORTED)


def _skill_ref(skill: str) -> dict[str, str]:
    """``repo@sha`` names the skill by the last path segment of ``repo``, with
    the commit alongside it; any other string (a skill directory `card_from_run`
    could not resolve to a repo) is named by its own last path segment instead.
    Shares its resolution test (`_resolved_skill`, `trap.models.card`) with
    `card_label`, so the name shown here and the name shown in
    ``identity.name`` for the same skill never disagree."""
    resolved = _resolved_skill(skill)
    if resolved is None:
        return {"name": _last_segment(skill)}
    repo, commit = resolved
    return {"name": _last_segment(repo), "repo": repo, "commit": commit}


def _disabled(flag: str) -> dict[str, str]:
    return {"status": "disabled", "reason": flag}


def _unsupported(reason: str) -> dict[str, str]:
    return {"status": "unsupported", "reason": reason}


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")

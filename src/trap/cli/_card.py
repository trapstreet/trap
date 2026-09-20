"""Read back the card a shape printed, and finish the parts only the CLI can know.

The shape prints labels about itself; it cannot say where a skill came from, because
it only ever saw a directory. Here the skill's directory is resolved to `repo@sha` --
the same handle the site uses for everything else -- so "this skill on sonnet" is
reproducible by someone who has neither the directory nor the run."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from trap.git_ops import LocalRepo
from trap.models.card import SolutionCard
from trap.runner.layout import CaseLayout
from trap.shapes._case import CARD_PREFIX


def card_from_run(run_dir: Path, case_ids: Sequence[str]) -> SolutionCard | None:
    """The card of the first case that printed one, or None for a solution that is not
    a built-in shape. A malformed line is ignored rather than failing the run: the run
    happened, and a report without a card is still a true report."""
    for case_id in case_ids:
        stderr = CaseLayout.for_case(run_dir, case_id).solution_capture.stderr
        try:
            lines = [line for line in stderr.read_text().splitlines() if line.startswith(CARD_PREFIX)]
        except OSError:
            continue
        for line in reversed(lines):
            try:
                card = SolutionCard.model_validate_json(line[len(CARD_PREFIX) :])
            except (ValidationError, json.JSONDecodeError):
                continue
            return _with_skill_origin(card)
    return None


def _with_skill_origin(card: SolutionCard) -> SolutionCard:
    """A carded skill is a local directory; say where that directory came from."""
    if not card.skill:
        return card
    git = LocalRepo.provenance_of(Path(card.skill))
    if not git.repo or not git.commit:
        return card
    return card.model_copy(update={"skill": f"{git.repo}@{git.commit}"})

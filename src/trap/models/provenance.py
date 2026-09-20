from __future__ import annotations

from pydantic import BaseModel, Field

from trap.models.card import SolutionCard


class GitProvenance(BaseModel):
    """Git origin of one checkout: {repo, commit, subdirectory}. repo and commit are
    None when the tree isn't a clean, remote-backed git repo — `issue` then names why
    (see LocalRepo.provenance_issue). Anchored checkouts carry no issue.

    `subdirectory` locates the checkout within the repo (None at the root), so two
    solutions or tasks living in different subdirectories of one repo stay distinct."""

    repo: str | None = None
    commit: str | None = None
    subdirectory: str | None = None
    issue: str | None = None
    #: Set on the solution side only: how the solution was driven (`SolutionCard`), and
    #: the content address the site stores beside repo and commit so two configurations
    #: of one repository stay two solutions. Absent for a solution that carries its own
    #: `trap.yaml` -- there the code and the configuration travel in the same commit.
    adapter: SolutionCard | None = None
    adapter_digest: str | None = None


class Provenance(BaseModel):
    """The two checkouts that fully reproduce a run: the solution under test and the
    task it ran against. Re-clone both at their commit and everything else (cmd,
    judge, fixtures, ...) is recovered from the checkouts."""

    solution: GitProvenance = Field(default_factory=GitProvenance)
    task: GitProvenance = Field(default_factory=GitProvenance)

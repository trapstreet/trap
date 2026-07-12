from __future__ import annotations

from pydantic import BaseModel, Field


class GitProvenance(BaseModel):
    """Git origin of one checkout: {repo, commit}. Both None when the tree isn't a
    clean, remote-backed git repo — `issue` then names why (see
    LocalRepo.provenance_issue). Anchored checkouts carry no issue."""

    repo: str | None = None
    commit: str | None = None
    issue: str | None = None


class Provenance(BaseModel):
    """The two checkouts that fully reproduce a run: the solution under test and the
    task it ran against. Re-clone both at their commit and everything else (cmd,
    judge, fixtures, ...) is recovered from the checkouts."""

    solution: GitProvenance = Field(default_factory=GitProvenance)
    task: GitProvenance = Field(default_factory=GitProvenance)

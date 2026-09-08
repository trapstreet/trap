"""Site grading from `tp run`: tp submits the answers, the site judges them.

For a task the site has admitted as an evaluation, the local judge is only a
preview. Each case's answer -- the solver's stdout, nothing else -- goes to the
site as the case finishes, and the site scores it with the task's own judge
against reference answers that never leave its grading worker. The result the
leaderboard trusts is the site's; what this run's report says is what the
local judge thought.

The rules that shape this module:

*Fail-open, always.* No network, a refused revision, a lost connection midway:
the run proceeds exactly as it would have, the report is saved, the exit code
is what the local run earned. The only trace is one line saying what happened.

*No queue.* An answer the site did not take when the case finished is not kept
for later. Grading is a conversation with a live server; a run that starts
offline is graded locally and only locally, and says so.

*One run on the site per run here.* The graded run's id is derived from the
live-sync session's (``<client_run_id>-site``): the site keys a session by
(owner, client_run_id) across BOTH channels, so the same id would collide with
the private progress session rather than join it. The report carries the
graded run's id and URL under ``site_grading``, which is the join. When there
is no live session, a fresh id is minted for the graded run alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from trap.auth.resolve import ResolvedAuth
from trap.auth.store import CredentialStore, CredentialStoreError
from trap.live.client import LiveApiError, LiveClient
from trap.live.delivery import tp_runtime
from trap.live.identity import new_client_run_id
from trap.models.provenance import GitProvenance
from trap.models.report import SiteGrading
from trap.models.results import CaseResult

#: The server-side channel a graded run must be on. Anything else means the id
#: landed on a session the site will not grade, and answers must not be sent.
GRADED_CHANNEL = "platform_grading"


def site_grading_disabled_by_env() -> bool:
    """``TRAP_NO_SITE_GRADING`` keeps every run in this environment locally judged."""
    return os.environ.get("TRAP_NO_SITE_GRADING", "").strip().lower() in {"1", "true", "yes", "on"}


class SiteGrader:
    """Submits one run's answers to its graded run on the site, case by case.

    Synchronous on purpose: a submission is a short request with a short
    timeout, made between one case and the next, and the first failure turns
    the grader off for the rest of the run. That bounds what a slow server can
    cost the run to one timeout, and keeps the answer path free of threads.
    """

    def __init__(
        self,
        *,
        client: LiveClient,
        answer_of: Callable[[str], str],
        run_id: str | None = None,
        url: str | None = None,
        notice: str | None = None,
    ) -> None:
        self._client = client
        self._run_id = run_id
        self._url = url
        self._answer_of = answer_of
        self._notice = notice
        self._off = run_id is None
        self._submitted = 0

    @property
    def opened(self) -> bool:
        """Whether the site opened a graded run for this one. False is the fail-open
        outcome: nothing will be submitted, and ``notice`` says why."""
        return self._run_id is not None

    @property
    def url(self) -> str | None:
        return self._url

    @property
    def notice(self) -> str | None:
        """One line for the user, or None. The first thing worth saying is kept."""
        return self._notice

    def summary(self) -> SiteGrading | None:
        """The block the report carries: where the site's verdicts live. Recorded
        whenever a graded run was opened, even if contact was lost later -- the
        run exists on the site either way."""
        if self._run_id is None or self._url is None:
            return None
        return SiteGrading(run_id=self._run_id, url=self._url)

    def on_case_done(self, result: CaseResult) -> None:
        """Submit this case's answer. Never raises; the first failure ends submissions."""
        if self._off or self._run_id is None:
            return
        try:
            answer = self._answer_of(result.case_id)
        except OSError as e:
            self._stop(f"site grading: could not read the answer for case {self._submitted + 1} ({e})")
            return
        try:
            self._client.submit_answers(self._run_id, [self._submission(result, answer)])
        except LiveApiError as e:
            self._stop(
                f"site grading: lost contact with the site after {self._submitted} answer(s) ({e}); "
                "the remaining answers were not submitted — the site's run stays unfinished"
            )
            return
        self._submitted += 1

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _submission(result: CaseResult, answer: str) -> dict[str, Any]:
        """The report's own ``cases_results`` shape, plus what the site records as
        self-declared: timing and cost, labelled as the client's word."""
        reported: dict[str, Any] = {"duration_ms": int(result.duration * 1000)}
        if result.cost is not None and result.cost.cost_usd is not None:
            reported["cost_usd"] = result.cost.cost_usd
        return {
            "case_id": result.case_id,
            "answer": answer,
            "duration": result.duration,
            "exit_code": result.exit_code,
            "client_reported": reported,
        }

    def _stop(self, notice: str) -> None:
        self._off = True
        if self._notice is None:
            self._notice = notice


def start_site_grading(
    *,
    task: GitProvenance,
    cases_total: int,
    answer_of: Callable[[str], str],
    client_run_id: str | None = None,
    server_override: str | None = None,
    enabled: bool = True,
) -> SiteGrader | None:
    """Open a graded run on the site for this task, or return None with nothing said.

    None is the quiet path: switched off, not paired, a task with no git anchor
    (the site cannot know which task it is), or a task the site has not admitted
    for grading -- all ordinary, none worth a line. A server that *should* have
    answered but could not is the one case worth a note, and even then the run
    is unaffected: site grading is simply off for it.
    """
    if not enabled or site_grading_disabled_by_env():
        return None
    if task.repo is None or task.commit is None:
        return None
    try:
        auth = ResolvedAuth.resolve(CredentialStore(), server_override)
    except CredentialStoreError:
        return None
    if not auth.api_key:
        return None

    client = LiveClient(auth.server, auth.api_key)
    grader = _open(
        client,
        repo=task.repo,
        commit=task.commit,
        path=task.subdirectory,
        cases_total=cases_total,
        answer_of=answer_of,
        client_run_id=f"{client_run_id}-site" if client_run_id else new_client_run_id(),
    )
    if grader is None:
        client.close()
    return grader


def _open(
    client: LiveClient,
    *,
    repo: str,
    commit: str,
    path: str | None,
    cases_total: int,
    answer_of: Callable[[str], str],
    client_run_id: str,
) -> SiteGrader | None:
    """Resolve the revision and open the run. A grader that never opened, with a
    notice, is the fail-open outcome for a server that answered wrongly or not
    at all; None is the quiet one."""
    try:
        revision = client.resolve_evaluation(repo=repo, commit=commit, path=path)
    except LiveApiError as e:
        if e.status == 404:
            return None  # no admitted evaluation for this task: the ordinary case
        return _unavailable(client, answer_of, _why(e, f"could not resolve the task ({e})"))
    revision_id = revision.get("revision_id")
    if revision.get("admitted") is not True or not isinstance(revision_id, str):
        return None

    try:
        opened = client.open_evaluation(
            revision_id=revision_id, client_run_id=client_run_id, runtime=tp_runtime()
        )
    except LiveApiError as e:
        return _unavailable(client, answer_of, _why(e, f"could not open a graded run ({e})"))
    run = opened.get("run")
    run_id = run.get("id") if isinstance(run, dict) else None
    if not isinstance(run, dict) or not isinstance(run_id, str):
        return _unavailable(client, answer_of, "the site answered without a run id")
    if run.get("channel", GRADED_CHANNEL) != GRADED_CHANNEL:
        # The id already names a session the site will not grade. Sending
        # answers there would be refused one by one; say it once instead.
        return _unavailable(client, answer_of, "the site holds this run id as a non-graded session")

    url = opened.get("view_url")
    if not isinstance(url, str):
        url = f"{client.server}/runs/{run_id}"
    notice = None
    site_total = revision.get("cases_total")
    if isinstance(site_total, int) and site_total != cases_total:
        notice = (
            f"site grading: the site's evaluation has {site_total} case(s) and this run covers "
            f"{cases_total} — the graded run stays unfinished until every case is answered"
        )
    return SiteGrader(client=client, run_id=run_id, url=url, answer_of=answer_of, notice=notice)


def _why(error: LiveApiError, fallback: str) -> str:
    """A server that refuses this build says so in its own words -- they name the
    install command -- and those are worth more than ``http 426``."""
    if error.client_too_old:
        return error.server_message or "this server needs a newer tp"
    return fallback


def _unavailable(client: LiveClient, answer_of: Callable[[str], str], reason: str) -> SiteGrader:
    """A grader that will submit nothing and says why, once. The run is judged
    locally, and nothing is kept to send later."""
    notice = f"site grading off for this run: {reason} — answers are judged locally only"
    return SiteGrader(client=client, answer_of=answer_of, notice=notice)

"""Deciding whether to track a run, and wiring one up if so.

Kept apart from the tracker so that `tp run` has exactly one thing to call and
exactly one thing to check: a tracker, or None. Every reason not to track --
switched off, not paired, an unwritable workspace -- resolves here, and none of
them is an error.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from trap.auth.resolve import ResolvedAuth
from trap.auth.store import CredentialStore, CredentialStoreError
from trap.live.client import LiveClient
from trap.live.identity import LiveSession, new_client_run_id
from trap.live.outbox import Outbox, OutboxError
from trap.live.tracker import LiveTracker


def live_disabled_by_env() -> bool:
    """``TRAP_NO_LIVE`` switches sync off for every run in this environment."""
    return os.environ.get("TRAP_NO_LIVE", "").strip().lower() in {"1", "true", "yes", "on"}


def start_tracking(
    *,
    run_dir: Path,
    case_ids: Sequence[str],
    server_override: str | None = None,
    enabled: bool = True,
) -> LiveTracker | None:
    """Start mirroring this run, or return None with nothing said.

    Returns None when sync is off, when there is no credential for the target
    server, or when the run directory cannot hold an outbox. None is the quiet
    path on purpose: a user who has never paired a CLI should not be nagged on
    every run, and the pairing hint belongs to `tp auth`, not to the middle of
    a run's output.

    The one thing this function must not do is fail. A run proceeds whatever
    happens here.
    """
    if not enabled or live_disabled_by_env():
        return None
    try:
        auth = ResolvedAuth.resolve(CredentialStore(), server_override)
    except CredentialStoreError:
        # An unreadable credential file is a pairing problem, not a run
        # problem. `tp auth status` is where it should surface.
        return None
    if not auth.api_key:
        return None

    # The owner is frozen here, from the id stored at pairing, with no network
    # call: the first case must not wait on the server, and a run that never
    # reaches it still knows whose queue it is. A credential that was never
    # verified leaves it None, which `tp sync` will not adopt without --claim.
    session = LiveSession(client_run_id=new_client_run_id(), server=auth.server, user_id=auth.user_id)
    outbox = Outbox(run_dir)
    try:
        outbox.prepare(session.producer_generation)
        # Written before the first case, so that a crash immediately after
        # still leaves a run that can be identified and resumed.
        session.save(run_dir)
    except (OutboxError, OSError):
        return None

    tracker = LiveTracker(
        client=LiveClient(auth.server, auth.api_key),
        session=session,
        outbox=outbox,
        run_dir=run_dir,
        case_ids=case_ids,
    )
    tracker.start()
    return tracker

"""Live progress sync — a run's progress mirrored to the paired trapstreet
account while it happens.

The whole package is built around one rule: **nothing here may change what the
run does.** A network outage, an expired token, a read-only disk or a bug in
this code must leave the solution, the judge, the grader, the report on disk
and the process exit code exactly as they would have been with sync switched
off. Every entry point is therefore failure-contained, and the sending happens
on a background thread that the per-case path never waits on.

The second rule is that only whitelisted progress leaves the machine: case
ordinals rather than case names, error codes rather than messages, and no
inputs, expected answers, outputs, stdout, paths, environment or command
lines. A case name can *be* the question; a path can name a private client.

Layout inside a run directory::

    <run_dir>/live/session.json    identity + how far the server has acked
    <run_dir>/live/outbox.jsonl    every event, in order, durable before sending

A CLI has no daemon, so whatever the network never took when the process exited
stays in that outbox until ``tp sync`` (see :mod:`trap.live.sync`) picks it up.
"""

from trap.live.client import LiveApiError, LiveClient
from trap.live.delivery import Delivery
from trap.live.identity import LiveSession, new_client_run_id
from trap.live.outbox import Outbox, OutboxEvent
from trap.live.sync import SyncReport, sync_run
from trap.live.tracker import LiveTracker

__all__ = [
    "Delivery",
    "LiveApiError",
    "LiveClient",
    "LiveSession",
    "LiveTracker",
    "Outbox",
    "OutboxEvent",
    "SyncReport",
    "new_client_run_id",
    "sync_run",
]

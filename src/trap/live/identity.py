"""Who this run is, and to whom.

The run directory is named by a local timestamp, which is not an identity: two
machines can produce the same name, and a timestamp says nothing about which
account or which server a run belongs to. So a run mints a UUID once, writes it
to a sidecar next to its artifacts, and that is what the server keys on.

The sidecar also freezes the *account* the run is being mirrored to. Freezing
matters for the offline case: events queued while the network was down must not
be delivered to whoever happens to be logged in later. A token can rotate and
still belong to the same person -- that is fine -- but a different user id, or a
different server, means the queue stays put.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ValidationError


def new_client_run_id() -> str:
    """A fresh globally-unique id for one logical run.

    One id per *execution*, not per attempt to send it: a network retry, a
    later `tp sync`, and the eventual report upload all name the same run, so
    the server can tell "the same run again" from "a second run".
    """
    return f"r-{uuid.uuid4().hex}"


class LiveSession(BaseModel):
    """The sidecar: identity, target and how far the server has acknowledged.

    Written before the first case runs so that a crash one line later still
    leaves a resumable record. ``run_id`` fills in once the server answers; the
    URL shown to the user does not wait for it, because the server also
    resolves a run by its ``client_run_id``.
    """

    client_run_id: str
    server: str
    #: Stable account id from ``/api/me``. None when the server did not give
    #: one (an older deployment), which downgrades this run to "cannot be
    #: safely resumed under a different login".
    user_id: str | None = None
    run_id: str | None = None
    producer_generation: int = 1
    #: Highest contiguous client_seq the server has confirmed. Everything at or
    #: below it is safe to consider delivered; everything above it is not, even
    #: if a response mentioned it.
    acked_seq: int = 0

    FILENAME: ClassVar[str] = "session.json"

    @classmethod
    def path_in(cls, run_dir: Path) -> Path:
        return run_dir / "live" / cls.FILENAME

    @classmethod
    def load(cls, run_dir: Path) -> LiveSession | None:
        """The sidecar for ``run_dir``, or None when absent or unreadable.

        A corrupt sidecar reads as absent rather than raising: it must never be
        able to stop a run from starting.
        """
        path = cls.path_in(run_dir)
        try:
            return cls.model_validate_json(path.read_text())
        except (OSError, ValidationError, ValueError):
            return None

    def save(self, run_dir: Path) -> None:
        """Write the sidecar atomically, so a crash mid-write cannot corrupt it."""
        path = self.path_in(run_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.model_dump(), indent=2))
        tmp.replace(path)

    def belongs_to(self, server: str, user_id: str | None) -> bool:
        """May a queue written under this sidecar be delivered as ``user_id`` on ``server``?

        Same server and same account: yes, even across a token rotation. A
        different account, a different server, or an identity we were never
        able to establish: no. The events stay on disk for the original owner
        rather than being handed to whoever logged in next.
        """
        if self.server.rstrip("/") != server.rstrip("/"):
            return False
        if self.user_id is None or user_id is None:
            return False
        return self.user_id == user_id

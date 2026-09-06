"""The durable outbox: an event is on disk before anyone tries to send it.

Ordering matters more than it looks. The server accepts a batch and answers
with the highest *contiguous* sequence number it has, which is only meaningful
if sequence numbers are assigned in one place, exactly once, and never reused.
So a sequence number is allocated by the act of appending a line: the file is
the counter.

Append-only JSONL rather than a database because the failure mode has to be
benign. A truncated last line (the machine lost power mid-write) costs one
event and is skipped on read; there is no schema to migrate, no lock file to
leak, and the file can be read by a person looking at why a run did not sync.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, ValidationError


class OutboxEvent(BaseModel):
    """One progress event, exactly as it will be sent.

    ``payload`` is already whitelisted by the caller -- this layer stores what
    it is given and never enriches it, so there is a single place (the
    tracker) where the question "may this field leave the machine?" is decided.
    """

    event_id: str
    client_seq: int
    producer_generation: int
    type: str
    payload: dict[str, float | int | str]
    client_occurred_at: str

    def wire(self) -> dict[str, object]:
        return self.model_dump()


class OutboxError(Exception):
    """The outbox could not be written. Sync gives up; the run continues."""


class Outbox:
    """Append-only event log for one run, with sequence allocation.

    Thread-safe: the tracker's producer appends from the run's thread while the
    sender reads from its own. The lock covers allocation and write together,
    because a sequence number that is allocated but not written would leave a
    permanent hole the server can never ack past.
    """

    FILENAME = "outbox.jsonl"

    def __init__(self, run_dir: Path) -> None:
        self.path = run_dir / "live" / self.FILENAME
        self._lock = Lock()
        self._next_seq = 1

    def prepare(self) -> None:
        """Create the directory and adopt the highest sequence already on disk.

        Called once before the first append. Resuming from an existing file
        continues its numbering rather than restarting at 1, which would make
        two different events share a slot.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
        except OSError as e:
            raise OutboxError(str(e)) from e
        highest = max((event.client_seq for event in self.read_all()), default=0)
        self._next_seq = highest + 1

    def append(
        self,
        *,
        event_id: str,
        type: str,
        payload: dict[str, float | int | str],
        producer_generation: int = 1,
    ) -> OutboxEvent:
        """Allocate the next sequence number and write the event.

        The number is only kept if the write succeeded, so a failed append does
        not burn a slot and strand every later event behind a gap.
        """
        with self._lock:
            seq = self._next_seq
            event = OutboxEvent(
                event_id=event_id,
                client_seq=seq,
                producer_generation=producer_generation,
                type=type,
                payload=payload,
                client_occurred_at=datetime.now(UTC).isoformat(),
            )
            line = json.dumps(event.model_dump(), separators=(",", ":"))
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError as e:
                raise OutboxError(str(e)) from e
            self._next_seq = seq + 1
            return event

    def read_all(self) -> list[OutboxEvent]:
        return list(self._iter_events())

    def pending(self, acked_seq: int) -> list[OutboxEvent]:
        """Events the server has not confirmed, oldest first."""
        return [event for event in self._iter_events() if event.client_seq > acked_seq]

    def _iter_events(self) -> Iterator[OutboxEvent]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                yield OutboxEvent.model_validate_json(line)
            except (ValidationError, ValueError):
                # A half-written final line from a killed process. Skipping it
                # loses one progress ping; raising would lose the whole queue.
                continue

"""JSON-RPC 2.0 over an agent's stdio — the transport the Agent Client Protocol runs on.

One JSON object per line. The agent sends three kinds of message: a response to one of
our requests, a notification (``session/update``: the reply as it streams, tool calls,
usage), or a request of its own (``session/request_permission``). A reader thread sorts
them; each of our requests waits on its own box.

The reader thread must never die silently: a handler bug (``on_update``/``on_request``
raising something other than ``AcpError``) is caught, recorded as the connection's
failure, and used to fail every pending request — the ones already in flight and any
made afterwards — instead of leaving them to hang until their caller's timeout."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from trap.shapes._case import kill_group

#: Our own error code for "the agent's stdout closed with this request unanswered" — or,
#: more generally, "this connection is done and will not answer anything else".
AGENT_EXITED = -32099
#: JSON-RPC's "internal error": the code an error reply gets when it carries no usable one.
INTERNAL_ERROR = -32603


class AcpError(Exception):
    """A JSON-RPC error from the agent, or the agent going away mid-request."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


def _error_from(error: Any) -> AcpError:
    """The AcpError a JSON-RPC ``error`` member stands for, whatever shape the agent gave
    it: a bare value is the message, and a missing or non-integer code is INTERNAL_ERROR."""
    err = error if isinstance(error, dict) else {"message": error}
    code = err.get("code")
    valid = isinstance(code, int) and not isinstance(code, bool)
    return AcpError(code if valid else INTERNAL_ERROR, str(err.get("message") or "error"), err.get("data"))


class Pending:
    """One request in flight. ``wait`` may time out and be called again: the response
    stays in the box until someone takes it, which is how a cancelled prompt's final
    ``cancelled`` answer is still collected.

    What ``wait`` hands back is always protocol-shaped — a result object (``{}`` when the
    agent sent something else) or an AcpError — so a malformed reply reaches the caller
    as an agent error, never as an AttributeError somewhere downstream."""

    def __init__(self) -> None:
        self._box: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)

    def resolve(self, message: dict[str, Any]) -> None:
        self._box.put(message)

    def wait(self, timeout: float) -> dict[str, Any]:
        try:
            message = self._box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if "error" in message:
            raise _error_from(message["error"])
        result = message.get("result")
        return result if isinstance(result, dict) else {}


class AcpConnection:
    """An agent process and the JSON-RPC session over its stdio. The agent runs as its
    own process group so ``close`` can take its children down with it; its stderr is
    inherited, so under ``tp run`` it lands in the case's stderr capture."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        on_update: Callable[[dict[str, Any]], None],
        on_request: Callable[[str, dict[str, Any]], dict[str, Any]],
    ) -> None:
        self._proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
            start_new_session=True,
        )
        # Captured once, non-optional from here on: stdin=PIPE/stdout=PIPE above means
        # Popen always gives us real pipes, never None — checking "is not None" anywhere
        # else in this class would be a branch nothing can ever take the other side of.
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._stdin: IO[bytes] = self._proc.stdin
        self._stdout: IO[bytes] = self._proc.stdout
        self._on_update = on_update
        self._on_request = on_request
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, Pending] = {}
        self._closed = False
        #: Set once, from the reader thread, when a handler raised something other than
        #: ``AcpError`` while processing a message. ``None`` means the agent just exited
        #: (or hasn't yet) — no handler is to blame.
        self._failure: AcpError | None = None
        threading.Thread(target=self._read, daemon=True).start()

    def request(self, method: str, params: dict[str, Any]) -> Pending:
        pending = Pending()
        with self._lock:
            if self._closed:
                pending.resolve(self._failure_reply())
                return pending
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = pending
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return pending

    def call(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self.request(method, params).wait(timeout)

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self, grace: float = 2.0) -> None:
        """End the session: close the agent's stdin, give it ``grace`` to exit, then kill
        its process group."""
        try:
            self._stdin.close()
        except OSError:
            # e.g. a broken pipe with unflushed bytes still queued from a write made
            # after the agent had already gone — see _send below.
            pass
        try:
            self._proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        kill_group(self._proc, grace)

    def _send(self, message: dict[str, Any]) -> None:
        line = (json.dumps(message) + "\n").encode()
        with self._lock:
            try:
                self._stdin.write(line)
                self._stdin.flush()
            except (OSError, ValueError):
                # OSError: the agent exited and its end of the pipe is gone (a broken
                # pipe), possibly with these bytes never leaving our own buffer.
                # ValueError: we already closed our end ourselves, in close().
                # Either way the agent is gone; the reader fails every pending request.
                pass

    def _failure_reply(self) -> dict[str, Any]:
        """The JSON-RPC error to hand a pending (or new) request once the connection is
        done. Call with ``self._lock`` held. ``self._failure`` is the recorded reason a
        handler blew up, if that's how the connection ended, else the agent simply exited."""
        failure = self._failure or AcpError(AGENT_EXITED, "the agent exited")
        return {"error": {"code": failure.code, "message": failure.message}}

    def _read(self) -> None:
        failure: AcpError | None = None
        try:
            for raw in self._stdout:
                try:
                    message = json.loads(raw)
                except (ValueError, RecursionError):
                    continue  # a banner on stdout, or JSON nested too deep to parse
                if not isinstance(message, dict):
                    continue  # e.g. a bare JSON scalar — not a JSON-RPC message either
                try:
                    self._dispatch(message)
                except Exception as e:  # a handler's bug must not hang every pending request
                    failure = AcpError(AGENT_EXITED, f"trap could not handle a message from the agent: {e}")
                    break
        finally:
            with self._lock:
                self._closed = True
                self._failure = failure
                reply = self._failure_reply()
                orphans = list(self._pending.values())
                self._pending.clear()
            for pending in orphans:
                pending.resolve(reply)

    def _dispatch(self, message: dict[str, Any]) -> None:
        """Sort one decoded message to its handler. Left to the caller (``_read``) to
        guard: ``on_update``/``on_request`` are the user's code and may raise."""
        if "method" in message and "id" in message:
            self._answer(message)
        elif "method" in message:
            update = (message.get("params") or {}).get("update")
            if message["method"] == "session/update" and isinstance(update, dict):
                self._on_update(update)
        elif "id" in message:
            with self._lock:
                pending = self._pending.pop(message["id"], None)
            if pending is not None:
                pending.resolve(message)

    def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = self._on_request(str(message["method"]), message.get("params") or {})
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        except AcpError as e:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": e.code, "message": e.message}}
        self._send(reply)

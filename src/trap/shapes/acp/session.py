"""One case over ACP: handshake, pick the model, ask once, keep the last thing it said.

Protocol v1. tp declares no client capabilities — no ``fs``, no ``terminal`` — so the
agent works on the case directory with its own tools, and the only thing it can ask tp
for is permission, which is granted one call at a time. Everything besides the answer
(the config the case ran with, permissions, self-reported usage) comes back as notes for
stderr."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trap.shapes._case import Deadline, ShapeExit
from trap.shapes.acp.connection import AcpConnection, AcpError

PROTOCOL_VERSION = 1
#: Seconds an agent gets to answer ``session/cancel`` before its process group is killed.
CANCEL_GRACE = 10.0
#: The first ``npx`` run of an agent downloads it; the handshake waits that long at most.
HANDSHAKE_TIMEOUT = 300.0

STOP_EXIT = {
    "end_turn": ShapeExit.OK,
    "refusal": ShapeExit.REFUSAL,
    "max_tokens": ShapeExit.MAX_TOKENS,
    "max_turn_requests": ShapeExit.MAX_TURNS,
}


class ConfigMismatch(Exception):
    """The agent does not offer the model or the option value this case asks for."""


@dataclass
class CaseOutcome:
    answer: str
    exit_code: int
    notes: list[str] = field(default_factory=list)


class MessageCollector:
    """Splits the streamed reply into the agent's separate messages and keeps them all;
    the answer is the last. A new message starts when ``messageId`` changes, or — for an
    agent that sends none — after a tool call, which closes the running no-id message
    right away: a turn that ends on a tool call answers with "", not with whatever it
    said before the tool call. Joining every chunk would put "let me read the file
    first…" in front of the answer, and a judge that wants three letters marks that
    wrong."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.cost: dict[str, Any] | None = None
        self._current: object = None
        self._boundary = True

    def _start_new_segment(self, mid: object) -> None:
        """Open a fresh message for ``mid`` to append to — reusing the last one if a
        tool call already opened it and nothing has filled it yet, so a tool call
        followed immediately by another (or by nothing) never leaves a stray empty
        message in the middle of ``messages``."""
        if not self.messages or self.messages[-1] != "":
            self.messages.append("")
        self._current = mid
        self._boundary = False

    def on_update(self, update: dict[str, Any]) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            text = content.get("text", "") if content.get("type") == "text" else ""
            mid = update.get("messageId")
            starts = (mid is not None and mid != self._current) or (mid is None and self._boundary)
            if starts or not self.messages:
                self._start_new_segment(mid)
            self.messages[-1] += text
        elif kind in ("tool_call", "tool_call_update"):
            if self._current is None and self.messages and self.messages[-1]:
                self._start_new_segment(None)
            self._boundary = True
        elif kind == "usage_update" and isinstance(update.get("cost"), dict):
            self.cost = update["cost"]

    @property
    def final(self) -> str:
        return self.messages[-1] if self.messages else ""


def reported_no_model_use(result: Mapping[str, Any]) -> bool:
    """True when the agent says outright that no model answered this turn. codex-acp
    answered a 401 with ``end_turn`` and the error text as its message (probe,
    2026-09-12); the tell was an absent usage with an empty ``model_usage``. An agent
    that reports no usage at all is not accused."""
    usage = result.get("usage")
    if isinstance(usage, dict):
        counts = [v for k, v in usage.items() if k.endswith("Tokens") and isinstance(v, int | float)]
        return bool(counts) and sum(counts) == 0
    quota = (result.get("_meta") or {}).get("quota") or {}
    return quota.get("model_usage") == []


def grant_once(notes: list[str]) -> Callable[[str, dict[str, Any]], dict[str, Any]]:
    """The answer to the agent's requests. Permission: the ``allow_once`` option, else
    ``reject_once``, else cancelled — never ``allow_always``, which in claude-agent-acp
    rewrites permission rules and can carry one case's grant into the next. This is
    unattended execution of whatever the agent asks for; the work directory is not a
    sandbox."""

    def on_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != "session/request_permission":
            raise AcpError(-32601, f"tp does not provide {method}")
        options = params.get("options") or []
        title = (params.get("toolCall") or {}).get("title") or "a tool call"
        pick = next((o for o in options if o.get("kind") == "allow_once"), None) or next(
            (o for o in options if o.get("kind") == "reject_once"), None
        )
        if pick is None:
            notes.append(f"permission for {title}: no allow_once option, cancelled")
            return {"outcome": {"outcome": "cancelled"}}
        notes.append(f"permission for {title}: {pick.get('kind')}")
        return {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}

    return on_request


def option_values(option: Mapping[str, Any]) -> list[str]:
    """A select option's values, flattened when the agent groups them."""
    values: list[str] = []
    for entry in option.get("options") or []:
        if isinstance(entry, dict) and isinstance(entry.get("options"), list):
            values += [str(v.get("value")) for v in entry["options"] if isinstance(v, dict)]
        elif isinstance(entry, dict):
            values.append(str(entry.get("value")))
    return values


def _readback(config_options: list[dict[str, Any]]) -> dict[str, str]:
    """Every option's current value, as ``{id: value}`` — what a fresh ``configOptions``
    list (the session's own, or one echoed back after a set) says the case runs with."""
    return {str(o.get("id")): str(o.get("currentValue")) for o in config_options if "currentValue" in o}


def apply_config(
    conn: Any,
    session_id: str,
    config_options: list[dict[str, Any]],
    *,
    model: str,
    options: Mapping[str, str],
    timeout: float,
) -> dict[str, str]:
    """Set the model (the option whose ``category`` is ``model`` — its id is the agent's
    choice), then each named option, changing only what differs. Returns every option's
    value afterwards: what the case actually ran with. ``timeout`` is a total budget
    across every ``session/set_config_option`` call this makes, not a fresh allowance for
    each one — a case that already spent most of its deadline on the handshake must not
    get a full new timeout per option it sets."""
    model_option = next((o for o in config_options if o.get("category") == "model"), None)
    if model_option is None:
        raise ConfigMismatch("the agent offers no model option (no configOption with category 'model')")
    wanted: list[tuple[dict[str, Any], str]] = [(model_option, model)]
    for option_id, value in options.items():
        option = next((o for o in config_options if o.get("id") == option_id), None)
        if option is None:
            ids = ", ".join(str(o.get("id")) for o in config_options)
            raise ConfigMismatch(f"the agent has no option {option_id!r}; it has: {ids}")
        wanted.append((option, value))
    now = _readback(config_options)
    end = time.monotonic() + timeout
    for option, value in wanted:
        values = option_values(option)
        if value not in values:
            raise ConfigMismatch(
                f"{option.get('id')} {value!r} is not offered; choose one of: {', '.join(values)}"
            )
        if now.get(str(option["id"])) == value:
            continue
        reply = conn.call(
            "session/set_config_option",
            {"sessionId": session_id, "configId": option["id"], "value": value},
            max(0.0, end - time.monotonic()),
        )
        echoed = reply.get("configOptions")
        if isinstance(echoed, list):
            now = _readback(echoed)
            if now.get(str(option["id"])) != value:
                raise ConfigMismatch(
                    f"{option['id']} stayed {now.get(str(option['id']))!r} after setting {value!r}"
                )
        else:
            now[str(option["id"])] = value
    return now


def _open_session(
    conn: AcpConnection, *, workdir: Path, meta: Mapping[str, Any] | None, timeout: float
) -> dict[str, Any]:
    """``timeout`` is a total budget for both calls (``initialize`` then ``session/new``),
    not a fresh allowance for each — else the second call can still be waiting long after
    the deadline that was meant to bound the whole handshake has passed, orphaning the
    agent once the runner's own timeout kills only this shape."""
    end = time.monotonic() + timeout
    conn.call(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
        max(0.0, end - time.monotonic()),
    )
    params: dict[str, Any] = {"cwd": str(workdir), "mcpServers": []}
    if meta:
        params["_meta"] = dict(meta)
    return conn.call("session/new", params, max(0.0, end - time.monotonic()))


def run_case(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    workdir: Path,
    question: str,
    model: str,
    options: Mapping[str, str],
    meta: Mapping[str, Any] | None,
    deadline: Deadline,
    cancel_grace: float = CANCEL_GRACE,
) -> CaseOutcome:
    """Start the agent in ``workdir``, ask ``question`` once, and return its last message
    with the exit code the turn earned. The agent's process group is gone on return."""
    notes: list[str] = []
    collector = MessageCollector()
    conn = AcpConnection(
        argv, env=env, cwd=workdir, on_update=collector.on_update, on_request=grant_once(notes)
    )
    try:
        outcome = _converse(
            conn, collector, notes, workdir, question, model, options, meta, deadline, cancel_grace
        )
    finally:
        conn.close()
    if collector.cost is not None:
        outcome.notes.append(f"agent self-reported session cost: {collector.cost}")
    return outcome


def _converse(
    conn: AcpConnection,
    collector: MessageCollector,
    notes: list[str],
    workdir: Path,
    question: str,
    model: str,
    options: Mapping[str, str],
    meta: Mapping[str, Any] | None,
    deadline: Deadline,
    cancel_grace: float,
) -> CaseOutcome:
    try:
        session = _open_session(
            conn, workdir=workdir, meta=meta, timeout=min(HANDSHAKE_TIMEOUT, deadline.remaining())
        )
        session_id = str(session["sessionId"])
        ran_with = apply_config(
            conn,
            session_id,
            session.get("configOptions") or [],
            model=model,
            options=options,
            timeout=deadline.remaining(),
        )
    except ConfigMismatch as e:
        notes.append(str(e))
        return CaseOutcome("", ShapeExit.CONFIG_ERROR, notes)
    except TimeoutError:
        notes.append("deadline reached before the question was sent")
        return CaseOutcome("", ShapeExit.TIMEOUT, notes)
    except (AcpError, KeyError, TypeError) as e:
        notes.append(f"could not open a session: {e}")
        return CaseOutcome("", ShapeExit.AGENT_ERROR, notes)
    notes.append("agent config: " + ", ".join(f"{k}={v}" for k, v in sorted(ran_with.items())))

    prompt = conn.request(
        "session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": question}]}
    )
    try:
        result = prompt.wait(deadline.remaining())
    except TimeoutError:
        conn.notify("session/cancel", {"sessionId": session_id})
        try:
            prompt.wait(cancel_grace)
        except (TimeoutError, AcpError):
            pass
        notes.append("deadline reached; the session was cancelled")
        return CaseOutcome(collector.final, ShapeExit.TIMEOUT, notes)
    except AcpError as e:
        notes.append(f"the agent failed the turn: {e}")
        if collector.final:
            notes.append(f"its last message, not an answer: {collector.final[:500]}")
        return CaseOutcome("", ShapeExit.AGENT_ERROR, notes)

    stop = result.get("stopReason")
    if isinstance(result.get("usage"), dict):
        notes.append(f"agent self-reported usage: {result['usage']}")
    if stop == "end_turn" and reported_no_model_use(result):
        notes.append(
            "the agent reported that no model answered; its message is not an answer: "
            f"{collector.final[:500]}"
        )
        return CaseOutcome("", ShapeExit.AGENT_ERROR, notes)
    code = STOP_EXIT.get(str(stop), ShapeExit.AGENT_ERROR)
    if code is ShapeExit.AGENT_ERROR:
        notes.append(f"unexpected stopReason {stop!r}")
        return CaseOutcome("", code, notes)
    return CaseOutcome(collector.final, code, notes)


def describe_agent(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    meta: Mapping[str, Any] | None,
    timeout: float = HANDSHAKE_TIMEOUT,
) -> list[dict[str, Any]]:
    """The agent's config options — what ``--model`` and ``--option`` may say. Opens a
    session and closes it without asking anything, so it costs no tokens."""
    conn = AcpConnection(argv, env=env, cwd=cwd, on_update=lambda update: None, on_request=grant_once([]))
    try:
        session = _open_session(conn, workdir=cwd, meta=meta, timeout=timeout)
    finally:
        conn.close()
    return [
        {
            "id": o.get("id"),
            "category": o.get("category"),
            "current": o.get("currentValue"),
            "values": option_values(o),
        }
        for o in session.get("configOptions") or []
    ]

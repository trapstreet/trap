"""One case over ACP: handshake, pick the model, ask once, keep the last thing it said.

Protocol v1. tp declares no client capabilities — no ``fs``, no ``terminal`` — so the
agent works on the case directory with its own tools, and the only thing it can ask tp
for is permission, which is granted one call at a time. Everything besides the answer
(the config the case ran with, the agent's earlier messages and tool calls, permissions,
self-reported usage) comes back, in order, as notes for stderr."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trap.shapes._case import Deadline, ShapeExit, kill_on_interrupt
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
    #: The agent's self-reported build (name@version), once the handshake has answered
    #: -- even if the turn itself later fails, since the card wants to know what it was
    #: talking to regardless.
    agent: str | None = None
    #: Every option that actually took effect, minus the model's own id (see
    #: apply_config) so the model is not stated twice.
    options: dict[str, str] = field(default_factory=dict)


#: How much of an earlier agent message the case's stderr keeps.
NOTED_CHARS = 500


class MessageCollector:
    """Splits the streamed reply into the agent's separate messages and keeps them all;
    the answer is the last. A new message starts when ``messageId`` changes, or — for an
    agent that sends none — after a tool call, which closes the running no-id message
    right away: a turn that ends on a tool call answers with "", not with whatever it
    said before the tool call. Joining every chunk would put "let me read the file
    first…" in front of the answer, and a judge that wants three letters marks that
    wrong.

    Everything before the answer goes to ``notes`` as it happens — each earlier message
    (cut to NOTED_CHARS) once a tool call or another message follows it, and each tool
    call's title and status — so the case's stderr shows how the agent got to the
    message that was taken as its answer."""

    def __init__(self, notes: list[str] | None = None) -> None:
        self.messages: list[str] = []
        self.cost: dict[str, Any] | None = None
        self._current: object = None
        self._boundary = True
        self._notes = notes if notes is not None else []
        self._noted = 0  # how much of the running message is already in the notes
        self._titles: dict[str, str] = {}

    def _note_message(self) -> None:
        """Note what the running message has said since it was last noted: something
        followed it, so it is part of the process, not the answer."""
        said = self.messages[-1][self._noted :] if self.messages else ""
        if said:
            cut = "…" if len(said) > NOTED_CHARS else ""
            self._notes.append(f"agent message: {said[:NOTED_CHARS]}{cut}")
        self._noted += len(said)

    def _note_tool_call(self, kind: str, update: dict[str, Any]) -> None:
        """Note a tool call when it starts, and again whenever an update changes its
        status; a call is named by its title, or its id until it has one."""
        tool = str(update.get("toolCallId"))
        if update.get("title"):
            self._titles[tool] = str(update["title"])
        if kind == "tool_call" or "status" in update:
            status = update.get("status")
            self._notes.append(
                f"tool call: {self._titles.get(tool, tool)}" + (f" ({status})" if status else "")
            )

    def _start_new_segment(self, mid: object) -> None:
        """Open a fresh message for ``mid`` to append to — reusing the last one if a
        tool call already opened it and nothing has filled it yet, so a tool call
        followed immediately by another (or by nothing) never leaves a stray empty
        message in the middle of ``messages``."""
        self._note_message()
        if not self.messages or self.messages[-1] != "":
            self.messages.append("")
        self._noted = 0
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
            self._note_message()
            if self._current is None and self.messages and self.messages[-1]:
                self._start_new_segment(None)
            self._boundary = True
            self._note_tool_call(kind, update)
        elif kind == "usage_update" and isinstance(update.get("cost"), dict):
            self.cost = update["cost"]

    @property
    def final(self) -> str:
        return self.messages[-1] if self.messages else ""


def reported_no_model_use(result: Mapping[str, Any]) -> bool:
    """True when the agent says outright that no model answered this turn. codex-acp
    answered a 401 with ``end_turn`` and the error text as its message (probe,
    2026-09-12); the tell was an absent usage with an empty ``model_usage``. An agent
    that reports no usage at all is not accused. Token counts decide when there are any;
    a usage without them says nothing, and the quota is asked instead."""
    usage = result.get("usage")
    if isinstance(usage, dict):
        counts = [v for k, v in usage.items() if k.endswith("Tokens") and isinstance(v, int | float)]
        if counts:
            return sum(counts) == 0
    meta = result.get("_meta")
    quota = meta.get("quota") if isinstance(meta, dict) else None
    return isinstance(quota, dict) and quota.get("model_usage") == []


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
    entries = option.get("options")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("options"), list):
            values += [str(v.get("value")) for v in entry["options"] if isinstance(v, dict)]
        elif isinstance(entry, dict):
            values.append(str(entry.get("value")))
    return values


def _objects(entries: list[Any]) -> list[dict[str, Any]]:
    """The entries of a ``configOptions`` list that are objects; anything else in it is
    not an option, and is skipped rather than tripped over."""
    return [e for e in entries if isinstance(e, dict)]


def config_options(session: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``session/new``'s ``configOptions``. Absent or null is no options; a value that is
    not a list is not the protocol — a TypeError, which callers report as an agent error
    rather than as an agent that offers no model."""
    found = session.get("configOptions")
    if found is None:
        return []
    if not isinstance(found, list):
        raise TypeError(f"configOptions is not a list (it is a {type(found).__name__})")
    return _objects(found)


def _readback(config_options: list[dict[str, Any]]) -> dict[str, str]:
    """Every option's current value, as ``{id: value}`` — what a fresh ``configOptions``
    list (the session's own, or one echoed back after a set) says the case runs with."""
    return {str(o.get("id")): str(o.get("currentValue")) for o in config_options if "currentValue" in o}


def _set_option(
    conn: Any,
    session_id: str,
    option: dict[str, Any],
    value: str,
    now: dict[str, str],
    available: list[dict[str, Any]],
    end: float,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Apply one option, having already checked it is offered — validate the value,
    skip the call when it already holds, else set it and read back what took. Returns
    the option values afterwards and what the agent now offers: an echoed
    ``configOptions`` after the call, since setting one option (the model, above all)
    can add or remove others, and a later option in the same ``apply_config`` call must
    see that fresh list, not the one the case started with."""
    option_id = str(option["id"])
    values = option_values(option)
    if value not in values:
        raise ConfigMismatch(f"{option_id} {value!r} is not offered; choose one of: {', '.join(values)}")
    if now.get(option_id) == value:
        return now, available
    reply = conn.call(
        "session/set_config_option",
        {"sessionId": session_id, "configId": option_id, "value": value},
        max(0.0, end - time.monotonic()),
    )
    echoed = reply.get("configOptions")
    if isinstance(echoed, list):
        fresh = _objects(echoed)
        updated = _readback(fresh)
        if updated.get(option_id) != value:
            raise ConfigMismatch(f"{option_id} stayed {updated.get(option_id)!r} after setting {value!r}")
        return updated, fresh
    now = dict(now)
    now[option_id] = value
    return now, available


def apply_config(
    conn: Any,
    session_id: str,
    config_options: list[dict[str, Any]],
    *,
    model: str,
    options: Mapping[str, str],
    timeout: float,
    notes: list[str] | None = None,
) -> dict[str, str]:
    """Set the model (the option whose ``category`` is ``model`` — its id is the agent's
    choice), then each named option, changing only what differs — **skipping** one the
    chosen model no longer offers, noted on ``notes`` rather than raised: switching models
    can retire an option (claude-acp drops ``effort`` under haiku), and that is a fact
    about the model, not the same failure as asking for an option the agent never had at
    all, which stays a ``ConfigMismatch``. Returns every option's value afterwards: what
    the case actually ran with — a skipped option is never among them, so it can never
    reach the card as "applied". ``timeout`` is a total budget across every
    ``session/set_config_option`` call this makes, not a fresh allowance for each one — a
    case that already spent most of its deadline on the handshake must not get a full new
    timeout per option it sets."""
    notes = notes if notes is not None else []
    model_option = next((o for o in config_options if o.get("category") == "model"), None)
    if model_option is None:
        raise ConfigMismatch("the agent offers no model option (no configOption with category 'model')")

    now = _readback(config_options)
    end = time.monotonic() + timeout
    now, available = _set_option(conn, session_id, model_option, model, now, config_options, end)

    # "Never offered at all" is checked against every id this agent has shown, before
    # *and* after the model change — not just the list the case started with. An agent
    # can default to a model that already lacks an option a later --model does offer
    # (claude-acp defaulting to haiku, say), so the pre-change list alone would call a
    # perfectly valid option a mismatch; checking only the post-change list would do the
    # same the other way around for one only the *previous* model offered.
    known_ids = {o.get("id") for o in config_options} | {o.get("id") for o in available}
    for option_id in options:
        if option_id not in known_ids:
            ids = ", ".join(str(o.get("id")) for o in config_options)
            raise ConfigMismatch(f"the agent has no option {option_id!r}; it has: {ids}")

    for option_id, value in options.items():
        option = next((o for o in available if o.get("id") == option_id), None)
        if option is None:
            notes.append(f"{option_id!r} is not offered with model {model!r} — skipped, not applied")
            continue
        now, available = _set_option(conn, session_id, option, value, now, available, end)
    return now


def _agent_from_reply(reply: Mapping[str, Any]) -> str | None:
    """``initialize``'s ``agentInfo``, as ``name@version`` — the name alone when there
    is no version, ``None`` when the agent says nothing at all, or says it in a shape
    that isn't an object (``agentInfo: "claude"``, say) — a malformed handshake must end
    the case cleanly, never escape as a traceback the way ``info.get`` would on
    anything that isn't a mapping."""
    info = reply.get("agentInfo")
    if not isinstance(info, Mapping):
        return None
    name = info.get("name")
    if not name:
        return None
    version = info.get("version")
    return f"{name}@{version}" if version else str(name)


def _open_session(
    conn: AcpConnection, *, workdir: Path, meta: Mapping[str, Any] | None, timeout: float
) -> tuple[dict[str, Any], str | None]:
    """``timeout`` is a total budget for both calls (``initialize`` then ``session/new``),
    not a fresh allowance for each — else the second call can still be waiting long after
    the deadline that was meant to bound the whole handshake has passed, orphaning the
    agent once the runner's own timeout kills only this shape.

    Returns the session and the agent's self-reported build (see ``_agent_from_reply``) —
    kept from ``initialize``'s own reply, since ``session/new``'s has nothing to say
    about the agent itself."""
    end = time.monotonic() + timeout
    reply = conn.call(
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION, "clientCapabilities": {}},
        max(0.0, end - time.monotonic()),
    )
    agent = _agent_from_reply(reply)
    params: dict[str, Any] = {"cwd": str(workdir), "mcpServers": []}
    if meta:
        params["_meta"] = dict(meta)
    session = conn.call("session/new", params, max(0.0, end - time.monotonic()))
    return session, agent


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
    with the exit code the turn earned. The agent's process group is gone on return — and
    on an interrupt, before the shape exits."""
    notes: list[str] = []
    collector = MessageCollector(notes)
    conn = AcpConnection(
        argv, env=env, cwd=workdir, on_update=collector.on_update, on_request=grant_once(notes)
    )
    with kill_on_interrupt(conn.pid):
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
    #: Known as soon as the handshake answers, kept for every return below it — even one
    #: that fails later (a bad option, a failed turn) still knew who it was talking to.
    agent: str | None = None
    try:
        session, agent = _open_session(
            conn, workdir=workdir, meta=meta, timeout=min(HANDSHAKE_TIMEOUT, deadline.remaining())
        )
        session_id = str(session["sessionId"])
        offered = config_options(session)
        ran_with = apply_config(
            conn,
            session_id,
            offered,
            model=model,
            options=options,
            timeout=deadline.remaining(),
            notes=notes,
        )
    except ConfigMismatch as e:
        notes.append(str(e))
        return CaseOutcome("", ShapeExit.CONFIG_ERROR, notes, agent=agent)
    except TimeoutError:
        notes.append("deadline reached before the question was sent")
        return CaseOutcome("", ShapeExit.TIMEOUT, notes, agent=agent)
    except (AcpError, KeyError, TypeError) as e:
        notes.append(f"could not open a session: {e}")
        return CaseOutcome("", ShapeExit.AGENT_ERROR, notes, agent=agent)
    # apply_config only returns once the model option was found and set, so this is
    # always a real id -- what the card must not repeat under its own "options" field.
    model_id = str(next(o.get("id") for o in offered if o.get("category") == "model"))
    applied = {k: v for k, v in ran_with.items() if k != model_id}
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
        return CaseOutcome(collector.final, ShapeExit.TIMEOUT, notes, agent=agent, options=applied)
    except AcpError as e:
        notes.append(f"the agent failed the turn: {e}")
        if collector.final:
            notes.append(f"its last message, not an answer: {collector.final[:500]}")
        return CaseOutcome("", ShapeExit.AGENT_ERROR, notes, agent=agent, options=applied)

    stop = result.get("stopReason")
    if isinstance(result.get("usage"), dict):
        notes.append(f"agent self-reported usage: {result['usage']}")
    if stop == "end_turn" and reported_no_model_use(result):
        notes.append(
            "the agent reported that no model answered; its message is not an answer: "
            f"{collector.final[:500]}"
        )
        return CaseOutcome("", ShapeExit.AGENT_ERROR, notes, agent=agent, options=applied)
    code = STOP_EXIT.get(str(stop), ShapeExit.AGENT_ERROR)
    if code is ShapeExit.AGENT_ERROR:
        notes.append(f"unexpected stopReason {stop!r}")
        return CaseOutcome("", code, notes, agent=agent, options=applied)
    return CaseOutcome(collector.final, code, notes, agent=agent, options=applied)


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
    with kill_on_interrupt(conn.pid):
        try:
            session, _ = _open_session(conn, workdir=cwd, meta=meta, timeout=timeout)
        finally:
            conn.close()
    return [
        {
            "id": o.get("id"),
            "category": o.get("category"),
            "current": o.get("currentValue"),
            "values": option_values(o),
        }
        for o in config_options(session)
    ]

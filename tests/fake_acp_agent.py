"""A scripted ACP agent for the bridge tests: JSON-RPC over stdio, one behaviour per
FAKE_ACP_MODE. What it receives — plus its cwd, files, a full text dump of everything
under that cwd, and its environment at startup — is appended to FAKE_ACP_LOG as JSON
lines, so a test can check what the bridge sent it. Not a test module: tests start it as
a subprocess.

``garbage`` and ``stray`` exist only to drive lines of trap.shapes.acp.connection that
no protocol-shaped conversation reaches on its own (a non-JSON stdout line, a JSON
scalar, JSON nested too deep to parse, a response for an id nobody asked for, a message
with neither ``method`` nor ``id``) — coverage, not protocol behaviour.
``permission_no_once``, ``permission_none_offered``, ``null_chunk``, ``unknown_stop``,
``bad_session_new`` and ``hang_handshake`` exist the same way, for
trap.shapes.acp.session.

``error_string``, ``list_result``, ``dict_config`` and ``junk_config`` answer with
replies shaped unlike the protocol — an error that is a bare string, a prompt result
that is a list, configOptions that is an object, configOptions padded with non-objects —
so the tests can check none of them escapes the shape as a traceback.

``surrogate`` answers with a message that carries a lone UTF-16 surrogate (what
``json.loads`` turns a JSON escape like ``"\\ud800"`` into) — the same character a real
agent's JSON-RPC reply can carry, and stdout cannot print outright."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MODE = os.environ.get("FAKE_ACP_MODE", "ok")
LOG = os.environ.get("FAKE_ACP_LOG")
STATE = {"model": "default", "effort": "default"}
USAGE = {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13}
PENDING: dict[str, object] = {}


def tree() -> dict[str, str]:
    """Every text file under the cwd, keyed by its POSIX-style relative path — so a test
    can check not just that a file landed somewhere, but that it landed at the right
    depth with the right content. Skips anything that isn't decodable text rather than
    fail the whole startup log over one binary file."""
    found: dict[str, str] = {}
    for p in Path().rglob("*"):
        if p.is_file():
            try:
                found[p.as_posix()] = p.read_text()
            except (UnicodeDecodeError, OSError):
                pass
    return found


def log(entry: dict) -> None:
    if LOG:
        with open(LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def read() -> dict | None:
    line = sys.stdin.readline()
    return json.loads(line) if line else None


def result(rid: object, body: dict) -> None:
    send({"jsonrpc": "2.0", "id": rid, "result": body})


def update(sid: str, body: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": body}})


def say(sid: str, text: str, mid: str | None = None) -> None:
    body: dict = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    if mid is not None:
        body["messageId"] = mid
    update(sid, body)


def tool(sid: str, title: str) -> None:
    update(
        sid,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": title,
            "kind": "read",
            "status": "pending",
        },
    )
    update(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})


def config_options() -> list[dict]:
    def select(oid: str, category: str, values: list[str]) -> dict:
        return {
            "id": oid,
            "name": oid,
            "category": category,
            "type": "select",
            "currentValue": STATE[oid],
            "options": [{"value": v, "name": v} for v in values],
        }

    return [
        select("model", "model", ["default", "sonnet", "haiku"]),
        select("effort", "thought_level", ["default", "low", "high"]),
    ]


def permission_prompt(rid: object, sid: str, options: list[dict]) -> None:
    params = {"sessionId": sid, "toolCall": {"toolCallId": "t1", "title": "Run ls"}, "options": options}
    send({"jsonrpc": "2.0", "id": "perm-1", "method": "session/request_permission", "params": params})
    log({"permission_reply": read()})
    say(sid, "perm-ok", "m1")
    result(rid, {"stopReason": "end_turn", "usage": USAGE})


def prompt(rid: object, sid: str) -> None:
    if MODE in ("ok", "garbage", "stray", "junk_config"):
        say(sid, "Let me read the file first.", "m1")
        tool(sid, "Read question.txt")
        say(sid, "4", "m2")
        say(sid, "2", "m2")
        update(
            sid,
            {
                "sessionUpdate": "usage_update",
                "used": 100,
                "size": 1000,
                "cost": {"amount": 0.01, "currency": "USD"},
            },
        )
        result(rid, {"stopReason": "end_turn", "usage": USAGE})
    elif MODE == "no_message_id":
        say(sid, "thinking aloud")
        tool(sid, "ls")
        say(sid, "final answer")
        result(rid, {"stopReason": "end_turn", "usage": USAGE})
    elif MODE == "auth_error":
        say(sid, "Failed to authenticate", "e1")
        send(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "error": {"code": -32603, "message": "Internal error: Failed to authenticate"},
            }
        )
    elif MODE == "no_model_use":
        say(sid, "unexpected status 401 Unauthorized")
        result(
            rid,
            {
                "stopReason": "end_turn",
                "usage": None,
                "_meta": {"quota": {"token_count": None, "model_usage": []}},
            },
        )
    elif MODE == "refusal":
        say(sid, "I can't help with that.", "m1")
        result(rid, {"stopReason": "refusal", "usage": USAGE})
    elif MODE == "permission":
        permission_prompt(
            rid,
            sid,
            [
                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                {"optionId": "once", "name": "Once", "kind": "allow_once"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        )
    elif MODE == "permission_no_once":
        permission_prompt(
            rid,
            sid,
            [
                {"optionId": "always", "name": "Always", "kind": "allow_always"},
                {"optionId": "no", "name": "No", "kind": "reject_once"},
            ],
        )
    elif MODE == "permission_none_offered":
        permission_prompt(rid, sid, [{"optionId": "always", "name": "Always", "kind": "allow_always"}])
    elif MODE in ("hang", "hang_hard"):
        say(sid, "partial", "m1")
        PENDING["prompt"] = rid
    elif MODE == "crash":
        say(sid, "about to crash", "m1")
        sys.exit(3)
    elif MODE == "null_chunk":
        # A chunk whose content.text is null: malformed. MessageCollector.on_update
        # raises on it (None is not a str to concatenate); the case must fail promptly
        # as AGENT_ERROR rather than hang to the deadline or be read as a crashed agent.
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": None}})
        result(rid, {"stopReason": "end_turn", "usage": USAGE})
    elif MODE == "unknown_stop":
        say(sid, "done, sort of", "m1")
        result(rid, {"stopReason": "something_else", "usage": USAGE})
    elif MODE == "list_result":
        say(sid, "4", "m1")
        send({"jsonrpc": "2.0", "id": rid, "result": ["end_turn"]})
    elif MODE == "surrogate":
        say(sid, "ok \ud800 done", "m1")
        result(rid, {"stopReason": "end_turn", "usage": USAGE})


def main() -> None:
    if MODE == "hang_hard":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen(["sleep", "60"])
        log({"child_pid": child.pid})
    if MODE == "garbage":
        # Not valid JSON, not a JSON object, and JSON nested too deep for the parser —
        # each must be skipped rather than kill the reader thread reading this stdout.
        sys.stdout.write("this is not json\n")
        sys.stdout.write(json.dumps([1, 2, 3]) + "\n")
        sys.stdout.write("[" * 100_000 + "]" * 100_000 + "\n")
        sys.stdout.flush()
    log(
        {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "files": sorted(os.listdir(".")),
            "tree": tree(),
            "env": dict(os.environ),
        }
    )
    while (message := read()) is not None:
        log({"received": message})
        method, rid, params = message.get("method"), message.get("id"), message.get("params") or {}
        if MODE == "stray" and method == "initialize":
            send({"jsonrpc": "2.0"})  # neither "method" nor "id" — matches no dispatch case
            send({"jsonrpc": "2.0", "id": "no-such-id", "result": {}})  # nobody is waiting on this id
            send({"jsonrpc": "2.0", "method": "session/other", "params": {}})  # a notification we ignore
        if method == "initialize":
            result(rid, {"protocolVersion": 1, "agentCapabilities": {}, "authMethods": []})
        elif method == "session/new":
            if MODE == "hang_handshake":
                pass  # never respond; the caller must hit its own deadline
            elif MODE == "bad_session_new":
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32001, "message": "cannot create session"},
                    }
                )
            elif MODE == "error_string":
                send({"jsonrpc": "2.0", "id": rid, "error": "boom"})
            elif MODE == "dict_config":
                result(rid, {"sessionId": "s1", "configOptions": {o["id"]: o for o in config_options()}})
            elif MODE == "junk_config":
                result(rid, {"sessionId": "s1", "configOptions": ["model", 7, None, *config_options()]})
            else:
                result(rid, {"sessionId": "s1", "configOptions": config_options()})
        elif method == "session/set_config_option":
            oid, value = params.get("configId"), params.get("value")
            allowed = {o["id"]: [v["value"] for v in o["options"]] for o in config_options()}
            if oid not in allowed or value not in allowed[oid]:
                send(
                    {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"bad {oid}={value}"}}
                )
            else:
                STATE[oid] = value
                result(rid, {"configOptions": config_options()})
        elif method == "session/prompt":
            prompt(rid, params.get("sessionId", "s1"))
        elif method == "session/cancel" and MODE == "hang" and "prompt" in PENDING:
            result(PENDING.pop("prompt"), {"stopReason": "cancelled"})
    if MODE == "hang_hard":
        time.sleep(60)


if __name__ == "__main__":
    main()

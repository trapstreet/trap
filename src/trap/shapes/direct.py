"""``tp shape direct``: the question, once, to a model API — no harness, no tools.

The case's question is the only user message; ``--system-file`` (a SKILL.md, say) is the
system prompt. One non-streamed call, answered by the reply's visible text. The base URL
comes from the provider's usual variable, so under ``tp run`` the call goes through the
cost proxy and is metered like any solution's.

Generation settings are fixed and said on stderr: Anthropic gets ``max_tokens`` 16000 —
Claude 5 models think by default and the thinking shares that ceiling, so a small one
leaves the answer empty or cut (baseline-no-skill's notes) — OpenAI-compatible APIs get
no cap, and thinking / reasoning effort stay at each API's default.

    cmd: tp shape direct --model claude-sonnet-5 --system-file SKILL.md
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from trap.cost.providers import provider_config
from trap.shapes._case import Deadline, ShapeError, ShapeExit, ShapeParser, add_case_args, fail, open_case

ANTHROPIC_MAX_TOKENS = 16000
ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class DirectProvider:
    path: str  # appended to the base URL, the way the provider's own SDK does
    anthropic: bool  # Messages API; otherwise OpenAI-compatible chat completions


PROVIDERS: dict[str, DirectProvider] = {
    "anthropic": DirectProvider("/v1/messages", anthropic=True),
    "openai": DirectProvider("/chat/completions", anthropic=False),
    "openrouter": DirectProvider("/chat/completions", anthropic=False),
    "deepseek": DirectProvider("/chat/completions", anthropic=False),
    "moonshot": DirectProvider("/chat/completions", anthropic=False),
    "mistral": DirectProvider("/v1/chat/completions", anthropic=False),
}

_PREFIXES = (
    (("claude-",), "anthropic"),
    (("gpt-", "chatgpt-", "o1", "o3", "o4"), "openai"),
    (("deepseek-",), "deepseek"),
    (("kimi-", "moonshot-"), "moonshot"),
    (("mistral-", "magistral-", "codestral-", "devstral-", "ministral-"), "mistral"),
)
_OK_STOPS = {"end_turn", "stop_sequence", "stop"}
_LIMIT_STOPS = {"max_tokens", "length"}
_REFUSAL_STOPS = {"refusal", "content_filter"}


def infer_provider(model: str) -> str | None:
    """The provider a model id belongs to; ``vendor/model`` is an OpenRouter id."""
    if "/" in model:
        return "openrouter"
    return next((provider for prefixes, provider in _PREFIXES if model.startswith(prefixes)), None)


@dataclass
class Reply:
    text: str
    stop: str | None
    usage: dict[str, Any]


def build_request(
    provider: str, model: str, question: str, system: str | None
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """(url, headers, body) for one call."""
    spec = PROVIDERS[provider]
    config = provider_config(provider)
    key = os.environ.get(config.key_env)
    if not key:
        raise ShapeError(ShapeExit.CONFIG_ERROR, f"{config.key_env} is not set")
    url = config.resolve_upstream().rstrip("/") + spec.path
    user = {"role": "user", "content": question}
    if spec.anthropic:
        headers = {
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        body: dict[str, Any] = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "messages": [user]}
        if system:
            body["system"] = system
        return url, headers, body
    headers = {"authorization": f"Bearer {key}", "content-type": "application/json"}
    messages = [{"role": "system", "content": system}, user] if system else [user]
    return url, headers, {"model": model, "messages": messages}


def parse_reply(provider: str, data: dict[str, Any]) -> Reply:
    """The reply's visible text, why it stopped, and its usage as the vendor reported it."""
    if PROVIDERS[provider].anthropic:
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        return Reply(text, data.get("stop_reason"), data.get("usage") or {})
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    return Reply(text, choice.get("finish_reason"), data.get("usage") or {})


def exit_for(reply: Reply) -> tuple[ShapeExit, str | None]:
    """The exit code a reply earns, and why when it is not a plain answer."""
    if reply.stop in _REFUSAL_STOPS:
        return ShapeExit.REFUSAL, f"the model refused ({reply.stop})"
    if reply.stop in _LIMIT_STOPS:
        return ShapeExit.MAX_TOKENS, f"the reply hit the output ceiling ({reply.stop})"
    if reply.stop not in _OK_STOPS:
        return ShapeExit.AGENT_ERROR, f"unexpected stop reason {reply.stop!r}"
    if not reply.text.strip():
        return ShapeExit.AGENT_ERROR, "the reply had no visible text"
    return ShapeExit.OK, None


def main(argv: Sequence[str] | None = None) -> int:
    parser = ShapeParser(prog="tp shape direct", description="Ask a model API one case's question.")
    parser.add_argument("--model", required=True, help="the API's model id, e.g. claude-sonnet-5")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), help="default: read off the model id")
    parser.add_argument("--system-file", type=Path, help="a file sent as the system prompt, e.g. a SKILL.md")
    add_case_args(parser)
    args = parser.parse_args(argv)
    provider = args.provider or infer_provider(args.model)
    if provider is None:
        return fail(
            ShapeError(ShapeExit.CONFIG_ERROR, f"cannot tell the provider of {args.model!r}; pass --provider")
        )
    deadline = Deadline(args.deadline)
    try:
        # This shape starts no child process, so the scrubbed env open_case returns has
        # nothing to be handed to — only the sandboxed question is read below.
        sandbox, _ = open_case(args)
    except ShapeError as e:
        return fail(e)
    try:
        if extra := sandbox.extra_inputs():
            raise ShapeError(
                ShapeExit.CONFIG_ERROR,
                f"this case has input files besides {args.prompt_file} "
                f"({', '.join(extra[:5])}); a model called directly cannot see them — "
                "use an agent (tp shape acp)",
            )
        if args.system_file:
            try:
                system = args.system_file.read_text()
            except OSError as e:
                raise ShapeError(
                    ShapeExit.CONFIG_ERROR, f"cannot read --system-file {args.system_file}: {e}"
                ) from None
        else:
            system = None
        url, headers, body = build_request(provider, args.model, sandbox.question, system)
        print(
            f"[trap] {provider} {args.model}: max_tokens={body.get('max_tokens', 'api-default')}, "
            "thinking=api-default",
            file=sys.stderr,
        )
        reply = _call(provider, url, headers, body, deadline)
    except ShapeError as e:
        return fail(e)
    finally:
        sandbox.close()
    print(f"[trap] usage as the vendor reported it: {reply.usage}", file=sys.stderr)
    code, why = exit_for(reply)
    if why:
        print(f"[trap] {why}", file=sys.stderr)
    if code in (ShapeExit.OK, ShapeExit.REFUSAL, ShapeExit.MAX_TOKENS) and reply.text:
        print(reply.text)
    return int(code)


def _call(
    provider: str, url: str, headers: dict[str, str], body: dict[str, Any], deadline: Deadline
) -> Reply:
    try:
        resp = httpx.post(url, headers=headers, json=body, timeout=max(deadline.remaining(), 1.0))
    except httpx.TimeoutException:
        raise ShapeError(ShapeExit.TIMEOUT, "the model did not answer before the deadline") from None
    except httpx.HTTPError as e:
        raise ShapeError(ShapeExit.AGENT_ERROR, f"the request failed: {e}") from None
    if resp.status_code >= 400:
        raise ShapeError(ShapeExit.AGENT_ERROR, f"HTTP {resp.status_code}: {resp.text[:500]}")
    try:
        data = resp.json()
    except ValueError:
        raise ShapeError(ShapeExit.AGENT_ERROR, "the reply was not JSON") from None
    if not isinstance(data, dict):
        raise ShapeError(ShapeExit.AGENT_ERROR, "the reply was not a JSON object")
    return parse_reply(provider, data)


if __name__ == "__main__":
    raise SystemExit(main())

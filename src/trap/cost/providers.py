from __future__ import annotations

import enum
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from trap.models.cost import CallUsage

# -- Response parsing ---------------------------------------------------------
#
# Every vendor's usage is normalised to CallUsage's four DISJOINT counts: uncached input,
# cache reads, cache writes, output. Anthropic reports that split natively; the
# OpenAI-compatible vendors report cached tokens as a subset of the prompt, so the parser
# takes them out of it. Field names are the vendors' documented ones (the verbatim samples
# and their sources are in tests/test_usage_shapes.py).


class _ProtocolStyle(enum.StrEnum):
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"
    OPENAI_COMPATIBLE = "openai-compatible"

    def parse(self, content_type: str, body: bytes) -> CallUsage:
        """One API call's normalised usage, from its (streamed or whole) response body."""
        is_streaming = "text/event-stream" in content_type
        match (self, is_streaming):
            case (_ProtocolStyle.ANTHROPIC_COMPATIBLE, True):
                return _anthropic_sse(body.decode("utf-8", errors="replace"))
            case (_ProtocolStyle.ANTHROPIC_COMPATIBLE, False):
                data = _json_object(body)
                return _anthropic_usage(_object(data.get("usage")), data.get("model"))
            case (_ProtocolStyle.OPENAI_COMPATIBLE, True):
                return _openai_sse(body.decode("utf-8", errors="replace"))
            case (_ProtocolStyle.OPENAI_COMPATIBLE, False):
                data = _json_object(body)
                return _openai_usage(_object(data.get("usage")), data.get("model"))
            case _:  # pragma: no cover - exhaustive over the two styles above
                raise ValueError(f"Unsupported protocol style: {self!r}")


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _json_object(raw: bytes | str) -> dict[str, Any]:
    try:
        return _object(json.loads(raw))
    except ValueError:  # JSONDecodeError and undecodable bytes are both ValueErrors
        return {}


def _count(value: object) -> int:
    """A token count off the wire; absent, null (the Anthropic API types its cache counts
    "number or null") or anything else that is not a count reads as 0."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _sse_events(text: str) -> Iterator[dict[str, Any]]:
    """The JSON objects on an SSE body's ``data:`` lines; anything else is skipped."""
    for line in text.splitlines():
        if line.startswith("data:"):
            event = _json_object(line[5:].strip())
            if event:
                yield event


def _anthropic_usage(usage: dict[str, Any], model: object) -> CallUsage:
    """Anthropic's ``input_tokens`` already excludes both cache counts. The TTL breakdown
    sums to ``cache_creation_input_tokens``; only its 1-hour slice is priced apart."""
    write = _count(usage.get("cache_creation_input_tokens"))
    write_1h = _count(_object(usage.get("cache_creation")).get("ephemeral_1h_input_tokens"))
    return CallUsage(
        model=model if isinstance(model, str) else None,
        prompt_tokens=_count(usage.get("input_tokens")),
        completion_tokens=_count(usage.get("output_tokens")),
        cache_read_tokens=_count(usage.get("cache_read_input_tokens")),
        cache_write_tokens=write,
        cache_write_1h_tokens=min(write_1h, write),
    )


def _anthropic_sse(text: str) -> CallUsage:
    """``message_start`` opens the usage and ``message_delta`` restates it CUMULATIVELY
    (input and cache counts included once server tools run), so the two are merged per
    field — a later non-null value wins. Never summed (that double counts), never replaced
    wholesale (a delta carrying only ``output_tokens`` would zero the input)."""
    usage: dict[str, Any] = {}
    model = None
    for event in _sse_events(text):
        if event.get("type") == "message_start":
            message = _object(event.get("message"))
            model = model or message.get("model")
            carried = _object(message.get("usage"))
        elif event.get("type") == "message_delta":
            carried = _object(event.get("usage"))
        else:
            continue
        usage |= {key: value for key, value in carried.items() if value is not None}
    return _anthropic_usage(usage, model)


def _openai_usage(usage: dict[str, Any], model: object) -> CallUsage:
    """The OpenAI-compatible shapes, where cached tokens are PART of the input: Chat
    Completions (``prompt_tokens`` / ``prompt_tokens_details``; OpenAI, DeepSeek, OpenRouter,
    Moonshot, Mistral) and the Responses API (``input_tokens`` / ``input_tokens_details``).
    Cache reads and writes (OpenAI GPT-5.6+, OpenRouter) both come out of the input. A cache
    hit is one count however many names it is reported under — DeepSeek's
    ``prompt_tokens_details.cached_tokens`` is the "Same as prompt_cache_hit_tokens", Moonshot
    also puts it top-level as ``cached_tokens`` — so the candidates are read once, as their
    max, never summed."""
    responses = "input_tokens" in usage and "prompt_tokens" not in usage
    details = _object(usage.get("input_tokens_details" if responses else "prompt_tokens_details"))
    total = _count(usage.get("input_tokens" if responses else "prompt_tokens"))
    hits = (details.get("cached_tokens"), usage.get("prompt_cache_hit_tokens"), usage.get("cached_tokens"))
    read = min(max(_count(hit) for hit in hits), total)
    write = min(_count(details.get("cache_write_tokens")), total - read)
    return CallUsage(
        model=model if isinstance(model, str) else None,
        prompt_tokens=total - read - write,
        completion_tokens=_count(usage.get("output_tokens" if responses else "completion_tokens")),
        cache_read_tokens=read,
        cache_write_tokens=write,
    )


def _openai_sse(text: str) -> CallUsage:
    """Chat Completions streams put usage on the last chunk (OpenAI with ``include_usage``,
    DeepSeek and OpenRouter always; Moonshot also inside the finish chunk's ``choices[0]``);
    the Responses API nests it in the Response that its ``response.completed`` (or
    ``.incomplete`` / ``.failed``) event carries. Other chunks have none or ``null``. The last
    real usage wins, so a trailing usage-only chunk counts and a later ``null`` cannot erase it."""
    usage: dict[str, Any] = {}
    model = None
    for chunk in _sse_events(text):
        carrier = _object(chunk.get("response")) or chunk
        model = carrier.get("model") or model
        found = carrier.get("usage")
        if not isinstance(found, dict) and isinstance(choices := carrier.get("choices"), list) and choices:
            found = _object(choices[0]).get("usage")
        if isinstance(found, dict):
            usage = found
    return _openai_usage(usage, model)


# -- Provider registry --------------------------------------------------------


@dataclass(frozen=True)
class _ProviderConfig:
    key_env: str  # env var name for the API key
    # Base-URL env vars the proxy points at this provider's port -- more than one when
    # different clients read different names for the same endpoint. The first one a user
    # has set is the upstream override.
    base_envs: tuple[str, ...]
    upstream: str  # default upstream base URL
    style: _ProtocolStyle  # request/response format used by this provider
    always_intercept: bool = False  # True for OAuth-based tools that set no API key env var
    # The provider the report files these calls under, when the registry key only names a
    # second endpoint of it (default: the registry key).
    label: str | None = None

    def resolve_upstream(self) -> str:
        """Return the effective upstream URL, honouring any user-set env override."""
        return next((url for env in self.base_envs if (url := os.environ.get(env))), self.upstream)


# Central registry of supported LLM providers, one proxy port per entry.
#
# To add a new provider: add an entry to _CONFIGS. Set always_intercept=True for
# OAuth-based tools (Claude Code, …) that set no API key env var but still respect
# the base URL override.
_CONFIGS: dict[str, _ProviderConfig] = {
    "anthropic": _ProviderConfig(
        "ANTHROPIC_API_KEY",
        ("ANTHROPIC_BASE_URL",),
        "https://api.anthropic.com",
        style=_ProtocolStyle.ANTHROPIC_COMPATIBLE,
        always_intercept=True,
    ),
    "openai": _ProviderConfig(
        "OPENAI_API_KEY",
        ("OPENAI_BASE_URL",),
        "https://api.openai.com/v1",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
        always_intercept=True,
    ),
    "mistral": _ProviderConfig(
        "MISTRAL_API_KEY",
        ("MISTRAL_BASE_URL",),
        "https://api.mistral.ai",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
    ),
    "moonshot": _ProviderConfig(
        "MOONSHOT_API_KEY",
        # MOONSHOT_API_BASE is the var litellm (what Aider and similar frameworks call under
        # the hood) auto-reads -- confirmed in litellm/llms/moonshot/chat/transformation.py's
        # _get_openai_compatible_provider_info(). No standard SDK auto-reads a Moonshot
        # base-URL var, so a direct OpenAI-SDK caller must read this one explicitly. A .cn
        # account overrides it to https://api.moonshot.cn/v1.
        ("MOONSHOT_API_BASE",),
        # OpenAI SDK drops /v1 when the base URL is overridden → upstream carries it.
        "https://api.moonshot.ai/v1",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
    ),
    "deepseek": _ProviderConfig(
        "DEEPSEEK_API_KEY",
        # The DeepSeek harness (deepseek-ai/deepseek-harness, packages/llm/llm-deepseek) reads
        # DEEPSEEK_BASE_URL -- from the inherited environment only, never a .env file -- and
        # POSTs streamed chat completions to `${base}/chat/completions`, no /v1, from a
        # default of https://api.deepseek.com; so the upstream carries no suffix either.
        ("DEEPSEEK_BASE_URL",),
        "https://api.deepseek.com",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
    ),
    "deepseek-search": _ProviderConfig(
        "DEEPSEEK_API_KEY",
        # The same harness's web_search tool is a separate billed call, non-streaming and in
        # Anthropic format, to `${DEEPSEEK_SEARCH_BASE_URL}/messages` (packages/web/
        # web-search-deepseek) -- which DEEPSEEK_BASE_URL does not reach. Filed as deepseek.
        ("DEEPSEEK_SEARCH_BASE_URL",),
        "https://api.deepseek.com/anthropic/v1",
        style=_ProtocolStyle.ANTHROPIC_COMPATIBLE,
        label="deepseek",
    ),
    "openrouter": _ProviderConfig(
        "OPENROUTER_API_KEY",
        # OpenRouter's TypeScript SDK (@openrouter/sdk) reads OPENROUTER_BASE_URL, but litellm
        # -- what Aider and similar frameworks call under the hood -- reads OPENROUTER_API_BASE
        # (litellm/main.py's openrouter branch); neither reads the other's. Both get the port.
        # Both bases carry /api/v1, and the clients append /chat/completions to it.
        ("OPENROUTER_BASE_URL", "OPENROUTER_API_BASE"),
        "https://openrouter.ai/api/v1",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
    ),
}


def active_provider_configs() -> dict[str, _ProviderConfig]:
    """Providers active in this environment: those with their API key env var set,
    plus the always-intercept ones (OAuth tools that honour the base URL override)."""
    return {
        name: cfg for name, cfg in _CONFIGS.items() if os.environ.get(cfg.key_env) or cfg.always_intercept
    }

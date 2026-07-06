from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass

# -- Provider registry --------------------------------------------------------


class _ProtocolStyle(enum.StrEnum):
    ANTHROPIC_COMPATIBLE = "anthropic-compatible"
    OPENAI_COMPATIBLE = "openai-compatible"

    def parse(self, content_type: str, body: bytes) -> tuple[int, int, str | None]:
        """Extract (prompt_tokens, completion_tokens, model) from an API response."""
        is_streaming = "text/event-stream" in content_type
        match (self, is_streaming):
            case (_ProtocolStyle.ANTHROPIC_COMPATIBLE, True):
                return self._parse_anthropic_style_sse(body.decode("utf-8", errors="replace"))
            case (_ProtocolStyle.ANTHROPIC_COMPATIBLE, False):
                return self._parse_json(body, "input_tokens", "output_tokens")
            case (_ProtocolStyle.OPENAI_COMPATIBLE, True):
                return self._parse_openai_style_sse(body.decode("utf-8", errors="replace"))
            case (_ProtocolStyle.OPENAI_COMPATIBLE, False):
                return self._parse_json(body, "prompt_tokens", "completion_tokens")
            case _:  # pragma: no cover - exhaustive over the two styles above
                raise ValueError(f"Unsupported protocol style: {self!r}")

    @staticmethod
    def _parse_json(body: bytes, prompt_key: str, completion_key: str) -> tuple[int, int, str | None]:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            return 0, 0, None
        usage = data.get("usage", {})
        return usage.get(prompt_key, 0), usage.get(completion_key, 0), data.get("model")

    @staticmethod
    def _parse_anthropic_style_sse(text: str) -> tuple[int, int, str | None]:
        prompt_tokens = 0
        completion_tokens = 0
        model = None
        for line in text.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except (json.JSONDecodeError, ValueError):
                continue
            if event.get("type") == "message_start":
                msg = event.get("message", {})
                usage = msg.get("usage", {})
                prompt_tokens += usage.get("input_tokens", 0)
                model = model or msg.get("model")
            elif event.get("type") == "message_delta":
                completion_tokens += event.get("usage", {}).get("output_tokens", 0)
        return prompt_tokens, completion_tokens, model

    @staticmethod
    def _parse_openai_style_sse(text: str) -> tuple[int, int, str | None]:
        for line in text.splitlines():
            if not line.startswith("data: ") or "[DONE]" in line:
                continue
            try:
                chunk = json.loads(line[6:])
            except (json.JSONDecodeError, ValueError):
                continue
            usage = chunk.get("usage")
            if usage:
                return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), chunk.get("model")
        return 0, 0, None


@dataclass(frozen=True)
class _ProviderConfig:
    key_env: str  # env var name for the API key
    base_env: str  # env var name for the base URL override
    upstream: str  # default upstream base URL
    style: _ProtocolStyle  # request/response format used by this provider
    always_intercept: bool = False  # True for OAuth-based tools that set no API key env var

    def resolve_upstream(self) -> str:
        """Return the effective upstream URL, honouring any user-set env override."""
        return os.environ.get(self.base_env) or self.upstream


# Central registry of supported LLM providers.
#
# To add a new provider: add an entry to _CONFIGS. Set always_intercept=True for
# OAuth-based tools (Claude Code, Codex CLI, …) that set no API key env var but
# still respect the base URL override.
_CONFIGS: dict[str, _ProviderConfig] = {
    "anthropic": _ProviderConfig(
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "https://api.anthropic.com",
        style=_ProtocolStyle.ANTHROPIC_COMPATIBLE,
        always_intercept=True,
    ),
    "openai": _ProviderConfig(
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "https://api.openai.com/v1",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
        always_intercept=True,
    ),
    "mistral": _ProviderConfig(
        "MISTRAL_API_KEY",
        "MISTRAL_BASE_URL",
        "https://api.mistral.ai",
        style=_ProtocolStyle.OPENAI_COMPATIBLE,
    ),
}


def active_provider_configs() -> dict[str, _ProviderConfig]:
    """Providers active in this environment: those with their API key env var set,
    plus the always-intercept ones (OAuth tools that honour the base URL override)."""
    return {
        name: cfg for name, cfg in _CONFIGS.items() if os.environ.get(cfg.key_env) or cfg.always_intercept
    }

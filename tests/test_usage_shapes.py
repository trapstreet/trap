"""Real response shapes, provider x streaming/non-streaming → the normalised four counts.

Each fixture is the vendor's documented sample (verbatim where the docs have one, with the
URL beside it), so a parser change that misreads a real response fails here. The four
counts are disjoint: ``prompt_tokens`` is the uncached input, whatever the vendor calls it.
"""

from __future__ import annotations

import json

import pytest

from trap.cost.providers import _ProtocolStyle
from trap.models.cost import CallUsage

ANTH = _ProtocolStyle.ANTHROPIC_COMPATIBLE
OAI = _ProtocolStyle.OPENAI_COMPATIBLE
JSON = "application/json"
SSE = "text/event-stream; charset=utf-8"


def _sse(*events: dict | str) -> bytes:
    """An SSE body: dicts become ``data:`` lines, strings pass through verbatim."""
    lines = [e if isinstance(e, str) else f"data: {json.dumps(e)}" for e in events]
    return ("\n\n".join(lines) + "\n\n").encode()


# -- Anthropic Messages API ---------------------------------------------------------
# https://platform.claude.com/docs/en/build-with-claude/prompt-caching — "input_tokens:
# Number of input tokens which were not read from or used to create a cache", and
# cache_creation_input_tokens "equals the sum of the values in the cache_creation object".

ANTHROPIC_CACHED_USAGE = {  # the caching page's sample, verbatim
    "input_tokens": 2048,
    "cache_read_input_tokens": 1800,
    "cache_creation_input_tokens": 248,
    "output_tokens": 503,
    "cache_creation": {"ephemeral_5m_input_tokens": 148, "ephemeral_1h_input_tokens": 100},
}


def test_anthropic_json_splits_cache_reads_writes_and_the_1h_slice():
    body = json.dumps({"type": "message", "model": "claude-opus-5", "usage": ANTHROPIC_CACHED_USAGE})
    assert ANTH.parse(JSON, body.encode()) == CallUsage(
        model="claude-opus-5",
        prompt_tokens=2048,  # already uncached: Anthropic excludes both cache counts
        cache_read_tokens=1800,
        cache_write_tokens=248,
        cache_write_1h_tokens=100,
        completion_tokens=503,
    )


def test_anthropic_json_null_cache_fields_read_as_zero():
    # the API reference types both cache counts and the breakdown as "number or null"
    usage = {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": None}
    usage |= {"cache_creation_input_tokens": None, "cache_creation": None}
    body = json.dumps({"model": "claude-haiku-4-5", "usage": usage}).encode()
    assert ANTH.parse(JSON, body) == CallUsage(model="claude-haiku-4-5", prompt_tokens=7, completion_tokens=3)


# https://platform.claude.com/docs/en/build-with-claude/streaming — "The token counts shown
# in the usage field of the message_delta event are cumulative", and MessageDeltaUsage may
# carry input_tokens / cache_*_input_tokens too. So the two events are merged per field
# (a later non-null value wins), never summed, and never replaced wholesale.


def test_anthropic_sse_simple_stream():
    # the streaming page's basic sample, verbatim
    body = (
        "event: message_start\n"
        'data: {"type": "message_start", "message": {"id": "msg_1nZdL29xx5MUA1yADyHTEsnR8uuvGzszyY", '
        '"type": "message", "role": "assistant", "content": [], "model": "claude-opus-5", '
        '"stop_reason": null, "stop_sequence": null, "usage": {"input_tokens": 25, "output_tokens": 1}}}\n\n'
        "event: message_delta\n"
        'data: {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence":null}, '
        '"usage": {"output_tokens": 15}}\n\n'
    )
    assert ANTH.parse(SSE, body.encode()) == CallUsage(
        model="claude-opus-5", prompt_tokens=25, completion_tokens=15
    )


def test_anthropic_sse_cumulative_delta_overrides_the_start():
    # the streaming page's web-search sample, verbatim: input grows mid-stream (server tool
    # use), and the delta's cumulative 10682 is the call's input — not 2679, not the sum
    body = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_01G...","type":"message","role":"assistant",'
        '"model":"claude-opus-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":'
        '{"input_tokens":2679,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":3}}}\n\n'
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":'
        '{"input_tokens":10682,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,'
        '"output_tokens":510,"server_tool_use":{"web_search_requests":1}}}\n\n'
    )
    assert ANTH.parse(SSE, body.encode()) == CallUsage(
        model="claude-opus-5", prompt_tokens=10682, completion_tokens=510
    )


def test_anthropic_sse_cache_counts_from_the_start_survive_a_delta_without_them():
    # message_start carries the cache counts and the TTL breakdown; a delta carrying only
    # output_tokens must not zero them (the breakdown never appears in a delta at all)
    body = _sse(
        {"type": "message_start", "message": {"model": "claude-sonnet-5", "usage": ANTHROPIC_CACHED_USAGE}},
        {"type": "ping"},
        {"type": "message_delta", "usage": {"output_tokens": 900, "cache_read_input_tokens": None}},
        "data: notjson",
        "ignored line",
    )
    assert ANTH.parse(SSE, body) == CallUsage(
        model="claude-sonnet-5",
        prompt_tokens=2048,
        cache_read_tokens=1800,
        cache_write_tokens=248,
        cache_write_1h_tokens=100,
        completion_tokens=900,
    )


# -- OpenAI Chat Completions --------------------------------------------------------
# openai-python src/openai/types/completion_usage.py: PromptTokensDetails.cached_tokens
# ("Cached tokens present in the prompt") and .cache_write_tokens ("The unadjusted number of
# prompt tokens written to cache", GPT-5.6+) are both parts of prompt_tokens.


def test_openai_chat_sse_recorded_stream():
    # openai-python's committed recording (gpt-4o, include_usage), last chunks verbatim:
    # .inline-snapshot/external/83b060bae42eb41c4f1edbb7c1542b954b37d9dfd1910b964ddebc9677e6ae85.bin
    body = (
        'data: {"id":"chatcmpl-ABfw5EzoqmfXjnnsXY7Yd8OC6tb3c","object":"chat.completion.chunk",'
        '"created":1727346173,'
        '"model":"gpt-4o-2024-08-06","system_fingerprint":"fp_5050236cbd","choices":[{"index":0,"delta":{},'
        '"logprobs":null,"finish_reason":"stop"}]}\n\n'
        'data: {"id":"chatcmpl-ABfw5EzoqmfXjnnsXY7Yd8OC6tb3c","object":"chat.completion.chunk",'
        '"created":1727346173,'
        '"model":"gpt-4o-2024-08-06","system_fingerprint":"fp_5050236cbd","choices":[],"usage":{"prompt_tokens":9,'
        '"completion_tokens":2,"total_tokens":11,"completion_tokens_details":{"reasoning_tokens":0}}}\n\n'
        "data: [DONE]\n\n"
    )
    assert OAI.parse(SSE, body.encode()) == CallUsage(
        model="gpt-4o-2024-08-06", prompt_tokens=9, completion_tokens=2
    )


def test_openai_chat_json_documented_sample():
    # the openapi.yaml chat completion sample's usage, verbatim
    usage = {"prompt_tokens": 19, "completion_tokens": 10, "total_tokens": 29}
    usage |= {"prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 0}}
    usage |= {"completion_tokens_details": {"reasoning_tokens": 0, "audio_tokens": 0}}
    body = json.dumps({"object": "chat.completion", "model": "gpt-5.5", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(model="gpt-5.5", prompt_tokens=19, completion_tokens=10)


def test_openai_chat_json_cache_reads_and_gpt_5_6_writes():
    usage = {"prompt_tokens": 2006, "completion_tokens": 300, "total_tokens": 2306}
    usage |= {"prompt_tokens_details": {"cached_tokens": 1920, "cache_write_tokens": 64}}
    body = json.dumps({"model": "gpt-5.6-terra", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="gpt-5.6-terra",
        prompt_tokens=22,
        cache_read_tokens=1920,
        cache_write_tokens=64,
        completion_tokens=300,
    )


# -- OpenAI Responses API -----------------------------------------------------------
# openai-python src/openai/types/responses/response_usage.py; the prompt-caching guide's own
# cost function: ordinaryInputTokens = inputTokens - cachedTokens - cacheWriteTokens.


def test_openai_responses_json_documented_sample():
    # the openapi.yaml Response sample's usage, verbatim
    usage = {"input_tokens": 36, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}
    usage |= {"output_tokens": 87, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 123}
    body = json.dumps({"object": "response", "model": "gpt-6-astra", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(model="gpt-6-astra", prompt_tokens=36, completion_tokens=87)


def test_openai_responses_json_cached_input():
    # Codex's own parser test shape: 100 input of which 40 read from and 60 written to the cache
    usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 60}}
    usage |= {"output_tokens": 7, "total_tokens": 107}
    body = json.dumps({"object": "response", "model": "gpt-5.6-sol", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="gpt-5.6-sol", cache_read_tokens=40, cache_write_tokens=60, completion_tokens=7
    )


def test_openai_responses_sse_usage_is_nested_under_response():
    # response.created / in_progress carry "usage": null; response.completed carries the
    # Response, usage inside it. Its data line, verbatim from openapi.yaml (the sample omits
    # input_tokens_details, so a missing details object must read as no cache). No [DONE].
    completed = (
        "event: response.completed\n"
        'data: {"type":"response.completed","response":'
        '{"id":"resp_67c9fdcecf488190bdd9a0409de3a1ec07b8b0ad4e5eb654",'
        '"object":"response","created_at":1741290958,"status":"completed","error":null,"incomplete_details":null,'
        '"instructions":"You are a helpful assistant.","max_output_tokens":null,"model":"gpt-6-astra",'
        '"output":'
        '[{"id":"msg_67c9fdcf37fc8190ba82116e33fb28c507b8b0ad4e5eb654","type":"message","status":"completed",'
        '"role":"assistant","content":[{"type":"output_text","text":"Hi there! How can I assist you today?",'
        '"annotations":[]}]}],"parallel_tool_calls":true,"previous_response_id":null,"reasoning":{"effort":null,'
        '"summary":null},"store":true,"temperature":1.0,"text":{"format":{"type":"text"}},"tool_choice":"auto",'
        '"tools":[],"top_p":1.0,"truncation":"disabled","usage":{"input_tokens":37,"output_tokens":11,'
        '"output_tokens_details":{"reasoning_tokens":0},"total_tokens":48},"user":null,"metadata":{}}}\n\n'
    )
    created = {"type": "response.created", "response": {"model": "gpt-6-astra", "usage": None}}
    delta = {"type": "response.output_text.delta", "delta": "Hi", "sequence_number": 3}
    body = _sse(created, delta).decode() + completed
    assert OAI.parse(SSE, body.encode()) == CallUsage(
        model="gpt-6-astra", prompt_tokens=37, completion_tokens=11
    )


def test_openai_responses_sse_incomplete_response_is_still_billed():
    # response.incomplete carries the same Response object; an incomplete response is billed
    usage = {"input_tokens": 5000, "input_tokens_details": {"cached_tokens": 4096}, "output_tokens": 2048}
    body = _sse({"type": "response.incomplete", "response": {"model": "gpt-5.5", "usage": usage}})
    assert OAI.parse(SSE, body) == CallUsage(
        model="gpt-5.5", prompt_tokens=904, cache_read_tokens=4096, completion_tokens=2048
    )


# -- Moonshot (Kimi) ----------------------------------------------------------------
# https://platform.kimi.ai/docs/api/chat: a top-level usage.cached_tokens ("Number of tokens
# served from cache"), part of prompt_tokens.


def test_moonshot_json_top_level_cached_tokens():
    # the API reference's example usage
    usage = {"prompt_tokens": 19, "completion_tokens": 21, "total_tokens": 40, "cached_tokens": 10}
    body = json.dumps({"model": "kimi-k3", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="kimi-k3", prompt_tokens=9, cache_read_tokens=10, completion_tokens=21
    )


def test_moonshot_sse_usage_in_the_finish_choice_and_again_top_level():
    # https://platform.kimi.ai/docs/guide/utilize-the-streaming-output-feature-of-kimi-api —
    # the same usage twice, inside choices[0] of the finish chunk and top-level after it: read once
    finish = (
        'data: {"id":"cmpl-1305b94c570f447fbde3180560736287","object":"chat.completion.chunk",'
        '"created":1698999575,'
        '"model":"kimi-k3","choices":[{"index":0,"delta":{},"finish_reason":"stop","usage":{"prompt_tokens":19,'
        '"completion_tokens":13,"total_tokens":32}}]}\n\n'
    )
    final = (
        'data: {"id":"cmpl-1305b94c570f447fbde3180560736287","object":"chat.completion.chunk",'
        '"created":1698999575,'
        '"model":"kimi-k3","choices":[],"usage":{"prompt_tokens":19,"completion_tokens":13,"total_tokens":32}}\n\n'
    )
    expected = CallUsage(model="kimi-k3", prompt_tokens=19, completion_tokens=13)
    assert OAI.parse(SSE, (finish + final).encode()) == expected
    # a stream whose only usage is the one inside the finish choice is still metered
    assert OAI.parse(SSE, finish.encode()) == expected


# -- Mistral ------------------------------------------------------------------------
# https://docs.mistral.ai/studio-api/conversations/advanced/prompt-caching: "The billable
# uncached input tokens are prompt_tokens - cached_tokens"; the page's sample, verbatim.


def test_mistral_json_cached_prompt_tokens():
    usage = {"prompt_tokens": 1013, "total_tokens": 1043, "completion_tokens": 30}
    usage |= {"prompt_tokens_details": {"cached_tokens": 1008}}
    body = json.dumps({"model": "mistral-medium-3-5", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="mistral-medium-3-5", prompt_tokens=5, cache_read_tokens=1008, completion_tokens=30
    )


# -- DeepSeek (OpenAI-compatible) ---------------------------------------------------
# https://api-docs.deepseek.com/api/create-chat-completion — prompt_tokens "equals
# prompt_cache_hit_tokens + prompt_cache_miss_tokens", and prompt_tokens_details.cached_tokens
# is the "Same as prompt_cache_hit_tokens": one number reported twice, counted once.

DEEPSEEK_SAMPLE_USAGE = {  # the API reference's sample, verbatim
    "completion_tokens": 10,
    "prompt_tokens": 16,
    "total_tokens": 26,
    "prompt_tokens_details": {"cached_tokens": 0},
    "prompt_cache_hit_tokens": 0,
    "prompt_cache_miss_tokens": 16,
}


def test_deepseek_json_documented_sample():
    body = json.dumps({"model": "deepseek-flash", "usage": DEEPSEEK_SAMPLE_USAGE}).encode()
    assert OAI.parse(JSON, body) == CallUsage(model="deepseek-flash", prompt_tokens=16, completion_tokens=10)


def test_deepseek_json_cache_hit_is_counted_once():
    usage = {"completion_tokens": 50, "prompt_tokens": 1000, "total_tokens": 1050}
    usage |= {"prompt_tokens_details": {"cached_tokens": 800}}
    usage |= {"prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}
    body = json.dumps({"model": "deepseek-v4-pro", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="deepseek-v4-pro", prompt_tokens=200, cache_read_tokens=800, completion_tokens=50
    )


def test_deepseek_json_hit_count_alone_is_enough():
    # a DeepSeek-compatible host that reports only the DeepSeek-native field
    usage = {"completion_tokens": 5, "prompt_tokens": 100, "prompt_cache_hit_tokens": 64}
    body = json.dumps({"model": "deepseek-flash", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="deepseek-flash", prompt_tokens=36, cache_read_tokens=64, completion_tokens=5
    )


def test_deepseek_sse_usage_rides_the_last_chunk():
    # "the last chunk before the data: [DONE] message carries the token usage statistics for
    # the entire request" — the documented final chunk, verbatim, after a content chunk
    body = (
        'data: {"choices": [{"delta": {"content": "Hi", "role": "assistant"}, "index": 0}], '
        '"model": "deepseek-flash", "object": "chat.completion.chunk", "usage": null}\n\n'
        'data: {"choices": [{"delta": {"content": "", "role": null}, "finish_reason": "stop", "index": 0, '
        '"logprobs": null}], "created": 1718345013, "id": "1f633d8bfc032625086f14113c411638", '
        '"model": "deepseek-flash", "object": "chat.completion.chunk", '
        '"system_fingerprint": "fp_a49d71b8a1", '
        '"usage": {"completion_tokens": 9, "prompt_tokens": 17, "total_tokens": 26, "prompt_tokens_details": '
        '{"cached_tokens": 0}, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 17}}\n\n'
        "data: [DONE]\n\n"
    )
    assert OAI.parse(SSE, body.encode()) == CallUsage(
        model="deepseek-flash", prompt_tokens=17, completion_tokens=9
    )


def test_deepseek_sse_cached_stream_and_a_trailing_usage_only_chunk():
    # the harness's own types note usage may also come "as a trailing usage-only chunk": the
    # last non-null usage wins, and a later "usage": null cannot erase it
    usage = {"completion_tokens": 40, "prompt_tokens": 5000, "prompt_tokens_details": {"cached_tokens": 4864}}
    usage |= {"prompt_cache_hit_tokens": 4864, "prompt_cache_miss_tokens": 136}
    body = _sse(
        {"choices": [{"delta": {"content": "x"}, "index": 0}], "model": "deepseek-flash", "usage": None},
        {"choices": [], "model": "deepseek-flash", "usage": usage},
        {"choices": [], "model": "deepseek-flash", "usage": None},
        "data: [DONE]",
    )
    assert OAI.parse(SSE, body) == CallUsage(
        model="deepseek-flash", prompt_tokens=136, cache_read_tokens=4864, completion_tokens=40
    )


# -- OpenRouter (OpenAI-compatible) -------------------------------------------------
# https://openrouter.ai/openapi.json ChatUsage, and the usage-accounting guide: "Full usage
# details are now always included automatically in every response". Cache reads
# (prompt_tokens_details.cached_tokens) and writes (.cache_write_tokens, only for models with
# explicit caching) both sit inside prompt_tokens — OpenRouter normalises to OpenAI's shape.

OPENROUTER_RECORDED_STREAM = (  # verbatim tail of a recorded stream, anthropic/claude-opus-4.7 on Bedrock:
    # github.com/sst/opencode packages/llm/test/fixtures/recordings/openai-compatible-chat/
    # openrouter-claude-opus-4-7-drives-a-tool-loop.json @ 193de13
    ": OPENROUTER PROCESSING\n\n"
    ": OPENROUTER PROCESSING\n\n"
    'data: {"id":"gen-1778031311-S3NlfYGRwAnOoPoNrThK","object":"chat.completion.chunk","created":1778031311,'
    '"model":"anthropic/claude-4.7-opus-20260416","provider":"Amazon Bedrock","service_tier":"standard",'
    '"choices":[{"index":0,"delta":{"content":"","role":"assistant"},"finish_reason":"tool_calls",'
    '"native_finish_reason":"tool_use"}],"usage":{"prompt_tokens":802,"completion_tokens":66,'
    '"total_tokens":868,"cost":0.00566,"is_byok":false,"prompt_tokens_details":{"cached_tokens":0,'
    '"cache_write_tokens":0,"audio_tokens":0,"video_tokens":0},"cost_details":{"upstream_inference_cost":'
    '0.00566,"upstream_inference_prompt_cost":0.00401,"upstream_inference_completions_cost":0.00165},'
    '"completion_tokens_details":{"reasoning_tokens":0,"image_tokens":0,"audio_tokens":0}}}\n\n'
    "data: [DONE]\n\n"
)


def test_openrouter_sse_recorded_stream():
    # the usage chunk still carries a choice (OpenRouter departs from OpenAI's empty choices),
    # and the ": OPENROUTER PROCESSING" keep-alive comments are skipped
    assert OAI.parse(SSE, OPENROUTER_RECORDED_STREAM.encode()) == CallUsage(
        model="anthropic/claude-4.7-opus-20260416", prompt_tokens=802, completion_tokens=66
    )


def test_openrouter_json_splits_reads_and_writes_out_of_the_prompt():
    usage = {"prompt_tokens": 10339, "completion_tokens": 120, "total_tokens": 10459, "cost": 0.0042}
    usage |= {"prompt_tokens_details": {"cached_tokens": 8000, "cache_write_tokens": 2000, "audio_tokens": 0}}
    body = json.dumps({"model": "anthropic/claude-sonnet-4.6", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="anthropic/claude-sonnet-4.6",
        prompt_tokens=339,
        cache_read_tokens=8000,
        cache_write_tokens=2000,
        completion_tokens=120,
    )


def test_openrouter_cache_counts_never_exceed_the_prompt():
    # a malformed report cannot drive the uncached input negative
    usage = {"prompt_tokens": 100, "completion_tokens": 1}
    usage |= {"prompt_tokens_details": {"cached_tokens": 90, "cache_write_tokens": 50}}
    body = json.dumps({"model": "m", "usage": usage}).encode()
    assert OAI.parse(JSON, body) == CallUsage(
        model="m", prompt_tokens=0, cache_read_tokens=90, cache_write_tokens=10, completion_tokens=1
    )


# -- malformed / empty --------------------------------------------------------------


@pytest.mark.parametrize("style", [ANTH, OAI])
def test_unparseable_bodies_meter_nothing(style):
    assert style.parse(JSON, b"notjson") == CallUsage()
    assert style.parse(JSON, b"[1, 2]") == CallUsage()
    assert style.parse(JSON, json.dumps({"model": "m", "usage": "junk"}).encode()) == CallUsage(model="m")
    assert style.parse(SSE, b"event: ping\ndata: {}\ndata: bad\ndata: [3]\n") == CallUsage()
    assert CallUsage().is_empty

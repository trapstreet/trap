# Cost tracking

trap measures LLM token usage and spend per case — no changes to your solution code. On
by default; disable with `tp run --no-cost`.

## How it works

Before each case, trap starts a local reverse proxy per active provider and points the
provider's base-URL env var (e.g. `ANTHROPIC_BASE_URL`) at it. The proxy forwards every
request to the real API, reads the token counts off the response, then shuts down after
the case. The solution uses the same SDK and key — the proxy is transparent.

## Auto-detection

Activates when a provider's key env var is set:

| Provider | Key env var | Base URL redirected |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `ANTHROPIC_BASE_URL` |
| OpenAI | `OPENAI_API_KEY` | `OPENAI_BASE_URL` |
| Mistral | `MISTRAL_API_KEY` | `MISTRAL_BASE_URL` |
| Moonshot (Kimi) | `MOONSHOT_API_KEY` | `MOONSHOT_API_BASE` |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_BASE_URL`, and `DEEPSEEK_SEARCH_BASE_URL` (the DeepSeek harness's web search) |
| OpenRouter | `OPENROUTER_API_KEY` | `OPENROUTER_BASE_URL` and `OPENROUTER_API_BASE` (both, one port) |

**Claude Code** (`claude -p`) is always intercepted (OAuth, no key env var). With no key
set and no always-intercept provider, cost tracking is a no-op.

A base-URL var you set yourself is where the proxy forwards to, so a solution pointed at
a gateway or a compatible endpoint is still metered — see the Claude Code note below.

## Provider support

- **Anthropic, OpenAI, Claude Code** — work out of the box (the SDK auto-reads the base URL).
- **Claude Code on an Anthropic-compatible endpoint** — set `ANTHROPIC_BASE_URL` to it as
  that vendor's guide says (DeepSeek: `https://api.deepseek.com/anthropic`; OpenRouter:
  `https://openrouter.ai/api`). trap forwards there, keeps the path, and prices each call
  at the rates of the model the endpoint reports — DeepSeek's for `deepseek-flash`, not
  Anthropic's.
- **Mistral** — needs one line, as its SDK doesn't auto-read the env var:
  ```python
  client = Mistral(api_key=os.environ.get("MISTRAL_API_KEY"),
                   server_url=os.environ.get("MISTRAL_BASE_URL"))   # only set under trap
  ```
- **Moonshot (Kimi)** — trap redirects `MOONSHOT_API_BASE`, the var litellm (used internally
  by frameworks like Aider) auto-reads, so a litellm-based solution works out of the box.
  No standard SDK auto-reads a Moonshot base-URL var, so a solution calling the OpenAI SDK
  directly should read the same var explicitly
  (`base_url=os.environ.get("MOONSHOT_API_BASE")`).
- **DeepSeek** — the DeepSeek harness reads `DEEPSEEK_BASE_URL` (from the environment it
  inherits, not a `.env` file) and works out of the box. Its `web_search` tool is a second
  billed call, in Anthropic format, to `DEEPSEEK_SEARCH_BASE_URL`; trap redirects that too
  and files it under `deepseek`. A direct OpenAI-SDK caller reads
  `base_url=os.environ.get("DEEPSEEK_BASE_URL")`.
- **OpenRouter** — clients disagree on the var: OpenRouter's TypeScript SDK reads
  `OPENROUTER_BASE_URL`, litellm (and so Aider) reads `OPENROUTER_API_BASE`. trap points
  both at the same port, so either works out of the box; a direct OpenAI-SDK caller reads
  `base_url=os.environ.get("OPENROUTER_BASE_URL")`.
- **Codex CLI** — not metered out of the box: current releases ignore `OPENAI_BASE_URL`
  (the base URL is the `openai_base_url` config key), and prefer a WebSocket transport the
  proxy does not read.
- **AWS Bedrock / Google Vertex** — unsupported (SDK-level auth, no redirectable base URL); the run still works, cost is just absent.

## In report.json

Each case carries a `cost` object — or `null` if the solution made no LLM calls. Its
`by_model` list has one entry per (provider, model), each with:

| Field | Meaning |
|---|---|
| `prompt_tokens` | **uncached** input tokens |
| `cache_read_tokens` | input tokens read from the provider's prompt cache |
| `cache_write_tokens` | input tokens written to the cache (every TTL) |
| `completion_tokens` | output tokens (reasoning included) |
| `cost_usd` | what the calls cost, cache included |
| `cache_cost_usd` | the part of `cost_usd` spent on cache reads and writes |
| `calls` | API calls |

The four token counts are **disjoint**: the input a model was sent is
`prompt_tokens + cache_read_tokens + cache_write_tokens`. That is Anthropic's own split;
vendors that report cached tokens as part of the prompt (OpenAI, DeepSeek, OpenRouter,
Moonshot, Mistral) have them taken out of `prompt_tokens`, so the counts compare across
vendors. It is also the split the site uses (`input` / `cache_read` / `cache_creation`).
`cache_cost_usd` is inside `cost_usd`, never on top of it. The `cost` object also sums
every field over its models; the terminal table shows those per-case sums, with
`cache_rd` / `cache_wr` columns when any case used the cache.

`cost_usd` and `cache_cost_usd` are `null` when unknown — see below. A report written
before cache accounting loads with cache counts of 0 and `cache_cost_usd` `null`.

## Pricing

The price table is served data, not CLI code — trapstreet.run is the source of
truth (`GET /api/pricing`, kept fresh server-side). The CLI resolves prices through
a chain that never blocks or breaks a run (`PriceCatalogue.resolve` in
`src/trap/cost/pricing.py`):

1. a local cache fresher than 24h (`~/.config/trapstreet/pricing.json`);
2. a best-effort server fetch (3s timeout; any failure is silent);
3. the stale cache (still newer than the wheel);
4. `default_prices.json` bundled in the wheel — a captured `/api/pricing` snapshot,
   used only as a last resort (fresh install, offline, no cache).

All four sources share one JSON shape (`PriceTable`) and one parse path, so a
malformed or wrong-`unit` payload falls through to the next source rather than
mispricing. Rows are prefix-matched against the model id the API reports; the table
is ordered specific-first, so the first matching prefix is the most specific one
(e.g. `gpt-5.5-pro` before `gpt-5.5`). A price update is a data change on the
server — no CLI release. `TRAPSTREET_URL` redirects the fetch (e.g. at UAT),
`TRAP_PRICING_CACHE` relocates the cache file.

### Cache rates

A row may carry `cache_read_per_mtok` and `cache_write_per_mtok` (a standard write —
Anthropic's 5-minute TTL); both are optional, and a client that predates them ignores
them. A row without them falls back to the vendor's documented multiples of its input
rate (`CACHE_MULTIPLIERS` in `pricing.py`, each with its source):

| Model family | Cache read | Cache write |
|---|---|---|
| Claude | 0.1x (0.025x on Fable 5.1 / Mythos 5.1) | 1.25x (5-minute), 2x (1-hour) |
| GPT-5.6+, GPT-6 | 0.1x | 1.25x |
| GPT-5 – 5.5 | 0.1x | no write fee |
| GPT-4.1, o3, o4-mini | 0.25x | no write fee |
| GPT-4o, o1, o3-mini | 0.5x | no write fee |
| `-pro` models (gpt-5-pro … gpt-5.5-pro, o1-pro, o3-pro) | 1x (no cached discount) | no write fee |
| DeepSeek (`deepseek-flash`, `-v4-flash`, `-v4-pro`) | 0.02x, 0.02x, 1/30x | no write fee |
| Kimi (`kimi-k3`, `-k2.7-code`, `-k2.6`) | 0.1x, 0.2x, 0.168x | no write fee |

An OpenRouter id (`anthropic/claude-…`, `openai/gpt-…`) takes its maker's multiples only
for first-party routes — Anthropic and OpenAI serve their own models at their own rates.
An open-weight model on OpenRouter is resold by many hosts at their own cache prices, so
a routed `deepseek/…` or `moonshotai/…` id gets no fallback.

**Unknown is never priced as zero, or as full input.** `cost_usd` is `null` when the model
is absent from the table (or a local server like Ollama/vLLM), and also when a call used
the cache on a model with no cache rate — no column, no documented multiple. Either would
be a wrong number, and a wrong number is worse than a missing one; the token counts are
recorded either way.

Not modelled: DeepSeek's off-peak discount (it halves both rates, so the ratio holds; the
row's base rate decides), long-context and batch/flex/priority tiers, and the TTL of cache
writes OpenRouter reports (one total, priced as 5-minute).

## Proxy internals

- **One port per provider endpoint.** trap starts a separate proxy server per active registry
  entry, each bound to a random localhost port (port `0` → OS-assigned). Because a port serves
  exactly one endpoint, the proxy never has to detect the provider per request. No TLS
  interception is needed — the proxy just forwards over HTTPS and tees the response to read
  `usage`.
- **Streams are metered from their last word.** Usage in a stream is cumulative, so the proxy
  keeps the last usage it sees (Anthropic's `message_delta` restates `message_start` per field;
  the Responses API nests it in `response.completed`), never a sum.
- **Upstream URLs compensate for SDK path quirks.** SDKs differ in whether they keep the `/v1`
  path prefix when the base URL is overridden, so each provider's configured upstream must
  match:
  - Anthropic SDK keeps `/v1` → upstream `https://api.anthropic.com` (no suffix)
  - OpenAI SDK drops `/v1` → upstream `https://api.openai.com/v1`
  - Mistral SDK keeps `/v1` → upstream `https://api.mistral.ai` (no suffix)
  - Moonshot (Kimi), via the OpenAI SDK, drops `/v1` → upstream `https://api.moonshot.ai/v1`
    (a `.cn` account sets `MOONSHOT_API_BASE=https://api.moonshot.cn/v1`)
  - The DeepSeek harness appends `/chat/completions` with no `/v1` → upstream
    `https://api.deepseek.com`; its web search appends `/messages` → upstream
    `https://api.deepseek.com/anthropic/v1`
  - OpenRouter clients' bases carry `/api/v1` → upstream `https://openrouter.ai/api/v1`

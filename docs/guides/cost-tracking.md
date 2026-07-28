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
| Moonshot (Kimi) | `MOONSHOT_API_KEY` | `MOONSHOT_BASE_URL` and `MOONSHOT_API_BASE` |

**Claude Code** (`claude -p`) is always intercepted (OAuth, no key env var). With no key
set and no always-intercept provider, cost tracking is a no-op.

## Provider support

- **Anthropic, OpenAI, Claude Code** — work out of the box (the SDK auto-reads the base URL).
- **Mistral** — needs one line, as its SDK doesn't auto-read the env var:
  ```python
  client = Mistral(api_key=os.environ.get("MISTRAL_API_KEY"),
                   server_url=os.environ.get("MISTRAL_BASE_URL"))   # only set under trap
  ```
- **Moonshot (Kimi)** — a solution calling the OpenAI SDK directly against Moonshot reads
  `MOONSHOT_BASE_URL` and works out of the box. A solution going through **litellm**
  instead (e.g. any framework built on it, like Aider) needs no code change either — trap
  redirects `MOONSHOT_API_BASE` too, since that's the env var litellm's own Moonshot
  integration reads for this override.
- **AWS Bedrock / Google Vertex** — unsupported (SDK-level auth, no redirectable base URL); the run still works, cost is just absent.

## In report.json

Each case carries a `cost` object — per-model breakdown plus aggregate `prompt_tokens`,
`completion_tokens`, `cost_usd`, `calls` — or `null` if the solution made no LLM calls.
The terminal table shows per-case aggregates; per-model detail lives in `report.json`.

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

Models absent everywhere (or local servers like Ollama/vLLM) still get token
counts, but `cost_usd` is `null` — an unknown cost, deliberately distinct from `0.0`.

## Proxy internals

- **One port per provider.** trap starts a separate proxy server per active provider, each
  bound to a random localhost port (port `0` → OS-assigned). Because a port serves exactly one
  provider, the proxy never has to detect the provider per request. No TLS interception is
  needed — the proxy just forwards over HTTPS and tees the response to read `usage`.
- **Upstream URLs compensate for SDK path quirks.** SDKs differ in whether they keep the `/v1`
  path prefix when the base URL is overridden, so each provider's configured upstream must
  match:
  - Anthropic SDK keeps `/v1` → upstream `https://api.anthropic.com` (no suffix)
  - OpenAI SDK drops `/v1` → upstream `https://api.openai.com/v1`
  - Mistral SDK keeps `/v1` → upstream `https://api.mistral.ai` (no suffix)
  - Moonshot (Kimi), via the OpenAI SDK, drops `/v1` → upstream `https://api.moonshot.ai/v1`
    (a `.cn` account sets `MOONSHOT_BASE_URL=https://api.moonshot.cn/v1`)

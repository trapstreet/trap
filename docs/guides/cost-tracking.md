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

**Claude Code** (`claude -p`) is always intercepted (OAuth, no key env var). With no key
set and no always-intercept provider, cost tracking is a no-op.

## Provider support

- **Anthropic, OpenAI, Claude Code** — work out of the box (the SDK auto-reads the base URL).
- **Mistral** — needs one line, as its SDK doesn't auto-read the env var:
  ```python
  client = Mistral(api_key=os.environ.get("MISTRAL_API_KEY"),
                   server_url=os.environ.get("MISTRAL_BASE_URL"))   # only set under trap
  ```
- **AWS Bedrock / Google Vertex** — unsupported (SDK-level auth, no redirectable base URL); the run still works, cost is just absent.

## In report.json

Each case carries a `cost` object — per-model breakdown plus aggregate `prompt_tokens`,
`completion_tokens`, `cost_usd`, `calls` — or `null` if the solution made no LLM calls.
The terminal table shows per-case aggregates; per-model detail lives in `report.json`.

## Pricing

USD is computed from an explicit per-model price table maintained in
`src/trap/cost/calculator.py` (prefix-matched against the model id the API reports).
Models absent from the table (or local servers like Ollama/vLLM) still get token
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

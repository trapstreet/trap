# Built-in shapes

Test an agent product, a bare model, or someone else's program against a task without
writing a solution: point `trap.yaml`'s `cmd:` at one of tp's built-in solution
programs. A shape is a solution like any other — trap runs it once per case, keeps its
stdout as the answer, and judges, grades, meters and reports it the usual way.

```yaml
# trap.yaml — Claude Code, driven over ACP, on the Haiku model
cmd: >-
  tp shape acp --agent-id claude-acp --model haiku --scrub ../task
  --agent-cmd "npx -y @agentclientprotocol/claude-agent-acp@0.76.0"
timeout: 600
tasks:
  t:
    source: ../task
```

| Shape | Tests | Needs |
|---|---|---|
| `tp shape acp` | an agent that speaks the [Agent Client Protocol](https://agentclientprotocol.com) — Claude Code, Codex, Gemini CLI, Cursor, … | the agent installed and logged in (or runnable through `npx`) |
| `tp shape direct` | a model on its own: the question, once, no tools | the provider's API key |
| `tp shape cmd` | any program, through a one-line command template | the program |

## What every shape does

- **Reads the question** from the case's `question.txt` (`--prompt-file` to change it).
- **Works in a copy.** The case's input files are copied to a fresh temporary directory
  outside the task checkout and `.trap/`; the program under test runs there and the
  directory is removed afterwards. Task questions that say "the file is in the current
  directory" work as written.
- **Scrubs the environment before starting a child.** `cmd` and `acp` pass a scrubbed
  copy of the environment to the program or agent they start: it never sees
  `TRAP_MANIFEST` (it points at `inputs/`, and `expected/` sits next to it), your
  `manifest_envvar`, or any variable whose value names the case's `inputs/` directory.
  Add `--scrub <task checkout>` to drop variables that name the task root too. `direct`
  starts no child process — it calls the model API itself — so there is nothing to
  scrub; it still accepts `--scrub`, for uniformity, and ignores it. `PATH`, `HOME`,
  `TMPDIR`, `LANG`, `SHELL`, `TERM`, `USER` and `LOGNAME` are never dropped, whatever
  they contain — so that a broad `--scrub` (a repo root that also holds a venv, say)
  can't take `PATH` with it. `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` are
  the ACP/MCP SDKs' default inherited set; `TMPDIR` and `LANG` are added to it.
- **Stops itself.** `--deadline` (seconds, default 570) must sit below `trap.yaml`'s
  `timeout`: at the deadline the shape stops what it started — the child process (and
  everything it spawned), or the API call — and exits 124. The runner alone would kill
  only the shape and leave an agent running.
- **Exits with a code that says how the case ended:**

  | Exit | Meaning |
  |---|---|
  | 0 | an answer |
  | 20 | the model or agent refused (the refusal text is still the answer) |
  | 21 | the output ceiling was hit (a partial answer, if any) |
  | 22 | the agent hit its turn limit |
  | 23 | agent error: crash, protocol error, failed login, or a reply that is not an answer — stdout stays empty |
  | 24 | configuration: bad arguments, a model or option the agent does not offer, input the shape cannot pass on, a missing program |
  | 124 | the deadline (a partial answer, if any) |

  Like any solution's exit code these are facts about the case; they never fail `tp run`.
  `tp shape cmd` only ever produces 24 (its own configuration errors — an unset or
  unreadable manifest, an unparseable or empty template, `{repo}` with no `--repo`, a
  command that can't be found) or 124 (the deadline) itself — any other code is whatever
  the wrapped program exited with.

## `tp shape acp`

| Flag | Meaning |
|---|---|
| `--agent-cmd` | how to start the agent, e.g. `npx -y @agentclientprotocol/codex-acp@1.11.0` |
| `--agent-id` | its [ACP registry](https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json) id — `claude-acp`, `codex-acp`, `gemini`, … — which turns on what tp knows about that agent |
| `--model` | required (unless `--describe`); **a value the agent itself lists**, not an API model id |
| `--option ID=VALUE` | set another of the agent's options, e.g. `effort=low` |
| `--skill DIR` | install a skill for the case (Claude Code only) |
| `--describe` | print the agent's options and their values, then exit — with the same scrubbed environment a case would run under |

Find the `--model` values with `--describe`:

```bash
tp shape acp --agent-id claude-acp --describe --agent-cmd "npx -y @agentclientprotocol/claude-agent-acp@0.76.0"
```

Claude Code lists aliases (`default`, `sonnet`, `haiku`, `opus[1m]`, …); Codex lists
model ids (`gpt-5.5`, `gpt-5.6-luna`, …). Which model an alias means is decided by the
agent's version — pin the version in `--agent-cmd`. If `--agent-cmd` cannot be started
at all — the program is not found, or isn't executable — the shape exits 24, the same
as any other configuration error.

Per case the bridge opens one session in the work directory, sets the model and options,
sends the question once, and prints the agent's **answer**. The answer is the agent's
last message: a new message starts whenever the agent's `messageId` changes, so the
text before "let me read the file first" is not glued onto what follows it. An agent
that never sends a `messageId` gets the same treatment a different way — a tool call
closes off whatever message came before it, so the answer is the text *after* the last
tool call; a turn that ends right on a tool call, with nothing said afterward, answers
with `""`. Opening the session and setting the model/options share one time budget with
`--deadline`, not a fresh allowance each — a case that spent most of its deadline on a
slow handshake has that much less left for the question. The config the case ran with,
each permission granted, and the agent's self-reported usage go to the case's stderr.

- **Permissions** are granted one call at a time (`allow_once`, falling back to
  `reject_once` then to cancelling the request), never "always". This is unattended
  execution of whatever the agent decides to run; the work directory is not a sandbox.
- **Claude Code** runs with only project settings (`settingSources: ["project"]`): your own
  `CLAUDE.md`, skills, hooks and plugins stay out of the run, and cannot redirect its API
  calls around the cost proxy.
- **Answers that are not answers.** A turn that failed (a login error, say) or in which the
  agent reports that no model answered exits 23 with empty stdout, even when the agent
  sent the error text as a message.

**Cost.** The cost proxy measures what the agent sends through the provider's base URL:

| Agent | Measured? |
|---|---|
| Claude Code with `ANTHROPIC_API_KEY` | yes |
| Claude Code with a subscription login | not yet verified |
| Codex with an API key | the bridge points it at the proxy (`CODEX_CONFIG`); not yet verified |
| Codex with a ChatGPT login | no — its calls go to ChatGPT's backend |
| Cursor, GitHub Copilot | no — their calls leave from their own servers |
| Gemini CLI, Qwen Code | no — the proxy does not cover Google or Alibaba |

An unmeasured run reports its cost as unknown, never zero.

## `tp shape direct`

```yaml
cmd: tp shape direct --model claude-sonnet-5 --system-file SKILL.md
```

| Flag | Meaning |
|---|---|
| `--model` | the API's model id, e.g. `claude-sonnet-5`, `gpt-5.5`, `deepseek-v4-pro`, `anthropic/claude-sonnet-5` |
| `--provider` | `anthropic`, `openai`, `openrouter`, `deepseek`, `moonshot` or `mistral`; by default read off the model id (`vendor/model` is OpenRouter) |
| `--system-file` | a file sent as the system prompt — a skill's `SKILL.md`, for instance |

The question is the only user message, sent once without tools, and the reply's visible
text is the answer. The key comes from the provider's usual variable (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, …) and the call goes through the cost proxy like any solution's.

Generation settings are fixed and written to stderr: Anthropic gets `max_tokens` 16000
(Claude 5 models think by default, and the thinking shares that ceiling); OpenAI-compatible
APIs get no cap; thinking and reasoning effort stay at each API's default. These differ
from hand-tuned baseline solutions, so compare scores across the same shape.

An OpenAI-compatible reply that carries a `refusal` exits 20, the same as an Anthropic
refusal — the refusal text is still printed as the answer. A reasoning model that returns
its content as a list of parts (Mistral's `magistral-`, for instance) is read as the
joined text of its text parts, not rejected. A reply that isn't JSON, isn't a JSON
object, or otherwise doesn't match what a provider's API is expected to send back exits
23, as does an HTTP error status from the provider.

A case with input files besides the question is refused (exit 24) — a model called
directly cannot see them. Use an agent for those tasks. `--system-file` is read as
UTF-8; a path that can't be read, or whose contents aren't valid UTF-8, is also a
configuration error (exit 24).

## `tp shape cmd`

```yaml
cmd: tp shape cmd --repo . --template "python {repo}/main.py --question {prompt}"
```

| Placeholder | Becomes |
|---|---|
| `{prompt}` | the question's text, as a single argument |
| `{prompt_file}` | the path of the question file in the work directory |
| `{repo}` | `--repo`, resolved to an absolute path |

With neither `{prompt}` nor `{prompt_file}` the question goes to the program's stdin. The
template is split like a shell command but never run by a shell — no pipes or `&&`; for
those, wrap the command in `sh -c '…'` and use `{prompt_file}`. The program runs in the
work directory, so a relative path in the template resolves there: refer to your code
through `{repo}`. Whatever it prints on stdout is the answer, and its exit code is the
case's.

## Limits

- Answers are stdout. A task whose answer is a folder of files is out of reach for now.
- `--skill` installs skills for Claude Code only.
- Coming next: `tp run --agent … / --model … / --cmd …` will build these for you, without a
  `trap.yaml`.

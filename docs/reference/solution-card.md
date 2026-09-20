# Solution card

The card is the set of labels that decide *what a run measured*: which shape drove the
solution, which agent build, which model was asked for, which options actually took
effect, which skill (if any) was installed, and under what deadline. Two runs of the
same repository at the same commit are the same *solution* only when their cards match
— that is what lets the site put "this skill on sonnet" and "this skill on haiku" in two
leaderboard rows instead of collapsing them into one row that hides half the story.

The card's **digest** is a content address computed from its own fields, over
`sha256`. It is not a hash of the repository or the commit — it names the way the
solution was driven, so the same commit can carry several digests (one per model or
skill it was run with) and a different commit run the same way keeps the same digest.

This page is implementation-independent: a server implementing the digest in another
language needs only what is written here, and can check itself against
[`tests/data/solution_card_vectors.json`](https://github.com/trapstreet/trap/blob/main/tests/data/solution_card_vectors.json)
in this repository — the CLI and the site both run that file's vectors, so the two can
never quietly drift apart. The Python model lives at `src/trap/models/card.py`.

## Fields

Every field except `name` is **digest-bearing** — it participates in the hash. `name`
is display-only: it is never hashed, so renaming a card's leaderboard title does not
change its identity.

| Field | In digest? | Meaning |
|---|---|---|
| `shape` | yes | Which of the built-in shapes drove the solution: `"acp"`, `"model"`, or `"cmd"`. |
| `shape_version` | yes | Behaviour version of that shape. The answer rule, generation defaults, and what gets copied all move the score, so a change to any of them is a new card, not a quiet re-scoring of an old one. |
| `agent` | yes | ACP only: the agent's full `package@version`, as resolved when it started. |
| `model` | yes | The model that was **asked for**. What a harness actually used underneath (a coding agent may reach for a smaller model on its own) is reported per model in the run's cost, not here — see below. |
| `provider` | yes | Model-direct only: which vendor API the request went to. |
| `options` | yes | Agent options that **actually took effect**, as `id -> value`. An option the chosen model does not offer is skipped when the case runs, so it never lands here — see below. |
| `skill` | yes | The skill that was installed, as `repo@sha` when the CLI could resolve one. |
| `cmd` | yes | The command template (`cmd` shape). |
| `setup` | yes | The one-off install line a program needed before it could run. |
| `timeout` | yes | The per-case deadline the shape ran under, in seconds. |
| `name` | **no** | Display only — becomes `solutions.title` on the site. Two cards that differ only in `name` have the same digest. |

Two fields carry a rule that is easy to get backwards, so it is stated here rather than
only in a comment:

- **`options` holds the values that took effect**, not the values requested. If a
  requested option is not offered by the chosen model, it is dropped before the card is
  built, and never appears in `options`.
- **`model` holds the value that was requested**, not necessarily the model that ended
  up doing the work. A harness is free to substitute underneath (e.g. reach for a
  cheaper model for a sub-step); that substitution is a cost-tracking concern, reported
  per model in the run's cost data, and does not change the card or its digest.

## Canonicalisation and the digest

The digest is computed in two steps, and both must be reproduced exactly for the
digest to match:

1. **Build the payload.** Take every digest-bearing field (every field in the table
   above except `name`). Drop any field that is unset (`null`/`None`), or set to an
   empty value (`""` for a string, `{}` for `options`). A field that is unset and a
   field that is explicitly set to nothing are the same card — this is what lets a
   future optional field be added without changing the digest of cards that never use
   it. What remains is a flat JSON object of `{field: value}`.

2. **Serialise it canonically**, to bytes:
   - object keys **sorted** lexicographically;
   - **no inserted whitespace** — item separator `,`, key/value separator `:` (i.e.
     JSON's `separators=(",", ":")`);
   - text encoded as **UTF-8**, and non-ASCII characters left as themselves, not
     escaped as `\uXXXX` (JSON's `ensure_ascii=False`);
   - numbers and booleans in their ordinary JSON form (`shape_version` and `timeout`
     are numbers, not strings).

   Call the result the **canonical JSON** of the card. It is a byte string, not a
   Python `str` — the UTF-8 encoding is part of what gets hashed.

3. **Hash it.** The digest is `sha256(canonical_json)`, rendered as **64 lowercase hex
   characters**.

Any implementation that produces the same canonical JSON bytes for the same card
produces the same digest, because `sha256` is deterministic. Conversely, the canonical
JSON is the thing to compare when two implementations disagree — check step 2 before
suspecting the hash function.

## Worked example

Card (JSON, as it would appear in a submission or in `SolutionCard.model_validate`):

```json
{"shape": "acp", "shape_version": 1, "agent": "@agentclientprotocol/claude-agent-acp@0.76.0", "model": "sonnet"}
```

Canonical JSON (keys sorted, no whitespace, UTF-8):

```
{"agent":"@agentclientprotocol/claude-agent-acp@0.76.0","model":"sonnet","shape":"acp","shape_version":1}
```

Digest (`sha256` of the bytes above, lowercase hex):

```
f7d3d713161b9956437730bcb3dab2b9972d57d5269d4d5c8d90e5764b44e0cc
```

This is the first vector (`acp-minimal`) in
`tests/data/solution_card_vectors.json`; the other three vectors there cover options,
a skill, `model`-shape cards, and a `cmd`-shape card with non-ASCII text in its command
template. Every vector's `canonical_json` and `digest` were generated from the Python
model, never hand-written — an implementation that disagrees with a vector has a bug,
not the other way around.

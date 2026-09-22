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
| `skill` | yes | The skill that was installed, as `repo@sha` when the CLI could resolve one — see below. |
| `cmd` | yes | The command template (`cmd` shape). |
| `setup` | yes | The one-off install line a program needed before it could run. |
| `timeout` | yes | The per-case deadline the shape ran under, in **whole seconds** (an integer, never a fraction — see below). |
| `name` | **no** | Display only — becomes `solutions.title` on the site. Two cards that differ only in `name` have the same digest. |

Three fields carry a rule that is easy to get backwards, so it is stated here rather than
only in a comment:

- **`options` holds the values that took effect**, not the values requested. If a
  requested option is not offered by the chosen model, it is dropped before the card is
  built, and never appears in `options`.
- **`model` holds the value that was requested**, not necessarily the model that ended
  up doing the work. A harness is free to substitute underneath (e.g. reach for a
  cheaper model for a sub-step); that substitution is a cost-tracking concern, reported
  per model in the run's cost data, and does not change the card or its digest.
- **`skill` is named on the site by reconstructing it, never by forwarding it.** This
  is a display/labelling rule, not a digest rule — the raw `skill` string is what gets
  hashed either way (see below) — but it governs what a client is allowed to *show*:
  no value shown for a skill is ever a copy of (part of) the `skill` string itself;
  every value shown is rebuilt from named components parsed out of it, glued into a
  fixed template, and nothing else about `skill` is ever read. Concretely: leading and
  trailing whitespace is **stripped from the whole value first** (so an otherwise valid
  reference that picked up a stray space still resolves, rather than falling back to a
  leaf name with the commit stuck to it), and the result is then parsed at its **last**
  `@` (so a repo URL that itself contains one, e.g. `git@host:owner/repo`, is not split
  in the wrong place) into a candidate repo and a candidate commit.

  It is a **publishable reference** exactly when: the commit half is **lowercase hex,
  7 to 64 characters**; the repo half is an **http(s) URL** — its scheme literally
  `http` or `https`, nothing else, kept in the output *exactly as given* (an `http`
  remote is published as `http://`, never upgraded, which would name an endpoint the
  remote may not answer) — with a host and a port (when present) both read and kept
  as given the same way, and exactly two path segments (owner, repo, an optional
  `.git` suffix on the repo segment stripped); and none of the host, owner or repo
  contains a control character. Anything a parser raises on rather than cleanly
  rejects — a malformed port, a bracketed host that is not a valid IPv6 literal (a
  bracketed IPv4 address, say, or one with a stray character in it) — must be treated
  as "does not parse" too, the same as a value the parser hands back cleanly: a JS
  `URL` getter throws for exactly these inputs, the same way Python's URL parser does,
  so an implementation must catch that rather than let it propagate. Two further
  segment values are refused outright even though they parse cleanly: `.` and `..`,
  since no forge allows either and any URL-normalising re-parse (including the site's
  own) collapses them, so the two-segment structure published would not survive one.

  A publishable reference is shown as `owner/repo`, with the commit's first 7
  characters alongside it, and its **`repo` URL is rebuilt as
  `<scheme>://<host>[:<port>]/<owner>/<repo>`, an IPv6 host re-bracketed** (a bare
  IPv6 address is not a valid URL host; `new URL(...).hostname` in JavaScript strips
  the brackets exactly as Python's URL parser does, so they must be added back by
  hand). A credential embedded in the input is absent from that URL for a categorically
  different reason than the scheme, host and port are present in it: userinfo is a
  component the parse never reads at all, so there is nothing to strip, while the
  scheme, host and port are components the parse does read and does publish, exactly
  as given, because the rebuilt URL is meant to be the endpoint the input actually
  named — conflating "never read" with "read and kept" is exactly what silently drops
  a port or upgrades a scheme. A `git://`/`ssh://` remote (or a scp-style one, already
  normalised to an http(s) form before it ever reaches a card) has its scheme read
  correctly too, but that scheme is not one of the two ever republished, so it is
  treated the same as anything else that does not parse — not "present but
  downgraded", simply never a candidate for reconstruction.

  **Anything that is not a publishable reference is not filtered or rejected by
  shape — it is a different case with a different display rule.** It is shown by its
  own last path segment alone (splitting on both `/` and `\`, since nothing about the
  field's type rules out a Windows-style value even though nothing here produces one),
  with no repo or commit shown at all — control characters stripped from the result,
  and a result left empty by that stripping shown as the literal `skill` (a card that
  installed a skill must not report installing none). This is the same case a Windows
  drive letter or a UNC share falls into, and for the same reason a control character
  in the owner or repo of an otherwise well-formed URL does too: each is a value the
  parse either never had a scheme/host/port/owner/repo to extract from, or refuses to
  publish once extracted — never "present but withheld", simply not a reference.

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
   - **every number in a card is an integer** (`shape_version`, `timeout`), so its
     canonical JSON never contains a decimal point. This is stated as its own rule,
     not folded into "numbers in their ordinary JSON form", because "ordinary" differs
     by language: JavaScript has a single numeric type, so `JSON.stringify({timeout:
     570})` gives `570`, but Python's `json.dumps` renders a whole-number `float` as
     `570.0` — same value, different bytes, different digest. Fixing every numeric
     field to an integer removes the ambiguity instead of asking every implementation
     to special-case it: there is nothing a canonicaliser can do with a decimal point
     it never has to produce.

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

A second example, to make the integer rule concrete — the `model-direct` vector, whose
card carries a `timeout`:

```json
{"shape": "model", "shape_version": 1, "provider": "anthropic", "model": "claude-sonnet-5", "timeout": 570.0}
```

`570.0` here is only how this *input* JSON happened to spell the number — a submission
is free to send a float, so long as it is integral. The canonical JSON always renders
it as the integer `570`, with no decimal point:

```
{"model":"claude-sonnet-5","provider":"anthropic","shape":"model","shape_version":1,"timeout":570}
```

```
46c1bb1251e63906313c1dfaad1fcbdd99dcbb994a5a77cfbeb08ee4ed7056d0
```

An implementation that instead reproduces the input's `570.0` verbatim (as a
JavaScript-side canonicaliser might, if it forgot this rule) would compute a different
digest and fail this vector.

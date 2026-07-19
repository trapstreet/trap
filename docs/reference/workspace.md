# Workspace (.trap/)

Every `tp run` writes artifacts under `.trap/runs/{solution-key}/{alias}/{timestamp}/`.
`{alias}` is the `tasks:` key from trap.yaml; `{solution-key}` identifies the solution the
run was launched for, so runs of different solutions sharing one workspace never mix —
`latest` is always scoped to a single (solution, task) pair.

## Directory layout

The workspace root holds one directory per data type: `runs/` for run artifacts and
`repos/` for the task clone cache. Inside a run, each actor (solution, judge, grader)
owns its own subdirectory; its captures live there unprefixed, which keeps the
solution's `outputs/` clean.

```
.trap/
├── repos/                                    # task clone cache (remote task sources)
└── runs/
    └── {solution-key}/                       # e.g. claude-haiku-4-5-3f2a1b0c
        └── {alias}/                          # the tasks: key from trap.yaml
            └── 2026-05-09T14:30:00/          # one run, named by its start time
                ├── {case_id}/
                │   ├── solution/
                │   │   ├── stdout            # solution stdout (always captured)
                │   │   ├── stderr            # solution stderr
                │   │   ├── meta.json         # {"exit_code": 0, "duration": 0.12}
                │   │   └── outputs/          # files the solution wrote (it owns this dir)
                │   └── judge/                # only present if a judge ran
                │       ├── stdout            # the judge's metrics JSON
                │       ├── stderr
                │       └── meta.json         # {"exit_code": 0, "duration": 0.05}
                ├── grader/                   # only present if a grader ran
                │   ├── stdout                # the grader's metrics JSON
                │   ├── stderr
                │   └── meta.json
                └── report.json               # full serialised run report
```

## The solution key

`{solution-key}` is `<basename>-<hash8>`: the solution directory's basename plus a short
hash of its full identity — the resolved absolute path for a local solution, or the
normalised URL + subdirectory for a `git+` one. Aliases of the same solution (`./x`,
`x`, an absolute path) map to the same key; same-named solutions at different paths get
different keys. You never type the key: `tp report` / `tp submit` derive it from the
same `SOLUTION` argument you ran with.

## `latest` is derived

The CLI resolves `latest` at every use by picking the newest timestamp-named run
directory that contains a `report.json` — a crashed, half-written run can never be
`latest`. Nothing on disk records which run is latest, so there is no pointer to go
stale.

## Key files

**`{case_id}/solution/meta.json`** — written by the runner after each case:
```json
{"exit_code": 0, "duration": 0.12}
```

**`{case_id}/solution/outputs/`** — holds **only** files the solution wrote; trap never writes here, so a judge can list it to see exactly what the solution produced.

**`{case_id}/judge/stdout`** and **`grader/stdout`** — the JSON each actor emits, stored in the report as the case's `metrics` and the run's `grader_metrics`.

**`report.json`** — the full run report in JSON format. Use `tp report --output json` to print it to stdout instead of reading the file directly.

## Re-displaying a run

Use `tp report` to re-render any stored run without re-executing the solution:

```bash
tp report                                       # latest run
tp report --task test --run 2026-05-09T14:30:00 # specific run by timestamp
```

`tp run` prints the run id and report path when it finishes; the id doubles as the
`--run` argument for `tp report` and `tp submit`.

## Ignoring the workspace

Add `.trap/` to your `.gitignore`:

```
.trap/
```

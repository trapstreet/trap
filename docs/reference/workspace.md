# Workspace (.trap/)

Every `tp run` writes artifacts under `.trap/{alias}/{timestamp}/` and updates a `latest` symlink alongside it. `{alias}` is the `tasks:` key from trap.yaml.

## Directory layout

Each actor (solution, judge, grader) owns its own subdirectory; its captures live
there unprefixed, which keeps the solution's `outputs/` clean.

```
.trap/
└── {alias}/                              # the tasks: key from trap.yaml
    ├── latest -> 2026-05-09T14:30:00/    # symlink to most recent run
    └── 2026-05-09T14:30:00/              # one run, named by its start time
        ├── {case_id}/
        │   ├── solution/
        │   │   ├── stdout                # solution stdout (always captured)
        │   │   ├── stderr                # solution stderr
        │   │   ├── meta.json             # {"exit_code": 0, "duration": 0.12}
        │   │   └── outputs/              # files the solution wrote (it owns this dir)
        │   └── judge/                    # only present if a judge ran
        │       ├── stdout                # the judge's metrics JSON
        │       ├── stderr
        │       └── meta.json             # {"exit_code": 0, "duration": 0.05}
        ├── grader/                       # only present if a grader ran
        │   ├── stdout                    # the grader's metrics JSON
        │   ├── stderr
        │   └── meta.json
        └── report.json                  # full serialised run report
```

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
tp report                              # latest run
tp report test 2026-05-09T14:30:00    # specific run by timestamp
```

## Ignoring the workspace

Add `.trap/` to your `.gitignore`:

```
.trap/
```

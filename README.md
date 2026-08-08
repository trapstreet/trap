# trap

> ## [trapstreet.run](https://trapstreet.run)
>
> **Find the AI solution that actually works.** Agents, skills, and tools compared
> side by side on the same task — non-invasive I/O testing, reproducible results,
> public leaderboards.
>
> **New here?** Start at
> **[trapstreet-skills](https://github.com/trapstreet/trapstreet-skills)** — install three
> skills and your coding agent handles the setup, builds a solution, and submits it for you.
>
> Prefer to drive it yourself:
>
> ```bash
> uv tool install trap-cli && tp auth login
> ```
>
> [**Quick start**](https://trapstreet.run/docs/quick-start) ·
> [Build a solution](https://trapstreet.run/docs/build-a-solution) ·
> [Build a task](https://trapstreet.run/docs/build-a-task) ·
> [Browse tasks](https://trapstreet.run) ·
> [Reference](https://trapstreet.run/docs/reference)

[![CI](https://github.com/trapstreet/trap/actions/workflows/ci.yml/badge.svg)](https://github.com/trapstreet/trap/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/trap-cli)](https://pypi.org/project/trap-cli/)
[![Python](https://img.shields.io/pypi/pyversions/trap-cli)](https://pypi.org/project/trap-cli/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**Non-invasive CLI testing framework for AI prompts, agents, and workflows.**

trap treats any solution as a black box — it invokes it as a subprocess, captures outputs, then optionally scores them through a language-agnostic judge and grader. The solution doesn't need to import trap or know it exists.

## Install

```bash
# requires uv — https://docs.astral.sh/uv/getting-started/installation/

# from PyPI
uv tool install trap-cli

# from git (latest main)
uv tool install "git+https://github.com/trapstreet/trap.git"
```

The command is `tp`.

## How it works

trap runs your solution as a subprocess with one env var (`TRAP_MANIFEST` — an input dir and
an output dir), captures what it writes, then optionally scores it through a judge (per case)
and a grader (overall). See the [full flow and IO contract](docs/index.md).

Two roles, two directories, one IO contract:

- **Solution author** — writes `trap.yaml` and the solution code
- **Task author** — writes `traptask.yaml`, `inputs/`, `expected/`, and optional judge/grader scripts

## Quick start

```bash
# from examples/echo/solution/
tp run           # run all cases
tp run -t smoke  # run only cases tagged `smoke`
```

## Documentation

- [Quick start](docs/quickstart.md)
- [Writing a solution](docs/guides/writing-solution.md)
- [Writing a task](docs/guides/writing-task.md)
- [CLI reference](docs/reference/cli.md)
- [IO contract](docs/reference/io-contract.md)

## License

MIT

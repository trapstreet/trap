# trap

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

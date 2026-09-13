"""Built-in solution programs — the shapes tp can run without a solution of your own.

Each shape is an ordinary solution under trap's IO contract: the runner starts it as the
solution subprocess, it reads ``TRAP_MANIFEST``, prints the answer on stdout and exits.
Nothing downstream of the runner can tell a shape from a hand-written solution — the
judge, site grading, the cost proxy and the report all see one solver process.

  - ``acp``      drives an agent that speaks the Agent Client Protocol and offers a
                 model option (Claude Code and Codex are verified) for one prompt per
                 case.
  - ``direct``   sends the case's question to a model API once — no harness.
  - ``command``  runs a one-line command template around someone else's program.

Run one from trap.yaml as ``cmd: tp shape <acp|direct|cmd> ...``; see
docs/guides/built-in-shapes.md.
"""

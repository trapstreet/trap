"""``python -m trap.shapes.acp``: the entry point a solution's ``cmd:`` line runs."""

from __future__ import annotations

from trap.shapes.acp.bridge import main

if __name__ == "__main__":
    raise SystemExit(main())

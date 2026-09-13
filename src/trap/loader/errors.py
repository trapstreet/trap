from __future__ import annotations

# ConfigError lives in the leaf trap.errors (the runner raises it too); re-exported here
# so `from trap.loader.errors import ConfigError` keeps working.
from trap.errors import ConfigError

__all__ = ["ConfigError"]

try:
    from trap._version import __version__
except ImportError:  # pragma: no cover - only when built without hatch-vcs
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]

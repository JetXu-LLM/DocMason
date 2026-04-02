"""Top-level package for DocMason."""

from __future__ import annotations

from importlib import metadata

from .release_version import read_project_version

__all__ = ["__version__"]


def _package_version() -> str:
    """Return the committed source version when available, else the installed version."""
    try:
        return read_project_version()
    except (OSError, ValueError):
        return metadata.version("docmason")


__version__ = _package_version()

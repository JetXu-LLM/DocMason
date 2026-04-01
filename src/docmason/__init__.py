"""Top-level package for DocMason."""

from __future__ import annotations

from importlib import metadata

from .release_version import read_project_version

__all__ = ["__version__"]


def _package_version() -> str:
    """Return the installed package version or the committed source version."""
    try:
        return metadata.version("docmason")
    except metadata.PackageNotFoundError:
        return read_project_version()


__version__ = _package_version()

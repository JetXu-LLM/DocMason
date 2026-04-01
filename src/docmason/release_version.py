"""Helpers for the committed DocMason release-version contract."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?$")
RELEASE_TAG_PATTERN = re.compile(r"^v(?P<version>.+)$")
PROJECT_HEADER = "[project]"


def default_pyproject_path(*, repo_root: Path | None = None) -> Path:
    """Return the canonical `pyproject.toml` path for the repository."""
    if repo_root is not None:
        return repo_root / "pyproject.toml"
    return Path(__file__).resolve().parents[2] / "pyproject.toml"


def read_project_version(pyproject_path: Path | None = None) -> str:
    """Read the committed project version from `pyproject.toml`."""
    path = pyproject_path or default_pyproject_path()
    payload = tomllib.loads(path.read_text(encoding="utf-8"))
    version = payload.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ValueError("`pyproject.toml` does not declare `project.version`.")
    validate_project_version(version)
    return version


def validate_project_version(version: str) -> str:
    """Validate a DocMason release version string."""
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(
            "DocMason release versions must use a committed PEP 440 style such as "
            "`0.1.0`, `0.1.0rc2`, or `0.1.0a1`."
        )
    return version


def release_tag_for_version(version: str) -> str:
    """Return the Git tag expected for a committed package version."""
    validate_project_version(version)
    tag_version = re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1-\2\3", version)
    return f"v{tag_version}"


def version_for_release_tag(tag: str) -> str:
    """Normalize a Git release tag back to the committed package version."""
    match = RELEASE_TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ValueError("Release tags must start with `v`.")
    version = re.sub(r"(\d)-(a|b|rc)(\d+)$", r"\1\2\3", match.group("version"))
    return validate_project_version(version)


def validate_release_tag(tag: str, *, expected_version: str | None = None) -> str:
    """Validate that a release tag matches the committed package version."""
    normalized_version = version_for_release_tag(tag)
    target_version = expected_version or read_project_version()
    if normalized_version != target_version:
        expected_tag = release_tag_for_version(target_version)
        raise ValueError(
            f"Release tag `{tag}` does not match committed project version `{target_version}`. "
            f"Use `{expected_tag}` or update `pyproject.toml` first."
        )
    return normalized_version


def write_project_version(version: str, *, pyproject_path: Path | None = None) -> str:
    """Update `project.version` inside `pyproject.toml` and return the new version."""
    normalized_version = validate_project_version(version)
    path = pyproject_path or default_pyproject_path()
    lines = path.read_text(encoding="utf-8").splitlines()
    updated_lines: list[str] = []
    in_project_block = False
    replaced = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project_block = stripped == PROJECT_HEADER
        if in_project_block and stripped.startswith("version = "):
            updated_lines.append(f'version = "{normalized_version}"')
            replaced = True
            continue
        updated_lines.append(line)

    if not replaced:
        raise ValueError("Could not find `project.version` inside `pyproject.toml`.")

    path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return normalized_version

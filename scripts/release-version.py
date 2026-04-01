#!/usr/bin/env python3
"""Manage the committed DocMason release-version contract."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from docmason.release_version import (  # noqa: E402
    read_project_version,
    release_tag_for_version,
    validate_release_tag,
    write_project_version,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser for release-version management."""
    parser = argparse.ArgumentParser(
        description="Read, update, and validate the committed DocMason release version."
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="Repository root. Defaults to the current checkout.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("print-version", help="Print the committed package version.")
    subparsers.add_parser("print-tag", help="Print the Git tag expected for the current version.")

    set_parser = subparsers.add_parser(
        "set-version",
        help="Update `pyproject.toml` to the committed release version.",
    )
    set_parser.add_argument("version", help="PEP 440 package version, for example `0.1.0`.")

    validate_parser = subparsers.add_parser(
        "validate-tag",
        help="Validate that a release tag matches the committed package version.",
    )
    validate_parser.add_argument("tag", help="Git tag to validate, for example `v0.1.0`.")
    return parser


def main() -> int:
    """Run the requested release-version command."""
    parser = build_parser()
    args = parser.parse_args()
    pyproject_path = args.repo_root.resolve() / "pyproject.toml"

    if args.command == "print-version":
        print(read_project_version(pyproject_path))
        return 0
    if args.command == "print-tag":
        print(release_tag_for_version(read_project_version(pyproject_path)))
        return 0
    if args.command == "set-version":
        version = write_project_version(args.version, pyproject_path=pyproject_path)
        print(version)
        return 0
    if args.command == "validate-tag":
        validate_release_tag(args.tag, expected_version=read_project_version(pyproject_path))
        print(args.tag)
        return 0
    parser.error("Unknown command.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

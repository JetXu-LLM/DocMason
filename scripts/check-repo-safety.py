#!/usr/bin/env python3
"""Validate that private local directories stay out of tracked Git content."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROTECTED_TOP_LEVEL = {
    ".docmason",
    "adapters",
    "knowledge_base",
    "original_doc",
    "planning",
    "runtime",
}
ALLOWED_TRACKED_FILES = {
    "original_doc/.gitkeep",
    "knowledge_base/.gitkeep",
    "runtime/.gitkeep",
    "adapters/.gitkeep",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check DocMason repository safety boundaries.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root. Defaults to the current checkout.",
    )
    parser.add_argument(
        "--staged-only",
        action="store_true",
        help="Inspect only staged Git paths instead of the full tracked tree.",
    )
    return parser.parse_args()


def git_paths(repo_root: Path, command: list[str]) -> list[str]:
    result = subprocess.run(
        command,
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def inside_git_checkout(repo_root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is required to inspect tracked repository paths.") from exc
    except subprocess.CalledProcessError:
        return False
    return result.stdout.strip() == "true"


def tracked_or_staged_paths(repo_root: Path, staged_only: bool) -> list[str]:
    if staged_only:
        return git_paths(repo_root, ["git", "diff", "--cached", "--name-only", "-z"])
    return git_paths(repo_root, ["git", "ls-files", "-z"])


def validate_paths(paths: list[str]) -> list[str]:
    violations: list[str] = []
    for raw_path in paths:
        if raw_path in ALLOWED_TRACKED_FILES:
            continue
        top_level = Path(raw_path).parts[0] if Path(raw_path).parts else ""
        if top_level in PROTECTED_TOP_LEVEL:
            violations.append(raw_path)
    return violations


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()

    try:
        git_checkout = inside_git_checkout(repo_root)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not git_checkout:
        print("DocMason safety check skipped: repository root is not a Git checkout.")
        return 0

    violations = validate_paths(tracked_or_staged_paths(repo_root, args.staged_only))
    if violations:
        mode = "staged" if args.staged_only else "tracked"
        print(
            f"DocMason safety check failed: found {mode} paths beneath protected private "
            "directories:",
            file=sys.stderr,
        )
        for path in violations:
            print(f"- {path}", file=sys.stderr)
        return 1
    print("DocMason safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

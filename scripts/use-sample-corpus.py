#!/usr/bin/env python3
"""Materialize a tracked public sample corpus into the local workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PRESET_COPY_DIRS = {
    "ico-gcs": ("ico", "gcs"),
}


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def visible_files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(root).parts)
    )


def clear_visible_workspace(root: Path) -> None:
    for child in sorted(root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def copy_preset(sample_root: Path, target_root: Path, preset: str) -> list[str]:
    copied: list[str] = []
    for name in PRESET_COPY_DIRS[preset]:
        source = sample_root / name
        destination = target_root / name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(source, destination)
        copied.append(name)
    marker = target_root / ".docmason-sample.json"
    marker.write_text(
        json.dumps(
            {
                "preset": preset,
                "materialized_at": utc_now(),
                "source_root": str(sample_root),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return copied


def run_command(command: list[str], cwd: Path) -> None:
    result = subprocess.run(command, cwd=cwd, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Copy a tracked public sample corpus into original_doc/."
    )
    parser.add_argument(
        "--preset",
        default="ico-gcs",
        choices=sorted(PRESET_COPY_DIRS),
        help="Sample corpus preset to materialize.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="DocMason repository root. Defaults to the current checkout.",
    )
    parser.add_argument(
        "--target",
        type=Path,
        default=None,
        help="Destination directory. Defaults to <repo-root>/original_doc.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace visible files already present beneath the target directory.",
    )
    parser.add_argument(
        "--prepare",
        action="store_true",
        help="Run bootstrap-workspace.sh --yes after copying the sample corpus.",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Run bootstrap-workspace.sh --yes and docmason sync after copying the sample corpus.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    target_root = (args.target or (repo_root / "original_doc")).resolve()
    sample_root = repo_root / "sample_corpus" / args.preset
    if not sample_root.exists():
        raise SystemExit(f"Sample preset not found: {sample_root}")

    target_root.mkdir(parents=True, exist_ok=True)
    existing = visible_files(target_root)
    if existing and not args.force:
        print(
            "Refusing to overwrite visible files already present under "
            f"{target_root}. Rerun with --force if replacement is intentional.",
            file=sys.stderr,
        )
        return 2
    if existing and args.force:
        clear_visible_workspace(target_root)

    copied = copy_preset(sample_root, target_root, args.preset)
    print(
        "Materialized sample preset "
        f"`{args.preset}` into {target_root} with top-level directories: {', '.join(copied)}."
    )

    if args.prepare or args.sync:
        run_command([str(repo_root / "scripts" / "bootstrap-workspace.sh"), "--yes"], repo_root)
    if args.sync:
        run_command(
            [str(repo_root / ".venv" / "bin" / "python"), "-m", "docmason", "sync"],
            repo_root,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

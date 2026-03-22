#!/usr/bin/env python3
"""Overlay a newer clean or demo bundle onto an existing workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

PROTECTED_TOP_LEVEL = {
    ".git",
    ".venv",
    "adapters",
    "knowledge_base",
    "original_doc",
    "runtime",
    "venv",
}

CHANNEL_TO_ASSET = {
    "clean": "DocMason-clean.zip",
    "demo-ico-gcs": "DocMason-demo-ico-gcs.zip",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update the DocMason core files in-place.")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory to update. Defaults to the current directory.",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="Local release bundle to apply. When omitted, the script fetches the latest asset.",
    )
    return parser.parse_args()


def load_manifest(workspace: Path) -> dict[str, object]:
    manifest_path = workspace / "distribution-manifest.json"
    if not manifest_path.exists():
        raise SystemExit(
            "distribution-manifest.json is missing. "
            "This updater only supports generated clean/demo bundles."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def fetch_latest_bundle(workspace: Path, manifest: dict[str, object]) -> Path:
    source_repo = manifest.get("source_repo")
    channel = str(manifest.get("distribution_channel") or "")
    asset_name = CHANNEL_TO_ASSET.get(channel)
    if not isinstance(source_repo, str) or not source_repo or not asset_name:
        raise SystemExit(
            "This workspace does not record a source_repo/channel pair. "
            "Rerun with --bundle <path-to-zip>."
        )
    asset_url = f"https://github.com/{source_repo}/releases/latest/download/{asset_name}"
    temp_zip = workspace / f".{asset_name}.download"
    urllib.request.urlretrieve(asset_url, temp_zip)
    return temp_zip


def overlay_tree(source_root: Path, workspace: Path) -> None:
    for child in sorted(source_root.iterdir()):
        if child.name in PROTECTED_TOP_LEVEL:
            continue
        destination = workspace / child.name
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, destination)


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    manifest = load_manifest(workspace)
    bundle_path = args.bundle.resolve() if args.bundle else fetch_latest_bundle(workspace, manifest)
    cleanup_bundle = args.bundle is None

    try:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            with zipfile.ZipFile(bundle_path) as archive:
                archive.extractall(tempdir)
            overlay_tree(tempdir, workspace)
    finally:
        if cleanup_bundle and bundle_path.exists():
            bundle_path.unlink()

    print(
        "Updated DocMason core files while preserving "
        "original_doc/, knowledge_base/, runtime/, adapters/, and .git."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

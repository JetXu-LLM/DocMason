"""Published-snapshot helpers for DocMason knowledge-base publication."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .coordination import workspace_lease
from .project import WorkspacePaths, read_json, write_json


def build_snapshot_id(validation_report: dict[str, Any]) -> str:
    """Build a deterministic-enough published snapshot identifier."""
    source_signature = str(validation_report.get("source_signature") or "unknown")
    return f"{source_signature[:12]}-{uuid.uuid4().hex[:12]}"


def activate_snapshot(paths: WorkspacePaths, snapshot_id: str) -> None:
    """Point the compatibility `knowledge_base/current` path at one immutable snapshot."""
    snapshot_dir = paths.knowledge_version_dir(snapshot_id)
    if not snapshot_dir.exists():
        raise FileNotFoundError(snapshot_dir)

    current_path = paths.knowledge_base_current_dir
    temp_link = paths.knowledge_base_dir / f".current-link-{snapshot_id}"
    if temp_link.exists() or temp_link.is_symlink():
        if temp_link.is_dir() and not temp_link.is_symlink():
            shutil.rmtree(temp_link)
        else:
            temp_link.unlink()
    target = Path(os.path.relpath(snapshot_dir, paths.knowledge_base_dir))
    os.symlink(target, temp_link)

    if current_path.exists() and not current_path.is_symlink():
        legacy_id = f"legacy-current-{uuid.uuid4().hex[:12]}"
        os.replace(current_path, paths.knowledge_version_dir(legacy_id))
    os.replace(temp_link, current_path)


def publish_staging_snapshot(
    paths: WorkspacePaths,
    *,
    validation_report: dict[str, Any],
    published_at: str,
) -> dict[str, Any]:
    """Publish staging into an immutable snapshot and activate it."""
    snapshot_id = build_snapshot_id(validation_report)
    snapshot_dir = paths.knowledge_version_dir(snapshot_id)
    with workspace_lease(paths, "publish"):
        paths.knowledge_base_versions_dir.mkdir(parents=True, exist_ok=True)
        if snapshot_dir.exists():
            shutil.rmtree(snapshot_dir)
        shutil.copytree(paths.knowledge_base_staging_dir, snapshot_dir, symlinks=True)

        interaction_manifest_path = snapshot_dir / "interaction" / "manifest.json"
        interaction_manifest = read_json(interaction_manifest_path)
        if interaction_manifest:
            interaction_manifest["pending_entry_count"] = 0
            interaction_manifest["pending_memory_count"] = 0
            write_json(interaction_manifest_path, interaction_manifest)

        publish_manifest_path = snapshot_dir / "publish_manifest.json"
        publish_manifest = read_json(publish_manifest_path)
        publish_manifest["published_at"] = published_at
        publish_manifest["validation_status"] = validation_report["status"]
        publish_manifest["snapshot_id"] = snapshot_id
        write_json(publish_manifest_path, publish_manifest)

        pointer_payload = {
            "snapshot_id": snapshot_id,
            "snapshot_path": str(snapshot_dir.relative_to(paths.root)),
            "published_at": published_at,
        }
        write_json(paths.knowledge_base_dir / "current-pointer.json", pointer_payload)
        activate_snapshot(paths, snapshot_id)
        return publish_manifest

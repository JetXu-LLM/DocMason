"""Published-state helpers for the single-current DocMason KB model."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .coordination import workspace_lease
from .project import WorkspacePaths, append_jsonl, read_json, write_json

PUBLISH_LEDGER_SCHEMA_VERSION = 1
PUBLISH_DRIVER_SOURCE_DELTA = "source-delta"
PUBLISH_DRIVER_INTERACTION_PROMOTION = "interaction-promotion"
PUBLISH_DRIVER_LEGACY_UNKNOWN = "legacy-unknown"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _relative_path(paths: WorkspacePaths, path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return str(path)


def _file_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_symlink():
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError:
            return 0
        if resolved.is_dir():
            return sum(1 for item in resolved.rglob("*") if item.is_file())
        return 1
    if path.is_file():
        return 1
    return sum(1 for item in path.rglob("*") if item.is_file())


def build_snapshot_id(validation_report: dict[str, Any]) -> str:
    """Build a logical publish-generation identifier."""
    source_signature = str(validation_report.get("source_signature") or "unknown")
    return f"{source_signature[:12]}-{uuid.uuid4().hex[:12]}"


def _published_roots(paths: WorkspacePaths) -> list[Path]:
    if not paths.knowledge_base_published_dir.exists():
        return []
    return sorted(
        path
        for path in paths.knowledge_base_published_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )


def _current_hidden_publish_root(paths: WorkspacePaths) -> Path | None:
    current_path = paths.knowledge_base_current_dir
    if not current_path.is_symlink():
        return None
    try:
        resolved = current_path.resolve(strict=True)
    except FileNotFoundError:
        return None
    if resolved.parent == paths.knowledge_base_published_dir:
        return resolved
    return None


def _switch_current_to_root(paths: WorkspacePaths, target_root: Path) -> Path | None:
    current_path = paths.knowledge_base_current_dir
    temp_link = paths.knowledge_base_dir / f".current-link-{target_root.name}"
    backup_dir: Path | None = None
    _remove_path(temp_link)
    target = Path(os.path.relpath(target_root, paths.knowledge_base_dir))
    os.symlink(target, temp_link)

    if current_path.exists() and not current_path.is_symlink():
        backup_dir = paths.knowledge_base_dir / f".legacy-current-backup-{uuid.uuid4().hex[:12]}"
        _remove_path(backup_dir)
        os.replace(current_path, backup_dir)

    os.replace(temp_link, current_path)
    return backup_dir


def _normalize_interaction_manifest(root: Path) -> None:
    interaction_manifest_path = root / "interaction" / "manifest.json"
    interaction_manifest = read_json(interaction_manifest_path)
    if not interaction_manifest:
        return
    interaction_manifest["pending_entry_count"] = 0
    interaction_manifest["pending_memory_count"] = 0
    write_json(interaction_manifest_path, interaction_manifest)


def _write_publish_pointer(
    paths: WorkspacePaths,
    *,
    snapshot_id: str,
    published_at: str,
    published_source_signature: str | None,
    published_root: Path,
) -> None:
    write_json(
        paths.current_publish_pointer_path,
        {
            "snapshot_id": snapshot_id,
            "published_root_path": _relative_path(paths, published_root),
            "published_at": published_at,
            "published_source_signature": published_source_signature,
        },
    )


def _prune_published_roots(paths: WorkspacePaths, *, keep_roots: set[Path]) -> list[str]:
    deleted: list[str] = []
    for root in _published_roots(paths):
        if root in keep_roots:
            continue
        deleted.append(str(root.name))
        _remove_path(root)
    return deleted


def publish_ledger_entries(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """Load the compact logical publish ledger."""
    if not paths.publish_ledger_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in paths.publish_ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _ledger_snapshot_ids(paths: WorkspacePaths) -> set[str]:
    return {
        str(record.get("snapshot_id"))
        for record in publish_ledger_entries(paths)
        if isinstance(record.get("snapshot_id"), str) and record.get("snapshot_id")
    }


def append_publish_ledger_record(
    paths: WorkspacePaths,
    *,
    snapshot_id: str,
    published_at: str,
    published_source_signature: str | None,
    validation_status: str | None,
    rebuild_cause: str | None,
    publish_driver: str,
    legacy_backfilled: bool = False,
) -> dict[str, Any]:
    """Append one logical publish-generation record."""
    payload = {
        "schema_version": PUBLISH_LEDGER_SCHEMA_VERSION,
        "recorded_at": _utc_now(),
        "snapshot_id": snapshot_id,
        "published_at": published_at,
        "published_source_signature": published_source_signature,
        "validation_status": validation_status,
        "rebuild_cause": rebuild_cause,
        "publish_driver": publish_driver,
        "legacy_backfilled": legacy_backfilled,
    }
    append_jsonl(paths.publish_ledger_path, payload)
    return payload


def _publish_storage_summary(paths: WorkspacePaths, *, recent_limit: int = 5) -> dict[str, Any]:
    ledger = publish_ledger_entries(paths)
    current_manifest = read_json(paths.current_publish_manifest_path)
    current_pointer = read_json(paths.current_publish_pointer_path)
    legacy = legacy_publish_storage_state(paths)
    recent_records = list(reversed(ledger[-recent_limit:]))
    return {
        "publish_model": "single-current",
        "current_snapshot_id": current_manifest.get("snapshot_id")
        or current_pointer.get("snapshot_id"),
        "published_root_count": len(_published_roots(paths)),
        "publish_ledger_count": len(ledger),
        "recent_publish_snapshot_ids": [
            record["snapshot_id"]
            for record in recent_records
            if isinstance(record.get("snapshot_id"), str) and record.get("snapshot_id")
        ],
        "legacy_archive_detected": legacy["detected"],
        "legacy_archive_version_count": legacy["archive_manifest_count"],
        "legacy_runtime_files": legacy["legacy_runtime_files"],
        "legacy_archive_note": legacy.get("note"),
    }


def storage_lifecycle_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Return a compact artifact-family lifecycle summary for local workspace storage."""

    publish_storage = _publish_storage_summary(paths)

    def family(
        *,
        name: str,
        path: Path,
        truth_class: str,
        retention_unit: str,
        delete_trigger: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "name": name,
            "path": _relative_path(paths, path),
            "present": path.exists(),
            "file_count": _file_count(path),
            "truth_class": truth_class,
            "retention_unit": retention_unit,
            "delete_trigger": delete_trigger,
        }
        if extra:
            payload.update(extra)
        return payload

    families = [
        family(
            name="published-roots",
            path=paths.knowledge_base_published_dir,
            truth_class="canonical",
            retention_unit="publish-root",
            delete_trigger="next-publish-switch",
            extra={"root_count": publish_storage["published_root_count"]},
        ),
        family(
            name="current-published",
            path=paths.knowledge_base_current_dir,
            truth_class="canonical",
            retention_unit="publish-surface",
            delete_trigger="next-publish-switch",
            extra={"current_snapshot_id": publish_storage["current_snapshot_id"]},
        ),
        family(
            name="staging",
            path=paths.knowledge_base_staging_dir,
            truth_class="rebuildable",
            retention_unit="staging-tree",
            delete_trigger="next-sync-or-manual-cleanup",
        ),
        family(
            name="publish-ledger",
            path=paths.publish_ledger_path,
            truth_class="canonical",
            retention_unit="publish-generation-record",
            delete_trigger="manual-runtime-cleanup",
            extra={"entry_count": publish_storage["publish_ledger_count"]},
        ),
        family(
            name="answers",
            path=paths.answers_dir,
            truth_class="canonical",
            retention_unit="answer-file",
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="query-sessions",
            path=paths.query_sessions_dir,
            truth_class="derived",
            retention_unit="session-log",
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="retrieval-traces",
            path=paths.retrieval_traces_dir,
            truth_class="derived",
            retention_unit="trace-log",
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="review-artifacts",
            path=paths.review_logs_dir,
            truth_class="derived",
            retention_unit="review-artifact",
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="control-plane",
            path=paths.control_plane_dir,
            truth_class="canonical",
            retention_unit="job-or-state-record",
            delete_trigger="settlement-or-manual-cleanup",
        ),
        family(
            name="interaction-ingest",
            path=paths.interaction_ingest_dir,
            truth_class="transient",
            retention_unit="ingest-entry",
            delete_trigger="promotion-or-manual-cleanup",
        ),
        family(
            name="agent-work",
            path=paths.agent_work_dir,
            truth_class="transient",
            retention_unit="work-artifact",
            delete_trigger="manual-cleanup",
        ),
        family(
            name="eval-artifacts",
            path=paths.eval_dir,
            truth_class="derived",
            retention_unit="eval-run-or-baseline",
            delete_trigger="manual-cleanup",
        ),
    ]
    return {
        "family_count": len(families),
        **publish_storage,
        "families": families,
    }


def publish_storage_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Return the compact publish-storage summary used by status and sync surfaces."""
    return _publish_storage_summary(paths)


def _legacy_snapshot_records(paths: WorkspacePaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.knowledge_base_versions_dir.exists():
        return records
    for snapshot_dir in sorted(paths.knowledge_base_versions_dir.iterdir()):
        if not snapshot_dir.is_dir():
            continue
        publish_manifest = read_json(snapshot_dir / "publish_manifest.json")
        validation_report = read_json(snapshot_dir / "validation_report.json")
        if not publish_manifest and not validation_report:
            continue
        snapshot_id = str(publish_manifest.get("snapshot_id") or snapshot_dir.name)
        records.append(
            {
                "snapshot_id": snapshot_id,
                "directory": snapshot_dir,
                "published_at": publish_manifest.get("published_at"),
                "published_source_signature": publish_manifest.get("published_source_signature")
                or validation_report.get("source_signature"),
                "validation_status": publish_manifest.get("validation_status")
                or validation_report.get("status"),
            }
        )
    records.sort(
        key=lambda item: (
            _parse_timestamp(item.get("published_at")) or datetime.min.replace(tzinfo=UTC),
            str(item.get("snapshot_id") or ""),
        )
    )
    return records


def legacy_publish_storage_state(paths: WorkspacePaths) -> dict[str, Any]:
    """Describe whether the workspace still carries the old archive-retention model."""
    legacy_runtime_files = [
        _relative_path(paths, path)
        for path in (paths.snapshot_retention_state_path, paths.snapshot_pins_path)
        if path.exists()
    ]
    archive_records = _legacy_snapshot_records(paths)
    detected = (
        paths.knowledge_base_versions_dir.exists()
        or bool(legacy_runtime_files)
        or (
            paths.knowledge_base_current_dir.is_symlink()
            and paths.knowledge_base_current_dir.resolve(strict=False).parent
            == paths.knowledge_base_versions_dir
        )
    )
    note = None
    if detected:
        note = (
            "Legacy archived KB storage is still present and will be compacted on the next "
            "mutating sync."
        )
    return {
        "detected": detected,
        "archive_manifest_count": len(archive_records),
        "archive_snapshot_ids": [
            record["snapshot_id"]
            for record in archive_records
            if isinstance(record.get("snapshot_id"), str)
        ],
        "legacy_runtime_files": [value for value in legacy_runtime_files if isinstance(value, str)],
        "note": note,
    }


def _current_published_source_dir(paths: WorkspacePaths) -> Path | None:
    current_path = paths.knowledge_base_current_dir
    if current_path.is_symlink():
        try:
            resolved = current_path.resolve(strict=True)
        except FileNotFoundError:
            resolved = None
        if isinstance(resolved, Path) and resolved.is_dir():
            return resolved
    if current_path.exists() and current_path.is_dir():
        return current_path

    current_pointer = read_json(paths.current_publish_pointer_path)
    snapshot_id = str(current_pointer.get("snapshot_id") or "")
    if snapshot_id:
        snapshot_dir = paths.knowledge_version_dir(snapshot_id)
        if snapshot_dir.exists():
            return snapshot_dir
    return None


def _current_snapshot_id(paths: WorkspacePaths, *, current_source_dir: Path | None = None) -> str | None:
    current_manifest = read_json(paths.current_publish_manifest_path)
    if isinstance(current_manifest.get("snapshot_id"), str) and current_manifest.get("snapshot_id"):
        return str(current_manifest["snapshot_id"])

    current_pointer = read_json(paths.current_publish_pointer_path)
    if isinstance(current_pointer.get("snapshot_id"), str) and current_pointer.get("snapshot_id"):
        return str(current_pointer["snapshot_id"])

    if isinstance(current_source_dir, Path):
        source_manifest = read_json(current_source_dir / "publish_manifest.json")
        if isinstance(source_manifest.get("snapshot_id"), str) and source_manifest.get("snapshot_id"):
            return str(source_manifest["snapshot_id"])
        if current_source_dir.parent in {
            paths.knowledge_base_versions_dir,
            paths.knowledge_base_published_dir,
        }:
            return current_source_dir.name
    return None


def _backfill_current_publish_record_if_needed(
    paths: WorkspacePaths,
    *,
    snapshot_id: str,
    current_source_dir: Path,
) -> dict[str, Any] | None:
    if snapshot_id in _ledger_snapshot_ids(paths):
        return None
    publish_manifest = read_json(current_source_dir / "publish_manifest.json")
    validation_report = read_json(current_source_dir / "validation_report.json")
    published_at = str(publish_manifest.get("published_at") or _utc_now())
    return append_publish_ledger_record(
        paths,
        snapshot_id=snapshot_id,
        published_at=published_at,
        published_source_signature=publish_manifest.get("published_source_signature")
        or validation_report.get("source_signature"),
        validation_status=publish_manifest.get("validation_status")
        or validation_report.get("status"),
        rebuild_cause="legacy-unknown",
        publish_driver=PUBLISH_DRIVER_LEGACY_UNKNOWN,
        legacy_backfilled=True,
    )


def _materialize_current_root_into_single_current_storage(
    paths: WorkspacePaths,
    *,
    current_source_dir: Path,
    current_snapshot_id: str,
) -> Path:
    """Move or copy the live current published tree into `.published/`."""
    target_root = paths.knowledge_published_root_dir(current_snapshot_id)
    if current_source_dir == target_root:
        return target_root
    paths.knowledge_base_published_dir.mkdir(parents=True, exist_ok=True)
    _remove_path(target_root)
    if current_source_dir.parent in {paths.knowledge_base_versions_dir, paths.knowledge_base_dir}:
        os.replace(current_source_dir, target_root)
    else:
        shutil.copytree(current_source_dir, target_root, symlinks=True)
    return target_root


def migrate_legacy_publish_storage(paths: WorkspacePaths) -> dict[str, Any]:
    """Compact one legacy archive workspace into single-current publish mode."""
    legacy = legacy_publish_storage_state(paths)
    if not legacy["detected"]:
        return {"legacy_detected": False, "migrated": False, "actions": []}

    actions: list[dict[str, Any]] = []
    ledger_ids = _ledger_snapshot_ids(paths)
    backfilled_snapshot_ids: list[str] = []
    for record in _legacy_snapshot_records(paths):
        snapshot_id = str(record.get("snapshot_id") or "")
        if not snapshot_id or snapshot_id in ledger_ids:
            continue
        append_publish_ledger_record(
            paths,
            snapshot_id=snapshot_id,
            published_at=str(record.get("published_at") or _utc_now()),
            published_source_signature=record.get("published_source_signature"),
            validation_status=record.get("validation_status"),
            rebuild_cause="legacy-unknown",
            publish_driver=PUBLISH_DRIVER_LEGACY_UNKNOWN,
            legacy_backfilled=True,
        )
        ledger_ids.add(snapshot_id)
        backfilled_snapshot_ids.append(snapshot_id)
    if backfilled_snapshot_ids:
        actions.append(
            {
                "kind": "backfilled-publish-ledger",
                "snapshot_ids": backfilled_snapshot_ids,
                "count": len(backfilled_snapshot_ids),
            }
        )

    current_source_dir = _current_published_source_dir(paths)
    current_snapshot_id = _current_snapshot_id(paths, current_source_dir=current_source_dir)
    if current_source_dir is not None and current_snapshot_id:
        _backfill_current_publish_record_if_needed(
            paths,
            snapshot_id=current_snapshot_id,
            current_source_dir=current_source_dir,
        )
        target_root = _materialize_current_root_into_single_current_storage(
            paths,
            current_source_dir=current_source_dir,
            current_snapshot_id=current_snapshot_id,
        )
        backup_dir = _switch_current_to_root(paths, target_root)
        _normalize_interaction_manifest(target_root)
        _write_publish_pointer(
            paths,
            snapshot_id=current_snapshot_id,
            published_at=str(
                read_json(target_root / "publish_manifest.json").get("published_at") or _utc_now()
            ),
            published_source_signature=read_json(target_root / "publish_manifest.json").get(
                "published_source_signature"
            ),
            published_root=target_root,
        )
        if backup_dir is not None:
            _remove_path(backup_dir)
        deleted_roots = _prune_published_roots(paths, keep_roots={target_root})
        actions.append(
            {
                "kind": "compacted-legacy-current-publish-root",
                "snapshot_id": current_snapshot_id,
                "deleted_hidden_root_ids": deleted_roots,
            }
        )

    deleted_archive_dirs: list[str] = []
    if paths.knowledge_base_versions_dir.exists():
        deleted_archive_dirs = [path.name for path in paths.knowledge_base_versions_dir.iterdir()]
        shutil.rmtree(paths.knowledge_base_versions_dir)
        actions.append(
            {
                "kind": "deleted-legacy-archive-dir",
                "path": _relative_path(paths, paths.knowledge_base_versions_dir),
                "deleted_entry_count": len(deleted_archive_dirs),
            }
        )

    retired_files: list[str] = []
    for legacy_file in (paths.snapshot_retention_state_path, paths.snapshot_pins_path):
        if legacy_file.exists():
            retired_files.append(str(legacy_file.relative_to(paths.root)))
            legacy_file.unlink()
    if retired_files:
        actions.append(
            {
                "kind": "retired-legacy-runtime-files",
                "paths": retired_files,
            }
        )

    return {
        "legacy_detected": True,
        "migrated": True,
        "actions": actions,
        "backfilled_snapshot_ids": backfilled_snapshot_ids,
        "deleted_archive_dirs": deleted_archive_dirs,
        "current_snapshot_id": current_snapshot_id,
    }


def publish_staging_snapshot(
    paths: WorkspacePaths,
    *,
    validation_report: dict[str, Any],
    published_at: str,
    rebuild_cause: str | None,
    publish_driver: str,
) -> dict[str, Any]:
    """Publish staging into the single-current hidden publish root and switch `current`."""
    snapshot_id = build_snapshot_id(validation_report)
    target_root = paths.knowledge_published_root_dir(snapshot_id)
    with workspace_lease(paths, "publish"):
        paths.knowledge_base_published_dir.mkdir(parents=True, exist_ok=True)
        previous_hidden_root = _current_hidden_publish_root(paths)
        _remove_path(target_root)
        shutil.copytree(paths.knowledge_base_staging_dir, target_root, symlinks=True)
        _normalize_interaction_manifest(target_root)

        publish_manifest_path = target_root / "publish_manifest.json"
        publish_manifest = read_json(publish_manifest_path)
        publish_manifest["published_at"] = published_at
        publish_manifest["validation_status"] = validation_report["status"]
        publish_manifest["snapshot_id"] = snapshot_id
        publish_manifest["published_source_signature"] = validation_report.get("source_signature")
        write_json(publish_manifest_path, publish_manifest)

        backup_dir = _switch_current_to_root(paths, target_root)
        if backup_dir is not None:
            _remove_path(backup_dir)
        _write_publish_pointer(
            paths,
            snapshot_id=snapshot_id,
            published_at=published_at,
            published_source_signature=validation_report.get("source_signature"),
            published_root=target_root,
        )
        keep_roots = {target_root}
        if previous_hidden_root is not None and previous_hidden_root == target_root:
            keep_roots.add(previous_hidden_root)
        _prune_published_roots(paths, keep_roots=keep_roots)
        append_publish_ledger_record(
            paths,
            snapshot_id=snapshot_id,
            published_at=published_at,
            published_source_signature=validation_report.get("source_signature"),
            validation_status=validation_report.get("status"),
            rebuild_cause=rebuild_cause,
            publish_driver=publish_driver,
        )
        return publish_manifest


def stale_run_cutoff(now: datetime | None = None) -> datetime:
    """Return the hard-coded stale-active-run cutoff."""
    reference = now or datetime.now(tz=UTC)
    return reference - timedelta(hours=24)

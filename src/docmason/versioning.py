"""Published-state helpers for the single-current DocMason KB model."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
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


def _snapshot_tree_stats(path: Path) -> tuple[set[str], int, int]:
    """Return inventory and logical file cost in one metadata walk."""
    inventory: set[str] = set()
    file_count = 0
    logical_bytes = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            inventory.add(str(item.relative_to(path)))
            continue
        if not item.is_file():
            continue
        inventory.add(str(item.relative_to(path)))
        file_count += 1
        logical_bytes += item.stat().st_size
    return inventory, file_count, logical_bytes


def _snapshot_tree_cost(path: Path) -> tuple[int, int, int]:
    """Return entry count, regular-file count, and logical bytes without path materialization."""
    inventory_entries = 0
    file_count = 0
    logical_bytes = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            inventory_entries += 1
            continue
        if not item.is_file():
            continue
        inventory_entries += 1
        file_count += 1
        logical_bytes += item.stat().st_size
    return inventory_entries, file_count, logical_bytes


def _darwin_clone_tree(source: Path, target: Path) -> bool:
    """Atomically clone an APFS directory hierarchy with the native syscall."""
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    clonefile = libc.clonefile
    clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clonefile.restype = ctypes.c_int
    return bool(clonefile(os.fsencode(source), os.fsencode(target), 0) == 0)


def clone_or_copy_path(source: Path, target: Path) -> dict[str, Any]:
    """Clone one file or tree when supported, with a verified safe-copy fallback."""
    _remove_path(target)
    method = "copy"
    clone_strategy: str | None = None
    if platform.system() == "Darwin":
        try:
            cloned = _darwin_clone_tree(source, target)
        except (AttributeError, OSError):
            cloned = False
        if cloned:
            method = "apfs-clone"
            clone_strategy = "clonefile"
    elif platform.system() == "Linux":
        result = subprocess.run(
            ["cp", "--reflink=always", "-a", str(source), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            method = "linux-reflink"
            clone_strategy = "cp-reflink"
    if not target.exists():
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)

    if source.is_dir():
        source_inventory, file_count, logical_bytes = _snapshot_tree_stats(source)
        target_inventory, target_file_count, target_logical_bytes = _snapshot_tree_stats(target)
        valid = (
            source_inventory == target_inventory
            and file_count == target_file_count
            and logical_bytes == target_logical_bytes
        )
        inventory_entries = len(source_inventory)
    else:
        source_stat = source.stat()
        target_stat = target.stat()
        file_count = 1
        logical_bytes = source_stat.st_size
        inventory_entries = 1
        valid = source_stat.st_size == target_stat.st_size
    if not valid:
        _remove_path(target)
        raise ValueError(f"Clone/copy verification failed for `{source}`.")
    cloned = method in {"apfs-clone", "linux-reflink"}
    return {
        "method": method,
        "clone_strategy": clone_strategy,
        "files_cloned": file_count if cloned else 0,
        "bytes_cloned": logical_bytes if cloned else 0,
        "files_copied": 0 if cloned else file_count,
        "bytes_copied": 0 if cloned else logical_bytes,
        "inventory_entries": inventory_entries,
    }


def copy_snapshot_tree(source: Path, target: Path) -> dict[str, Any]:
    """Materialize a publish candidate with clone/reflink/copy fallback telemetry."""
    _remove_path(target)
    method = "copytree"
    clone_strategy: str | None = None
    if platform.system() == "Darwin":
        try:
            native_cloned = _darwin_clone_tree(source, target)
        except (AttributeError, OSError):
            native_cloned = False
        if native_cloned:
            method = "apfs-clone"
            clone_strategy = "clonefile"
        else:
            _remove_path(target)
            result = subprocess.run(
                ["/bin/cp", "-cR", str(source), str(target)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                method = "apfs-clone"
                clone_strategy = "cp-clone"
            else:
                _remove_path(target)
    elif platform.system() == "Linux":
        result = subprocess.run(
            ["cp", "--reflink=always", "-a", str(source), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            method = "linux-reflink"
            clone_strategy = "cp-reflink"
        else:
            _remove_path(target)
    if not target.exists():
        shutil.copytree(source, target, symlinks=True)

    required = {
        "catalog.json",
        "coverage_manifest.json",
        "graph_edges.json",
        "pending_work.json",
        "hybrid_work.json",
        "publish_manifest.json",
        "validation_report.json",
    }
    source_inventory, file_count, logical_bytes = _snapshot_tree_stats(source)
    target_inventory, target_file_count, target_logical_bytes = _snapshot_tree_stats(target)
    if (
        source_inventory != target_inventory
        or file_count != target_file_count
        or logical_bytes != target_logical_bytes
        or not required.issubset(target_inventory)
    ):
        _remove_path(target)
        raise ValueError("Publish candidate failed root-manifest or file-inventory validation.")
    inventory_entries = len(source_inventory)

    cloned = method in {"apfs-clone", "linux-reflink"}
    return {
        "method": method,
        "clone_strategy": clone_strategy,
        "files_cloned": file_count if cloned else 0,
        "bytes_cloned": logical_bytes if cloned else 0,
        "files_copied": 0 if cloned else file_count,
        "bytes_copied": 0 if cloned else logical_bytes,
        "inventory_entries": inventory_entries,
    }


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
    if resolved.parent.resolve() == paths.knowledge_base_published_dir.resolve():
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


def _restore_current_after_failed_switch(
    paths: WorkspacePaths,
    *,
    previous_hidden_root: Path | None,
    legacy_backup_dir: Path | None,
) -> None:
    """Restore the exact pre-publication current surface after a failed commit."""
    current_path = paths.knowledge_base_current_dir
    if current_path.exists() or current_path.is_symlink():
        _remove_path(current_path)
    if legacy_backup_dir is not None and legacy_backup_dir.exists():
        os.replace(legacy_backup_dir, current_path)
        return
    if previous_hidden_root is not None and previous_hidden_root.exists():
        temporary_link = paths.knowledge_base_dir / f".current-rollback-{uuid.uuid4().hex}"
        target = Path(
            os.path.relpath(
                previous_hidden_root.resolve(),
                paths.knowledge_base_dir.resolve(),
            )
        )
        os.symlink(target, temporary_link)
        os.replace(temporary_link, current_path)


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


def _current_snapshot_id(
    paths: WorkspacePaths, *, current_source_dir: Path | None = None
) -> str | None:
    current_manifest = read_json(paths.current_publish_manifest_path)
    if isinstance(current_manifest.get("snapshot_id"), str) and current_manifest.get("snapshot_id"):
        return str(current_manifest["snapshot_id"])

    current_pointer = read_json(paths.current_publish_pointer_path)
    if isinstance(current_pointer.get("snapshot_id"), str) and current_pointer.get("snapshot_id"):
        return str(current_pointer["snapshot_id"])

    if isinstance(current_source_dir, Path):
        source_manifest = read_json(current_source_dir / "publish_manifest.json")
        if isinstance(source_manifest.get("snapshot_id"), str) and source_manifest.get(
            "snapshot_id"
        ):
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
        publish_copy = copy_snapshot_tree(paths.knowledge_base_staging_dir, target_root)
        candidate_validation = read_json(target_root / "validation_report.json")
        if (
            candidate_validation.get("status") != validation_report.get("status")
            or candidate_validation.get("source_signature")
            != validation_report.get("source_signature")
        ):
            _remove_path(target_root)
            raise ValueError("Publish candidate validation does not match validated staging.")
        _normalize_interaction_manifest(target_root)

        publish_manifest_path = target_root / "publish_manifest.json"
        publish_manifest = read_json(publish_manifest_path)
        publish_manifest["published_at"] = published_at
        publish_manifest["validation_status"] = validation_report["status"]
        publish_manifest["snapshot_id"] = snapshot_id
        publish_manifest["published_source_signature"] = validation_report.get("source_signature")
        publish_manifest["publish_copy"] = publish_copy
        write_json(publish_manifest_path, publish_manifest)

        prior_pointer = (
            paths.current_publish_pointer_path.read_bytes()
            if paths.current_publish_pointer_path.exists()
            else None
        )
        prior_ledger = (
            paths.publish_ledger_path.read_bytes()
            if paths.publish_ledger_path.exists()
            else None
        )
        backup_dir = _switch_current_to_root(paths, target_root)
        try:
            _write_publish_pointer(
                paths,
                snapshot_id=snapshot_id,
                published_at=published_at,
                published_source_signature=validation_report.get("source_signature"),
                published_root=target_root,
            )
            append_publish_ledger_record(
                paths,
                snapshot_id=snapshot_id,
                published_at=published_at,
                published_source_signature=validation_report.get("source_signature"),
                validation_status=validation_report.get("status"),
                rebuild_cause=rebuild_cause,
                publish_driver=publish_driver,
            )
        except Exception:
            _restore_current_after_failed_switch(
                paths,
                previous_hidden_root=previous_hidden_root,
                legacy_backup_dir=backup_dir,
            )
            if prior_pointer is None:
                if paths.current_publish_pointer_path.exists():
                    paths.current_publish_pointer_path.unlink()
            else:
                paths.current_publish_pointer_path.parent.mkdir(parents=True, exist_ok=True)
                paths.current_publish_pointer_path.write_bytes(prior_pointer)
            if prior_ledger is None:
                paths.publish_ledger_path.unlink(missing_ok=True)
            else:
                paths.publish_ledger_path.parent.mkdir(parents=True, exist_ok=True)
                paths.publish_ledger_path.write_bytes(prior_ledger)
            _remove_path(target_root)
            raise
        if backup_dir is not None:
            _remove_path(backup_dir)
        keep_roots = {target_root}
        cleanup: dict[str, Any]
        try:
            deleted_roots = _prune_published_roots(paths, keep_roots=keep_roots)
            cleanup = {"status": "completed", "deleted_roots": deleted_roots}
        except OSError as error:
            cleanup = {
                "status": "degraded",
                "deleted_roots": [],
                "error": str(error),
            }
        publish_manifest["publish_cleanup"] = cleanup
        try:
            write_json(publish_manifest_path, publish_manifest)
        except OSError as error:
            # Publication is already committed once `current`, the pointer, and the
            # ledger agree. A best-effort cleanup annotation must never turn that
            # successful commit into a reported failure that callers may retry.
            cleanup["manifest_update"] = "degraded"
            cleanup["manifest_error"] = str(error)
        return publish_manifest


def stale_run_cutoff(now: datetime | None = None) -> datetime:
    """Return the hard-coded stale-active-run cutoff."""
    reference = now or datetime.now(tz=UTC)
    return reference - timedelta(hours=24)

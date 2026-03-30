"""Published-snapshot helpers for DocMason knowledge-base publication."""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .coordination import workspace_lease
from .project import WorkspacePaths, list_visible_files, read_json, write_json

SNAPSHOT_RETENTION_SCHEMA_VERSION = 1
DEFAULT_UNPINNED_SNAPSHOT_RETENTION_COUNT = 2
DEFAULT_UNPINNED_SNAPSHOT_RETENTION_DAYS = 3


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _snapshot_records(paths: WorkspacePaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not paths.knowledge_base_versions_dir.exists():
        return records
    for snapshot_dir in sorted(paths.knowledge_base_versions_dir.iterdir()):
        if not snapshot_dir.is_dir():
            continue
        publish_manifest = read_json(snapshot_dir / "publish_manifest.json")
        validation_report = read_json(snapshot_dir / "validation_report.json")
        snapshot_id = str(publish_manifest.get("snapshot_id") or snapshot_dir.name)
        published_at = publish_manifest.get("published_at")
        published_source_signature = (
            publish_manifest.get("published_source_signature")
            or validation_report.get("source_signature")
        )
        records.append(
            {
                "snapshot_id": snapshot_id,
                "path": str(snapshot_dir.relative_to(paths.root)),
                "directory": snapshot_dir,
                "published_at": published_at,
                "published_source_signature": published_source_signature,
                "validation_status": publish_manifest.get("validation_status"),
            }
        )
    records.sort(
        key=lambda item: (
            _parse_timestamp(item.get("published_at")) or datetime.min.replace(tzinfo=UTC),
            str(item.get("snapshot_id") or ""),
        ),
        reverse=True,
    )
    return records


def _record_snapshot_pin(pin_reasons: dict[str, list[str]], snapshot_id: str, reason: str) -> None:
    if not snapshot_id or not reason:
        return
    reasons = pin_reasons.setdefault(snapshot_id, [])
    if reason not in reasons:
        reasons.append(reason)


def _snapshot_pin_reasons(paths: WorkspacePaths) -> dict[str, list[str]]:
    pin_reasons: dict[str, list[str]] = {}

    current_pointer = read_json(paths.knowledge_base_dir / "current-pointer.json")
    current_snapshot_id = str(current_pointer.get("snapshot_id") or "")
    if current_snapshot_id:
        _record_snapshot_pin(pin_reasons, current_snapshot_id, "current")

    if paths.runs_dir.exists():
        for run_state_path in sorted(paths.runs_dir.glob("*/state.json")):
            run_state = read_json(run_state_path)
            if not run_state or run_state.get("status") != "active":
                continue
            version_context = run_state.get("version_context")
            if not isinstance(version_context, dict):
                version_context = {}
            snapshot_id = str(
                version_context.get("published_snapshot_id")
                or run_state.get("published_snapshot_id_used")
                or ""
            )
            if snapshot_id:
                _record_snapshot_pin(
                    pin_reasons,
                    snapshot_id,
                    f"active-run:{run_state_path.parent.name}",
                )

    shared_jobs_index = read_json(paths.shared_jobs_index_path)
    active_by_key = shared_jobs_index.get("active_by_key")
    if isinstance(active_by_key, dict):
        for job_id in active_by_key.values():
            if not isinstance(job_id, str) or not job_id:
                continue
            manifest = read_json(paths.shared_jobs_dir / job_id / "manifest.json")
            if not manifest:
                continue
            scope = manifest.get("scope")
            if not isinstance(scope, dict):
                scope = {}
            snapshot_id = str(scope.get("published_snapshot_id") or "")
            if snapshot_id:
                job_family = str(manifest.get("job_family") or "shared-job")
                _record_snapshot_pin(
                    pin_reasons,
                    snapshot_id,
                    f"active-shared-job:{job_family}:{job_id}",
                )

    if paths.eval_benchmarks_dir.exists():
        for baseline_path in sorted(paths.eval_benchmarks_dir.glob("*/baseline.json")):
            baseline = read_json(baseline_path)
            if not baseline:
                continue
            version_context = baseline.get("version_context")
            if not isinstance(version_context, dict):
                version_context = {}
            snapshot_id = str(version_context.get("published_snapshot_id") or "")
            if snapshot_id:
                _record_snapshot_pin(
                    pin_reasons,
                    snapshot_id,
                    f"eval-baseline:{baseline_path.parent.name}",
                )

    manual_pins = read_json(paths.snapshot_pins_path)
    for item in manual_pins.get("pins", []):
        if not isinstance(item, dict):
            continue
        snapshot_id = str(item.get("snapshot_id") or "")
        if not snapshot_id:
            continue
        pin_kind = str(item.get("pin_kind") or "manual")
        pin_id = str(item.get("pin_id") or "")
        label = f"{pin_kind}:{pin_id}" if pin_id else pin_kind
        _record_snapshot_pin(pin_reasons, snapshot_id, label)

    return pin_reasons


def _snapshot_retention_payload(
    paths: WorkspacePaths,
    *,
    apply_deletions: bool,
    persist_state: bool,
) -> dict[str, Any]:
    """Build one snapshot-retention decision payload and optionally apply it."""
    policy = {
        "schema_version": SNAPSHOT_RETENTION_SCHEMA_VERSION,
        "max_unpinned_snapshots": DEFAULT_UNPINNED_SNAPSHOT_RETENTION_COUNT,
        "max_unpinned_age_days": DEFAULT_UNPINNED_SNAPSHOT_RETENTION_DAYS,
    }
    pin_reasons = _snapshot_pin_reasons(paths)
    records = _snapshot_records(paths)
    now = datetime.now(tz=UTC)
    decisions: list[dict[str, Any]] = []
    deleted_snapshot_ids: list[str] = []
    eligible_delete_snapshot_ids: list[str] = []
    deletion_failures: list[dict[str, str]] = []
    retained_snapshot_ids: list[str] = []
    unpinned_rank = 0

    for record in records:
        snapshot_id = str(record.get("snapshot_id") or "")
        published_at = _parse_timestamp(record.get("published_at"))
        age_days = (
            round(max((now - published_at).total_seconds(), 0.0) / 86400.0, 6)
            if published_at is not None
            else None
        )
        reasons = list(pin_reasons.get(snapshot_id, []))
        retained = False
        deleted = False
        retention_reason = ""
        if reasons:
            retained = True
            retention_reason = "pinned"
        else:
            unpinned_rank += 1
            if unpinned_rank <= DEFAULT_UNPINNED_SNAPSHOT_RETENTION_COUNT:
                retained = True
                retention_reason = "recent-count"
            elif (
                age_days is not None
                and age_days <= DEFAULT_UNPINNED_SNAPSHOT_RETENTION_DAYS
            ):
                retained = True
                retention_reason = "recent-age"
            else:
                snapshot_dir = record.get("directory")
                if not apply_deletions:
                    retention_reason = "expired-unpinned"
                    eligible_delete_snapshot_ids.append(snapshot_id)
                elif isinstance(snapshot_dir, Path) and snapshot_dir.exists():
                    try:
                        shutil.rmtree(snapshot_dir)
                        deleted = True
                        retention_reason = "expired-unpinned"
                        deleted_snapshot_ids.append(snapshot_id)
                    except OSError as exc:
                        retained = True
                        retention_reason = "delete-failed"
                        deletion_failures.append(
                            {
                                "snapshot_id": snapshot_id,
                                "detail": str(exc),
                            }
                        )
                else:
                    deleted = True
                    retention_reason = "expired-unpinned"
                    deleted_snapshot_ids.append(snapshot_id)
        if retained:
            retained_snapshot_ids.append(snapshot_id)
        decisions.append(
            {
                "snapshot_id": snapshot_id,
                "path": record.get("path"),
                "published_at": record.get("published_at"),
                "published_source_signature": record.get("published_source_signature"),
                "validation_status": record.get("validation_status"),
                "pin_reasons": reasons,
                "age_days": age_days,
                "retained": retained,
                "deleted": deleted,
                "retention_reason": retention_reason,
            }
        )

    payload = {
        "schema_version": SNAPSHOT_RETENTION_SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "applied": apply_deletions,
        "policy": policy,
        "snapshot_count": len(records),
        "deleted_count": len(deleted_snapshot_ids),
        "eligible_delete_snapshot_ids": eligible_delete_snapshot_ids,
        "pinned_snapshot_count": len(pin_reasons),
        "retained_snapshot_ids": retained_snapshot_ids,
        "deleted_snapshot_ids": deleted_snapshot_ids,
        "deletion_failures": deletion_failures,
        "snapshots": decisions,
    }
    if persist_state:
        write_json(paths.snapshot_retention_state_path, payload)
    return payload


def snapshot_retention_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Return the latest retention summary or a non-mutating preview when absent."""
    persisted = read_json(paths.snapshot_retention_state_path)
    if persisted:
        return persisted
    return _snapshot_retention_payload(
        paths,
        apply_deletions=False,
        persist_state=False,
    )


def apply_snapshot_retention(paths: WorkspacePaths) -> dict[str, Any]:
    """Apply the minimal snapshot retention policy and persist a decision record."""
    return _snapshot_retention_payload(
        paths,
        apply_deletions=True,
        persist_state=True,
    )


def storage_lifecycle_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Return a compact artifact-family lifecycle summary for local workspace storage."""
    retention_state = read_json(paths.snapshot_retention_state_path)
    if not retention_state:
        retention_state = snapshot_retention_summary(paths)

    def family(
        *,
        name: str,
        path: Path,
        truth_class: str,
        retention_unit: str,
        pin_sources: list[str],
        delete_trigger: str,
    ) -> dict[str, Any]:
        visible_files = list_visible_files(path)
        return {
            "name": name,
            "path": str(path.relative_to(paths.root)),
            "present": path.exists(),
            "visible_file_count": len(visible_files),
            "truth_class": truth_class,
            "retention_unit": retention_unit,
            "pin_sources": pin_sources,
            "delete_trigger": delete_trigger,
        }

    families = [
        family(
            name="snapshots",
            path=paths.knowledge_base_versions_dir,
            truth_class="immutable",
            retention_unit="snapshot",
            pin_sources=[
                "current",
                "active-run",
                "active-shared-job",
                "eval-baseline",
                "manual-pin",
            ],
            delete_trigger="snapshot-retention-policy",
        ),
        family(
            name="current-published",
            path=paths.knowledge_base_current_dir,
            truth_class="canonical",
            retention_unit="publish-root",
            pin_sources=["publish-pointer"],
            delete_trigger="next-publish-switch",
        ),
        family(
            name="staging",
            path=paths.knowledge_base_staging_dir,
            truth_class="rebuildable",
            retention_unit="staging-tree",
            pin_sources=[],
            delete_trigger="next-sync-or-manual-cleanup",
        ),
        family(
            name="answers",
            path=paths.answers_dir,
            truth_class="canonical",
            retention_unit="answer-file",
            pin_sources=[],
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="query-sessions",
            path=paths.query_sessions_dir,
            truth_class="derived",
            retention_unit="session-log",
            pin_sources=[],
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="retrieval-traces",
            path=paths.retrieval_traces_dir,
            truth_class="derived",
            retention_unit="trace-log",
            pin_sources=[],
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="review-artifacts",
            path=paths.review_logs_dir,
            truth_class="derived",
            retention_unit="review-artifact",
            pin_sources=[],
            delete_trigger="manual-runtime-cleanup",
        ),
        family(
            name="control-plane",
            path=paths.control_plane_dir,
            truth_class="canonical",
            retention_unit="job-or-state-record",
            pin_sources=["active-shared-job"],
            delete_trigger="settlement-or-manual-cleanup",
        ),
        family(
            name="interaction-ingest",
            path=paths.interaction_ingest_dir,
            truth_class="transient",
            retention_unit="ingest-entry",
            pin_sources=[],
            delete_trigger="promotion-or-manual-cleanup",
        ),
        family(
            name="agent-work",
            path=paths.agent_work_dir,
            truth_class="transient",
            retention_unit="work-artifact",
            pin_sources=[],
            delete_trigger="manual-cleanup",
        ),
        family(
            name="eval-artifacts",
            path=paths.eval_dir,
            truth_class="derived",
            retention_unit="eval-run-or-baseline",
            pin_sources=["eval-baseline"],
            delete_trigger="manual-cleanup",
        ),
    ]
    return {
        "family_count": len(families),
        "pinned_snapshot_count": retention_state.get("pinned_snapshot_count", 0),
        "eligible_delete_snapshot_ids": retention_state.get(
            "eligible_delete_snapshot_ids", []
        ),
        "families": families,
    }


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
        publish_manifest["published_source_signature"] = validation_report.get("source_signature")
        write_json(publish_manifest_path, publish_manifest)

        pointer_payload = {
            "snapshot_id": snapshot_id,
            "snapshot_path": str(snapshot_dir.relative_to(paths.root)),
            "published_at": published_at,
            "published_source_signature": validation_report.get("source_signature"),
        }
        write_json(paths.knowledge_base_dir / "current-pointer.json", pointer_payload)
        activate_snapshot(paths, snapshot_id)
        return publish_manifest

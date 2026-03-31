"""Shared control-plane state and shared-job helpers for DocMason."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .coordination import workspace_lease
from .project import WorkspacePaths, append_jsonl, read_json, write_json

WORKSPACE_STATE_SCHEMA_VERSION = 1
SHARED_JOB_SCHEMA_VERSION = 1
PROJECTION_STATE_SCHEMA_VERSION = 1
SHARED_JOB_ACTIVE_STATUSES = frozenset({"running", "awaiting-confirmation"})
SHARED_JOB_SETTLED_STATUSES = frozenset({"completed", "blocked", "declined"})
CONFIRMATION_KIND_PROMPTS = {
    "material-sync": (
        "A large unpublished workspace change set was detected. "
        "Build or refresh the knowledge base now before continuing this question?"
    ),
    "high-intrusion-prepare": (
        "This question requires additional local dependencies before it can continue safely. "
        "Prepare the workspace now?"
    ),
    "host-access-upgrade": (
        "This question needs a host-access upgrade before machine-level setup can continue. "
        "Upgrade host access now and then continue the same task?"
    ),
}
AFFIRMATIVE_CONFIRMATIONS = frozenset(
    {"y", "yes", "ok", "okay", "start", "continue", "是", "好", "开始", "继续", "确认"}
)
NEGATIVE_CONFIRMATIONS = frozenset(
    {"n", "no", "stop", "cancel", "decline", "否", "不", "不要", "取消"}
)
SHARED_JOB_STALE_AFTER = timedelta(minutes=10)


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _stable_json_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _hash_file_or_empty(path: Path) -> str:
    if not path.exists():
        return ""
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def shared_job_dir(paths: WorkspacePaths, job_id: str) -> Path:
    return paths.shared_jobs_dir / job_id


def shared_job_manifest_path(paths: WorkspacePaths, job_id: str) -> Path:
    return shared_job_dir(paths, job_id) / "manifest.json"


def shared_job_journal_path(paths: WorkspacePaths, job_id: str) -> Path:
    return shared_job_dir(paths, job_id) / "journal.jsonl"


def shared_job_result_path(paths: WorkspacePaths, job_id: str) -> Path:
    return shared_job_dir(paths, job_id) / "result.json"


def load_shared_jobs_index(paths: WorkspacePaths) -> dict[str, Any]:
    payload = read_json(paths.shared_jobs_index_path)
    active_by_key = payload.get("active_by_key")
    latest_settled_by_key = payload.get("latest_settled_by_key")
    return {
        "schema_version": int(payload.get("schema_version", SHARED_JOB_SCHEMA_VERSION) or 0),
        "updated_at": payload.get("updated_at"),
        "active_by_key": active_by_key if isinstance(active_by_key, dict) else {},
        "latest_settled_by_key": (
            latest_settled_by_key if isinstance(latest_settled_by_key, dict) else {}
        ),
    }


def _write_shared_jobs_index(
    paths: WorkspacePaths,
    *,
    active_by_key: dict[str, str],
    latest_settled_by_key: dict[str, str],
) -> dict[str, Any]:
    payload = {
        "schema_version": SHARED_JOB_SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "active_by_key": dict(active_by_key),
        "latest_settled_by_key": dict(latest_settled_by_key),
    }
    write_json(paths.shared_jobs_index_path, payload)
    return payload


def load_shared_job(paths: WorkspacePaths, job_id: str) -> dict[str, Any]:
    return read_json(shared_job_manifest_path(paths, job_id))


def load_shared_job_result(paths: WorkspacePaths, job_id: str) -> dict[str, Any]:
    return read_json(shared_job_result_path(paths, job_id))


def shared_job_is_settled(manifest: dict[str, Any]) -> bool:
    return str(manifest.get("status") or "") in SHARED_JOB_SETTLED_STATUSES


def shared_job_is_active(manifest: dict[str, Any]) -> bool:
    return str(manifest.get("status") or "") in SHARED_JOB_ACTIVE_STATUSES


def resolved_attached_shared_job_ids(
    *,
    turn: dict[str, Any],
    run_state: dict[str, Any] | None,
    hybrid_refresh_job_ids: list[str] | None = None,
) -> list[str]:
    """Return the canonical union of shared-job ids linked to the turn or run."""
    values = [
        *turn.get("attached_shared_job_ids", []),
        *((run_state or {}).get("attached_shared_job_ids", [])),
        *(hybrid_refresh_job_ids or turn.get("hybrid_refresh_job_ids", [])),
    ]
    return list(dict.fromkeys(item for item in values if isinstance(item, str) and item))


def _normalize_owner(owner: dict[str, Any] | None = None) -> dict[str, Any]:
    raw_owner = owner if isinstance(owner, dict) else {}
    kind = str(raw_owner.get("kind") or "command")
    owner_id = str(raw_owner.get("id") or "unknown-owner")
    normalized: dict[str, Any] = {"kind": kind, "id": owner_id}
    owner_pid = raw_owner.get("pid")
    if isinstance(owner_pid, int) and owner_pid > 0:
        normalized["pid"] = owner_pid
    return normalized


def _append_shared_job_event(
    paths: WorkspacePaths,
    *,
    job_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    append_jsonl(
        shared_job_journal_path(paths, job_id),
        {
            "recorded_at": _utc_now(),
            "job_id": job_id,
            "event_type": event_type,
            "payload": payload or {},
        },
    )


def pending_interaction_signature(paths: WorkspacePaths) -> str:
    return _stable_json_digest(
        {
            "overlay": _hash_file_or_empty(paths.interaction_overlay_manifest_path),
            "promotion_queue": _hash_file_or_empty(paths.interaction_promotion_queue_path),
        }
    )


def strong_source_fingerprint_signature(active_sources: list[dict[str, Any]]) -> str:
    descriptors: list[dict[str, str]] = []
    for source in active_sources:
        if not isinstance(source, dict):
            continue
        fingerprint = source.get("source_fingerprint")
        if not isinstance(fingerprint, str) or not fingerprint:
            continue
        current_path = str(source.get("current_path") or "")
        identity_basis = str(source.get("identity_basis") or "")
        change_classification = str(source.get("change_classification") or "")
        stable_source_id = ""
        raw_source_id = source.get("source_id")
        if (
            isinstance(raw_source_id, str)
            and raw_source_id
            and identity_basis in {"path", "fingerprint", "relocation-heuristic"}
            and change_classification not in {"added", "ambiguous"}
        ):
            stable_source_id = raw_source_id
        descriptors.append(
            {
                "stable_source_id": stable_source_id,
                "current_path": current_path,
                "fingerprint": fingerprint,
            }
        )
    descriptors.sort(
        key=lambda item: (
            item["stable_source_id"],
            item["current_path"],
            item["fingerprint"],
        )
    )
    return _stable_json_digest(descriptors)


def sync_input_signature(
    *,
    active_sources: list[dict[str, Any]],
    change_set: dict[str, Any],
    pending_interaction_signature_value: str,
    target: str = "current",
    mode: str = "default",
) -> str:
    stable_changes: list[dict[str, Any]] = []
    for change in change_set.get("changes", []):
        if not isinstance(change, dict):
            continue
        classification = str(change.get("change_classification") or "")
        current_path = str(change.get("current_path") or "")
        previous_path = str(change.get("previous_path") or "")
        source_fingerprint = str(change.get("source_fingerprint") or "")
        source_id = change.get("source_id")
        matched_source_ids = [
            value
            for value in change.get("matched_source_ids", [])
            if isinstance(value, str) and value
        ]
        if (
            classification in {"unchanged", "modified", "moved-or-renamed", "deleted"}
            and isinstance(source_id, str)
        ):
            stable_id = source_id
        elif classification == "ambiguous":
            stable_id = "|".join(sorted(matched_source_ids))
        else:
            stable_id = current_path
        stable_changes.append(
            {
                "stable_id": stable_id,
                "classification": classification,
                "current_path": current_path,
                "previous_path": previous_path,
                "source_fingerprint": source_fingerprint,
                "matched_source_ids": sorted(matched_source_ids),
            }
        )
    stable_changes.sort(
        key=lambda item: (
            item["stable_id"],
            item["classification"],
            item["current_path"],
            item["previous_path"],
            item["source_fingerprint"],
        )
    )
    return _stable_json_digest(
        {
            "target": target,
            "mode": mode,
            "strong_source_fingerprint_signature": strong_source_fingerprint_signature(
                active_sources
            ),
            "stable_changes": stable_changes,
            "pending_interaction_signature": pending_interaction_signature_value,
        }
    )


def classify_sync_materiality(
    *,
    change_set: dict[str, Any],
    active_source_count: int,
    published_present: bool,
) -> dict[str, Any]:
    stats = change_set.get("stats", {})
    if not isinstance(stats, dict):
        stats = {}
    changed_total = sum(
        int(stats.get(key, 0) or 0)
        for key in ("added", "modified", "moved_or_renamed", "deleted", "ambiguous")
    )
    changed_ratio = (
        (changed_total / active_source_count)
        if active_source_count > 0
        else 0.0
    )
    destructive_total = sum(
        int(stats.get(key, 0) or 0) for key in ("deleted", "moved_or_renamed", "ambiguous")
    )
    reasons: list[str] = []
    if changed_total >= 12:
        reasons.append(f"changed_total={changed_total} >= 12")
    if changed_ratio >= 0.15:
        reasons.append(f"changed_ratio={changed_ratio:.3f} >= 0.15")
    if destructive_total >= 3:
        reasons.append(
            "deleted + moved_or_renamed + ambiguous "
            f"= {destructive_total} >= 3"
        )
    if not published_present and active_source_count >= 12:
        reasons.append(f"first_publish active_source_count={active_source_count} >= 12")
    return {
        "changed_total": changed_total,
        "changed_ratio": changed_ratio,
        "materiality": "material" if reasons else "minor",
        "materiality_reasons": reasons,
    }


def required_prepare_capabilities(
    paths: WorkspacePaths,
    *,
    editable_install: bool | None = None,
    editable_detail: str | None = None,
    office_snapshot: dict[str, Any] | None = None,
    machine_baseline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from .commands import inspect_editable_install
    from .workspace_probe import office_renderer_snapshot

    if editable_install is None or editable_detail is None:
        editable_install, editable_detail = inspect_editable_install(paths)
    if office_snapshot is None:
        office_snapshot = office_renderer_snapshot(paths)
    if machine_baseline is None:
        machine_baseline = {
            "applicable": False,
            "ready": True,
            "status": "not-applicable",
            "host_access_required": False,
            "detail": "",
        }
    required_capabilities: list[str] = []
    intrusion_class = "repo-local"
    reasons: list[str] = []
    confirmation_kind: str | None = None
    if not editable_install:
        required_capabilities.append("editable-install")
        reasons.append(str(editable_detail))
    if bool(machine_baseline.get("applicable")) and not bool(machine_baseline.get("ready")):
        required_capabilities.append("machine-baseline")
        intrusion_class = "high-intrusion"
        reasons.append(str(machine_baseline.get("detail") or "Machine baseline is unavailable."))
        if bool(machine_baseline.get("host_access_required")):
            intrusion_class = "host-access-upgrade"
            confirmation_kind = "host-access-upgrade"
    elif office_snapshot.get("required") and not office_snapshot.get("ready"):
        required_capabilities.append("office-rendering")
        intrusion_class = "high-intrusion"
        reasons.append(str(office_snapshot.get("detail") or "Office rendering is unavailable."))
        confirmation_kind = "high-intrusion-prepare"
    return {
        "required_capabilities": sorted(required_capabilities),
        "required_capability_signature": _stable_json_digest(sorted(required_capabilities)),
        "intrusion_class": intrusion_class,
        "high_intrusion_required": intrusion_class in {"high-intrusion", "host-access-upgrade"},
        "confirmation_kind": confirmation_kind,
        "host_access_upgrade_required": intrusion_class == "host-access-upgrade",
        "reasons": reasons,
    }


def shared_job_control_plane_payload(
    manifest: dict[str, Any],
    *,
    next_command: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    attached_run_ids = manifest.get("attached_run_ids", [])
    if not isinstance(attached_run_ids, list):
        attached_run_ids = []
    payload = {
        "state": state or manifest.get("status"),
        "shared_job_id": manifest.get("job_id"),
        "shared_job_key": manifest.get("job_key"),
        "job_family": manifest.get("job_family"),
        "confirmation_kind": manifest.get("confirmation_kind"),
        "confirmation_prompt": manifest.get("confirmation_prompt"),
        "confirmation_reason": manifest.get("confirmation_reason"),
        "attached_run_count": len([value for value in attached_run_ids if isinstance(value, str)]),
    }
    if isinstance(next_command, str) and next_command:
        payload["next_command"] = next_command
    return payload


def lane_c_job_key(*, published_snapshot_id: str, source_id: str) -> str:
    """Return the canonical shared-job key for one Lane C source under one snapshot."""
    return f"lane-c:{published_snapshot_id}:{source_id}"


def lane_b_job_key(*, staging_source_signature: str, target: str = "staging") -> str:
    """Return the canonical shared-job key for one staging-scoped Lane B batch."""
    return f"lane-b:{target}:{staging_source_signature}"


def _create_shared_job(
    paths: WorkspacePaths,
    *,
    job_key: str,
    job_family: str,
    criticality: str,
    scope: dict[str, Any],
    input_signature: str,
    owner: dict[str, Any],
    run_id: str | None = None,
    requires_confirmation: bool = False,
    confirmation_kind: str | None = None,
    confirmation_prompt: str | None = None,
    confirmation_reason: str | None = None,
) -> dict[str, Any]:
    now = _utc_now()
    job_id = str(uuid.uuid4())
    attached_run_ids = [run_id] if isinstance(run_id, str) and run_id else []
    manifest = {
        "schema_version": SHARED_JOB_SCHEMA_VERSION,
        "job_id": job_id,
        "job_key": job_key,
        "job_family": job_family,
        "criticality": criticality,
        "status": "awaiting-confirmation" if requires_confirmation else "running",
        "scope": scope,
        "input_signature": input_signature,
        "owner": _normalize_owner(owner),
        "attached_run_ids": attached_run_ids,
        "requires_confirmation": requires_confirmation,
        "confirmation_kind": confirmation_kind,
        "confirmation_prompt": confirmation_prompt,
        "confirmation_reason": confirmation_reason,
        "attempt_count": 1,
        "created_at": now,
        "updated_at": now,
    }
    write_json(shared_job_manifest_path(paths, job_id), manifest)
    _append_shared_job_event(
        paths,
        job_id=job_id,
        event_type="job-created",
        payload={
            "job_key": job_key,
            "status": manifest["status"],
            "requires_confirmation": requires_confirmation,
        },
    )
    return manifest


def _shared_job_stale(manifest: dict[str, Any]) -> bool:
    if not shared_job_is_active(manifest):
        return False
    owner = manifest.get("owner", {})
    owner_pid = owner.get("pid") if isinstance(owner, dict) else None
    if isinstance(owner_pid, int) and owner_pid > 0:
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        except OSError:
            return True
    updated_at = _parse_timestamp(manifest.get("updated_at"))
    if updated_at is None:
        return True
    return datetime.now(tz=UTC) - updated_at > SHARED_JOB_STALE_AFTER


def ensure_shared_job(
    paths: WorkspacePaths,
    *,
    job_key: str,
    job_family: str,
    criticality: str,
    scope: dict[str, Any],
    input_signature: str,
    owner: dict[str, Any],
    run_id: str | None = None,
    requires_confirmation: bool = False,
    confirmation_kind: str | None = None,
    confirmation_prompt: str | None = None,
    confirmation_reason: str | None = None,
) -> dict[str, Any]:
    resource = f"shared-job:{job_key}"
    normalized_owner = _normalize_owner(owner)
    with workspace_lease(paths, resource, timeout_seconds=30.0):
        index = load_shared_jobs_index(paths)
        active_by_key = dict(index["active_by_key"])
        latest_settled_by_key = dict(index["latest_settled_by_key"])
        active_job_id = active_by_key.get(job_key)
        if isinstance(active_job_id, str) and active_job_id:
            manifest = load_shared_job(paths, active_job_id)
            if manifest and manifest.get("input_signature") == input_signature:
                caller_role = "waiter"
                if _shared_job_stale(manifest):
                    manifest["owner"] = normalized_owner
                    manifest["attempt_count"] = int(manifest.get("attempt_count", 1) or 1) + 1
                    manifest["updated_at"] = _utc_now()
                    write_json(shared_job_manifest_path(paths, active_job_id), manifest)
                    _append_shared_job_event(
                        paths,
                        job_id=active_job_id,
                        event_type="owner-restarted",
                        payload={"owner": normalized_owner},
                    )
                    caller_role = "owner"
                elif manifest.get("status") == "awaiting-confirmation":
                    caller_role = "awaiting-confirmation"
                elif manifest.get("owner") == normalized_owner:
                    caller_role = "owner"
                if isinstance(run_id, str) and run_id:
                    manifest = _attach_run_to_shared_job_unlocked(
                        paths,
                        manifest=manifest,
                        run_id=run_id,
                    )
                return {"manifest": manifest, "created": False, "caller_role": caller_role}
            if manifest and shared_job_is_settled(manifest):
                active_by_key.pop(job_key, None)
        manifest = _create_shared_job(
            paths,
            job_key=job_key,
            job_family=job_family,
            criticality=criticality,
            scope=scope,
            input_signature=input_signature,
            owner=normalized_owner,
            run_id=run_id,
            requires_confirmation=requires_confirmation,
            confirmation_kind=confirmation_kind,
            confirmation_prompt=confirmation_prompt,
            confirmation_reason=confirmation_reason,
        )
        active_by_key[job_key] = str(manifest["job_id"])
        _write_shared_jobs_index(
            paths,
            active_by_key=active_by_key,
            latest_settled_by_key=latest_settled_by_key,
        )
        if isinstance(run_id, str) and run_id:
            _ensure_run_attachment(paths, run_id=run_id, job_id=str(manifest["job_id"]))
        return {"manifest": manifest, "created": True, "caller_role": "owner"}


def _ensure_run_attachment(paths: WorkspacePaths, *, run_id: str, job_id: str) -> None:
    """Mirror shared-job attachment into run state when the run exists."""
    try:
        from .run_control import attach_shared_job_to_run

        attach_shared_job_to_run(paths, run_id=run_id, job_id=job_id)
    except FileNotFoundError:
        return


def _attach_run_to_shared_job_unlocked(
    paths: WorkspacePaths,
    *,
    manifest: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    """Attach a run while the caller already owns the shared-job lease."""
    job_id = str(manifest["job_id"])
    attached_run_ids = manifest.get("attached_run_ids", [])
    if not isinstance(attached_run_ids, list):
        attached_run_ids = []
    if run_id not in attached_run_ids:
        attached_run_ids.append(run_id)
        manifest["attached_run_ids"] = attached_run_ids
        manifest["updated_at"] = _utc_now()
        write_json(shared_job_manifest_path(paths, job_id), manifest)
        _append_shared_job_event(
            paths,
            job_id=job_id,
            event_type="run-attached",
            payload={"run_id": run_id},
        )
    _ensure_run_attachment(paths, run_id=run_id, job_id=job_id)
    return manifest


def attach_run_to_shared_job(paths: WorkspacePaths, *, job_id: str, run_id: str) -> dict[str, Any]:
    """Attach one run to the shared-job manifest and mirror it into run state."""
    manifest = load_shared_job(paths, job_id)
    if not manifest:
        raise FileNotFoundError(shared_job_manifest_path(paths, job_id))
    job_key = str(manifest["job_key"])
    with workspace_lease(paths, f"shared-job:{job_key}", timeout_seconds=30.0):
        manifest = load_shared_job(paths, job_id)
        return _attach_run_to_shared_job_unlocked(paths, manifest=manifest, run_id=run_id)


def _settle_shared_job(
    paths: WorkspacePaths,
    *,
    job_id: str,
    status: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in SHARED_JOB_SETTLED_STATUSES:
        raise ValueError(f"Unsupported settled shared-job status `{status}`.")
    manifest = load_shared_job(paths, job_id)
    if not manifest:
        raise FileNotFoundError(shared_job_manifest_path(paths, job_id))
    job_key = str(manifest["job_key"])
    with workspace_lease(paths, f"shared-job:{job_key}", timeout_seconds=30.0):
        manifest = load_shared_job(paths, job_id)
        existing_result = load_shared_job_result(paths, job_id)
        incoming_result = result or {}
        if shared_job_is_settled(manifest):
            existing_status = str(manifest.get("status") or "")
            existing_result_digest = _stable_json_digest(existing_result.get("result", {}))
            incoming_result_digest = _stable_json_digest(incoming_result)
            if existing_status == status and existing_result_digest == incoming_result_digest:
                return manifest
            raise ValueError(
                f"Shared job `{job_id}` is already settled as `{existing_status}` "
                "and may not be mutated."
            )
        manifest["status"] = status
        manifest["updated_at"] = _utc_now()
        write_json(shared_job_manifest_path(paths, job_id), manifest)
        write_json(
            shared_job_result_path(paths, job_id),
            {
                "job_id": job_id,
                "job_key": job_key,
                "status": status,
                "recorded_at": manifest["updated_at"],
                "result": incoming_result,
            },
        )
        index = load_shared_jobs_index(paths)
        active_by_key = dict(index["active_by_key"])
        latest_settled_by_key = dict(index["latest_settled_by_key"])
        active_by_key.pop(job_key, None)
        latest_settled_by_key[job_key] = job_id
        _write_shared_jobs_index(
            paths,
            active_by_key=active_by_key,
            latest_settled_by_key=latest_settled_by_key,
        )
        _append_shared_job_event(
            paths,
            job_id=job_id,
            event_type="job-settled",
            payload={"status": status},
        )
        return manifest


def approve_shared_job(
    paths: WorkspacePaths,
    job_id: str,
    *,
    owner: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    manifest = load_shared_job(paths, job_id)
    if not manifest:
        raise FileNotFoundError(shared_job_manifest_path(paths, job_id))
    job_key = str(manifest["job_key"])
    with workspace_lease(paths, f"shared-job:{job_key}", timeout_seconds=30.0):
        manifest = load_shared_job(paths, job_id)
        if manifest.get("status") != "awaiting-confirmation":
            return manifest
        manifest["status"] = "running"
        if owner is not None:
            manifest["owner"] = _normalize_owner(owner)
        if isinstance(run_id, str) and run_id:
            attached_run_ids = manifest.get("attached_run_ids", [])
            if not isinstance(attached_run_ids, list):
                attached_run_ids = []
            if run_id not in attached_run_ids:
                attached_run_ids.append(run_id)
            manifest["attached_run_ids"] = attached_run_ids
        manifest["updated_at"] = _utc_now()
        write_json(shared_job_manifest_path(paths, job_id), manifest)
        _append_shared_job_event(paths, job_id=job_id, event_type="job-approved")
        if isinstance(run_id, str) and run_id:
            _ensure_run_attachment(paths, run_id=run_id, job_id=job_id)
        return manifest


def decline_shared_job(
    paths: WorkspacePaths,
    job_id: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    return _settle_shared_job(
        paths,
        job_id=job_id,
        status="declined",
        result={"reason": reason or "The confirmation-required shared job was declined."},
    )


def complete_shared_job(
    paths: WorkspacePaths,
    job_id: str,
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _settle_shared_job(paths, job_id=job_id, status="completed", result=result)


def block_shared_job(
    paths: WorkspacePaths,
    job_id: str,
    *,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _settle_shared_job(paths, job_id=job_id, status="blocked", result=result)


def normalize_confirmation_reply(text: str) -> str | None:
    normalized = "".join(
        character.lower()
        for character in text.strip()
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )
    if normalized in AFFIRMATIVE_CONFIRMATIONS:
        return "approve"
    if normalized in NEGATIVE_CONFIRMATIONS:
        return "decline"
    return None


def find_conversation_confirmation_job(
    paths: WorkspacePaths,
    conversation_id: str,
) -> dict[str, Any]:
    conversation = read_json(paths.conversations_dir / f"{conversation_id}.json")
    turns = conversation.get("turns", [])
    if not isinstance(turns, list):
        return {}
    pending_turns: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        if turn.get("turn_state") != "awaiting-confirmation":
            continue
        attached = turn.get("attached_shared_job_ids", [])
        if not isinstance(attached, list) or len(
            [value for value in attached if isinstance(value, str)]
        ) != 1:
            continue
        pending_turns.append(turn)
    if len(pending_turns) != 1:
        return {}
    turn = pending_turns[0]
    job_id = next(
        value
        for value in turn.get("attached_shared_job_ids", [])
        if isinstance(value, str) and value
    )
    manifest = load_shared_job(paths, job_id)
    if not manifest or manifest.get("status") != "awaiting-confirmation":
        return {}
    return {
        "conversation_id": conversation_id,
        "turn": turn,
        "job_id": job_id,
        "manifest": manifest,
    }


def repair_stale_shared_jobs(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """Settle or clear active shared jobs whose owners no longer exist legally."""
    from .run_control import load_run_state

    repairs: list[dict[str, Any]] = []
    index = load_shared_jobs_index(paths)
    for job_key, job_id in list(index["active_by_key"].items()):
        if not isinstance(job_id, str) or not job_id:
            continue
        manifest = load_shared_job(paths, job_id)
        if not manifest:
            active_by_key = dict(load_shared_jobs_index(paths)["active_by_key"])
            latest_settled_by_key = dict(load_shared_jobs_index(paths)["latest_settled_by_key"])
            active_by_key.pop(job_key, None)
            _write_shared_jobs_index(
                paths,
                active_by_key=active_by_key,
                latest_settled_by_key=latest_settled_by_key,
            )
            repairs.append(
                {
                    "kind": "dropped-missing-shared-job",
                    "job_id": job_id,
                    "job_key": job_key,
                }
            )
            continue
        if not shared_job_is_active(manifest):
            active_by_key = dict(load_shared_jobs_index(paths)["active_by_key"])
            latest_settled_by_key = dict(load_shared_jobs_index(paths)["latest_settled_by_key"])
            active_by_key.pop(job_key, None)
            _write_shared_jobs_index(
                paths,
                active_by_key=active_by_key,
                latest_settled_by_key=latest_settled_by_key,
            )
            repairs.append(
                {
                    "kind": "cleared-nonactive-shared-job",
                    "job_id": job_id,
                    "job_key": job_key,
                }
            )
            continue
        owner = manifest.get("owner", {})
        owner_pid = owner.get("pid") if isinstance(owner, dict) else None
        if isinstance(owner_pid, int) and owner_pid > 0 and _shared_job_stale(manifest):
            block_shared_job(
                paths,
                job_id,
                result={"reason": "The shared job owner process is no longer active."},
            )
            repairs.append(
                {
                    "kind": "blocked-inactive-owner-process",
                    "job_id": job_id,
                    "job_key": job_key,
                    "owner_kind": owner.get("kind") if isinstance(owner, dict) else None,
                    "owner_id": owner.get("id") if isinstance(owner, dict) else None,
                    "owner_pid": owner_pid,
                }
            )
            continue
        if manifest.get("criticality") != "answer-critical":
            continue
        if not isinstance(owner, dict) or owner.get("kind") != "run":
            continue
        owner_run_id = owner.get("id")
        if not isinstance(owner_run_id, str) or not owner_run_id:
            continue
        run_state = load_run_state(paths, owner_run_id)
        if not run_state:
            block_shared_job(
                paths,
                job_id,
                result={"reason": "The shared job owner run no longer exists."},
            )
            repairs.append(
                {
                    "kind": "blocked-missing-owner-run",
                    "job_id": job_id,
                    "job_key": job_key,
                    "owner_run_id": owner_run_id,
                }
            )
            continue
        if run_state.get("status") != "active":
            block_shared_job(
                paths,
                job_id,
                result={"reason": "The shared job owner run is no longer active."},
            )
            repairs.append(
                {
                    "kind": "blocked-inactive-owner-run",
                    "job_id": job_id,
                    "job_key": job_key,
                    "owner_run_id": owner_run_id,
                    "run_status": run_state.get("status"),
                }
            )
    return repairs


def workspace_state_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    from .interaction import interaction_ingest_snapshot
    from .project import cached_bootstrap_readiness, knowledge_base_snapshot
    from .run_control import repair_stale_active_runs
    from .workspace_probe import preview_source_changes

    repair_actions = repair_stale_shared_jobs(paths)
    repair_actions.extend(repair_stale_active_runs(paths))
    environment_state = cached_bootstrap_readiness(paths, require_sync_capability=False)
    sync_environment_state = cached_bootstrap_readiness(paths, require_sync_capability=True)
    ready = bool(environment_state.get("ready"))
    sync_ready = bool(sync_environment_state.get("ready"))
    kb_snapshot = knowledge_base_snapshot(paths)
    publish_manifest = read_json(paths.current_publish_manifest_path)
    _index_payload, active_sources, _ambiguous_match, change_set = preview_source_changes(paths)
    interaction_snapshot = interaction_ingest_snapshot(paths)
    interaction_signature = pending_interaction_signature(paths)
    materiality = classify_sync_materiality(
        change_set=change_set,
        active_source_count=len(active_sources),
        published_present=bool(kb_snapshot.get("present")),
    )
    jobs_index = load_shared_jobs_index(paths)
    active_jobs: list[dict[str, Any]] = []
    for job_id in jobs_index["active_by_key"].values():
        if not isinstance(job_id, str) or not job_id:
            continue
        manifest = load_shared_job(paths, job_id)
        if not manifest:
            continue
        active_jobs.append(
            {
                "job_id": job_id,
                "job_key": manifest.get("job_key"),
                "job_family": manifest.get("job_family"),
                "status": manifest.get("status"),
                "criticality": manifest.get("criticality"),
                "requires_confirmation": manifest.get("requires_confirmation"),
                "confirmation_kind": manifest.get("confirmation_kind"),
            }
        )
    next_legal_actions: list[str] = []
    host_access_upgrade_pending = False
    for job in active_jobs:
        if job.get("status") != "awaiting-confirmation":
            continue
        if job.get("job_family") == "prepare":
            if job.get("confirmation_kind") == "host-access-upgrade":
                host_access_upgrade_pending = True
                next_legal_actions.append("switch-host-to-full-access")
            else:
                next_legal_actions.append("prepare --yes")
        elif job.get("job_family") == "sync":
            next_legal_actions.append("sync --yes")
    if not ready and not host_access_upgrade_pending:
        next_legal_actions.append("prepare")
    if ready and (not kb_snapshot.get("present") or kb_snapshot.get("stale")):
        next_legal_actions.append("sync")
    payload = {
        "schema_version": WORKSPACE_STATE_SCHEMA_VERSION,
        "updated_at": _utc_now(),
        "workspace_root": str(paths.root),
        "environment": {
            "ready": bool(ready),
            "sync_capable": bool(sync_ready),
            "bootstrap_reason": environment_state.get("reason"),
            "capability_gaps": (
                []
                if sync_ready
                else [str(sync_environment_state.get("reason") or "workspace-not-sync-capable")]
            ),
        },
        "published_state": {
            "present": bool(kb_snapshot.get("present")),
            "stale": bool(kb_snapshot.get("stale")),
            "validation_status": kb_snapshot.get("validation_status"),
            "snapshot_id": publish_manifest.get("snapshot_id"),
            "published_source_signature": kb_snapshot.get("published_source_signature"),
            "staging_present": bool(kb_snapshot.get("staging_present")),
        },
        "corpus_delta": {
            "source_signature": change_set.get("source_signature"),
            "strong_source_fingerprint_signature": strong_source_fingerprint_signature(
                active_sources
            ),
            "stats": change_set.get("stats", {}),
            "changed_source_count": materiality["changed_total"],
            "changed_ratio": materiality["changed_ratio"],
            "materiality": materiality["materiality"],
            "materiality_reasons": materiality["materiality_reasons"],
        },
        "interaction_state": {
            "pending_promotion_count": interaction_snapshot.get("pending_promotion_count"),
            "sync_recommended": interaction_snapshot.get("sync_recommended"),
            "load_warnings": interaction_snapshot.get("load_warnings", []),
            "pending_interaction_signature": interaction_signature,
        },
        "active_answer_critical_jobs": [
            job for job in active_jobs if job.get("criticality") == "answer-critical"
        ],
        "next_legal_actions": list(dict.fromkeys(next_legal_actions)),
        "repair_actions": repair_actions,
    }
    write_json(paths.workspace_state_path, payload)
    return payload


def workspace_state_ref(paths: WorkspacePaths, *, force_refresh: bool = False) -> dict[str, Any]:
    snapshot = (
        workspace_state_snapshot(paths)
        if force_refresh
        else read_json(paths.workspace_state_path)
    )
    if not snapshot:
        snapshot = workspace_state_snapshot(paths)
    return {
        "path": str(paths.workspace_state_path.relative_to(paths.root)),
        "updated_at": snapshot.get("updated_at"),
        "digest": _stable_json_digest(snapshot),
        "snapshot_excerpt": {
            "environment": snapshot.get("environment"),
            "published_state": snapshot.get("published_state"),
            "corpus_delta": snapshot.get("corpus_delta"),
            "interaction_state": snapshot.get("interaction_state"),
            "active_answer_critical_jobs": snapshot.get("active_answer_critical_jobs"),
            "next_legal_actions": snapshot.get("next_legal_actions", []),
            "repair_actions": snapshot.get("repair_actions", []),
        },
    }


def projection_inputs_digest(paths: WorkspacePaths) -> str:
    run_commits: list[dict[str, Any]] = []
    for path in sorted(paths.runs_dir.glob("*/commit.json")):
        payload = read_json(path)
        if payload:
            run_commits.append(
                {
                    "run_id": payload.get("run_id"),
                    "turn_id": payload.get("turn_id"),
                    "conversation_id": payload.get("conversation_id"),
                    "committed_at": payload.get("committed_at"),
                    "answer_state": payload.get("answer_state"),
                    "support_basis": payload.get("support_basis"),
                }
            )
    settled_jobs: list[dict[str, Any]] = []
    for path in sorted(paths.shared_jobs_dir.glob("*/result.json")):
        payload = read_json(path)
        if payload:
            settled_jobs.append(
                {
                    "job_id": payload.get("job_id"),
                    "job_key": payload.get("job_key"),
                    "status": payload.get("status"),
                    "recorded_at": payload.get("recorded_at"),
                }
            )
    return _stable_json_digest({"run_commits": run_commits, "settled_jobs": settled_jobs})

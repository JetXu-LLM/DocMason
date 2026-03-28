"""Projection scheduling and refresh helpers for review-facing runtime artifacts."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from .control_plane import (
    block_shared_job,
    complete_shared_job,
    ensure_shared_job,
    load_shared_job,
    shared_job_is_active,
)
from .coordination import workspace_lease
from .project import WorkspacePaths, read_json, write_json

PROJECTION_STATE_SCHEMA_VERSION = 2


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _projection_target_digest(change_sequence: int) -> str:
    return hashlib.sha256(f"projection-sequence:{change_sequence}".encode()).hexdigest()


def _projection_state_defaults() -> dict[str, Any]:
    return {
        "schema_version": PROJECTION_STATE_SCHEMA_VERSION,
        "updated_at": None,
        "dirty": False,
        "change_sequence": 0,
        "target_digest": None,
        "settled_sequence": 0,
        "settled_digest": None,
        "active_job_id": None,
        "last_success_at": None,
        "last_failure_at": None,
        "last_failure_reason": None,
    }


def load_projection_state(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the projection scheduler state with compatibility backfill."""
    payload = read_json(paths.projection_state_path)
    if not payload:
        return _projection_state_defaults()
    if int(payload.get("schema_version", 0) or 0) >= PROJECTION_STATE_SCHEMA_VERSION:
        state = _projection_state_defaults()
        state.update(payload)
        state["schema_version"] = PROJECTION_STATE_SCHEMA_VERSION
        state["dirty"] = bool(state.get("dirty"))
        state["change_sequence"] = int(state.get("change_sequence", 0) or 0)
        state["settled_sequence"] = int(state.get("settled_sequence", 0) or 0)
        return state
    legacy_digest = payload.get("projection_inputs_digest")
    updated_at = payload.get("updated_at")
    if isinstance(legacy_digest, str) and legacy_digest:
        return {
            "schema_version": PROJECTION_STATE_SCHEMA_VERSION,
            "updated_at": updated_at,
            "dirty": False,
            "change_sequence": 1,
            "target_digest": legacy_digest,
            "settled_sequence": 1,
            "settled_digest": legacy_digest,
            "active_job_id": None,
            "last_success_at": updated_at,
            "last_failure_at": None,
            "last_failure_reason": None,
        }
    return _projection_state_defaults()


def projection_outputs_exist(paths: WorkspacePaths) -> bool:
    """Return whether the derived projection files currently exist on disk."""
    required_paths = (
        paths.review_summary_path,
        paths.benchmark_candidates_path,
        paths.answer_history_index_path,
    )
    return all(path.exists() for path in required_paths)


def projection_state_is_fresh(state: dict[str, Any]) -> bool:
    """Return whether projection state is fully settled for the current target."""
    return (
        not bool(state.get("dirty"))
        and int(state.get("change_sequence", 0) or 0)
        == int(state.get("settled_sequence", 0) or 0)
        and state.get("target_digest") == state.get("settled_digest")
    )


def projection_state_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Return a compact projection scheduler summary for command payloads."""
    state = load_projection_state(paths)
    return {
        "schema_version": state.get("schema_version"),
        "dirty": bool(state.get("dirty")),
        "change_sequence": int(state.get("change_sequence", 0) or 0),
        "target_digest": state.get("target_digest"),
        "settled_sequence": int(state.get("settled_sequence", 0) or 0),
        "settled_digest": state.get("settled_digest"),
        "active_job_id": state.get("active_job_id"),
        "last_success_at": state.get("last_success_at"),
        "last_failure_at": state.get("last_failure_at"),
        "last_failure_reason": state.get("last_failure_reason"),
        "outputs_present": projection_outputs_exist(paths),
        "fresh": projection_state_is_fresh(state) and projection_outputs_exist(paths),
    }


def _write_projection_state(paths: WorkspacePaths, state: dict[str, Any]) -> dict[str, Any]:
    payload = dict(_projection_state_defaults())
    payload.update(state)
    payload["schema_version"] = PROJECTION_STATE_SCHEMA_VERSION
    payload["updated_at"] = payload.get("updated_at") or _utc_now()
    write_json(paths.projection_state_path, payload)
    return payload


def _projection_job_owner(owner_id: str) -> dict[str, Any]:
    return {"kind": "projection-worker", "id": owner_id, "pid": os.getpid()}


def _active_projection_manifest(paths: WorkspacePaths, state: dict[str, Any]) -> dict[str, Any]:
    job_id = state.get("active_job_id")
    if not isinstance(job_id, str) or not job_id:
        return {}
    return load_shared_job(paths, job_id)


def _ensure_projection_job_for_target(
    paths: WorkspacePaths,
    *,
    change_sequence: int,
    target_digest: str,
    owner_id: str,
) -> dict[str, Any]:
    job_info = ensure_shared_job(
        paths,
        job_key=f"projection:{target_digest}",
        job_family="projection-refresh",
        criticality="derived-state",
        scope={
            "change_sequence": change_sequence,
            "target_digest": target_digest,
        },
        input_signature=target_digest,
        owner=_projection_job_owner(owner_id),
    )
    manifest = job_info.get("manifest")
    return manifest if isinstance(manifest, dict) else {}


def _background_projection_spawn_allowed() -> bool:
    if os.environ.get("DOCMASON_DISABLE_BACKGROUND_PROJECTIONS") == "1":
        return False
    if os.environ.get("DOCMASON_PROJECTION_WORKER") == "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False
    return True


def _spawn_projection_worker(paths: WorkspacePaths) -> bool:
    if not _background_projection_spawn_allowed():
        return False
    python_bin = paths.venv_python if paths.venv_python.exists() else None
    executable = str(python_bin or sys.executable)
    env = os.environ.copy()
    env["DOCMASON_PROJECTION_WORKER"] = "1"
    subprocess.Popen(
        [executable, "-m", "docmason._projection_worker", str(paths.root)],
        cwd=str(paths.root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=env,
    )
    return True


def queue_projection_refresh(
    paths: WorkspacePaths,
    *,
    reason: str,
    spawn_worker: bool = True,
) -> dict[str, Any]:
    """Mark derived projections dirty and ensure one deduplicated shared job exists."""
    with workspace_lease(paths, "projection-state", timeout_seconds=30.0):
        state = load_projection_state(paths)
        next_sequence = max(
            int(state.get("change_sequence", 0) or 0),
            int(state.get("settled_sequence", 0) or 0),
        ) + 1
        target_digest = _projection_target_digest(next_sequence)
        active_manifest = _active_projection_manifest(paths, state)
        created_new_job = False
        if not active_manifest or not shared_job_is_active(active_manifest):
            active_manifest = _ensure_projection_job_for_target(
                paths,
                change_sequence=next_sequence,
                target_digest=target_digest,
                owner_id=f"queue-{next_sequence}",
            )
            created_new_job = True
        state.update(
            {
                "dirty": True,
                "change_sequence": next_sequence,
                "target_digest": target_digest,
                "active_job_id": active_manifest.get("job_id"),
                "updated_at": _utc_now(),
            }
        )
        state = _write_projection_state(paths, state)
    spawned = False
    if created_new_job and spawn_worker:
        spawned = _spawn_projection_worker(paths)
    return {
        "reason": reason,
        "spawned": spawned,
        "shared_job_id": state.get("active_job_id"),
        "projection_state": projection_state_summary(paths),
    }


def refresh_conversation_projections(paths: WorkspacePaths) -> None:
    """Refresh projection-only conversation views from canonical conversation state."""
    paths.conversation_projections_dir.mkdir(parents=True, exist_ok=True)
    live_files = {path.name for path in paths.conversations_dir.glob("*.json") if path.is_file()}
    for path in sorted(paths.conversations_dir.glob("*.json")):
        payload = read_json(path)
        if not payload:
            continue
        write_json(paths.conversation_projections_dir / path.name, payload)
    for path in sorted(paths.conversation_projections_dir.glob("*.json")):
        if path.name not in live_files:
            os.remove(path)


def _rebuild_runtime_projections(paths: WorkspacePaths) -> dict[str, Any]:
    from .review import build_answer_history_index, build_benchmark_candidates, build_review_summary

    refresh_conversation_projections(paths)
    summary = build_review_summary(paths)
    write_json(paths.review_summary_path, summary)
    write_json(paths.benchmark_candidates_path, build_benchmark_candidates(paths, summary=summary))
    write_json(paths.answer_history_index_path, build_answer_history_index(paths))
    return summary


def _mark_projection_failure(
    paths: WorkspacePaths,
    *,
    job_id: str,
    error_text: str,
) -> dict[str, Any]:
    block_shared_job(
        paths,
        job_id,
        result={"reason": error_text},
    )
    with workspace_lease(paths, "projection-state", timeout_seconds=30.0):
        state = load_projection_state(paths)
        if state.get("active_job_id") == job_id:
            state["active_job_id"] = None
        state["last_failure_at"] = _utc_now()
        state["last_failure_reason"] = error_text
        state["updated_at"] = _utc_now()
        state = _write_projection_state(paths, state)
    return state


def run_projection_refresh_worker(
    paths: WorkspacePaths,
    *,
    trigger: str = "manual-refresh",
) -> dict[str, Any]:
    """Refresh global derived projections until the current target is settled."""
    refreshed = False
    last_summary: dict[str, Any] = {}
    with workspace_lease(paths, "projection-refresh", timeout_seconds=300.0):
        while True:
            state = load_projection_state(paths)
            if projection_state_is_fresh(state) and projection_outputs_exist(paths):
                return {
                    "status": "ready",
                    "refreshed": refreshed,
                    "projection_state": projection_state_summary(paths),
                    "summary": last_summary or read_json(paths.review_summary_path),
                }
            if not bool(state.get("dirty")) and not projection_outputs_exist(paths):
                queued = queue_projection_refresh(
                    paths,
                    reason="Projection outputs were missing during an explicit refresh request.",
                    spawn_worker=False,
                )
                state = queued["projection_state"]
            current_manifest = _active_projection_manifest(paths, state)
            if not current_manifest or not shared_job_is_active(current_manifest):
                current_manifest = _ensure_projection_job_for_target(
                    paths,
                    change_sequence=int(state.get("change_sequence", 0) or 0),
                    target_digest=str(state.get("target_digest") or ""),
                    owner_id=f"{trigger}:{os.getpid()}",
                )
                with workspace_lease(paths, "projection-state", timeout_seconds=30.0):
                    latest_state = load_projection_state(paths)
                    latest_state["active_job_id"] = current_manifest.get("job_id")
                    latest_state["updated_at"] = _utc_now()
                    _write_projection_state(paths, latest_state)
            job_id = str(current_manifest["job_id"])
            job_scope = current_manifest.get("scope", {})
            processing_sequence = int(
                job_scope.get("change_sequence")
                or state.get("change_sequence", 0)
                or 0
            )
            processing_digest = str(
                job_scope.get("target_digest")
                or state.get("target_digest")
                or ""
            )
            try:
                last_summary = _rebuild_runtime_projections(paths)
            except Exception as exc:  # pragma: no cover - exercised via higher-level tests
                _mark_projection_failure(paths, job_id=job_id, error_text=str(exc))
                raise
            refreshed = True
            complete_shared_job(
                paths,
                job_id,
                result={
                    "change_sequence": processing_sequence,
                    "target_digest": processing_digest,
                    "trigger": trigger,
                },
            )
            with workspace_lease(paths, "projection-state", timeout_seconds=30.0):
                state = load_projection_state(paths)
                state["settled_sequence"] = max(
                    int(state.get("settled_sequence", 0) or 0),
                    processing_sequence,
                )
                state["settled_digest"] = (
                    processing_digest
                    if int(state.get("settled_sequence", 0) or 0) == processing_sequence
                    else state.get("settled_digest")
                )
                if state.get("active_job_id") == job_id:
                    state["active_job_id"] = None
                state["last_success_at"] = _utc_now()
                state["last_failure_at"] = None
                state["last_failure_reason"] = None
                state["dirty"] = not (
                    int(state.get("change_sequence", 0) or 0)
                    == int(state.get("settled_sequence", 0) or 0)
                    and state.get("target_digest") == state.get("settled_digest")
                )
                if bool(state.get("dirty")) and not state.get("active_job_id"):
                    next_manifest = _ensure_projection_job_for_target(
                        paths,
                        change_sequence=int(state.get("change_sequence", 0) or 0),
                        target_digest=str(state.get("target_digest") or ""),
                        owner_id=f"{trigger}:{os.getpid()}",
                    )
                    state["active_job_id"] = next_manifest.get("job_id")
                state["updated_at"] = _utc_now()
                _write_projection_state(paths, state)


def ensure_runtime_projections_fresh(
    paths: WorkspacePaths,
    *,
    consumer: str,
) -> dict[str, Any]:
    """Ensure review-facing projections are fresh for an explicit read-side consumer."""
    state = load_projection_state(paths)
    if projection_state_is_fresh(state) and projection_outputs_exist(paths):
        return {
            "status": "ready",
            "refreshed": False,
            "projection_state": projection_state_summary(paths),
            "summary": read_json(paths.review_summary_path),
        }
    return run_projection_refresh_worker(paths, trigger=f"read:{consumer}")


def load_answer_history_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Return the settled answer-history snapshot for ordinary ask warm-start only."""
    state = load_projection_state(paths)
    if not projection_state_is_fresh(state):
        return {}
    return read_json(paths.answer_history_index_path)


def refresh_runtime_projections(paths: WorkspacePaths) -> dict[str, Any]:
    """Explicitly refresh derived projections through the governed worker path."""
    result = run_projection_refresh_worker(paths, trigger="explicit-refresh")
    summary = result.get("summary")
    return summary if isinstance(summary, dict) else {}

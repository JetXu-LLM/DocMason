"""Answer-admissibility checks ahead of the commit barrier."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .control_plane import (
    lane_c_job_key,
    load_shared_job,
    resolved_attached_shared_job_ids,
    shared_job_is_settled,
)
from .conversation import load_turn_record
from .project import WorkspacePaths, read_json
from .run_control import load_run_state

ILLEGAL_WORK_AREA_MARKERS = (
    "knowledge_base/staging/",
    "knowledge_base/.staging-build/",
    "original_doc/",
)


def _latest_trace_payload(paths: WorkspacePaths, trace_ids: list[str]) -> dict[str, Any]:
    for trace_id in reversed(trace_ids):
        payload = read_json(paths.retrieval_traces_dir / f"{trace_id}.json")
        if payload:
            return payload
    return {}


def _latest_session_payload(paths: WorkspacePaths, session_ids: list[str]) -> dict[str, Any]:
    for session_id in reversed(session_ids):
        payload = read_json(paths.query_sessions_dir / f"{session_id}.json")
        if payload:
            return payload
    return {}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _latest_settled_job_timestamp(paths: WorkspacePaths, job_ids: list[str]) -> datetime | None:
    latest: datetime | None = None
    for job_id in job_ids:
        if not isinstance(job_id, str) or not job_id:
            continue
        result_payload = read_json(paths.shared_jobs_dir / job_id / "result.json")
        manifest_payload = load_shared_job(paths, job_id)
        candidate = _parse_timestamp(result_payload.get("recorded_at")) or _parse_timestamp(
            manifest_payload.get("updated_at") if isinstance(manifest_payload, dict) else None
        )
        if candidate is None:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest


def _payload_has_unresolved_gap(payload: dict[str, Any]) -> bool:
    return bool(
        payload
        and (
            payload.get("published_artifacts_sufficient") is False
            or payload.get("source_escalation_required") is True
        )
    )


def _payload_recorded_at(payload: dict[str, Any]) -> datetime | None:
    return _parse_timestamp(payload.get("recorded_at"))


def _payload_identity_issue(
    payload: dict[str, Any],
    *,
    label: str,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
) -> str | None:
    payload_conversation_id = payload.get("conversation_id")
    if (
        isinstance(payload_conversation_id, str)
        and payload_conversation_id
        and payload_conversation_id != conversation_id
    ):
        return f"Linked {label} belongs to a different conversation."
    payload_turn_id = payload.get("turn_id")
    if isinstance(payload_turn_id, str) and payload_turn_id and payload_turn_id != turn_id:
        return f"Linked {label} belongs to a different turn."
    if isinstance(run_id, str) and run_id:
        payload_run_id = payload.get("run_id")
        if isinstance(payload_run_id, str) and payload_run_id and payload_run_id != run_id:
            return f"Linked {label} belongs to a different run."
    return None

def evaluate_commit_admissibility(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
    turn_snapshot: dict[str, Any] | None = None,
    answer_file_path: str | None,
    answer_state: str | None,
    support_basis: str | None,
    support_manifest_path: str | None,
    trace_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Evaluate whether a turn may legally cross the commit barrier."""
    turn = (
        dict(turn_snapshot)
        if isinstance(turn_snapshot, dict)
        else load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    )
    issues: list[str] = []
    effective_run_id = str(run_id or turn.get("active_run_id") or "")
    run_state = load_run_state(paths, effective_run_id) if effective_run_id else {}
    if not effective_run_id or str(turn.get("active_run_id") or "") != effective_run_id:
        issues.append("The current run no longer owns the turn.")
    run_version_context = run_state.get("version_context")
    if not isinstance(run_version_context, dict):
        run_version_context = turn.get("version_context")
    hybrid_refresh_triggered = bool(turn.get("hybrid_refresh_triggered"))
    hybrid_refresh_snapshot_id = (
        str(turn.get("hybrid_refresh_snapshot_id"))
        if isinstance(turn.get("hybrid_refresh_snapshot_id"), str)
        and turn.get("hybrid_refresh_snapshot_id")
        else None
    )
    hybrid_refresh_job_ids = [
        item
        for item in turn.get("hybrid_refresh_job_ids", [])
        if isinstance(item, str) and item
    ]
    hybrid_refresh_sources = [
        item
        for item in turn.get("hybrid_refresh_sources", [])
        if isinstance(item, str) and item
    ]
    hybrid_refresh_completion_status = str(turn.get("hybrid_refresh_completion_status") or "")
    attached_shared_job_ids = resolved_attached_shared_job_ids(
        turn=turn,
        run_state=run_state,
        hybrid_refresh_job_ids=hybrid_refresh_job_ids,
    )
    for job_id in attached_shared_job_ids:
        if not isinstance(job_id, str) or not job_id:
            continue
        manifest = load_shared_job(paths, job_id)
        if not manifest:
            issues.append(f"Attached shared job `{job_id}` is missing.")
            continue
        if not shared_job_is_settled(manifest):
            issues.append(f"Attached shared job `{job_id}` is not yet settled.")
        if manifest.get("job_family") == "lane-c":
            scope = manifest.get("scope")
            scope_payload = scope if isinstance(scope, dict) else {}
            if hybrid_refresh_job_ids and job_id not in hybrid_refresh_job_ids:
                issues.append(
                    f"Turn truth omits governed multimodal refresh shared job `{job_id}`."
                )
            manifest_snapshot_id = (
                str(scope_payload.get("published_snapshot_id"))
                if isinstance(scope_payload.get("published_snapshot_id"), str)
                and scope_payload.get("published_snapshot_id")
                else None
            )
            if hybrid_refresh_snapshot_id and manifest_snapshot_id:
                if manifest_snapshot_id != hybrid_refresh_snapshot_id:
                    issues.append(
                        "Governed multimodal refresh snapshot truth does not match "
                        "the shared job manifest."
                    )
            manifest_source_id = (
                str(scope_payload.get("source_id"))
                if isinstance(scope_payload.get("source_id"), str)
                and scope_payload.get("source_id")
                else None
            )
            if hybrid_refresh_sources and manifest_source_id:
                if manifest_source_id not in hybrid_refresh_sources:
                    issues.append(
                        "Governed multimodal refresh source truth does not match "
                        "the shared job manifest."
                    )
        elif job_id in hybrid_refresh_job_ids:
            issues.append(
                f"Turn truth marks shared job `{job_id}` as governed multimodal "
                "refresh, but the manifest is not lane-c."
            )
    effective_trace_ids = (
        trace_ids
        if isinstance(trace_ids, list)
        else [value for value in turn.get("trace_ids", []) if isinstance(value, str) and value]
    )
    effective_session_ids = [
        value for value in turn.get("session_ids", []) if isinstance(value, str) and value
    ]
    trace_payload = _latest_trace_payload(paths, effective_trace_ids)
    session_payload = _latest_session_payload(paths, effective_session_ids)
    trace_version_context = trace_payload.get("version_context")
    session_identity_issue = _payload_identity_issue(
        session_payload,
        label="query session",
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=effective_run_id or None,
    )
    if session_identity_issue:
        issues.append(session_identity_issue)
    trace_identity_issue = _payload_identity_issue(
        trace_payload,
        label="trace",
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=effective_run_id or None,
    )
    if trace_identity_issue:
        issues.append(trace_identity_issue)
    trace_has_unresolved_gap = _payload_has_unresolved_gap(trace_payload)
    session_has_unresolved_gap = _payload_has_unresolved_gap(session_payload)
    trace_recorded_at = _payload_recorded_at(trace_payload)
    session_recorded_at = _payload_recorded_at(session_payload)
    latest_gap_source: str | None = None
    if session_has_unresolved_gap:
        latest_gap_source = "query session"
    if trace_payload:
        if (
            session_recorded_at is None
            or trace_recorded_at is None
            or trace_recorded_at >= session_recorded_at
        ):
            latest_gap_source = "trace" if trace_has_unresolved_gap else None
    if latest_gap_source and turn.get("published_artifacts_sufficient") is True:
        issues.append(
            "Final turn claims published artifacts are sufficient even though the "
            f"latest ask-owned {latest_gap_source} still records an unresolved "
            "hard-artifact or governed multimodal gap."
        )
    if latest_gap_source and turn.get("source_escalation_required") is False:
        issues.append(
            "Final turn clears source escalation even though the latest ask-owned "
            f"{latest_gap_source} still requires escalation."
        )
    if support_basis in {"kb-grounded", "mixed"}:
        if not effective_trace_ids or not trace_payload:
            issues.append("Canonical grounded ask commits require an ask-owned trace.")
        elif not isinstance(trace_version_context, dict):
            issues.append("KB-grounded commits require trace version truth.")
    if (
        trace_payload
        and isinstance(run_version_context, dict)
        and isinstance(trace_version_context, dict)
    ):
        if trace_version_context.get("published_snapshot_id") != run_version_context.get(
            "published_snapshot_id"
        ):
            issues.append("Trace snapshot context does not match the run version context.")
        if trace_version_context.get("published_source_signature") != run_version_context.get(
            "published_source_signature"
        ):
            issues.append("Trace source signature does not match the run version context.")
    if hybrid_refresh_triggered and hybrid_refresh_completion_status not in {"covered", "blocked"}:
        issues.append(
            "The governed multimodal refresh was triggered but did not settle "
            "to covered or blocked."
        )
    if hybrid_refresh_triggered and not effective_session_ids:
        issues.append(
            "The governed multimodal refresh settled without a post-refresh retrieve session."
        )
    if hybrid_refresh_triggered and not effective_trace_ids:
        issues.append("The governed multimodal refresh settled without a post-refresh trace.")
    if hybrid_refresh_triggered and not hybrid_refresh_snapshot_id:
        issues.append(
            "The governed multimodal refresh turn state is missing the settled snapshot id."
        )
    if hybrid_refresh_triggered and not hybrid_refresh_job_ids:
        issues.append(
            "The governed multimodal refresh turn state is missing the settled shared job id."
        )
    if hybrid_refresh_triggered and hybrid_refresh_snapshot_id and hybrid_refresh_sources:
        expected_job_keys = {
            lane_c_job_key(
                published_snapshot_id=hybrid_refresh_snapshot_id,
                source_id=source_id,
            )
            for source_id in hybrid_refresh_sources
        }
        matched_expected_job = False
        for job_id in hybrid_refresh_job_ids:
            manifest = load_shared_job(paths, job_id)
            if not manifest:
                issues.append(f"Governed multimodal refresh shared job `{job_id}` is missing.")
                continue
            if not shared_job_is_settled(manifest):
                issues.append(
                    f"Governed multimodal refresh shared job `{job_id}` is not yet settled."
                )
            if manifest.get("job_family") != "lane-c":
                issues.append(f"Shared job `{job_id}` is not a governed multimodal refresh job.")
            if manifest.get("job_key") in expected_job_keys:
                matched_expected_job = True
        if not matched_expected_job:
            issues.append(
                "The settled governed multimodal refresh jobs do not match "
                "the selected snapshot/source scope."
            )
    settled_refresh_at = (
        _latest_settled_job_timestamp(paths, hybrid_refresh_job_ids)
        if hybrid_refresh_completion_status == "covered"
        else None
    )
    if hybrid_refresh_completion_status == "covered" and settled_refresh_at is not None:
        latest_session_at = _parse_timestamp(session_payload.get("recorded_at"))
        latest_trace_at = _parse_timestamp(trace_payload.get("recorded_at"))
        if latest_session_at is None or latest_session_at <= settled_refresh_at:
            issues.append(
                "Covered governed multimodal refresh turns require a post-refresh "
                "retrieve session recorded after the shared job settled."
            )
        if latest_trace_at is None or latest_trace_at <= settled_refresh_at:
            issues.append(
                "Covered governed multimodal refresh turns require a post-refresh "
                "trace recorded after the shared job settled."
            )
    if hybrid_refresh_completion_status == "blocked" and support_basis != "governed-boundary":
        issues.append("Blocked governed multimodal refresh turns must commit as governed-boundary.")
    if isinstance(answer_file_path, str) and answer_file_path:
        answer_path = Path(answer_file_path)
        if not answer_path.is_absolute():
            answer_path = paths.root / answer_path
        if answer_path.exists():
            answer_text = answer_path.read_text(encoding="utf-8")
            if support_basis != "governed-boundary":
                for marker in ILLEGAL_WORK_AREA_MARKERS:
                    if marker in answer_text:
                        issues.append(
                            f"Canonical answer text references work-area path `{marker}`."
                        )
                        break
    if support_basis in {"external-source-verified", "mixed"}:
        if not isinstance(support_manifest_path, str) or not support_manifest_path:
            issues.append(f"support_basis `{support_basis}` requires a support manifest.")
    if answer_state == "abstained" and support_basis == "governed-boundary":
        pass
    allowed = not issues
    return {
        "allowed": allowed,
        "reason": None if allowed else issues[0],
        "issues": issues,
        "run_id": effective_run_id or None,
    }

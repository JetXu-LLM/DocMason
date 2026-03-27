"""Answer-admissibility checks ahead of the commit barrier."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .control_plane import lane_c_job_key, load_shared_job, shared_job_is_settled
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
    run_state = (
        load_run_state(paths, effective_run_id)
        if effective_run_id
        else {}
    )
    if not effective_run_id or str(turn.get("active_run_id") or "") != effective_run_id:
        issues.append("The current run no longer owns the turn.")
    run_version_context = run_state.get("version_context")
    if not isinstance(run_version_context, dict):
        run_version_context = turn.get("version_context")
    for job_id in turn.get("attached_shared_job_ids", []):
        if not isinstance(job_id, str) or not job_id:
            continue
        manifest = load_shared_job(paths, job_id)
        if not manifest or not shared_job_is_settled(manifest):
            issues.append(f"Attached shared job `{job_id}` is not yet settled.")
    effective_trace_ids = (
        trace_ids
        if isinstance(trace_ids, list)
        else [
            value
            for value in turn.get("trace_ids", [])
            if isinstance(value, str) and value
        ]
    )
    effective_session_ids = [
        value
        for value in turn.get("session_ids", [])
        if isinstance(value, str) and value
    ]
    trace_payload = _latest_trace_payload(paths, effective_trace_ids)
    trace_version_context = trace_payload.get("version_context")
    if support_basis in {"kb-grounded", "mixed"} and trace_payload and not isinstance(
        trace_version_context, dict
    ):
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
    hybrid_refresh_triggered = bool(turn.get("hybrid_refresh_triggered"))
    hybrid_refresh_snapshot_id = (
        str(turn.get("hybrid_refresh_snapshot_id"))
        if isinstance(turn.get("hybrid_refresh_snapshot_id"), str)
        and turn.get("hybrid_refresh_snapshot_id")
        else None
    )
    hybrid_refresh_job_ids = [
        value
        for value in turn.get("hybrid_refresh_job_ids", [])
        if isinstance(value, str) and value
    ]
    hybrid_refresh_sources = [
        value
        for value in turn.get("hybrid_refresh_sources", [])
        if isinstance(value, str) and value
    ]
    hybrid_refresh_completion_status = str(turn.get("hybrid_refresh_completion_status") or "")
    if hybrid_refresh_triggered and hybrid_refresh_completion_status not in {"covered", "blocked"}:
        issues.append(
            "The governed multimodal refresh was triggered but did not settle to covered or blocked."
        )
    if hybrid_refresh_triggered and not effective_session_ids:
        issues.append(
            "The governed multimodal refresh settled without a post-refresh retrieve session."
        )
    if hybrid_refresh_triggered and not effective_trace_ids:
        issues.append("The governed multimodal refresh settled without a post-refresh trace.")
    if hybrid_refresh_triggered and not hybrid_refresh_snapshot_id:
        issues.append("The governed multimodal refresh turn state is missing the settled snapshot id.")
    if hybrid_refresh_triggered and not hybrid_refresh_job_ids:
        issues.append("The governed multimodal refresh turn state is missing the settled shared job id.")
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
                "The settled governed multimodal refresh jobs do not match the selected snapshot/source scope."
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

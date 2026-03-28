"""Natural-intent routing helpers for the user-facing `ask` workflow."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .admissibility import evaluate_commit_admissibility
from .commands import ACTION_REQUIRED, prepare_workspace
from .commands import sync_workspace as run_sync_command
from .control_plane import (
    approve_shared_job,
    attach_run_to_shared_job,
    block_shared_job,
    complete_shared_job,
    decline_shared_job,
    ensure_shared_job,
    find_conversation_confirmation_job,
    lane_c_job_key,
    load_shared_job,
    normalize_confirmation_reply,
    resolved_attached_shared_job_ids,
    shared_job_is_settled,
)
from .conversation import (
    FRONT_DOOR_STATE_CANONICAL_ASK,
    LOG_ORIGIN_INTERACTIVE_ASK,
    build_log_context,
    current_host_identity,
    load_bound_conversation_record_for_host,
    load_turn_record,
    normalize_front_door_state,
    normalize_log_origin,
    open_conversation_turn,
    semantic_log_context_fields,
    semantic_log_context_from_record,
    update_conversation_turn,
    utc_now,
)
from .front_controller import (
    question_execution_profile,
    write_external_support_manifest,
    write_hybrid_refresh_work,
)
from .interaction import (
    interaction_ingest_snapshot,
    interaction_overlay_relevance,
    maybe_reconcile_active_thread,
)
from .project import (
    WorkspacePaths,
    cached_bootstrap_readiness,
    knowledge_base_snapshot,
    manual_workspace_recovery_doc,
    read_json,
    write_json,
)
from .projections import refresh_runtime_projections
from .routing import tokenize_text
from .run_control import (
    RUN_ORIGIN_ASK_FRONT_DOOR,
    attach_shared_job_to_run,
    commit_run,
    ensure_run_for_turn,
    load_run_state,
    record_run_event,
    record_run_event_for_runs,
    record_run_event_if_present,
    refresh_turn_run_version_truth,
    update_run_state,
)
from .source_references import (
    build_reference_resolution_summary,
    resolve_workspace_reference,
)


def _workspace_notices_enabled(question_domain: str) -> bool:
    return question_domain in {"workspace-corpus", "composition"}


def _latest_trace_record(paths: WorkspacePaths, trace_ids: list[str] | None) -> dict[str, Any]:
    if not isinstance(trace_ids, list):
        return {}
    for trace_id in reversed(trace_ids):
        if not isinstance(trace_id, str) or not trace_id:
            continue
        payload = read_json(paths.retrieval_traces_dir / f"{trace_id}.json")
        if payload:
            return payload
    return {}


def _resolve_scalar(
    explicit: Any,
    trace_payload: dict[str, Any],
    current_turn: dict[str, Any],
    field_name: str,
) -> Any:
    if explicit is not None:
        return explicit
    if field_name in trace_payload and trace_payload[field_name] is not None:
        return trace_payload[field_name]
    return current_turn.get(field_name)


def _resolve_list(
    explicit: list[Any] | None,
    trace_payload: dict[str, Any],
    current_turn: dict[str, Any],
    field_name: str,
) -> list[Any]:
    if explicit is not None:
        return explicit
    trace_value = trace_payload.get(field_name)
    if isinstance(trace_value, list):
        return trace_value
    current_value = current_turn.get(field_name)
    if isinstance(current_value, list):
        return current_value
    return []


def _resolve_mapping(
    explicit: dict[str, Any] | None,
    trace_payload: dict[str, Any],
    current_turn: dict[str, Any],
    field_name: str,
) -> dict[str, Any] | None:
    if isinstance(explicit, dict):
        return explicit
    trace_value = trace_payload.get(field_name)
    if isinstance(trace_value, dict):
        return trace_value
    current_value = current_turn.get(field_name)
    if isinstance(current_value, dict):
        return current_value
    return None


def _resolved_string_list(value: list[Any] | None) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item for item in value if isinstance(item, str) and item)
    )


def _normalized_artifact_path(paths: WorkspacePaths, value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = paths.root / path
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return str(path)


def _effective_ask_log_origin(
    explicit: Any,
    *,
    turn: dict[str, Any] | None = None,
    run_state: dict[str, Any] | None = None,
) -> str:
    return (
        normalize_log_origin(explicit)
        or normalize_log_origin((turn or {}).get("log_origin"))
        or normalize_log_origin((run_state or {}).get("log_origin"))
        or LOG_ORIGIN_INTERACTIVE_ASK
    )


def _log_payload_matches_turn(
    paths: WorkspacePaths,
    payload: dict[str, Any],
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
    inner_workflow_id: str,
    answer_file_path: str | None = None,
) -> bool:
    if payload.get("conversation_id") != conversation_id or payload.get("turn_id") != turn_id:
        return False
    if isinstance(run_id, str) and run_id:
        payload_run_id = payload.get("run_id")
        if isinstance(payload_run_id, str) and payload_run_id and payload_run_id != run_id:
            return False
    if payload.get("entry_workflow_id") != "ask":
        return False
    payload_inner_workflow = payload.get("inner_workflow_id")
    if (
        isinstance(payload_inner_workflow, str)
        and payload_inner_workflow
        and payload_inner_workflow != inner_workflow_id
    ):
        return False
    payload_front_door_state = normalize_front_door_state(payload.get("front_door_state"))
    if payload_front_door_state and payload_front_door_state != FRONT_DOOR_STATE_CANONICAL_ASK:
        return False
    expected_answer_file = _normalized_artifact_path(paths, answer_file_path)
    payload_answer_file = _normalized_artifact_path(paths, payload.get("answer_file_path"))
    if expected_answer_file and payload_answer_file and payload_answer_file != expected_answer_file:
        return False
    return True


def _discover_unique_turn_log_artifacts(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
    inner_workflow_id: str,
    answer_file_path: str | None,
) -> tuple[list[str], list[str]]:
    session_candidates: list[tuple[str, str]] = []
    trace_candidates: list[tuple[str, str]] = []
    for path in sorted(paths.query_sessions_dir.glob("*.json")):
        payload = read_json(path)
        if not payload or not _log_payload_matches_turn(
            paths,
            payload,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            inner_workflow_id=inner_workflow_id,
            answer_file_path=answer_file_path,
        ):
            continue
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_candidates.append((str(payload.get("recorded_at") or ""), session_id))
    for path in sorted(paths.retrieval_traces_dir.glob("*.json")):
        payload = read_json(path)
        if not payload or not _log_payload_matches_turn(
            paths,
            payload,
            conversation_id=conversation_id,
            turn_id=turn_id,
            run_id=run_id,
            inner_workflow_id=inner_workflow_id,
            answer_file_path=answer_file_path,
        ):
            continue
        trace_id = payload.get("trace_id")
        if isinstance(trace_id, str) and trace_id:
            trace_candidates.append((str(payload.get("recorded_at") or ""), trace_id))
    ordered_sessions = [
        session_id for _recorded_at, session_id in sorted(session_candidates)
    ]
    ordered_traces = [
        trace_id for _recorded_at, trace_id in sorted(trace_candidates)
    ]
    return ordered_sessions, ordered_traces


def _resolved_log_artifact_ids(
    *,
    explicit_ids: list[str] | None,
    current_ids: Any,
    discovered_ids: list[str],
) -> list[str]:
    if explicit_ids is not None:
        return _resolved_string_list(explicit_ids)
    existing_ids = _resolved_string_list(current_ids if isinstance(current_ids, list) else None)
    if existing_ids:
        return existing_ids
    if len(discovered_ids) == 1:
        return discovered_ids
    return []


def _preview_source_changes(
    paths: WorkspacePaths,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, dict[str, Any]]:
    """Load source-change preview lazily so ask imports stay light."""
    from .workspace_probe import preview_source_changes

    return preview_source_changes(paths)


def _sync_turn_log_artifacts(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
    session_ids: list[str],
    trace_ids: list[str],
    inner_workflow_id: str,
    native_turn_id: str | None,
    front_door_state: str | None = None,
    semantic_log_context: dict[str, str] | None = None,
    answer_file_path: str | None = None,
    answer_state: str | None = None,
    render_inspection_required: bool | None = None,
    inspection_scope: str | None = None,
    preferred_channels: list[str] | None = None,
    used_published_channels: list[str] | None = None,
    published_artifacts_sufficient: bool | None = None,
    reference_resolution: dict[str, Any] | None = None,
    reference_resolution_summary: str | None = None,
    source_escalation_required: bool | None = None,
    source_escalation_reason: str | None = None,
    auto_sync_triggered: bool | None = None,
    auto_sync_reason: str | None = None,
    auto_sync_summary: dict[str, Any] | None = None,
    log_origin: str | None = None,
    hybrid_refresh_triggered: bool | None = None,
    hybrid_refresh_sources: list[str] | None = None,
    hybrid_refresh_completion_status: str | None = None,
    hybrid_refresh_summary: dict[str, Any] | None = None,
) -> None:
    update_fields = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "entry_workflow_id": "ask",
        "inner_workflow_id": inner_workflow_id,
        "native_turn_id": native_turn_id,
        "front_door_state": normalize_front_door_state(front_door_state),
        "answer_file_path": answer_file_path,
        "answer_state": answer_state,
        "render_inspection_required": render_inspection_required,
        "inspection_scope": inspection_scope,
        "preferred_channels": preferred_channels or [],
        "used_published_channels": used_published_channels or [],
        "published_artifacts_sufficient": published_artifacts_sufficient,
        "reference_resolution": reference_resolution,
        "reference_resolution_summary": reference_resolution_summary,
        "source_escalation_required": source_escalation_required,
        "source_escalation_reason": source_escalation_reason,
        "auto_sync_triggered": auto_sync_triggered,
        "auto_sync_reason": auto_sync_reason,
        "auto_sync_summary": auto_sync_summary,
        "log_origin": normalize_log_origin(log_origin),
        "hybrid_refresh_triggered": hybrid_refresh_triggered,
        "hybrid_refresh_sources": hybrid_refresh_sources or [],
        "hybrid_refresh_completion_status": hybrid_refresh_completion_status,
        "hybrid_refresh_summary": hybrid_refresh_summary,
    }
    if semantic_log_context:
        update_fields.update(semantic_log_context)
    for session_id in session_ids:
        session_path = paths.query_sessions_dir / f"{session_id}.json"
        payload = read_json(session_path)
        if not payload:
            continue
        payload.update({key: value for key, value in update_fields.items() if value is not None})
        write_json(session_path, payload)
    for trace_id in trace_ids:
        trace_path = paths.retrieval_traces_dir / f"{trace_id}.json"
        payload = read_json(trace_path)
        if not payload:
            continue
        payload.update({key: value for key, value in update_fields.items() if value is not None})
        write_json(trace_path, payload)


def _changed_source_relevance(
    *,
    question: str,
    change_set: dict[str, Any],
    reference_resolution: dict[str, Any] | None,
    needs_latest_workspace_state: bool,
) -> tuple[bool, str]:
    changed_sources = [
        change
        for change in change_set.get("changes", [])
        if isinstance(change, dict) and change.get("change_classification") != "unchanged"
    ]
    if not changed_sources:
        return False, "No current source drift was detected."

    changed_source_ids = {
        str(change.get("source_id"))
        for change in changed_sources
        if isinstance(change.get("source_id"), str)
    }
    resolved_source_id = (
        str(reference_resolution.get("resolved_source_id"))
        if isinstance(reference_resolution, dict)
        and isinstance(reference_resolution.get("resolved_source_id"), str)
        else None
    )
    source_match_status = (
        str(reference_resolution.get("source_match_status") or "none")
        if isinstance(reference_resolution, dict)
        else "none"
    )
    if resolved_source_id and source_match_status in {"exact", "approximate"}:
        if resolved_source_id in changed_source_ids:
            return True, "The resolved source reference points to a changed source."
        if source_match_status == "exact":
            return False, "The resolved source reference points to an unchanged source."

    if needs_latest_workspace_state:
        return True, "The semantic analysis explicitly requires the latest workspace state."

    question_tokens = set(tokenize_text(question))
    for change in changed_sources:
        current_path = str(change.get("current_path") or "")
        previous_path = str(change.get("previous_path") or "")
        searchable = set(tokenize_text(f"{current_path} {previous_path}"))
        if question_tokens & searchable:
            return True, "Changed source paths overlap lexically with the current question."

    return True, "Change relevance is uncertain, so the ask path is biasing to sync."


def _auto_sync_summary(sync_result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": sync_result.get("sync_status") or sync_result.get("status"),
        "command_status": sync_result.get("status"),
        "detail": sync_result.get("detail"),
        "published": bool(sync_result.get("published")),
        "change_stats": dict(sync_result.get("change_set", {}).get("stats", {})),
        "repair_count": int(sync_result.get("auto_repairs", {}).get("repair_count", 0) or 0),
        "authored_count": int(
            sync_result.get("auto_authoring", {}).get("authored_count", 0) or 0
        ),
        "steps": [
            {
                "step": step.get("step"),
                "status": step.get("status"),
                "detail": step.get("detail"),
            }
            for step in sync_result.get("autonomous_steps", [])
            if isinstance(step, dict)
        ],
    }


def _auto_prepare_summary(report: Any) -> dict[str, Any]:
    payload = getattr(report, "payload", {}) or {}
    raw_environment = payload.get("environment")
    environment: dict[str, Any] = raw_environment if isinstance(raw_environment, dict) else {}
    return {
        "status": payload.get("status"),
        "actions_performed": list(payload.get("actions_performed", [])),
        "actions_skipped": list(payload.get("actions_skipped", [])),
        "next_steps": list(payload.get("next_steps", [])),
        "control_plane": dict(payload.get("control_plane", {}))
        if isinstance(payload.get("control_plane"), dict)
        else {},
        "package_manager": environment.get("package_manager"),
        "manual_recovery_doc": (
            payload.get("manual_recovery_doc")
            or environment.get("manual_recovery_doc")
            or manual_workspace_recovery_doc()
        ),
    }


def _prepare_with_optional_owner(
    paths: WorkspacePaths,
    *,
    assume_yes: bool,
    interactive: bool,
    run_id: str | None = None,
) -> Any:
    owner = {"kind": "run", "id": run_id} if isinstance(run_id, str) and run_id else None
    try:
        return prepare_workspace(
            paths,
            assume_yes=assume_yes,
            interactive=interactive,
            owner=owner,
            run_id=run_id,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return prepare_workspace(paths, assume_yes=assume_yes, interactive=interactive)


def _sync_with_optional_owner(
    paths: WorkspacePaths,
    *,
    assume_yes: bool,
    run_id: str | None = None,
) -> Any:
    owner = {"kind": "run", "id": run_id} if isinstance(run_id, str) and run_id else None
    try:
        return run_sync_command(
            paths,
            assume_yes=assume_yes,
            owner=owner,
            run_id=run_id,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return run_sync_command(paths, assume_yes=assume_yes)


def _ensure_workspace_environment(
    paths: WorkspacePaths,
    *,
    require_sync_capability: bool = False,
    run_id: str | None = None,
) -> tuple[bool, dict[str, Any], bool, str | None, dict[str, Any] | None]:
    """Use the cached bootstrap marker first, then silently repair the workspace when needed."""
    cached = cached_bootstrap_readiness(paths, require_sync_capability=require_sync_capability)
    if cached["ready"]:
        return True, cached, False, None, None

    reason = str(cached.get("detail") or "The cached bootstrap marker is missing or invalid.")
    report = _prepare_with_optional_owner(
        paths,
        assume_yes=False,
        interactive=False,
        run_id=run_id,
    )
    summary = _auto_prepare_summary(report)
    refreshed = cached_bootstrap_readiness(paths, require_sync_capability=require_sync_capability)
    return bool(refreshed["ready"]), refreshed, True, reason, summary


def _commit_governed_boundary_turn(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    reason: str,
    extra_turn_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    run_id = (
        str(turn.get("active_run_id"))
        if isinstance(turn.get("active_run_id"), str) and turn.get("active_run_id")
        else None
    )
    answer_file_path = str(turn.get("answer_file_path") or "")
    answer_path = paths.root / answer_file_path
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(reason.strip() + "\n", encoding="utf-8")
    updated = commit_run(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        status="completed",
        answer_state="abstained",
        support_basis="governed-boundary",
        support_manifest_path=None,
        answer_file_path=answer_file_path,
        response_excerpt=reason.strip(),
        admissibility_gate_result={
            "allowed": True,
            "reason": None,
            "issues": [],
            "run_id": turn.get("active_run_id"),
        },
        turn_updates={
            **(extra_turn_updates or {}),
            "status": "completed",
            "turn_state": "completed",
            "answer_state": "abstained",
            "support_basis": "governed-boundary",
            "response_excerpt": reason.strip(),
        },
    )
    refresh_runtime_projections(paths)
    record_run_event_if_present(
        paths,
        run_id=run_id,
        stage="projection",
        event_type="projection-refreshed",
        payload={"conversation_id": conversation_id, "turn_id": turn_id},
    )
    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        **updated,
    }


def _apply_control_plane_pause(
    paths: WorkspacePaths,
    *,
    run_id: str,
    control_plane: dict[str, Any],
    attached_shared_job_ids: list[str],
    control_plane_pause_state: str | None,
    confirmation_kind: str | None,
    confirmation_prompt: str | None,
    confirmation_reason: str | None,
) -> tuple[list[str], str | None, str | None, str | None, str | None]:
    """Apply one control-plane pause payload onto the current ask turn state."""
    if control_plane.get("state") not in {"awaiting-confirmation", "waiting-shared-job"}:
        return (
            attached_shared_job_ids,
            control_plane_pause_state,
            confirmation_kind,
            confirmation_prompt,
            confirmation_reason,
        )

    pause_state = str(control_plane["state"])
    job_id = (
        str(control_plane.get("shared_job_id"))
        if (
            isinstance(control_plane.get("shared_job_id"), str)
            and control_plane.get("shared_job_id")
        )
        else None
    )
    if job_id:
        attached_shared_job_ids = [job_id]
        try:
            attach_run_to_shared_job(paths, job_id=job_id, run_id=run_id)
        except FileNotFoundError:
            attach_shared_job_to_run(paths, run_id=run_id, job_id=job_id)
        record_run_event_if_present(
            paths,
            run_id=run_id,
            stage="control-plane",
            event_type="shared-job-waiting",
            payload={"job_id": job_id, "state": pause_state},
        )
    return (
        attached_shared_job_ids,
        pause_state,
        (
            str(control_plane.get("confirmation_kind"))
            if isinstance(control_plane.get("confirmation_kind"), str)
            else confirmation_kind
        ),
        (
            str(control_plane.get("confirmation_prompt"))
            if isinstance(control_plane.get("confirmation_prompt"), str)
            else confirmation_prompt
        ),
        (
            str(control_plane.get("confirmation_reason"))
            if isinstance(control_plane.get("confirmation_reason"), str)
            else confirmation_reason
        ),
    )


def _maybe_handle_confirmation_reply(
    paths: WorkspacePaths,
    *,
    question: str,
    semantic_analysis: dict[str, Any] | None,
) -> tuple[str, dict[str, Any] | None] | None:
    action = normalize_confirmation_reply(question)
    if action is None:
        return None
    active = read_json(paths.active_conversation_path)
    if not active:
        active = read_json(paths.legacy_active_conversation_path)
    conversation_id = active.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        bound = load_bound_conversation_record_for_host(
            paths,
            host_identity=current_host_identity(),
        )
        conversation_id = bound.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return None
    pending = find_conversation_confirmation_job(paths, conversation_id)
    if not pending:
        return None
    turn = pending["turn"]
    manifest = pending["manifest"]
    turn_id = str(turn["turn_id"])
    run_id = (
        str(turn.get("active_run_id"))
        if isinstance(turn.get("active_run_id"), str) and turn.get("active_run_id")
        else None
    )
    if action == "decline":
        declined_job = decline_shared_job(
            paths,
            str(manifest["job_id"]),
            reason=str(
                manifest.get("confirmation_reason")
                or manifest.get("confirmation_prompt")
                or "The confirmation-required shared job was declined."
            ),
        )
        record_run_event_for_runs(
            paths,
            run_ids=declined_job.get("attached_run_ids"),
            stage="control-plane",
            event_type="shared-job-declined",
            payload={"job_id": declined_job.get("job_id")},
        )
        record_run_event_for_runs(
            paths,
            run_ids=declined_job.get("attached_run_ids"),
            stage="control-plane",
            event_type="shared-job-settled",
            payload={"job_id": declined_job.get("job_id"), "status": "declined"},
        )
        reason = (
            str(
                manifest.get("confirmation_prompt")
                or "The confirmation-required step was declined."
            )
            + " The current task was not continued."
        )
        return "declined", _commit_governed_boundary_turn(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason=reason,
        )
    approved_job = approve_shared_job(
        paths,
        str(manifest["job_id"]),
        owner={"kind": "run", "id": run_id} if isinstance(run_id, str) and run_id else None,
        run_id=run_id,
    )
    record_run_event_for_runs(
        paths,
        run_ids=approved_job.get("attached_run_ids"),
        stage="control-plane",
        event_type="shared-job-approved",
        payload={"job_id": approved_job.get("job_id")},
    )
    job_family = str(manifest.get("job_family") or "")
    report = None
    if job_family == "prepare":
        report = _prepare_with_optional_owner(
            paths,
            assume_yes=True,
            interactive=False,
            run_id=run_id,
        )
    elif job_family == "sync":
        report = _sync_with_optional_owner(
            paths,
            assume_yes=True,
            run_id=run_id,
        )
        if report.payload.get("sync_status") in {"valid", "warnings"} and bool(
            report.payload.get("published")
        ):
            refresh_turn_run_version_truth(
                paths,
                conversation_id=conversation_id,
                turn_id=turn_id,
                run_id=run_id,
            )
    if report is not None and report.payload.get("status") == ACTION_REQUIRED:
        updated = update_conversation_turn(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
            updates={
                "status": "action-required",
                "turn_state": "awaiting-confirmation"
                if report.payload.get("control_plane", {}).get("state") == "awaiting-confirmation"
                else "prepared",
                "freshness_notice": report.payload.get("detail"),
            },
        )
        return "blocked", updated
    settled_manifest = load_shared_job(paths, str(manifest["job_id"]))
    if settled_manifest and shared_job_is_settled(settled_manifest):
        record_run_event_for_runs(
            paths,
            run_ids=settled_manifest.get("attached_run_ids"),
            stage="control-plane",
            event_type="shared-job-settled",
            payload={
                "job_id": settled_manifest.get("job_id"),
                "status": settled_manifest.get("status"),
            },
        )
    original_question = str(turn.get("user_question") or "").strip()
    if not original_question:
        return None
    restored_analysis = (
        dict(turn.get("semantic_analysis"))
        if isinstance(turn.get("semantic_analysis"), dict)
        else semantic_analysis
    )
    return original_question, restored_analysis


def begin_lane_c_shared_refresh(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str,
    query: str,
    recommended_targets: list[dict[str, Any]],
    selected_source_id: str | None = None,
    target: str = "current",
) -> dict[str, Any]:
    """Create or attach the governed ask-time multimodal refresh for one source."""
    from .run_control import load_run_state

    run_state = load_run_state(paths, run_id)
    raw_version_truth = run_state.get("version_context")
    version_truth = dict(raw_version_truth) if isinstance(raw_version_truth, dict) else {}
    published_snapshot_id = str(version_truth.get("published_snapshot_id") or "")
    if not published_snapshot_id:
        raise ValueError("The governed multimodal refresh requires a published snapshot id.")

    normalized_targets = [
        item
        for item in recommended_targets
        if (
            isinstance(item, dict)
            and isinstance(item.get("source_id"), str)
            and item.get("source_id")
        )
    ]
    if not normalized_targets:
        raise ValueError(
            "The governed multimodal refresh requires at least one recommended target."
        )
    chosen_target = None
    if isinstance(selected_source_id, str) and selected_source_id:
        for item in normalized_targets:
            if item.get("source_id") == selected_source_id:
                chosen_target = item
                break
        if chosen_target is None:
            raise ValueError(
                f"Selected multimodal refresh source `{selected_source_id}` is not recommended."
            )
    else:
        chosen_target = normalized_targets[0]
    source_id = str(chosen_target["source_id"])
    work_path = write_hybrid_refresh_work(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        query=query,
        source_ids=[source_id],
        recommended_targets=[chosen_target],
        target=target,
    )
    job_key = lane_c_job_key(
        published_snapshot_id=published_snapshot_id,
        source_id=source_id,
    )
    job_info = ensure_shared_job(
        paths,
        job_key=job_key,
        job_family="lane-c",
        criticality="answer-critical",
        scope={
            "target": target,
            "published_snapshot_id": published_snapshot_id,
            "source_id": source_id,
            "required_overlay_slots": list(chosen_target.get("required_overlay_slots", [])),
            "target_artifact_ids": list(chosen_target.get("target_artifact_ids", [])),
        },
        input_signature=job_key,
        owner={"kind": "run", "id": run_id},
        run_id=run_id,
    )
    manifest = job_info["manifest"]
    caller_role = str(job_info.get("caller_role") or "owner")
    updates: dict[str, Any] = {
        "turn_state": "waiting-shared-job",
        "status": "waiting-shared-job",
        "freshness_notice": "The ask is waiting on a governed multimodal refresh.",
        "hybrid_refresh_triggered": True,
        "hybrid_refresh_sources": [source_id],
        "hybrid_refresh_snapshot_id": published_snapshot_id,
        "hybrid_refresh_job_ids": [str(manifest["job_id"])],
        "hybrid_refresh_summary": {
            "mode": "ask-hybrid",
            "work_path": work_path,
            "recommended_target_count": len(normalized_targets),
            "selected_source_id": source_id,
            "caller_role": caller_role,
        },
        "attached_shared_job_ids": [str(manifest["job_id"])],
    }
    if caller_role in {"owner", "waiter"}:
        record_run_event_if_present(
            paths,
            run_id=run_id,
            stage="control-plane",
            event_type="shared-job-waiting",
            payload={"job_id": manifest.get("job_id"), "state": "waiting-shared-job"},
        )
    update_conversation_turn(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        updates=updates,
    )
    return {
        "job_id": manifest.get("job_id"),
        "job_key": manifest.get("job_key"),
        "caller_role": caller_role,
        "work_path": work_path,
        "published_snapshot_id": published_snapshot_id,
        "selected_source_id": source_id,
    }


def settle_lane_c_shared_refresh(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    job_id: str,
    completion_status: str,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Settle one governed ask-time multimodal refresh and persist turn-visible state."""
    from .run_control import load_run_state

    if completion_status not in {"covered", "blocked"}:
        raise ValueError("Multimodal refresh completion_status must be `covered` or `blocked`.")
    if completion_status == "covered":
        manifest = complete_shared_job(paths, job_id, result=summary or {"status": "covered"})
    else:
        manifest = block_shared_job(paths, job_id, result=summary or {"status": "blocked"})
    record_run_event_for_runs(
        paths,
        run_ids=manifest.get("attached_run_ids"),
        stage="control-plane",
        event_type="shared-job-settled",
        payload={"job_id": manifest.get("job_id"), "status": manifest.get("status")},
    )
    scope = manifest.get("scope", {})
    settled_snapshot_id = (
        str(scope.get("published_snapshot_id"))
        if (
            isinstance(scope.get("published_snapshot_id"), str)
            and scope.get("published_snapshot_id")
        )
        else None
    )
    settled_source_id = (
        str(scope.get("source_id"))
        if isinstance(scope.get("source_id"), str) and scope.get("source_id")
        else None
    )
    attached_turn_refs: list[tuple[str, str, str | None]] = []
    seen_turns: set[tuple[str, str]] = set()
    for attached_run_id in manifest.get("attached_run_ids", []):
        if not isinstance(attached_run_id, str) or not attached_run_id:
            continue
        try:
            run_state = load_run_state(paths, attached_run_id)
        except FileNotFoundError:
            continue
        attached_conversation_id = run_state.get("conversation_id")
        attached_turn_id = run_state.get("turn_id")
        if (
            isinstance(attached_conversation_id, str)
            and attached_conversation_id
            and isinstance(attached_turn_id, str)
            and attached_turn_id
            and (attached_conversation_id, attached_turn_id) not in seen_turns
        ):
            seen_turns.add((attached_conversation_id, attached_turn_id))
            attached_turn_refs.append(
                (attached_conversation_id, attached_turn_id, attached_run_id)
            )
    if (conversation_id, turn_id) not in seen_turns:
        attached_turn_refs.append((conversation_id, turn_id, None))

    settled_turns: list[dict[str, Any]] = []
    common_updates = {
        "hybrid_refresh_triggered": True,
        "hybrid_refresh_sources": [settled_source_id] if settled_source_id else [],
        "hybrid_refresh_snapshot_id": settled_snapshot_id,
        "hybrid_refresh_completion_status": completion_status,
        "hybrid_refresh_job_ids": [job_id],
        "hybrid_refresh_summary": summary or {},
        "attached_shared_job_ids": [job_id],
    }
    if completion_status == "covered":
        for attached_conversation_id, attached_turn_id, _attached_run_id in attached_turn_refs:
            settled_turns.append(
                update_conversation_turn(
                    paths,
                    conversation_id=attached_conversation_id,
                    turn_id=attached_turn_id,
                    updates={
                        **common_updates,
                        "status": "prepared",
                        "turn_state": "prepared",
                        "freshness_notice": (
                            "The governed multimodal refresh finished. Rerun "
                            "retrieval and trace before committing the answer."
                        ),
                    },
                )
            )
        return {"manifest": manifest, "turns": settled_turns}

    boundary_reason = str(
        (summary or {}).get("detail")
        or (summary or {}).get("reason")
        or "The required multimodal source refresh could not continue safely."
    )
    for attached_conversation_id, attached_turn_id, _attached_run_id in attached_turn_refs:
        settled_turns.append(
            _commit_governed_boundary_turn(
                paths,
                conversation_id=attached_conversation_id,
                turn_id=attached_turn_id,
                reason=boundary_reason,
                extra_turn_updates=common_updates,
            )
        )
    return {"manifest": manifest, "turns": settled_turns}


def _effective_turn_snapshot(
    current_turn: dict[str, Any],
    *,
    session_ids: list[str],
    trace_ids: list[str],
    attached_shared_job_ids: list[str],
    log_origin: str,
    question_domain: str | None,
    support_basis: str,
    support_manifest_path: str | None,
    render_inspection_required: Any,
    inspection_scope: Any,
    preferred_channels: list[str],
    used_published_channels: list[str],
    published_artifacts_sufficient: Any,
    reference_resolution: dict[str, Any] | None,
    reference_resolution_summary: Any,
    source_escalation_required: Any,
    source_escalation_reason: Any,
    auto_sync_triggered: Any,
    auto_sync_reason: Any,
    auto_sync_summary: dict[str, Any] | None,
    hybrid_refresh_triggered: Any,
    hybrid_refresh_sources: list[str],
    hybrid_refresh_completion_status: Any,
    hybrid_refresh_summary: dict[str, Any] | None,
    hybrid_refresh_snapshot_id: Any,
    hybrid_refresh_job_ids: list[str],
) -> dict[str, Any]:
    snapshot = dict(current_turn)
    snapshot.update(
        {
            "session_ids": session_ids,
            "trace_ids": trace_ids,
            "attached_shared_job_ids": attached_shared_job_ids,
            "log_origin": log_origin,
            "question_domain": question_domain,
            "support_basis": support_basis,
            "support_manifest_path": support_manifest_path,
            "render_inspection_required": render_inspection_required,
            "inspection_scope": inspection_scope,
            "preferred_channels": preferred_channels,
            "used_published_channels": used_published_channels,
            "published_artifacts_sufficient": published_artifacts_sufficient,
            "reference_resolution": reference_resolution,
            "reference_resolution_summary": reference_resolution_summary,
            "source_escalation_required": source_escalation_required,
            "source_escalation_reason": source_escalation_reason,
            "auto_sync_triggered": auto_sync_triggered,
            "auto_sync_reason": auto_sync_reason,
            "auto_sync_summary": auto_sync_summary,
            "hybrid_refresh_triggered": hybrid_refresh_triggered,
            "hybrid_refresh_sources": hybrid_refresh_sources,
            "hybrid_refresh_completion_status": hybrid_refresh_completion_status,
            "hybrid_refresh_summary": hybrid_refresh_summary,
            "hybrid_refresh_snapshot_id": hybrid_refresh_snapshot_id,
            "hybrid_refresh_job_ids": hybrid_refresh_job_ids,
        }
    )
    return snapshot


def _maybe_begin_lane_c_before_commit(
    paths: WorkspacePaths,
    *,
    current_turn: dict[str, Any],
    run_id: str | None,
    latest_trace_payload: dict[str, Any],
    effective_turn_snapshot: dict[str, Any],
    inner_workflow_id: str,
) -> dict[str, Any] | None:
    if not isinstance(run_id, str) or not run_id:
        return None
    if effective_turn_snapshot.get("question_domain") not in {"workspace-corpus", "composition"}:
        return None
    if effective_turn_snapshot.get("source_escalation_required") is not True:
        return None
    if effective_turn_snapshot.get("published_artifacts_sufficient") is not False:
        return None
    if effective_turn_snapshot.get("support_basis") in {
        "external-source-verified",
        "model-knowledge",
        "governed-boundary",
    }:
        return None
    if bool(effective_turn_snapshot.get("hybrid_refresh_triggered")):
        return None
    recommended_targets = [
        item
        for item in latest_trace_payload.get("recommended_hybrid_targets", [])
        if isinstance(item, dict)
    ]
    if not recommended_targets:
        raise ValueError(
            "A governed multimodal refresh is required for this turn, but the "
            "trace payload does not include "
            "recommended_hybrid_targets."
        )
    target = (
        str(latest_trace_payload.get("target"))
        if (
            isinstance(latest_trace_payload.get("target"), str)
            and latest_trace_payload.get("target")
        )
        else "current"
    )
    begin_lane_c_shared_refresh(
        paths,
        conversation_id=str(current_turn["conversation_id"]),
        turn_id=str(current_turn["turn_id"]),
        run_id=run_id,
        query=str(current_turn.get("user_question") or ""),
        recommended_targets=recommended_targets,
        target=target,
    )
    update_conversation_turn(
        paths,
        conversation_id=str(current_turn["conversation_id"]),
        turn_id=str(current_turn["turn_id"]),
        updates={
            "session_ids": _resolved_string_list(effective_turn_snapshot.get("session_ids")),
            "trace_ids": _resolved_string_list(effective_turn_snapshot.get("trace_ids")),
            "question_domain": effective_turn_snapshot.get("question_domain"),
            "support_basis": effective_turn_snapshot.get("support_basis"),
            "support_manifest_path": effective_turn_snapshot.get("support_manifest_path"),
            "render_inspection_required": effective_turn_snapshot.get("render_inspection_required"),
            "inspection_scope": effective_turn_snapshot.get("inspection_scope"),
            "preferred_channels": _resolved_string_list(
                effective_turn_snapshot.get("preferred_channels")
            ),
            "used_published_channels": _resolved_string_list(
                effective_turn_snapshot.get("used_published_channels")
            ),
            "published_artifacts_sufficient": effective_turn_snapshot.get(
                "published_artifacts_sufficient"
            ),
            "reference_resolution": effective_turn_snapshot.get("reference_resolution"),
            "reference_resolution_summary": effective_turn_snapshot.get(
                "reference_resolution_summary"
            ),
            "source_escalation_required": effective_turn_snapshot.get(
                "source_escalation_required"
            ),
            "source_escalation_reason": effective_turn_snapshot.get("source_escalation_reason"),
        },
    )
    updated_turn = load_turn_record(
        paths,
        conversation_id=str(current_turn["conversation_id"]),
        turn_id=str(current_turn["turn_id"]),
    )
    _sync_turn_log_artifacts(
        paths,
        conversation_id=str(current_turn["conversation_id"]),
        turn_id=str(current_turn["turn_id"]),
        run_id=run_id,
        session_ids=_resolved_string_list(updated_turn.get("session_ids")),
        trace_ids=_resolved_string_list(updated_turn.get("trace_ids")),
        inner_workflow_id=inner_workflow_id,
        native_turn_id=updated_turn.get("native_turn_id")
        if isinstance(updated_turn.get("native_turn_id"), str)
        else None,
        front_door_state=updated_turn.get("front_door_state")
        if isinstance(updated_turn.get("front_door_state"), str)
        else None,
        semantic_log_context={
            **semantic_log_context_from_record(updated_turn),
            **semantic_log_context_fields(
                question_domain=updated_turn.get("question_domain")
                if isinstance(updated_turn.get("question_domain"), str)
                else None,
                support_basis=updated_turn.get("support_basis")
                if isinstance(updated_turn.get("support_basis"), str)
                else None,
                support_manifest_path=updated_turn.get("support_manifest_path")
                if isinstance(updated_turn.get("support_manifest_path"), str)
                else None,
            ),
        },
        log_origin=updated_turn.get("log_origin")
        if isinstance(updated_turn.get("log_origin"), str)
        else None,
        answer_file_path=updated_turn.get("answer_file_path")
        if isinstance(updated_turn.get("answer_file_path"), str)
        else None,
        answer_state=updated_turn.get("answer_state")
        if isinstance(updated_turn.get("answer_state"), str)
        else None,
        render_inspection_required=updated_turn.get("render_inspection_required")
        if isinstance(updated_turn.get("render_inspection_required"), bool)
        else None,
        inspection_scope=updated_turn.get("inspection_scope")
        if isinstance(updated_turn.get("inspection_scope"), str)
        else None,
        preferred_channels=_resolved_string_list(updated_turn.get("preferred_channels")),
        used_published_channels=_resolved_string_list(updated_turn.get("used_published_channels")),
        published_artifacts_sufficient=updated_turn.get("published_artifacts_sufficient")
        if isinstance(updated_turn.get("published_artifacts_sufficient"), bool)
        else None,
        reference_resolution=updated_turn.get("reference_resolution")
        if isinstance(updated_turn.get("reference_resolution"), dict)
        else None,
        reference_resolution_summary=updated_turn.get("reference_resolution_summary")
        if isinstance(updated_turn.get("reference_resolution_summary"), str)
        else None,
        source_escalation_required=updated_turn.get("source_escalation_required")
        if isinstance(updated_turn.get("source_escalation_required"), bool)
        else None,
        source_escalation_reason=updated_turn.get("source_escalation_reason")
        if isinstance(updated_turn.get("source_escalation_reason"), str)
        else None,
        hybrid_refresh_triggered=True,
        hybrid_refresh_sources=_resolved_string_list(updated_turn.get("hybrid_refresh_sources")),
        hybrid_refresh_completion_status=updated_turn.get("hybrid_refresh_completion_status")
        if isinstance(updated_turn.get("hybrid_refresh_completion_status"), str)
        else None,
        hybrid_refresh_summary=updated_turn.get("hybrid_refresh_summary")
        if isinstance(updated_turn.get("hybrid_refresh_summary"), dict)
        else None,
    )
    return {
        "conversation_id": str(current_turn["conversation_id"]),
        "turn_id": str(current_turn["turn_id"]),
        **updated_turn,
    }


def _prepared_turn_response(
    *,
    opened: dict[str, Any],
    run_id: str,
    inner_workflow_id: str,
    question_class: str,
    question_domain: str,
    route_reason: str,
    knowledge_base_missing: bool,
    knowledge_base_stale: bool,
    auto_prepare_triggered: bool,
    auto_prepare_reason: str | None,
    auto_prepare_summary: dict[str, Any] | None,
    sync_suggested: bool,
    auto_sync_triggered: bool,
    auto_sync_reason: str | None,
    auto_sync_summary: dict[str, Any] | None,
    interaction_sync_suggested: bool,
    interaction_snapshot: dict[str, Any],
    memory_query_profile: dict[str, Any],
    evidence_mode: str,
    support_strategy: str,
    evidence_requirements: dict[str, Any],
    inspection_scope: str,
    preferred_channels: list[str],
    reference_resolution: dict[str, Any] | None,
    reference_resolution_summary: str | None,
    prefer_published_artifacts: bool,
    analysis_origin: str,
    normalized_semantic_analysis: dict[str, Any],
    research_depth: str,
    bundle_paths: list[str],
    warm_start: dict[str, Any],
    prefer_sync_before_answer: bool,
    freshness_notice: str | None,
    status: str,
    log_origin: str,
) -> dict[str, Any]:
    return {
        **opened,
        "run_id": run_id,
        "log_origin": log_origin,
        "entry_workflow_id": "ask",
        "inner_workflow_id": inner_workflow_id,
        "question_class": question_class,
        "question_domain": question_domain,
        "route_reason": route_reason,
        "knowledge_base_missing": knowledge_base_missing,
        "knowledge_base_stale": knowledge_base_stale,
        "auto_prepare_triggered": auto_prepare_triggered,
        "auto_prepare_reason": auto_prepare_reason,
        "auto_prepare_summary": auto_prepare_summary,
        "sync_suggested": sync_suggested,
        "sync_requested": auto_sync_triggered,
        "auto_sync_triggered": auto_sync_triggered,
        "auto_sync_reason": auto_sync_reason,
        "auto_sync_summary": auto_sync_summary,
        "interaction_sync_suggested": interaction_sync_suggested,
        "pending_interaction_count": interaction_snapshot["pending_promotion_count"],
        "memory_query_profile": memory_query_profile,
        "evidence_mode": evidence_mode,
        "support_strategy": support_strategy,
        "evidence_requirements": evidence_requirements,
        "inspection_scope": inspection_scope,
        "preferred_channels": preferred_channels,
        "reference_resolution": reference_resolution,
        "reference_resolution_summary": reference_resolution_summary,
        "prefer_published_artifacts": prefer_published_artifacts,
        "analysis_origin": analysis_origin,
        "semantic_analysis": normalized_semantic_analysis,
        "research_depth": research_depth,
        "bundle_paths": bundle_paths,
        "warm_start_evidence": warm_start,
        "prefer_sync_before_answer": prefer_sync_before_answer,
        "freshness_notice": freshness_notice,
        "status": status,
        "log_context": build_log_context(
            conversation_id=opened["conversation_id"],
            turn_id=opened["turn_id"],
            run_id=run_id,
            entry_workflow_id="ask",
            inner_workflow_id=inner_workflow_id,
            native_turn_id=opened["native_turn_id"]
            if isinstance(opened.get("native_turn_id"), str)
            else None,
            front_door_state=opened.get("front_door_state")
            if isinstance(opened.get("front_door_state"), str)
            else None,
            question_class=question_class,
            question_domain=question_domain,
            support_strategy=support_strategy,
            analysis_origin=analysis_origin,
        ),
    }


def _upgrade_turn_to_canonical_ask(
    paths: WorkspacePaths,
    *,
    opened: dict[str, Any],
    run_id: str,
    log_origin: str,
) -> dict[str, Any]:
    """Upgrade one live turn and run from reconciliation-only to canonical ask ownership."""
    front_door_opened_at = (
        str(opened.get("front_door_opened_at"))
        if isinstance(opened.get("front_door_opened_at"), str)
        and opened.get("front_door_opened_at")
        else utc_now()
    )
    update_run_state(
        paths,
        run_id=run_id,
        updates={
            "run_origin": RUN_ORIGIN_ASK_FRONT_DOOR,
            "log_origin": normalize_log_origin(log_origin),
        },
    )
    updated_turn = update_conversation_turn(
        paths,
        conversation_id=opened["conversation_id"],
        turn_id=opened["turn_id"],
        updates={
            "active_run_id": run_id,
            "front_door_state": FRONT_DOOR_STATE_CANONICAL_ASK,
            "front_door_opened_at": front_door_opened_at,
            "front_door_run_id": run_id,
            "log_origin": normalize_log_origin(log_origin),
        },
    )
    opened["front_door_state"] = updated_turn.get("front_door_state")
    opened["front_door_opened_at"] = updated_turn.get("front_door_opened_at")
    opened["front_door_run_id"] = updated_turn.get("front_door_run_id")
    opened["native_turn_id"] = updated_turn.get("native_turn_id")
    return updated_turn


def prepare_ask_turn(
    paths: WorkspacePaths,
    *,
    question: str,
    semantic_analysis: dict[str, Any] | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Open or reuse one canonical ask turn.

    This is an internal ask-lifecycle primitive used by canonical workflow code.
    It is not a preferred host integration entrypoint.
    """
    confirmation_resolution = _maybe_handle_confirmation_reply(
        paths,
        question=question,
        semantic_analysis=semantic_analysis,
    )
    if confirmation_resolution is not None:
        if confirmation_resolution[0] in {"declined", "blocked"}:
            return dict(confirmation_resolution[1] or {})
        question = confirmation_resolution[0]
        if isinstance(confirmation_resolution[1], dict):
            semantic_analysis = confirmation_resolution[1]
    else:
        maybe_reconcile_active_thread(paths)
    opened = open_conversation_turn(paths, user_question=question, entry_workflow_id="ask")
    run_payload = ensure_run_for_turn(
        paths,
        conversation_id=opened["conversation_id"],
        turn_id=opened["turn_id"],
        user_question=question,
        entry_workflow_id="ask",
        run_origin=RUN_ORIGIN_ASK_FRONT_DOOR,
    )
    run_id = str(run_payload["run_id"])
    effective_log_origin = _effective_ask_log_origin(
        log_origin,
        run_state=run_payload,
    )
    _upgrade_turn_to_canonical_ask(
        paths,
        opened=opened,
        run_id=run_id,
        log_origin=effective_log_origin,
    )
    knowledge_base = opened["workspace_snapshot"]["knowledge_base"]
    interaction_snapshot = interaction_ingest_snapshot(paths)
    profile = question_execution_profile(
        paths,
        conversation_id=opened["conversation_id"],
        turn_id=opened["turn_id"],
        question=question,
        semantic_analysis=semantic_analysis,
    )
    question_class = str(profile["question_class"])
    question_domain = str(profile["question_domain"])
    inner_workflow_id = str(profile["inner_workflow_id"])
    route_reason = str(profile["route_reason"])
    record_run_event(
        paths,
        run_id=run_id,
        stage="prepare",
        event_type="preanswer-governance-started",
        payload={
            "question_class": question_class,
            "question_domain": question_domain,
            "inner_workflow_id": inner_workflow_id,
        },
    )
    interaction_relevance = interaction_overlay_relevance(
        paths,
        question,
        question_class=question_class,
        question_domain=question_domain,
    )
    memory_query_profile = profile["memory_query_profile"]
    bundle_paths = profile["bundle_paths"]
    evidence_mode = str(profile["evidence_mode"])
    research_depth = str(profile["research_depth"])
    support_strategy = str(profile["support_strategy"])
    evidence_requirements = dict(profile["evidence_requirements"])
    preferred_channels = [
        channel
        for channel in evidence_requirements.get("preferred_channels", [])
        if isinstance(channel, str)
    ]
    inspection_scope = str(evidence_requirements.get("inspection_scope"))
    prefer_published_artifacts = bool(
        evidence_requirements.get("prefer_published_artifacts", True)
    )
    needs_latest_workspace_state = bool(profile["needs_latest_workspace_state"])
    analysis_origin = str(profile["analysis_origin"])
    normalized_semantic_analysis = dict(profile["semantic_analysis"])
    warm_start = profile["warm_start_evidence"]
    workspace_notices_enabled = _workspace_notices_enabled(question_domain)
    reference_resolution = (
        resolve_workspace_reference(paths, query=question)
        if knowledge_base["present"]
        else None
    )
    if isinstance(reference_resolution, dict) and not reference_resolution.get("detected"):
        reference_resolution = None
    reference_resolution_summary = build_reference_resolution_summary(reference_resolution)

    environment_state = cached_bootstrap_readiness(paths)
    environment_ready = bool(environment_state["ready"])
    action_required = False
    sync_suggested = False
    prefer_sync_before_answer = False
    freshness_notice = None
    auto_prepare_triggered = False
    auto_prepare_reason = None
    auto_prepare_summary = None
    auto_sync_triggered = False
    auto_sync_reason = None
    auto_sync_summary = None
    control_plane_pause_state: str | None = None
    attached_shared_job_ids: list[str] = []
    confirmation_kind: str | None = None
    confirmation_prompt: str | None = None
    confirmation_reason: str | None = None

    if workspace_notices_enabled:
        (
            environment_ready,
            environment_state,
            auto_prepare_triggered,
            auto_prepare_reason,
            auto_prepare_summary,
        ) = _ensure_workspace_environment(paths, run_id=run_id)
        control_plane = (
            dict(auto_prepare_summary.get("control_plane", {}))
            if isinstance(auto_prepare_summary, dict)
            and isinstance(auto_prepare_summary.get("control_plane"), dict)
            else {}
        )
        (
            attached_shared_job_ids,
            control_plane_pause_state,
            confirmation_kind,
            confirmation_prompt,
            confirmation_reason,
        ) = _apply_control_plane_pause(
            paths,
            run_id=run_id,
            control_plane=control_plane,
            attached_shared_job_ids=attached_shared_job_ids,
            control_plane_pause_state=control_plane_pause_state,
            confirmation_kind=confirmation_kind,
            confirmation_prompt=confirmation_prompt,
            confirmation_reason=confirmation_reason,
        )

    if workspace_notices_enabled and not environment_ready:
        action_required = True
        inner_workflow_id = "workspace-bootstrap"
        route_reason = (
            "The ask path could not complete the automatic workspace repair required before "
            "workspace evidence can be used safely."
            if auto_prepare_triggered
            else (
                "The workspace environment is not ready for ordinary workspace evidence work."
            )
        )
        freshness_notice = str(
            environment_state.get("detail")
            or "The workspace environment is not ready for ordinary workspace evidence work."
        )

    if (
        not action_required
        and not knowledge_base["present"]
        and workspace_notices_enabled
        and not environment_ready
    ):
        action_required = True
        inner_workflow_id = "workspace-bootstrap"
        route_reason = (
            "The ask path could not complete the automatic workspace bootstrap required before "
            "a missing knowledge base can be built and answered safely."
            if auto_prepare_triggered
            else (
                "A published knowledge base is missing and the workspace environment is not ready "
                "for an automatic sync."
            )
        )
        freshness_notice = "No published knowledge base is available yet."
    elif workspace_notices_enabled and question_domain == "workspace-corpus" and environment_ready:
        should_auto_sync = False
        candidate_reason = None
        if not knowledge_base["present"]:
            should_auto_sync = True
            candidate_reason = (
                "A published knowledge base is missing for this workspace-corpus question."
            )
        elif knowledge_base["stale"]:
            _index_preview, _active_preview, _ambiguous_preview, preview_change_set = (
                _preview_source_changes(paths)
            )
            should_auto_sync, candidate_reason = _changed_source_relevance(
                question=question,
                change_set=preview_change_set,
                reference_resolution=reference_resolution,
                needs_latest_workspace_state=needs_latest_workspace_state,
            )
            sync_suggested = should_auto_sync
            prefer_sync_before_answer = should_auto_sync
            if not should_auto_sync:
                freshness_notice = (
                    "The published knowledge base is stale, but the current question appears "
                    "unrelated to the changed sources."
                )
    if (
        workspace_notices_enabled
        and question_domain == "workspace-corpus"
        and not action_required
        and interaction_snapshot["pending_promotion_count"]
    ):
        if interaction_snapshot.get("load_warnings"):
            sync_suggested = True
            if freshness_notice:
                freshness_notice = (
                    f"{freshness_notice} Pending interaction-derived runtime state could not be "
                    "read completely during this check, so a sync may be needed once active "
                    "writes finish."
                )
            else:
                freshness_notice = (
                    "Pending interaction-derived runtime state could not be read completely "
                    "during this check, so a sync may be needed once active writes finish."
                )
        elif interaction_relevance["has_relevant_pending_interaction"]:
            sync_suggested = True
            if freshness_notice:
                freshness_notice = (
                    f"{freshness_notice} Pending interaction-derived knowledge also appears "
                    "relevant and still awaits sync-time promotion."
                )
            else:
                freshness_notice = (
                    "Pending interaction-derived knowledge appears relevant and still awaits "
                    "sync-time promotion."
                )
            if environment_ready:
                sync_suggested = True
                prefer_sync_before_answer = True

    if (
        workspace_notices_enabled
        and question_domain == "workspace-corpus"
        and environment_ready
        and (
            (not knowledge_base["present"])
            or (knowledge_base["stale"] and prefer_sync_before_answer)
            or (
                interaction_snapshot["pending_promotion_count"]
                and (
                    interaction_relevance["has_relevant_pending_interaction"]
                    or bool(interaction_snapshot.get("load_warnings"))
                )
            )
        )
    ):
        if auto_sync_reason is None:
            if not knowledge_base["present"]:
                auto_sync_reason = (
                    "A published knowledge base is missing for this workspace-corpus question."
                )
            elif knowledge_base["stale"] and prefer_sync_before_answer:
                auto_sync_reason = candidate_reason
            else:
                auto_sync_reason = (
                    "Relevant pending interaction-derived knowledge still awaits "
                    "sync-time promotion."
                )
        (
            sync_environment_ready,
            sync_environment_state,
            sync_auto_prepare_triggered,
            sync_auto_prepare_reason,
            sync_auto_prepare_summary,
        ) = _ensure_workspace_environment(
            paths,
            require_sync_capability=True,
            run_id=run_id,
        )
        if sync_auto_prepare_triggered:
            auto_prepare_triggered = True
            auto_prepare_reason = sync_auto_prepare_reason
            auto_prepare_summary = sync_auto_prepare_summary
            environment_state = sync_environment_state
            environment_ready = sync_environment_ready
        if not sync_environment_ready:
            action_required = True
            inner_workflow_id = "workspace-bootstrap"
            route_reason = (
                "The ask path attempted an automatic workspace repair because fresh workspace "
                "evidence is required, but the environment still lacks the capabilities needed "
                "for sync."
            )
            freshness_notice = str(
                sync_environment_state.get("detail")
                or "The workspace is not ready for an automatic sync."
            )
        else:
            sync_report = _sync_with_optional_owner(
                paths,
                assume_yes=False,
                run_id=run_id,
            )
            sync_payload = dict(sync_report.payload)
            auto_sync_triggered = True
            auto_sync_summary = _auto_sync_summary(sync_payload)
            control_plane = (
                dict(sync_payload.get("control_plane", {}))
                if isinstance(sync_payload.get("control_plane"), dict)
                else {}
            )
            if control_plane.get("state") in {"awaiting-confirmation", "waiting-shared-job"}:
                (
                    attached_shared_job_ids,
                    control_plane_pause_state,
                    confirmation_kind,
                    confirmation_prompt,
                    confirmation_reason,
                ) = _apply_control_plane_pause(
                    paths,
                    run_id=run_id,
                    control_plane=control_plane,
                    attached_shared_job_ids=attached_shared_job_ids,
                    control_plane_pause_state=control_plane_pause_state,
                    confirmation_kind=confirmation_kind,
                    confirmation_prompt=confirmation_prompt,
                    confirmation_reason=confirmation_reason,
                )
                freshness_notice = str(
                    control_plane.get("confirmation_prompt")
                    or sync_payload.get("detail")
                    or "The ask is waiting on a shared sync job."
                )
                sync_suggested = True
            elif sync_payload.get("sync_status") in {"valid", "warnings"} and bool(
                sync_payload.get("published")
            ):
                refresh_turn_run_version_truth(
                    paths,
                    conversation_id=opened["conversation_id"],
                    turn_id=opened["turn_id"],
                    run_id=run_id,
                )
                knowledge_base = knowledge_base_snapshot(paths)
                interaction_snapshot = interaction_ingest_snapshot(paths)
                freshness_notice = (
                    "The knowledge base was refreshed automatically before answering."
                )
                sync_suggested = False
                prefer_sync_before_answer = False
                if knowledge_base["present"]:
                    reference_resolution = resolve_workspace_reference(paths, query=question)
                    if (
                        isinstance(reference_resolution, dict)
                        and not reference_resolution.get("detected")
                    ):
                        reference_resolution = None
                    reference_resolution_summary = build_reference_resolution_summary(
                        reference_resolution
                    )
            else:
                action_required = True
                inner_workflow_id = "knowledge-base-sync"
                route_reason = (
                    "The ask path attempted an automatic sync because the question needs fresh "
                    "workspace evidence, but final publication did not succeed."
                )
                freshness_notice = str(
                    sync_payload.get("detail") or "Automatic sync did not complete."
                )
    elif not action_required and not knowledge_base["present"] and workspace_notices_enabled:
        action_required = True
        inner_workflow_id = "knowledge-base-sync" if environment_ready else "workspace-bootstrap"
        route_reason = (
            "A published knowledge base is missing, so the user question cannot be answered "
            "safely from current state."
        )
        freshness_notice = "No published knowledge base is available yet."
    elif not action_required and knowledge_base["stale"] and workspace_notices_enabled:
        sync_suggested = True
        freshness_notice = "The published knowledge base appears stale relative to `original_doc/`."
        if needs_latest_workspace_state:
            prefer_sync_before_answer = True

    interaction_sync_suggested = bool(
        not action_required
        and
        workspace_notices_enabled
        and question_domain == "workspace-corpus"
        and interaction_snapshot["pending_promotion_count"]
        and (
            interaction_relevance["has_relevant_pending_interaction"]
            or bool(interaction_snapshot.get("load_warnings"))
        )
    )

    if control_plane_pause_state == "awaiting-confirmation":
        status = "awaiting-confirmation"
        if confirmation_prompt:
            freshness_notice = confirmation_prompt
    elif control_plane_pause_state == "waiting-shared-job":
        status = "waiting-shared-job"
    else:
        status = "action-required" if action_required else "prepared"
    update_conversation_turn(
        paths,
        conversation_id=opened["conversation_id"],
        turn_id=opened["turn_id"],
        updates={
            "inner_workflow_id": inner_workflow_id,
            "question_class": question_class,
            "question_domain": question_domain,
            "knowledge_base_missing": not knowledge_base["present"],
            "knowledge_base_stale": knowledge_base["stale"],
            "auto_prepare_triggered": auto_prepare_triggered,
            "auto_prepare_reason": auto_prepare_reason,
            "auto_prepare_summary": auto_prepare_summary,
            "sync_suggested": sync_suggested,
            "sync_requested": auto_sync_triggered,
            "auto_sync_triggered": auto_sync_triggered,
            "auto_sync_reason": auto_sync_reason,
            "auto_sync_summary": auto_sync_summary,
            "interaction_sync_suggested": interaction_sync_suggested,
            "evidence_mode": evidence_mode,
            "support_strategy": support_strategy,
            "inspection_scope": inspection_scope,
            "preferred_channels": preferred_channels,
            "reference_resolution": reference_resolution,
            "reference_resolution_summary": reference_resolution_summary,
            "analysis_origin": analysis_origin,
            "semantic_analysis": normalized_semantic_analysis,
            "research_depth": research_depth,
            "bundle_paths": bundle_paths,
            "reused_previous_evidence": bool(warm_start.get("matched_records")),
            "attached_shared_job_ids": attached_shared_job_ids,
            "confirmation_kind": confirmation_kind,
            "confirmation_prompt": confirmation_prompt,
            "confirmation_reason": confirmation_reason,
            "front_door_state": FRONT_DOOR_STATE_CANONICAL_ASK,
            "front_door_opened_at": opened.get("front_door_opened_at"),
            "front_door_run_id": run_id,
            "log_origin": effective_log_origin,
            "turn_state": (
                status
                if status in {"awaiting-confirmation", "waiting-shared-job"}
                else "prepared"
            ),
            "status": status,
            "route_reason": route_reason,
            "freshness_notice": freshness_notice,
        },
    )
    record_run_event(
        paths,
        run_id=run_id,
        stage="prepare",
        event_type="ask-prepared",
        payload={
            "question_domain": question_domain,
            "inner_workflow_id": inner_workflow_id,
            "status": status,
            "auto_prepare_triggered": auto_prepare_triggered,
            "auto_sync_triggered": auto_sync_triggered,
            "attached_shared_job_ids": attached_shared_job_ids,
        },
    )
    response = _prepared_turn_response(
        opened=opened,
        run_id=run_id,
        inner_workflow_id=inner_workflow_id,
        question_class=question_class,
        question_domain=question_domain,
        route_reason=route_reason,
        knowledge_base_missing=not knowledge_base["present"],
        knowledge_base_stale=knowledge_base["stale"],
        auto_prepare_triggered=auto_prepare_triggered,
        auto_prepare_reason=auto_prepare_reason,
        auto_prepare_summary=auto_prepare_summary,
        sync_suggested=sync_suggested,
        auto_sync_triggered=auto_sync_triggered,
        auto_sync_reason=auto_sync_reason,
        auto_sync_summary=auto_sync_summary,
        interaction_sync_suggested=interaction_sync_suggested,
        interaction_snapshot=interaction_snapshot,
        memory_query_profile=memory_query_profile,
        evidence_mode=evidence_mode,
        support_strategy=support_strategy,
        evidence_requirements=evidence_requirements,
        inspection_scope=inspection_scope,
        preferred_channels=preferred_channels,
        reference_resolution=reference_resolution,
        reference_resolution_summary=reference_resolution_summary,
        prefer_published_artifacts=prefer_published_artifacts,
        analysis_origin=analysis_origin,
        normalized_semantic_analysis=normalized_semantic_analysis,
        research_depth=research_depth,
        bundle_paths=bundle_paths,
        warm_start=warm_start,
        prefer_sync_before_answer=prefer_sync_before_answer,
        freshness_notice=freshness_notice,
        status=status,
        log_origin=effective_log_origin,
    )
    response["attached_shared_job_ids"] = attached_shared_job_ids
    response["confirmation_kind"] = confirmation_kind
    response["confirmation_prompt"] = confirmation_prompt
    response["confirmation_reason"] = confirmation_reason
    return response


def complete_ask_turn(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    inner_workflow_id: str,
    session_ids: list[str] | None = None,
    trace_ids: list[str] | None = None,
    answer_state: str | None = None,
    render_inspection_required: bool | None = None,
    answer_file_path: str | None = None,
    response_excerpt: str | None = None,
    sync_requested: bool = False,
    question_domain: str | None = None,
    support_basis: str | None = None,
    support_manifest_sources: list[dict[str, Any]] | None = None,
    support_manifest_key_assertions: list[str] | None = None,
    support_manifest_notes: str | None = None,
    support_manifest_path: str | None = None,
    source_escalation_used: bool | None = None,
    inspection_scope: str | None = None,
    preferred_channels: list[str] | None = None,
    used_published_channels: list[str] | None = None,
    published_artifacts_sufficient: bool | None = None,
    source_escalation_required: bool | None = None,
    source_escalation_reason: str | None = None,
    evidence_mode: str | None = None,
    research_depth: str | None = None,
    bundle_paths: list[str] | None = None,
    hybrid_refresh_triggered: bool | None = None,
    hybrid_refresh_sources: list[str] | None = None,
    hybrid_refresh_completion_status: str | None = None,
    hybrid_refresh_summary: dict[str, Any] | None = None,
    hybrid_refresh_snapshot_id: str | None = None,
    hybrid_refresh_job_ids: list[str] | None = None,
    log_origin: str | None = None,
    status: str = "completed",
) -> dict[str, Any]:
    """Commit one canonical ask turn through the shared barrier.

    This is an internal ask-lifecycle primitive used by canonical workflow code.
    It is not a preferred host integration entrypoint.
    """
    current_turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    current_turn = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        **current_turn,
    }
    run_id = (
        str(current_turn.get("active_run_id"))
        if isinstance(current_turn.get("active_run_id"), str) and current_turn.get("active_run_id")
        else None
    )
    run_state = load_run_state(paths, run_id) if isinstance(run_id, str) and run_id else {}
    resolved_log_origin = _effective_ask_log_origin(
        log_origin,
        turn=current_turn,
        run_state=run_state,
    )
    if isinstance(run_id, str) and run_id and run_state.get("log_origin") != resolved_log_origin:
        run_state = update_run_state(
            paths,
            run_id=run_id,
            updates={"log_origin": resolved_log_origin},
        )
    if current_turn.get("log_origin") != resolved_log_origin:
        current_turn = {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            **update_conversation_turn(
                paths,
                conversation_id=conversation_id,
                turn_id=turn_id,
                updates={"log_origin": resolved_log_origin},
            ),
        }
    resolved_front_door_state = normalize_front_door_state(current_turn.get("front_door_state"))
    provisional_answer_file_path = answer_file_path or current_turn.get("answer_file_path")
    discovered_session_ids, discovered_trace_ids = _discover_unique_turn_log_artifacts(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        inner_workflow_id=inner_workflow_id,
        answer_file_path=(
            provisional_answer_file_path
            if isinstance(provisional_answer_file_path, str)
            else None
        ),
    )
    resolved_session_ids = _resolved_log_artifact_ids(
        explicit_ids=session_ids,
        current_ids=current_turn.get("session_ids"),
        discovered_ids=discovered_session_ids,
    )
    effective_trace_ids = _resolved_log_artifact_ids(
        explicit_ids=trace_ids,
        current_ids=current_turn.get("trace_ids"),
        discovered_ids=discovered_trace_ids,
    )
    latest_trace_payload = _latest_trace_record(paths, effective_trace_ids)
    resolved_question_domain = _resolve_scalar(
        question_domain,
        latest_trace_payload,
        current_turn,
        "question_domain",
    )
    resolved_answer_file_path = answer_file_path or current_turn.get("answer_file_path")
    resolved_support_basis = _resolve_scalar(
        support_basis,
        latest_trace_payload,
        current_turn,
        "support_basis",
    )
    resolved_support_manifest_path = _resolve_scalar(
        support_manifest_path,
        latest_trace_payload,
        current_turn,
        "support_manifest_path",
    )
    resolved_answer_state = _resolve_scalar(
        answer_state,
        latest_trace_payload,
        current_turn,
        "answer_state",
    )
    resolved_render_inspection_required = _resolve_scalar(
        render_inspection_required,
        latest_trace_payload,
        current_turn,
        "render_inspection_required",
    )
    resolved_inspection_scope = _resolve_scalar(
        inspection_scope,
        latest_trace_payload,
        current_turn,
        "inspection_scope",
    )
    resolved_preferred_channels = _resolve_list(
        preferred_channels,
        latest_trace_payload,
        current_turn,
        "preferred_channels",
    )
    resolved_used_published_channels = _resolve_list(
        used_published_channels,
        latest_trace_payload,
        current_turn,
        "used_published_channels",
    )
    resolved_published_artifacts_sufficient = _resolve_scalar(
        published_artifacts_sufficient,
        latest_trace_payload,
        current_turn,
        "published_artifacts_sufficient",
    )
    resolved_reference_resolution = _resolve_mapping(
        None,
        latest_trace_payload,
        current_turn,
        "reference_resolution",
    )
    resolved_reference_resolution_summary = _resolve_scalar(
        build_reference_resolution_summary(resolved_reference_resolution),
        latest_trace_payload,
        current_turn,
        "reference_resolution_summary",
    )
    resolved_source_escalation_required = _resolve_scalar(
        source_escalation_required,
        latest_trace_payload,
        current_turn,
        "source_escalation_required",
    )
    resolved_source_escalation_reason = _resolve_scalar(
        source_escalation_reason,
        latest_trace_payload,
        current_turn,
        "source_escalation_reason",
    )
    resolved_auto_sync_triggered = _resolve_scalar(
        None,
        latest_trace_payload,
        current_turn,
        "auto_sync_triggered",
    )
    resolved_auto_sync_reason = _resolve_scalar(
        None,
        latest_trace_payload,
        current_turn,
        "auto_sync_reason",
    )
    resolved_auto_sync_summary = _resolve_mapping(
        None,
        latest_trace_payload,
        current_turn,
        "auto_sync_summary",
    )
    resolved_hybrid_refresh_triggered = _resolve_scalar(
        hybrid_refresh_triggered,
        latest_trace_payload,
        current_turn,
        "hybrid_refresh_triggered",
    )
    resolved_hybrid_refresh_sources = _resolve_list(
        hybrid_refresh_sources,
        latest_trace_payload,
        current_turn,
        "hybrid_refresh_sources",
    )
    resolved_hybrid_refresh_completion_status = _resolve_scalar(
        hybrid_refresh_completion_status,
        latest_trace_payload,
        current_turn,
        "hybrid_refresh_completion_status",
    )
    resolved_hybrid_refresh_summary = _resolve_mapping(
        hybrid_refresh_summary,
        latest_trace_payload,
        current_turn,
        "hybrid_refresh_summary",
    )
    resolved_hybrid_refresh_snapshot_id = _resolve_scalar(
        hybrid_refresh_snapshot_id,
        latest_trace_payload,
        current_turn,
        "hybrid_refresh_snapshot_id",
    )
    resolved_hybrid_refresh_job_ids = _resolve_list(
        hybrid_refresh_job_ids,
        latest_trace_payload,
        current_turn,
        "hybrid_refresh_job_ids",
    )
    resolved_attached_job_ids = resolved_attached_shared_job_ids(
        turn=current_turn,
        run_state=run_state,
        hybrid_refresh_job_ids=resolved_hybrid_refresh_job_ids,
    )
    effective_support_basis = (
        resolved_support_basis
        if isinstance(resolved_support_basis, str)
        else (
            "external-source-verified"
            if isinstance(resolved_support_manifest_path, str) and resolved_support_manifest_path
            else "kb-grounded"
        )
    )
    effective_answer_state = (
        resolved_answer_state
        if isinstance(resolved_answer_state, str)
        else ("abstained" if effective_support_basis == "governed-boundary" else None)
    )
    if effective_answer_state is None:
        effective_answer_state = (
            "grounded"
            if effective_support_basis == "external-source-verified"
            and isinstance(resolved_support_manifest_path, str)
            and resolved_support_manifest_path
            else (
                "partially-grounded"
                if effective_support_basis == "mixed"
                and isinstance(resolved_support_manifest_path, str)
                and resolved_support_manifest_path
                else "unresolved"
            )
        )
    if (
        effective_support_basis == "external-source-verified"
        and not resolved_support_manifest_path
        and isinstance(resolved_answer_file_path, str)
        and isinstance(support_manifest_sources, list)
        and support_manifest_sources
    ):
        resolved_support_manifest_path = write_external_support_manifest(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
            answer_file_path=resolved_answer_file_path,
            support_basis=effective_support_basis,
            sources=support_manifest_sources,
            key_assertions=support_manifest_key_assertions,
            verification_notes=support_manifest_notes,
        )
    if (
        not isinstance(resolved_answer_state, str)
        and effective_support_basis == "external-source-verified"
        and isinstance(resolved_support_manifest_path, str)
        and resolved_support_manifest_path
    ):
        effective_answer_state = "grounded"
    effective_turn_snapshot = _effective_turn_snapshot(
        current_turn,
        session_ids=resolved_session_ids,
        trace_ids=effective_trace_ids,
        attached_shared_job_ids=resolved_attached_job_ids,
        log_origin=resolved_log_origin,
        question_domain=resolved_question_domain
        if isinstance(resolved_question_domain, str)
        else None,
        support_basis=effective_support_basis,
        support_manifest_path=resolved_support_manifest_path
        if isinstance(resolved_support_manifest_path, str)
        else None,
        render_inspection_required=resolved_render_inspection_required,
        inspection_scope=resolved_inspection_scope,
        preferred_channels=resolved_preferred_channels,
        used_published_channels=resolved_used_published_channels,
        published_artifacts_sufficient=resolved_published_artifacts_sufficient,
        reference_resolution=resolved_reference_resolution,
        reference_resolution_summary=resolved_reference_resolution_summary,
        source_escalation_required=resolved_source_escalation_required,
        source_escalation_reason=resolved_source_escalation_reason,
        auto_sync_triggered=resolved_auto_sync_triggered,
        auto_sync_reason=resolved_auto_sync_reason,
        auto_sync_summary=resolved_auto_sync_summary,
        hybrid_refresh_triggered=resolved_hybrid_refresh_triggered,
        hybrid_refresh_sources=resolved_hybrid_refresh_sources,
        hybrid_refresh_completion_status=resolved_hybrid_refresh_completion_status,
        hybrid_refresh_summary=resolved_hybrid_refresh_summary,
        hybrid_refresh_snapshot_id=resolved_hybrid_refresh_snapshot_id,
        hybrid_refresh_job_ids=resolved_hybrid_refresh_job_ids,
    )
    lane_c_transition = _maybe_begin_lane_c_before_commit(
        paths,
        current_turn=current_turn,
        run_id=run_id,
        latest_trace_payload=latest_trace_payload,
        effective_turn_snapshot=effective_turn_snapshot,
        inner_workflow_id=inner_workflow_id,
    )
    if lane_c_transition is not None:
        return lane_c_transition
    admissibility_gate_result = evaluate_commit_admissibility(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        turn_snapshot=effective_turn_snapshot,
        answer_file_path=(
            resolved_answer_file_path if isinstance(resolved_answer_file_path, str) else None
        ),
        answer_state=effective_answer_state,
        support_basis=effective_support_basis,
        support_manifest_path=(
            resolved_support_manifest_path
            if isinstance(resolved_support_manifest_path, str)
            else None
        ),
        trace_ids=effective_trace_ids,
    )
    if effective_trace_ids:
        record_run_event_if_present(
            paths,
            run_id=run_id,
            stage="trace",
            event_type="trace-completed",
            payload={"trace_ids": effective_trace_ids},
        )
    record_run_event_if_present(
        paths,
        run_id=run_id,
        stage="admissibility",
        event_type=(
            "admissibility-passed"
            if admissibility_gate_result["allowed"]
            else "admissibility-failed"
        ),
        payload={"issues": admissibility_gate_result.get("issues", [])},
    )
    if not admissibility_gate_result["allowed"]:
        raise ValueError(
            str(admissibility_gate_result.get("reason") or "The turn is not commit-admissible.")
        )
    updated = commit_run(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        status=status,
        answer_state=effective_answer_state,
        support_basis=effective_support_basis,
        support_manifest_path=(
            resolved_support_manifest_path
            if isinstance(resolved_support_manifest_path, str)
            else None
        ),
        answer_file_path=(
            resolved_answer_file_path if isinstance(resolved_answer_file_path, str) else None
        ),
        response_excerpt=response_excerpt,
        admissibility_gate_result=admissibility_gate_result,
        turn_updates={
            "inner_workflow_id": inner_workflow_id,
            "session_ids": resolved_session_ids,
            "trace_ids": effective_trace_ids,
            "freshness_notice": None,
            "answer_state": effective_answer_state,
            "render_inspection_required": resolved_render_inspection_required,
            "sync_requested": sync_requested,
            "question_domain": resolved_question_domain,
            "support_basis": effective_support_basis,
            "support_manifest_path": resolved_support_manifest_path,
            "source_escalation_used": source_escalation_used,
            "inspection_scope": resolved_inspection_scope,
            "preferred_channels": resolved_preferred_channels,
            "used_published_channels": resolved_used_published_channels,
            "published_artifacts_sufficient": resolved_published_artifacts_sufficient,
            "reference_resolution": resolved_reference_resolution,
            "reference_resolution_summary": resolved_reference_resolution_summary,
            "source_escalation_required": resolved_source_escalation_required,
            "source_escalation_reason": resolved_source_escalation_reason,
            "auto_sync_triggered": resolved_auto_sync_triggered,
            "auto_sync_reason": resolved_auto_sync_reason,
            "auto_sync_summary": resolved_auto_sync_summary,
            "log_origin": resolved_log_origin,
            "attached_shared_job_ids": resolved_attached_job_ids,
            "hybrid_refresh_triggered": resolved_hybrid_refresh_triggered,
            "hybrid_refresh_sources": resolved_hybrid_refresh_sources,
            "hybrid_refresh_completion_status": resolved_hybrid_refresh_completion_status,
            "hybrid_refresh_summary": resolved_hybrid_refresh_summary,
            "hybrid_refresh_snapshot_id": resolved_hybrid_refresh_snapshot_id,
            "hybrid_refresh_job_ids": resolved_hybrid_refresh_job_ids,
            "evidence_mode": evidence_mode,
            "research_depth": research_depth,
            "bundle_paths": bundle_paths or [],
        },
    )
    _sync_turn_log_artifacts(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=run_id,
        session_ids=resolved_session_ids,
        trace_ids=effective_trace_ids,
        inner_workflow_id=inner_workflow_id,
        native_turn_id=updated.get("native_turn_id")
        if isinstance(updated.get("native_turn_id"), str)
        else None,
        front_door_state=(
            updated.get("front_door_state")
            if isinstance(updated.get("front_door_state"), str)
            else resolved_front_door_state
        ),
        semantic_log_context={
            **semantic_log_context_from_record(updated),
            **semantic_log_context_fields(
                question_domain=resolved_question_domain
                if isinstance(resolved_question_domain, str)
                else None,
                support_basis=effective_support_basis,
                support_manifest_path=resolved_support_manifest_path,
            ),
        },
        log_origin=resolved_log_origin,
        answer_file_path=(
            resolved_answer_file_path
            if isinstance(resolved_answer_file_path, str)
            else None
        ),
        answer_state=effective_answer_state,
        render_inspection_required=(
            resolved_render_inspection_required
            if isinstance(resolved_render_inspection_required, bool)
            else None
        ),
        inspection_scope=resolved_inspection_scope
        if isinstance(resolved_inspection_scope, str)
        else None,
        preferred_channels=resolved_preferred_channels,
        used_published_channels=resolved_used_published_channels,
        published_artifacts_sufficient=(
            resolved_published_artifacts_sufficient
            if isinstance(resolved_published_artifacts_sufficient, bool)
            else None
        ),
        reference_resolution=resolved_reference_resolution,
        reference_resolution_summary=resolved_reference_resolution_summary
        if isinstance(resolved_reference_resolution_summary, str)
        else None,
        source_escalation_required=(
            resolved_source_escalation_required
            if isinstance(resolved_source_escalation_required, bool)
            else None
        ),
        source_escalation_reason=resolved_source_escalation_reason
        if isinstance(resolved_source_escalation_reason, str)
        else None,
        auto_sync_triggered=(
            resolved_auto_sync_triggered
            if isinstance(resolved_auto_sync_triggered, bool)
            else None
        ),
        auto_sync_reason=resolved_auto_sync_reason
        if isinstance(resolved_auto_sync_reason, str)
        else None,
        auto_sync_summary=resolved_auto_sync_summary,
        hybrid_refresh_triggered=(
            resolved_hybrid_refresh_triggered
            if isinstance(resolved_hybrid_refresh_triggered, bool)
            else None
        ),
        hybrid_refresh_sources=resolved_hybrid_refresh_sources,
        hybrid_refresh_completion_status=resolved_hybrid_refresh_completion_status
        if isinstance(resolved_hybrid_refresh_completion_status, str)
        else None,
        hybrid_refresh_summary=resolved_hybrid_refresh_summary,
    )
    refresh_runtime_projections(paths)
    record_run_event_if_present(
        paths,
        run_id=run_id,
        stage="projection",
        event_type="projection-refreshed",
        payload={"conversation_id": conversation_id, "turn_id": turn_id},
    )
    return updated

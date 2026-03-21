"""Natural-intent routing helpers for the user-facing `ask` workflow."""

from __future__ import annotations

from typing import Any

from .conversation import (
    build_log_context,
    load_turn_record,
    open_conversation_turn,
    semantic_log_context_fields,
    semantic_log_context_from_record,
    update_conversation_turn,
)
from .front_controller import question_execution_profile, write_external_support_manifest
from .interaction import (
    interaction_ingest_snapshot,
    interaction_overlay_relevance,
    maybe_reconcile_active_thread,
)
from .knowledge import preview_source_changes
from .knowledge import sync_workspace as sync_knowledge_base
from .project import (
    WorkspacePaths,
    bootstrap_state,
    knowledge_base_snapshot,
    read_json,
    write_json,
)
from .projections import refresh_runtime_projections
from .routing import tokenize_text
from .run_control import commit_run, ensure_run_for_turn, record_run_event
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
) -> None:
    update_fields = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "entry_workflow_id": "ask",
        "inner_workflow_id": inner_workflow_id,
        "native_turn_id": native_turn_id,
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
        "status": sync_result.get("status"),
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


def prepare_ask_turn(
    paths: WorkspacePaths,
    *,
    question: str,
    semantic_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Prepare one `ask` turn with routing, freshness guidance, and conversation linkage."""
    maybe_reconcile_active_thread(paths)
    opened = open_conversation_turn(paths, user_question=question, entry_workflow_id="ask")
    run_payload = ensure_run_for_turn(
        paths,
        conversation_id=opened["conversation_id"],
        turn_id=opened["turn_id"],
        user_question=question,
        entry_workflow_id="ask",
    )
    run_id = str(run_payload["run_id"])
    knowledge_base = opened["workspace_snapshot"]["knowledge_base"]
    environment_ready = bool(bootstrap_state(paths).get("editable_install")) and bool(
        paths.venv_python.exists()
    )
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

    action_required = False
    sync_suggested = False
    prefer_sync_before_answer = False
    freshness_notice = None
    auto_sync_triggered = False
    auto_sync_reason = None
    auto_sync_summary = None

    if not knowledge_base["present"] and workspace_notices_enabled and not environment_ready:
        action_required = True
        inner_workflow_id = "workspace-bootstrap"
        route_reason = (
            "A published knowledge base is missing and the workspace environment is not ready "
            "for an automatic sync."
        )
        freshness_notice = "No published knowledge base is available yet."
    elif workspace_notices_enabled and question_domain == "workspace-corpus" and environment_ready:
        should_auto_sync = False
        candidate_reason = None
        if not knowledge_base["present"]:
            should_auto_sync = True
            candidate_reason = "A published knowledge base is missing for this workspace-corpus question."
        elif knowledge_base["stale"]:
            _index_preview, _active_preview, _ambiguous_preview, preview_change_set = (
                preview_source_changes(paths)
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
                    "Relevant pending interaction-derived knowledge still awaits sync-time promotion."
                )
        sync_result = sync_knowledge_base(paths)
        auto_sync_triggered = True
        auto_sync_summary = _auto_sync_summary(sync_result)
        knowledge_base = knowledge_base_snapshot(paths)
        interaction_snapshot = interaction_ingest_snapshot(paths)
        if sync_result.get("status") in {"valid", "warnings"} and bool(sync_result.get("published")):
            freshness_notice = "The knowledge base was refreshed automatically before answering."
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
            freshness_notice = str(sync_result.get("detail") or "Automatic sync did not complete.")
    elif not knowledge_base["present"] and workspace_notices_enabled:
        action_required = True
        inner_workflow_id = "knowledge-base-sync" if environment_ready else "workspace-bootstrap"
        route_reason = (
            "A published knowledge base is missing, so the user question cannot be answered "
            "safely from current state."
        )
        freshness_notice = "No published knowledge base is available yet."
    elif knowledge_base["stale"] and workspace_notices_enabled:
        sync_suggested = True
        freshness_notice = "The published knowledge base appears stale relative to `original_doc/`."
        if needs_latest_workspace_state:
            prefer_sync_before_answer = True

    interaction_sync_suggested = bool(
        workspace_notices_enabled
        and question_domain == "workspace-corpus"
        and interaction_snapshot["pending_promotion_count"]
        and (
            interaction_relevance["has_relevant_pending_interaction"]
            or bool(interaction_snapshot.get("load_warnings"))
        )
    )

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
            "auto_sync_triggered": auto_sync_triggered,
        },
    )
    return {
        **opened,
        "run_id": run_id,
        "entry_workflow_id": "ask",
        "inner_workflow_id": inner_workflow_id,
        "question_class": question_class,
        "question_domain": question_domain,
        "route_reason": route_reason,
        "knowledge_base_missing": not knowledge_base["present"],
        "knowledge_base_stale": knowledge_base["stale"],
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
            question_class=question_class,
            question_domain=question_domain,
            support_strategy=support_strategy,
            analysis_origin=analysis_origin,
        ),
    }


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
    status: str = "completed",
) -> dict[str, Any]:
    """Complete one `ask` turn after the routed workflow finishes."""
    current_turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    run_id = (
        str(current_turn.get("active_run_id"))
        if isinstance(current_turn.get("active_run_id"), str) and current_turn.get("active_run_id")
        else None
    )
    effective_trace_ids = trace_ids or current_turn.get("trace_ids")
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
        else (
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
        turn_updates={
            "inner_workflow_id": inner_workflow_id,
            "session_ids": session_ids or [],
            "trace_ids": effective_trace_ids or [],
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
        session_ids=session_ids or [],
        trace_ids=effective_trace_ids or [],
        inner_workflow_id=inner_workflow_id,
        native_turn_id=updated.get("native_turn_id")
        if isinstance(updated.get("native_turn_id"), str)
        else None,
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
    )
    refresh_runtime_projections(paths)
    return updated

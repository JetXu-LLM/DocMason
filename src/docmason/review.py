"""Review-summary and request-audit helpers for DocMason runtime logs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any
import uuid

from .control_plane import load_shared_job, load_shared_jobs_index
from .conversation import (
    LOG_ORIGIN_EVALUATION_SUITE,
    LOG_ORIGIN_INTERACTIVE_ASK,
    current_host_identity,
    load_bound_conversation_record_for_host,
    normalize_front_door_state,
    normalize_log_origin,
    semantic_log_context_from_record,
    utc_now,
)
from .project import WorkspacePaths, read_json, write_json

RECENT_LIMIT = 10
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}
REVIEW_REQUEST_SCHEMA_VERSION = 1


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _deduplicated_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _review_request_stable_summary(summary: dict[str, Any], *, final_status: str) -> str:
    recent_conversations = summary.get("conversations", {}).get("recent", [])
    recent_query_sessions = summary.get("query_sessions", {}).get("recent", [])
    recent_retrieval_traces = summary.get("retrieval_traces", {}).get("recent", [])
    benchmark_candidates = summary.get("benchmark_candidates", {}).get("candidate_cases", [])
    return (
        "runtime-log-review "
        f"status={final_status}; "
        f"recent_conversations={len(recent_conversations) if isinstance(recent_conversations, list) else 0}; "
        f"recent_query_sessions={len(recent_query_sessions) if isinstance(recent_query_sessions, list) else 0}; "
        f"recent_retrieval_traces={len(recent_retrieval_traces) if isinstance(recent_retrieval_traces, list) else 0}; "
        f"candidate_cases={len(benchmark_candidates) if isinstance(benchmark_candidates, list) else 0}"
    )


def _latest_runtime_review_turn(paths: WorkspacePaths) -> dict[str, Any]:
    host_identity = current_host_identity()
    bound = load_bound_conversation_record_for_host(paths, host_identity=host_identity)
    turns = bound.get("turns", [])
    if not isinstance(turns, list):
        return {}
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        if normalize_front_door_state(turn.get("front_door_state")) != "canonical-ask":
            continue
        question_class = str(turn.get("question_class") or "")
        inner_workflow_id = str(turn.get("inner_workflow_id") or "")
        if question_class == "runtime-review" or inner_workflow_id == "runtime-log-review":
            return {
                "conversation_id": bound.get("conversation_id"),
                "turn_id": turn.get("turn_id"),
                "run_id": turn.get("active_run_id") or turn.get("committed_run_id"),
                "request_text": turn.get("user_question"),
                "question_class": turn.get("question_class"),
                "question_domain": turn.get("question_domain"),
                "host_provider": host_identity.get("host_provider"),
                "host_thread_ref": host_identity.get("host_thread_ref"),
                "host_identity_source": host_identity.get("host_identity_source"),
            }
    return {
        "host_provider": host_identity.get("host_provider"),
        "host_thread_ref": host_identity.get("host_thread_ref"),
        "host_identity_source": host_identity.get("host_identity_source"),
    }


def _review_consulted_runtime_ids(summary: dict[str, Any]) -> dict[str, list[str]]:
    consulted_conversation_ids: list[str] = []
    consulted_turn_refs: list[str] = []
    consulted_run_ids: list[str] = []
    consulted_session_ids: list[str] = []
    consulted_trace_ids: list[str] = []

    for item in summary.get("committed_turns", {}).get("recent", []):
        if not isinstance(item, dict):
            continue
        conversation_id = _nonempty_string(item.get("conversation_id"))
        turn_id = _nonempty_string(item.get("turn_id"))
        run_id = _nonempty_string(item.get("run_id"))
        if conversation_id is not None:
            consulted_conversation_ids.append(conversation_id)
        if conversation_id is not None and turn_id is not None:
            consulted_turn_refs.append(f"{conversation_id}:{turn_id}")
        if run_id is not None:
            consulted_run_ids.append(run_id)

    for item in summary.get("query_sessions", {}).get("recent", []):
        if not isinstance(item, dict):
            continue
        conversation_id = _nonempty_string(item.get("conversation_id"))
        turn_id = _nonempty_string(item.get("turn_id"))
        session_id = _nonempty_string(item.get("session_id"))
        if conversation_id is not None:
            consulted_conversation_ids.append(conversation_id)
        if conversation_id is not None and turn_id is not None:
            consulted_turn_refs.append(f"{conversation_id}:{turn_id}")
        if session_id is not None:
            consulted_session_ids.append(session_id)

    for item in summary.get("retrieval_traces", {}).get("recent", []):
        if not isinstance(item, dict):
            continue
        conversation_id = _nonempty_string(item.get("conversation_id"))
        turn_id = _nonempty_string(item.get("turn_id"))
        trace_id = _nonempty_string(item.get("trace_id"))
        session_id = _nonempty_string(item.get("session_id"))
        if conversation_id is not None:
            consulted_conversation_ids.append(conversation_id)
        if conversation_id is not None and turn_id is not None:
            consulted_turn_refs.append(f"{conversation_id}:{turn_id}")
        if trace_id is not None:
            consulted_trace_ids.append(trace_id)
        if session_id is not None:
            consulted_session_ids.append(session_id)

    return {
        "consulted_conversation_ids": _deduplicated_strings(consulted_conversation_ids),
        "consulted_turn_refs": _deduplicated_strings(consulted_turn_refs),
        "consulted_run_ids": _deduplicated_strings(consulted_run_ids),
        "consulted_session_ids": _deduplicated_strings(consulted_session_ids),
        "consulted_trace_ids": _deduplicated_strings(consulted_trace_ids),
    }


def record_runtime_review_request(
    paths: WorkspacePaths,
    *,
    summary: dict[str, Any],
    final_status: str,
    request_text: str | None = None,
    entry_surface: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Persist one replayable request-level audit artifact for runtime review work."""
    inferred_context = _latest_runtime_review_turn(paths)
    request_id = str(uuid.uuid4())
    stable_summary = _review_request_stable_summary(summary, final_status=final_status)
    effective_conversation_id = (
        _nonempty_string(conversation_id) or _nonempty_string(inferred_context.get("conversation_id"))
    )
    effective_turn_id = _nonempty_string(turn_id) or _nonempty_string(inferred_context.get("turn_id"))
    effective_run_id = _nonempty_string(run_id) or _nonempty_string(inferred_context.get("run_id"))
    effective_request_text = (
        _nonempty_string(request_text) or _nonempty_string(inferred_context.get("request_text"))
    )
    effective_entry_surface = _nonempty_string(entry_surface)
    if effective_entry_surface is None:
        effective_entry_surface = (
            "ask/runtime-log-review" if effective_turn_id is not None else "workflow/runtime-log-review"
        )
    consulted_ids = _review_consulted_runtime_ids(summary)
    artifact = {
        "schema_version": REVIEW_REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "recorded_at": utc_now(),
        "entry_surface": effective_entry_surface,
        "stable_summary": stable_summary,
        "final_status": final_status,
        "host_provider": _nonempty_string(inferred_context.get("host_provider")),
        "host_thread_ref": _nonempty_string(inferred_context.get("host_thread_ref")),
        "host_identity_source": _nonempty_string(inferred_context.get("host_identity_source")),
        "conversation_id": effective_conversation_id,
        "turn_id": effective_turn_id,
        "run_id": effective_run_id,
        "request_text": effective_request_text,
        "derived_output_paths": [
            str(paths.review_summary_path.relative_to(paths.root)),
            str(paths.benchmark_candidates_path.relative_to(paths.root)),
            str(paths.answer_history_index_path.relative_to(paths.root)),
        ],
        "review_summary_generated_at": _nonempty_string(summary.get("generated_at")),
        **consulted_ids,
    }
    artifact_path = paths.review_requests_dir / f"{request_id}.json"
    write_json(artifact_path, artifact)
    artifact["artifact_path"] = str(artifact_path.relative_to(paths.root))
    return artifact


def _load_log_payloads(directory: Path) -> list[dict[str, Any]]:
    payloads = [read_json(path) for path in sorted(directory.glob("*.json"))]
    return [payload for payload in payloads if payload]


def _recorded_at(payload: dict[str, Any]) -> str:
    recorded_at = payload.get("recorded_at")
    if isinstance(recorded_at, str):
        return recorded_at
    return ""


def _record_has_canonical_ask_ownership(record: dict[str, Any] | None) -> bool:
    if not isinstance(record, dict):
        return False
    return (
        record.get("entry_workflow_id") == "ask"
        and normalize_front_door_state(record.get("front_door_state")) == "canonical-ask"
    )


def _conversation_has_canonical_truth(payload: dict[str, Any]) -> bool:
    turns = payload.get("turns", [])
    if not isinstance(turns, list):
        return False
    return any(
        _record_has_canonical_ask_ownership(turn)
        for turn in turns
        if isinstance(turn, dict)
    )


def _record_log_origin(record: dict[str, Any] | None) -> str | None:
    if not isinstance(record, dict):
        return None
    return normalize_log_origin(record.get("log_origin"))


def _is_synthetic_turn(turn: dict[str, Any] | None) -> bool:
    return _record_log_origin(turn) == LOG_ORIGIN_EVALUATION_SUITE


def _conversation_has_real_canonical_truth(payload: dict[str, Any]) -> bool:
    turns = payload.get("turns", [])
    if not isinstance(turns, list):
        return False
    return any(
        _record_has_canonical_ask_ownership(turn) and not _is_synthetic_turn(turn)
        for turn in turns
        if isinstance(turn, dict)
    )


def _effective_log_origin(
    payload: dict[str, Any],
    *,
    linked_turn: dict[str, Any] | None = None,
) -> str | None:
    explicit = _record_log_origin(payload)
    if explicit is not None:
        return explicit
    linked_turn_origin = _record_log_origin(linked_turn)
    if linked_turn_origin is not None:
        return linked_turn_origin
    if _record_has_canonical_ask_ownership(payload) or _record_has_canonical_ask_ownership(
        linked_turn
    ):
        return LOG_ORIGIN_INTERACTIVE_ASK
    if isinstance(payload.get("conversation_id"), str):
        return "workflow-linked"
    return None


def _is_synthetic_runtime_record(
    payload: dict[str, Any],
    *,
    linked_turn: dict[str, Any] | None = None,
) -> bool:
    return _effective_log_origin(payload, linked_turn=linked_turn) == LOG_ORIGIN_EVALUATION_SUITE


def _merged_semantic_context(*records: dict[str, Any] | None) -> dict[str, str]:
    """Merge flat semantic context from linked runtime records."""
    merged: dict[str, str] = {}
    for record in records:
        merged.update(semantic_log_context_from_record(record))
    return merged


def _compact_query_session(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "log_origin": _effective_log_origin(payload),
        "conversation_id": payload.get("conversation_id"),
        "turn_id": payload.get("turn_id"),
        "native_turn_id": payload.get("native_turn_id"),
        "session_id": payload.get("session_id"),
        "recorded_at": payload.get("recorded_at"),
        "command": payload.get("command"),
        "status": payload.get("status"),
        "query": payload.get("query"),
        "entry_workflow_id": payload.get("entry_workflow_id"),
        "inner_workflow_id": payload.get("inner_workflow_id"),
        **semantic_log_context_from_record(payload),
        "answer_file_path": payload.get("answer_file_path"),
        "trace_id": payload.get("trace_id"),
        "answer_state": payload.get("answer_state"),
        "inspection_scope": payload.get("inspection_scope"),
        "preferred_channels": payload.get("preferred_channels", []),
        "used_published_channels": payload.get("used_published_channels", []),
        "published_artifacts_sufficient": payload.get("published_artifacts_sufficient"),
        "reference_resolution_summary": payload.get("reference_resolution_summary"),
        "source_escalation_required": payload.get("source_escalation_required"),
        "source_escalation_reason": payload.get("source_escalation_reason"),
        "render_inspection_required": payload.get("render_inspection_required", False),
    }


def _compact_trace_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "log_origin": _effective_log_origin(payload),
        "conversation_id": payload.get("conversation_id"),
        "turn_id": payload.get("turn_id"),
        "native_turn_id": payload.get("native_turn_id"),
        "trace_id": payload.get("trace_id"),
        "session_id": payload.get("session_id"),
        "recorded_at": payload.get("recorded_at"),
        "trace_mode": payload.get("trace_mode"),
        "status": payload.get("status"),
        "entry_workflow_id": payload.get("entry_workflow_id"),
        "inner_workflow_id": payload.get("inner_workflow_id"),
        **semantic_log_context_from_record(payload),
        "answer_file_path": payload.get("answer_file_path"),
        "answer_state": payload.get("answer_state"),
        "inspection_scope": payload.get("inspection_scope"),
        "preferred_channels": payload.get("preferred_channels", []),
        "used_published_channels": payload.get("used_published_channels", []),
        "published_artifacts_sufficient": payload.get("published_artifacts_sufficient"),
        "reference_resolution_summary": payload.get("reference_resolution_summary"),
        "source_escalation_required": payload.get("source_escalation_required"),
        "source_escalation_reason": payload.get("source_escalation_reason"),
        "render_inspection_required": payload.get("render_inspection_required", False),
        "segment_count": payload.get("segment_count"),
    }


def _compact_conversation(payload: dict[str, Any]) -> dict[str, Any]:
    turns = payload.get("turns", [])
    return {
        "conversation_id": payload.get("conversation_id"),
        "agent_surface": payload.get("agent_surface"),
        "opened_at": payload.get("opened_at"),
        "updated_at": payload.get("updated_at"),
        "turn_count": len(turns) if isinstance(turns, list) else 0,
    }


def _projection_conversations(paths: WorkspacePaths) -> list[dict[str, Any]]:
    return sorted(
        [
            payload
            for payload in _load_log_payloads(paths.conversation_projections_dir)
            if _conversation_has_real_canonical_truth(payload)
        ],
        key=lambda payload: str(payload.get("updated_at") or ""),
        reverse=True,
    )


def _load_conversations(paths: WorkspacePaths) -> dict[tuple[str, str], dict[str, Any]]:
    conversations: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in _load_log_payloads(paths.conversations_dir):
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        turns = payload.get("turns", [])
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            if not _record_has_canonical_ask_ownership(turn):
                continue
            if _is_synthetic_turn(turn):
                continue
            turn_id = turn.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                conversations[(conversation_id, turn_id)] = {
                    "conversation_id": conversation_id,
                    "conversation_path": str(
                        paths.conversations_dir.joinpath(f"{conversation_id}.json").relative_to(
                            paths.root
                        )
                    ),
                    "turn": turn,
                }
    return conversations


def _committed_canonical_turn_artifacts(
    paths: WorkspacePaths,
) -> tuple[
    dict[tuple[str, str], dict[str, Any]],
    set[str],
    set[str],
]:
    """Return committed canonical turns plus the session and trace ids they own."""
    turn_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    owned_session_ids: set[str] = set()
    owned_trace_ids: set[str] = set()
    for payload in _load_log_payloads(paths.conversations_dir):
        conversation_id = payload.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        turns = payload.get("turns", [])
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            if not _record_has_canonical_ask_ownership(turn) or _is_synthetic_turn(turn):
                continue
            turn_id = turn.get("turn_id")
            committed_run_id = turn.get("committed_run_id")
            if (
                not isinstance(turn_id, str)
                or not turn_id
                or not isinstance(committed_run_id, str)
                or not committed_run_id
            ):
                continue
            key = (conversation_id, turn_id)
            turn_lookup[key] = turn
            support = resolve_canonical_turn_support(paths, turn=turn)
            owned_session_ids.update(support["session_ids"])
            owned_trace_ids.update(support["trace_ids"])
    return turn_lookup, owned_session_ids, owned_trace_ids


def _payload_is_noncanonical_leftover(
    payload: dict[str, Any],
    *,
    committed_turn_lookup: dict[tuple[str, str], dict[str, Any]],
    owned_session_ids: set[str],
    owned_trace_ids: set[str],
) -> bool:
    conversation_id = payload.get("conversation_id")
    turn_id = payload.get("turn_id")
    if not isinstance(conversation_id, str) or not isinstance(turn_id, str):
        return False
    if (conversation_id, turn_id) not in committed_turn_lookup:
        return False
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id not in owned_session_ids
    trace_id = payload.get("trace_id")
    if isinstance(trace_id, str) and trace_id:
        return trace_id not in owned_trace_ids
    return False


def _load_native_ledgers(paths: WorkspacePaths) -> list[dict[str, Any]]:
    if not paths.native_ledger_dir.exists():
        return []
    ledgers = [read_json(path) for path in sorted(paths.native_ledger_dir.glob("*.json"))]
    return [payload for payload in ledgers if payload]


def _compact_native_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    turns = payload.get("turns", [])
    latest_turn = turns[-1] if isinstance(turns, list) and turns else {}
    host_identity = payload.get("host_identity")
    closure = latest_turn.get("closure") if isinstance(latest_turn, dict) else {}
    operator_evidence = (
        latest_turn.get("operator_evidence") if isinstance(latest_turn, dict) else {}
    )
    return {
        "ledger_id": payload.get("ledger_id"),
        "host_provider": host_identity.get("host_provider")
        if isinstance(host_identity, dict)
        else None,
        "host_thread_ref": host_identity.get("host_thread_ref")
        if isinstance(host_identity, dict)
        else None,
        "host_identity_trust": host_identity.get("host_identity_trust")
        if isinstance(host_identity, dict)
        else None,
        "anomaly_flags": (
            [
                value
                for value in host_identity.get("anomaly_flags", [])
                if isinstance(value, str) and value
            ]
            if isinstance(host_identity, dict)
            else []
        ),
        "updated_at": payload.get("updated_at"),
        "turn_count": len(turns) if isinstance(turns, list) else 0,
        "latest_native_turn_id": latest_turn.get("native_turn_id")
        if isinstance(latest_turn, dict)
        else None,
        "latest_question_class": latest_turn.get("question_class")
        if isinstance(latest_turn, dict)
        else None,
        "latest_question_domain": latest_turn.get("question_domain")
        if isinstance(latest_turn, dict)
        else None,
        "latest_route_reason": latest_turn.get("route_reason")
        if isinstance(latest_turn, dict)
        else None,
        "latest_closure_status": closure.get("status")
        if isinstance(closure, dict)
        else None,
        "latest_closure_source": closure.get("source")
        if isinstance(closure, dict)
        else None,
        "latest_operator_evidence_status": operator_evidence.get("status")
        if isinstance(operator_evidence, dict)
        else None,
        "latest_operator_evidence_classification": operator_evidence.get("classification")
        if isinstance(operator_evidence, dict)
        else None,
        "latest_operator_evidence_detail": operator_evidence.get("detail")
        if isinstance(operator_evidence, dict)
        else None,
    }


def _native_turn_recorded_at(payload: dict[str, Any], turn: dict[str, Any]) -> str:
    for field_name in ("completed_at", "opened_at", "updated_at"):
        value = turn.get(field_name)
        if isinstance(value, str) and value:
            return value
    updated_at = payload.get("updated_at")
    return updated_at if isinstance(updated_at, str) else ""


def _compact_native_ledger_turn(payload: dict[str, Any], turn: dict[str, Any]) -> dict[str, Any]:
    host_identity = payload.get("host_identity")
    closure = turn.get("closure") if isinstance(turn.get("closure"), dict) else {}
    operator_evidence = (
        turn.get("operator_evidence") if isinstance(turn.get("operator_evidence"), dict) else {}
    )
    return {
        "ledger_id": payload.get("ledger_id"),
        "host_provider": host_identity.get("host_provider")
        if isinstance(host_identity, dict)
        else None,
        "host_thread_ref": host_identity.get("host_thread_ref")
        if isinstance(host_identity, dict)
        else None,
        "host_identity_trust": host_identity.get("host_identity_trust")
        if isinstance(host_identity, dict)
        else None,
        "anomaly_flags": (
            [
                value
                for value in host_identity.get("anomaly_flags", [])
                if isinstance(value, str) and value
            ]
            if isinstance(host_identity, dict)
            else []
        ),
        "recorded_at": _native_turn_recorded_at(payload, turn),
        "native_turn_id": (
            turn.get("native_turn_id") if isinstance(turn.get("native_turn_id"), str) else None
        ),
        "question_class": (
            turn.get("question_class") if isinstance(turn.get("question_class"), str) else None
        ),
        "question_domain": (
            turn.get("question_domain") if isinstance(turn.get("question_domain"), str) else None
        ),
        "route_reason": (
            turn.get("route_reason") if isinstance(turn.get("route_reason"), str) else None
        ),
        "closure_status": closure.get("status") if isinstance(closure, dict) else None,
        "closure_source": closure.get("source") if isinstance(closure, dict) else None,
        "operator_evidence_status": (
            operator_evidence.get("status") if isinstance(operator_evidence, dict) else None
        ),
        "operator_evidence_classification": (
            operator_evidence.get("classification")
            if isinstance(operator_evidence, dict)
            else None
        ),
        "operator_evidence_detail": (
            operator_evidence.get("detail") if isinstance(operator_evidence, dict) else None
        ),
        "captured_interaction_id": (
            turn.get("captured_interaction_id")
            if isinstance(turn.get("captured_interaction_id"), str)
            else None
        ),
    }


def _native_ledger_turn_bucket(
    payload: dict[str, Any],
    *,
    classifications: set[str],
) -> list[dict[str, Any]]:
    turns = payload.get("turns", [])
    if not isinstance(turns, list):
        return []
    matched_turns = [
        _compact_native_ledger_turn(payload, turn)
        for turn in turns
        if isinstance(turn, dict)
        and isinstance(turn.get("operator_evidence"), dict)
        and turn["operator_evidence"].get("classification") in classifications
    ]
    return sorted(
        matched_turns,
        key=lambda item: str(item.get("recorded_at") or ""),
        reverse=True,
    )


def _run_commit_payload(paths: WorkspacePaths, run_id: str | None) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id:
        return {}
    payload = read_json(paths.runs_dir / run_id / "commit.json")
    if payload:
        return payload
    return read_json(paths.runs_dir / run_id / "state.json")


def resolve_canonical_turn_support(
    paths: WorkspacePaths,
    *,
    turn: dict[str, Any],
) -> dict[str, Any]:
    """Return the final canonical trace-owned support set for one committed turn."""
    trace_ids = [
        value for value in turn.get("trace_ids", []) if isinstance(value, str) and value
    ]
    session_ids: list[str] = []
    supporting_source_ids: list[str] = []
    supporting_unit_ids: list[str] = []
    supporting_artifact_ids: list[str] = []
    canonical_support_summary: dict[str, Any] | None = None
    for trace_id in trace_ids:
        payload = read_json(paths.retrieval_traces_dir / f"{trace_id}.json")
        if not payload:
            continue
        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            session_ids.append(session_id)
        supporting_source_ids.extend(
            value
            for value in payload.get("supporting_source_ids", [])
            if isinstance(value, str) and value
        )
        supporting_unit_ids.extend(
            value
            for value in payload.get("supporting_unit_ids", [])
            if isinstance(value, str) and value
        )
        supporting_artifact_ids.extend(
            value
            for value in payload.get("supporting_artifact_ids", [])
            if isinstance(value, str) and value
        )
        if isinstance(payload.get("canonical_support_summary"), dict):
            canonical_support_summary = dict(payload["canonical_support_summary"])
    return {
        "trace_ids": list(dict.fromkeys(trace_ids)),
        "session_ids": list(dict.fromkeys(session_ids)),
        "supporting_source_ids": list(dict.fromkeys(supporting_source_ids)),
        "supporting_unit_ids": list(dict.fromkeys(supporting_unit_ids)),
        "supporting_artifact_ids": list(dict.fromkeys(supporting_artifact_ids)),
        "canonical_support_summary": canonical_support_summary or {},
    }


def _question_is_mixed_language(text: str) -> bool:
    has_latin = any("a" <= char.lower() <= "z" for char in text)
    has_cjk = any("\u4e00" <= char <= "\u9fff" for char in text)
    return has_latin and has_cjk


def _question_mentions_ambiguity(text: str) -> bool:
    normalized = text.lower()
    markers = (
        "contradiction",
        "contradict",
        "inconsistent",
        "ambigu",
        "final negotiated",
        "award decision",
        "exact dependency order",
        "矛盾",
        "冲突",
        "歧义",
        "最终合同",
        "最终中标",
        "精确顺序",
    )
    return any(marker in normalized for marker in markers)


def _support_basis(payload: dict[str, Any]) -> str | None:
    value = payload.get("support_basis")
    if isinstance(value, str) and value:
        return value
    return None


def _is_external_verified_success(payload: dict[str, Any]) -> bool:
    return (
        _support_basis(payload) == "external-source-verified"
        and payload.get("status") == "ready"
    )


def _suggest_benchmark_family(*, question: str, payload: dict[str, Any]) -> str:
    if _is_external_verified_success(payload):
        return "external-source-verified-answer"
    if payload.get("render_inspection_required"):
        return "render-required-visual-evidence"
    if _question_is_mixed_language(question):
        return "mixed-language-query"
    if payload.get("status") == "no-results":
        return "insufficient-or-unanswerable"
    if _question_mentions_ambiguity(question):
        return "ambiguity-or-contradiction"
    if payload.get("answer_state") in {"partially-grounded", "unresolved"}:
        return "degraded-grounded-answer"
    return "retrieval-or-trace-review"


def _suggest_feedback_tags(*, question: str, payload: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if _is_external_verified_success(payload):
        return tags
    if payload.get("status") == "no-results":
        tags.extend(["retrieval_miss", "should_abstain"])
    answer_state = payload.get("answer_state")
    if answer_state == "unresolved":
        tags.append("unsupported_synthesis")
    if answer_state == "partially-grounded":
        tags.extend(["coverage_gap", "incomplete_citation"])
    if payload.get("render_inspection_required"):
        tags.append("render_required")
    if _question_mentions_ambiguity(question):
        tags.append("contradiction_missed")
    if _question_is_mixed_language(question):
        tags.append("coverage_gap")
    return list(dict.fromkeys(tags))


def _feedback_records(paths: WorkspacePaths) -> list[dict[str, Any]]:
    return sorted(
        _load_log_payloads(paths.user_feedback_dir),
        key=_recorded_at,
        reverse=True,
    )


def _candidate_priority(
    *,
    conversation_id: str | None,
    log_origin: str | None,
    feedback_match_count: int,
    source_scope_satisfied: bool,
    render_required: bool,
    degraded_answer_state: str | None,
) -> str:
    if (
        source_scope_satisfied
        and render_required is False
        and degraded_answer_state != "unresolved"
    ):
        base = "low"
    elif conversation_id and log_origin == "interactive-ask":
        base = "high"
    elif conversation_id:
        base = "medium"
    else:
        base = "low"
    if feedback_match_count <= 0:
        return base
    if base == "low":
        return "medium"
    return "high"


def _candidate_severity(payload: dict[str, Any]) -> int:
    if payload.get("render_inspection_required"):
        return 3
    if payload.get("status") == "no-results":
        return 3
    if payload.get("answer_state") == "unresolved":
        return 2
    if payload.get("answer_state") == "partially-grounded":
        return 1
    return 0


def build_benchmark_candidates(
    paths: WorkspacePaths,
    *,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build candidate benchmark suggestions from runtime logs and conversation turns."""
    review_summary = summary or read_json(paths.review_summary_path)
    committed_turn_lookup, owned_session_ids, owned_trace_ids = _committed_canonical_turn_artifacts(
        paths
    )
    query_sessions = sorted(
        _load_log_payloads(paths.query_sessions_dir),
        key=_recorded_at,
        reverse=True,
    )
    retrieval_traces = sorted(
        _load_log_payloads(paths.retrieval_traces_dir),
        key=_recorded_at,
        reverse=True,
    )
    conversation_lookup = _load_conversations(paths)
    summary_candidate_cases = review_summary.get("query_sessions", {}).get("candidate_cases", [])
    summary_case_lookup: dict[tuple[str | None, str | None], dict[str, Any]] = {}
    if isinstance(summary_candidate_cases, list):
        for item in summary_candidate_cases:
            if not isinstance(item, dict):
                continue
            key = (
                item.get("session_id") if isinstance(item.get("session_id"), str) else None,
                item.get("trace_id") if isinstance(item.get("trace_id"), str) else None,
            )
            summary_case_lookup[key] = item
    feedback_records = _feedback_records(paths)
    feedback_by_session: dict[str, list[dict[str, Any]]] = {}
    feedback_by_trace: dict[str, list[dict[str, Any]]] = {}
    for record in feedback_records:
        session_id = record.get("session_id")
        trace_id = record.get("trace_id")
        if isinstance(session_id, str) and session_id:
            feedback_by_session.setdefault(session_id, []).append(record)
        if isinstance(trace_id, str) and trace_id:
            feedback_by_trace.setdefault(trace_id, []).append(record)

    grouped: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
    for payload in [*query_sessions, *retrieval_traces]:
        if _payload_is_noncanonical_leftover(
            payload,
            committed_turn_lookup=committed_turn_lookup,
            owned_session_ids=owned_session_ids,
            owned_trace_ids=owned_trace_ids,
        ):
            continue
        conversation_id = payload.get("conversation_id")
        turn_id = payload.get("turn_id")
        session_id = (
            payload.get("session_id")
            if isinstance(payload.get("session_id"), str)
            else None
        )
        trace_id = payload.get("trace_id") if isinstance(payload.get("trace_id"), str) else None
        conversation_record = None
        if isinstance(conversation_id, str) and isinstance(turn_id, str):
            conversation_record = conversation_lookup.get((conversation_id, turn_id))
            if (
                isinstance(conversation_record, dict)
                and not isinstance(
                    conversation_record["turn"].get("committed_run_id"),
                    str,
                )
            ):
                continue
        effective_log_origin = _effective_log_origin(
            payload,
            linked_turn=(
                conversation_record["turn"] if isinstance(conversation_record, dict) else None
            ),
        )
        if effective_log_origin == LOG_ORIGIN_EVALUATION_SUITE:
            continue
        if _is_external_verified_success(payload):
            continue
        status = payload.get("status")
        trace_mode = payload.get("trace_mode")
        if (
            status not in {"no-results", "degraded"}
            and not payload.get("render_inspection_required")
        ):
            continue
        if trace_mode == "citation-first" and status == "ready":
            continue
        question = payload.get("query")
        if not isinstance(question, str) or not question:
            question = (
                conversation_record["turn"].get("user_question")
                if isinstance(conversation_record, dict)
                else None
            )
        if not isinstance(question, str) or not question:
            question = payload.get("final_answer") or payload.get("answer_text") or ""
        if not isinstance(question, str) or not question:
            continue
        group_key: tuple[str | None, str | None, str | None]
        candidate_source = (
            "committed-turn"
            if isinstance(conversation_record, dict)
            else "audit-leftover"
        )
        if isinstance(conversation_id, str) and isinstance(turn_id, str):
            group_key = (conversation_id, turn_id, None)
        else:
            group_key = (
                conversation_id if isinstance(conversation_id, str) else None,
                turn_id if isinstance(turn_id, str) else None,
                str(trace_id or session_id or payload.get("recorded_at") or ""),
            )
        group = grouped.get(group_key)
        if group is None:
            canonical_support = (
                resolve_canonical_turn_support(paths, turn=conversation_record["turn"])
                if isinstance(conversation_record, dict)
                else {
                    "trace_ids": [],
                    "session_ids": [],
                    "supporting_source_ids": [],
                    "supporting_unit_ids": [],
                    "supporting_artifact_ids": [],
                }
            )
            group = {
                "candidate_id": (
                    f"candidate-{conversation_id}-{turn_id}"
                    if isinstance(conversation_id, str) and isinstance(turn_id, str)
                    else f"candidate-{trace_id or session_id}"
                ),
                "recorded_at": payload.get("recorded_at"),
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "run_id": payload.get("run_id")
                if isinstance(payload.get("run_id"), str)
                else (
                    conversation_record["turn"].get("committed_run_id")
                    if isinstance(conversation_record, dict)
                    and isinstance(conversation_record["turn"].get("committed_run_id"), str)
                    else None
                ),
                "session_ids": [],
                "trace_ids": [],
                "original_user_question": question,
                "routed_workflow": payload.get("entry_workflow_id") or payload.get("command"),
                "inner_workflow_id": payload.get("inner_workflow_id"),
                "requires_render_inspection": False,
                "log_origin": effective_log_origin,
                "candidate_source": candidate_source,
                "support_basis": payload.get("support_basis"),
                "question_context": _merged_semantic_context(
                    payload,
                    conversation_record["turn"] if isinstance(conversation_record, dict) else None,
                ),
                "reason": None,
                "severity": -1,
                "feedback_tags": [],
                "feedback_match_count": 0,
                "reference_resolution_summary": None,
                "canonical_support": canonical_support,
                "candidate_gate_reason": None,
            }
            if isinstance(conversation_record, dict):
                group["conversation_path"] = conversation_record["conversation_path"]
                group["answer_file_path"] = conversation_record["turn"].get("answer_file_path")
                group["bundle_paths"] = conversation_record["turn"].get("bundle_paths", [])
            grouped[group_key] = group
        group["question_context"].update(
            _merged_semantic_context(
                payload,
                conversation_record["turn"] if isinstance(conversation_record, dict) else None,
            )
        )
        canonical_support = group.get("canonical_support", {})
        canonical_session_ids = canonical_support.get("session_ids", [])
        canonical_trace_ids = canonical_support.get("trace_ids", [])
        if session_id and (
            not isinstance(conversation_record, dict) or session_id in canonical_session_ids
        ):
            group["session_ids"].append(session_id)
            group["feedback_match_count"] += len(feedback_by_session.get(session_id, []))
            for record in feedback_by_session.get(session_id, []):
                tags = record.get("feedback_tags", [])
                if isinstance(tags, list):
                    group["feedback_tags"].extend(tag for tag in tags if isinstance(tag, str))
        if trace_id and (
            not isinstance(conversation_record, dict) or trace_id in canonical_trace_ids
        ):
            group["trace_ids"].append(trace_id)
            group["feedback_match_count"] += len(feedback_by_trace.get(trace_id, []))
            for record in feedback_by_trace.get(trace_id, []):
                tags = record.get("feedback_tags", [])
                if isinstance(tags, list):
                    group["feedback_tags"].extend(tag for tag in tags if isinstance(tag, str))
        severity = _candidate_severity(payload)
        family = _suggest_benchmark_family(question=question, payload=payload)
        feedback_tags = _suggest_feedback_tags(question=question, payload=payload)
        if severity >= group["severity"]:
            group["severity"] = severity
            group["suggested_benchmark_family"] = family
            group["suggested_expected_status"] = status
            group["suggested_expected_answer_state"] = payload.get("answer_state")
            group["requires_render_inspection"] = bool(payload.get("render_inspection_required"))
            group["support_basis"] = payload.get("support_basis")
            reference_summary = payload.get("reference_resolution_summary")
            if not isinstance(reference_summary, str) and isinstance(conversation_record, dict):
                reference_summary = conversation_record["turn"].get("reference_resolution_summary")
            if isinstance(reference_summary, str):
                group["reference_resolution_summary"] = reference_summary
            summary_case = summary_case_lookup.get((session_id, trace_id))
            if isinstance(summary_case, dict) and isinstance(summary_case.get("reason"), str):
                group["reason"] = summary_case["reason"]
            elif status == "no-results":
                group["reason"] = "The turn produced a no-results boundary."
            else:
                group["reason"] = (
                    "The turn produced a degraded or render-required outcome worth future replay."
                )
        group["feedback_tags"].extend(feedback_tags)

    candidates: list[dict[str, Any]] = []
    for group in grouped.values():
        session_ids = list(
            dict.fromkeys(item for item in group["session_ids"] if isinstance(item, str))
        )
        trace_ids = list(
            dict.fromkeys(item for item in group["trace_ids"] if isinstance(item, str))
        )
        canonical_support = (
            group["canonical_support"] if isinstance(group.get("canonical_support"), dict) else {}
        )
        canonical_support_summary: dict[str, Any] = (
            dict(canonical_support["canonical_support_summary"])
            if isinstance(canonical_support.get("canonical_support_summary"), dict)
            else {}
        )
        candidate_priority = _candidate_priority(
            conversation_id=(
                group["conversation_id"]
                if isinstance(group["conversation_id"], str)
                else None
            ),
            log_origin=group["log_origin"] if isinstance(group["log_origin"], str) else None,
            feedback_match_count=int(group["feedback_match_count"]),
            source_scope_satisfied=bool(
                canonical_support_summary.get("source_scope_satisfied")
            ),
            render_required=bool(group["requires_render_inspection"]),
            degraded_answer_state=(
                str(group.get("suggested_expected_answer_state"))
                if isinstance(group.get("suggested_expected_answer_state"), str)
                else None
            ),
        )
        should_admit = bool(group["requires_render_inspection"]) or (
            str(group.get("suggested_expected_answer_state") or "") == "unresolved"
        )
        if not should_admit and _question_mentions_ambiguity(str(group["original_user_question"])):
            should_admit = True
            group["candidate_gate_reason"] = "ambiguity-or-contradiction"
        if (
            not should_admit
            and group.get("support_basis") == "mixed"
            and not bool(canonical_support_summary.get("mixed_support_explainable"))
        ):
            should_admit = True
            group["candidate_gate_reason"] = "mixed-support-unexplained"
        if not should_admit and int(group["feedback_match_count"]) > 0:
            should_admit = True
            group["candidate_gate_reason"] = "explicit-feedback"
        if not should_admit:
            continue
        candidate: dict[str, Any] = {
            "candidate_id": group["candidate_id"],
            "recorded_at": group["recorded_at"],
            "conversation_id": group["conversation_id"],
            "turn_id": group["turn_id"],
            "run_id": group.get("run_id"),
            "session_id": session_ids[0] if session_ids else None,
            "trace_id": trace_ids[0] if trace_ids else None,
            "session_ids": session_ids,
            "trace_ids": trace_ids,
            "original_user_question": group["original_user_question"],
            "routed_workflow": group["routed_workflow"],
            "inner_workflow_id": group["inner_workflow_id"],
            "suggested_benchmark_family": group.get("suggested_benchmark_family"),
            "suggested_expected_status": group.get("suggested_expected_status"),
            "suggested_expected_answer_state": group.get("suggested_expected_answer_state"),
            "suggested_feedback_tags": list(
                dict.fromkeys(tag for tag in group["feedback_tags"] if isinstance(tag, str))
            ),
            "requires_render_inspection": bool(group["requires_render_inspection"]),
            "candidate_priority": candidate_priority,
            "feedback_match_count": int(group["feedback_match_count"]),
            "log_origin": group["log_origin"],
            "reason": group["reason"],
            "support_basis": group.get("support_basis"),
            "candidate_source": group.get("candidate_source"),
            "candidate_gate_reason": group.get("candidate_gate_reason"),
            **group["question_context"],
        }
        if (
            isinstance(group.get("candidate_source"), str)
            and group["candidate_source"] == "committed-turn"
        ):
            candidate["supporting_source_ids"] = canonical_support.get("supporting_source_ids", [])
            candidate["supporting_unit_ids"] = canonical_support.get("supporting_unit_ids", [])
            candidate["supporting_artifact_ids"] = canonical_support.get(
                "supporting_artifact_ids",
                [],
            )
            candidate["canonical_support_summary"] = canonical_support_summary
            candidate["source_scope_satisfied"] = canonical_support_summary.get(
                "source_scope_satisfied"
            )
        if isinstance(group.get("reference_resolution_summary"), str):
            candidate["reference_resolution_summary"] = group["reference_resolution_summary"]
        if isinstance(group.get("conversation_path"), str):
            candidate["conversation_path"] = group["conversation_path"]
        if isinstance(group.get("answer_file_path"), str):
            candidate["answer_file_path"] = group["answer_file_path"]
        if isinstance(group.get("bundle_paths"), list):
            candidate["bundle_paths"] = group["bundle_paths"]
        candidates.append(candidate)
    candidates = sorted(
        candidates,
        key=lambda item: str(item.get("recorded_at") or ""),
        reverse=True,
    )
    candidates = sorted(
        candidates,
        key=lambda item: PRIORITY_ORDER.get(str(item.get("candidate_priority")), 3),
    )[:RECENT_LIMIT]
    return {
        "generated_at": max(
            (str(item.get("recorded_at") or "") for item in candidates),
            default="",
        ),
        "review_summary_generated_at": review_summary.get("generated_at"),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _iter_source_unit_pairs(payload: dict[str, Any]) -> list[tuple[str, str]]:
    consulted_results = payload.get("consulted_results", [])
    pairs: list[tuple[str, str]] = []
    if not isinstance(consulted_results, list):
        return pairs
    for item in consulted_results:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("source_id"), str):
            source_id = item["source_id"]
            matched_unit_ids = item.get("matched_unit_ids", [])
            if isinstance(matched_unit_ids, list):
                pairs.extend(
                    (source_id, unit_id)
                    for unit_id in matched_unit_ids
                    if isinstance(unit_id, str) and unit_id
                )
            continue
        results = item.get("results", [])
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict) or not isinstance(result.get("source_id"), str):
                continue
            source_id = result["source_id"]
            matched_unit_ids = result.get("matched_unit_ids", [])
            if not isinstance(matched_unit_ids, list):
                continue
            pairs.extend(
                (source_id, unit_id)
                for unit_id in matched_unit_ids
                if isinstance(unit_id, str) and unit_id
            )
    return pairs


def _top_counts(counter: Counter[str], *, key_name: str) -> list[dict[str, Any]]:
    return [
        {key_name: name, "count": count}
        for name, count in sorted(
            counter.items(),
            key=lambda item: (-item[1], item[0]),
        )[:RECENT_LIMIT]
    ]


def build_review_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Build a review-friendly summary over runtime query and trace logs."""
    committed_turn_lookup, owned_session_ids, owned_trace_ids = _committed_canonical_turn_artifacts(
        paths
    )
    query_sessions = sorted(
        _load_log_payloads(paths.query_sessions_dir),
        key=_recorded_at,
        reverse=True,
    )
    retrieval_traces = sorted(
        _load_log_payloads(paths.retrieval_traces_dir),
        key=_recorded_at,
        reverse=True,
    )
    live_conversation_lookup = _load_conversations(paths)
    conversations = _projection_conversations(paths)
    native_ledgers = sorted(
        _load_native_ledgers(paths),
        key=lambda payload: str(payload.get("updated_at") or ""),
        reverse=True,
    )
    real_query_sessions = [
        payload for payload in query_sessions if not _is_synthetic_runtime_record(payload)
    ]
    synthetic_query_sessions = [
        payload for payload in query_sessions if _is_synthetic_runtime_record(payload)
    ]
    real_retrieval_traces = [
        payload for payload in retrieval_traces if not _is_synthetic_runtime_record(payload)
    ]
    synthetic_retrieval_traces = [
        payload for payload in retrieval_traces if _is_synthetic_runtime_record(payload)
    ]
    orphaned_query_sessions = [
        payload
        for payload in real_query_sessions
        if not (
            isinstance(payload.get("conversation_id"), str)
            and isinstance(payload.get("turn_id"), str)
            and (payload["conversation_id"], payload["turn_id"]) in live_conversation_lookup
        )
    ]
    orphaned_retrieval_traces = [
        payload
        for payload in real_retrieval_traces
        if not (
            isinstance(payload.get("conversation_id"), str)
            and isinstance(payload.get("turn_id"), str)
            and (payload["conversation_id"], payload["turn_id"]) in live_conversation_lookup
        )
    ]
    committed_turns = sorted(
        [
            {
                **{
                    "conversation_id": payload["conversation_id"],
                    "turn_id": turn["turn_id"],
                    "run_id": turn.get("committed_run_id"),
                    "recorded_at": (
                        turn.get("completed_at")
                        or turn.get("updated_at")
                        or turn.get("opened_at")
                    ),
                    "status": turn.get("status"),
                    "answer_state": turn.get("answer_state"),
                    "support_basis": turn.get("support_basis"),
                    "question_domain": turn.get("question_domain"),
                    "version_context": turn.get("version_context"),
                    "execution_cost_profile": turn.get("execution_cost_profile"),
                },
                "canonical_support": resolve_canonical_turn_support(paths, turn=turn),
            }
            for payload in _load_log_payloads(paths.conversations_dir)
            if isinstance(payload.get("conversation_id"), str)
            for turn in (
                payload.get("turns", [])
                if isinstance(payload.get("turns", []), list)
                else []
            )
            if isinstance(turn, dict)
            and isinstance(turn.get("turn_id"), str)
            and isinstance(turn.get("committed_run_id"), str)
            and _record_has_canonical_ask_ownership(turn)
            and not _is_synthetic_turn(turn)
        ],
        key=lambda item: str(item.get("recorded_at") or ""),
        reverse=True,
    )

    source_counter: Counter[str] = Counter()
    unit_counter: Counter[str] = Counter()
    for payload in real_query_sessions:
        pairs = _iter_source_unit_pairs(payload)
        source_counter.update(source_id for source_id, _unit_id in pairs)
        unit_counter.update(f"{source_id}:{unit_id}" for source_id, unit_id in pairs)

    no_result_queries = [
        payload
        for payload in real_query_sessions
        if payload.get("status") == "no-results"
        and not _payload_is_noncanonical_leftover(
            payload,
            committed_turn_lookup=committed_turn_lookup,
            owned_session_ids=owned_session_ids,
            owned_trace_ids=owned_trace_ids,
        )
    ]
    degraded_answer_runs = [
        payload
        for payload in real_query_sessions
        if payload.get("final_answer")
        and payload.get("status") != "ready"
        and not _is_external_verified_success(payload)
        and not _payload_is_noncanonical_leftover(
            payload,
            committed_turn_lookup=committed_turn_lookup,
            owned_session_ids=owned_session_ids,
            owned_trace_ids=owned_trace_ids,
        )
    ]

    failure_pattern_counter: Counter[str] = Counter()
    failure_examples: dict[str, list[str]] = {}
    for payload in no_result_queries:
        failure_pattern_counter["no-results-retrieval"] += 1
        failure_examples.setdefault("no-results-retrieval", []).append(
            str(payload.get("session_id"))
        )
    for payload in degraded_answer_runs:
        failure_pattern_counter["degraded-answer-run"] += 1
        failure_examples.setdefault("degraded-answer-run", []).append(
            str(payload.get("session_id"))
        )
    for payload in real_retrieval_traces:
        if _payload_is_noncanonical_leftover(
            payload,
            committed_turn_lookup=committed_turn_lookup,
            owned_session_ids=owned_session_ids,
            owned_trace_ids=owned_trace_ids,
        ):
            continue
        if payload.get("trace_mode") != "answer-first":
            continue
        if _is_external_verified_success(payload):
            continue
        if payload.get("render_inspection_required"):
            failure_pattern_counter["render-inspection-required"] += 1
            failure_examples.setdefault("render-inspection-required", []).append(
                str(payload.get("trace_id"))
            )
        answer_state = payload.get("answer_state")
        if answer_state in {"partially-grounded", "unresolved"}:
            pattern = f"{answer_state}-answer-state"
            failure_pattern_counter[pattern] += 1
            failure_examples.setdefault(pattern, []).append(str(payload.get("trace_id")))

    candidate_cases: list[dict[str, Any]] = []
    for payload in no_result_queries[:RECENT_LIMIT]:
        candidate_cases.append(
            {
                "case_type": "no-results",
                "recorded_at": payload.get("recorded_at"),
                "session_id": payload.get("session_id"),
                "reason": "No grounded retrieval results were found.",
                "query": payload.get("query"),
            }
        )
    for payload in real_retrieval_traces:
        if _payload_is_noncanonical_leftover(
            payload,
            committed_turn_lookup=committed_turn_lookup,
            owned_session_ids=owned_session_ids,
            owned_trace_ids=owned_trace_ids,
        ):
            continue
        if payload.get("trace_mode") != "answer-first":
            continue
        if _is_external_verified_success(payload):
            continue
        if payload.get("status") == "ready" and payload.get("answer_state") in {
            "grounded",
            "abstained",
        }:
            continue
        candidate_cases.append(
            {
                "case_type": "degraded-answer-trace",
                "recorded_at": payload.get("recorded_at"),
                "conversation_id": payload.get("conversation_id"),
                "turn_id": payload.get("turn_id"),
                "session_id": payload.get("session_id"),
                "trace_id": payload.get("trace_id"),
                "reason": "The answer-first trace requires qualification or operator review.",
                "answer_state": payload.get("answer_state"),
            }
        )
    candidate_cases = sorted(
        candidate_cases,
        key=lambda item: str(item.get("recorded_at") or ""),
        reverse=True,
    )[:RECENT_LIMIT]

    summary = {
        "generated_at": max(
            [
                *[
                    _recorded_at(payload)
                    for payload in [*query_sessions, *retrieval_traces]
                    if payload
                ],
                *[
                    str(payload.get("updated_at") or "")
                    for payload in conversations
                    if isinstance(payload, dict)
                ],
            ],
            default="",
        ),
        "query_sessions": {
            "total": len(query_sessions),
            "real_total": len(real_query_sessions),
            "synthetic_total": len(synthetic_query_sessions),
            "recent": [
                _compact_query_session(payload) for payload in real_query_sessions[:RECENT_LIMIT]
            ],
            "synthetic_recent": [
                _compact_query_session(payload)
                for payload in synthetic_query_sessions[:RECENT_LIMIT]
            ],
            "no_results": [
                _compact_query_session(payload) for payload in no_result_queries[:RECENT_LIMIT]
            ],
            "degraded_answer_runs": [
                _compact_query_session(payload) for payload in degraded_answer_runs[:RECENT_LIMIT]
            ],
            "frequent_sources": _top_counts(source_counter, key_name="source_id"),
            "frequent_units": _top_counts(unit_counter, key_name="unit_id"),
            "failure_patterns": [
                {
                    "pattern": pattern,
                    "count": count,
                    "example_ids": failure_examples.get(pattern, [])[:3],
                }
                for pattern, count in sorted(
                    failure_pattern_counter.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            ],
            "candidate_cases": candidate_cases,
        },
        "retrieval_traces": {
            "total": len(retrieval_traces),
            "real_total": len(real_retrieval_traces),
            "synthetic_total": len(synthetic_retrieval_traces),
            "recent": [
                _compact_trace_record(payload) for payload in real_retrieval_traces[:RECENT_LIMIT]
            ],
            "synthetic_recent": [
                _compact_trace_record(payload)
                for payload in synthetic_retrieval_traces[:RECENT_LIMIT]
            ],
        },
        "control_plane": {
            "active_jobs": [
                {
                    "job_id": manifest.get("job_id"),
                    "job_key": manifest.get("job_key"),
                    "job_family": manifest.get("job_family"),
                    "status": manifest.get("status"),
                    "requires_confirmation": manifest.get("requires_confirmation"),
                    "confirmation_kind": manifest.get("confirmation_kind"),
                }
                for manifest in (
                    load_shared_job(paths, job_id)
                    for job_id in load_shared_jobs_index(paths).get("active_by_key", {}).values()
                    if isinstance(job_id, str) and job_id
                )
                if manifest
            ][:RECENT_LIMIT],
            "active_waiting_jobs": [
                {
                    "job_id": manifest.get("job_id"),
                    "job_key": manifest.get("job_key"),
                    "job_family": manifest.get("job_family"),
                    "status": manifest.get("status"),
                    "attached_run_count": len(
                        [
                            run_id
                            for run_id in manifest.get("attached_run_ids", [])
                            if isinstance(run_id, str) and run_id
                        ]
                    ),
                }
                for manifest in (
                    load_shared_job(paths, job_id)
                    for job_id in load_shared_jobs_index(paths).get("active_by_key", {}).values()
                    if isinstance(job_id, str) and job_id
                )
                if manifest and manifest.get("status") == "running"
            ][:RECENT_LIMIT],
            "active_awaiting_confirmation_jobs": [
                {
                    "job_id": manifest.get("job_id"),
                    "job_key": manifest.get("job_key"),
                    "job_family": manifest.get("job_family"),
                    "status": manifest.get("status"),
                    "confirmation_kind": manifest.get("confirmation_kind"),
                    "confirmation_prompt": manifest.get("confirmation_prompt"),
                }
                for manifest in (
                    load_shared_job(paths, job_id)
                    for job_id in load_shared_jobs_index(paths).get("active_by_key", {}).values()
                    if isinstance(job_id, str) and job_id
                )
                if manifest and manifest.get("status") == "awaiting-confirmation"
            ][:RECENT_LIMIT],
            "orphaned_query_sessions": [
                _compact_query_session(payload)
                for payload in orphaned_query_sessions[:RECENT_LIMIT]
            ],
            "orphaned_retrieval_traces": [
                _compact_trace_record(payload)
                for payload in orphaned_retrieval_traces[:RECENT_LIMIT]
            ],
        },
        "committed_turns": {
            "total": len(committed_turns),
            "recent": committed_turns[:RECENT_LIMIT],
        },
        "conversations": {
            "total": len(conversations),
            "recent": [_compact_conversation(payload) for payload in conversations[:RECENT_LIMIT]],
        },
        "native_reconciliation": {
            "total": len(native_ledgers),
            "recent": [
                _compact_native_ledger(payload)
                for payload in native_ledgers[:RECENT_LIMIT]
            ],
            "anomalous_recent": [
                _compact_native_ledger(payload)
                for payload in native_ledgers
                if isinstance(payload.get("host_identity"), dict)
                and any(
                    isinstance(value, str) and value == "anomalous-host-identity"
                    for value in payload["host_identity"].get("anomaly_flags", [])
                )
            ][:RECENT_LIMIT],
            "host_runtime_failures_recent": sorted(
                [
                    bucket_item
                    for payload in native_ledgers
                    for bucket_item in _native_ledger_turn_bucket(
                        payload,
                        classifications={"host-runtime-failure", "host-runtime-overload"},
                    )
                ],
                key=lambda item: str(item.get("recorded_at") or ""),
                reverse=True,
            )[:RECENT_LIMIT],
            "incomplete_recent": sorted(
                [
                    bucket_item
                    for payload in native_ledgers
                    for bucket_item in _native_ledger_turn_bucket(
                        payload,
                        classifications={"incomplete-session"},
                    )
                ],
                key=lambda item: str(item.get("recorded_at") or ""),
                reverse=True,
            )[:RECENT_LIMIT],
        },
    }
    return summary


def _external_urls_from_turn(paths: WorkspacePaths, turn: dict[str, Any]) -> list[str]:
    support_manifest_path = turn.get("support_manifest_path")
    if not isinstance(support_manifest_path, str) or not support_manifest_path:
        return []
    manifest_path = Path(support_manifest_path)
    if not manifest_path.is_absolute():
        manifest_path = paths.root / support_manifest_path
    manifest = read_json(manifest_path)
    sources = manifest.get("sources", [])
    if not isinstance(sources, list):
        return []
    urls: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if isinstance(url, str) and url:
            urls.append(url)
    return list(dict.fromkeys(urls))


def build_answer_history_index(paths: WorkspacePaths) -> dict[str, Any]:
    """Build an answer-history index for evidence warm-start and review analysis."""
    records: list[dict[str, Any]] = []
    for path in sorted(paths.conversations_dir.glob("*.json")):
        conversation = read_json(path)
        conversation_id = conversation.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        turns = conversation.get("turns", [])
        if not isinstance(turns, list):
            continue
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            if not _record_has_canonical_ask_ownership(turn):
                continue
            if _is_synthetic_turn(turn):
                continue
            turn_id = turn.get("turn_id")
            question_text = turn.get("user_question")
            answer_file_path = turn.get("answer_file_path")
            if not isinstance(turn_id, str) or not isinstance(question_text, str):
                continue
            if not isinstance(answer_file_path, str) or not answer_file_path:
                continue
            committed_run_id = turn.get("committed_run_id")
            if not isinstance(committed_run_id, str) or not committed_run_id:
                continue
            run_commit = _run_commit_payload(paths, committed_run_id)
            run_commit_version_context = run_commit.get("version_context")
            turn_version_context = turn.get("version_context")
            version_context = (
                dict(run_commit_version_context)
                if isinstance(run_commit_version_context, dict)
                else (
                    dict(turn_version_context)
                    if isinstance(turn_version_context, dict)
                    else {}
                )
            )
            corpus_signature = (
                version_context.get("published_source_signature")
                or version_context.get("corpus_signature")
            )
            canonical_support = resolve_canonical_turn_support(paths, turn=turn)
            records.append(
                {
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    "run_id": committed_run_id,
                    "question_text": question_text,
                    "question_class": turn.get("question_class"),
                    "question_domain": turn.get("question_domain"),
                    "support_strategy": turn.get("support_strategy"),
                    "analysis_origin": turn.get("analysis_origin"),
                    "inspection_scope": turn.get("inspection_scope"),
                    "preferred_channels": turn.get("preferred_channels", []),
                    "used_published_channels": turn.get("used_published_channels", []),
                    "published_artifacts_sufficient": turn.get(
                        "published_artifacts_sufficient"
                    ),
                    "source_escalation_required": turn.get("source_escalation_required"),
                    "source_escalation_reason": turn.get("source_escalation_reason"),
                    "support_basis": turn.get("support_basis"),
                    "answer_state": turn.get("answer_state"),
                    "answer_file_path": answer_file_path,
                    "canonical_support_summary": canonical_support.get(
                        "canonical_support_summary",
                        {},
                    ),
                    "kb_source_ids": canonical_support["supporting_source_ids"],
                    "kb_unit_ids": canonical_support["supporting_unit_ids"],
                    "kb_artifact_ids": canonical_support["supporting_artifact_ids"],
                    "external_urls": _external_urls_from_turn(paths, turn),
                    "session_ids": canonical_support["session_ids"],
                    "trace_ids": canonical_support["trace_ids"],
                    "recorded_at": turn.get("completed_at")
                    or turn.get("updated_at")
                    or turn.get("opened_at"),
                    "corpus_signature": corpus_signature,
                    "published_snapshot_id": version_context.get("published_snapshot_id"),
                    "version_context": version_context,
                }
            )
    records.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    return {
        "generated_at": max((str(item.get("recorded_at") or "") for item in records), default=""),
        "record_count": len(records),
        "records": records,
    }


def refresh_log_review_summary(paths: WorkspacePaths) -> dict[str, Any]:
    """Rebuild and persist the runtime log-review summary."""
    from .projections import ensure_runtime_projections_fresh

    result = ensure_runtime_projections_fresh(paths, consumer="runtime-log-review")
    summary = result.get("summary")
    return summary if isinstance(summary, dict) else {}

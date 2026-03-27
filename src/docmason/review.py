"""Read-only review-summary helpers for DocMason runtime logs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .conversation import normalize_front_door_state, semantic_log_context_from_record
from .control_plane import load_shared_jobs_index, load_shared_job
from .project import WorkspacePaths, read_json

RECENT_LIMIT = 10
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


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
    return any(_record_has_canonical_ask_ownership(turn) for turn in turns if isinstance(turn, dict))


def _effective_log_origin(
    payload: dict[str, Any],
    *,
    linked_turn: dict[str, Any] | None = None,
) -> str | None:
    explicit = payload.get("log_origin")
    if isinstance(explicit, str) and explicit:
        return explicit
    if _record_has_canonical_ask_ownership(payload) or _record_has_canonical_ask_ownership(
        linked_turn
    ):
        return "interactive-ask"
    if isinstance(payload.get("conversation_id"), str):
        return "workflow-linked"
    return None


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
            if _conversation_has_canonical_truth(payload)
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


def _load_native_ledgers(paths: WorkspacePaths) -> list[dict[str, Any]]:
    if not paths.native_ledger_dir.exists():
        return []
    ledgers = [read_json(path) for path in sorted(paths.native_ledger_dir.glob("*.json"))]
    return [payload for payload in ledgers if payload]


def _compact_native_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    turns = payload.get("turns", [])
    latest_turn = turns[-1] if isinstance(turns, list) and turns else {}
    host_identity = payload.get("host_identity")
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
    }


def _run_commit_payload(paths: WorkspacePaths, run_id: str | None) -> dict[str, Any]:
    if not isinstance(run_id, str) or not run_id:
        return {}
    payload = read_json(paths.runs_dir / run_id / "commit.json")
    if payload:
        return payload
    return read_json(paths.runs_dir / run_id / "state.json")


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
) -> str:
    if conversation_id and log_origin == "interactive-ask":
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
        if payload.get("log_origin") == "evaluation-suite":
            continue
        if _is_external_verified_success(payload):
            continue
        status = payload.get("status")
        trace_mode = payload.get("trace_mode")
        if status not in {"no-results", "degraded"} and not payload.get("render_inspection_required"):
            continue
        if trace_mode == "citation-first" and status == "ready":
            continue
        conversation_id = payload.get("conversation_id")
        turn_id = payload.get("turn_id")
        session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else None
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
                "log_origin": _effective_log_origin(
                    payload,
                    linked_turn=(
                        conversation_record["turn"] if isinstance(conversation_record, dict) else None
                    ),
                ),
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
        if session_id:
            group["session_ids"].append(session_id)
            group["feedback_match_count"] += len(feedback_by_session.get(session_id, []))
            for record in feedback_by_session.get(session_id, []):
                tags = record.get("feedback_tags", [])
                if isinstance(tags, list):
                    group["feedback_tags"].extend(tag for tag in tags if isinstance(tag, str))
        if trace_id:
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
        session_ids = list(dict.fromkeys(item for item in group["session_ids"] if isinstance(item, str)))
        trace_ids = list(dict.fromkeys(item for item in group["trace_ids"] if isinstance(item, str)))
        candidate_priority = _candidate_priority(
            conversation_id=group["conversation_id"] if isinstance(group["conversation_id"], str) else None,
            log_origin=group["log_origin"] if isinstance(group["log_origin"], str) else None,
            feedback_match_count=int(group["feedback_match_count"]),
        )
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
            **group["question_context"],
        }
        if isinstance(group.get("reference_resolution_summary"), str):
            candidate["reference_resolution_summary"] = group["reference_resolution_summary"]
        if isinstance(group.get("conversation_path"), str):
            candidate["conversation_path"] = group["conversation_path"]
        if isinstance(group.get("answer_file_path"), str):
            candidate["answer_file_path"] = group["answer_file_path"]
        if isinstance(group.get("bundle_paths"), list):
            candidate["bundle_paths"] = group["bundle_paths"]
        candidates.append(candidate)
    candidates = sorted(candidates, key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
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
        payload for payload in query_sessions if payload.get("log_origin") != "evaluation-suite"
    ]
    synthetic_query_sessions = [
        payload for payload in query_sessions if payload.get("log_origin") == "evaluation-suite"
    ]
    real_retrieval_traces = [
        payload for payload in retrieval_traces if payload.get("log_origin") != "evaluation-suite"
    ]
    synthetic_retrieval_traces = [
        payload for payload in retrieval_traces if payload.get("log_origin") == "evaluation-suite"
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
                "conversation_id": payload["conversation_id"],
                "turn_id": turn["turn_id"],
                "run_id": turn.get("committed_run_id"),
                "recorded_at": turn.get("completed_at") or turn.get("updated_at") or turn.get("opened_at"),
                "status": turn.get("status"),
                "answer_state": turn.get("answer_state"),
                "support_basis": turn.get("support_basis"),
                "question_domain": turn.get("question_domain"),
                "version_context": turn.get("version_context"),
            }
            for payload in _load_log_payloads(paths.conversations_dir)
            if isinstance(payload.get("conversation_id"), str)
            for turn in (payload.get("turns", []) if isinstance(payload.get("turns", []), list) else [])
            if isinstance(turn, dict)
            and isinstance(turn.get("turn_id"), str)
            and isinstance(turn.get("committed_run_id"), str)
            and _record_has_canonical_ask_ownership(turn)
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
        payload for payload in real_query_sessions if payload.get("status") == "no-results"
    ]
    degraded_answer_runs = [
        payload
        for payload in real_query_sessions
        if payload.get("final_answer")
        and payload.get("status") != "ready"
        and not _is_external_verified_success(payload)
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
                _compact_query_session(payload) for payload in orphaned_query_sessions[:RECENT_LIMIT]
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
            "recent": [_compact_native_ledger(payload) for payload in native_ledgers[:RECENT_LIMIT]],
            "anomalous_recent": [
                _compact_native_ledger(payload)
                for payload in native_ledgers
                if isinstance(payload.get("host_identity"), dict)
                and any(
                    isinstance(value, str) and value == "anomalous-host-identity"
                    for value in payload["host_identity"].get("anomaly_flags", [])
                )
            ][:RECENT_LIMIT],
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
    session_lookup = {
        path.stem: read_json(path)
        for path in sorted(paths.query_sessions_dir.glob("*.json"))
        if path.is_file()
    }
    trace_lookup = {
        path.stem: read_json(path)
        for path in sorted(paths.retrieval_traces_dir.glob("*.json"))
        if path.is_file()
    }
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
            session_ids = [
                value for value in turn.get("session_ids", []) if isinstance(value, str) and value
            ]
            trace_ids = [
                value for value in turn.get("trace_ids", []) if isinstance(value, str) and value
            ]
            kb_source_ids: list[str] = []
            for session_id in session_ids:
                payload = session_lookup.get(session_id, {})
                consulted_results = payload.get("consulted_results", [])
                if not isinstance(consulted_results, list):
                    continue
                for item in consulted_results:
                    if not isinstance(item, dict):
                        continue
                    source_id = item.get("source_id")
                    if isinstance(source_id, str) and source_id:
                        kb_source_ids.append(source_id)
                    results = item.get("results", [])
                    if isinstance(results, list):
                        kb_source_ids.extend(
                            [
                                str(result.get("source_id"))
                                for result in results
                                if isinstance(result, dict)
                                and isinstance(result.get("source_id"), str)
                            ]
                        )
            for trace_id in trace_ids:
                payload = trace_lookup.get(trace_id, {})
                source_ids = payload.get("supporting_source_ids", [])
                if isinstance(source_ids, list):
                    kb_source_ids.extend(
                        value for value in source_ids if isinstance(value, str) and value
                    )
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
                    "kb_source_ids": list(dict.fromkeys(kb_source_ids)),
                    "external_urls": _external_urls_from_turn(paths, turn),
                    "session_ids": session_ids,
                    "trace_ids": trace_ids,
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
    from .projections import refresh_runtime_projections

    return refresh_runtime_projections(paths)

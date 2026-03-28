"""Write-time indexing and canonical binding validation for runtime logs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .conversation import (
    FRONT_DOOR_STATE_CANONICAL_ASK,
    load_turn_record,
    normalize_front_door_state,
)
from .project import WorkspacePaths, read_json, write_json
from .run_control import RUN_ORIGIN_ASK_FRONT_DOOR, load_run_state, normalize_run_origin

TURN_ARTIFACT_INDEX_SCHEMA_VERSION = 1


def _turn_artifact_index_path(
    paths: WorkspacePaths,
    conversation_id: str,
    turn_id: str,
) -> Path:
    return paths.turn_artifact_index_dir / conversation_id / f"{turn_id}.json"


def _demote_canonical_binding(payload: dict[str, Any], *, reason: str) -> dict[str, Any]:
    demoted = dict(payload)
    for field_name in (
        "conversation_id",
        "turn_id",
        "run_id",
        "entry_workflow_id",
        "inner_workflow_id",
        "front_door_state",
        "answer_file_path",
    ):
        demoted.pop(field_name, None)
    demoted["canonical_binding_status"] = "demoted"
    demoted["canonical_binding_reason"] = reason
    return demoted


def sanitize_canonical_log_payload(
    paths: WorkspacePaths,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate canonical ask linkage and demote invalid log bindings into audit-only records."""
    conversation_id = payload.get("conversation_id")
    turn_id = payload.get("turn_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return dict(payload)
    if not isinstance(turn_id, str) or not turn_id:
        return dict(payload)
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return _demote_canonical_binding(
            payload,
            reason="Canonical ask-linked runtime logs require a live run id.",
        )
    try:
        turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    except KeyError:
        return _demote_canonical_binding(
            payload,
            reason="The referenced canonical turn no longer exists.",
        )
    run_state = load_run_state(paths, run_id)
    if not run_state:
        return _demote_canonical_binding(
            payload,
            reason="The referenced canonical run no longer exists.",
        )
    if run_state.get("conversation_id") != conversation_id or run_state.get("turn_id") != turn_id:
        return _demote_canonical_binding(
            payload,
            reason="The referenced run does not belong to the linked canonical turn.",
        )
    if normalize_run_origin(run_state.get("run_origin")) != RUN_ORIGIN_ASK_FRONT_DOOR:
        return _demote_canonical_binding(
            payload,
            reason="Only ask-front-door runs may write canonical turn-linked runtime logs.",
        )
    if normalize_front_door_state(turn.get("front_door_state")) != FRONT_DOOR_STATE_CANONICAL_ASK:
        return _demote_canonical_binding(
            payload,
            reason="The linked turn is not currently a canonical ask turn.",
        )
    if turn.get("committed_run_id") == run_id:
        return _demote_canonical_binding(
            payload,
            reason="Post-commit runtime logs may not append new canonical ask-owned artifacts.",
        )
    active_run_id = turn.get("active_run_id")
    if isinstance(active_run_id, str) and active_run_id and active_run_id != run_id:
        return _demote_canonical_binding(
            payload,
            reason="The linked run is no longer the legal active ask run for this turn.",
        )
    enriched = dict(payload)
    enriched["canonical_binding_status"] = "canonical"
    return enriched


def _load_turn_artifact_index(
    paths: WorkspacePaths,
    conversation_id: str,
    turn_id: str,
) -> dict[str, Any]:
    payload = read_json(_turn_artifact_index_path(paths, conversation_id, turn_id))
    if not payload:
        return {
            "schema_version": TURN_ARTIFACT_INDEX_SCHEMA_VERSION,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "updated_at": None,
            "session_candidates": [],
            "trace_candidates": [],
        }
    payload.setdefault("session_candidates", [])
    payload.setdefault("trace_candidates", [])
    return payload


def _artifact_entry(payload: dict[str, Any], *, artifact_id_field: str) -> dict[str, Any]:
    return {
        artifact_id_field: payload.get(artifact_id_field),
        "recorded_at": payload.get("recorded_at"),
        "conversation_id": payload.get("conversation_id"),
        "turn_id": payload.get("turn_id"),
        "run_id": payload.get("run_id"),
        "entry_workflow_id": payload.get("entry_workflow_id"),
        "inner_workflow_id": payload.get("inner_workflow_id"),
        "front_door_state": payload.get("front_door_state"),
        "answer_file_path": payload.get("answer_file_path"),
        "log_origin": payload.get("log_origin"),
    }


def update_turn_artifact_index(
    paths: WorkspacePaths,
    *,
    payload: dict[str, Any],
) -> None:
    """Persist one canonical turn-linked runtime artifact into the per-turn index."""
    if payload.get("canonical_binding_status") != "canonical":
        return
    conversation_id = payload.get("conversation_id")
    turn_id = payload.get("turn_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return
    if not isinstance(turn_id, str) or not turn_id:
        return
    session_id = payload.get("session_id")
    trace_id = payload.get("trace_id")
    if not isinstance(session_id, str) and not isinstance(trace_id, str):
        return
    index = _load_turn_artifact_index(paths, conversation_id, turn_id)
    if isinstance(session_id, str) and session_id:
        session_candidates = [
            candidate
            for candidate in index.get("session_candidates", [])
            if not (
                isinstance(candidate, dict)
                and candidate.get("session_id") == session_id
            )
        ]
        session_candidates.append(_artifact_entry(payload, artifact_id_field="session_id"))
        session_candidates.sort(key=lambda item: str(item.get("recorded_at") or ""))
        index["session_candidates"] = session_candidates
    if isinstance(trace_id, str) and trace_id:
        trace_candidates = [
            candidate
            for candidate in index.get("trace_candidates", [])
            if not (
                isinstance(candidate, dict)
                and candidate.get("trace_id") == trace_id
            )
        ]
        trace_candidates.append(_artifact_entry(payload, artifact_id_field="trace_id"))
        trace_candidates.sort(key=lambda item: str(item.get("recorded_at") or ""))
        index["trace_candidates"] = trace_candidates
    index["updated_at"] = payload.get("recorded_at")
    write_json(_turn_artifact_index_path(paths, conversation_id, turn_id), index)


def discover_turn_artifact_candidates(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
    inner_workflow_id: str,
    answer_file_path: str | None,
) -> tuple[list[str], list[str]]:
    """Return ordered turn artifact candidates from the write-time per-turn index."""
    index = _load_turn_artifact_index(paths, conversation_id, turn_id)

    def _matches(candidate: dict[str, Any]) -> bool:
        if not isinstance(candidate, dict):
            return False
        candidate_run_id = candidate.get("run_id")
        if isinstance(run_id, str) and run_id:
            if (
                isinstance(candidate_run_id, str)
                and candidate_run_id
                and candidate_run_id != run_id
            ):
                return False
        candidate_inner = candidate.get("inner_workflow_id")
        if (
            isinstance(candidate_inner, str)
            and candidate_inner
            and candidate_inner != inner_workflow_id
        ):
            return False
        candidate_front_door = normalize_front_door_state(candidate.get("front_door_state"))
        if candidate_front_door and candidate_front_door != FRONT_DOOR_STATE_CANONICAL_ASK:
            return False
        candidate_answer_file = candidate.get("answer_file_path")
        if (
            isinstance(answer_file_path, str)
            and answer_file_path
            and isinstance(candidate_answer_file, str)
            and candidate_answer_file
            and candidate_answer_file != answer_file_path
        ):
            return False
        return True

    session_ids = [
        str(candidate.get("session_id"))
        for candidate in index.get("session_candidates", [])
        if isinstance(candidate, dict)
        and isinstance(candidate.get("session_id"), str)
        and _matches(candidate)
    ]
    trace_ids = [
        str(candidate.get("trace_id"))
        for candidate in index.get("trace_candidates", [])
        if isinstance(candidate, dict)
        and isinstance(candidate.get("trace_id"), str)
        and _matches(candidate)
    ]
    return session_ids, trace_ids

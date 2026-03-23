"""Conversation-native runtime helpers for natural multi-turn DocMason usage."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .coordination import workspace_lease
from .project import (
    WorkspacePaths,
    ensure_json_parent,
    knowledge_base_snapshot,
    read_json,
    source_inventory_signature,
    sync_state,
    write_json,
)

ACTIVE_CONVERSATION_IDLE_WINDOW = timedelta(hours=8)
OPEN_TURN_REUSE_WINDOW = timedelta(minutes=15)
LOG_CONTEXT_CORE_FIELD_NAMES = (
    "conversation_id",
    "turn_id",
    "run_id",
    "entry_workflow_id",
    "inner_workflow_id",
    "native_turn_id",
)
SEMANTIC_LOG_CONTEXT_FIELD_NAMES = (
    "question_class",
    "question_domain",
    "support_strategy",
    "analysis_origin",
    "support_basis",
    "support_manifest_path",
)
LOG_CONTEXT_FIELD_NAMES = LOG_CONTEXT_CORE_FIELD_NAMES + SEMANTIC_LOG_CONTEXT_FIELD_NAMES


def utc_now() -> str:
    """Return the current UTC time in ISO 8601 form."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _backfill_turn_runtime_fields(turn: dict[str, Any]) -> dict[str, Any]:
    if "active_run_id" not in turn:
        turn["active_run_id"] = None
    if "committed_run_id" not in turn:
        turn["committed_run_id"] = None
    if "turn_state" not in turn:
        turn["turn_state"] = "committed" if turn.get("committed_run_id") else "opened"
    if "version_context" not in turn:
        turn["version_context"] = None
    if "capability_profile" not in turn:
        turn["capability_profile"] = None
    if "hybrid_refresh_triggered" not in turn:
        turn["hybrid_refresh_triggered"] = False
    if "hybrid_refresh_sources" not in turn:
        turn["hybrid_refresh_sources"] = []
    if "hybrid_refresh_completion_status" not in turn:
        turn["hybrid_refresh_completion_status"] = None
    if "hybrid_refresh_summary" not in turn:
        turn["hybrid_refresh_summary"] = None
    return turn


def _conversation_resource(conversation_id: str) -> str:
    """Return the coordination resource name for one mutable conversation record."""
    return f"conversation:{conversation_id}"


def semantic_log_context_fields(
    *,
    question_class: str | None = None,
    question_domain: str | None = None,
    support_strategy: str | None = None,
    analysis_origin: str | None = None,
    support_basis: str | None = None,
    support_manifest_path: str | None = None,
) -> dict[str, str]:
    """Return normalized flat semantic fields suitable for linked log artifacts."""
    fields = {
        "question_class": question_class,
        "question_domain": question_domain,
        "support_strategy": support_strategy,
        "analysis_origin": analysis_origin,
        "support_basis": support_basis,
        "support_manifest_path": support_manifest_path,
    }
    return {
        field_name: normalized
        for field_name, value in fields.items()
        if (normalized := _nonempty_string(value)) is not None
    }


def semantic_log_context_from_record(record: dict[str, Any] | None) -> dict[str, str]:
    """Extract flat semantic log fields from one turn or log record."""
    if not isinstance(record, dict):
        return {}
    return semantic_log_context_fields(
        question_class=_nonempty_string(record.get("question_class")),
        question_domain=_nonempty_string(record.get("question_domain")),
        support_strategy=_nonempty_string(record.get("support_strategy")),
        analysis_origin=_nonempty_string(record.get("analysis_origin")),
        support_basis=_nonempty_string(record.get("support_basis")),
        support_manifest_path=_nonempty_string(record.get("support_manifest_path")),
    )


def _turn_answer_file_is_empty(paths: WorkspacePaths, turn: dict[str, Any]) -> bool:
    answer_file_path = turn.get("answer_file_path")
    if not isinstance(answer_file_path, str) or not answer_file_path:
        return True
    path = Path(answer_file_path)
    if not path.is_absolute():
        path = paths.root / answer_file_path
    if not path.exists():
        return True
    return not path.read_text(encoding="utf-8").strip()


def detect_agent_surface() -> str:
    """Infer the current agent surface from the environment when possible.

    Detection priority:
    1. Explicit ``DOCMASON_AGENT_SURFACE`` override
    2. ``CODEX_THREAD_ID`` or Codex originator override → ``codex``
    3. ``CLAUDE_PROJECT_DIR`` (injected by Claude Code) → ``claude-code``
    4. ``CLAUDE_SESSION_ID`` or ``CLAUDE_CONVERSATION_ID`` → ``claude-code``
    5. Broad env-scan fallback for any ``claude`` occurrence → ``claude-code``
    6. ``unknown-agent``
    """
    explicit = os.environ.get("DOCMASON_AGENT_SURFACE")
    if explicit:
        return explicit.strip().lower()
    if os.environ.get("CODEX_THREAD_ID"):
        return "codex"
    # Claude Code injects CLAUDE_PROJECT_DIR into hook and tool processes.
    if os.environ.get("CLAUDE_PROJECT_DIR"):
        return "claude-code"
    if os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("CLAUDE_CONVERSATION_ID"):
        return "claude-code"
    origin = os.environ.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").lower()
    if "codex" in origin:
        return "codex"
    # Broad env-scan fallback — less reliable but preserves existing behavior.
    env_keys = " ".join(os.environ.keys()).lower()
    env_values = " ".join(os.environ.values()).lower()
    if "claude" in env_keys or "claude" in env_values:
        return "claude-code"
    return "unknown-agent"


def current_corpus_signature(paths: WorkspacePaths) -> str | None:
    """Return the current published corpus signature when available."""
    state = sync_state(paths)
    published_signature = state.get("published_source_signature")
    if isinstance(published_signature, str) and published_signature:
        return published_signature
    validation_report = read_json(paths.current_validation_report_path)
    source_signature = validation_report.get("source_signature")
    if isinstance(source_signature, str) and source_signature:
        return source_signature
    return None


def workspace_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Return a compact workspace and corpus snapshot for conversation records."""
    kb_snapshot = knowledge_base_snapshot(paths)
    return {
        "captured_at": utc_now(),
        "knowledge_base": {
            "present": kb_snapshot["present"],
            "stale": kb_snapshot["stale"],
            "validation_status": kb_snapshot["validation_status"],
            "last_publish_at": kb_snapshot["last_publish_at"],
        },
        "corpus_signature": current_corpus_signature(paths),
        "source_inventory_signature": source_inventory_signature(paths),
    }


def native_conversation_id() -> tuple[str | None, str | None]:
    """Return a conversation identifier from the current agent surface when available."""
    for variable in (
        "DOCMASON_CONVERSATION_ID",
        "CODEX_THREAD_ID",
        "CLAUDE_CONVERSATION_ID",
        "CLAUDE_SESSION_ID",
    ):
        value = os.environ.get(variable)
        if value:
            return value, variable.lower()
    return None, None


def _load_active_conversation(paths: WorkspacePaths) -> dict[str, Any]:
    return read_json(paths.active_conversation_path)


def resolve_conversation_id(paths: WorkspacePaths, *, agent_surface: str) -> tuple[str, str]:
    """Resolve the parent conversation identifier for the current chat."""
    native_id, native_source = native_conversation_id()
    if native_id is not None and native_source is not None:
        return native_id, native_source

    active = _load_active_conversation(paths)
    active_id = active.get("conversation_id")
    active_agent = active.get("agent_surface")
    updated_at = _parse_timestamp(active.get("updated_at"))
    if (
        isinstance(active_id, str)
        and active_id
        and active_agent == agent_surface
        and updated_at is not None
        and datetime.now(tz=UTC) - updated_at <= ACTIVE_CONVERSATION_IDLE_WINDOW
    ):
        return active_id, "workspace-active-fallback"
    return str(uuid.uuid4()), "generated"


def conversation_path(paths: WorkspacePaths, conversation_id: str) -> Path:
    """Return the runtime path for one parent conversation record."""
    return paths.conversations_dir / f"{conversation_id}.json"


def answer_file_path(paths: WorkspacePaths, *, conversation_id: str, turn_id: str) -> Path:
    """Return the canonical answer-file path for one conversation turn."""
    return paths.answers_dir / conversation_id / f"{turn_id}.md"


def load_conversation_record(paths: WorkspacePaths, conversation_id: str) -> dict[str, Any]:
    """Load one persisted conversation record."""
    conversation = read_json(conversation_path(paths, conversation_id))
    turns = conversation.get("turns")
    if isinstance(turns, list):
        conversation["turns"] = [
            _backfill_turn_runtime_fields(turn) if isinstance(turn, dict) else turn
            for turn in turns
        ]
    return conversation


def base_turn_record(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    user_question: str,
    entry_workflow_id: str = "ask",
) -> dict[str, Any]:
    """Build the canonical baseline shape for one conversation turn."""
    answer_path = answer_file_path(paths, conversation_id=conversation_id, turn_id=turn_id)
    now = utc_now()
    return {
        "turn_id": turn_id,
        "native_turn_id": None,
        "active_run_id": None,
        "committed_run_id": None,
        "turn_state": "opened",
        "version_context": None,
        "capability_profile": None,
        "opened_at": now,
        "updated_at": now,
        "completed_at": None,
        "user_question": user_question,
        "entry_workflow_id": entry_workflow_id,
        "inner_workflow_id": None,
        "knowledge_base_missing": False,
        "knowledge_base_stale": False,
        "sync_suggested": False,
        "sync_requested": False,
        "auto_sync_triggered": False,
        "auto_sync_reason": None,
        "auto_sync_summary": None,
        "hybrid_refresh_triggered": False,
        "hybrid_refresh_sources": [],
        "hybrid_refresh_completion_status": None,
        "hybrid_refresh_summary": None,
        "session_ids": [],
        "trace_ids": [],
        "captured_interaction_ids": [],
        "answer_file_path": str(answer_path.relative_to(paths.root)),
        "answer_state": None,
        "render_inspection_required": None,
        "status": "opened",
        "route_reason": None,
        "freshness_notice": None,
        "response_excerpt": None,
        "continuation_type": None,
        "reused_previous_evidence": False,
        "new_retrieval_executed": False,
        "new_trace_executed": False,
        "question_class": None,
        "question_domain": None,
        "analysis_origin": None,
        "semantic_analysis": None,
        "evidence_mode": None,
        "support_strategy": None,
        "inspection_scope": None,
        "preferred_channels": [],
        "used_published_channels": [],
        "published_artifacts_sufficient": None,
        "reference_resolution": None,
        "reference_resolution_summary": None,
        "source_escalation_required": None,
        "source_escalation_reason": None,
        "support_basis": None,
        "support_manifest_path": None,
        "source_escalation_used": False,
        "research_depth": None,
        "bundle_paths": [],
        "attachments": [],
        "tool_use_audit": None,
        "reconciliation": None,
    }


def ensure_conversation_record(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    conversation_id_source: str,
    agent_surface: str,
) -> dict[str, Any]:
    """Load or initialize one parent conversation record."""
    now = utc_now()
    conversation = load_conversation_record(paths, conversation_id)
    if conversation:
        return conversation
    conversation = {
        "conversation_id": conversation_id,
        "conversation_id_source": conversation_id_source,
        "agent_surface": agent_surface,
        "opened_at": now,
        "updated_at": now,
        "workspace_snapshot": workspace_snapshot(paths),
        "turns": [],
    }
    ensure_json_parent(conversation_path(paths, conversation_id))
    write_json(conversation_path(paths, conversation_id), conversation)
    return conversation


def find_turn_index(
    conversation: dict[str, Any],
    *,
    turn_id: str | None = None,
    native_turn_id: str | None = None,
) -> int | None:
    """Return the turn index that matches either a turn id or a native turn id."""
    turns = conversation.get("turns", [])
    if not isinstance(turns, list):
        return None
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        if turn_id is not None and turn.get("turn_id") == turn_id:
            return index
        if native_turn_id is not None and turn.get("native_turn_id") == native_turn_id:
            return index
    return None


def find_reusable_open_turn_index(
    paths: WorkspacePaths,
    conversation: dict[str, Any],
    *,
    user_question: str,
    entry_workflow_id: str,
) -> int | None:
    """Return the latest reusable turn for the same live ask question when present."""
    turns = conversation.get("turns", [])
    if not isinstance(turns, list) or not turns:
        return None
    latest_turn = turns[-1]
    if not isinstance(latest_turn, dict):
        return None
    if latest_turn.get("entry_workflow_id") != entry_workflow_id:
        return None
    if str(latest_turn.get("user_question", "")).strip() != user_question.strip():
        return None
    updated_at = _parse_timestamp(latest_turn.get("updated_at"))
    if updated_at is None or datetime.now(tz=UTC) - updated_at > OPEN_TURN_REUSE_WINDOW:
        return None
    if latest_turn.get("response_excerpt"):
        return None
    if latest_turn.get("session_ids") or latest_turn.get("trace_ids"):
        return None
    if not _turn_answer_file_is_empty(paths, latest_turn):
        return None
    return len(turns) - 1


def open_conversation_turn(
    paths: WorkspacePaths,
    *,
    user_question: str,
    entry_workflow_id: str = "ask",
) -> dict[str, Any]:
    """Open or continue a parent conversation and append a new turn."""
    user_question = user_question.strip()
    if not user_question:
        raise ValueError("User question is empty.")
    agent_surface = detect_agent_surface()
    conversation_id, conversation_id_source = resolve_conversation_id(
        paths,
        agent_surface=agent_surface,
    )
    now = utc_now()
    path = conversation_path(paths, conversation_id)
    with workspace_lease(paths, _conversation_resource(conversation_id)):
        conversation = ensure_conversation_record(
            paths,
            conversation_id=conversation_id,
            conversation_id_source=conversation_id_source,
            agent_surface=agent_surface,
        )
        turns = conversation.get("turns", [])
        if not isinstance(turns, list):
            turns = []
        reusable_index = find_reusable_open_turn_index(
            paths,
            conversation,
            user_question=user_question,
            entry_workflow_id=entry_workflow_id,
        )
        if reusable_index is not None:
            turn = _backfill_turn_runtime_fields(turns[reusable_index])
            turn_id = str(turn["turn_id"])
            answer_path = answer_file_path(paths, conversation_id=conversation_id, turn_id=turn_id)
            turn["updated_at"] = now
        else:
            turn_index = len(turns) + 1
            turn_id = f"turn-{turn_index:03d}"
            answer_path = answer_file_path(paths, conversation_id=conversation_id, turn_id=turn_id)
            turn = base_turn_record(
                paths,
                conversation_id=conversation_id,
                turn_id=turn_id,
                user_question=user_question,
                entry_workflow_id=entry_workflow_id,
            )
            turn["opened_at"] = now
            turn["updated_at"] = now
            turns.append(turn)
        conversation["turns"] = turns
        conversation["updated_at"] = now
        conversation["workspace_snapshot"] = workspace_snapshot(paths)
        ensure_json_parent(path)
        write_json(path, conversation)
        write_json(
            paths.active_conversation_path,
            {
                "conversation_id": conversation_id,
                "agent_surface": agent_surface,
                "updated_at": now,
            },
        )
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "conversation_path": str(path.relative_to(paths.root)),
        "answer_file_path": str(answer_path.relative_to(paths.root)),
        "conversation_id_source": conversation_id_source,
        "agent_surface": agent_surface,
        "workspace_snapshot": conversation["workspace_snapshot"],
    }


def load_turn_record(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
) -> dict[str, Any]:
    """Load one stored conversation turn."""
    conversation = load_conversation_record(paths, conversation_id)
    turns = conversation.get("turns", [])
    if not isinstance(turns, list):
        raise KeyError(turn_id)
    for turn in turns:
        if isinstance(turn, dict) and turn.get("turn_id") == turn_id:
            return _backfill_turn_runtime_fields(turn)
    raise KeyError(turn_id)


def find_turn_by_question(
    conversation: dict[str, Any],
    *,
    user_question: str,
) -> int | None:
    """Return the latest turn index with the same user question when the native id is absent."""
    turns = conversation.get("turns", [])
    if not isinstance(turns, list):
        return None
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        if not isinstance(turn, dict):
            continue
        if turn.get("native_turn_id"):
            continue
        if str(turn.get("user_question", "")).strip() != user_question.strip():
            continue
        return index
    return None


def update_conversation_turn(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Merge updates into an existing conversation turn and persist the result."""
    path = conversation_path(paths, conversation_id)
    with workspace_lease(paths, _conversation_resource(conversation_id)):
        conversation = read_json(path)
        if not conversation:
            raise FileNotFoundError(path)
        turns = conversation.get("turns", [])
        if not isinstance(turns, list):
            raise ValueError(f"`turns` in `{path}` must be a list.")
        updated_turn: dict[str, Any] | None = None
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("turn_id") != turn_id:
                continue
            updated_turn = _backfill_turn_runtime_fields(turn)
            break
        if updated_turn is None:
            raise KeyError(turn_id)

        for key, value in updates.items():
            if key in {"session_ids", "trace_ids", "captured_interaction_ids", "bundle_paths"}:
                current_values = updated_turn.get(key, [])
                if not isinstance(current_values, list):
                    current_values = []
                additional_values = value if isinstance(value, list) else [value]
                updated_turn[key] = _deduplicate_strings(
                    [
                        item
                        for item in [*current_values, *additional_values]
                        if isinstance(item, str)
                    ]
                )
                continue
            if key == "attachments":
                current_attachments = updated_turn.get("attachments", [])
                if not isinstance(current_attachments, list):
                    current_attachments = []
                additional_attachments = value if isinstance(value, list) else [value]
                deduped: list[dict[str, Any]] = []
                seen_attachment_ids: set[str] = set()
                for attachment in [*current_attachments, *additional_attachments]:
                    if not isinstance(attachment, dict):
                        continue
                    attachment_id = attachment.get("attachment_id")
                    key_value = (
                        attachment_id
                        if isinstance(attachment_id, str) and attachment_id
                        else json.dumps(attachment, sort_keys=True)
                    )
                    if key_value in seen_attachment_ids:
                        continue
                    seen_attachment_ids.add(key_value)
                    deduped.append(attachment)
                updated_turn[key] = deduped
                continue
            updated_turn[key] = value
        now = utc_now()
        updated_turn["updated_at"] = now
        if updated_turn.get("status") in {"completed", "answered", "action-required"}:
            updated_turn["completed_at"] = now
        conversation["updated_at"] = now
        conversation["workspace_snapshot"] = workspace_snapshot(paths)
        write_json(path, conversation)
        write_json(
            paths.active_conversation_path,
            {
                "conversation_id": conversation_id,
                "agent_surface": conversation.get("agent_surface", detect_agent_surface()),
                "updated_at": now,
            },
        )
        return updated_turn


def build_log_context(
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None = None,
    entry_workflow_id: str,
    inner_workflow_id: str,
    native_turn_id: str | None = None,
    question_class: str | None = None,
    question_domain: str | None = None,
    support_strategy: str | None = None,
    analysis_origin: str | None = None,
    support_basis: str | None = None,
    support_manifest_path: str | None = None,
) -> dict[str, str]:
    """Build log-linkage metadata for retrieval and trace records."""
    context = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "entry_workflow_id": entry_workflow_id,
        "inner_workflow_id": inner_workflow_id,
    }
    optional_fields = {
        "run_id": run_id,
        "native_turn_id": native_turn_id,
    }
    for field_name, value in optional_fields.items():
        if isinstance(value, str) and value:
            context[field_name] = value
    context.update(
        semantic_log_context_fields(
            question_class=question_class,
            question_domain=question_domain,
            support_strategy=support_strategy,
            analysis_origin=analysis_origin,
            support_basis=support_basis,
            support_manifest_path=support_manifest_path,
        )
    )
    return context

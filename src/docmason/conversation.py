"""Conversation-native runtime helpers for natural multi-turn DocMason usage."""

from __future__ import annotations

import hashlib
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
    "front_door_state",
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
FRONT_DOOR_STATE_NATIVE_RECONCILED_ONLY = "native-reconciled-only"
FRONT_DOOR_STATE_CANONICAL_ASK = "canonical-ask"
_FRONT_DOOR_STATE_PRIORITY = {
    None: 0,
    FRONT_DOOR_STATE_NATIVE_RECONCILED_ONLY: 1,
    FRONT_DOOR_STATE_CANONICAL_ASK: 2,
}
_BINDABLE_HOST_IDENTITY_SOURCES = frozenset(
    {
        "codex_thread_id",
        "claude_session_id",
        "claude_conversation_id",
        "docmason_conversation_id",
    }
)
_HOST_IDENTITY_SOURCE_PRIORITY = {
    None: 0,
    "claude_project_dir": 1,
    "docmason_conversation_id": 2,
    "codex_thread_id": 3,
    "claude_conversation_id": 3,
    "claude_session_id": 4,
}
_HOST_IDENTITY_TRUST_PRIORITY = {
    None: 0,
    "unbound": 0,
    "weak-host-env": 1,
    "manual-override": 2,
    "host-env-claimed": 3,
    "reconciliation-argument": 4,
}


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


def _stable_json_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def normalize_front_door_state(value: Any) -> str | None:
    """Normalize one persisted front-door state into a supported value."""
    normalized = _nonempty_string(value)
    if normalized in {
        FRONT_DOOR_STATE_NATIVE_RECONCILED_ONLY,
        FRONT_DOOR_STATE_CANONICAL_ASK,
    }:
        return normalized
    return None


def stronger_front_door_state(current: Any, candidate: Any) -> str | None:
    """Return the stronger of two front-door states without demotion."""
    current_state = normalize_front_door_state(current)
    candidate_state = normalize_front_door_state(candidate)
    if _FRONT_DOOR_STATE_PRIORITY[candidate_state] > _FRONT_DOOR_STATE_PRIORITY[current_state]:
        return candidate_state
    return current_state


def turn_has_canonical_ask_ownership(turn: dict[str, Any] | None) -> bool:
    """Return whether one turn is explicitly owned by canonical ask."""
    if not isinstance(turn, dict):
        return False
    return normalize_front_door_state(turn.get("front_door_state")) == FRONT_DOOR_STATE_CANONICAL_ASK


def conversation_has_canonical_ask_ownership(conversation: dict[str, Any] | None) -> bool:
    """Return whether one conversation contains any canonical-ask turn."""
    if not isinstance(conversation, dict):
        return False
    turns = conversation.get("turns", [])
    if not isinstance(turns, list):
        return False
    return any(
        turn_has_canonical_ask_ownership(turn)
        for turn in turns
        if isinstance(turn, dict)
    )


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
    if "attached_shared_job_ids" not in turn:
        turn["attached_shared_job_ids"] = []
    if "confirmation_kind" not in turn:
        turn["confirmation_kind"] = None
    if "confirmation_prompt" not in turn:
        turn["confirmation_prompt"] = None
    if "confirmation_reason" not in turn:
        turn["confirmation_reason"] = None
    if "hybrid_refresh_triggered" not in turn:
        turn["hybrid_refresh_triggered"] = False
    if "hybrid_refresh_sources" not in turn:
        turn["hybrid_refresh_sources"] = []
    if "hybrid_refresh_snapshot_id" not in turn:
        turn["hybrid_refresh_snapshot_id"] = None
    if "hybrid_refresh_job_ids" not in turn:
        turn["hybrid_refresh_job_ids"] = []
    if "hybrid_refresh_completion_status" not in turn:
        turn["hybrid_refresh_completion_status"] = None
    if "hybrid_refresh_summary" not in turn:
        turn["hybrid_refresh_summary"] = None
    if "front_door_state" not in turn:
        turn["front_door_state"] = None
    else:
        turn["front_door_state"] = normalize_front_door_state(turn.get("front_door_state"))
    if "front_door_opened_at" not in turn:
        turn["front_door_opened_at"] = None
    if "front_door_run_id" not in turn:
        turn["front_door_run_id"] = None
    if "native_ledger_ref" not in turn:
        turn["native_ledger_ref"] = None
    if "promotion_kind" not in turn:
        turn["promotion_kind"] = None
    if "promotion_reason" not in turn:
        turn["promotion_reason"] = None
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


def current_host_identity(*, agent_surface: str | None = None) -> dict[str, Any]:
    """Return the normalized host identity envelope for the current execution context."""
    provider = agent_surface or detect_agent_surface()
    explicit_surface = _nonempty_string(os.environ.get("DOCMASON_AGENT_SURFACE"))
    explicit_conversation_id = _nonempty_string(os.environ.get("DOCMASON_CONVERSATION_ID"))
    codex_thread_id = _nonempty_string(os.environ.get("CODEX_THREAD_ID"))
    claude_session_id = _nonempty_string(os.environ.get("CLAUDE_SESSION_ID"))
    claude_conversation_id = _nonempty_string(os.environ.get("CLAUDE_CONVERSATION_ID"))
    claude_project_dir = _nonempty_string(os.environ.get("CLAUDE_PROJECT_DIR"))

    host_thread_ref = None
    host_identity_source = None
    host_identity_trust = "unbound"
    anomaly_flags: list[str] = []

    if provider == "codex" and codex_thread_id:
        host_thread_ref = codex_thread_id
        host_identity_source = "codeX_thread_id".lower()
        host_identity_trust = "host-env-claimed"
    elif provider == "claude-code" and claude_session_id:
        host_thread_ref = claude_session_id
        host_identity_source = "claude_session_id"
        host_identity_trust = "host-env-claimed"
    elif provider == "claude-code" and claude_conversation_id:
        host_thread_ref = claude_conversation_id
        host_identity_source = "claude_conversation_id"
        host_identity_trust = "host-env-claimed"
    elif provider == "claude-code" and claude_project_dir:
        host_thread_ref = claude_project_dir
        host_identity_source = "claude_project_dir"
        host_identity_trust = "weak-host-env"
    elif explicit_conversation_id:
        host_thread_ref = explicit_conversation_id
        host_identity_source = "docmason_conversation_id"
        host_identity_trust = "manual-override"

    if explicit_surface and explicit_surface != provider:
        anomaly_flags.extend(["anomalous-host-identity", "provider-surface-mismatch"])
    if explicit_conversation_id:
        trusted_host_ref = None
        if codex_thread_id:
            trusted_host_ref = codex_thread_id
        elif claude_session_id:
            trusted_host_ref = claude_session_id
        elif claude_conversation_id:
            trusted_host_ref = claude_conversation_id
        if trusted_host_ref and trusted_host_ref != explicit_conversation_id:
            anomaly_flags.extend(["anomalous-host-identity", "manual-alias-override"])
    if provider == "unknown-agent" and host_thread_ref:
        anomaly_flags.extend(["anomalous-host-identity", "unknown-provider-host-ref"])

    return {
        "host_provider": provider,
        "host_thread_ref": host_thread_ref,
        "host_identity_source": host_identity_source,
        "host_identity_trust": host_identity_trust,
        "anomaly_flags": _deduplicate_strings(anomaly_flags),
    }


def host_identity_is_bindable(host_identity: dict[str, Any] | None) -> bool:
    """Return whether the host identity is strong enough for canonical binding."""
    if not isinstance(host_identity, dict):
        return False
    source = _nonempty_string(host_identity.get("host_identity_source"))
    thread_ref = _nonempty_string(host_identity.get("host_thread_ref"))
    return source in _BINDABLE_HOST_IDENTITY_SOURCES and thread_ref is not None


def host_identity_key(host_identity: dict[str, Any] | None) -> str | None:
    """Return a stable binding key for one host identity envelope when possible."""
    if not isinstance(host_identity, dict):
        return None
    provider = _nonempty_string(host_identity.get("host_provider"))
    thread_ref = _nonempty_string(host_identity.get("host_thread_ref"))
    if provider is None or thread_ref is None or not host_identity_is_bindable(host_identity):
        return None
    return _stable_json_digest(
        {
            "host_provider": provider,
            "host_thread_ref": thread_ref,
        }
    )


def _host_identity_priority(host_identity: dict[str, Any] | None) -> tuple[int, int, int]:
    if not isinstance(host_identity, dict):
        return (0, 0, 0)
    return (
        1 if host_identity_is_bindable(host_identity) else 0,
        _HOST_IDENTITY_SOURCE_PRIORITY.get(
            _nonempty_string(host_identity.get("host_identity_source")),
            0,
        ),
        _HOST_IDENTITY_TRUST_PRIORITY.get(
            _nonempty_string(host_identity.get("host_identity_trust")),
            0,
        ),
    )


def _should_upgrade_host_identity(
    current: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
) -> bool:
    if not isinstance(candidate, dict) or not candidate:
        return False
    if not isinstance(current, dict) or not current:
        return True
    return _host_identity_priority(candidate) > _host_identity_priority(current)


def load_host_identity_bindings(paths: WorkspacePaths) -> dict[str, Any]:
    """Load host-identity to canonical-conversation bindings."""
    payload = read_json(paths.host_identity_bindings_path)
    bindings = payload.get("bindings")
    return {
        "schema_version": int(payload.get("schema_version", 1) or 1),
        "updated_at": payload.get("updated_at"),
        "bindings": bindings if isinstance(bindings, dict) else {},
    }


def _write_host_identity_bindings(
    paths: WorkspacePaths,
    *,
    bindings: dict[str, Any],
) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "bindings": bindings,
    }
    write_json(paths.host_identity_bindings_path, payload)
    return payload


def bind_host_identity_to_conversation(
    paths: WorkspacePaths,
    *,
    host_identity: dict[str, Any] | None,
    conversation_id: str,
) -> None:
    """Persist the canonical conversation bound to one host identity envelope."""
    key = host_identity_key(host_identity)
    if key is None:
        return
    bindings = load_host_identity_bindings(paths)["bindings"]
    bindings[key] = {
        "conversation_id": conversation_id,
        "host_identity": dict(host_identity or {}),
        "bound_at": utc_now(),
    }
    _write_host_identity_bindings(paths, bindings=bindings)


def _active_conversation_fallback_id(
    paths: WorkspacePaths,
    *,
    agent_surface: str,
    host_identity: dict[str, Any] | None,
) -> str | None:
    current_host_identity_key = host_identity_key(host_identity)
    active = read_json(paths.active_conversation_path)
    active_id = active.get("conversation_id")
    active_agent = active.get("agent_surface")
    active_host_identity = active.get("host_identity")
    active_host_identity_key = _nonempty_string(active.get("host_identity_key"))
    active_effective_key = active_host_identity_key or host_identity_key(active_host_identity)
    updated_at = _parse_timestamp(active.get("updated_at"))
    if (
        isinstance(active_id, str)
        and active_id
        and active_agent == agent_surface
        and updated_at is not None
        and datetime.now(tz=UTC) - updated_at <= ACTIVE_CONVERSATION_IDLE_WINDOW
        and (
            current_host_identity_key is None
            or active_effective_key == current_host_identity_key
            or active_effective_key is None
        )
    ):
        return active_id

    legacy_active = read_json(paths.legacy_active_conversation_path)
    legacy_id = legacy_active.get("conversation_id")
    if (
        current_host_identity_key is None
        and isinstance(legacy_id, str)
        and legacy_id
        and _conversation_record_exists(paths, legacy_id)
    ):
        return legacy_id
    return None


def bound_conversation_id_for_host(
    paths: WorkspacePaths,
    *,
    host_identity: dict[str, Any] | None,
) -> str | None:
    """Return the canonical conversation bound to the current host identity, when present."""
    key = host_identity_key(host_identity)
    if key is None:
        return None
    binding = load_host_identity_bindings(paths)["bindings"].get(key)
    if not isinstance(binding, dict):
        legacy_host_ref = (
            _nonempty_string(host_identity.get("host_thread_ref"))
            if isinstance(host_identity, dict)
            else None
        )
        legacy_conversation = (
            load_conversation_record(paths, legacy_host_ref)
            if legacy_host_ref and _conversation_record_exists(paths, legacy_host_ref)
            else {}
        )
        if legacy_host_ref and conversation_has_canonical_ask_ownership(legacy_conversation):
            bind_host_identity_to_conversation(
                paths,
                host_identity=host_identity,
                conversation_id=legacy_host_ref,
            )
            return legacy_host_ref
        return None
    conversation_id = _nonempty_string(binding.get("conversation_id"))
    if conversation_id and _conversation_record_exists(paths, conversation_id):
        return conversation_id
    return None


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
    """Return the current host-native thread reference when one is available."""
    host_identity = current_host_identity()
    return (
        _nonempty_string(host_identity.get("host_thread_ref")),
        _nonempty_string(host_identity.get("host_identity_source")),
    )


def _load_active_conversation(paths: WorkspacePaths) -> dict[str, Any]:
    active = read_json(paths.active_conversation_path)
    if active:
        return active
    return read_json(paths.legacy_active_conversation_path)


def load_active_conversation_record(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the current active conversation record when it still exists."""
    active = _load_active_conversation(paths)
    conversation_id = active.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        return {}
    return load_conversation_record(paths, conversation_id)


def load_bound_conversation_record_for_host(
    paths: WorkspacePaths,
    *,
    host_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the canonical conversation currently bound to the host identity envelope."""
    resolved_host_identity = (
        dict(host_identity) if isinstance(host_identity, dict) else current_host_identity()
    )
    conversation_id = bound_conversation_id_for_host(
        paths,
        host_identity=resolved_host_identity,
    )
    if not isinstance(conversation_id, str) or not conversation_id:
        conversation_id = _active_conversation_fallback_id(
            paths,
            agent_surface=str(
                resolved_host_identity.get("host_provider") or detect_agent_surface()
            ),
            host_identity=resolved_host_identity,
        )
    if not isinstance(conversation_id, str) or not conversation_id:
        return {}
    return load_conversation_record(paths, conversation_id)


def _conversation_record_exists(paths: WorkspacePaths, conversation_id: str) -> bool:
    return conversation_path(paths, conversation_id).exists() or legacy_conversation_path(
        paths, conversation_id
    ).exists()


def resolve_conversation_id(paths: WorkspacePaths, *, agent_surface: str) -> tuple[str, str]:
    """Resolve the canonical conversation identifier for the current chat."""
    host_identity = current_host_identity(agent_surface=agent_surface)
    bound_conversation_id = bound_conversation_id_for_host(paths, host_identity=host_identity)
    if bound_conversation_id is not None:
        return bound_conversation_id, "host-identity-binding"

    active_fallback_id = _active_conversation_fallback_id(
        paths,
        agent_surface=agent_surface,
        host_identity=host_identity,
    )
    if isinstance(active_fallback_id, str) and active_fallback_id:
        return active_fallback_id, "workspace-active-fallback"

    conversation_id = str(uuid.uuid4())
    bind_host_identity_to_conversation(
        paths,
        host_identity=host_identity,
        conversation_id=conversation_id,
    )
    return conversation_id, "generated"


def conversation_path(paths: WorkspacePaths, conversation_id: str) -> Path:
    """Return the runtime path for one parent conversation record."""
    return paths.conversations_dir / f"{conversation_id}.json"


def legacy_conversation_path(paths: WorkspacePaths, conversation_id: str) -> Path:
    """Return the legacy projection path for one parent conversation record."""
    return paths.conversation_projections_dir / f"{conversation_id}.json"


def answer_file_path(paths: WorkspacePaths, *, conversation_id: str, turn_id: str) -> Path:
    """Return the canonical answer-file path for one conversation turn."""
    return paths.answers_dir / conversation_id / f"{turn_id}.md"


def load_conversation_record(paths: WorkspacePaths, conversation_id: str) -> dict[str, Any]:
    """Load one persisted conversation record."""
    conversation = read_json(conversation_path(paths, conversation_id))
    if not conversation:
        conversation = read_json(legacy_conversation_path(paths, conversation_id))
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
        "native_ledger_ref": None,
        "promotion_kind": None,
        "promotion_reason": None,
        "turn_state": "opened",
        "version_context": None,
        "capability_profile": None,
        "attached_shared_job_ids": [],
        "confirmation_kind": None,
        "confirmation_prompt": None,
        "confirmation_reason": None,
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
        "hybrid_refresh_snapshot_id": None,
        "hybrid_refresh_job_ids": [],
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
    host_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load or initialize one parent conversation record."""
    now = utc_now()
    conversation = load_conversation_record(paths, conversation_id)
    if conversation:
        if _should_upgrade_host_identity(
            conversation.get("host_identity")
            if isinstance(conversation.get("host_identity"), dict)
            else None,
            host_identity,
        ):
            conversation["host_identity"] = dict(host_identity)
            conversation["host_identity_key"] = host_identity_key(host_identity)
            write_json(conversation_path(paths, conversation_id), conversation)
        if host_identity_is_bindable(host_identity):
            bind_host_identity_to_conversation(
                paths,
                host_identity=host_identity,
                conversation_id=conversation_id,
            )
        if not conversation_path(paths, conversation_id).exists():
            ensure_json_parent(conversation_path(paths, conversation_id))
            write_json(conversation_path(paths, conversation_id), conversation)
        return conversation
    conversation = {
        "conversation_id": conversation_id,
        "conversation_id_source": conversation_id_source,
        "agent_surface": agent_surface,
        "host_identity": dict(host_identity or {}),
        "host_identity_key": host_identity_key(host_identity),
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
    if latest_turn.get("turn_state") in {"awaiting-confirmation", "waiting-shared-job"}:
        return len(turns) - 1
    if (
        normalize_front_door_state(latest_turn.get("front_door_state"))
        == FRONT_DOOR_STATE_NATIVE_RECONCILED_ONLY
    ):
        return len(turns) - 1
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
    host_identity = current_host_identity(agent_surface=agent_surface)
    host_identity_key_value = host_identity_key(host_identity)
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
            host_identity=host_identity,
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
                "host_identity": dict(host_identity),
                "host_identity_key": host_identity_key_value,
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
        "host_identity": dict(host_identity),
        "host_identity_key": host_identity_key_value,
        "workspace_snapshot": conversation["workspace_snapshot"],
        "native_turn_id": turn.get("native_turn_id"),
        "front_door_state": turn.get("front_door_state"),
        "front_door_opened_at": turn.get("front_door_opened_at"),
        "front_door_run_id": turn.get("front_door_run_id"),
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
            conversation = read_json(legacy_conversation_path(paths, conversation_id))
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
            if key in {
                "session_ids",
                "trace_ids",
                "captured_interaction_ids",
                "bundle_paths",
                "attached_shared_job_ids",
            }:
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
                "host_identity": (
                    dict(conversation.get("host_identity"))
                    if isinstance(conversation.get("host_identity"), dict)
                    else {}
                ),
                "host_identity_key": conversation.get("host_identity_key"),
                "updated_at": now,
            },
        )
        return updated_turn


def latest_conversation_turn(conversation: dict[str, Any]) -> dict[str, Any] | None:
    """Return the latest turn from one loaded conversation record."""
    turns = conversation.get("turns", [])
    if not isinstance(turns, list) or not turns:
        return None
    latest = turns[-1]
    if not isinstance(latest, dict):
        return None
    return _backfill_turn_runtime_fields(latest)


def build_log_context(
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None = None,
    entry_workflow_id: str,
    inner_workflow_id: str,
    native_turn_id: str | None = None,
    front_door_state: str | None = None,
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
        "front_door_state": normalize_front_door_state(front_door_state),
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

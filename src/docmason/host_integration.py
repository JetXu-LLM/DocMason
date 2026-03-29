"""Hidden host-integration wrapper for canonical ask lifecycle ownership."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .ask import (
    _commit_governed_boundary_turn,
    complete_ask_turn,
    prepare_ask_turn,
    quarantine_noncanonical_answer_file,
)
from .conversation import (
    bind_host_identity_to_conversation,
    current_host_identity,
    detect_agent_surface,
    load_turn_record,
    update_conversation_turn,
)
from .project import WorkspacePaths, locate_workspace


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _resolved_host_identity(
    *,
    host_provider: str | None,
    host_thread_ref: str | None,
    host_identity_source: str | None,
) -> dict[str, Any]:
    provider = _nonempty_string(host_provider) or detect_agent_surface()
    identity = current_host_identity(agent_surface=provider)
    identity["host_provider"] = provider
    normalized_ref = _nonempty_string(host_thread_ref)
    normalized_source = _nonempty_string(host_identity_source)
    if normalized_source is None and normalized_ref is not None:
        if provider == "codex":
            normalized_source = "codex_thread_id"
        elif provider == "claude-code":
            normalized_source = "claude_session_id"
        else:
            normalized_source = "hidden-host-wrapper"
    if normalized_ref is not None:
        identity["host_thread_ref"] = normalized_ref
        identity["host_identity_source"] = normalized_source
        identity["host_identity_trust"] = "reconciliation-argument"
    elif normalized_source is not None:
        identity["host_identity_source"] = normalized_source
    return identity


def _persist_host_turn_context(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    host_identity: dict[str, Any],
    semantic_analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {
        "host_provider": _nonempty_string(host_identity.get("host_provider")),
        "host_thread_ref": _nonempty_string(host_identity.get("host_thread_ref")),
        "host_identity_source": _nonempty_string(host_identity.get("host_identity_source")),
    }
    if isinstance(semantic_analysis, dict):
        updates["semantic_analysis"] = dict(semantic_analysis)
    updated = update_conversation_turn(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        updates=updates,
    )
    bind_host_identity_to_conversation(
        paths,
        host_identity=host_identity,
        conversation_id=conversation_id,
    )
    return {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        **updated,
    }


def _answer_text(paths: WorkspacePaths, answer_file_path: str | None) -> str | None:
    relative = _nonempty_string(answer_file_path)
    if relative is None:
        return None
    path = Path(relative)
    if not path.is_absolute():
        path = paths.root / path
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def _next_step(status: str) -> str:
    if status == "execute":
        return "continue-inner-workflow"
    if status == "awaiting-confirmation":
        return "wait-for-user-confirmation"
    if status == "waiting-shared-job":
        return "wait-for-shared-job"
    if status == "boundary":
        return "return-boundary-answer"
    return "do-not-return-final-answer"


def _open_status(response: dict[str, Any]) -> tuple[str, bool]:
    status = str(response.get("status") or "")
    support_basis = str(response.get("support_basis") or "")
    answer_state = str(response.get("answer_state") or "")
    if status == "prepared":
        return "execute", False
    if status == "awaiting-confirmation":
        return "awaiting-confirmation", False
    if status == "waiting-shared-job":
        return "waiting-shared-job", False
    if status == "completed" and (
        support_basis == "governed-boundary" or answer_state == "abstained"
    ):
        return "boundary", True
    return "blocked", False


def open_canonical_ask(
    paths: WorkspacePaths,
    *,
    question: str,
    semantic_analysis: dict[str, Any] | None = None,
    log_origin: str | None = None,
    host_provider: str | None = None,
    host_thread_ref: str | None = None,
    host_identity_source: str | None = None,
) -> dict[str, Any]:
    """Open one canonical ask turn through the hidden host wrapper."""
    prepared = prepare_ask_turn(
        paths,
        question=question,
        semantic_analysis=semantic_analysis,
        log_origin=log_origin,
    )
    conversation_id = _nonempty_string(prepared.get("conversation_id"))
    turn_id = _nonempty_string(prepared.get("turn_id"))
    if conversation_id is None or turn_id is None:
        return {
            "status": "blocked",
            "user_reply_allowed": False,
            "detail": "Canonical ask did not return a stable turn reference.",
            "next_step": _next_step("blocked"),
        }
    host_identity = _resolved_host_identity(
        host_provider=host_provider,
        host_thread_ref=host_thread_ref,
        host_identity_source=host_identity_source,
    )
    persisted_turn = _persist_host_turn_context(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        host_identity=host_identity,
        semantic_analysis=semantic_analysis,
    )
    reference_resolution = (
        _mapping(prepared.get("reference_resolution"))
        if isinstance(prepared.get("reference_resolution"), dict)
        else {}
    )
    if (
        str(prepared.get("status") or "") == "prepared"
        and bool(reference_resolution.get("hard_boundary"))
        and str(reference_resolution.get("unresolved_reason") or "") == "missing-source"
        and not bool(prepared.get("auto_sync_triggered"))
        and not _string_list(prepared.get("attached_shared_job_ids"))
    ):
        boundary_reason = _nonempty_string(reference_resolution.get("notice_text")) or (
            "I could not find the requested published source, so I am stopping at that "
            "boundary."
        )
        committed_boundary = _commit_governed_boundary_turn(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
            reason=boundary_reason,
            extra_turn_updates={
                "reference_resolution": reference_resolution,
                "reference_resolution_summary": _nonempty_string(
                    prepared.get("reference_resolution_summary")
                ),
                "source_scope_policy": _mapping(prepared.get("source_scope_policy")) or None,
                "host_provider": _nonempty_string(host_identity.get("host_provider")),
                "host_thread_ref": _nonempty_string(host_identity.get("host_thread_ref")),
                "host_identity_source": _nonempty_string(
                    host_identity.get("host_identity_source")
                ),
            },
        )
        return {
            "status": "boundary",
            "user_reply_allowed": True,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "run_id": _nonempty_string(committed_boundary.get("committed_run_id")),
            "inner_workflow_id": _nonempty_string(prepared.get("inner_workflow_id")),
            "question_domain": _nonempty_string(prepared.get("question_domain")),
            "support_strategy": _nonempty_string(prepared.get("support_strategy")),
            "answer_file_path": _nonempty_string(committed_boundary.get("answer_file_path")),
            "bundle_paths": _string_list(prepared.get("bundle_paths")),
            "reference_resolution": reference_resolution or None,
            "source_scope_policy": _mapping(prepared.get("source_scope_policy")) or None,
            "preferred_channels": _string_list(prepared.get("preferred_channels")),
            "inspection_scope": _nonempty_string(prepared.get("inspection_scope")),
            "detail": boundary_reason,
            "next_step": _next_step("boundary"),
            "answer_text": _answer_text(paths, committed_boundary.get("answer_file_path")),
            "host_provider": _nonempty_string(host_identity.get("host_provider")),
            "host_thread_ref": _nonempty_string(host_identity.get("host_thread_ref")),
            "host_identity_source": _nonempty_string(host_identity.get("host_identity_source")),
        }
    status, user_reply_allowed = _open_status({**prepared, **persisted_turn})
    answer_text = (
        _answer_text(paths, persisted_turn.get("answer_file_path"))
        if user_reply_allowed
        else None
    )
    return {
        "status": status,
        "user_reply_allowed": user_reply_allowed,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "run_id": _nonempty_string(prepared.get("run_id") or persisted_turn.get("active_run_id")),
        "inner_workflow_id": _nonempty_string(prepared.get("inner_workflow_id")),
        "question_domain": _nonempty_string(prepared.get("question_domain")),
        "support_strategy": _nonempty_string(prepared.get("support_strategy")),
        "answer_file_path": _nonempty_string(persisted_turn.get("answer_file_path")),
        "bundle_paths": _string_list(prepared.get("bundle_paths")),
        "reference_resolution": _mapping(prepared.get("reference_resolution")) or None,
        "source_scope_policy": _mapping(prepared.get("source_scope_policy")) or None,
        "preferred_channels": _string_list(prepared.get("preferred_channels")),
        "inspection_scope": _nonempty_string(prepared.get("inspection_scope")),
        "detail": _nonempty_string(
            prepared.get("freshness_notice")
            or prepared.get("detail")
            or prepared.get("route_reason")
        ),
        "next_step": _next_step(status),
        "answer_text": answer_text,
        "host_provider": _nonempty_string(host_identity.get("host_provider")),
        "host_thread_ref": _nonempty_string(host_identity.get("host_thread_ref")),
        "host_identity_source": _nonempty_string(host_identity.get("host_identity_source")),
    }


def _disallowed_finalize_override(request: dict[str, Any]) -> str | None:
    for field_name in ("answer_state", "reference_resolution"):
        if field_name in request:
            return field_name
    explicit_support_basis = _nonempty_string(request.get("support_basis"))
    if explicit_support_basis is not None and explicit_support_basis not in {
        "external-source-verified",
        "mixed",
    }:
        return "support_basis"
    return None


def finalize_canonical_ask(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Finalize one canonical ask turn through the hidden host wrapper."""
    turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    committed_run_id = _nonempty_string(turn.get("committed_run_id"))
    if committed_run_id is not None:
        return {
            "status": "blocked",
            "user_reply_allowed": False,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "run_id": committed_run_id,
            "detail": "already-committed-canonical-turn",
            "primary_issue_code": "already-committed-canonical-turn",
            "issue_codes": ["already-committed-canonical-turn"],
        }
    illegal_override = _disallowed_finalize_override(request)
    if illegal_override is not None:
        return {
            "status": "blocked",
            "user_reply_allowed": False,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "run_id": _nonempty_string(turn.get("active_run_id")),
            "detail": (
                "ordinary-kb-finalize-disallows-canonical-truth-override:"
                + illegal_override
            ),
            "primary_issue_code": "illegal-finalize-override",
            "issue_codes": ["illegal-finalize-override"],
        }
    try:
        completed = complete_ask_turn(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
            inner_workflow_id=str(turn.get("inner_workflow_id") or "grounded-answer"),
            answer_file_path=_nonempty_string(request.get("answer_file_path")),
            response_excerpt=_nonempty_string(request.get("response_excerpt")),
            support_basis=_nonempty_string(request.get("support_basis")),
            support_manifest_path=_nonempty_string(request.get("support_manifest_path")),
            support_manifest_sources=(
                request.get("support_manifest_sources")
                if isinstance(request.get("support_manifest_sources"), list)
                else None
            ),
            support_manifest_key_assertions=(
                request.get("support_manifest_key_assertions")
                if isinstance(request.get("support_manifest_key_assertions"), list)
                else None
            ),
            support_manifest_notes=_nonempty_string(request.get("support_manifest_notes")),
        )
    except Exception as exc:
        quarantined_path = quarantine_noncanonical_answer_file(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
        failed_turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
        issue_codes = _string_list(failed_turn.get("issue_codes"))
        primary_issue_code = _nonempty_string(failed_turn.get("primary_issue_code"))
        if primary_issue_code is None and str(exc).strip():
            primary_issue_code = "finalize-blocked"
        if not issue_codes and primary_issue_code is not None:
            issue_codes = [primary_issue_code]
        return {
            "status": "blocked",
            "user_reply_allowed": False,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "run_id": _nonempty_string(failed_turn.get("active_run_id")),
            "detail": str(exc),
            "primary_issue_code": primary_issue_code,
            "issue_codes": issue_codes,
            "noncanonical_answer_file_path": quarantined_path,
        }
    completed_turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    support_basis = _nonempty_string(completed_turn.get("support_basis"))
    answer_state = _nonempty_string(completed_turn.get("answer_state"))
    status = (
        "boundary"
        if support_basis == "governed-boundary" or answer_state == "abstained"
        else "completed"
    )
    return {
        "status": status,
        "user_reply_allowed": True,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "run_id": _nonempty_string(
            completed_turn.get("committed_run_id") or completed.get("committed_run_id")
        ),
        "answer_text": _answer_text(paths, completed_turn.get("answer_file_path")),
        "answer_state": answer_state,
        "support_basis": support_basis,
        "session_ids": _string_list(completed_turn.get("session_ids")),
        "trace_ids": _string_list(completed_turn.get("trace_ids")),
        "detail": _nonempty_string(completed_turn.get("response_excerpt")),
        "primary_issue_code": _nonempty_string(completed_turn.get("primary_issue_code")),
        "issue_codes": _string_list(completed_turn.get("issue_codes")),
    }


def handle_hidden_ask_request(
    request: dict[str, Any],
    *,
    paths: WorkspacePaths | None = None,
) -> dict[str, Any]:
    """Handle one hidden host-integration ask request."""
    workspace = paths or locate_workspace()
    action = _nonempty_string(request.get("action"))
    if action == "open":
        question = _nonempty_string(request.get("question"))
        if question is None:
            return {
                "status": "blocked",
                "user_reply_allowed": False,
                "detail": "Hidden ask open requires a non-empty question.",
                "primary_issue_code": "missing-question",
                "issue_codes": ["missing-question"],
            }
        return open_canonical_ask(
            workspace,
            question=question,
            semantic_analysis=(
                request.get("semantic_analysis")
                if isinstance(request.get("semantic_analysis"), dict)
                else None
            ),
            log_origin=_nonempty_string(request.get("log_origin")),
            host_provider=_nonempty_string(request.get("host_provider")),
            host_thread_ref=_nonempty_string(request.get("host_thread_ref")),
            host_identity_source=_nonempty_string(request.get("host_identity_source")),
        )
    if action == "finalize":
        conversation_id = _nonempty_string(request.get("conversation_id"))
        turn_id = _nonempty_string(request.get("turn_id"))
        if conversation_id is None or turn_id is None:
            return {
                "status": "blocked",
                "user_reply_allowed": False,
                "detail": "Hidden ask finalize requires conversation_id and turn_id.",
                "primary_issue_code": "missing-turn-reference",
                "issue_codes": ["missing-turn-reference"],
            }
        return finalize_canonical_ask(
            workspace,
            conversation_id=conversation_id,
            turn_id=turn_id,
            request=request,
        )
    return {
        "status": "blocked",
        "user_reply_allowed": False,
        "detail": "Hidden ask action must be `open` or `finalize`.",
        "primary_issue_code": "unsupported-hidden-ask-action",
        "issue_codes": ["unsupported-hidden-ask-action"],
    }


def run_hidden_ask_cli(stdin_text: str) -> int:
    """Run the hidden host-integration ask wrapper from stdin JSON."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {"action": None}
    result = handle_hidden_ask_request(payload if isinstance(payload, dict) else {})
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0

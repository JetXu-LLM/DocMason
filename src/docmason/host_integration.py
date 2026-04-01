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
    settle_lane_c_shared_refresh,
)
from .conversation import (
    bind_host_identity_to_conversation,
    build_log_context,
    current_host_identity,
    detect_agent_surface,
    load_turn_record,
    update_conversation_turn,
)
from .project import WorkspacePaths, locate_workspace
from .release_entry import maybe_run_release_entry_check

_WAITING_TURN_STATES = frozenset({"awaiting-confirmation", "waiting-shared-job"})


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
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
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


def _status_from_turn(turn: dict[str, Any]) -> tuple[str, bool]:
    support_basis = _nonempty_string(turn.get("support_basis"))
    answer_state = _nonempty_string(turn.get("answer_state"))
    committed_run_id = _nonempty_string(turn.get("committed_run_id"))
    turn_state = _nonempty_string(turn.get("turn_state") or turn.get("status"))
    if committed_run_id is not None:
        if support_basis == "governed-boundary" or answer_state == "abstained":
            return "boundary", True
        return "completed", True
    if turn_state == "prepared":
        return "execute", False
    if turn_state == "awaiting-confirmation":
        return "awaiting-confirmation", False
    if turn_state == "waiting-shared-job":
        return "waiting-shared-job", False
    return "blocked", False


def _turn_log_context(turn: dict[str, Any]) -> dict[str, str] | None:
    conversation_id = _nonempty_string(turn.get("conversation_id"))
    turn_id = _nonempty_string(turn.get("turn_id"))
    entry_workflow_id = _nonempty_string(turn.get("entry_workflow_id"))
    inner_workflow_id = _nonempty_string(turn.get("inner_workflow_id"))
    if (
        conversation_id is None
        or turn_id is None
        or entry_workflow_id is None
        or inner_workflow_id is None
    ):
        return None
    return build_log_context(
        conversation_id=conversation_id,
        turn_id=turn_id,
        run_id=_nonempty_string(turn.get("active_run_id")),
        entry_workflow_id=entry_workflow_id,
        inner_workflow_id=inner_workflow_id,
        native_turn_id=_nonempty_string(turn.get("native_turn_id")),
        front_door_state=_nonempty_string(turn.get("front_door_state")),
        question_class=_nonempty_string(turn.get("question_class")),
        question_domain=_nonempty_string(turn.get("question_domain")),
        support_strategy=_nonempty_string(turn.get("support_strategy")),
        analysis_origin=_nonempty_string(turn.get("analysis_origin")),
        support_basis=_nonempty_string(turn.get("support_basis")),
        support_manifest_path=_nonempty_string(turn.get("support_manifest_path")),
    )


def _turn_detail(turn: dict[str, Any], *, fallback: str | None = None) -> str | None:
    for candidate in (
        fallback,
        _nonempty_string(turn.get("freshness_notice")),
        _nonempty_string(turn.get("confirmation_prompt")),
        _nonempty_string(turn.get("response_excerpt")),
        _nonempty_string(turn.get("route_reason")),
        _nonempty_string(turn.get("primary_issue_code")),
    ):
        if candidate is not None:
            return candidate
    return None


def _hidden_ask_exception_payload(
    *,
    action: str | None,
    exc: Exception,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    normalized_action = action if action in {"open", "progress", "finalize"} else "request"
    detail = str(exc).strip() or type(exc).__name__
    payload = {
        "status": "blocked",
        "user_reply_allowed": False,
        "detail": f"Hidden ask {normalized_action} failed: {detail}",
        "primary_issue_code": f"hidden-ask-{normalized_action}-failed",
        "issue_codes": [f"hidden-ask-{normalized_action}-failed"],
    }
    if conversation_id is not None:
        payload["conversation_id"] = conversation_id
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return payload


def _bundle_notice(turn: dict[str, Any], *, user_reply_allowed: bool) -> str | None:
    if not user_reply_allowed:
        return None
    if _nonempty_string(turn.get("inner_workflow_id")) != "grounded-composition":
        return None
    bundle_paths = _string_list(turn.get("bundle_paths"))
    if not bundle_paths:
        return None
    return f"Bundle artifacts available at {bundle_paths[0]}."


def _host_turn_payload(
    paths: WorkspacePaths,
    *,
    turn: dict[str, Any],
    detail: str | None = None,
    include_release_entry: bool = False,
) -> dict[str, Any]:
    status, user_reply_allowed = _status_from_turn(turn)
    answer_text = (
        _answer_text(paths, _nonempty_string(turn.get("answer_file_path")))
        if user_reply_allowed
        else None
    )
    release_entry_notice = None
    release_entry_status = None
    if include_release_entry and user_reply_allowed:
        try:
            release_entry = maybe_run_release_entry_check(paths)
        except Exception:
            release_entry = {"notice": None, "release_entry_status": None}
        release_entry_notice = _nonempty_string(release_entry.get("notice"))
        release_entry_status = release_entry.get("release_entry_status")
        if answer_text is not None and release_entry_notice is not None:
            answer_text = f"{answer_text}\n\n{release_entry_notice}"
    return {
        "status": status,
        "user_reply_allowed": user_reply_allowed,
        "conversation_id": _nonempty_string(turn.get("conversation_id")),
        "turn_id": _nonempty_string(turn.get("turn_id")),
        "run_id": _nonempty_string(turn.get("committed_run_id") or turn.get("active_run_id")),
        "answer_text": answer_text,
        "answer_state": _nonempty_string(turn.get("answer_state")),
        "support_basis": _nonempty_string(turn.get("support_basis")),
        "session_ids": _string_list(turn.get("session_ids")),
        "trace_ids": _string_list(turn.get("trace_ids")),
        "detail": _turn_detail(turn, fallback=detail),
        "primary_issue_code": _nonempty_string(turn.get("primary_issue_code")),
        "issue_codes": _string_list(turn.get("issue_codes")),
        "answer_file_path": _nonempty_string(turn.get("answer_file_path")),
        "bundle_paths": _string_list(turn.get("bundle_paths")),
        "bundle_notice": _bundle_notice(turn, user_reply_allowed=user_reply_allowed),
        "inner_workflow_id": _nonempty_string(turn.get("inner_workflow_id")),
        "question_domain": _nonempty_string(turn.get("question_domain")),
        "support_strategy": _nonempty_string(turn.get("support_strategy")),
        "reference_resolution": _mapping(turn.get("reference_resolution")) or None,
        "reference_resolution_summary": _nonempty_string(
            turn.get("reference_resolution_summary")
        ),
        "source_scope_policy": _mapping(turn.get("source_scope_policy")) or None,
        "preferred_channels": _string_list(turn.get("preferred_channels")),
        "inspection_scope": _nonempty_string(turn.get("inspection_scope")),
        "next_step": _next_step(status),
        "host_provider": _nonempty_string(turn.get("host_provider")),
        "host_thread_ref": _nonempty_string(turn.get("host_thread_ref")),
        "host_identity_source": _nonempty_string(turn.get("host_identity_source")),
        "log_context": _turn_log_context(turn),
        "canonical_turn_state": _nonempty_string(turn.get("turn_state")),
        "canonical_turn_status": _nonempty_string(turn.get("status")),
        "release_entry_notice": release_entry_notice,
        "release_entry_status": release_entry_status,
    }


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
        return _host_turn_payload(
            paths,
            turn={"conversation_id": conversation_id, "turn_id": turn_id, **committed_boundary},
            detail=boundary_reason,
        )
    return _host_turn_payload(
        paths,
        turn={
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            **persisted_turn,
            "active_run_id": _nonempty_string(
                prepared.get("run_id") or persisted_turn.get("active_run_id")
            ),
        },
        detail=_nonempty_string(
            prepared.get("freshness_notice")
            or prepared.get("detail")
            or prepared.get("route_reason")
        ),
    )


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
    if _nonempty_string(turn.get("turn_state")) in _WAITING_TURN_STATES:
        return _host_turn_payload(
            paths,
            turn={"conversation_id": conversation_id, "turn_id": turn_id, **turn},
        )
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
            session_ids=(
                _string_list(request.get("session_ids")) if "session_ids" in request else None
            ),
            trace_ids=(
                _string_list(request.get("trace_ids")) if "trace_ids" in request else None
            ),
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
    return _host_turn_payload(
        paths,
        turn={
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            **completed_turn,
            "committed_run_id": _nonempty_string(
                completed_turn.get("committed_run_id") or completed.get("committed_run_id")
            ),
        },
        include_release_entry=True,
    )


def progress_canonical_ask(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    request: dict[str, Any],
) -> dict[str, Any]:
    """Advance or inspect one governed ask turn without finalizing it."""
    turn = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        **load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id),
    }
    completion_status = _nonempty_string(
        request.get("hybrid_refresh_completion_status") or request.get("completion_status")
    )
    if completion_status is None:
        return _host_turn_payload(paths, turn=turn)
    if completion_status not in {"covered", "blocked"}:
        return {
            "status": "blocked",
            "user_reply_allowed": False,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "run_id": _nonempty_string(turn.get("active_run_id")),
            "detail": "Hidden ask progress only accepts `covered` or `blocked` settlement.",
            "primary_issue_code": "illegal-progress-settlement",
            "issue_codes": ["illegal-progress-settlement"],
        }
    if _nonempty_string(turn.get("turn_state")) != "waiting-shared-job":
        return _host_turn_payload(paths, turn=turn)
    hybrid_job_ids = _string_list(turn.get("hybrid_refresh_job_ids"))
    job_id = _nonempty_string(request.get("job_id"))
    if job_id is None:
        if len(hybrid_job_ids) != 1:
            return {
                "status": "blocked",
                "user_reply_allowed": False,
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "run_id": _nonempty_string(turn.get("active_run_id")),
                "detail": (
                    "Hidden ask progress could not isolate a single governed multimodal "
                    "refresh job."
                ),
                "primary_issue_code": "missing-progress-job-id",
                "issue_codes": ["missing-progress-job-id"],
            }
        job_id = hybrid_job_ids[0]
    settle_lane_c_shared_refresh(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        job_id=job_id,
        completion_status=completion_status,
        summary=_mapping(request.get("hybrid_refresh_summary")) or None,
    )
    updated_turn = {
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        **load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id),
    }
    return _host_turn_payload(paths, turn=updated_turn)


def handle_hidden_ask_request(
    request: dict[str, Any],
    *,
    paths: WorkspacePaths | None = None,
) -> dict[str, Any]:
    """Handle one hidden host-integration ask request."""
    workspace = paths or locate_workspace()
    action = _nonempty_string(request.get("action"))
    conversation_id = _nonempty_string(request.get("conversation_id"))
    turn_id = _nonempty_string(request.get("turn_id"))
    try:
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
        if action == "progress":
            if conversation_id is None or turn_id is None:
                return {
                    "status": "blocked",
                    "user_reply_allowed": False,
                    "detail": "Hidden ask progress requires conversation_id and turn_id.",
                    "primary_issue_code": "missing-turn-reference",
                    "issue_codes": ["missing-turn-reference"],
                }
            return progress_canonical_ask(
                workspace,
                conversation_id=conversation_id,
                turn_id=turn_id,
                request=request,
            )
        return {
            "status": "blocked",
            "user_reply_allowed": False,
            "detail": "Hidden ask action must be `open`, `progress`, or `finalize`.",
            "primary_issue_code": "unsupported-hidden-ask-action",
            "issue_codes": ["unsupported-hidden-ask-action"],
        }
    except Exception as exc:
        return _hidden_ask_exception_payload(
            action=action,
            exc=exc,
            conversation_id=conversation_id,
            turn_id=turn_id,
        )


def run_hidden_ask_cli(stdin_text: str) -> int:
    """Run the hidden host-integration ask wrapper from stdin JSON."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {"action": None}
    request = payload if isinstance(payload, dict) else {}
    try:
        result = handle_hidden_ask_request(request)
    except Exception as exc:
        result = _hidden_ask_exception_payload(
            action=_nonempty_string(request.get("action")),
            exc=exc,
            conversation_id=_nonempty_string(request.get("conversation_id")),
            turn_id=_nonempty_string(request.get("turn_id")),
        )
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0

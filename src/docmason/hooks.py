"""Internal best-effort handler for ChatGPT Work/Codex and Claude Code hooks.

This module translates host payloads into one shared semantic decision core and
writes host-specific JSONL mirror records to the runtime interaction-ingest
directory. It is called by the committed ``.codex/hooks/`` and
``.claude/hooks/`` shell adapters and exposed through a hidden ``_hook`` CLI
subcommand.

Not a public command surface. This is internal plumbing.
Hosts may skip project Hooks when the project or Hook definition is untrusted,
disabled, or blocked by policy. Canonical workflow correctness must therefore
never depend on this module running.

Hook event types handled:
- session (SessionStart + SessionEnd, distinguished by hook_event_name)
- prompt-submit (UserPromptSubmit)
- post-tool-use (PostToolUse)
- stop (Stop)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .conversation import (
    bound_conversation_id_for_host,
    claim_turn_closure_continuation,
    load_conversation_record,
)
from .project import WorkspacePaths, append_jsonl

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPORTED_EVENTS = frozenset({
    "session",
    "prompt-submit",
    "post-tool-use",
    "stop",
})

_RECORD_TYPE_MAP: dict[str, str] = {
    "SessionStart": "session-start",
    "SessionEnd": "session-end",
    "UserPromptSubmit": "prompt-submit",
    "PostToolUse": "tool-use",
    "Stop": "stop",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def _resolve_workspace_root() -> Path:
    """Resolve the workspace root directory.

    Uses the host-provided project root when available, otherwise falls back to CWD.
    """
    project_dir = os.environ.get("DOCMASON_PROJECT_DIR") or os.environ.get(
        "CLAUDE_PROJECT_DIR"
    )
    if project_dir:
        return Path(project_dir)
    return Path.cwd()


def _hook_host(payload: dict[str, Any] | None = None) -> str:
    explicit = os.environ.get("DOCMASON_HOOK_HOST")
    if explicit in {"codex", "chatgpt-work", "claude-code"}:
        return "chatgpt-work" if explicit in {"codex", "chatgpt-work"} else explicit
    if isinstance(payload, dict) and payload.get("host_provider") in {
        "codex",
        "chatgpt-work",
        "claude-code",
    }:
        return (
            "chatgpt-work"
            if payload.get("host_provider") in {"codex", "chatgpt-work"}
            else "claude-code"
        )
    return "claude-code" if os.environ.get("CLAUDE_PROJECT_DIR") else "chatgpt-work"


def _mirror_root(workspace_root: Path) -> Path:
    """Return the host-specific interaction mirror directory."""
    return workspace_root / "runtime" / "interaction-ingest" / _hook_host()


def _mirror_path(workspace_root: Path, session_id: str) -> Path:
    """Return the JSONL mirror file path for a session."""
    return _mirror_root(workspace_root) / f"{session_id}.jsonl"


def _append_record(workspace_root: Path, session_id: str, record: dict[str, Any]) -> None:
    """Append a single JSON record to the session JSONL mirror file."""
    path = _mirror_path(workspace_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, record)


def _optional_record_fields(
    payload: dict[str, Any],
    field_names: tuple[str, ...],
) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for field_name in field_names:
        if field_name not in payload:
            continue
        value = payload.get(field_name)
        if value in ("", None) or value == [] or value == {}:
            continue
        record[field_name] = value
    return record


def _maybe_refresh_session_start_skill_shims(workspace_root: Path) -> None:
    """Refresh thin repo-local skill shims for prepared Claude workspaces.

    This is intentionally narrower than full adapter sync:
    - only runs on already prepared self-contained workspaces
    - only refreshes the local shim layer used for slash-command discovery
    - never raises, because hook plumbing must remain best-effort
    """
    try:
        from .commands import sync_repo_local_skill_shims
        from .project import WorkspacePaths, bootstrap_state
    except Exception:
        return

    try:
        paths = WorkspacePaths(root=workspace_root)
        state = bootstrap_state(paths)
        if not isinstance(state, dict) or not state:
            return
        recorded_root = state.get("workspace_root")
        if isinstance(recorded_root, str) and recorded_root:
            if Path(recorded_root).resolve() != workspace_root.resolve():
                return
        if not bool(state.get("environment_ready")):
            return
        if str(state.get("isolation_grade") or "") != "self-contained":
            return
        if not paths.canonical_skills_dir.exists():
            return
        sync_repo_local_skill_shims(paths)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _handle_session_start(payload: dict[str, Any], workspace_root: Path) -> None:
    """Handle a SessionStart event."""
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    record: dict[str, Any] = {
        "record_type": "session-start",
        "session_id": session_id,
        "recorded_at": _utc_now(),
        "cwd": payload.get("cwd", ""),
        "transcript_path": payload.get("transcript_path", ""),
        "model": payload.get("model", ""),
        "source": payload.get("source", ""),
    }
    _append_record(workspace_root, session_id, record)
    _maybe_refresh_session_start_skill_shims(workspace_root)


def _handle_session_end(payload: dict[str, Any], workspace_root: Path) -> None:
    """Handle a SessionEnd event."""
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    record: dict[str, Any] = {
        "record_type": "session-end",
        "session_id": session_id,
        "recorded_at": _utc_now(),
        "reason": payload.get("reason", "other"),
        **_optional_record_fields(
            payload,
            (
                "session_end_reason",
                "stop_condition",
                "host_error_text",
                "error_text",
                "error",
                "hook_activity_state",
            ),
        ),
    }
    _append_record(workspace_root, session_id, record)


def _handle_prompt_submit(payload: dict[str, Any], workspace_root: Path) -> None:
    """Handle a UserPromptSubmit event."""
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    record: dict[str, Any] = {
        "record_type": "prompt-submit",
        "session_id": session_id,
        "recorded_at": _utc_now(),
        "prompt": payload.get("prompt", ""),
        "permission_mode": payload.get("permission_mode", ""),
        **_optional_record_fields(
            payload,
            ("attachments", "transcript_path", "model", "cwd"),
        ),
    }
    _append_record(workspace_root, session_id, record)


def _handle_post_tool_use(payload: dict[str, Any], workspace_root: Path) -> None:
    """Handle a PostToolUse event."""
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    record: dict[str, Any] = {
        "record_type": "tool-use",
        "session_id": session_id,
        "recorded_at": _utc_now(),
        "tool_name": payload.get("tool_name", ""),
        "tool_input": payload.get("tool_input", {}),
        "tool_response": payload.get("tool_response", {}),
        "tool_use_id": payload.get("tool_use_id", ""),
    }
    _append_record(workspace_root, session_id, record)


def _handle_stop(payload: dict[str, Any], workspace_root: Path) -> None:
    """Handle a Stop event."""
    session_id = payload.get("session_id", "")
    if not session_id:
        return
    record: dict[str, Any] = {
        "record_type": "stop",
        "session_id": session_id,
        "recorded_at": _utc_now(),
        "last_assistant_message": payload.get("last_assistant_message", ""),
        "stop_hook_active": payload.get("stop_hook_active", False),
        **_optional_record_fields(
            payload,
            (
                "stop_reason",
                "stop_condition",
                "reason",
                "host_error_text",
                "error_text",
                "error",
                "hook_activity_state",
            ),
        ),
    }
    _append_record(workspace_root, session_id, record)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_HANDLER_MAP = {
    "SessionStart": _handle_session_start,
    "SessionEnd": _handle_session_end,
    "UserPromptSubmit": _handle_prompt_submit,
    "PostToolUse": _handle_post_tool_use,
    "Stop": _handle_stop,
}


def normalize_hook_context(
    *,
    host: str,
    event_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Translate host protocol fields into the shared semantic hook input."""
    normalized_host = "chatgpt-work" if host in {"codex", "chatgpt-work"} else "claude-code"
    return {
        "host": normalized_host,
        "event_name": event_name,
        "session_id": str(payload.get("session_id") or ""),
        "native_turn_id": str(payload.get("turn_id") or ""),
        "prompt": str(payload.get("prompt") or ""),
        "last_assistant_message": str(payload.get("last_assistant_message") or ""),
        "stop_hook_active": bool(payload.get("stop_hook_active", False)),
    }


def _linked_turn(
    paths: WorkspacePaths,
    context: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    session_id = str(context.get("session_id") or "")
    if not session_id:
        return None
    provider = "codex" if context.get("host") == "chatgpt-work" else "claude-code"
    identity = {
        "host_provider": provider,
        "host_thread_ref": session_id,
        "host_identity_source": (
            "codex_thread_id" if provider == "codex" else "claude_session_id"
        ),
        "host_identity_trust": "hook-payload",
    }
    conversation_id = bound_conversation_id_for_host(paths, host_identity=identity)
    if conversation_id is None:
        return None
    conversation = load_conversation_record(paths, conversation_id)
    turns = [turn for turn in conversation.get("turns", []) if isinstance(turn, dict)]
    if not turns:
        return None
    native_turn_id = str(context.get("native_turn_id") or "")
    if native_turn_id:
        matches = [
            turn
            for turn in turns
            if native_turn_id in {str(turn.get("native_turn_id") or ""), str(turn.get("turn_id"))}
        ]
        if len(matches) == 1:
            return conversation_id, matches[0]
    return conversation_id, turns[-1]


def _artifact_exists(paths: WorkspacePaths, turn: dict[str, Any]) -> bool:
    candidates = [turn.get("answer_file_path"), *turn.get("bundle_paths", [])]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        path = Path(candidate)
        if not path.is_absolute():
            path = paths.root / path
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _current_prompt_matches_turn(
    paths: WorkspacePaths,
    context: dict[str, Any],
    turn: dict[str, Any],
) -> bool:
    """Require the current host prompt to own the candidate closure turn."""
    session_id = str(context.get("session_id") or "")
    host = str(context.get("host") or "")
    if not session_id or host not in {"chatgpt-work", "claude-code"}:
        return False
    mirror = paths.interaction_ingest_dir / host / f"{session_id}.jsonl"
    try:
        lines = mirror.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    prompt = ""
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("record_type") != "prompt-submit":
            continue
        prompt = str(record.get("prompt") or "").strip()
        break
    question = str(turn.get("user_question") or "").strip()
    return bool(prompt and question and prompt == question)


def evaluate_hook_decision(
    paths: WorkspacePaths,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Return the host-neutral sensor decision without doing workflow work."""
    linked = _linked_turn(paths, context)
    linked_conversation_id = linked[0] if linked else ""
    turn = linked[1] if linked else {}
    turn_id = str(turn.get("turn_id") or "")
    if context.get("event_name") == "UserPromptSubmit":
        if not turn_id:
            return {
                "action": "allow",
                "reason": "no-continuation-state",
                "additional_context": "",
                "linked_conversation_id": linked_conversation_id,
                "linked_turn_id": "",
            }
        reminders = [
            "DocMason continuation state (advisory; ignore for unrelated requests): "
            f"the latest linked canonical turn is `{turn_id}`. Use this exact id as "
            "`revision_of` only when the user explicitly revises that work."
        ]
        gate = turn.get("decision_frontier")
        if isinstance(gate, dict) and gate.get("status") == "open":
            option_summary = "; ".join(
                f"{option.get('id')}={option.get('label')}"
                for option in gate.get("options", [])
                if isinstance(option, dict)
                and option.get("id")
                and option.get("label")
            )
            reminders.append(
                f"Open decision gate `{gate.get('gate_id', '')}`: "
                f"{gate.get('question', '')} "
                + (
                    f"Legal option ids are {option_summary}. "
                    if option_summary
                    else ""
                )
                + "If the user explicitly answers it, pass the exact gate id and either one "
                "legal option id or an explicit free-form decision; otherwise leave it open."
            )
        accepted_scopes = [
            str(scope.get("scope_id"))
            for scope in turn.get("accepted_scopes", [])
            if isinstance(scope, dict) and scope.get("status") == "accepted"
        ]
        if accepted_scopes:
            reminders.append(
                "For an explicit continuation, preserve accepted scopes unless an upstream "
                "dependency makes one at-risk: "
                + ", ".join(accepted_scopes)
            )
        return {
            "action": "allow",
            "reason": "prompt-context",
            "additional_context": " ".join(reminders),
            "linked_conversation_id": linked_conversation_id,
            "linked_turn_id": turn_id,
        }

    allow = {
        "action": "allow",
        "reason": "stop-not-eligible",
        "additional_context": "",
        "linked_conversation_id": linked_conversation_id,
        "linked_turn_id": turn_id,
    }
    if context.get("event_name") != "Stop" or context.get("stop_hook_active"):
        return allow
    if not linked or not turn_id:
        return allow
    if not _current_prompt_matches_turn(paths, context, turn):
        return allow
    if turn.get("committed_run_id") or turn.get("turn_state") != "prepared":
        return allow
    gate = turn.get("decision_frontier")
    if isinstance(gate, dict) and gate.get("status") == "open":
        return allow
    if turn.get("attached_shared_job_ids") or turn.get("turn_state") == "waiting-shared-job":
        return allow
    if turn.get("status") in {"action-required", "blocked", "boundary", "completed"}:
        return allow
    if turn.get("scope_freshness") == "unresolved-target-freshness":
        return allow
    if turn.get("source_escalation_required") or turn.get("admissibility_repair"):
        return allow
    if turn.get("hybrid_refresh_triggered") and not turn.get(
        "hybrid_refresh_completion_status"
    ):
        return allow
    if not _artifact_exists(paths, turn):
        return allow
    support_basis = str(turn.get("support_basis") or "kb-grounded")
    has_session_support = bool(turn.get("session_ids"))
    evidence_items = (
        turn.get("evidence_packet", {}).get("items", [])
        if isinstance(turn.get("evidence_packet"), dict)
        else []
    )
    has_external_support = bool(turn.get("support_manifest_path")) or any(
        isinstance(item, dict)
        and item.get("availability") == "available"
        and item.get("authority") not in {"untrusted", "reference-only"}
        for item in evidence_items
    )
    if support_basis == "mixed" and (not has_session_support or not has_external_support):
        return allow
    if support_basis == "external-source-verified" and not has_external_support:
        return allow
    if support_basis not in {
        "mixed",
        "external-source-verified",
        "model-knowledge",
    } and not has_session_support:
        return allow
    if any(
        isinstance(item, dict)
        and (
            item.get("availability") != "available"
            or item.get("freshness") in {"stale", "superseded", "unknown"}
        )
        for item in evidence_items
    ):
        return allow
    if any(
        isinstance(scope, dict) and scope.get("status") == "at-risk"
        for scope in turn.get("accepted_scopes", [])
    ):
        return allow
    if not claim_turn_closure_continuation(
        paths,
        conversation_id=linked_conversation_id,
        turn_id=turn_id,
        host=str(context.get("host") or ""),
    ):
        return allow
    return {
        **allow,
        "action": "continue-once",
        "reason": (
            "Complete only the pending DocMason trace/finalize closure for the existing "
            "answer. Do not sync, retrieve, broaden scope, or alter the business deliverable."
        ),
    }


def serialize_hook_decision(
    context: dict[str, Any],
    decision: dict[str, Any],
) -> dict[str, Any] | None:
    """Translate a semantic decision into the current host hook protocol."""
    if context.get("event_name") == "Stop":
        if decision.get("action") != "continue-once":
            return None
        return {"decision": "block", "reason": decision.get("reason", "")}
    additional_context = str(decision.get("additional_context") or "")
    if not additional_context:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }


def handle_hook_event(event_name: str, stdin_text: str) -> dict[str, Any] | None:
    """Parse the stdin JSON payload and dispatch to the correct handler.

    This is the main entry point called by the hidden ``_hook`` CLI subcommand.
    Silently returns on any parse error or missing data — hooks must never crash.
    """
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    workspace_root = _resolve_workspace_root()

    # The hook_event_name from the payload is authoritative. The CLI argument
    # is a hint for the "session" case which handles both start and end.
    hook_event_name = payload.get("hook_event_name", "")

    # Map CLI event names to hook_event_name when the payload is missing it.
    if not hook_event_name:
        cli_to_hook: dict[str, str] = {
            "session": "SessionStart",
            "prompt-submit": "UserPromptSubmit",
            "post-tool-use": "PostToolUse",
            "stop": "Stop",
        }
        hook_event_name = cli_to_hook.get(event_name, "")

    handler = _HANDLER_MAP.get(hook_event_name)
    if handler is not None:
        try:
            handler(payload, workspace_root)
        except (OSError, ValueError, TypeError):
            # Hooks must never crash. Swallow filesystem or data errors.
            pass
    if hook_event_name not in {"UserPromptSubmit", "Stop"}:
        return None
    try:
        context = normalize_hook_context(
            host=_hook_host(payload),
            event_name=hook_event_name,
            payload=payload,
        )
        decision = evaluate_hook_decision(
            WorkspacePaths(root=workspace_root),
            context,
        )
        return serialize_hook_decision(context, decision)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return None


def run_hook_cli(event_name: str) -> int:
    """CLI entry point for ``docmason _hook <event-name>``.

    Reads stdin, dispatches to the handler, and returns an exit code.
    Always returns 0 — hooks must not produce non-zero exits that could
    block Claude Code operation.
    """
    if event_name not in SUPPORTED_EVENTS:
        return 0

    stdin_text = ""
    try:
        if not sys.stdin.isatty():
            stdin_text = sys.stdin.read()
    except (OSError, ValueError):
        pass

    output = handle_hook_event(event_name, stdin_text)
    if isinstance(output, dict):
        sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    return 0

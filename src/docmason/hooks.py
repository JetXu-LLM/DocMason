"""Internal handler for Claude Code hook events.

This module processes Claude Code hook payloads received via stdin and writes
structured JSONL mirror records to the runtime interaction-ingest directory.
It is called by the committed hook shell scripts in ``.claude/hooks/`` and
exposed through a hidden ``_hook`` CLI subcommand.

Not a public command surface. This is internal plumbing.

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

    Uses ``$CLAUDE_PROJECT_DIR`` when available, otherwise falls back to CWD.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        return Path(project_dir)
    return Path.cwd()


def _mirror_root(workspace_root: Path) -> Path:
    """Return the Claude Code mirror directory."""
    return workspace_root / "runtime" / "interaction-ingest" / "claude-code"


def _mirror_path(workspace_root: Path, session_id: str) -> Path:
    """Return the JSONL mirror file path for a session."""
    return _mirror_root(workspace_root) / f"{session_id}.jsonl"


def _append_record(workspace_root: Path, session_id: str, record: dict[str, Any]) -> None:
    """Append a single JSON record to the session JSONL mirror file."""
    path = _mirror_path(workspace_root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


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


def handle_hook_event(event_name: str, stdin_text: str) -> None:
    """Parse the stdin JSON payload and dispatch to the correct handler.

    This is the main entry point called by the hidden ``_hook`` CLI subcommand.
    Silently returns on any parse error or missing data — hooks must never crash.
    """
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return

    if not isinstance(payload, dict):
        return

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

    handle_hook_event(event_name, stdin_text)
    return 0

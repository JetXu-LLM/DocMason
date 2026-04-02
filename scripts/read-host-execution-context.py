#!/usr/bin/env python3
"""Read the current host execution context for native Codex cold-start gating.

This helper intentionally uses only the Python standard library and Python 3.9
compatible syntax so the bootstrap launcher can call it before DocMason itself
is importable from a prepared repo-local runtime.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional


def _nonempty_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _normalized_sandbox_policy(raw_value: Any) -> Optional[str]:
    if isinstance(raw_value, dict):
        return _nonempty_string(raw_value.get("type")) or _nonempty_string(raw_value.get("mode"))
    text = _nonempty_string(raw_value)
    if text is None:
        return None
    if text.startswith("{"):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return text
        if isinstance(decoded, dict):
            return _normalized_sandbox_policy(decoded)
    return text


def _normalized_bool(raw_value: Any) -> Optional[bool]:
    if isinstance(raw_value, bool):
        return raw_value
    text = _nonempty_string(raw_value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"1", "true", "yes", "enabled"}:
        return True
    if lowered in {"0", "false", "no", "disabled", "restricted"}:
        return False
    return None


def _normalized_writable_roots(raw_value: Any) -> List[str]:
    if isinstance(raw_value, list):
        return [item for item in raw_value if isinstance(item, str) and item]
    text = _nonempty_string(raw_value)
    if text is None:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return [item for item in decoded if isinstance(item, str) and item]
    return [item for item in text.split(os.pathsep) if item]


def _detect_agent_surface(env: Dict[str, str]) -> str:
    explicit = _nonempty_string(env.get("DOCMASON_AGENT_SURFACE"))
    if explicit is not None:
        return explicit.lower()
    if env.get("CODEX_THREAD_ID"):
        return "codex"
    if env.get("CLAUDE_PROJECT_DIR"):
        return "claude-code"
    if env.get("CLAUDE_SESSION_ID") or env.get("CLAUDE_CONVERSATION_ID"):
        return "claude-code"
    origin = env.get("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "").lower()
    if "codex" in origin:
        return "codex"
    env_keys = " ".join(env.keys()).lower()
    env_values = " ".join(env.values()).lower()
    if "claude" in env_keys or "claude" in env_values:
        return "claude-code"
    return "unknown-agent"


def _normalized_permission_mode(
    provider: str,
    sandbox_policy: Optional[str],
    approval_mode: Optional[str],
    explicit_mode: Optional[str] = None,
) -> Optional[str]:
    if explicit_mode:
        return explicit_mode
    if provider == "codex":
        if sandbox_policy == "danger-full-access":
            return "full-access"
        if sandbox_policy == "workspace-write":
            return "default-permissions"
    if provider == "claude-code" and approval_mode == "bypassPermissions":
        return "full-access"
    return None


def _codex_state_db_path(home_dir: Path) -> Path:
    return home_dir / ".codex" / "state_5.sqlite"


def _codex_sessions_root(home_dir: Path) -> Path:
    return home_dir / ".codex" / "sessions"


def _codex_thread_metadata(thread_id: str, *, state_db_path: Path) -> Dict[str, Any]:
    if not state_db_path.exists():
        raise FileNotFoundError(state_db_path)
    with closing(sqlite3.connect(state_db_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            (
                "SELECT rollout_path, sandbox_policy, approval_mode "
                "FROM threads WHERE id = ?"
            ),
            (thread_id,),
        ).fetchone()
    if row is None:
        raise KeyError(thread_id)
    return dict(row)


def _locate_codex_rollout_path(
    thread_id: str,
    *,
    metadata: Dict[str, Any],
    sessions_root: Path,
) -> Optional[Path]:
    rollout_hint = metadata.get("rollout_path")
    if isinstance(rollout_hint, str) and rollout_hint:
        hinted = Path(rollout_hint).expanduser()
        if hinted.exists():
            return hinted
    candidates = sorted(sessions_root.glob("**/rollout-*%s.jsonl" % thread_id))
    if candidates:
        return candidates[-1]
    return None


def _latest_codex_turn_context(rollout_path: Path) -> Dict[str, Any]:
    latest: Dict[str, Any] = {}
    for line in rollout_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") != "turn_context":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict):
            latest = payload
    return latest


def _context_from_turn_context(payload: Dict[str, Any]) -> Dict[str, Any]:
    sandbox_payload = payload.get("sandbox_policy")
    return {
        "sandbox_policy": _normalized_sandbox_policy(sandbox_payload),
        "approval_mode": _nonempty_string(payload.get("approval_policy")),
        "workspace_write_network_access": (
            _normalized_bool(sandbox_payload.get("network_access"))
            if isinstance(sandbox_payload, dict)
            else None
        ),
        "sandbox_writable_roots": (
            _normalized_writable_roots(sandbox_payload.get("writable_roots"))
            if isinstance(sandbox_payload, dict)
            else []
        ),
    }


def _context_from_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    sandbox_payload = metadata.get("sandbox_policy")
    decoded_payload = None
    if isinstance(sandbox_payload, str) and sandbox_payload.startswith("{"):
        try:
            decoded_payload = json.loads(sandbox_payload)
        except json.JSONDecodeError:
            decoded_payload = None
    elif isinstance(sandbox_payload, dict):
        decoded_payload = sandbox_payload
    return {
        "sandbox_policy": _normalized_sandbox_policy(sandbox_payload),
        "approval_mode": _nonempty_string(metadata.get("approval_mode")),
        "workspace_write_network_access": (
            _normalized_bool(decoded_payload.get("network_access"))
            if isinstance(decoded_payload, dict)
            else None
        ),
        "sandbox_writable_roots": (
            _normalized_writable_roots(decoded_payload.get("writable_roots"))
            if isinstance(decoded_payload, dict)
            else []
        ),
    }


def read_host_execution_context(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    current_env = dict(os.environ if env is None else env)
    provider = _detect_agent_surface(current_env)
    explicit_permission_mode = _nonempty_string(current_env.get("DOCMASON_PERMISSION_MODE"))
    explicit_sandbox_policy = _nonempty_string(
        current_env.get("DOCMASON_SANDBOX_POLICY")
        or current_env.get("DOCMASON_CODEX_SANDBOX_POLICY")
    )
    explicit_approval_mode = _nonempty_string(
        current_env.get("DOCMASON_APPROVAL_MODE")
        or current_env.get("DOCMASON_CODEX_APPROVAL_MODE")
    )
    explicit_network_access = _normalized_bool(
        current_env.get("DOCMASON_WORKSPACE_WRITE_NETWORK_ACCESS")
        or current_env.get("DOCMASON_CODEX_NETWORK_ACCESS")
    )
    explicit_writable_roots = _normalized_writable_roots(
        current_env.get("DOCMASON_SANDBOX_WRITABLE_ROOTS")
        or current_env.get("DOCMASON_CODEX_WRITABLE_ROOTS")
    )

    metadata: Dict[str, Any] = {}
    rollout_context: Dict[str, Any] = {}
    context_source = "env-override" if (
        explicit_permission_mode is not None
        or explicit_sandbox_policy is not None
        or explicit_approval_mode is not None
        or explicit_network_access is not None
        or explicit_writable_roots
    ) else "unknown"

    thread_id = _nonempty_string(current_env.get("CODEX_THREAD_ID"))
    if provider == "codex" and thread_id and context_source == "unknown":
        home_dir = Path.home()
        state_db_path = _codex_state_db_path(home_dir)
        try:
            metadata = _codex_thread_metadata(thread_id, state_db_path=state_db_path)
        except (FileNotFoundError, KeyError, OSError, sqlite3.DatabaseError):
            metadata = {}

        rollout_path = _locate_codex_rollout_path(
            thread_id,
            metadata=metadata,
            sessions_root=_codex_sessions_root(home_dir),
        )
        if rollout_path is not None:
            try:
                rollout_context = _latest_codex_turn_context(rollout_path)
            except OSError:
                rollout_context = {}
        if rollout_context:
            context_source = "codex-turn-context"
        elif metadata:
            context_source = "codex-thread-metadata"

    turn_context = _context_from_turn_context(rollout_context) if rollout_context else {}
    metadata_context = _context_from_metadata(metadata) if metadata else {}

    sandbox_policy = _normalized_sandbox_policy(
        explicit_sandbox_policy
        or turn_context.get("sandbox_policy")
        or metadata_context.get("sandbox_policy")
    )
    approval_mode = (
        explicit_approval_mode
        or _nonempty_string(turn_context.get("approval_mode"))
        or _nonempty_string(metadata_context.get("approval_mode"))
    )
    workspace_write_network_access = (
        explicit_network_access
        if explicit_network_access is not None
        else turn_context.get("workspace_write_network_access")
        if turn_context.get("workspace_write_network_access") is not None
        else metadata_context.get("workspace_write_network_access")
    )
    sandbox_writable_roots = (
        explicit_writable_roots
        or turn_context.get("sandbox_writable_roots")
        or metadata_context.get("sandbox_writable_roots")
        or []
    )
    permission_mode = _normalized_permission_mode(
        provider,
        sandbox_policy,
        approval_mode,
        explicit_mode=explicit_permission_mode,
    )

    return {
        "host_provider": provider,
        "sandbox_policy": sandbox_policy,
        "approval_mode": approval_mode,
        "permission_mode": permission_mode,
        "full_machine_access": permission_mode == "full-access",
        "workspace_write_network_access": workspace_write_network_access,
        "sandbox_writable_roots": sandbox_writable_roots,
        "context_source": context_source,
    }


def _emit_shell(payload: Dict[str, Any]) -> int:
    for key in sorted(payload):
        value = payload[key]
        variable_name = "DOCMASON_HOST_" + key.upper()
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif value is None:
            rendered = ""
        elif isinstance(value, (list, dict)):
            rendered = json.dumps(value, sort_keys=True)
        else:
            rendered = str(value)
        sys.stdout.write("%s=%s\n" % (variable_name, shlex.quote(rendered)))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--format",
        choices=("json", "shell"),
        default="json",
        help="Output format.",
    )
    args = parser.parse_args(argv)
    payload = read_host_execution_context()
    if args.format == "shell":
        return _emit_shell(payload)
    json.dump(payload, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

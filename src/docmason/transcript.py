"""Normalized native transcript schemas and provider-agnostic history readers.

Supports both Codex rollout transcripts and Claude Code hook-mirror sessions.
"""

from __future__ import annotations

import json
import sqlite3
from base64 import b64decode
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _iso_timestamp(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _strip_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def is_agent_contract_message(text: str) -> bool:
    """Return whether a native user message is the injected repository contract."""
    normalized = text.lstrip()
    return normalized.startswith("# AGENTS.md instructions for ")


def _message_text_chunks(content: Any) -> list[str]:
    chunks: list[str] = []
    if not isinstance(content, list):
        return chunks
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text"} and isinstance(item.get("text"), str):
            text = item["text"].strip()
            if text:
                chunks.append(text)
    return chunks


def _message_attachments(content: Any) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return attachments
    for index, item in enumerate(content, start=1):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "input_image":
            continue
        image_url = item.get("image_url")
        if not isinstance(image_url, str) or not image_url:
            continue
        attachments.append(
            {
                "attachment_id": f"attachment-{index:03d}",
                "attachment_type": "image",
                "image_url": image_url,
            }
        )
    return attachments


def decode_data_url(data_url: str) -> tuple[str, bytes]:
    """Decode a data URL into a mime type and raw bytes."""
    if not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("Only data URLs are supported.")
    header, payload = data_url.split(",", 1)
    if ";base64" not in header:
        raise ValueError("Only base64 data URLs are supported.")
    mime_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    return mime_type, b64decode(payload)


@dataclass(frozen=True)
class CodexThreadLocation:
    """Resolved local storage paths for one Codex native thread."""

    thread_id: str
    state_db_path: Path
    rollout_path: Path


def codex_state_db_path() -> Path:
    """Return the default local Codex SQLite path."""
    return Path.home() / ".codex" / "state_5.sqlite"


def codex_sessions_root() -> Path:
    """Return the default local Codex sessions directory."""
    return Path.home() / ".codex" / "sessions"


def codex_thread_metadata(thread_id: str, *, state_db_path: Path | None = None) -> dict[str, Any]:
    """Load basic metadata for one native Codex thread from SQLite."""
    database_path = state_db_path or codex_state_db_path()
    if not database_path.exists():
        raise FileNotFoundError(database_path)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            (
                "SELECT id, rollout_path, created_at, updated_at, source, model_provider, cwd, "
                "title, sandbox_policy, approval_mode, tokens_used, cli_version, "
                "first_user_message, agent_nickname, agent_role, memory_mode "
                "FROM threads WHERE id = ?"
            ),
            (thread_id,),
        ).fetchone()
    if row is None:
        raise KeyError(thread_id)
    return dict(row)


def locate_codex_thread(thread_id: str) -> CodexThreadLocation:
    """Resolve the rollout JSONL path for one Codex thread."""
    state_db_path = codex_state_db_path()
    metadata = codex_thread_metadata(thread_id, state_db_path=state_db_path)
    rollout_hint = metadata.get("rollout_path")
    candidates: list[Path] = []
    if isinstance(rollout_hint, str) and rollout_hint:
        hinted = Path(rollout_hint).expanduser()
        if hinted.exists():
            candidates.append(hinted)
    candidates.extend(sorted(codex_sessions_root().glob(f"**/rollout-*{thread_id}.jsonl")))
    if not candidates:
        raise FileNotFoundError(f"Could not locate a rollout JSONL for Codex thread `{thread_id}`.")
    return CodexThreadLocation(
        thread_id=thread_id,
        state_db_path=state_db_path,
        rollout_path=sorted(dict.fromkeys(candidates))[-1],
    )


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of JSON objects."""
    payloads: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if isinstance(record, dict):
            payloads.append(record)
    return payloads


def _normalized_function_call(
    payload: dict[str, Any], *, recorded_at: str | None
) -> dict[str, Any]:
    arguments = payload.get("arguments")
    parsed_arguments: dict[str, Any] | None = None
    if isinstance(arguments, str) and arguments.strip():
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            parsed_arguments = decoded
    return {
        "recorded_at": recorded_at,
        "tool_name": payload.get("name"),
        "call_id": payload.get("call_id"),
        "arguments": parsed_arguments,
        "arguments_text": arguments if isinstance(arguments, str) else None,
    }


def load_codex_transcript(thread_id: str) -> dict[str, Any]:
    """Load one native Codex thread into a normalized transcript structure."""
    location = locate_codex_thread(thread_id)
    metadata = codex_thread_metadata(thread_id, state_db_path=location.state_db_path)
    records = iter_jsonl(location.rollout_path)

    turns: list[dict[str, Any]] = []
    current_turn: dict[str, Any] | None = None
    for record in records:
        recorded_at = _iso_timestamp(record.get("timestamp"))
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record_type == "event_msg" and payload.get("type") == "task_started":
            current_turn = {
                "native_turn_id": payload.get("turn_id"),
                "opened_at": recorded_at,
                "completed_at": None,
                "user_messages": [],
                "assistant_messages": [],
                "attachments": [],
                "function_calls": [],
                "function_call_outputs": [],
            }
            turns.append(current_turn)
            continue
        if current_turn is None:
            continue
        if record_type == "event_msg" and payload.get("type") == "task_complete":
            current_turn["completed_at"] = recorded_at
            continue
        if record_type != "response_item":
            continue
        payload_type = payload.get("type")
        if payload_type == "message":
            role = payload.get("role")
            text_chunks = _message_text_chunks(payload.get("content"))
            text = "\n\n".join(text_chunks).strip()
            if role == "user":
                if text and not is_agent_contract_message(text):
                    current_turn["user_messages"].append({"recorded_at": recorded_at, "text": text})
                current_turn["attachments"].extend(_message_attachments(payload.get("content")))
            elif role == "assistant" and text:
                current_turn["assistant_messages"].append(
                    {"recorded_at": recorded_at, "text": text}
                )
            continue
        if payload_type == "function_call":
            current_turn["function_calls"].append(
                _normalized_function_call(payload, recorded_at=recorded_at)
            )
            continue
        if payload_type == "function_call_output":
            current_turn["function_call_outputs"].append(
                {
                    "recorded_at": recorded_at,
                    "call_id": payload.get("call_id"),
                    "output": payload.get("output"),
                }
            )

    normalized_turns: list[dict[str, Any]] = []
    for turn in turns:
        user_text = "\n\n".join(
            item["text"]
            for item in turn.get("user_messages", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ).strip()
        assistant_text = "\n\n".join(
            item["text"]
            for item in turn.get("assistant_messages", [])
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ).strip()
        if not user_text and not turn.get("attachments"):
            continue
        normalized_turns.append(
            {
                "native_turn_id": turn.get("native_turn_id"),
                "opened_at": turn.get("opened_at"),
                "completed_at": turn.get("completed_at"),
                "user_text": user_text,
                "assistant_text": assistant_text,
                "assistant_message_count": len(turn.get("assistant_messages", [])),
                "assistant_final_text": (
                    str(turn["assistant_messages"][-1]["text"]).strip()
                    if turn.get("assistant_messages")
                    and isinstance(turn["assistant_messages"][-1], dict)
                    and isinstance(turn["assistant_messages"][-1].get("text"), str)
                    else assistant_text
                ),
                "attachments": turn.get("attachments", []),
                "function_calls": turn.get("function_calls", []),
                "function_call_outputs": turn.get("function_call_outputs", []),
            }
        )

    transcript = {
        "provider": "codex",
        "native_thread_id": thread_id,
        "title": metadata.get("title"),
        "cwd": metadata.get("cwd"),
        "source": metadata.get("source"),
        "model_provider": metadata.get("model_provider"),
        "cli_version": metadata.get("cli_version"),
        "created_at": metadata.get("created_at"),
        "updated_at": metadata.get("updated_at"),
        "rollout_path": str(location.rollout_path),
        "fidelity": {
            "has_full_tool_calls": True,
            "has_mid_turn_messages": True,
            "capability_scope": "captured-transcript",
            "attachments_captured": True,
            "capture_method": "codex-rollout",
            "fidelity_notes": (
                "This fidelity block describes what the current DocMason Codex "
                "transcript loader captured from local rollout storage, not the "
                "full host product capability envelope."
            ),
        },
        "turns": normalized_turns,
    }
    validate_normalized_transcript(transcript)
    return transcript


SUPPORTED_PROVIDERS = frozenset({"codex", "claude-code"})


def validate_normalized_transcript(payload: dict[str, Any]) -> None:
    """Validate the minimal normalized transcript contract.

    Accepts any provider listed in ``SUPPORTED_PROVIDERS``.
    """
    provider = payload.get("provider")
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(
            f"Normalized transcript provider must be one of {sorted(SUPPORTED_PROVIDERS)}, "
            f"got {provider!r}."
        )
    if not isinstance(payload.get("native_thread_id"), str) or not payload["native_thread_id"]:
        raise ValueError("Normalized transcript must include a native_thread_id.")
    turns = payload.get("turns")
    if not isinstance(turns, list):
        raise ValueError("Normalized transcript turns must be a list.")
    for turn in turns:
        if not isinstance(turn, dict):
            raise ValueError("Normalized transcript turns must be objects.")
        if not isinstance(turn.get("user_text"), str):
            raise ValueError("Normalized transcript turns must include user_text strings.")
        attachments = turn.get("attachments", [])
        if not isinstance(attachments, list):
            raise ValueError("Normalized transcript turn attachments must be a list.")


# ---------------------------------------------------------------------------
# Claude Code hook-mirror transcript reader
# ---------------------------------------------------------------------------

def claude_code_mirror_root(workspace_root: Path) -> Path:
    """Return the Claude Code hook-mirror directory."""
    return workspace_root / "runtime" / "interaction-ingest" / "claude-code"


def locate_claude_code_session(session_id: str, workspace_root: Path) -> Path | None:
    """Find the JSONL mirror file for a Claude Code session.

    Returns None when the file does not exist.
    """
    path = claude_code_mirror_root(workspace_root) / f"{session_id}.jsonl"
    if path.exists():
        return path
    return None


def load_claude_code_native_transcript(transcript_path: str | Path) -> dict[str, Any] | None:
    """Best-effort reader for the Claude Code native transcript JSONL.

    When the native transcript at *transcript_path* is readable, returns a
    mapping of enrichment data that can be merged into the hook-mirror
    transcript.  Returns ``None`` when the file is missing, unreadable, or
    in an unexpected format.  Never raises.
    """
    try:
        path = Path(transcript_path)
        if not path.exists():
            return None
        records = iter_jsonl(path)
        if not records:
            return None
        # Extract mid-turn assistant messages and richer tool context.
        assistant_messages: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        for record in records:
            record_type = record.get("type", "")
            # Claude Code native JSONL uses "assistant" type for model replies
            if record_type == "assistant" and isinstance(record.get("message"), dict):
                msg = record["message"]
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "").strip()
                            if text:
                                assistant_messages.append({
                                    "text": text,
                                    "recorded_at": record.get("timestamp"),
                                })
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_calls.append({
                                "tool_name": block.get("name", ""),
                                "tool_use_id": block.get("id", ""),
                                "tool_input": block.get("input", {}),
                            })
            # Claude Code native JSONL uses "tool_result" for tool outputs
            if record_type == "tool_result":
                tool_calls.append({
                    "tool_name": record.get("tool_name", ""),
                    "tool_use_id": record.get("tool_use_id", ""),
                    "tool_response": record.get("content", ""),
                })
        if not assistant_messages and not tool_calls:
            return None
        return {
            "assistant_messages": assistant_messages,
            "tool_calls": tool_calls,
        }
    except (OSError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        return None


def load_claude_code_transcript(
    session_id: str,
    workspace_root: Path,
) -> dict[str, Any]:
    """Load a Claude Code session from the hook-written mirror JSONL.

    When a native transcript path is recorded in the session-start event
    and the native file is readable, enriches the transcript with mid-turn
    messages and richer tool context.

    Returns the same normalized transcript schema as
    :func:`load_codex_transcript`.

    Raises ``FileNotFoundError`` when the mirror file does not exist.
    """
    mirror_path = locate_claude_code_session(session_id, workspace_root)
    if mirror_path is None:
        raise FileNotFoundError(
            f"No Claude Code mirror file for session {session_id!r} "
            f"under {claude_code_mirror_root(workspace_root)}."
        )
    records = iter_jsonl(mirror_path)

    # Phase 1: extract session metadata and native transcript path.
    cwd: str = ""
    transcript_path_str: str = ""
    model: str = ""
    for record in records:
        if record.get("record_type") == "session-start":
            cwd = record.get("cwd", "")
            transcript_path_str = record.get("transcript_path", "")
            model = record.get("model", "")
            break

    # Phase 2: reconstruct turns by pairing prompt-submit → stop records.
    turns: list[dict[str, Any]] = []
    current_turn: dict[str, Any] | None = None
    turn_ordinal = 0
    for record in records:
        record_type = record.get("record_type", "")
        if record_type == "prompt-submit":
            turn_ordinal += 1
            current_turn = {
                "native_turn_id": f"turn-{turn_ordinal:03d}",
                "opened_at": record.get("recorded_at"),
                "completed_at": None,
                "user_text": record.get("prompt", ""),
                "assistant_text": None,
                "assistant_message_count": 0,
                "assistant_final_text": "",
                "attachments": [],
                "function_calls": [],
                "function_call_outputs": [],
            }
            turns.append(current_turn)
        elif record_type == "tool-use" and current_turn is not None:
            current_turn["function_calls"].append({
                "recorded_at": record.get("recorded_at"),
                "tool_name": record.get("tool_name", ""),
                "call_id": record.get("tool_use_id", ""),
                "arguments": record.get("tool_input"),
                "arguments_text": None,
            })
            response_text = record.get("tool_response")
            if response_text:
                current_turn["function_call_outputs"].append({
                    "recorded_at": record.get("recorded_at"),
                    "call_id": record.get("tool_use_id", ""),
                    "output": response_text,
                })
        elif record_type == "stop" and current_turn is not None:
            final_text = record.get("last_assistant_message", "")
            current_turn["completed_at"] = record.get("recorded_at")
            current_turn["assistant_text"] = final_text
            current_turn["assistant_final_text"] = final_text
            current_turn["assistant_message_count"] = 1 if final_text else 0
            current_turn = None

    # Phase 3: attempt native transcript enrichment.
    has_mid_turn = False
    capture_method = "hook-mirror"
    if transcript_path_str:
        enrichment = load_claude_code_native_transcript(transcript_path_str)
        if enrichment is not None:
            has_mid_turn = True
            capture_method = "hook-mirror-plus-native"

    transcript = {
        "provider": "claude-code",
        "native_thread_id": session_id,
        "title": None,
        "cwd": cwd,
        "model": model,
        "fidelity": {
            "has_full_tool_calls": True,
            "has_mid_turn_messages": has_mid_turn,
            "capability_scope": "captured-transcript",
            "attachments_captured": False,
            "capture_method": capture_method,
            "fidelity_notes": (
                "This fidelity block describes what the current DocMason Claude "
                "Code transcript loader reconstructed from the hook mirror and "
                "optional native transcript enrichment. It does not claim that "
                "Claude Code itself lacks attachment or multimodal features."
            ),
        },
        "turns": turns,
    }
    validate_normalized_transcript(transcript)
    return transcript

"""Normalized native transcript schemas and provider-agnostic history readers.

Supports both Codex rollout transcripts and Claude Code hook-mirror sessions.
"""

from __future__ import annotations

import json
import sqlite3
from base64 import b64decode
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
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
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
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


def _claude_text_chunks(content: Any) -> list[str]:
    chunks: list[str] = []
    if not isinstance(content, list):
        return chunks
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = _strip_text(item.get("text"))
        if text:
            chunks.append(text)
    return chunks


def _claude_tool_use_blocks(content: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return blocks
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        blocks.append(item)
    return blocks


def _claude_tool_result_blocks(content: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return blocks
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_result":
            continue
        blocks.append(item)
    return blocks


def _parse_iso_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _claude_attachment_image_url(item: dict[str, Any]) -> str | None:
    image_url = item.get("image_url")
    if isinstance(image_url, str) and image_url:
        return image_url
    source = item.get("source")
    if not isinstance(source, dict):
        return None
    source_type = source.get("type")
    if source_type == "base64":
        payload = source.get("data")
        if not isinstance(payload, str) or not payload:
            return None
        media_type = (
            source.get("media_type")
            if isinstance(source.get("media_type"), str) and source.get("media_type")
            else (
                item.get("media_type")
                if isinstance(item.get("media_type"), str) and item.get("media_type")
                else "application/octet-stream"
            )
        )
        return f"data:{media_type};base64,{payload}"
    if source_type == "url":
        url = source.get("url")
        if isinstance(url, str) and url.startswith("data:"):
            return url
    return None


def _claude_message_attachments(content: Any) -> list[dict[str, Any]]:
    attachments: list[dict[str, Any]] = []
    attachment_ordinal = 0

    def visit(value: Any) -> None:
        nonlocal attachment_ordinal
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        item_type = value.get("type")
        if item_type in {"input_image", "image"}:
            image_url = _claude_attachment_image_url(value)
            if image_url:
                attachment_ordinal += 1
                attachments.append(
                    {
                        "attachment_id": f"attachment-{attachment_ordinal:03d}",
                        "attachment_type": "image",
                        "image_url": image_url,
                    }
                )
        if item_type == "tool_result":
            visit(value.get("content"))

    visit(content)
    return attachments


def _claude_is_meta_user_record(
    record: dict[str, Any],
    *,
    user_text: str,
    tool_results: list[dict[str, Any]],
) -> bool:
    if record.get("isMeta") is True:
        return True
    if isinstance(record.get("sourceToolUseID"), str) and record.get("sourceToolUseID"):
        return True
    if tool_results:
        return False
    return user_text.startswith("Base directory for this skill:")


def _merge_attachments(
    primary_attachments: list[dict[str, Any]],
    enriched_attachments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for attachment in [*primary_attachments, *enriched_attachments]:
        if not isinstance(attachment, dict):
            continue
        attachment_id = attachment.get("attachment_id")
        key = (
            attachment_id
            if isinstance(attachment_id, str) and attachment_id
            else json.dumps(attachment, sort_keys=True, ensure_ascii=False)
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(attachment)
    return merged


def _claude_window_end(
    mirror_turns: list[dict[str, Any]],
    index: int,
    session_end_record: dict[str, Any] | None,
) -> datetime | None:
    if index + 1 < len(mirror_turns):
        return _parse_iso_timestamp(mirror_turns[index + 1].get("opened_at"))
    if isinstance(session_end_record, dict):
        return _parse_iso_timestamp(session_end_record.get("recorded_at"))
    return None


def _normalized_matching_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).strip().lower()


def _match_native_turns_to_mirror_turns(
    mirror_turns: list[dict[str, Any]],
    native_turns: list[dict[str, Any]],
    *,
    session_end_record: dict[str, Any] | None,
) -> dict[int, dict[str, Any]]:
    matched: dict[int, dict[str, Any]] = {}
    used_native_indexes: set[int] = set()
    for mirror_index, mirror_turn in enumerate(mirror_turns):
        mirror_text = _normalized_matching_text(mirror_turn.get("user_text"))
        mirror_opened_at = _parse_iso_timestamp(mirror_turn.get("opened_at"))
        mirror_window_end = _claude_window_end(mirror_turns, mirror_index, session_end_record)
        best_index: int | None = None
        best_score = -1
        for native_index, native_turn in enumerate(native_turns):
            if native_index in used_native_indexes:
                continue
            score = 0
            native_text = _normalized_matching_text(native_turn.get("user_text"))
            if mirror_text and native_text:
                if native_text == mirror_text:
                    score += 100
                elif native_text in mirror_text or mirror_text in native_text:
                    score += 60
            native_opened_at = _parse_iso_timestamp(native_turn.get("opened_at"))
            if (
                native_opened_at is not None
                and mirror_opened_at is not None
                and native_opened_at >= mirror_opened_at
            ):
                score += 20
            if (
                native_opened_at is not None
                and mirror_window_end is not None
                and native_opened_at < mirror_window_end
            ):
                score += 20
            if native_index == mirror_index:
                score += 5
            if score > best_score:
                best_score = score
                best_index = native_index
        if best_index is None or best_score <= 0:
            continue
        used_native_indexes.add(best_index)
        matched[mirror_index] = native_turns[best_index]
    return matched


def _claude_optional_fields(payload: dict[str, Any], field_names: tuple[str, ...]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for field_name in field_names:
        if field_name not in payload:
            continue
        value = payload.get(field_name)
        if value in ("", None) or value == [] or value == {}:
            continue
        record[field_name] = value
    return record


def _claude_normalized_function_call(
    *,
    recorded_at: Any,
    tool_name: Any,
    call_id: Any,
    tool_input: Any,
) -> dict[str, Any]:
    return {
        "recorded_at": _iso_timestamp(recorded_at),
        "tool_name": tool_name if isinstance(tool_name, str) else None,
        "call_id": call_id if isinstance(call_id, str) else None,
        "arguments": tool_input if isinstance(tool_input, dict) else None,
        "arguments_text": None,
    }


def _new_claude_turn(native_turn_id: str, *, opened_at: Any, user_text: str) -> dict[str, Any]:
    return {
        "native_turn_id": native_turn_id,
        "opened_at": _iso_timestamp(opened_at),
        "completed_at": None,
        "user_text": user_text,
        "assistant_text": "",
        "assistant_messages": [],
        "assistant_message_count": 0,
        "assistant_final_text": "",
        "attachments": [],
        "function_calls": [],
        "function_call_outputs": [],
        "closure": {
            "status": "open",
            "source": None,
            "stop_reason": None,
            "session_end_reason": None,
            "diagnostics": {},
        },
        "operator_evidence": {
            "status": "captured",
            "classification": None,
            "detail": None,
        },
    }


def _finalize_claude_turn(turn: dict[str, Any]) -> dict[str, Any]:
    assistant_messages = [
        item
        for item in turn.get("assistant_messages", [])
        if isinstance(item, dict) and isinstance(item.get("text"), str) and item.get("text")
    ]
    assistant_text = "\n\n".join(item["text"] for item in assistant_messages).strip()
    if assistant_text:
        turn["assistant_text"] = assistant_text
        turn["assistant_message_count"] = len(assistant_messages)
        turn["assistant_final_text"] = assistant_messages[-1]["text"]
    elif not isinstance(turn.get("assistant_text"), str):
        turn["assistant_text"] = ""
    if not isinstance(turn.get("assistant_final_text"), str):
        turn["assistant_final_text"] = ""
    return turn


def _merge_function_calls(
    primary_calls: list[dict[str, Any]],
    enriched_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    by_call_id: dict[str, dict[str, Any]] = {}
    anonymous_index = 0
    for call in [*primary_calls, *enriched_calls]:
        if not isinstance(call, dict):
            continue
        normalized = {
            "recorded_at": call.get("recorded_at"),
            "tool_name": call.get("tool_name"),
            "call_id": call.get("call_id"),
            "arguments": call.get("arguments"),
            "arguments_text": call.get("arguments_text"),
        }
        call_id = normalized.get("call_id")
        if isinstance(call_id, str) and call_id:
            existing = by_call_id.get(call_id)
            if existing is None:
                by_call_id[call_id] = normalized
                merged.append(normalized)
                continue
            for field_name in ("recorded_at", "tool_name", "arguments", "arguments_text"):
                if not existing.get(field_name) and normalized.get(field_name):
                    existing[field_name] = normalized[field_name]
            continue
        anonymous_index += 1
        normalized["call_id"] = normalized.get("call_id") or f"anonymous-call-{anonymous_index:03d}"
        merged.append(normalized)
    return merged


def _merge_function_outputs(
    primary_outputs: list[dict[str, Any]],
    enriched_outputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for output in [*primary_outputs, *enriched_outputs]:
        if not isinstance(output, dict):
            continue
        call_id = output.get("call_id")
        rendered = json.dumps(output, sort_keys=True, ensure_ascii=False)
        key = f"{call_id}:{rendered}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(output)
    return merged


def _classify_claude_operator_evidence(
    *,
    closure: dict[str, Any],
    assistant_final_text: str,
) -> dict[str, Any]:
    host_signal_text = " ".join(
        str(value)
        for value in [
            closure.get("stop_reason"),
            closure.get("session_end_reason"),
            *(
                value
                for value in closure.get("diagnostics", {}).values()
                if isinstance(value, (str, int, float))
            ),
        ]
        if isinstance(value, (str, int, float)) and str(value).strip()
    ).lower()
    assistant_signal_text = assistant_final_text.strip().lower()
    host_failure_markers = (
        "cannot read properties of undefined",
        "error during execution",
        "sdk-error",
        "runtime failure",
        "runtime error",
    )
    host_overload_markers = (
        "context budget exceeded",
        "exceeded context budget",
        "context window exceeded",
        "context overload",
        "multimodal overload",
        "exhausted context budget",
        "context exhausted",
    )
    assistant_failure_fallback_markers = (
        "cannot read properties of undefined",
        "error during execution",
    )
    assistant_overload_fallback_markers = (
        "context budget exceeded",
        "exceeded context budget",
        "context window exceeded",
        "context overload",
        "multimodal overload",
        "exhausted context budget",
        "context exhausted",
    )
    if (
        any(marker in host_signal_text for marker in host_failure_markers)
        or any(
            marker in assistant_signal_text
            for marker in assistant_failure_fallback_markers
        )
    ):
        return {
            "status": "degraded",
            "classification": "host-runtime-failure",
            "detail": "Claude session captured an explicit host/runtime failure signal.",
        }
    if (
        any(marker in host_signal_text for marker in host_overload_markers)
        or any(
            marker in assistant_signal_text
            for marker in assistant_overload_fallback_markers
        )
    ):
        return {
            "status": "degraded",
            "classification": "host-runtime-overload",
            "detail": "Claude session captured an explicit multimodal or context-overload signal.",
        }
    if closure.get("status") != "completed":
        return {
            "status": "degraded",
            "classification": "incomplete-session",
            "detail": "Claude session ended without a clean stop or native final assistant closure.",
        }
    return {
        "status": "captured",
        "classification": None,
        "detail": None,
    }


def load_claude_code_native_transcript(transcript_path: str | Path) -> dict[str, Any] | None:
    """Best-effort reader for the Claude Code native transcript JSONL.

    When the native transcript at *transcript_path* is readable, returns
    per-turn assistant and tool context that can enrich the hook-mirror
    transcript honestly. Returns ``None`` when the file is missing,
    unreadable, or in an unexpected format. Never raises.
    """
    try:
        path = Path(transcript_path)
        if not path.exists():
            return None
        records = iter_jsonl(path)
        if not records:
            return None
        turns: list[dict[str, Any]] = []
        current_turn: dict[str, Any] | None = None
        turn_ordinal = 0
        for record in records:
            record_type = record.get("type")
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if record_type == "user" and isinstance(message, dict):
                user_text = "\n\n".join(_claude_text_chunks(content)).strip()
                attachments = _claude_message_attachments(content)
                tool_results = _claude_tool_result_blocks(content)
                is_meta_user_record = _claude_is_meta_user_record(
                    record,
                    user_text=user_text,
                    tool_results=tool_results,
                )
                if (user_text or attachments) and not tool_results and not is_meta_user_record:
                    if current_turn is not None:
                        turns.append(_finalize_claude_turn(current_turn))
                    turn_ordinal += 1
                    current_turn = _new_claude_turn(
                        f"turn-{turn_ordinal:03d}",
                        opened_at=record.get("timestamp"),
                        user_text=user_text,
                    )
                    current_turn["attachments"] = attachments
                    continue
                if current_turn is None:
                    continue
                if is_meta_user_record:
                    current_turn.setdefault("system_events", []).append(
                        {
                            "recorded_at": _iso_timestamp(record.get("timestamp")),
                            "kind": "meta-user-message",
                            "text": user_text or None,
                            **_claude_optional_fields(record, ("sourceToolUseID", "toolUseResult")),
                        }
                    )
                if attachments:
                    current_turn["attachments"] = _merge_attachments(
                        current_turn.get("attachments", []),
                        attachments,
                    )
                for block in tool_results:
                    tool_result_content = block.get("content")
                    current_turn["function_call_outputs"].append(
                        {
                            "recorded_at": _iso_timestamp(record.get("timestamp")),
                            "call_id": block.get("tool_use_id"),
                            "output": tool_result_content,
                        }
                    )
                continue
            if record_type == "assistant" and isinstance(message, dict) and current_turn is not None:
                content = message.get("content")
                for text in _claude_text_chunks(content):
                    current_turn["assistant_messages"].append(
                        {
                            "recorded_at": _iso_timestamp(record.get("timestamp")),
                            "text": text,
                        }
                    )
                for block in _claude_tool_use_blocks(content):
                    current_turn["function_calls"].append(
                        _claude_normalized_function_call(
                            recorded_at=record.get("timestamp"),
                            tool_name=block.get("name"),
                            call_id=block.get("id"),
                            tool_input=block.get("input"),
                        )
                    )
                stop_reason = message.get("stop_reason")
                if (
                    stop_reason in {"end_turn", "stop_sequence"}
                    and isinstance(record.get("timestamp"), str)
                ):
                    current_turn["completed_at"] = record["timestamp"]
            if record_type == "system" and current_turn is not None:
                current_turn.setdefault("system_events", []).append(
                    {
                        "recorded_at": _iso_timestamp(record.get("timestamp")),
                        **_claude_optional_fields(record, ("subtype", "content", "message")),
                    }
                )
        if current_turn is not None:
            turns.append(_finalize_claude_turn(current_turn))
        if not turns:
            return None
        return {
            "turns": turns,
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
    session_end_record: dict[str, Any] | None = None
    for record in records:
        if record.get("record_type") == "session-start":
            cwd = record.get("cwd", "")
            transcript_path_str = record.get("transcript_path", "")
            model = record.get("model", "")
        elif record.get("record_type") == "session-end":
            session_end_record = record

    # Phase 2: reconstruct turns by pairing prompt-submit → stop records.
    turns: list[dict[str, Any]] = []
    current_turn: dict[str, Any] | None = None
    turn_ordinal = 0
    for record in records:
        record_type = record.get("record_type", "")
        if record_type == "prompt-submit":
            if current_turn is not None:
                current_turn["closure"] = {
                    "status": "incomplete",
                    "source": "hook-mirror",
                    "stop_reason": None,
                    "session_end_reason": None,
                    "diagnostics": {"detail": "A new prompt started before the previous turn closed."},
                }
                current_turn["operator_evidence"] = _classify_claude_operator_evidence(
                    closure=current_turn["closure"],
                    assistant_final_text=str(current_turn.get("assistant_final_text") or ""),
                )
            turn_ordinal += 1
            current_turn = _new_claude_turn(
                f"turn-{turn_ordinal:03d}",
                opened_at=record.get("recorded_at"),
                user_text=str(record.get("prompt", "")),
            )
            turns.append(current_turn)
        elif record_type == "tool-use" and current_turn is not None:
            current_turn["function_calls"].append(
                _claude_normalized_function_call(
                    recorded_at=record.get("recorded_at"),
                    tool_name=record.get("tool_name"),
                    call_id=record.get("tool_use_id"),
                    tool_input=record.get("tool_input"),
                )
            )
            response_text = record.get("tool_response")
            if response_text:
                current_turn["function_call_outputs"].append(
                    {
                        "recorded_at": record.get("recorded_at"),
                        "call_id": record.get("tool_use_id", ""),
                        "output": response_text,
                    }
                )
        elif record_type == "stop" and current_turn is not None:
            final_text = str(record.get("last_assistant_message", ""))
            current_turn["completed_at"] = record.get("recorded_at")
            current_turn["assistant_text"] = final_text
            current_turn["assistant_final_text"] = final_text
            current_turn["assistant_messages"] = (
                [
                    {
                        "recorded_at": _iso_timestamp(record.get("recorded_at")),
                        "text": final_text,
                    }
                ]
                if final_text
                else []
            )
            current_turn["assistant_message_count"] = len(current_turn["assistant_messages"])
            current_turn["closure"] = {
                "status": "completed",
                "source": "hook-stop",
                "stop_reason": record.get("stop_reason") or record.get("reason"),
                "session_end_reason": None,
                "diagnostics": _claude_optional_fields(
                    record,
                    (
                        "stop_condition",
                        "reason",
                        "host_error_text",
                        "error_text",
                        "error",
                        "hook_activity_state",
                    ),
                ),
            }
            current_turn["operator_evidence"] = _classify_claude_operator_evidence(
                closure=current_turn["closure"],
                assistant_final_text=final_text,
            )
            current_turn = None
    if current_turn is not None:
        current_turn["closure"] = {
            "status": "incomplete",
            "source": "session-end" if session_end_record else "hook-mirror",
            "stop_reason": None,
            "session_end_reason": (
                session_end_record.get("session_end_reason") or session_end_record.get("reason")
                if isinstance(session_end_record, dict)
                else None
            ),
            "diagnostics": _claude_optional_fields(
                session_end_record or {},
                (
                    "host_error_text",
                    "error_text",
                    "error",
                    "hook_activity_state",
                    "stop_condition",
                ),
            ),
        }
        current_turn["operator_evidence"] = _classify_claude_operator_evidence(
            closure=current_turn["closure"],
            assistant_final_text=str(current_turn.get("assistant_final_text") or ""),
        )

    # Phase 3: attempt native transcript enrichment.
    has_mid_turn = False
    capture_method = "hook-mirror"
    attachments_captured = any(
        isinstance(turn.get("attachments"), list) and turn.get("attachments")
        for turn in turns
        if isinstance(turn, dict)
    )
    if transcript_path_str:
        enrichment = load_claude_code_native_transcript(transcript_path_str)
        if enrichment is not None and isinstance(enrichment.get("turns"), list):
            capture_method = "hook-mirror-plus-native"
            native_turns = [
                turn for turn in enrichment.get("turns", []) if isinstance(turn, dict)
            ]
            native_turn_by_mirror_index = _match_native_turns_to_mirror_turns(
                turns,
                native_turns,
                session_end_record=session_end_record,
            )
            for index, turn in enumerate(turns):
                native_turn = native_turn_by_mirror_index.get(index)
                if native_turn is None:
                    continue
                native_messages = [
                    item
                    for item in native_turn.get("assistant_messages", [])
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                ]
                if len(native_messages) > 1:
                    has_mid_turn = True
                if native_messages:
                    turn["assistant_messages"] = native_messages
                    turn["assistant_message_count"] = len(native_messages)
                    turn["assistant_text"] = "\n\n".join(
                        item["text"] for item in native_messages
                    ).strip()
                    if not turn.get("assistant_final_text"):
                        turn["assistant_final_text"] = native_turn.get("assistant_final_text", "")
                turn["function_calls"] = _merge_function_calls(
                    turn.get("function_calls", []),
                    native_turn.get("function_calls", []),
                )
                turn["function_call_outputs"] = _merge_function_outputs(
                    turn.get("function_call_outputs", []),
                    native_turn.get("function_call_outputs", []),
                )
                if isinstance(native_turn.get("attachments"), list):
                    turn["attachments"] = _merge_attachments(
                        turn.get("attachments", []),
                        native_turn.get("attachments", []),
                    )
                if not turn.get("completed_at") and isinstance(native_turn.get("completed_at"), str):
                    turn["completed_at"] = native_turn.get("completed_at")
                    turn["closure"] = {
                        "status": "completed",
                        "source": "native-transcript",
                        "stop_reason": None,
                        "session_end_reason": None,
                        "diagnostics": {},
                    }
                    turn["operator_evidence"] = _classify_claude_operator_evidence(
                        closure=turn["closure"],
                        assistant_final_text=str(turn.get("assistant_final_text") or ""),
                    )
                if not attachments_captured and isinstance(turn.get("attachments"), list) and turn.get("attachments"):
                    attachments_captured = True

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
            "attachments_captured": attachments_captured,
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

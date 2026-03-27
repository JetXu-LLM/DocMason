"""Native chat reconciliation and interaction-ingest runtime helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .affordances import (
    DEFAULT_AFFORDANCE_FILENAME,
    derive_source_affordances,
    merge_derived_affordances,
)
from .conversation import (
    bound_conversation_id_for_host,
    current_host_identity,
    host_identity_key,
    semantic_log_context_fields,
    update_conversation_turn,
)
from .coordination import workspace_lease
from .front_controller import question_execution_profile
from .project import WorkspacePaths, ensure_json_parent, read_json, write_json
from .projections import refresh_runtime_projections
from .routing import (
    infer_entry_semantics,
    infer_memory_query_profile,
    infer_question_class,
    infer_question_domain,
    normalize_memory_semantics,
    normalize_question_analysis,
)
from .transcript import (
    codex_sessions_root,
    codex_state_db_path,
    decode_data_url,
    load_claude_code_transcript,
    load_codex_transcript,
)

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
RELATION_PRIORITY = {
    "corrects-source": 0,
    "clarifies-source": 1,
    "extends-source": 2,
    "visual-reference-for": 3,
    "constraint-for": 4,
    "derived-from-turn": 5,
}
MEMORY_KIND_PRIORITY = {
    "constraint": 0,
    "preference": 1,
    "correction": 2,
    "clarification": 3,
    "stakeholder-context": 4,
    "political-context": 5,
    "operator-intent": 6,
    "working-note": 7,
}
INTERACTION_REQUIRED_KNOWLEDGE_KEYS = (
    "source_id",
    "source_fingerprint",
    "title",
    "source_language",
    "summary_en",
    "summary_source",
    "document_type",
    "key_points",
    "entities",
    "claims",
    "known_gaps",
    "ambiguities",
    "confidence",
    "citations",
    "related_sources",
)
def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def tokenize_text(text: str) -> list[str]:
    """Return normalized lexical tokens."""
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def _detect_language(text: str) -> str:
    if not text.strip():
        return "unknown"
    ascii_ratio = sum(1 for character in text if ord(character) < 128) / max(len(text), 1)
    return "en" if ascii_ratio > 0.95 else "mixed-or-non-en"


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _json_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.json") if path.is_file())


def _read_interaction_runtime_json(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], str | None]:
    """Read mutable interaction-ingest JSON and tolerate transient partial writes."""
    try:
        return read_json(path), None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as exc:
        return (
            {},
            (
                f"Interaction runtime artifact `{path.name}` for {label} was unreadable during "
                f"this check and was ignored ({type(exc).__name__})."
            ),
        )


def _attachment_extension(mime_type: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/webp": ".webp",
        "application/pdf": ".pdf",
    }
    return mapping.get(mime_type.lower(), ".bin")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _current_catalog_source_lookup(
    paths: WorkspacePaths,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    catalog = read_json(paths.current_catalog_path).get("sources", [])
    by_current_path: dict[str, str] = {}
    by_basename: dict[str, str] = {}
    known_source_ids: set[str] = set()
    if not isinstance(catalog, list):
        return by_current_path, by_basename, known_source_ids
    for item in catalog:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        current_path = item.get("current_path")
        if not isinstance(source_id, str) or not isinstance(current_path, str):
            continue
        known_source_ids.add(source_id)
        by_current_path[current_path] = source_id
        by_basename[Path(current_path).name] = source_id
    return by_current_path, by_basename, known_source_ids


def refresh_generated_connector_manifests(paths: WorkspacePaths) -> dict[str, Any]:
    """Refresh generated local connector state for native interaction ingest."""
    codex_manifest = {
        "generated_at": utc_now(),
        "provider": "codex",
        "available": codex_state_db_path().exists() and codex_sessions_root().exists(),
        "state_db_path": str(codex_state_db_path()),
        "sessions_root": str(codex_sessions_root()),
        "connector_kind": "generated-local-adapter",
        "supports_native_reconciliation": True,
        "capability_scope": "connector-capture",
        "captures_attachments": True,
        "captures_multimodal_content": True,
        "host_product_capability_status": "not-evaluated",
        "fidelity_notes": (
            "These fields describe what the current DocMason Codex connector can "
            "reconcile from local Codex storage. They are not a ranking of host "
            "product capabilities."
        ),
    }
    write_json(paths.codex_connector_manifest_path, codex_manifest)
    claude_code_hooks_configured = (paths.root / ".claude" / "settings.json").exists()
    claude_code_manifest = {
        "generated_at": utc_now(),
        "provider": "claude-code",
        "available": claude_code_hooks_configured,
        "hooks_configured": claude_code_hooks_configured,
        "mirror_root": str(paths.claude_code_mirror_root),
        "connector_kind": "hook-mirror",
        "supports_native_reconciliation": True,
        "capability_scope": "connector-capture",
        "captures_attachments": False,
        "captures_multimodal_content": False,
        "host_product_capability_status": "not-evaluated",
        "fidelity_notes": (
            "Current DocMason Claude Code capture uses repo-local hooks plus "
            "optional native transcript enrichment. This connector records user "
            "prompts, tool calls (Bash+Agent), final responses, and session "
            "lifecycle. It does not currently ingest attachments or multimodal "
            "payloads through the hook-mirror path. These fields describe "
            "connector capture fidelity only, not Claude Code's native product "
            "capabilities."
        ),
    }
    write_json(paths.claude_code_connector_manifest_path, claude_code_manifest)
    manifest = {
        "generated_at": utc_now(),
        "connectors": [codex_manifest, claude_code_manifest],
    }
    write_json(paths.interaction_connector_manifest_path, manifest)
    return manifest


def _command_text(function_call: dict[str, Any]) -> str:
    tool_name = function_call.get("tool_name", "")
    # Codex uses "exec_command" with arguments.cmd or arguments_text.
    if tool_name == "exec_command":
        arguments = function_call.get("arguments")
        if isinstance(arguments, dict) and isinstance(arguments.get("cmd"), str):
            return str(arguments["cmd"])
        text = function_call.get("arguments_text")
        if isinstance(text, str):
            return text
        return ""
    # Claude Code uses "Bash" with tool_input.command.
    if tool_name == "Bash":
        arguments = function_call.get("arguments") or function_call.get("tool_input")
        if isinstance(arguments, dict) and isinstance(arguments.get("command"), str):
            return str(arguments["command"])
        return ""
    return ""


def _extract_docmason_commands(command_text: str) -> list[str]:
    normalized = command_text.replace("\n", " ")
    commands: list[str] = []
    for marker in ("docmason ", "-m docmason "):
        start = normalized.find(marker)
        if start < 0:
            continue
        command = normalized[start + len(marker) :].strip().split()
        if command:
            commands.append(f"docmason {command[0]}")
    return commands


def _extract_source_ids_from_command(
    command_text: str,
    *,
    by_current_path: dict[str, str],
    by_basename: dict[str, str],
    known_source_ids: set[str],
) -> list[str]:
    source_ids = [
        value for value in UUID_PATTERN.findall(command_text) if value in known_source_ids
    ]
    for current_path, source_id in by_current_path.items():
        if current_path in command_text:
            source_ids.append(source_id)
    for basename, source_id in by_basename.items():
        if basename and basename in command_text:
            source_ids.append(source_id)
    return _deduplicate_strings(source_ids)


def build_tool_use_audit(
    paths: WorkspacePaths, function_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    """Summarize native tool usage for one reconciled turn."""
    by_current_path, by_basename, known_source_ids = _current_catalog_source_lookup(paths)
    docmason_commands: list[str] = []
    consulted_source_ids: list[str] = []
    direct_knowledge_base_access = False
    direct_original_doc_access = False
    render_inspection_used = False
    tool_names: list[str] = []
    for function_call in function_calls:
        if not isinstance(function_call, dict):
            continue
        tool_name = function_call.get("tool_name")
        if isinstance(tool_name, str) and tool_name:
            tool_names.append(tool_name)
        if tool_name in {"view_image", "screenshot"}:
            render_inspection_used = True
        command_text = _command_text(function_call)
        if not command_text:
            continue
        if "knowledge_base/" in command_text:
            direct_knowledge_base_access = True
        if "original_doc/" in command_text:
            direct_original_doc_access = True
        if "/renders/" in command_text:
            render_inspection_used = True
        docmason_commands.extend(_extract_docmason_commands(command_text))
        consulted_source_ids.extend(
            _extract_source_ids_from_command(
                command_text,
                by_current_path=by_current_path,
                by_basename=by_basename,
                known_source_ids=known_source_ids,
            )
        )
    docmason_commands = _deduplicate_strings(docmason_commands)
    consulted_source_ids = _deduplicate_strings(consulted_source_ids)
    return {
        "tool_names": _deduplicate_strings(tool_names),
        "docmason_commands": docmason_commands,
        "consulted_source_ids": consulted_source_ids,
        "direct_knowledge_base_access": direct_knowledge_base_access,
        "direct_original_doc_access": direct_original_doc_access,
        "render_inspection_used": render_inspection_used,
        "docmason_adjacent": bool(
            docmason_commands
            or direct_knowledge_base_access
            or direct_original_doc_access
            or render_inspection_used
        ),
    }


def _looks_like_constraint_update(text: str) -> bool:
    normalized = text.lower()
    markers = (
        "reconsider",
        "update",
        "correction",
        "clarify",
        "constraint",
        "wrong",
        "should",
        "must",
        "强调",
        "重新考虑",
        "补充",
        "修正",
        "约束",
        "需要",
        "应该",
    )
    return any(marker in normalized for marker in markers)


def classify_continuation_type(
    *,
    turn_index: int,
    user_text: str,
    attachments: list[dict[str, Any]],
    audit: dict[str, Any],
    previous_consulted_source_ids: list[str],
) -> tuple[str | None, bool]:
    """Classify a follow-up turn as constraint-update, evidence-refresh, or mixed."""
    if turn_index <= 1:
        return None, False
    has_new_evidence = bool(
        audit.get("docmason_commands")
        or audit.get("direct_knowledge_base_access")
        or audit.get("direct_original_doc_access")
        or audit.get("render_inspection_used")
    )
    has_new_context = bool(attachments) or _looks_like_constraint_update(user_text)
    if not has_new_evidence:
        return "constraint-update", bool(previous_consulted_source_ids)
    if has_new_context:
        return "mixed", bool(previous_consulted_source_ids)
    return "evidence-refresh", False


def _relation_type_from_entry(entry: dict[str, Any]) -> str:
    user_text = str(entry.get("user_text", "")).lower()
    continuation_type = entry.get("continuation_type")
    if any(marker in user_text for marker in ("correct", "incorrect", "修正", "纠正")):
        return "corrects-source"
    if entry.get("attachment_ids"):
        return "visual-reference-for"
    if continuation_type == "constraint-update":
        return "constraint-for"
    if continuation_type == "mixed":
        return "clarifies-source"
    if any(marker in user_text for marker in ("extend", "additional", "补充", "增加")):
        return "extends-source"
    return "derived-from-turn"


def _entry_title(user_text: str, *, conversation_id: str, turn_id: str) -> str:
    stripped = " ".join(user_text.split())
    if stripped:
        return stripped[:96]
    return f"Interaction entry {conversation_id} {turn_id}"


def _entry_searchable_text(entry: dict[str, Any]) -> str:
    parts = [
        str(entry.get("title", "")),
        str(entry.get("user_text", "")),
        str(entry.get("assistant_excerpt", "")),
        " ".join(str(value) for value in entry.get("related_source_ids", [])),
    ]
    return "\n".join(part for part in parts if part.strip())


def _store_attachment(
    paths: WorkspacePaths,
    *,
    interaction_id: str,
    attachment_index: int,
    attachment: dict[str, Any],
) -> dict[str, Any]:
    image_url = attachment.get("image_url")
    attachment_id = f"{interaction_id}-attachment-{attachment_index:03d}"
    if not isinstance(image_url, str) or not image_url:
        return {
            "attachment_id": attachment_id,
            "attachment_type": attachment.get("attachment_type", "unknown"),
            "stored_path": None,
            "sha256": None,
            "mime_type": None,
        }
    mime_type, raw_bytes = decode_data_url(image_url)
    digest = _sha256_bytes(raw_bytes)
    extension = _attachment_extension(mime_type)
    stored_path = paths.interaction_attachments_dir / f"{digest}{extension}"
    if not stored_path.exists():
        ensure_json_parent(stored_path)
        stored_path.write_bytes(raw_bytes)
    return {
        "attachment_id": attachment_id,
        "attachment_type": attachment.get("attachment_type", "image"),
        "stored_path": str(stored_path.relative_to(paths.root)),
        "sha256": digest,
        "mime_type": mime_type,
    }


def _interaction_entry_path(paths: WorkspacePaths, interaction_id: str) -> Path:
    return paths.interaction_entries_dir / f"{interaction_id}.json"


def pending_interaction_entries(paths: WorkspacePaths) -> list[dict[str, Any]]:
    """Load all pending interaction-ingest entries."""
    entries: list[dict[str, Any]] = []
    for path in _json_files(paths.interaction_entries_dir):
        payload = read_json(path)
        if payload and payload.get("pending_promotion", True):
            entries.append(payload)
    return entries


def _entry_channel_descriptors(
    entry: dict[str, Any],
    *,
    attachment_paths: list[str],
) -> dict[str, list[str]]:
    text_excerpt = str(entry.get("searchable_text", "")).strip()
    question_domain = str(entry.get("question_domain") or "")
    question_class = str(entry.get("question_class") or "")
    support_strategy = str(entry.get("support_strategy") or "")
    descriptors: dict[str, list[str]] = {
        "text": [],
        "render": [],
        "structure": [],
        "notes": [],
        "media": [],
    }
    title = str(entry.get("title") or "").strip()
    if title:
        descriptors["text"].append(title)
    if text_excerpt:
        descriptors["text"].append(text_excerpt[:180])
    descriptors["structure"].append("Interaction turn metadata is available.")
    if question_class or question_domain or support_strategy:
        descriptors["structure"].append(
            "Interaction semantics: "
            + ", ".join(
                part
                for part in [
                    f"class={question_class}" if question_class else "",
                    f"domain={question_domain}" if question_domain else "",
                    f"strategy={support_strategy}" if support_strategy else "",
                ]
                if part
            )
        )
    if attachment_paths:
        descriptors["render"].append("Published interaction attachments are available.")
        descriptors["media"].append("Interaction attachments provide media evidence.")
    return descriptors


def _available_channels_from_descriptors(descriptors: dict[str, list[str]]) -> list[str]:
    ordered_channels = ("text", "render", "structure", "notes", "media")
    return [channel for channel in ordered_channels if descriptors.get(channel)]


def _overlay_source_record(entry: dict[str, Any]) -> dict[str, Any]:
    interaction_id = str(entry["interaction_id"])
    title = str(entry.get("title") or interaction_id)
    summary = str(entry.get("user_text") or "")[:500]
    attachment_paths = [
        str(attachment["stored_path"])
        for attachment in entry.get("attachment_refs", [])
        if isinstance(attachment, dict) and isinstance(attachment.get("stored_path"), str)
    ]
    channel_descriptors = _entry_channel_descriptors(
        entry,
        attachment_paths=attachment_paths,
    )
    unit_ids = [
        str(unit["unit_id"]) for unit in entry.get("overlay_units", []) if isinstance(unit, dict)
    ]
    return {
        "source_id": interaction_id,
        "source_fingerprint": entry.get("entry_fingerprint"),
        "current_path": entry.get("entry_path"),
        "document_type": "interaction",
        "source_family": "interaction-pending",
        "trust_tier": "interaction",
        "pending_promotion": True,
        "source_language": _detect_language(str(entry.get("searchable_text", ""))),
        "title": title,
        "summary_en": summary,
        "summary_source": summary,
        "summary_markdown": summary,
        "entities": [],
        "key_points": [summary] if summary else [],
        "claims": [],
        "known_gaps": [],
        "ambiguities": [],
        "citation_count": len(unit_ids),
        "citation_density": min(float(len(unit_ids)), 3.0),
        "question_class": entry.get("question_class"),
        "question_domain": entry.get("question_domain"),
        "support_strategy": entry.get("support_strategy"),
        "analysis_origin": entry.get("analysis_origin"),
        "support_basis": entry.get("support_basis"),
        "support_manifest_path": entry.get("support_manifest_path"),
        "memory_kind": entry.get("memory_kind"),
        "durability": entry.get("durability"),
        "uncertainty": entry.get("uncertainty"),
        "answer_use_policy": entry.get("answer_use_policy"),
        "retrieval_rank_prior": entry.get("retrieval_rank_prior"),
        "related_source_ids": entry.get("related_source_ids", []),
        "top_citation_unit_ids": unit_ids[:3],
        "available_channels": _available_channels_from_descriptors(channel_descriptors),
        "channel_descriptors": channel_descriptors,
        "affordance_confidence": "medium",
        "affordance_derivation_mode": "deterministic",
        "trust_prior": {
            "interaction_conversation_id": entry.get("conversation_id"),
            "interaction_turn_id": entry.get("turn_id"),
        },
        "path_tokens": tokenize_text(str(entry.get("entry_path", ""))),
        "searchable_text": entry.get("searchable_text", ""),
    }


def _overlay_units(entry: dict[str, Any]) -> list[dict[str, Any]]:
    interaction_id = str(entry["interaction_id"])
    units: list[dict[str, Any]] = []
    attachment_paths = [
        str(attachment["stored_path"])
        for attachment in entry.get("attachment_refs", [])
        if isinstance(attachment, dict) and isinstance(attachment.get("stored_path"), str)
    ]
    channel_descriptors = _entry_channel_descriptors(
        entry,
        attachment_paths=attachment_paths,
    )
    text_unit = {
        "source_id": interaction_id,
        "source_fingerprint": entry.get("entry_fingerprint"),
        "current_path": entry.get("entry_path"),
        "document_type": "interaction",
        "source_family": "interaction-pending",
        "trust_tier": "interaction",
        "pending_promotion": True,
        "unit_id": "turn-text",
        "unit_type": "interaction-turn",
        "ordinal": 1,
        "title": entry.get("title"),
        "text_asset": None,
        "structure_asset": None,
        "render_references": attachment_paths,
        "embedded_media": attachment_paths,
        "available_channels": _available_channels_from_descriptors(channel_descriptors),
        "channel_descriptors": channel_descriptors,
        "affordance_confidence": "medium",
        "affordance_derivation_mode": "deterministic",
        "hidden": False,
        "extraction_confidence": "medium",
        "citation_count": 1,
        "citation_density": 1.0,
        "trust_prior_inputs": {
            "conversation_id": entry.get("conversation_id"),
            "turn_id": entry.get("turn_id"),
        },
        "text": entry.get("searchable_text", ""),
        "structure_summary": json.dumps(
            {
                "conversation_id": entry.get("conversation_id"),
                "turn_id": entry.get("turn_id"),
                "native_turn_id": entry.get("native_turn_id"),
                "continuation_type": entry.get("continuation_type"),
                "question_class": entry.get("question_class"),
                "question_domain": entry.get("question_domain"),
                "support_strategy": entry.get("support_strategy"),
                "analysis_origin": entry.get("analysis_origin"),
                "memory_kind": entry.get("memory_kind"),
                "uncertainty": entry.get("uncertainty"),
            },
            sort_keys=True,
        ),
        "searchable_text": entry.get("searchable_text", ""),
    }
    units.append(text_unit)
    attachment_ordinal = 1
    for attachment in entry.get("attachment_refs", []):
        if not isinstance(attachment, dict):
            continue
        stored_path = attachment.get("stored_path")
        if not isinstance(stored_path, str) or not stored_path:
            continue
        units.append(
            {
                "source_id": interaction_id,
                "source_fingerprint": entry.get("entry_fingerprint"),
                "current_path": entry.get("entry_path"),
                "document_type": "interaction",
                "source_family": "interaction-pending",
                "trust_tier": "interaction",
                "pending_promotion": True,
                "unit_id": f"attachment-{attachment_ordinal:03d}",
                "unit_type": "interaction-attachment",
                "ordinal": attachment_ordinal + 1,
                "title": f"Attachment {attachment_ordinal}",
                "text_asset": None,
                "structure_asset": None,
                "render_references": [stored_path],
                "embedded_media": [stored_path],
                "available_channels": ["render", "structure", "media"],
                "channel_descriptors": {
                    "text": [],
                    "render": ["Published interaction attachment render is available."],
                    "structure": ["Attachment metadata is available for this interaction unit."],
                    "notes": [],
                    "media": ["Interaction attachment provides media evidence."],
                },
                "affordance_confidence": "medium",
                "affordance_derivation_mode": "deterministic",
                "hidden": False,
                "extraction_confidence": "low",
                "citation_count": 0,
                "citation_density": 0.0,
                "trust_prior_inputs": {
                    "conversation_id": entry.get("conversation_id"),
                    "turn_id": entry.get("turn_id"),
                },
                "text": "",
                "structure_summary": json.dumps(attachment, sort_keys=True),
                "searchable_text": json.dumps(attachment, sort_keys=True),
            }
        )
        attachment_ordinal += 1
    return units


def refresh_interaction_overlay(paths: WorkspacePaths) -> dict[str, Any]:
    """Rebuild the runtime pending overlay retrieval and trace artifacts."""
    with workspace_lease(paths, "interaction-overlay"):
        entries = pending_interaction_entries(paths)
        source_records: list[dict[str, Any]] = []
        unit_records: list[dict[str, Any]] = []
        graph_edges: list[dict[str, Any]] = []
        source_provenance: dict[str, Any] = {}
        unit_provenance: dict[str, Any] = {}
        relation_index: dict[str, Any] = {}
        knowledge_consumers: dict[str, Any] = {}
        queue_items: list[dict[str, Any]] = []

        for entry in entries:
            overlay_units = _overlay_units(entry)
            entry["overlay_units"] = overlay_units
            source_record = _overlay_source_record(entry)
            source_records.append(source_record)
            unit_records.extend(overlay_units)
            unit_ids = [
                unit["unit_id"] for unit in overlay_units if isinstance(unit.get("unit_id"), str)
            ]
            relations: list[dict[str, Any]] = []
            relation_type = _relation_type_from_entry(entry)
            for related_source_id in entry.get("related_source_ids", []):
                if not isinstance(related_source_id, str):
                    continue
                edge = {
                    "source_id": entry["interaction_id"],
                    "related_source_id": related_source_id,
                    "relation_type": relation_type,
                    "strength": "medium",
                    "status": "supported",
                    "citation_unit_ids": ["turn-text"],
                }
                graph_edges.append(edge)
                relations.append(edge)
            source_provenance[str(entry["interaction_id"])] = {
                "source_id": entry["interaction_id"],
                "source_fingerprint": entry.get("entry_fingerprint"),
                "current_path": entry.get("entry_path"),
                "document_type": "interaction",
                "source_family": "interaction-pending",
                "trust_tier": "interaction",
                "title": entry.get("title"),
                "summary_en": str(entry.get("user_text", ""))[:500],
                "summary_source": str(entry.get("user_text", ""))[:500],
                "available_channels": source_record.get("available_channels", []),
                "channel_descriptors": source_record.get("channel_descriptors", {}),
                "affordance_confidence": source_record.get("affordance_confidence"),
                "affordance_derivation_mode": source_record.get("affordance_derivation_mode"),
                "question_class": entry.get("question_class"),
                "question_domain": entry.get("question_domain"),
                "support_strategy": entry.get("support_strategy"),
                "analysis_origin": entry.get("analysis_origin"),
                "support_basis": entry.get("support_basis"),
                "support_manifest_path": entry.get("support_manifest_path"),
                "memory_kind": entry.get("memory_kind"),
                "durability": entry.get("durability"),
                "uncertainty": entry.get("uncertainty"),
                "answer_use_policy": entry.get("answer_use_policy"),
                "retrieval_rank_prior": entry.get("retrieval_rank_prior"),
                "summary_markdown_path": str(Path(entry.get("entry_path", "")).name),
                "summary_markdown": str(entry.get("user_text", "")),
                "source_manifest_path": entry.get("entry_path"),
                "evidence_manifest_path": entry.get("entry_path"),
                "top_citation_unit_ids": unit_ids[:3],
                "cited_unit_ids": unit_ids,
                "unit_citation_counts": {unit_id: 1 for unit_id in unit_ids},
                "relations": {"outgoing": relations, "incoming": []},
                "render_paths": [
                    attachment.get("stored_path")
                    for attachment in entry.get("attachment_refs", [])
                    if isinstance(attachment, dict)
                    and isinstance(attachment.get("stored_path"), str)
                ],
            }
            relation_index[str(entry["interaction_id"])] = {"outgoing": relations, "incoming": []}
            for unit in overlay_units:
                unit_key = f"{entry['interaction_id']}:{unit['unit_id']}"
                consumer = {
                    "consumer_type": "interaction-entry",
                    "support": "Interaction-derived overlay support",
                }
                unit_provenance[unit_key] = {
                    "source_id": entry["interaction_id"],
                    "unit_id": unit["unit_id"],
                    "document_type": "interaction",
                    "source_family": "interaction-pending",
                    "trust_tier": "interaction",
                    "current_path": entry.get("entry_path"),
                    "title": unit.get("title"),
                    "unit_type": unit.get("unit_type"),
                    "ordinal": unit.get("ordinal"),
                    "available_channels": unit.get("available_channels", []),
                    "channel_descriptors": unit.get("channel_descriptors", {}),
                    "affordance_confidence": unit.get("affordance_confidence"),
                    "affordance_derivation_mode": unit.get("affordance_derivation_mode"),
                    "extraction_confidence": unit.get("extraction_confidence"),
                    "hidden": False,
                    "text_asset": unit.get("text_asset"),
                    "structure_asset": unit.get("structure_asset"),
                    "render_references": unit.get("render_references", []),
                    "embedded_media": unit.get("embedded_media", []),
                    "text_excerpt": unit.get("text", ""),
                    "consumers": [consumer],
                }
                knowledge_consumers[unit_key] = {
                    "source_id": entry["interaction_id"],
                    "unit_id": unit["unit_id"],
                    "consumers": [consumer],
                    "consumer_summaries": ["Interaction-derived overlay support"],
                }
            queue_items.append(
                {
                    "interaction_id": entry["interaction_id"],
                    "conversation_id": entry.get("conversation_id"),
                    "turn_id": entry.get("turn_id"),
                    "recorded_at": entry.get("recorded_at"),
                    "question_class": entry.get("question_class"),
                    "question_domain": entry.get("question_domain"),
                    "support_strategy": entry.get("support_strategy"),
                    "analysis_origin": entry.get("analysis_origin"),
                    "related_source_ids": entry.get("related_source_ids", []),
                }
            )

        manifest = {
            "generated_at": utc_now(),
            "pending_entry_count": len(entries),
            "source_count": len(source_records),
            "unit_count": len(unit_records),
            "graph_edge_count": len(graph_edges),
        }
        write_json(paths.interaction_overlay_manifest_path, manifest)
        write_json(paths.interaction_overlay_source_records_path, {"records": source_records})
        write_json(paths.interaction_overlay_unit_records_path, {"records": unit_records})
        write_json(paths.interaction_overlay_graph_edges_path, {"edges": graph_edges})
        write_json(paths.interaction_overlay_source_provenance_path, source_provenance)
        write_json(paths.interaction_overlay_unit_provenance_path, unit_provenance)
        write_json(paths.interaction_overlay_relation_index_path, relation_index)
        write_json(paths.interaction_overlay_knowledge_consumers_path, knowledge_consumers)
        write_json(
            paths.interaction_promotion_queue_path,
            {
                "generated_at": utc_now(),
                "pending_promotion_count": len(entries),
                "entries": queue_items,
            },
        )
        return manifest


def load_interaction_overlay(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the runtime pending interaction overlay."""
    manifest, manifest_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_manifest_path,
        label="overlay manifest",
    )
    source_records_payload, source_records_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_source_records_path,
        label="overlay source records",
    )
    unit_records_payload, unit_records_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_unit_records_path,
        label="overlay unit records",
    )
    graph_edges_payload, graph_edges_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_graph_edges_path,
        label="overlay graph edges",
    )
    source_provenance, source_provenance_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_source_provenance_path,
        label="overlay source provenance",
    )
    unit_provenance, unit_provenance_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_unit_provenance_path,
        label="overlay unit provenance",
    )
    relation_index, relation_index_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_relation_index_path,
        label="overlay relation index",
    )
    knowledge_consumers, knowledge_consumers_warning = _read_interaction_runtime_json(
        paths.interaction_overlay_knowledge_consumers_path,
        label="overlay knowledge consumers",
    )
    return {
        "manifest": manifest,
        "source_records": source_records_payload.get("records", []),
        "unit_records": unit_records_payload.get("records", []),
        "graph_edges": graph_edges_payload.get("edges", []),
        "source_provenance": source_provenance,
        "unit_provenance": unit_provenance,
        "relation_index": relation_index,
        "knowledge_consumers": knowledge_consumers,
        "load_warnings": [
            warning
            for warning in [
                manifest_warning,
                source_records_warning,
                unit_records_warning,
                graph_edges_warning,
                source_provenance_warning,
                unit_provenance_warning,
                relation_index_warning,
                knowledge_consumers_warning,
            ]
            if warning
        ],
    }


def interaction_ingest_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Summarize runtime interaction-ingest state for doctor and status."""
    overlay = load_interaction_overlay(paths)
    overlay_manifest_raw = overlay.get("manifest")
    overlay_manifest = overlay_manifest_raw if isinstance(overlay_manifest_raw, dict) else {}
    connector_manifest, connector_warning = _read_interaction_runtime_json(
        paths.interaction_connector_manifest_path,
        label="connector manifest",
    )
    promotion_queue, promotion_warning = _read_interaction_runtime_json(
        paths.interaction_promotion_queue_path,
        label="promotion queue",
    )
    reconciliation_state, reconciliation_warning = _read_interaction_runtime_json(
        paths.interaction_reconciliation_state_path,
        label="reconciliation state",
    )
    load_warnings = [
        warning
        for warning in [
            connector_warning,
            promotion_warning,
            reconciliation_warning,
            *[
                warning
                for warning in overlay.get("load_warnings", [])
                if isinstance(warning, str) and warning.strip()
            ],
        ]
        if warning
    ]
    return {
        "connector_available": any(
            isinstance(item, dict) and item.get("available")
            for item in connector_manifest.get("connectors", [])
            if isinstance(connector_manifest.get("connectors"), list)
        ),
        "pending_capture_count": int(overlay_manifest.get("pending_entry_count", 0) or 0),
        "pending_promotion_count": int(
            promotion_queue.get(
                "pending_promotion_count", overlay_manifest.get("pending_entry_count", 0)
            )
            or 0
        ),
        "last_overlay_at": overlay_manifest.get("generated_at"),
        "last_reconcile_at": reconciliation_state.get("last_reconciled_at"),
        "sync_recommended": bool(
            int(
                promotion_queue.get(
                    "pending_promotion_count", overlay_manifest.get("pending_entry_count", 0)
                )
                or 0
            )
        ),
        "load_warnings": load_warnings,
    }


def interaction_overlay_relevance(
    paths: WorkspacePaths,
    question: str,
    *,
    question_class: str | None = None,
    question_domain: str | None = None,
) -> dict[str, Any]:
    """Return a compact relevance summary against pending interaction entries."""
    query_terms = set(tokenize_text(question))
    if question_class is None:
        question_class, _workflow_id, _route_reason = infer_question_class(question)
    if question_domain is None:
        question_domain = infer_question_domain(question, question_class=question_class)
    if question_domain == "external-factual":
        return {
            "best_source_id": None,
            "best_score": 0,
            "minimum_score": 99,
            "has_relevant_pending_interaction": False,
        }
    memory_profile = infer_memory_query_profile(
        question,
        question_class=question_class,
        question_domain=question_domain,
    )
    overlay = load_interaction_overlay(paths)
    best_source_id = None
    best_score = 0
    best_threshold = 0
    for source_record in overlay.get("source_records", []):
        if not isinstance(source_record, dict):
            continue
        searchable_text = str(source_record.get("searchable_text", ""))
        score = len(query_terms & set(tokenize_text(searchable_text)))
        memory_kind = str(source_record.get("memory_kind") or "")
        answer_use_policy = str(source_record.get("answer_use_policy") or "contextual-only")
        durability = str(source_record.get("durability") or "ephemeral")
        threshold = 1 if question_domain == "composition" else 2
        if question_domain == "general-stable":
            threshold = 3
        if memory_profile["mode"] == "minimal" and source_record.get(
            "answer_use_policy"
        ) == "contextual-only":
            score = max(score - 2, 0)
        relevant_kinds = set(memory_profile.get("relevant_memory_kinds", []))
        if relevant_kinds and memory_kind in relevant_kinds:
            score += 2
            threshold = max(1, threshold - 1)
        if answer_use_policy == "contextual-only":
            threshold += 1
        if durability in {"situational", "ephemeral"} and question_domain != "composition":
            threshold += 1
        if question_domain in {"workspace-corpus", "general-stable"} and memory_kind in {
            "operator-intent",
            "working-note",
        }:
            threshold += 1
        if score > best_score:
            best_score = score
            best_source_id = source_record.get("source_id")
            best_threshold = threshold
    return {
        "best_source_id": best_source_id,
        "best_score": best_score,
        "minimum_score": best_threshold,
        "has_relevant_pending_interaction": bool(
            best_source_id and best_threshold > 0 and best_score >= best_threshold
        ),
    }


def _write_answer_file_if_missing(
    paths: WorkspacePaths, *, conversation_id: str, turn_id: str, assistant_text: str
) -> str:
    answer_path = paths.answers_dir / conversation_id / f"{turn_id}.md"
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    if assistant_text.strip() and not answer_path.exists():
        answer_path.write_text(assistant_text.strip() + "\n", encoding="utf-8")
    return str(answer_path.relative_to(paths.root))


def _persist_interaction_entry(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    native_turn_id: str,
    recorded_at: str,
    user_text: str,
    assistant_excerpt: str,
    attachment_refs: list[dict[str, Any]],
    continuation_type: str | None,
    related_source_ids: list[str],
    tool_use_audit: dict[str, Any],
    question_class: str | None = None,
    question_domain: str | None = None,
    support_strategy: str | None = None,
    analysis_origin: str | None = None,
    support_basis: str | None = None,
    support_manifest_path: str | None = None,
    semantic_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    interaction_id = f"interaction-{conversation_id}-{native_turn_id}"
    existing_entry = read_json(_interaction_entry_path(paths, interaction_id))
    preserve_promotion = (
        isinstance(existing_entry, dict)
        and existing_entry.get("pending_promotion") is False
        and existing_entry.get("status") == "promoted"
    )
    title = _entry_title(user_text, conversation_id=conversation_id, turn_id=turn_id)
    relation_type = _relation_type_from_entry(
        {
            "user_text": user_text,
            "continuation_type": continuation_type,
            "attachment_ids": [item.get("attachment_id") for item in attachment_refs],
        }
    )
    semantics = infer_entry_semantics(
        user_text=user_text,
        continuation_type=continuation_type,
        tool_use_audit=tool_use_audit,
    )
    entry = {
        "interaction_id": interaction_id,
        "recorded_at": recorded_at,
        "updated_at": utc_now(),
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "native_turn_id": native_turn_id,
        "source_family": "interaction-pending",
        "trust_tier": "interaction",
        "pending_promotion": not preserve_promotion,
        "title": title,
        "user_text": user_text,
        "assistant_excerpt": assistant_excerpt[:500],
        "attachment_ids": [
            attachment.get("attachment_id")
            for attachment in attachment_refs
            if isinstance(attachment, dict) and isinstance(attachment.get("attachment_id"), str)
        ],
        "attachment_refs": attachment_refs,
        "continuation_type": continuation_type,
        "continuation_classification_method": "deterministic-v1",
        "memory_kind": semantics["memory_kind"],
        "durability": semantics["durability"],
        "uncertainty": semantics["uncertainty"],
        "answer_use_policy": semantics["answer_use_policy"],
        "retrieval_rank_prior": semantics["retrieval_rank_prior"],
        "related_source_ids": related_source_ids,
        "relation_hints": [
            {
                "related_source_id": related_source_id,
                "relation_type": relation_type,
            }
            for related_source_id in related_source_ids
        ],
        "tool_use_audit": tool_use_audit,
        "entry_path": str(_interaction_entry_path(paths, interaction_id).relative_to(paths.root)),
        "status": "promoted" if preserve_promotion else "pending",
    }
    if preserve_promotion:
        entry["promoted_memory_id"] = existing_entry.get("promoted_memory_id")
        entry["promoted_at"] = existing_entry.get("promoted_at")
    entry.update(
        semantic_log_context_fields(
            question_class=question_class,
            question_domain=question_domain,
            support_strategy=support_strategy,
            analysis_origin=analysis_origin,
            support_basis=support_basis,
            support_manifest_path=support_manifest_path,
        )
    )
    if isinstance(semantic_analysis, dict) and semantic_analysis:
        entry["semantic_analysis"] = dict(semantic_analysis)
    entry["searchable_text"] = _entry_searchable_text(entry)
    entry["entry_fingerprint"] = _sha256_text(json.dumps(entry, sort_keys=True))
    write_json(_interaction_entry_path(paths, interaction_id), entry)
    return entry


def _native_ledger_path(paths: WorkspacePaths, ledger_id: str) -> Path:
    return paths.native_ledger_dir / f"{ledger_id}.json"


def _native_ledger_id(host_identity: dict[str, Any]) -> str:
    key = host_identity_key(host_identity)
    if isinstance(key, str) and key:
        return key
    return _sha256_text(json.dumps(host_identity, sort_keys=True, ensure_ascii=False))


def load_native_ledger(paths: WorkspacePaths, ledger_id: str) -> dict[str, Any]:
    """Load one native reconciliation ledger."""
    return read_json(_native_ledger_path(paths, ledger_id))


def load_bound_native_ledger(
    paths: WorkspacePaths,
    *,
    host_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load the native ledger for the current host identity, when available."""
    resolved_host_identity = (
        dict(host_identity) if isinstance(host_identity, dict) else current_host_identity()
    )
    ledger_id = _native_ledger_id(resolved_host_identity)
    return load_native_ledger(paths, ledger_id)


def _base_native_ledger(
    *,
    ledger_id: str,
    host_identity: dict[str, Any],
) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "ledger_id": ledger_id,
        "host_identity": dict(host_identity),
        "opened_at": now,
        "updated_at": now,
        "turns": [],
    }


def _native_profile(
    question: str,
    *,
    semantic_analysis: dict[str, Any] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_question_analysis(
        question,
        semantic_analysis=semantic_analysis,
        fallback_hints=fallback_hints,
    )
    evidence_requirements = dict(normalized["evidence_requirements"])
    preferred_channels = [
        channel
        for channel in evidence_requirements.get("preferred_channels", [])
        if isinstance(channel, str)
    ]
    return {
        "question_class": str(normalized["question_class"]),
        "question_domain": str(normalized["question_domain"]),
        "inner_workflow_id": str(normalized["inner_workflow_id"]),
        "support_strategy": str(normalized["support_strategy"]),
        "route_reason": str(normalized["route_reason"]),
        "analysis_origin": str(normalized["analysis_origin"]),
        "semantic_analysis": dict(normalized),
        "inspection_scope": str(evidence_requirements.get("inspection_scope") or "unit"),
        "preferred_channels": preferred_channels,
        "evidence_requirements": evidence_requirements,
        "memory_query_profile": dict(normalized["memory_query_profile"]),
    }


def _find_native_turn_index(ledger: dict[str, Any], native_turn_id: str) -> int | None:
    turns = ledger.get("turns", [])
    if not isinstance(turns, list):
        return None
    for index, turn in enumerate(turns):
        if not isinstance(turn, dict):
            continue
        if turn.get("native_turn_id") == native_turn_id:
            return index
    return None


def _upsert_native_ledger_turn(
    paths: WorkspacePaths,
    *,
    host_identity: dict[str, Any],
    native_turn_id: str,
    user_text: str,
    assistant_excerpt: str,
    recorded_at: str,
    completed_at: str | None,
    tool_use_audit: dict[str, Any],
    related_source_ids: list[str],
    continuation_type: str | None,
    reused_previous_evidence: bool,
    profile: dict[str, Any],
    attachment_refs: list[dict[str, Any]],
    reconciliation: dict[str, Any],
) -> dict[str, Any]:
    ledger_id = _native_ledger_id(host_identity)
    path = _native_ledger_path(paths, ledger_id)
    ledger = load_native_ledger(paths, ledger_id)
    if not ledger:
        ledger = _base_native_ledger(ledger_id=ledger_id, host_identity=host_identity)
    turns = ledger.get("turns", [])
    if not isinstance(turns, list):
        turns = []
    index = _find_native_turn_index(ledger, native_turn_id)
    if index is None:
        turn = {
            "native_turn_id": native_turn_id,
            "recorded_at": recorded_at,
            "completed_at": completed_at,
            "user_text": user_text,
            "assistant_excerpt": assistant_excerpt[:500],
            "tool_use_audit": tool_use_audit,
            "related_source_ids": related_source_ids,
            "continuation_type": continuation_type,
            "reused_previous_evidence": reused_previous_evidence,
            "attachments": attachment_refs,
            "promotion": None,
        }
        turns.append(turn)
        index = len(turns) - 1
    else:
        turn = turns[index]
    turn.update(
        {
            "native_turn_id": native_turn_id,
            "recorded_at": recorded_at,
            "completed_at": completed_at,
            "user_text": user_text,
            "assistant_excerpt": assistant_excerpt[:500],
            "tool_use_audit": tool_use_audit,
            "related_source_ids": related_source_ids,
            "continuation_type": continuation_type,
            "reused_previous_evidence": reused_previous_evidence,
            "attachments": attachment_refs,
            "question_class": profile["question_class"],
            "question_domain": profile["question_domain"],
            "inner_workflow_id": profile["inner_workflow_id"],
            "support_strategy": profile["support_strategy"],
            "analysis_origin": profile["analysis_origin"],
            "route_reason": profile["route_reason"],
            "semantic_analysis": dict(profile["semantic_analysis"]),
            "inspection_scope": profile["inspection_scope"],
            "preferred_channels": list(profile["preferred_channels"]),
            "anomaly_flags": list(host_identity.get("anomaly_flags", [])),
            "reconciliation": reconciliation,
        }
    )
    ledger["turns"] = turns
    ledger["updated_at"] = utc_now()
    ledger["host_identity"] = dict(host_identity)
    ledger["anomaly_flags"] = list(host_identity.get("anomaly_flags", []))
    write_json(path, ledger)
    return {
        "ledger_id": ledger_id,
        "ledger_path": str(path.relative_to(paths.root)),
        "turn": dict(turn),
    }


def promote_native_ledger_turn(
    paths: WorkspacePaths,
    *,
    ledger_id: str,
    native_turn_id: str,
    conversation_id: str,
    turn_id: str,
    promotion_kind: str,
    promotion_reason: str,
) -> dict[str, Any]:
    """Promote one native ledger turn into an explicitly linked canonical turn."""
    ledger_path = _native_ledger_path(paths, ledger_id)
    ledger = read_json(ledger_path)
    turns = ledger.get("turns", [])
    if not isinstance(turns, list):
        raise KeyError(native_turn_id)
    promoted_turn: dict[str, Any] | None = None
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("native_turn_id") != native_turn_id:
            continue
        turn["promotion"] = {
            "promotion_kind": promotion_kind,
            "promotion_reason": promotion_reason,
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "promoted_at": utc_now(),
        }
        promoted_turn = turn
        break
    if promoted_turn is None:
        raise KeyError(native_turn_id)
    ledger["updated_at"] = utc_now()
    write_json(ledger_path, ledger)
    native_ledger_ref = {
        "ledger_id": ledger_id,
        "native_turn_id": native_turn_id,
        "ledger_path": str(ledger_path.relative_to(paths.root)),
    }
    update_conversation_turn(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        updates={
            "native_ledger_ref": native_ledger_ref,
            "promotion_kind": promotion_kind,
            "promotion_reason": promotion_reason,
        },
    )
    return {
        "ledger_id": ledger_id,
        "native_turn_id": native_turn_id,
        "native_ledger_ref": native_ledger_ref,
        "promotion_kind": promotion_kind,
        "promotion_reason": promotion_reason,
    }
def _resolved_host_identity(
    *,
    provider: str,
    host_thread_ref: str,
    argument_source: str,
) -> dict[str, Any]:
    host_identity = current_host_identity(agent_surface=provider)
    normalized_ref = str(host_thread_ref).strip()
    if not normalized_ref:
        return dict(host_identity)
    existing_ref = str(host_identity.get("host_thread_ref") or "").strip()
    if not existing_ref:
        host_identity["host_thread_ref"] = normalized_ref
        host_identity["host_identity_source"] = argument_source
        host_identity["host_identity_trust"] = "reconciliation-argument"
        return host_identity
    if existing_ref != normalized_ref:
        anomaly_flags = list(host_identity.get("anomaly_flags", []))
        anomaly_flags.extend(["anomalous-host-identity", "host-thread-ref-mismatch"])
        host_identity["anomaly_flags"] = _deduplicate_strings(anomaly_flags)
        host_identity["host_thread_ref"] = normalized_ref
        host_identity["host_identity_source"] = argument_source
        host_identity["host_identity_trust"] = "reconciliation-argument"
    return host_identity


def _reconcile_native_transcript(
    paths: WorkspacePaths,
    *,
    provider: str,
    host_thread_ref: str,
    host_identity_source: str,
    transcript: dict[str, Any],
    reconciliation_metadata: dict[str, Any],
) -> dict[str, Any]:
    host_identity = _resolved_host_identity(
        provider=provider,
        host_thread_ref=host_thread_ref,
        argument_source=host_identity_source,
    )
    native_ledger_id = _native_ledger_id(host_identity)
    canonical_conversation_id = bound_conversation_id_for_host(
        paths,
        host_identity=host_identity,
    )
    captured_interaction_ids: list[str] = []
    previous_consulted_source_ids: list[str] = []

    with workspace_lease(paths, f"native-ledger:{native_ledger_id}"):
        for turn_index, native_turn in enumerate(transcript.get("turns", []), start=1):
            if not isinstance(native_turn, dict):
                continue
            native_turn_id = native_turn.get("native_turn_id")
            if not isinstance(native_turn_id, str) or not native_turn_id:
                continue
            user_text = str(native_turn.get("user_text", "")).strip()
            attachments = native_turn.get("attachments", [])
            if not user_text and not attachments:
                continue

            audit = build_tool_use_audit(paths, native_turn.get("function_calls", []))
            continuation_type, reused_previous_evidence = classify_continuation_type(
                turn_index=turn_index,
                user_text=user_text,
                attachments=attachments if isinstance(attachments, list) else [],
                audit=audit,
                previous_consulted_source_ids=previous_consulted_source_ids,
            )
            previous_consulted_source_ids = (
                audit.get("consulted_source_ids", []) or previous_consulted_source_ids
            )
            stored_attachments = [
                _store_attachment(
                    paths,
                    interaction_id=f"interaction-{native_ledger_id}-{native_turn_id}",
                    attachment_index=index,
                    attachment=attachment,
                )
                for index, attachment in enumerate(
                    attachments if isinstance(attachments, list) else [],
                    start=1,
                )
                if isinstance(attachment, dict)
            ]
            related_source_ids = _deduplicate_strings(
                list(audit.get("consulted_source_ids", [])) or previous_consulted_source_ids
            )
            final_assistant_text = str(
                native_turn.get("assistant_final_text") or native_turn.get("assistant_text", "")
            )
            profile = _native_profile(user_text)
            interaction_entry = _persist_interaction_entry(
                paths,
                conversation_id=(
                    canonical_conversation_id
                    if isinstance(canonical_conversation_id, str) and canonical_conversation_id
                    else native_ledger_id
                ),
                turn_id=native_turn_id,
                native_turn_id=native_turn_id,
                recorded_at=str(native_turn.get("opened_at") or utc_now()),
                user_text=user_text,
                assistant_excerpt=final_assistant_text,
                attachment_refs=stored_attachments,
                continuation_type=continuation_type,
                related_source_ids=related_source_ids,
                tool_use_audit=audit,
                question_class=profile["question_class"],
                question_domain=profile["question_domain"],
                support_strategy=profile["support_strategy"],
                analysis_origin=profile["analysis_origin"],
                semantic_analysis=profile["semantic_analysis"],
            )
            captured_interaction_ids.append(str(interaction_entry["interaction_id"]))
            _upsert_native_ledger_turn(
                paths,
                host_identity=host_identity,
                native_turn_id=native_turn_id,
                user_text=user_text,
                assistant_excerpt=final_assistant_text,
                recorded_at=str(native_turn.get("opened_at") or utc_now()),
                completed_at=(
                    str(native_turn.get("completed_at"))
                    if isinstance(native_turn.get("completed_at"), str)
                    else None
                ),
                tool_use_audit=audit,
                related_source_ids=related_source_ids,
                continuation_type=continuation_type,
                reused_previous_evidence=reused_previous_evidence,
                profile=profile,
                attachment_refs=stored_attachments,
                reconciliation={
                    **reconciliation_metadata,
                    "provider": provider,
                    "status": "reconciled",
                    "reconciled_at": utc_now(),
                },
            )

        write_json(
            paths.interaction_reconciliation_state_path,
            {
                "last_reconciled_at": utc_now(),
                "last_host_provider": provider,
                "last_host_thread_ref": host_thread_ref,
                "last_ledger_id": native_ledger_id,
                "captured_interaction_ids": captured_interaction_ids,
                "turn_count": len(transcript.get("turns", [])),
            },
        )
        overlay_manifest = refresh_interaction_overlay(paths)
        refresh_runtime_projections(paths)
    return {
        "status": "reconciled",
        "native_ledger_id": native_ledger_id,
        "native_ledger_path": str(_native_ledger_path(paths, native_ledger_id).relative_to(paths.root)),
        "canonical_conversation_id": canonical_conversation_id,
        "captured_interaction_ids": captured_interaction_ids,
        "turn_count": len(transcript.get("turns", [])),
        "pending_overlay": overlay_manifest,
        "host_identity": host_identity,
    }


def reconcile_codex_thread(
    paths: WorkspacePaths,
    *,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Reconcile a native Codex thread into the native ledger and interaction ingest."""
    refresh_generated_connector_manifests(paths)
    resolved_thread_id = thread_id or os.environ.get("CODEX_THREAD_ID")
    if not isinstance(resolved_thread_id, str) or not resolved_thread_id:
        return {"status": "not-available", "detail": "No native Codex thread id is available."}
    transcript = load_codex_transcript(resolved_thread_id)
    transcript_cwd = transcript.get("cwd")
    if isinstance(transcript_cwd, str) and transcript_cwd:
        try:
            if Path(transcript_cwd).resolve() != paths.root.resolve():
                return {
                    "status": "ignored",
                    "detail": "Native thread cwd does not match the current workspace root.",
                    "native_ledger_id": None,
                }
        except OSError:
            pass
    return _reconcile_native_transcript(
        paths,
        provider="codex",
        host_thread_ref=resolved_thread_id,
        host_identity_source="codex_thread_id",
        transcript=transcript,
        reconciliation_metadata={
            "rollout_path": transcript.get("rollout_path"),
        },
    )


def maybe_reconcile_active_codex_thread(paths: WorkspacePaths) -> dict[str, Any] | None:
    """Reconcile the active Codex thread when the environment exposes one."""
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    try:
        return reconcile_codex_thread(paths, thread_id=thread_id)
    except (FileNotFoundError, KeyError, ValueError):
        return None


def reconcile_claude_code_thread(
    paths: WorkspacePaths,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Reconcile a Claude Code hook-mirror session into native ledger and interaction ingest.

    Reads from the hook-mirror JSONL written by the Claude Code hooks, optionally
    enriched by the native Claude Code transcript when ``transcript_path`` was captured.
    """
    refresh_generated_connector_manifests(paths)

    resolved_session_id = session_id or os.environ.get("CLAUDE_SESSION_ID")
    if not isinstance(resolved_session_id, str) or not resolved_session_id:
        return {"status": "not-available", "detail": "No Claude Code session id is available."}

    try:
        transcript = load_claude_code_transcript(resolved_session_id, paths.root)
    except FileNotFoundError:
        return {
            "status": "not-available",
            "detail": f"No hook-mirror file found for session {resolved_session_id!r}.",
        }

    # Workspace-root check: skip reconciliation when the session belongs to a
    # different workspace (mirrors the Codex cwd check).
    transcript_cwd = transcript.get("cwd")
    if isinstance(transcript_cwd, str) and transcript_cwd:
        try:
            if Path(transcript_cwd).resolve() != paths.root.resolve():
                return {
                    "status": "ignored",
                    "detail": "Hook-mirror session cwd does not match the current workspace root.",
                    "native_ledger_id": None,
                }
        except OSError:
            pass
    fidelity = transcript.get("fidelity", {})
    return _reconcile_native_transcript(
        paths,
        provider="claude-code",
        host_thread_ref=resolved_session_id,
        host_identity_source="claude_code_session_id",
        transcript=transcript,
        reconciliation_metadata={
            "capture_method": fidelity.get("capture_method", "hook-mirror"),
            "attachments_captured": fidelity.get(
                "attachments_captured",
                fidelity.get("has_attachments", False),
            ),
            "has_mid_turn_messages": fidelity.get("has_mid_turn_messages", False),
        },
    )


def maybe_reconcile_active_claude_code_thread(
    paths: WorkspacePaths,
) -> dict[str, Any] | None:
    """Reconcile the active Claude Code session when the environment exposes one."""
    session_id = os.environ.get("CLAUDE_SESSION_ID")
    if not isinstance(session_id, str) or not session_id:
        return None
    try:
        return reconcile_claude_code_thread(paths, session_id=session_id)
    except (FileNotFoundError, KeyError, ValueError):
        return None


def maybe_reconcile_active_thread(paths: WorkspacePaths) -> dict[str, Any] | None:
    """Provider-agnostic dispatch: reconcile whichever native thread is active.

    Detects the current agent surface from the environment and delegates to the
    matching provider-specific reconciler.  Returns ``None`` when no native
    thread is detected.
    """
    from .conversation import detect_agent_surface

    surface = detect_agent_surface()
    if surface == "codex":
        return maybe_reconcile_active_codex_thread(paths)
    if surface == "claude-code":
        return maybe_reconcile_active_claude_code_thread(paths)
    # Unknown surface — try both in priority order.
    result = maybe_reconcile_active_codex_thread(paths)
    if result is not None:
        return result
    return maybe_reconcile_active_claude_code_thread(paths)


def _memory_title(entries: list[dict[str, Any]], conversation_id: str) -> str:
    first_title = next(
        (
            str(entry.get("title"))
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("title"), str)
            and entry.get("title")
        ),
        "",
    )
    if first_title:
        return f"Interaction Memory: {first_title[:72]}"
    return f"Interaction Memory for {conversation_id}"


def _memory_summary(entries: list[dict[str, Any]]) -> str:
    fragments: list[str] = []
    for entry in entries[:5]:
        if not isinstance(entry, dict):
            continue
        user_text = " ".join(str(entry.get("user_text", "")).split())
        if user_text:
            fragments.append(user_text[:220])
    return "\n".join(f"- {fragment}" for fragment in fragments if fragment)


def _memory_language(entries: list[dict[str, Any]]) -> str:
    return _detect_language(
        "\n".join(
            str(entry.get("searchable_text", ""))
            for entry in entries
            if isinstance(entry, dict)
        )
    )


def _memory_semantics(entries: list[dict[str, Any]]) -> dict[str, str]:
    selected_kind = "working-note"
    selected_priority = MEMORY_KIND_PRIORITY[selected_kind]
    uncertainty = "confirmed"
    answer_use_policy = "contextual-only"
    retrieval_rank_prior = "low"
    durability = "ephemeral"
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        memory_kind = str(entry.get("memory_kind") or "working-note")
        priority = MEMORY_KIND_PRIORITY.get(memory_kind, 99)
        if priority < selected_priority:
            selected_kind = memory_kind
            selected_priority = priority
            durability = str(entry.get("durability") or durability)
            answer_use_policy = str(entry.get("answer_use_policy") or answer_use_policy)
            retrieval_rank_prior = str(entry.get("retrieval_rank_prior") or retrieval_rank_prior)
        entry_uncertainty = str(entry.get("uncertainty") or "confirmed")
        if entry_uncertainty == "stated-uncertain":
            uncertainty = "stated-uncertain"
        elif uncertainty != "stated-uncertain" and entry_uncertainty == "inferred":
            uncertainty = "inferred"
    return {
        "memory_kind": selected_kind,
        "durability": durability,
        "uncertainty": uncertainty,
        "answer_use_policy": answer_use_policy,
        "retrieval_rank_prior": retrieval_rank_prior,
    }


def _group_entries_for_promotion(entries: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        conversation_id = entry.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            continue
        groups.setdefault(conversation_id, []).append(entry)
    return groups


def _copy_attachment_into_memory(
    paths: WorkspacePaths, memory_dir: Path, attachment: dict[str, Any]
) -> str | None:
    stored_path = attachment.get("stored_path")
    if not isinstance(stored_path, str) or not stored_path:
        return None
    source_path = Path(stored_path)
    if not source_path.is_absolute():
        source_path = paths.root / stored_path
    if not source_path.exists():
        return None
    target_dir = memory_dir / "assets"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / source_path.name
    if not target_path.exists():
        target_path.write_bytes(source_path.read_bytes())
    return str(Path("assets") / target_path.name)


def _create_interaction_memory_work_item(
    memory_dir: Path,
    *,
    memory_id: str,
    conversation_id: str,
    interaction_ids: list[str],
    semantics: dict[str, str],
) -> None:
    write_json(
        memory_dir / "work_item.json",
        {
            "source_id": memory_id,
            "conversation_id": conversation_id,
            "interaction_ids": interaction_ids,
            "knowledge_path": "knowledge.json",
            "summary_path": "summary.md",
            "affordance_path": DEFAULT_AFFORDANCE_FILENAME,
            "context_path": "interaction_context.json",
            "required_knowledge_keys": list(INTERACTION_REQUIRED_KNOWLEDGE_KEYS),
            "semantic_hints": semantics,
            "summary_contract": [
                "Use `# <title>` as the first line.",
                "Include `## English Summary` and `## Source-Language Summary` sections.",
                "Mention the source ID in the body.",
                (
                    "Preserve explicit provenance to the contributing interaction turns "
                    "and attachments."
                ),
            ],
            "affordance_contract": [
                "Preserve KB-native published evidence channels for interaction odd questions.",
                "Keep channel_descriptors compact, interaction-backed, and grouped by channel.",
                "Do not relabel derived descriptors as source-authored facts.",
            ],
        },
    )


def _memory_semantics_fallback_text(
    *,
    interaction_context: dict[str, Any],
    knowledge: dict[str, Any] | None = None,
    summary_text: str = "",
) -> str:
    parts: list[str] = []
    if isinstance(knowledge, dict):
        for field_name in ("title", "summary_en", "summary_source"):
            value = knowledge.get(field_name)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    suggested_title = interaction_context.get("suggested_title")
    if isinstance(suggested_title, str) and suggested_title.strip():
        parts.append(suggested_title.strip())
    suggested_points = interaction_context.get("suggested_summary_points", [])
    if isinstance(suggested_points, list):
        parts.extend(
            point.strip()
            for point in suggested_points
            if isinstance(point, str) and point.strip()
        )
    if summary_text.strip():
        parts.append(summary_text.strip())
    return "\n".join(parts)


def _memory_kind_from_relation_types(relation_types: list[str]) -> str | None:
    if "corrects-source" in relation_types:
        return "correction"
    if "constraint-for" in relation_types:
        return "constraint"
    if "clarifies-source" in relation_types or "visual-reference-for" in relation_types:
        return "clarification"
    if "extends-source" in relation_types:
        return "working-note"
    return None


def _interaction_context_semantic_hints(
    paths: WorkspacePaths,
    *,
    interaction_context: dict[str, Any],
    source_manifest: dict[str, Any],
) -> dict[str, str]:
    relation_types = [
        str(item.get("relation_type"))
        for item in interaction_context.get("related_sources", [])
        if isinstance(item, dict) and isinstance(item.get("relation_type"), str)
    ]
    interaction_ids = source_manifest.get("interaction_ids", [])
    if isinstance(interaction_ids, list):
        fallback_memory_kind: str | None = None
        for interaction_id in interaction_ids:
            if not isinstance(interaction_id, str) or not interaction_id:
                continue
            entry = read_json(_interaction_entry_path(paths, interaction_id))
            continuation_type = entry.get("continuation_type")
            if continuation_type == "constraint-update":
                return {"memory_kind": "constraint"}
            if continuation_type == "mixed":
                fallback_memory_kind = "clarification"
        if fallback_memory_kind is not None:
            return {"memory_kind": fallback_memory_kind}
    memory_kind = _memory_kind_from_relation_types(relation_types)
    return {"memory_kind": memory_kind} if memory_kind else {}


def _apply_memory_semantics_to_directory(memory_dir: Path, *, semantics: dict[str, str]) -> None:
    for filename in ("source_manifest.json", "knowledge.json"):
        path = memory_dir / filename
        payload = read_json(path)
        if not payload:
            continue
        changed = False
        for field_name, value in semantics.items():
            if payload.get(field_name) != value:
                payload[field_name] = value
                changed = True
        if changed:
            write_json(path, payload)

    interaction_context_path = memory_dir / "interaction_context.json"
    interaction_context = read_json(interaction_context_path)
    if interaction_context and interaction_context.get("semantics") != semantics:
        interaction_context["semantics"] = semantics
        write_json(interaction_context_path, interaction_context)

    work_item_path = memory_dir / "work_item.json"
    work_item = read_json(work_item_path)
    if work_item and work_item.get("semantic_hints") != semantics:
        work_item["semantic_hints"] = semantics
        write_json(work_item_path, work_item)


def _existing_interaction_memory_lookup(
    paths: WorkspacePaths, *, target: str
) -> dict[str, dict[str, bytes]]:
    lookup: dict[str, dict[str, bytes]] = {}
    for candidate_target in (target, "current"):
        memories_dir = paths.interaction_memories_dir(candidate_target)
        if not memories_dir.exists():
            continue
        for memory_dir in sorted(path for path in memories_dir.iterdir() if path.is_dir()):
            if memory_dir.name in lookup:
                continue
            payload: dict[str, bytes] = {}
            for filename in ("knowledge.json", "summary.md", DEFAULT_AFFORDANCE_FILENAME):
                source = memory_dir / filename
                if source.exists():
                    payload[filename] = source.read_bytes()
            if payload:
                lookup[memory_dir.name] = payload
    return lookup


def _existing_interaction_memory_dir_lookup(
    paths: WorkspacePaths, *, target: str
) -> dict[str, Path]:
    lookup: dict[str, Path] = {}
    for candidate_target in ("current",):
        memories_dir = paths.interaction_memories_dir(candidate_target)
        if not memories_dir.exists():
            continue
        for memory_dir in sorted(path for path in memories_dir.iterdir() if path.is_dir()):
            lookup.setdefault(memory_dir.name, memory_dir)
    return lookup


def _existing_interaction_manifest_lookup(
    paths: WorkspacePaths, *, target: str
) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for candidate_target in (target, "current"):
        manifest = read_json(paths.interaction_manifest_path(candidate_target))
        memories = manifest.get("memories", [])
        if not isinstance(memories, list):
            continue
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            memory_id = memory.get("memory_id")
            if isinstance(memory_id, str) and memory_id:
                lookup.setdefault(memory_id, memory)
    return lookup


def _preserve_interaction_semantic_outputs(
    previous_payload: dict[str, bytes], memory_dir: Path
) -> None:
    for filename, data in previous_payload.items():
        (memory_dir / filename).write_bytes(data)


def _filter_related_source_records(
    related_sources: list[dict[str, Any]],
    *,
    active_source_ids: set[str] | None,
) -> tuple[list[dict[str, Any]], int]:
    """Keep only related-source records that still point at active staged corpus sources."""
    if not active_source_ids:
        return related_sources, 0
    filtered: list[dict[str, Any]] = []
    pruned = 0
    for related in related_sources:
        if not isinstance(related, dict):
            continue
        related_source_id = related.get("source_id")
        if not isinstance(related_source_id, str) or related_source_id not in active_source_ids:
            pruned += 1
            continue
        filtered.append(related)
    return filtered, pruned


def repair_interaction_memory_related_sources(
    memory_dir: Path,
    *,
    active_source_ids: set[str] | None,
) -> dict[str, int]:
    """Prune deleted-source references from one promoted interaction memory directory."""
    if not active_source_ids:
        return {"knowledge_pruned": 0, "context_pruned": 0}

    knowledge_path = memory_dir / "knowledge.json"
    knowledge = read_json(knowledge_path)
    knowledge_pruned = 0
    if knowledge:
        filtered_related_sources, knowledge_pruned = _filter_related_source_records(
            [
                related
                for related in knowledge.get("related_sources", [])
                if isinstance(related, dict)
            ],
            active_source_ids=active_source_ids,
        )
        if knowledge.get("related_sources") != filtered_related_sources:
            knowledge["related_sources"] = filtered_related_sources
            write_json(knowledge_path, knowledge)

    interaction_context_path = memory_dir / "interaction_context.json"
    interaction_context = read_json(interaction_context_path)
    context_pruned = 0
    if interaction_context:
        filtered_related_sources, context_pruned = _filter_related_source_records(
            [
                related
                for related in interaction_context.get("related_sources", [])
                if isinstance(related, dict)
            ],
            active_source_ids=active_source_ids,
        )
        if interaction_context.get("related_sources") != filtered_related_sources:
            interaction_context["related_sources"] = filtered_related_sources
            write_json(interaction_context_path, interaction_context)

    return {
        "knowledge_pruned": knowledge_pruned,
        "context_pruned": context_pruned,
    }


def _write_interaction_memory_affordances(memory_dir: Path) -> None:
    source_manifest = read_json(memory_dir / "source_manifest.json")
    evidence_manifest = read_json(memory_dir / "evidence_manifest.json")
    if not source_manifest or not evidence_manifest:
        return
    knowledge = read_json(memory_dir / "knowledge.json")
    summary_path = memory_dir / "summary.md"
    summary_text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    baseline = derive_source_affordances(
        source_manifest=source_manifest,
        evidence_manifest=evidence_manifest,
        source_dir=memory_dir,
        knowledge=knowledge or None,
        summary_text=summary_text,
    )
    affordance_path = memory_dir / DEFAULT_AFFORDANCE_FILENAME
    merged = merge_derived_affordances(baseline, read_json(affordance_path))
    write_json(affordance_path, merged)


def build_promoted_interaction_memories(
    paths: WorkspacePaths,
    *,
    target: str,
    active_source_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Build merged interaction memories for sync-time publication."""
    interaction_dir = paths.interaction_target_dir(target)
    memories_dir = paths.interaction_memories_dir(target)
    previous_lookup = _existing_interaction_memory_lookup(paths, target=target)
    previous_dir_lookup = _existing_interaction_memory_dir_lookup(paths, target=target)
    if interaction_dir.exists():
        for child in interaction_dir.iterdir():
            if child.is_dir():
                import shutil

                shutil.rmtree(child)
            else:
                child.unlink()
    memories_dir.mkdir(parents=True, exist_ok=True)

    entries = pending_interaction_entries(paths)
    grouped = _group_entries_for_promotion(entries)
    memories: list[dict[str, Any]] = []
    built_memory_ids: set[str] = set()
    for conversation_id, grouped_entries in grouped.items():
        grouped_entries = sorted(
            grouped_entries,
            key=lambda item: str(item.get("recorded_at") or ""),
        )
        memory_key = hashlib.sha256(
            "\n".join(str(entry.get("interaction_id")) for entry in grouped_entries).encode("utf-8")
        ).hexdigest()[:12]
        memory_id = f"interaction-memory-{memory_key}"
        memory_dir = memories_dir / memory_id
        extracted_dir = memory_dir / "extracted"
        extracted_dir.mkdir(parents=True, exist_ok=True)
        all_related_source_ids = _deduplicate_strings(
            [
                related_source_id
                for entry in grouped_entries
                for related_source_id in entry.get("related_source_ids", [])
                if isinstance(related_source_id, str)
            ]
        )
        if active_source_ids:
            all_related_source_ids = [
                source_id for source_id in all_related_source_ids if source_id in active_source_ids
            ]
        title = _memory_title(grouped_entries, conversation_id)
        summary_md = _memory_summary(grouped_entries)
        source_language = _memory_language(grouped_entries)
        semantics = normalize_memory_semantics(
            _memory_semantics(grouped_entries),
            fallback_text="\n".join(
                str(entry.get("user_text", "")).strip()
                for entry in grouped_entries
                if isinstance(entry, dict)
            ),
        )
        attachment_assets: list[str] = []
        units: list[dict[str, Any]] = []
        for index, entry in enumerate(grouped_entries, start=1):
            unit_id = f"turn-{index:03d}"
            text_asset = Path("extracted") / f"{unit_id}.txt"
            structure_asset = Path("extracted") / f"{unit_id}.json"
            entry_text = "\n\n".join(
                [
                    str(entry.get("user_text", "")).strip(),
                    str(entry.get("assistant_excerpt", "")).strip(),
                ]
            ).strip()
            (memory_dir / text_asset).write_text(
                entry_text + ("\n" if entry_text else ""), encoding="utf-8"
            )
            memory_attachments: list[str] = []
            for attachment in entry.get("attachment_refs", []):
                if not isinstance(attachment, dict):
                    continue
                copied = _copy_attachment_into_memory(paths, memory_dir, attachment)
                if copied:
                    memory_attachments.append(copied)
                    attachment_assets.append(copied)
            structure_payload = {
                "interaction_id": entry.get("interaction_id"),
                "conversation_id": entry.get("conversation_id"),
                "turn_id": entry.get("turn_id"),
                "native_turn_id": entry.get("native_turn_id"),
                "continuation_type": entry.get("continuation_type"),
                "related_source_ids": entry.get("related_source_ids", []),
                "attachments": memory_attachments,
            }
            write_json(memory_dir / structure_asset, structure_payload)
            units.append(
                {
                    "unit_id": unit_id,
                    "unit_type": "interaction-turn",
                    "ordinal": index,
                    "title": f"Conversation turn {index}",
                    "rendered_asset": memory_attachments[0]
                    if len(memory_attachments) == 1
                    else None,
                    "render_reference_ids": [Path(asset).stem for asset in memory_attachments],
                    "text_asset": str(text_asset),
                    "structure_asset": str(structure_asset),
                    "embedded_media": memory_attachments,
                    "extraction_confidence": "medium",
                    "trust_prior_inputs": {
                        "conversation_id": conversation_id,
                        "interaction_id": entry.get("interaction_id"),
                    },
                }
            )

        memory_fingerprint = _sha256_text(
            json.dumps([entry.get("interaction_id") for entry in grouped_entries], sort_keys=True)
        )
        source_manifest = {
            "source_id": memory_id,
            "current_path": f"interaction/{memory_id}",
            "prior_paths": [],
            "path_history": [f"interaction/{memory_id}"],
            "relative_path_lineage": ["interaction"],
            "document_type": "interaction",
            "source_fingerprint": memory_fingerprint,
            "file_size": 0,
            "modified_at": max(str(entry.get("recorded_at") or "") for entry in grouped_entries),
            "first_seen_at": min(str(entry.get("recorded_at") or "") for entry in grouped_entries),
            "last_seen_at": max(str(entry.get("recorded_at") or "") for entry in grouped_entries),
            "identity_confidence": "interaction-merge",
            "identity_basis": "interaction-group",
            "change_classification": "generated",
            "trust_prior": {
                "first_level_subtree": "interaction",
                "local_branch_depth": 1,
                "relative_path_lineage": ["interaction"],
                "modified_at": max(
                    str(entry.get("recorded_at") or "") for entry in grouped_entries
                ),
                "graph_centrality": None,
                "corroboration": None,
            },
            "render_strategy": "runtime-attachments",
            "staging_generated_at": utc_now(),
            "source_family": "interaction-memory",
            "trust_tier": "interaction",
            "memory_kind": semantics["memory_kind"],
            "durability": semantics["durability"],
            "uncertainty": semantics["uncertainty"],
            "answer_use_policy": semantics["answer_use_policy"],
            "retrieval_rank_prior": semantics["retrieval_rank_prior"],
            "conversation_ids": [conversation_id],
            "interaction_ids": [entry.get("interaction_id") for entry in grouped_entries],
        }
        evidence_manifest = {
            "source_id": memory_id,
            "document_type": "interaction",
            "source_fingerprint": memory_fingerprint,
            "generated_at": utc_now(),
            "rendering": {
                "renderer": "runtime-attachment-copy",
                "status": "ready" if attachment_assets else "text-only",
            },
            "document_renders": _deduplicate_strings(attachment_assets),
            "units": units,
            "failures": [],
            "structure_assets": [unit["structure_asset"] for unit in units],
            "embedded_media": _deduplicate_strings(attachment_assets),
            "language_candidates": [source_language],
        }
        related_sources = []
        for related_source_id in all_related_source_ids:
            relation_candidates: list[str] = [
                str(hint.get("relation_type"))
                for entry in grouped_entries
                for hint in entry.get("relation_hints", [])
                if isinstance(hint, dict)
                and hint.get("related_source_id") == related_source_id
                and isinstance(hint.get("relation_type"), str)
            ]
            relation_type = sorted(
                relation_candidates or ["derived-from-turn"],
                key=lambda value: RELATION_PRIORITY.get(value, 99),
            )[0]
            related_sources.append(
                {
                    "source_id": related_source_id,
                    "relation_type": relation_type,
                    "strength": "medium",
                    "status": "supported",
                    "citation_unit_ids": [unit["unit_id"] for unit in units[:3]],
                }
            )
        write_json(memory_dir / "source_manifest.json", source_manifest)
        write_json(memory_dir / "evidence_manifest.json", evidence_manifest)
        write_json(
            memory_dir / "interaction_context.json",
            {
                "memory_id": memory_id,
                "conversation_id": conversation_id,
                "interaction_ids": [entry.get("interaction_id") for entry in grouped_entries],
                "source_language": source_language,
                "semantics": semantics,
                "suggested_title": title,
                "suggested_summary_points": summary_md.splitlines(),
                "related_sources": related_sources,
            },
        )
        _create_interaction_memory_work_item(
            memory_dir,
            memory_id=memory_id,
            conversation_id=conversation_id,
            interaction_ids=[
                str(entry.get("interaction_id"))
                for entry in grouped_entries
                if isinstance(entry.get("interaction_id"), str)
            ],
            semantics=semantics,
        )
        previous_payload = previous_lookup.get(memory_id)
        if previous_payload is not None:
            _preserve_interaction_semantic_outputs(previous_payload, memory_dir)
        repair_interaction_memory_related_sources(
            memory_dir,
            active_source_ids=active_source_ids,
        )
        _apply_memory_semantics_to_directory(memory_dir, semantics=semantics)
        _write_interaction_memory_affordances(memory_dir)
        memories.append(
            {
                "memory_id": memory_id,
                "conversation_id": conversation_id,
                "entry_count": len(grouped_entries),
                "interaction_ids": [entry.get("interaction_id") for entry in grouped_entries],
                "related_source_ids": all_related_source_ids,
                "memory_kind": semantics["memory_kind"],
                "durability": semantics["durability"],
                "uncertainty": semantics["uncertainty"],
                "has_semantic_outputs": (memory_dir / "knowledge.json").exists()
                and (memory_dir / "summary.md").exists(),
            }
        )
        built_memory_ids.add(memory_id)

    for memory_id, previous_dir in previous_dir_lookup.items():
        if memory_id in built_memory_ids:
            continue
        target_dir = memories_dir / memory_id
        import shutil

        shutil.copytree(previous_dir, target_dir)
        source_manifest = read_json(target_dir / "source_manifest.json")
        conversation_ids = source_manifest.get("conversation_ids", [])
        if not isinstance(conversation_ids, list):
            conversation_ids = []
        interaction_ids = source_manifest.get("interaction_ids", [])
        if not isinstance(interaction_ids, list):
            interaction_ids = []
        interaction_context = read_json(target_dir / "interaction_context.json")
        knowledge = read_json(target_dir / "knowledge.json")
        summary_path = target_dir / "summary.md"
        summary_text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        repair_interaction_memory_related_sources(
            target_dir,
            active_source_ids=active_source_ids,
        )
        interaction_context = read_json(target_dir / "interaction_context.json")
        knowledge = read_json(target_dir / "knowledge.json")
        related_sources = interaction_context.get("related_sources", [])
        if not isinstance(related_sources, list):
            related_sources = []
        related_source_ids = _deduplicate_strings(
            [
                str(item.get("source_id"))
                for item in related_sources
                if isinstance(item, dict) and isinstance(item.get("source_id"), str)
            ]
        )
        semantics = normalize_memory_semantics(
            interaction_context.get("semantics"),
            fallback_text=_memory_semantics_fallback_text(
                interaction_context=interaction_context,
                knowledge=knowledge,
                summary_text=summary_text,
            ),
            semantic_hints=_interaction_context_semantic_hints(
                paths,
                interaction_context=interaction_context,
                source_manifest=source_manifest,
            ),
        )
        _apply_memory_semantics_to_directory(target_dir, semantics=semantics)
        _write_interaction_memory_affordances(target_dir)
        memories.append(
            {
                "memory_id": memory_id,
                "conversation_id": conversation_ids[0] if conversation_ids else None,
                "entry_count": len(
                    [
                        interaction_id
                        for interaction_id in interaction_ids
                        if isinstance(interaction_id, str)
                    ]
                ),
                "interaction_ids": interaction_ids,
                "related_source_ids": related_source_ids,
                "memory_kind": semantics["memory_kind"],
                "durability": semantics["durability"],
                "uncertainty": semantics["uncertainty"],
                "has_semantic_outputs": (target_dir / "knowledge.json").exists()
                and (target_dir / "summary.md").exists(),
            }
        )

    manifest = {
        "generated_at": utc_now(),
        "memory_count": len(memories),
        "pending_memory_count": sum(1 for memory in memories if not memory["has_semantic_outputs"]),
        "pending_entry_count": len(entries),
        "memories": memories,
    }
    write_json(paths.interaction_manifest_path(target), manifest)
    return manifest


def mark_promoted_interaction_entries(paths: WorkspacePaths, *, target: str) -> dict[str, Any]:
    """Mark pending runtime entries as promoted after a successful publish."""
    manifest = read_json(paths.interaction_manifest_path(target))
    promoted_count = 0
    for memory in manifest.get("memories", []):
        if not isinstance(memory, dict):
            continue
        memory_id = memory.get("memory_id")
        interaction_ids = memory.get("interaction_ids", [])
        if not isinstance(memory_id, str) or not isinstance(interaction_ids, list):
            continue
        for interaction_id in interaction_ids:
            if not isinstance(interaction_id, str):
                continue
            entry_path = _interaction_entry_path(paths, interaction_id)
            entry = read_json(entry_path)
            if not entry:
                continue
            entry["pending_promotion"] = False
            entry["status"] = "promoted"
            entry["promoted_memory_id"] = memory_id
            entry["promoted_at"] = utc_now()
            write_json(entry_path, entry)
            promoted_count += 1
    overlay_manifest = refresh_interaction_overlay(paths)
    return {
        "promoted_entry_count": promoted_count,
        "pending_overlay": overlay_manifest,
    }


def iter_promoted_interaction_dirs(paths: WorkspacePaths, *, target: str) -> list[Path]:
    """Return all promoted interaction memory directories for one KB target."""
    memories_dir = paths.interaction_memories_dir(target)
    if not memories_dir.exists():
        return []
    return sorted(path for path in memories_dir.iterdir() if path.is_dir())


def load_promoted_interaction_contexts(
    paths: WorkspacePaths, *, target: str
) -> list[dict[str, Any]]:
    """Load promoted interaction memories as retrieval or trace source contexts."""
    contexts: list[dict[str, Any]] = []
    for memory_dir in iter_promoted_interaction_dirs(paths, target=target):
        source_manifest = read_json(memory_dir / "source_manifest.json")
        evidence_manifest = read_json(memory_dir / "evidence_manifest.json")
        knowledge = read_json(memory_dir / "knowledge.json")
        interaction_context = read_json(memory_dir / "interaction_context.json")
        summary_path = memory_dir / "summary.md"
        summary_text = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
        if (
            not source_manifest
            or not evidence_manifest
            or not knowledge
            or not summary_text.strip()
        ):
            continue
        semantics = normalize_memory_semantics(
            interaction_context.get("semantics"),
            fallback_text=_memory_semantics_fallback_text(
                interaction_context=interaction_context,
                knowledge=knowledge,
                summary_text=summary_text,
            ),
            semantic_hints=_interaction_context_semantic_hints(
                paths,
                interaction_context=interaction_context,
                source_manifest=source_manifest,
            ),
        )
        for field_name, value in semantics.items():
            knowledge[field_name] = value
        contexts.append(
            {
                "source_manifest": source_manifest,
                "evidence_manifest": evidence_manifest,
                "knowledge": knowledge,
                "summary_text": summary_text,
                "artifact_dir": memory_dir,
                "source_family": "interaction-memory",
                "trust_tier": "interaction",
            }
        )
    return contexts

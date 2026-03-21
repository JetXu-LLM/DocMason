"""Phase 4 retrieval, trace, and structured query logging helpers."""

from __future__ import annotations

import json
import os
import re
import uuid
from collections import Counter, defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .affordances import (
    DEFAULT_AFFORDANCE_FILENAME,
    available_channels_from_record,
    channel_descriptors_from_record,
    confidence_from_record,
    flatten_channel_descriptors,
    normalize_evidence_requirements,
    plan_published_evidence,
    support_channels_from_supports,
)
from .contracts import ANSWER_STATES
from .conversation import LOG_CONTEXT_FIELD_NAMES, semantic_log_context_from_record
from .front_controller import load_support_manifest
from .interaction import load_interaction_overlay
from .project import WorkspacePaths, append_jsonl, read_json, write_json
from .projections import refresh_runtime_projections
from .routing import infer_memory_query_profile, normalize_memory_semantics
from .source_references import (
    build_reference_resolution_summary,
    normalize_source_record_reference,
    normalize_unit_record_reference,
    resolve_reference_query,
)

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?。！？])\s+")
FIELD_WEIGHTS = {
    "title": 5.0,
    "entities": 4.0,
    "summary": 3.0,
    "claims": 2.5,
    "key_points": 2.5,
    "affordance": 2.0,
    "path": 1.5,
    "unit_title": 2.0,
    "unit_text": 1.2,
    "unit_affordance": 1.1,
}
GRAPH_STRENGTH_WEIGHTS = {"high": 1.5, "medium": 0.8}
MEMORY_RANK_PRIOR_BONUS = {"high": 1.2, "medium": 0.5, "low": 0.0}
CHANNEL_PREFERENCE_BONUS = {"source": 0.35, "unit": 0.25}
RETRIEVAL_STRATEGY_ID = "phase4b-lexical-plus-graph-v1"
ANSWER_WORKFLOW_ID = "phase4b-grounded-answer-v1"
ABSTENTION_MARKERS = (
    "i cannot answer",
    "i can't answer",
    "i cannot determine",
    "i can't determine",
    "i do not have enough evidence",
    "i don't have enough evidence",
    "insufficient evidence",
    "cannot verify",
    "无法回答",
    "无法确定",
    "证据不足",
    "不能确认",
    "不能判断",
)
GROUNDING_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "by",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "there",
    "these",
    "this",
    "those",
    "to",
    "was",
    "were",
    "with",
}


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def tokenize_text(text: str) -> list[str]:
    """Return normalized lexical tokens for retrieval and trace matching."""
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def normalize_filename_stem(value: str) -> str:
    """Normalize a filename stem for relocation heuristics."""
    return " ".join(tokenize_text(value))


def safe_read_text(path: Path) -> str:
    """Read UTF-8 text when available and return an empty string otherwise."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def citations_from_knowledge(knowledge: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect all citation objects from a knowledge payload."""
    citations: list[dict[str, Any]] = []
    if isinstance(knowledge.get("citations"), list):
        citations.extend(item for item in knowledge["citations"] if isinstance(item, dict))
    for key in ("key_points", "claims", "ambiguities"):
        items = knowledge.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("citations"), list):
                citations.extend(
                    citation for citation in item["citations"] if isinstance(citation, dict)
                )
    return citations


def citation_density(citation_count: int, unit_count: int) -> float:
    """Return a bounded citation-density value for deterministic ranking."""
    if unit_count <= 0:
        return 0.0
    return min(citation_count / unit_count, 3.0)


def trust_prior_bonus(trust_prior: dict[str, Any]) -> float:
    """Return a small deterministic bonus from path-derived trust inputs."""
    local_branch_depth = trust_prior.get("local_branch_depth")
    if not isinstance(local_branch_depth, int):
        return 0.0
    return max(0.0, 0.3 - (0.05 * local_branch_depth))


def confidence_bonus(value: str | None) -> float:
    """Return a ranking bonus for extraction confidence labels."""
    if value == "high":
        return 0.4
    if value == "medium":
        return 0.2
    if value == "low":
        return -0.1
    return 0.0


def render_references_from_unit(unit: dict[str, Any]) -> list[str]:
    """Return the render references attached to a unit in normalized form."""
    refs: list[str] = []
    rendered_asset = unit.get("rendered_asset")
    if isinstance(rendered_asset, str) and rendered_asset:
        refs.append(rendered_asset)
    render_reference_ids = unit.get("render_reference_ids", [])
    if isinstance(render_reference_ids, list):
        refs.extend(value for value in render_reference_ids if isinstance(value, str))
    return list(dict.fromkeys(refs))


def summarized_consumer_text(consumer: dict[str, Any]) -> str:
    """Return a short human-readable text for a citation consumer."""
    consumer_type = consumer.get("consumer_type")
    if consumer_type == "summary":
        return "Document summary"
    if consumer_type == "top-level-citation":
        return "Top-level citation list"
    for key in ("text_en", "statement_en", "text_source", "statement_source"):
        value = consumer.get(key)
        if isinstance(value, str) and value:
            return value
    return str(consumer_type or "knowledge consumer")

def ensure_log_directories(paths: WorkspacePaths) -> None:
    """Create the Phase 4 private log directories."""
    for directory in (paths.query_sessions_dir, paths.retrieval_traces_dir):
        directory.mkdir(parents=True, exist_ok=True)


def _unit_affordance_lookup(context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    affordances = context.get("affordances", {})
    if not isinstance(affordances, dict):
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for item in affordances.get("unit_affordances", []):
        if not isinstance(item, dict) or not isinstance(item.get("unit_id"), str):
            continue
        lookup[item["unit_id"]] = item
    return lookup


def _source_affordance_metadata(
    context: dict[str, Any],
) -> tuple[list[str], dict[str, list[str]], str, str]:
    affordances = context.get("affordances", {})
    if not isinstance(affordances, dict):
        affordances = {}
    source_affordances = affordances.get("source_affordances", {})
    if not isinstance(source_affordances, dict):
        source_affordances = {}
    return (
        available_channels_from_record(source_affordances),
        channel_descriptors_from_record(source_affordances),
        str(affordances.get("confidence") or "medium"),
        str(affordances.get("derivation_mode") or "deterministic"),
    )


def _channel_preference_bonus(
    available_channels: list[str],
    *,
    preferred_channels: list[str],
    scope: str,
) -> float:
    if not preferred_channels:
        return 0.0
    matched = len(set(available_channels) & set(preferred_channels))
    if matched <= 0:
        return 0.0
    return CHANNEL_PREFERENCE_BONUS[scope] * matched


def build_retrieval_artifacts(
    paths: WorkspacePaths,
    *,
    target: str,
    source_contexts: list[dict[str, Any]],
    graph_edges: list[dict[str, Any]],
    source_signature: str | None,
) -> dict[str, Any]:
    """Build and persist retrieval artifacts for a validated knowledge-base target."""
    retrieval_dir = paths.retrieval_dir(target)
    retrieval_dir.mkdir(parents=True, exist_ok=True)

    source_records: list[dict[str, Any]] = []
    unit_records: list[dict[str, Any]] = []

    for context in source_contexts:
        source_manifest = context["source_manifest"]
        evidence_manifest = context["evidence_manifest"]
        knowledge = context["knowledge"]
        summary_text = context["summary_text"]
        (
            available_channels,
            channel_descriptors,
            affordance_confidence,
            affordance_derivation_mode,
        ) = _source_affordance_metadata(context)
        affordance_lookup = _unit_affordance_lookup(context)
        source_family = str(context.get("source_family", "corpus"))
        trust_tier = str(context.get("trust_tier", "source"))
        pending_promotion = bool(context.get("pending_promotion", False))
        artifact_dir = context.get("artifact_dir")
        if isinstance(artifact_dir, Path):
            source_dir = artifact_dir
        elif isinstance(artifact_dir, str):
            source_dir = Path(artifact_dir)
        else:
            source_dir = (
                paths.knowledge_target_dir(target) / "sources" / source_manifest["source_id"]
            )
        citation_count = len(citations_from_knowledge(knowledge))
        unit_count = len(
            [
                unit
                for unit in evidence_manifest.get("units", [])
                if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str)
            ]
        )
        density = citation_density(citation_count, unit_count)
        entities = [
            entity.get("name")
            for entity in knowledge.get("entities", [])
            if isinstance(entity, dict) and isinstance(entity.get("name"), str)
        ]
        key_points = [
            item.get("text_en") or item.get("text_source") or ""
            for item in knowledge.get("key_points", [])
            if isinstance(item, dict)
        ]
        claims = [
            item.get("statement_en") or item.get("statement_source") or ""
            for item in knowledge.get("claims", [])
            if isinstance(item, dict)
        ]
        related_sources = [
            item.get("source_id")
            for item in knowledge.get("related_sources", [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        top_citation_unit_ids = [
            citation.get("unit_id")
            for citation in knowledge.get("citations", [])
            if isinstance(citation, dict) and isinstance(citation.get("unit_id"), str)
        ]
        source_warnings = [
            warning
            for warning in evidence_manifest.get("warnings", [])
            if isinstance(warning, str) and warning.strip()
        ]
        source_record = {
            "source_id": source_manifest["source_id"],
            "source_fingerprint": source_manifest["source_fingerprint"],
            "current_path": source_manifest["current_path"],
            "prior_paths": source_manifest.get("prior_paths", []),
            "path_history": source_manifest.get("path_history", []),
            "document_type": source_manifest["document_type"],
            "support_tier": source_manifest.get("support_tier"),
            "source_extension": source_manifest.get("source_extension"),
            "source_origin": source_manifest.get("source_origin", "original-document"),
            "parent_source_id": source_manifest.get("parent_source_id"),
            "root_email_source_id": source_manifest.get("root_email_source_id"),
            "attachment_filename": source_manifest.get("attachment_filename"),
            "attachment_mime_type": source_manifest.get("attachment_mime_type"),
            "attachment_depth": source_manifest.get("attachment_depth"),
            "email_subject": source_manifest.get("email_subject")
            or source_manifest.get("email_metadata", {}).get("subject"),
            "message_id": source_manifest.get("message_id")
            or source_manifest.get("email_metadata", {}).get("message_id"),
            "source_family": source_family,
            "trust_tier": trust_tier,
            "pending_promotion": pending_promotion,
            "memory_kind": knowledge.get("memory_kind"),
            "durability": knowledge.get("durability"),
            "uncertainty": knowledge.get("uncertainty"),
            "answer_use_policy": knowledge.get("answer_use_policy"),
            "retrieval_rank_prior": knowledge.get("retrieval_rank_prior"),
            "source_language": knowledge.get("source_language"),
            "title": knowledge.get("title", ""),
            "summary_en": knowledge.get("summary_en", ""),
            "summary_source": knowledge.get("summary_source", ""),
            "summary_markdown": summary_text,
            "entities": entities,
            "key_points": key_points,
            "claims": claims,
            "known_gaps": [
                value for value in knowledge.get("known_gaps", []) if isinstance(value, str)
            ],
            "ambiguities": [
                value for value in knowledge.get("ambiguities", []) if isinstance(value, str)
            ],
            "citation_count": citation_count,
            "citation_density": density,
            "related_source_ids": related_sources,
            "top_citation_unit_ids": [
                unit_id for unit_id in top_citation_unit_ids if isinstance(unit_id, str)
            ],
            "available_channels": available_channels,
            "channel_descriptors": channel_descriptors,
            "affordance_confidence": affordance_confidence,
            "affordance_derivation_mode": affordance_derivation_mode,
            "derived_affordance_path": str(
                (source_dir / DEFAULT_AFFORDANCE_FILENAME).relative_to(paths.root)
            ),
            "path_aliases": source_manifest.get("path_aliases", []),
            "title_aliases": source_manifest.get("title_aliases", []),
            "source_aliases": source_manifest.get("source_aliases", []),
            "warnings": source_warnings,
            "trust_prior": source_manifest.get("trust_prior", {}),
            "path_tokens": tokenize_text(source_manifest["current_path"]),
            "searchable_text": "\n".join(
                [
                    str(knowledge.get("title", "")),
                    str(knowledge.get("summary_en", "")),
                    str(knowledge.get("summary_source", "")),
                    "\n".join(str(value) for value in entities),
                    "\n".join(str(value) for value in key_points),
                    "\n".join(str(value) for value in claims),
                    flatten_channel_descriptors(channel_descriptors),
                    source_manifest["current_path"],
                    summary_text,
                ]
            ),
        }
        source_records.append(source_record)
        unit_citation_counts: Counter[str] = Counter(
            citation["unit_id"]
            for citation in citations_from_knowledge(knowledge)
            if isinstance(citation.get("unit_id"), str)
        )
        for unit in evidence_manifest.get("units", []):
            if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str):
                continue
            text_asset = unit.get("text_asset")
            extracted_text = ""
            if isinstance(text_asset, str) and text_asset:
                extracted_text = safe_read_text(source_dir / text_asset).strip()
            structure_asset = unit.get("structure_asset")
            structure_data: dict[str, Any] = {}
            if isinstance(structure_asset, str) and structure_asset:
                structure_data = read_json(source_dir / structure_asset)
            unit_affordance = affordance_lookup.get(unit["unit_id"], {})
            unit_channels = available_channels_from_record(unit_affordance)
            unit_channel_descriptors = channel_descriptors_from_record(unit_affordance)
            record = {
                "source_id": source_manifest["source_id"],
                "source_fingerprint": source_manifest["source_fingerprint"],
                "current_path": source_manifest["current_path"],
                "document_type": source_manifest["document_type"],
                "support_tier": source_manifest.get("support_tier"),
                "source_extension": source_manifest.get("source_extension"),
                "source_origin": source_manifest.get("source_origin", "original-document"),
                "parent_source_id": source_manifest.get("parent_source_id"),
                "root_email_source_id": source_manifest.get("root_email_source_id"),
                "attachment_filename": source_manifest.get("attachment_filename"),
                "attachment_mime_type": source_manifest.get("attachment_mime_type"),
                "attachment_depth": source_manifest.get("attachment_depth"),
                "email_subject": source_manifest.get("email_subject")
                or source_manifest.get("email_metadata", {}).get("subject"),
                "message_id": source_manifest.get("message_id")
                or source_manifest.get("email_metadata", {}).get("message_id"),
                "source_family": source_family,
                "trust_tier": trust_tier,
                "pending_promotion": pending_promotion,
                "memory_kind": knowledge.get("memory_kind"),
                "durability": knowledge.get("durability"),
                "uncertainty": knowledge.get("uncertainty"),
                "answer_use_policy": knowledge.get("answer_use_policy"),
                "retrieval_rank_prior": knowledge.get("retrieval_rank_prior"),
                "unit_id": unit["unit_id"],
                "unit_type": unit.get("unit_type"),
                "ordinal": unit.get("ordinal"),
                "title": unit.get("title"),
                "text_asset": text_asset,
                "structure_asset": structure_asset,
                "render_references": render_references_from_unit(unit),
                "embedded_media": unit.get("embedded_media", []),
                "available_channels": unit_channels,
                "channel_descriptors": unit_channel_descriptors,
                "affordance_confidence": str(
                    unit_affordance.get("confidence") or affordance_confidence
                ),
                "affordance_derivation_mode": str(
                    unit_affordance.get("derivation_mode") or affordance_derivation_mode
                ),
                "hidden": bool(unit.get("hidden", False)),
                "extraction_confidence": unit.get("extraction_confidence"),
                "logical_ordinal": unit.get("logical_ordinal"),
                "render_ordinal": unit.get("render_ordinal"),
                "sheet_name": unit.get("sheet_name"),
                "line_start": unit.get("line_start"),
                "line_end": unit.get("line_end"),
                "slug_anchor": unit.get("slug_anchor"),
                "header_names": unit.get("header_names", []),
                "row_count": unit.get("row_count"),
                "heading_aliases": unit.get("heading_aliases", []),
                "semantic_page_aliases": unit.get("semantic_page_aliases", []),
                "locator_aliases": unit.get("locator_aliases", []),
                "cell_hint_supported": unit.get("cell_hint_supported", False),
                "child_source_id": structure_data.get("child_source_id"),
                "published_asset": structure_data.get("published_asset"),
                "warnings": [
                    warning
                    for warning in unit.get("warnings", [])
                    if isinstance(warning, str) and warning.strip()
                ],
                "citation_count": unit_citation_counts[unit["unit_id"]],
                "citation_density": min(unit_citation_counts[unit["unit_id"]], 3),
                "trust_prior_inputs": unit.get("trust_prior_inputs", {}),
                "text": extracted_text,
                "structure_summary": json.dumps(structure_data, ensure_ascii=False, sort_keys=True),
                "searchable_text": "\n".join(
                    [
                        str(unit.get("title", "")),
                        extracted_text,
                        json.dumps(structure_data, ensure_ascii=False, sort_keys=True),
                        flatten_channel_descriptors(unit_channel_descriptors),
                    ]
                ),
            }
            unit_records.append(record)

    write_json(paths.retrieval_source_records_path(target), {"records": source_records})
    write_json(paths.retrieval_unit_records_path(target), {"records": unit_records})
    manifest = {
        "generated_at": utc_now(),
        "target": target,
        "source_signature": source_signature,
        "source_count": len(source_records),
        "unit_count": len(unit_records),
        "graph_edge_count": len(graph_edges),
        "source_record_path": str(
            paths.retrieval_source_records_path(target).relative_to(
                paths.knowledge_target_dir(target)
            )
        ),
        "unit_record_path": str(
            paths.retrieval_unit_records_path(target).relative_to(
                paths.knowledge_target_dir(target)
            )
        ),
        "source_fingerprints": {
            record["source_id"]: record["source_fingerprint"] for record in source_records
        },
    }
    write_json(paths.retrieval_manifest_path(target), manifest)
    return manifest


def build_trace_artifacts(
    paths: WorkspacePaths,
    *,
    target: str,
    source_contexts: list[dict[str, Any]],
    graph_edges: list[dict[str, Any]],
    source_signature: str | None,
) -> dict[str, Any]:
    """Build and persist trace artifacts for a validated knowledge-base target."""
    trace_dir = paths.trace_dir(target)
    trace_dir.mkdir(parents=True, exist_ok=True)

    source_provenance: dict[str, Any] = {}
    unit_provenance: dict[str, Any] = {}
    relation_index: dict[str, Any] = {}
    knowledge_consumers: dict[str, Any] = {}

    incoming_edges: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    outgoing_edges: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph_edges:
        if not isinstance(edge.get("source_id"), str) or not isinstance(
            edge.get("related_source_id"), str
        ):
            continue
        outgoing_edges[edge["source_id"]].append(edge)
        incoming_edges[edge["related_source_id"]].append(edge)

    for context in source_contexts:
        source_manifest = context["source_manifest"]
        evidence_manifest = context["evidence_manifest"]
        knowledge = context["knowledge"]
        summary_text = context["summary_text"]
        (
            available_channels,
            channel_descriptors,
            affordance_confidence,
            affordance_derivation_mode,
        ) = _source_affordance_metadata(context)
        affordance_lookup = _unit_affordance_lookup(context)
        source_id = source_manifest["source_id"]
        source_family = str(context.get("source_family", "corpus"))
        trust_tier = str(context.get("trust_tier", "source"))
        pending_promotion = bool(context.get("pending_promotion", False))
        artifact_dir = context.get("artifact_dir")
        if isinstance(artifact_dir, Path):
            source_dir = artifact_dir
        elif isinstance(artifact_dir, str):
            source_dir = Path(artifact_dir)
        else:
            source_dir = paths.knowledge_target_dir(target) / "sources" / source_id

        consumers_by_unit: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        top_level_citations: list[dict[str, Any]] = []
        for citation in knowledge.get("citations", []):
            if not isinstance(citation, dict) or not isinstance(citation.get("unit_id"), str):
                continue
            top_level_citations.append(citation)
            consumers_by_unit[citation["unit_id"]].append(
                {
                    "consumer_type": "top-level-citation",
                    "support": citation.get("support"),
                }
            )

        for consumer_type, items in (
            ("key-point", knowledge.get("key_points", [])),
            ("claim", knowledge.get("claims", [])),
            ("ambiguity", knowledge.get("ambiguities", [])),
        ):
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items, start=1):
                if not isinstance(item, dict):
                    continue
                citations = item.get("citations", [])
                if not isinstance(citations, list):
                    continue
                for citation in citations:
                    if not isinstance(citation, dict) or not isinstance(
                        citation.get("unit_id"), str
                    ):
                        continue
                    consumer = {
                        "consumer_type": consumer_type,
                        "index": index,
                        "support": citation.get("support"),
                    }
                    for key in ("text_en", "text_source", "statement_en", "statement_source"):
                        value = item.get(key)
                        if isinstance(value, str) and value:
                            consumer[key] = value
                    consumers_by_unit[citation["unit_id"]].append(consumer)

        summary_citations = [
            str(citation["unit_id"])
            for citation in top_level_citations
            if isinstance(citation.get("unit_id"), str)
        ]
        source_warnings = [
            warning
            for warning in evidence_manifest.get("warnings", [])
            if isinstance(warning, str) and warning.strip()
        ]
        for unit_id in summary_citations:
            consumers_by_unit[unit_id].append({"consumer_type": "summary"})

        cited_unit_ids = sorted(consumers_by_unit.keys())
        source_provenance[source_id] = {
            "source_id": source_id,
            "source_fingerprint": source_manifest["source_fingerprint"],
            "current_path": source_manifest["current_path"],
            "prior_paths": source_manifest.get("prior_paths", []),
            "path_history": source_manifest.get("path_history", []),
            "document_type": source_manifest["document_type"],
            "support_tier": source_manifest.get("support_tier"),
            "source_extension": source_manifest.get("source_extension"),
            "source_origin": source_manifest.get("source_origin", "original-document"),
            "parent_source_id": source_manifest.get("parent_source_id"),
            "root_email_source_id": source_manifest.get("root_email_source_id"),
            "attachment_filename": source_manifest.get("attachment_filename"),
            "attachment_mime_type": source_manifest.get("attachment_mime_type"),
            "attachment_depth": source_manifest.get("attachment_depth"),
            "email_subject": source_manifest.get("email_subject")
            or source_manifest.get("email_metadata", {}).get("subject"),
            "message_id": source_manifest.get("message_id")
            or source_manifest.get("email_metadata", {}).get("message_id"),
            "source_family": source_family,
            "trust_tier": trust_tier,
            "pending_promotion": pending_promotion,
            "memory_kind": knowledge.get("memory_kind"),
            "durability": knowledge.get("durability"),
            "uncertainty": knowledge.get("uncertainty"),
            "answer_use_policy": knowledge.get("answer_use_policy"),
            "retrieval_rank_prior": knowledge.get("retrieval_rank_prior"),
            "title": knowledge.get("title"),
            "summary_en": knowledge.get("summary_en"),
            "summary_source": knowledge.get("summary_source"),
            "summary_markdown_path": "summary.md",
            "summary_markdown": summary_text,
            "available_channels": available_channels,
            "channel_descriptors": channel_descriptors,
            "affordance_confidence": affordance_confidence,
            "affordance_derivation_mode": affordance_derivation_mode,
            "source_manifest_path": "source_manifest.json",
            "evidence_manifest_path": "evidence_manifest.json",
            "derived_affordance_path": DEFAULT_AFFORDANCE_FILENAME,
            "path_aliases": source_manifest.get("path_aliases", []),
            "title_aliases": source_manifest.get("title_aliases", []),
            "source_aliases": source_manifest.get("source_aliases", []),
            "warnings": source_warnings,
            "top_citation_unit_ids": summary_citations,
            "cited_unit_ids": cited_unit_ids,
            "unit_citation_counts": {
                unit_id: len(consumers) for unit_id, consumers in consumers_by_unit.items()
            },
            "relations": {
                "outgoing": outgoing_edges[source_id],
                "incoming": incoming_edges[source_id],
            },
            "render_paths": sorted(
                {
                    render
                    for render in evidence_manifest.get("document_renders", [])
                    if isinstance(render, str)
                }
            ),
        }
        relation_index[source_id] = {
            "outgoing": outgoing_edges[source_id],
            "incoming": incoming_edges[source_id],
        }

        for unit in evidence_manifest.get("units", []):
            if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str):
                continue
            unit_id = unit["unit_id"]
            text_asset = unit.get("text_asset")
            extracted_text = ""
            if isinstance(text_asset, str) and text_asset:
                extracted_text = safe_read_text(source_dir / text_asset).strip()
            structure_asset = unit.get("structure_asset")
            structure_data: dict[str, Any] = {}
            if isinstance(structure_asset, str) and structure_asset:
                structure_data = read_json(source_dir / structure_asset)
            unit_affordance = affordance_lookup.get(unit_id, {})
            key = f"{source_id}:{unit_id}"
            unit_provenance[key] = {
                "source_id": source_id,
                "unit_id": unit_id,
                "document_type": source_manifest["document_type"],
                "support_tier": source_manifest.get("support_tier"),
                "source_extension": source_manifest.get("source_extension"),
                "source_origin": source_manifest.get("source_origin", "original-document"),
                "parent_source_id": source_manifest.get("parent_source_id"),
                "root_email_source_id": source_manifest.get("root_email_source_id"),
                "attachment_filename": source_manifest.get("attachment_filename"),
                "attachment_mime_type": source_manifest.get("attachment_mime_type"),
                "attachment_depth": source_manifest.get("attachment_depth"),
                "email_subject": source_manifest.get("email_subject")
                or source_manifest.get("email_metadata", {}).get("subject"),
                "message_id": source_manifest.get("message_id")
                or source_manifest.get("email_metadata", {}).get("message_id"),
                "source_family": source_family,
                "trust_tier": trust_tier,
                "pending_promotion": pending_promotion,
                "memory_kind": knowledge.get("memory_kind"),
                "durability": knowledge.get("durability"),
                "uncertainty": knowledge.get("uncertainty"),
                "answer_use_policy": knowledge.get("answer_use_policy"),
                "retrieval_rank_prior": knowledge.get("retrieval_rank_prior"),
                "current_path": source_manifest["current_path"],
                "title": unit.get("title"),
                "unit_type": unit.get("unit_type"),
                "ordinal": unit.get("ordinal"),
                "available_channels": available_channels_from_record(unit_affordance),
                "channel_descriptors": channel_descriptors_from_record(unit_affordance),
                "affordance_confidence": str(
                    unit_affordance.get("confidence") or affordance_confidence
                ),
                "affordance_derivation_mode": str(
                    unit_affordance.get("derivation_mode") or affordance_derivation_mode
                ),
                "extraction_confidence": unit.get("extraction_confidence"),
                "hidden": bool(unit.get("hidden", False)),
                "logical_ordinal": unit.get("logical_ordinal"),
                "render_ordinal": unit.get("render_ordinal"),
                "sheet_name": unit.get("sheet_name"),
                "line_start": unit.get("line_start"),
                "line_end": unit.get("line_end"),
                "slug_anchor": unit.get("slug_anchor"),
                "header_names": unit.get("header_names", []),
                "row_count": unit.get("row_count"),
                "heading_aliases": unit.get("heading_aliases", []),
                "semantic_page_aliases": unit.get("semantic_page_aliases", []),
                "locator_aliases": unit.get("locator_aliases", []),
                "cell_hint_supported": unit.get("cell_hint_supported", False),
                "child_source_id": structure_data.get("child_source_id"),
                "published_asset": structure_data.get("published_asset"),
                "warnings": [
                    warning
                    for warning in unit.get("warnings", [])
                    if isinstance(warning, str) and warning.strip()
                ],
                "text_asset": text_asset,
                "structure_asset": structure_asset,
                "render_references": render_references_from_unit(unit),
                "embedded_media": unit.get("embedded_media", []),
                "text_excerpt": extracted_text,
                "consumers": consumers_by_unit[unit_id],
            }
            knowledge_consumers[key] = {
                "source_id": source_id,
                "unit_id": unit_id,
                "consumers": consumers_by_unit[unit_id],
                "consumer_summaries": [
                    summarized_consumer_text(consumer) for consumer in consumers_by_unit[unit_id]
                ],
            }

    write_json(paths.trace_source_provenance_path(target), source_provenance)
    write_json(paths.trace_unit_provenance_path(target), unit_provenance)
    write_json(paths.trace_relation_index_path(target), relation_index)
    write_json(paths.trace_knowledge_consumers_path(target), knowledge_consumers)
    manifest = {
        "generated_at": utc_now(),
        "target": target,
        "source_signature": source_signature,
        "source_count": len(source_provenance),
        "unit_count": len(unit_provenance),
        "graph_edge_count": len(graph_edges),
        "source_provenance_path": str(
            paths.trace_source_provenance_path(target).relative_to(
                paths.knowledge_target_dir(target)
            )
        ),
        "unit_provenance_path": str(
            paths.trace_unit_provenance_path(target).relative_to(paths.knowledge_target_dir(target))
        ),
        "relation_index_path": str(
            paths.trace_relation_index_path(target).relative_to(paths.knowledge_target_dir(target))
        ),
        "knowledge_consumers_path": str(
            paths.trace_knowledge_consumers_path(target).relative_to(
                paths.knowledge_target_dir(target)
            )
        ),
    }
    write_json(paths.trace_manifest_path(target), manifest)
    return manifest


def _memory_semantics_fallback_text(record: dict[str, Any]) -> str:
    parts: list[str] = []
    for field_name in ("title", "summary_en", "summary_source", "searchable_text", "text"):
        value = record.get(field_name)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
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


def _interaction_semantic_hints(
    paths: WorkspacePaths,
    *,
    target: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    source_family = str(record.get("source_family", "corpus"))
    if source_family == "interaction-memory":
        source_id = record.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            return {}
        for candidate_target in (target, "current"):
            source_manifest_path = (
                paths.interaction_memories_dir(candidate_target)
                / source_id
                / "source_manifest.json"
            )
            context_path = (
                paths.interaction_memories_dir(candidate_target)
                / source_id
                / "interaction_context.json"
            )
            source_manifest = read_json(source_manifest_path)
            interaction_context = read_json(context_path)
            if not interaction_context:
                continue
            semantics = interaction_context.get("semantics")
            if isinstance(semantics, dict) and semantics:
                return semantics
            related_sources = interaction_context.get("related_sources", [])
            relation_types = [
                str(item.get("relation_type"))
                for item in related_sources
                if isinstance(item, dict) and isinstance(item.get("relation_type"), str)
            ]
            interaction_ids = source_manifest.get("interaction_ids", [])
            if isinstance(interaction_ids, list):
                memory_kind: str | None = None
                for interaction_id in interaction_ids:
                    if not isinstance(interaction_id, str) or not interaction_id:
                        continue
                    entry = read_json(paths.interaction_entries_dir / f"{interaction_id}.json")
                    continuation_type = (
                        str(entry.get("continuation_type")).strip()
                        if isinstance(entry.get("continuation_type"), str)
                        else None
                    )
                    if continuation_type == "constraint-update":
                        return {"memory_kind": "constraint"}
                    if continuation_type == "mixed":
                        memory_kind = "clarification"
                if memory_kind is not None:
                    return {"memory_kind": memory_kind}
            memory_kind = _memory_kind_from_relation_types(relation_types)
            if memory_kind is not None:
                return {"memory_kind": memory_kind}
            return {}
        return {}

    if source_family == "interaction-pending":
        current_path = record.get("current_path")
        if not isinstance(current_path, str) or not current_path:
            return {}
        entry_path = Path(current_path)
        if not entry_path.is_absolute():
            entry_path = paths.root / current_path
        entry = read_json(entry_path)
        if not entry:
            return {}
        continuation_type = (
            str(entry.get("continuation_type")).strip()
            if isinstance(entry.get("continuation_type"), str)
            else None
        )
        relation_hints = entry.get("relation_hints", [])
        relation_types = [
            str(item.get("relation_type"))
            for item in relation_hints
            if isinstance(item, dict) and isinstance(item.get("relation_type"), str)
        ]
        memory_kind = _memory_kind_from_relation_types(relation_types)
        if memory_kind is None and continuation_type == "constraint-update":
            memory_kind = "constraint"
        if memory_kind is None and continuation_type == "mixed":
            memory_kind = "clarification"
        return {"memory_kind": memory_kind} if memory_kind else {}

    return {}


def _normalize_memory_semantics_record(
    paths: WorkspacePaths,
    *,
    target: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    source_family = str(record.get("source_family", "corpus"))
    if source_family not in {"interaction-memory", "interaction-pending"}:
        return record
    normalized = normalize_memory_semantics(
        {
            field_name: record.get(field_name)
            for field_name in (
                "memory_kind",
                "durability",
                "uncertainty",
                "answer_use_policy",
                "retrieval_rank_prior",
            )
        },
        fallback_text=_memory_semantics_fallback_text(record),
        semantic_hints=_interaction_semantic_hints(paths, target=target, record=record),
    )
    enriched = dict(record)
    enriched.update(normalized)
    return enriched


def _normalize_affordance_record(record: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(record)
    enriched["available_channels"] = available_channels_from_record(record)
    enriched["channel_descriptors"] = channel_descriptors_from_record(record)
    enriched["affordance_confidence"] = confidence_from_record(record)
    explicit_mode = record.get("affordance_derivation_mode")
    enriched["affordance_derivation_mode"] = (
        str(explicit_mode)
        if explicit_mode in {"deterministic", "agent-authored", "hybrid"}
        else "deterministic"
    )
    affordance_text = flatten_channel_descriptors(enriched["channel_descriptors"])
    if affordance_text:
        existing_searchable = str(record.get("searchable_text", "")).strip()
        enriched["searchable_text"] = "\n".join(
            item for item in [existing_searchable, affordance_text] if item
        )
    return enriched


def _effective_evidence_requirements(
    evidence_requirements: dict[str, Any] | None,
    *,
    question_domain: str | None,
    question_class: str | None = None,
) -> dict[str, Any]:
    effective_question_domain = question_domain or "workspace-corpus"
    effective_question_class = question_class or (
        "composition" if effective_question_domain == "composition" else "answer"
    )
    raw_requirements = evidence_requirements if isinstance(evidence_requirements, dict) else {}
    if not raw_requirements and effective_question_domain in {"external-factual", "general-stable"}:
        raw_requirements = {"prefer_published_artifacts": False}
    return normalize_evidence_requirements(
        raw_requirements,
        question_class=effective_question_class,
        question_domain=effective_question_domain,
    )


def load_retrieval_data(paths: WorkspacePaths, *, target: str = "current") -> dict[str, Any]:
    """Load the retrieval artifacts for a knowledge-base target."""
    manifest = read_json(paths.retrieval_manifest_path(target))
    source_records = read_json(paths.retrieval_source_records_path(target)).get("records", [])
    unit_records = read_json(paths.retrieval_unit_records_path(target)).get("records", [])
    if not manifest or not isinstance(source_records, list) or not isinstance(unit_records, list):
        raise FileNotFoundError(
            f"Retrieval artifacts are missing for `{target}`. Rerun `docmason sync`."
        )
    return {
        "manifest": manifest,
        "source_records": [
            normalize_source_record_reference(
                _normalize_affordance_record(
                    _normalize_memory_semantics_record(paths, target=target, record=record)
                )
            )
            for record in source_records
            if isinstance(record, dict)
        ],
        "unit_records": [
            normalize_unit_record_reference(
                _normalize_affordance_record(
                    _normalize_memory_semantics_record(paths, target=target, record=record)
                )
            )
            for record in unit_records
            if isinstance(record, dict)
        ],
    }


def merge_pending_interaction_overlay(
    paths: WorkspacePaths,
    retrieval_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge the runtime pending interaction overlay into retrieval artifacts."""
    overlay = load_interaction_overlay(paths)
    merged: dict[str, Any] = {
        "manifest": dict(retrieval_data["manifest"]),
        "source_records": list(retrieval_data["source_records"]),
        "unit_records": list(retrieval_data["unit_records"]),
        "graph_edges": list(retrieval_data.get("graph_edges", [])),
    }
    overlay_source_records = overlay.get("source_records", [])
    overlay_unit_records = overlay.get("unit_records", [])
    overlay_graph_edges = overlay.get("graph_edges", [])
    if isinstance(overlay_source_records, list):
        merged["source_records"].extend(
            record for record in overlay_source_records if isinstance(record, dict)
        )
    if isinstance(overlay_unit_records, list):
        merged["unit_records"].extend(
            record for record in overlay_unit_records if isinstance(record, dict)
        )
    if isinstance(overlay_graph_edges, list):
        merged["graph_edges"].extend(edge for edge in overlay_graph_edges if isinstance(edge, dict))
    merged["manifest"]["pending_interaction_source_count"] = len(
        [record for record in overlay_source_records if isinstance(record, dict)]
    )
    return merged


def load_trace_data(paths: WorkspacePaths, *, target: str = "current") -> dict[str, Any]:
    """Load the trace artifacts for a knowledge-base target."""
    manifest = read_json(paths.trace_manifest_path(target))
    source_provenance = read_json(paths.trace_source_provenance_path(target))
    unit_provenance = read_json(paths.trace_unit_provenance_path(target))
    relation_index = read_json(paths.trace_relation_index_path(target))
    knowledge_consumers = read_json(paths.trace_knowledge_consumers_path(target))
    if not manifest:
        raise FileNotFoundError(
            f"Trace artifacts are missing for `{target}`. Rerun `docmason sync`."
        )
    return {
        "manifest": manifest,
        "source_provenance": {
            key: (
                normalize_source_record_reference(
                    _normalize_affordance_record(
                        _normalize_memory_semantics_record(paths, target=target, record=value)
                    )
                )
                if isinstance(value, dict)
                else value
            )
            for key, value in source_provenance.items()
        },
        "unit_provenance": {
            key: (
                normalize_unit_record_reference(
                    _normalize_affordance_record(
                        _normalize_memory_semantics_record(paths, target=target, record=value)
                    )
                )
                if isinstance(value, dict)
                else value
            )
            for key, value in unit_provenance.items()
        },
        "relation_index": relation_index,
        "knowledge_consumers": knowledge_consumers,
    }


def merge_pending_interaction_trace(
    paths: WorkspacePaths,
    trace_data: dict[str, Any],
) -> dict[str, Any]:
    """Merge runtime pending interaction trace metadata into citation-first lookups."""
    overlay = load_interaction_overlay(paths)
    merged = {
        "manifest": dict(trace_data["manifest"]),
        "source_provenance": dict(trace_data["source_provenance"]),
        "unit_provenance": dict(trace_data["unit_provenance"]),
        "relation_index": dict(trace_data["relation_index"]),
        "knowledge_consumers": dict(trace_data["knowledge_consumers"]),
    }
    for key in ("source_provenance", "unit_provenance", "relation_index", "knowledge_consumers"):
        payload = overlay.get(key)
        if isinstance(payload, dict):
            merged[key].update(payload)
    merged["manifest"]["pending_interaction_source_count"] = len(
        overlay.get("source_provenance", {})
    )
    return merged


def should_merge_pending_interaction(
    question_domain: str | None,
    memory_profile: dict[str, Any] | None = None,
) -> bool:
    """Return whether pending interaction overlay should participate for the query domain."""
    if question_domain == "composition":
        return True
    if question_domain == "workspace-corpus":
        if not isinstance(memory_profile, dict):
            return True
        return str(memory_profile.get("mode") or "minimal") != "minimal"
    return question_domain is None


def _effective_source_ids_from_reference(
    source_ids: list[str] | None,
    reference_resolution: dict[str, Any] | None,
) -> list[str]:
    """Derive safe retrieval source filters from layered reference-resolution outcomes."""
    explicit_source_ids = [
        source_id
        for source_id in (source_ids or [])
        if isinstance(source_id, str) and source_id.strip()
    ]
    if explicit_source_ids:
        return explicit_source_ids
    if not isinstance(reference_resolution, dict):
        return []
    resolved_source_id = reference_resolution.get("resolved_source_id")
    source_match_status = str(reference_resolution.get("source_match_status") or "none")
    unit_match_status = str(reference_resolution.get("unit_match_status") or "none")
    should_narrow = source_match_status == "exact" or (
        source_match_status == "approximate" and unit_match_status == "exact"
    )
    if should_narrow and isinstance(resolved_source_id, str) and resolved_source_id.strip():
        return [resolved_source_id]
    return []


def _turn_record_from_answer_file(
    paths: WorkspacePaths,
    *,
    answer_file_path: str | None,
) -> dict[str, Any]:
    if not isinstance(answer_file_path, str) or not answer_file_path:
        return {}
    answer_path = Path(answer_file_path)
    if not answer_path.is_absolute():
        answer_path = paths.root / answer_file_path
    try:
        relative = answer_path.relative_to(paths.answers_dir)
    except ValueError:
        return {}
    conversation_id = relative.parent.name
    turn_id = relative.stem
    if not conversation_id or not turn_id:
        return {}
    conversation = read_json(paths.conversations_dir / f"{conversation_id}.json")
    turns = conversation.get("turns", [])
    if not isinstance(turns, list):
        return {}
    for turn in turns:
        if isinstance(turn, dict) and turn.get("turn_id") == turn_id:
            enriched = dict(turn)
            enriched.setdefault("conversation_id", conversation_id)
            return enriched
    return {}


def combined_trace_status(
    *,
    answer_state: str,
    support_basis: str | None,
    support_manifest_path: str | None,
) -> str:
    """Return the user-consumable trace status across KB and external support contracts."""
    if answer_state == "abstained":
        return "ready"
    if support_basis == "kb-grounded":
        return "ready" if answer_state == "grounded" else "degraded"
    if support_basis == "mixed":
        if answer_state in {"grounded", "partially-grounded"} or support_manifest_path:
            return "ready"
        return "degraded"
    if support_basis in {"external-source-verified", "model-knowledge"}:
        return "ready" if answer_state in {"grounded", "partially-grounded"} else "degraded"
    return "ready" if answer_state == "grounded" else "degraded"


def combined_render_requirement(
    *,
    kb_render_required: bool,
    support_basis: str | None,
    answer_state: str,
    support_manifest_path: str | None,
) -> bool:
    """Return the final render-inspection requirement across combined support modes."""
    if answer_state == "abstained":
        return False
    if support_basis in {"external-source-verified", "model-knowledge"}:
        return False
    if (
        support_basis == "mixed"
        and answer_state in {"unresolved", "abstained"}
        and support_manifest_path
    ):
        return False
    return kb_render_required


def score_field(query_tokens: list[str], text: str, *, weight: float) -> tuple[float, set[str]]:
    """Score a single text field for the current query tokens."""
    if not text.strip():
        return 0.0, set()
    field_tokens = Counter(tokenize_text(text))
    matched_terms = {token for token in query_tokens if field_tokens[token] > 0}
    score = sum(min(field_tokens[token], 2) * weight for token in matched_terms)
    return score, matched_terms


def build_graph_adjacency(graph_edges: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Build an undirected adjacency structure over graph edges."""
    adjacency: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph_edges:
        source_id = edge.get("source_id")
        related_source_id = edge.get("related_source_id")
        if not isinstance(source_id, str) or not isinstance(related_source_id, str):
            continue
        adjacency[source_id].append(
            {
                "neighbor": related_source_id,
                "relation_type": edge.get("relation_type"),
                "strength": edge.get("strength"),
                "status": edge.get("status"),
                "citation_unit_ids": edge.get("citation_unit_ids", []),
                "direction": "outgoing",
            }
        )
        adjacency[related_source_id].append(
            {
                "neighbor": source_id,
                "relation_type": edge.get("relation_type"),
                "strength": edge.get("strength"),
                "status": edge.get("status"),
                "citation_unit_ids": edge.get("citation_unit_ids", []),
                "direction": "incoming",
            }
        )
    return adjacency


def choose_support_units(
    source_record: dict[str, Any],
    unit_scores: list[dict[str, Any]],
    units_by_source: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Choose the compact support units for a result bundle."""
    lookup = {
        unit["unit_id"]: unit
        for unit in units_by_source.get(source_record["source_id"], [])
        if isinstance(unit.get("unit_id"), str)
    }
    if unit_scores:
        scored_support_units: list[dict[str, Any]] = []
        for scored in unit_scores[:3]:
            unit_id = scored.get("unit_id")
            unit = lookup.get(unit_id, {}) if isinstance(unit_id, str) else {}
            scored_support_units.append(
                {
                    **unit,
                    **scored,
                    "render_references": unit.get("render_references", []),
                    "embedded_media": unit.get("embedded_media", []),
                    "structure_asset": unit.get("structure_asset"),
                    "line_start": unit.get("line_start"),
                    "line_end": unit.get("line_end"),
                    "slug_anchor": unit.get("slug_anchor"),
                    "header_names": unit.get("header_names", []),
                    "row_count": unit.get("row_count"),
                    "available_channels": unit.get("available_channels", []),
                    "channel_descriptors": unit.get("channel_descriptors", {}),
                    "affordance_confidence": unit.get("affordance_confidence"),
                    "affordance_derivation_mode": unit.get("affordance_derivation_mode"),
                    "extraction_confidence": unit.get("extraction_confidence"),
                    "warnings": unit.get("warnings", []),
                    "text_excerpt": unit.get("text", ""),
                }
            )
        return scored_support_units
    cited_units = [
        unit_id
        for unit_id in source_record.get("top_citation_unit_ids", [])
        if isinstance(unit_id, str)
    ]
    if not cited_units:
        return []
    cited_support_units: list[dict[str, Any]] = []
    for unit_id in cited_units:
        cited_unit = lookup.get(unit_id)
        if cited_unit is None:
            continue
        cited_support_units.append(
            {
                "unit_id": cited_unit["unit_id"],
                "title": cited_unit.get("title"),
                "score": {
                    "lexical": 0.0,
                    "metadata_bonus": confidence_bonus(cited_unit.get("extraction_confidence"))
                    + (0.2 * float(cited_unit.get("citation_density", 0))),
                    "total": confidence_bonus(cited_unit.get("extraction_confidence"))
                    + (0.2 * float(cited_unit.get("citation_density", 0))),
                },
                "matched_terms": [],
                "render_references": cited_unit.get("render_references", []),
                "embedded_media": cited_unit.get("embedded_media", []),
                "structure_asset": cited_unit.get("structure_asset"),
                "line_start": cited_unit.get("line_start"),
                "line_end": cited_unit.get("line_end"),
                "slug_anchor": cited_unit.get("slug_anchor"),
                "header_names": cited_unit.get("header_names", []),
                "row_count": cited_unit.get("row_count"),
                "available_channels": cited_unit.get("available_channels", []),
                "channel_descriptors": cited_unit.get("channel_descriptors", {}),
                "affordance_confidence": cited_unit.get("affordance_confidence"),
                "affordance_derivation_mode": cited_unit.get("affordance_derivation_mode"),
                "extraction_confidence": cited_unit.get("extraction_confidence"),
                "warnings": cited_unit.get("warnings", []),
                "text_excerpt": cited_unit.get("text", ""),
            }
        )
        if len(cited_support_units) >= 3:
            break
    return cited_support_units


def memory_score_adjustment(
    source_record: dict[str, Any],
    *,
    memory_profile: dict[str, Any],
    lexical_source: float,
    lexical_units: float,
    question_domain: str | None = None,
) -> tuple[bool, float]:
    """Return whether an interaction-memory record should participate and how much to adjust it."""
    source_family = str(source_record.get("source_family", "corpus"))
    if source_family not in {"interaction-memory", "interaction-pending"}:
        return True, 0.0

    answer_use_policy = str(source_record.get("answer_use_policy") or "contextual-only")
    retrieval_rank_prior = str(source_record.get("retrieval_rank_prior") or "low")
    memory_kind = str(source_record.get("memory_kind") or "")
    mode = str(memory_profile.get("mode") or "minimal")
    relevant_kinds = set(
        kind
        for kind in memory_profile.get("relevant_memory_kinds", [])
        if isinstance(kind, str) and kind
    )

    lexical_total = lexical_source + lexical_units
    if question_domain == "external-factual":
        return False, 0.0
    if question_domain == "workspace-corpus" and mode == "minimal":
        return False, 0.0
    if mode == "minimal" and lexical_total < 2.0:
        return False, 0.0
    if question_domain == "general-stable" and answer_use_policy == "contextual-only":
        if lexical_total < 4.0:
            return False, 0.0
    if question_domain == "workspace-corpus" and memory_kind in {"operator-intent", "working-note"}:
        if lexical_total < 3.0:
            return False, 0.0

    bonus = MEMORY_RANK_PRIOR_BONUS.get(retrieval_rank_prior, 0.0)
    if mode == "strong":
        bonus += 0.4
        if relevant_kinds and memory_kind in relevant_kinds:
            bonus += 0.6
    elif mode == "contextual":
        bonus += 0.15
        if relevant_kinds and memory_kind in relevant_kinds:
            bonus += 0.35
        if answer_use_policy == "contextual-only":
            bonus -= 0.1
    else:
        if answer_use_policy == "contextual-only":
            bonus -= 1.0
        else:
            bonus -= 0.4
    return True, bonus


def run_retrieval_query(
    retrieval_data: dict[str, Any],
    *,
    query: str,
    top: int,
    graph_hops: int,
    document_types: list[str] | None,
    source_ids: list[str] | None,
    include_renders: bool,
    question_domain: str | None = None,
    evidence_requirements: dict[str, Any] | None = None,
    reference_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run deterministic lexical plus graph retrieval over published artifacts."""
    query_tokens = tokenize_text(query)
    effective_question_domain = question_domain
    effective_evidence_requirements = _effective_evidence_requirements(
        evidence_requirements,
        question_domain=effective_question_domain,
    )
    preferred_channels = [
        channel
        for channel in effective_evidence_requirements.get("preferred_channels", [])
        if isinstance(channel, str)
    ]
    memory_profile = infer_memory_query_profile(query, question_domain=effective_question_domain)
    source_records = retrieval_data["source_records"]
    unit_records = retrieval_data["unit_records"]
    graph_edges = [edge for edge in retrieval_data.get("graph_edges", []) if isinstance(edge, dict)]

    filtered_document_types = set(document_types or [])
    filtered_source_ids = set(source_ids or [])
    effective_reference_resolution = (
        dict(reference_resolution) if isinstance(reference_resolution, dict) else {}
    )
    resolved_source_id = (
        str(effective_reference_resolution.get("resolved_source_id"))
        if isinstance(effective_reference_resolution.get("resolved_source_id"), str)
        else None
    )
    resolved_unit_id = (
        str(effective_reference_resolution.get("resolved_unit_id"))
        if isinstance(effective_reference_resolution.get("resolved_unit_id"), str)
        else None
    )
    resolution_status = (
        str(effective_reference_resolution.get("status"))
        if isinstance(effective_reference_resolution.get("status"), str)
        else "none"
    )
    units_by_source: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in unit_records:
        if isinstance(unit.get("source_id"), str):
            units_by_source[unit["source_id"]].append(unit)

    base_scores: dict[str, dict[str, Any]] = {}
    source_lookup: dict[str, dict[str, Any]] = {}
    for source_record in source_records:
        source_id = source_record.get("source_id")
        if not isinstance(source_id, str):
            continue
        if (
            filtered_document_types
            and source_record.get("document_type") not in filtered_document_types
        ):
            continue
        if filtered_source_ids and source_id not in filtered_source_ids:
            continue
        source_lookup[source_id] = source_record

        lexical_source = 0.0
        matched_terms: set[str] = set()
        field_breakdown: dict[str, float] = {}
        for field_name, text in (
            ("title", str(source_record.get("title", ""))),
            ("entities", "\n".join(str(value) for value in source_record.get("entities", []))),
            (
                "summary",
                "\n".join(
                    [
                        str(source_record.get("summary_en", "")),
                        str(source_record.get("summary_source", "")),
                        str(source_record.get("summary_markdown", "")),
                    ]
                ),
            ),
            ("claims", "\n".join(str(value) for value in source_record.get("claims", []))),
            ("key_points", "\n".join(str(value) for value in source_record.get("key_points", []))),
            (
                "affordance",
                flatten_channel_descriptors(source_record.get("channel_descriptors", {})),
            ),
            ("path", str(source_record.get("current_path", ""))),
        ):
            score, matches = score_field(query_tokens, text, weight=FIELD_WEIGHTS[field_name])
            if score > 0:
                field_breakdown[field_name] = score
            lexical_source += score
            matched_terms.update(matches)

        unit_scores: list[dict[str, Any]] = []
        exact_target_unit = bool(
            source_id == resolved_source_id
            and resolved_unit_id
            and resolution_status == "exact"
        )
        preferred_unit = bool(
            source_id == resolved_source_id
            and resolved_unit_id
            and resolution_status == "approximate"
        )
        for unit in units_by_source[source_id]:
            unit_id = unit.get("unit_id")
            if exact_target_unit and unit_id != resolved_unit_id:
                continue
            lexical_unit = 0.0
            unit_matches: set[str] = set()
            title_score, title_matches = score_field(
                query_tokens,
                str(unit.get("title", "")),
                weight=FIELD_WEIGHTS["unit_title"],
            )
            text_score, text_matches = score_field(
                query_tokens,
                "\n".join([str(unit.get("text", "")), str(unit.get("structure_summary", ""))]),
                weight=FIELD_WEIGHTS["unit_text"],
            )
            affordance_score, affordance_matches = score_field(
                query_tokens,
                flatten_channel_descriptors(unit.get("channel_descriptors", {})),
                weight=FIELD_WEIGHTS["unit_affordance"],
            )
            lexical_unit += title_score + text_score + affordance_score
            unit_matches.update(title_matches)
            unit_matches.update(text_matches)
            unit_matches.update(affordance_matches)
            channel_preference_bonus = _channel_preference_bonus(
                available_channels_from_record(unit),
                preferred_channels=preferred_channels,
                scope="unit",
            )
            reference_bonus = 0.0
            if isinstance(unit_id, str) and unit_id == resolved_unit_id:
                if exact_target_unit:
                    reference_bonus = 12.0
                elif preferred_unit:
                    reference_bonus = 6.0
            metadata_bonus = confidence_bonus(unit.get("extraction_confidence")) + (
                0.2 * float(unit.get("citation_density", 0))
            ) + channel_preference_bonus + reference_bonus
            total = lexical_unit + metadata_bonus
            if lexical_unit <= 0 and reference_bonus <= 0:
                continue
            matched_terms.update(unit_matches)
            unit_scores.append(
                {
                    "unit_id": unit_id,
                    "title": unit.get("title"),
                    "score": {
                        "lexical": lexical_unit,
                        "metadata_bonus": metadata_bonus,
                        "channel_preference_bonus": channel_preference_bonus,
                        "reference_bonus": reference_bonus,
                        "total": total,
                    },
                    "matched_terms": sorted(unit_matches),
                    "render_references": unit.get("render_references", []),
                    "embedded_media": unit.get("embedded_media", []),
                    "structure_asset": unit.get("structure_asset"),
                    "logical_ordinal": unit.get("logical_ordinal"),
                    "render_ordinal": unit.get("render_ordinal"),
                    "sheet_name": unit.get("sheet_name"),
                    "line_start": unit.get("line_start"),
                    "line_end": unit.get("line_end"),
                    "slug_anchor": unit.get("slug_anchor"),
                    "header_names": unit.get("header_names", []),
                    "row_count": unit.get("row_count"),
                    "heading_aliases": unit.get("heading_aliases", []),
                    "semantic_page_aliases": unit.get("semantic_page_aliases", []),
                    "locator_aliases": unit.get("locator_aliases", []),
                    "available_channels": unit.get("available_channels", []),
                    "channel_descriptors": unit.get("channel_descriptors", {}),
                    "affordance_confidence": unit.get("affordance_confidence"),
                    "affordance_derivation_mode": unit.get("affordance_derivation_mode"),
                    "extraction_confidence": unit.get("extraction_confidence"),
                    "warnings": unit.get("warnings", []),
                    "text_excerpt": str(unit.get("text", ""))[:500],
                }
            )
        unit_scores.sort(
            key=lambda item: (-float(item["score"]["total"]), str(item.get("unit_id", "")))
        )
        if isinstance(resolved_unit_id, str):
            for index, item in enumerate(unit_scores):
                if item.get("unit_id") != resolved_unit_id:
                    continue
                unit_scores.insert(0, unit_scores.pop(index))
                break

        channel_preference_bonus = _channel_preference_bonus(
            available_channels_from_record(source_record),
            preferred_channels=preferred_channels,
            scope="source",
        )
        source_reference_bonus = 0.0
        if source_id == resolved_source_id:
            source_reference_bonus = 5.0 if resolution_status == "exact" else 2.5
        metadata_bonus = (
            0.3 * float(source_record.get("citation_density", 0))
            + trust_prior_bonus(source_record.get("trust_prior", {}))
            + channel_preference_bonus
            + source_reference_bonus
        )
        lexical_units = sum(float(item["score"]["lexical"]) for item in unit_scores[:3])
        unit_metadata_bonus = sum(
            float(item["score"]["metadata_bonus"]) for item in unit_scores[:3]
        )
        if lexical_source <= 0 and lexical_units <= 0 and source_reference_bonus <= 0:
            continue
        allowed, memory_bonus = memory_score_adjustment(
            source_record,
            memory_profile=memory_profile,
            lexical_source=lexical_source,
            lexical_units=lexical_units,
            question_domain=effective_question_domain,
        )
        if not allowed:
            continue
        base_score = (
            lexical_source
            + lexical_units
            + metadata_bonus
            + unit_metadata_bonus
            + memory_bonus
        )
        base_scores[source_id] = {
            "source_record": source_record,
            "field_breakdown": field_breakdown,
            "matched_terms": matched_terms,
            "matched_units": choose_support_units(source_record, unit_scores, units_by_source),
            "score": {
                "lexical_source": lexical_source,
                "lexical_units": lexical_units,
                "metadata_bonus": metadata_bonus + unit_metadata_bonus,
                "channel_preference_bonus": channel_preference_bonus
                + sum(
                    float(item["score"].get("channel_preference_bonus", 0.0))
                    for item in unit_scores[:3]
                ),
                "reference_bonus": source_reference_bonus
                + sum(float(item["score"].get("reference_bonus", 0.0)) for item in unit_scores[:3]),
                "memory_bonus": memory_bonus,
                "graph_bonus": 0.0,
                "total": base_score,
            },
            "graph_expansions": [],
        }

    adjacency = build_graph_adjacency(graph_edges)
    for origin_id, result in list(base_scores.items()):
        origin_total = float(result["score"]["total"])
        if origin_total <= 0:
            continue
        if (
            float(result["score"]["lexical_source"]) + float(result["score"]["lexical_units"])
            <= 0.0
        ):
            continue
        queue: deque[tuple[str, int, float]] = deque([(origin_id, 0, origin_total)])
        visited: set[tuple[str, int]] = {(origin_id, 0)}
        while queue:
            current_id, hop, propagated_score = queue.popleft()
            if hop >= graph_hops:
                continue
            for edge in adjacency.get(current_id, []):
                neighbor = edge["neighbor"]
                if filtered_source_ids and neighbor not in filtered_source_ids:
                    continue
                neighbor_source = source_lookup.get(neighbor)
                if filtered_document_types and (
                    neighbor_source is None
                    or neighbor_source.get("document_type") not in filtered_document_types
                ):
                    continue
                if (neighbor, hop + 1) in visited:
                    continue
                visited.add((neighbor, hop + 1))
                bonus = (
                    propagated_score
                    * (0.15 / (hop + 1))
                    * GRAPH_STRENGTH_WEIGHTS.get(str(edge.get("strength")), 0.5)
                )
                if bonus <= 0:
                    continue
                neighbor_result = base_scores.setdefault(
                    neighbor,
                    {
                        "source_record": neighbor_source or {"source_id": neighbor},
                        "field_breakdown": {},
                        "matched_terms": [],
                        "matched_units": choose_support_units(
                            neighbor_source or {"source_id": neighbor},
                            [],
                            units_by_source,
                        ),
                        "score": {
                            "lexical_source": 0.0,
                            "lexical_units": 0.0,
                            "metadata_bonus": 0.0,
                            "graph_bonus": 0.0,
                            "total": 0.0,
                        },
                        "graph_expansions": [],
                    },
                )
                neighbor_result["score"]["graph_bonus"] += bonus
                neighbor_result["score"]["total"] += bonus
                neighbor_result["graph_expansions"].append(
                    {
                        "from_source_id": current_id,
                        "to_source_id": neighbor,
                        "relation_type": edge.get("relation_type"),
                        "strength": edge.get("strength"),
                        "status": edge.get("status"),
                        "hop": hop + 1,
                        "bonus": round(bonus, 3),
                        "citation_unit_ids": edge.get("citation_unit_ids", []),
                    }
                )
                queue.append((neighbor, hop + 1, bonus))

    results: list[dict[str, Any]] = []
    for source_id, result in base_scores.items():
        source_record = result["source_record"]
        support_units = result["matched_units"][:3]
        render_references = sorted(
            {
                reference
                for unit in support_units
                for reference in unit.get("render_references", [])
                if isinstance(reference, str)
            }
        )
        bundle = {
            "source_id": source_id,
            "document_type": source_record.get("document_type"),
            "support_tier": source_record.get("support_tier"),
            "source_extension": source_record.get("source_extension"),
            "source_origin": source_record.get("source_origin", "original-document"),
            "parent_source_id": source_record.get("parent_source_id"),
            "root_email_source_id": source_record.get("root_email_source_id"),
            "attachment_filename": source_record.get("attachment_filename"),
            "attachment_mime_type": source_record.get("attachment_mime_type"),
            "attachment_depth": source_record.get("attachment_depth"),
            "email_subject": source_record.get("email_subject"),
            "message_id": source_record.get("message_id"),
            "current_path": source_record.get("current_path"),
            "source_family": source_record.get("source_family", "corpus"),
            "trust_tier": source_record.get("trust_tier", "source"),
            "pending_promotion": bool(source_record.get("pending_promotion", False)),
            "memory_kind": source_record.get("memory_kind"),
            "durability": source_record.get("durability"),
            "uncertainty": source_record.get("uncertainty"),
            "answer_use_policy": source_record.get("answer_use_policy"),
            "retrieval_rank_prior": source_record.get("retrieval_rank_prior"),
            "title": source_record.get("title"),
            "summary_en": source_record.get("summary_en"),
            "available_channels": source_record.get("available_channels", []),
            "channel_descriptors": source_record.get("channel_descriptors", {}),
            "affordance_confidence": source_record.get("affordance_confidence"),
            "affordance_derivation_mode": source_record.get("affordance_derivation_mode"),
            "derived_affordance_path": source_record.get("derived_affordance_path"),
            "path_aliases": source_record.get("path_aliases", []),
            "title_aliases": source_record.get("title_aliases", []),
            "source_aliases": source_record.get("source_aliases", []),
            "warnings": source_record.get("warnings", []),
            "score": {key: round(float(value), 3) for key, value in result["score"].items()},
            "field_breakdown": {
                key: round(float(value), 3) for key, value in result["field_breakdown"].items()
            },
            "matched_terms": sorted(
                term for term in result["matched_terms"] if isinstance(term, str)
            ),
            "matched_units": support_units,
            "graph_expansions": sorted(
                result["graph_expansions"],
                key=lambda item: (
                    int(item["hop"]),
                    -float(item["bonus"]),
                    str(item["to_source_id"]),
                ),
            ),
            "render_references": render_references if include_renders else [],
        }
        if bundle["score"]["total"] > 0:
            results.append(bundle)

    results.sort(
        key=lambda item: (
            -float(item["score"]["total"]),
            -float(item["score"]["lexical_source"]),
            str(item["source_id"]),
        )
    )
    trimmed_results = results[:top]
    published_evidence_plan = plan_published_evidence(
        results=trimmed_results,
        evidence_requirements=effective_evidence_requirements,
    )
    return {
        "query": query,
        "results": trimmed_results,
        "result_count": len(results),
        "strategy": {
            "strategy_id": RETRIEVAL_STRATEGY_ID,
            "mode": "lexical-plus-graph",
            "graph_hops": graph_hops,
            "question_domain": effective_question_domain,
            "memory_profile": memory_profile,
            "field_weights": FIELD_WEIGHTS,
            "graph_strength_weights": GRAPH_STRENGTH_WEIGHTS,
        },
        "corpus_signature": retrieval_data["manifest"].get("source_signature"),
        "evidence_requirements": effective_evidence_requirements,
        **published_evidence_plan,
    }


def log_query_session(
    paths: WorkspacePaths,
    *,
    session_id: str,
    command: str,
    payload: dict[str, Any],
) -> None:
    """Persist a query-session log and append a usage history event."""
    ensure_log_directories(paths)
    session_path = paths.query_sessions_dir / f"{session_id}.json"
    write_json(session_path, payload)
    append_jsonl(
        paths.usage_history_path,
        {
            "recorded_at": utc_now(),
            "event_type": "query-session",
            "command": command,
            "session_id": session_id,
            "status": payload.get("status"),
        },
    )
    refresh_runtime_projections(paths)


def log_trace_record(
    paths: WorkspacePaths,
    *,
    trace_id: str,
    payload: dict[str, Any],
) -> None:
    """Persist a trace log and append a usage history event."""
    ensure_log_directories(paths)
    trace_path = paths.retrieval_traces_dir / f"{trace_id}.json"
    write_json(trace_path, payload)
    append_jsonl(
        paths.usage_history_path,
        {
            "recorded_at": utc_now(),
            "event_type": "retrieval-trace",
            "trace_id": trace_id,
            "status": payload.get("status"),
            "trace_mode": payload.get("trace_mode"),
        },
    )
    refresh_runtime_projections(paths)


def _enrich_log_payload(
    payload: dict[str, Any],
    *,
    log_context: dict[str, str] | None,
    answer_file_path: str | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Attach optional conversation and workflow linkage metadata to a log payload."""
    enriched = dict(payload)
    if log_context:
        for field_name in LOG_CONTEXT_FIELD_NAMES:
            value = log_context.get(field_name)
            if isinstance(value, str) and value:
                enriched[field_name] = value
    if answer_file_path is not None:
        enriched["answer_file_path"] = answer_file_path
    if isinstance(log_origin, str) and log_origin:
        enriched["log_origin"] = log_origin
    return enriched


def _log_context_from_env() -> dict[str, str] | None:
    """Read optional conversation linkage fields from the environment."""
    context = {
        field_name: os.environ.get(f"DOCMASON_{field_name.upper()}", "")
        for field_name in LOG_CONTEXT_FIELD_NAMES
    }
    normalized = {
        key: value.strip()
        for key, value in context.items()
        if isinstance(value, str) and value.strip()
    }
    return normalized or None


def _merge_log_context(
    *,
    explicit_log_context: dict[str, str] | None,
    fallback_record: dict[str, Any] | None,
) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    if fallback_record:
        for field_name in (
            "conversation_id",
            "turn_id",
            "entry_workflow_id",
            "inner_workflow_id",
            "native_turn_id",
        ):
            value = fallback_record.get(field_name)
            if isinstance(value, str) and value:
                merged[field_name] = value
        merged.update(semantic_log_context_from_record(fallback_record))
    if explicit_log_context:
        for field_name in LOG_CONTEXT_FIELD_NAMES:
            value = explicit_log_context.get(field_name)
            if isinstance(value, str) and value:
                merged[field_name] = value
    return merged or None


def _log_origin_from_env() -> str | None:
    value = os.environ.get("DOCMASON_LOG_ORIGIN")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _effective_log_origin(
    *,
    log_context: dict[str, str] | None,
    explicit_log_origin: str | None,
) -> str:
    if isinstance(explicit_log_origin, str) and explicit_log_origin:
        return explicit_log_origin
    if log_context and log_context.get("entry_workflow_id") == "ask":
        return "interactive-ask"
    if log_context:
        return "workflow-linked"
    return "direct-command"


def retrieve_corpus(
    paths: WorkspacePaths,
    *,
    query: str,
    top: int,
    graph_hops: int,
    document_types: list[str] | None,
    source_ids: list[str] | None,
    include_renders: bool,
    target: str = "current",
    write_logs: bool = True,
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
    question_domain: str | None = None,
    evidence_requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run retrieval over a knowledge-base target and optionally log the session."""
    effective_log_context = log_context or _log_context_from_env()
    effective_log_origin = _effective_log_origin(
        log_context=effective_log_context,
        explicit_log_origin=log_origin or _log_origin_from_env(),
    )
    effective_question_domain = question_domain or (
        str(effective_log_context.get("question_domain"))
        if effective_log_context and effective_log_context.get("question_domain")
        else None
    )
    memory_profile = infer_memory_query_profile(query, question_domain=effective_question_domain)
    retrieval_data = load_retrieval_data(paths, target=target)
    target_root = paths.knowledge_target_dir(target)
    retrieval_data["graph_edges"] = read_json(target_root / "graph_edges.json").get("edges", [])
    retrieval_data["manifest"]["target_root"] = str(target_root)
    if target == "current" and should_merge_pending_interaction(
        effective_question_domain,
        memory_profile=memory_profile,
    ):
        retrieval_data = merge_pending_interaction_overlay(paths, retrieval_data)
    reference_resolution = resolve_reference_query(
        query,
        source_records=retrieval_data["source_records"],
        unit_records=retrieval_data["unit_records"],
    )
    effective_source_ids = _effective_source_ids_from_reference(
        source_ids,
        reference_resolution,
    )
    payload = run_retrieval_query(
        retrieval_data,
        query=query,
        top=top,
        graph_hops=graph_hops,
        document_types=document_types,
        source_ids=effective_source_ids,
        include_renders=include_renders,
        question_domain=effective_question_domain,
        evidence_requirements=evidence_requirements,
        reference_resolution=reference_resolution,
    )
    session_id = str(uuid.uuid4())
    payload["session_id"] = session_id
    payload["status"] = "ready" if payload["results"] else "no-results"
    payload["target"] = target
    payload["question_domain"] = effective_question_domain
    payload["reference_resolution"] = reference_resolution
    payload["reference_resolution_summary"] = build_reference_resolution_summary(
        reference_resolution
    )
    payload["filters"] = {
        "document_types": document_types or [],
        "source_ids": effective_source_ids,
    }
    if write_logs:
        consulted_results = [
            {
                "source_id": result["source_id"],
                "source_family": result.get("source_family"),
                "trust_tier": result.get("trust_tier"),
                "pending_promotion": result.get("pending_promotion", False),
                "memory_kind": result.get("memory_kind"),
                "uncertainty": result.get("uncertainty"),
                "answer_use_policy": result.get("answer_use_policy"),
                "available_channels": result.get("available_channels", []),
                "score": result["score"],
                "matched_unit_ids": [
                    unit["unit_id"]
                    for unit in result.get("matched_units", [])
                    if unit.get("unit_id")
                ],
            }
            for result in payload["results"]
        ]
        log_query_session(
            paths,
            session_id=session_id,
            command="retrieve",
            payload=_enrich_log_payload(
                {
                    "recorded_at": utc_now(),
                    "command": "retrieve",
                    "status": payload["status"],
                    "target": target,
                    "query": query,
                    "session_id": session_id,
                    "question_domain": effective_question_domain,
                    "preferred_channels": payload.get("preferred_channels", []),
                    "inspection_scope": payload.get("inspection_scope"),
                    "matched_published_channels": payload.get("matched_published_channels", []),
                    "published_artifacts_sufficient": payload.get(
                        "published_artifacts_sufficient"
                    ),
                    "reference_resolution": reference_resolution,
                    "reference_resolution_summary": payload.get(
                        "reference_resolution_summary"
                    ),
                    "source_escalation_required": payload.get("source_escalation_required"),
                    "source_escalation_reason": payload.get("source_escalation_reason"),
                    "corpus_signature": payload.get("corpus_signature"),
                    "strategy": payload["strategy"],
                    "filters": payload["filters"],
                    "consulted_results": consulted_results,
                },
                log_context=effective_log_context,
                log_origin=effective_log_origin,
            ),
        )
    return payload


def build_segment_supports(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Return compact support data from a retrieval result for answer grounding."""
    supports: list[dict[str, Any]] = []
    for unit in result.get("matched_units", []):
        supports.append(
            {
                "source_id": result.get("source_id"),
                "source_family": result.get("source_family"),
                "trust_tier": result.get("trust_tier"),
                "pending_promotion": result.get("pending_promotion", False),
                "memory_kind": result.get("memory_kind"),
                "uncertainty": result.get("uncertainty"),
                "answer_use_policy": result.get("answer_use_policy"),
                "source_warnings": result.get("warnings", []),
                "unit_id": unit.get("unit_id"),
                "title": unit.get("title"),
                "score": unit.get("score"),
                "render_references": unit.get("render_references", []),
                "embedded_media": unit.get("embedded_media", []),
                "structure_asset": unit.get("structure_asset"),
                "available_channels": unit.get("available_channels", []),
                "channel_descriptors": unit.get("channel_descriptors", {}),
                "affordance_confidence": unit.get("affordance_confidence"),
                "affordance_derivation_mode": unit.get("affordance_derivation_mode"),
                "extraction_confidence": unit.get("extraction_confidence"),
                "warnings": unit.get("warnings", []),
                "text_excerpt": unit.get("text_excerpt"),
            }
        )
    return supports


def build_segment_supports_from_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collect compact support data across multiple retrieval results."""
    supports: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for result in results:
        for support in build_segment_supports(result):
            source_id = support.get("source_id")
            unit_id = support.get("unit_id")
            if not isinstance(source_id, str) or not isinstance(unit_id, str):
                continue
            key = (source_id, unit_id)
            if key in seen:
                continue
            seen.add(key)
            supports.append(support)
    return supports


def deduplicate_strings(values: list[str]) -> list[str]:
    """Deduplicate non-empty strings while preserving order."""
    return list(dict.fromkeys(value for value in values if value))


def compact_support_ids(results: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Build compact supporting source and unit identifiers for trace payloads."""
    source_ids: list[str] = []
    unit_ids: list[str] = []
    for result in results:
        source_id = result.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            continue
        source_ids.append(source_id)
        for unit in result.get("matched_units", []):
            if not isinstance(unit, dict):
                continue
            unit_id = unit.get("unit_id")
            if isinstance(unit_id, str) and unit_id:
                unit_ids.append(f"{source_id}:{unit_id}")
    return deduplicate_strings(source_ids), deduplicate_strings(unit_ids)


def needs_render_inspection_from_supports(
    supports: list[dict[str, Any]],
    *,
    preferred_channels: list[str] | None = None,
) -> bool:
    """Return whether the selected supports should trigger render inspection."""
    if not supports:
        return False
    preferred = {
        channel for channel in (preferred_channels or []) if isinstance(channel, str) and channel
    }
    if "render" in preferred and any(support.get("render_references") for support in supports):
        return True
    if "media" in preferred and any(support.get("embedded_media") for support in supports):
        return True
    for support in supports:
        if support.get("extraction_confidence") not in {None, "high"}:
            return True
        if support.get("render_references") and not str(support.get("text_excerpt", "")).strip():
            return True
    return False


def significant_query_terms(text: str) -> set[str]:
    """Return the subset of query tokens that should be semantically covered."""
    terms: set[str] = set()
    for token in tokenize_text(text):
        if token in GROUNDING_STOPWORDS:
            continue
        if token.isdigit() or len(token) >= 2:
            terms.add(token)
    return terms


def support_term_coverage(segment_text: str, result: dict[str, Any] | None) -> float:
    """Measure how much of a segment's significant vocabulary is covered by the top result."""
    significant_terms = significant_query_terms(segment_text)
    if not significant_terms or result is None:
        return 0.0
    matched_terms = {
        term for term in result.get("matched_terms", []) if isinstance(term, str) and term
    }
    covered_terms = significant_terms & matched_terms
    return len(covered_terms) / len(significant_terms)


def groundedness_from_result(result: dict[str, Any] | None, *, segment_text: str) -> str:
    """Classify answer grounding from the strongest retrieval result.

    The score thresholds alone are not sufficient: a segment can share domain words with the
    corpus while still making unsupported claims. Guard against that by requiring the top result to
    cover a strong share of the segment's significant vocabulary before upgrading the segment to
    `grounded`.
    """
    if not result:
        return "unresolved"
    total = float(result["score"]["total"])
    lexical = float(result["score"]["lexical_source"]) + float(result["score"]["lexical_units"])
    matched_units = len(result.get("matched_units", []))
    coverage = support_term_coverage(segment_text, result)
    if total >= 6.0 and (lexical >= 3.0 or matched_units > 0) and coverage >= 0.75:
        return "grounded"
    if total >= 6.0 and coverage >= 0.35:
        return "partially-grounded"
    if total >= 2.5 and coverage >= 0.35:
        return "partially-grounded"
    return "unresolved"


def answer_state_from_segments(segment_traces: list[dict[str, Any]]) -> str:
    """Collapse segment grounding states into the Phase 4b answer-state contract."""
    if not segment_traces:
        return "unresolved"
    grounding_states = {
        str(segment.get("grounding_status"))
        for segment in segment_traces
        if isinstance(segment, dict)
    }
    if grounding_states == {"grounded"}:
        return "grounded"
    if grounding_states & {"grounded", "partially-grounded"}:
        return "partially-grounded"
    return "unresolved"


def detected_abstention(answer_text: str) -> bool:
    """Return whether the answer text explicitly states a refusal or insufficiency boundary."""
    normalized = " ".join(answer_text.strip().lower().split())
    if not normalized:
        return False
    return any(marker in normalized for marker in ABSTENTION_MARKERS)


def final_answer_state(
    *,
    kb_answer_state: str,
    answer_text: str,
    support_basis: str | None,
    support_manifest_path: str | None,
    declared_answer_state: str | None,
) -> str:
    """Resolve the final four-state answer contract for newly written artifacts."""
    if declared_answer_state is not None:
        if declared_answer_state not in ANSWER_STATES:
            raise ValueError(f"Unsupported declared answer_state `{declared_answer_state}`.")
        return declared_answer_state
    if detected_abstention(answer_text):
        return "abstained"
    if support_basis == "external-source-verified" and support_manifest_path:
        return "grounded"
    if support_basis == "mixed" and support_manifest_path and kb_answer_state == "unresolved":
        return "partially-grounded"
    return kb_answer_state


def render_inspection_required_from_segments(segment_traces: list[dict[str, Any]]) -> bool:
    """Return whether any answer segment still requires render inspection."""
    return any(bool(segment.get("needs_render_inspection")) for segment in segment_traces)


def supporting_ids_from_segments(
    segment_traces: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Collect compact supporting source and unit identifiers across all segments."""
    source_ids: list[str] = []
    unit_ids: list[str] = []
    for segment in segment_traces:
        if not isinstance(segment, dict):
            continue
        if isinstance(segment.get("supporting_source_ids"), list):
            source_ids.extend(
                value for value in segment["supporting_source_ids"] if isinstance(value, str)
            )
        if isinstance(segment.get("supporting_unit_ids"), list):
            unit_ids.extend(
                value for value in segment["supporting_unit_ids"] if isinstance(value, str)
            )
    return deduplicate_strings(source_ids), deduplicate_strings(unit_ids)


def segment_answer_text(answer_text: str) -> list[str]:
    """Split answer text into compact grounding segments."""
    segments: list[str] = []
    for paragraph in [value.strip() for value in answer_text.split("\n\n") if value.strip()]:
        if len(paragraph) <= 320:
            segments.append(paragraph)
            continue
        for sentence in [
            value.strip() for value in SENTENCE_SPLIT_PATTERN.split(paragraph) if value.strip()
        ]:
            segments.append(sentence)
    return segments


def trace_source(
    paths: WorkspacePaths,
    *,
    source_id: str,
    unit_id: str | None,
    target: str = "current",
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Trace a source or evidence unit back to provenance artifacts."""
    effective_log_context = log_context or _log_context_from_env()
    effective_log_origin = _effective_log_origin(
        log_context=effective_log_context,
        explicit_log_origin=log_origin or _log_origin_from_env(),
    )
    trace_data = load_trace_data(paths, target=target)
    if target == "current":
        trace_data = merge_pending_interaction_trace(paths, trace_data)
    source_provenance = trace_data["source_provenance"]
    if source_id not in source_provenance:
        raise KeyError(source_id)
    trace_id = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "recorded_at": utc_now(),
        "trace_id": trace_id,
        "trace_mode": "citation-first",
        "target": target,
        "source": source_provenance[source_id],
        "status": "ready",
    }
    if unit_id is not None:
        unit_key = f"{source_id}:{unit_id}"
        unit_data = trace_data["unit_provenance"].get(unit_key)
        if not isinstance(unit_data, dict):
            raise KeyError(unit_key)
        payload["unit"] = unit_data
    payload = _enrich_log_payload(
        payload,
        log_context=effective_log_context,
        log_origin=effective_log_origin,
    )
    log_trace_record(paths, trace_id=trace_id, payload=payload)
    return payload


def trace_answer_text(
    paths: WorkspacePaths,
    *,
    answer_text: str,
    top: int,
    target: str = "current",
    session_id: str | None = None,
    log_context: dict[str, str] | None = None,
    answer_file_path: str | None = None,
    log_origin: str | None = None,
    question_domain: str | None = None,
    support_basis: str | None = None,
    support_manifest_path: str | None = None,
    evidence_requirements: dict[str, Any] | None = None,
    preferred_channels: list[str] | None = None,
    inspection_scope: str | None = None,
    prefer_published_artifacts: bool | None = None,
    declared_answer_state: str | None = None,
    reference_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trace free-form answer text back to knowledge-base evidence."""
    effective_log_context = log_context or _log_context_from_env()
    effective_log_origin = _effective_log_origin(
        log_context=effective_log_context,
        explicit_log_origin=log_origin or _log_origin_from_env(),
    )
    answer_text = answer_text.strip()
    if not answer_text:
        raise ValueError("Answer text is empty.")
    effective_question_domain = question_domain or (
        str(effective_log_context.get("question_domain"))
        if effective_log_context and effective_log_context.get("question_domain")
        else None
    )
    effective_support_basis = support_basis or (
        str(effective_log_context.get("support_basis"))
        if effective_log_context and effective_log_context.get("support_basis")
        else None
    )
    effective_support_manifest_path = support_manifest_path or (
        str(effective_log_context.get("support_manifest_path"))
        if effective_log_context and effective_log_context.get("support_manifest_path")
        else None
    )
    support_manifest = load_support_manifest(
        paths,
        support_manifest_path_value=effective_support_manifest_path,
    )
    if support_manifest and not effective_support_basis:
        manifest_support_basis = support_manifest.get("support_basis")
        if isinstance(manifest_support_basis, str) and manifest_support_basis:
            effective_support_basis = manifest_support_basis
    effective_evidence_requirements = _effective_evidence_requirements(
        {
            **(evidence_requirements or {}),
            **(
                {"preferred_channels": preferred_channels}
                if isinstance(preferred_channels, list)
                else {}
            ),
            **({"inspection_scope": inspection_scope} if isinstance(inspection_scope, str) else {}),
            **(
                {"prefer_published_artifacts": prefer_published_artifacts}
                if isinstance(prefer_published_artifacts, bool)
                else {}
            ),
        },
        question_domain=effective_question_domain,
    )
    effective_reference_resolution = (
        dict(reference_resolution) if isinstance(reference_resolution, dict) else None
    )

    recorded_at = utc_now()
    segment_traces: list[dict[str, Any]] = []
    consulted_results: list[dict[str, Any]] = []
    for index, segment in enumerate(segment_answer_text(answer_text), start=1):
        try:
            retrieval_payload = retrieve_corpus(
                paths,
                query=segment,
                top=top,
                graph_hops=1,
                document_types=None,
                source_ids=None,
                include_renders=True,
                target=target,
                write_logs=False,
                log_context=effective_log_context,
                log_origin=effective_log_origin,
                question_domain=effective_question_domain,
                evidence_requirements=effective_evidence_requirements,
            )
            results = retrieval_payload["results"]
        except FileNotFoundError:
            if effective_support_basis not in {
                "external-source-verified",
                "model-knowledge",
                "mixed",
            }:
                raise
            results = []
        top_result = results[0] if results else None
        supports = build_segment_supports_from_results(results[:top])
        supporting_source_ids, supporting_unit_ids = compact_support_ids(results[:top])
        segment_trace = {
            "segment_index": index,
            "segment_text": segment,
            "grounding_status": groundedness_from_result(top_result, segment_text=segment),
            "needs_render_inspection": needs_render_inspection_from_supports(
                supports,
                preferred_channels=list(
                    effective_evidence_requirements.get("preferred_channels", [])
                ),
            ),
            "supporting_source_ids": supporting_source_ids,
            "supporting_unit_ids": supporting_unit_ids,
            "supporting_results": results[:top],
            "supporting_units": supports,
        }
        consulted_results.append(
            {
                "segment_index": index,
                "query": segment,
                "results": [
                    {
                        "source_id": result["source_id"],
                        "source_family": result.get("source_family"),
                        "trust_tier": result.get("trust_tier"),
                        "pending_promotion": result.get("pending_promotion", False),
                        "score": result["score"],
                        "matched_unit_ids": [
                            unit["unit_id"]
                            for unit in result.get("matched_units", [])
                            if unit.get("unit_id")
                        ],
                    }
                    for result in results[:top]
                ],
            }
        )
        segment_traces.append(segment_trace)

    kb_answer_state = answer_state_from_segments(segment_traces)
    answer_state = final_answer_state(
        kb_answer_state=kb_answer_state,
        answer_text=answer_text,
        support_basis=effective_support_basis,
        support_manifest_path=effective_support_manifest_path,
        declared_answer_state=declared_answer_state,
    )
    kb_render_required = render_inspection_required_from_segments(segment_traces)
    supporting_source_ids, supporting_unit_ids = supporting_ids_from_segments(segment_traces)
    all_supports = [
        support
        for segment in segment_traces
        for support in segment.get("supporting_units", [])
        if isinstance(support, dict)
    ]
    used_published_channels = support_channels_from_supports(all_supports)
    published_evidence_plan = plan_published_evidence(
        results=[
            result
            for segment in segment_traces
            for result in segment.get("supporting_results", [])
            if isinstance(result, dict)
        ],
        evidence_requirements=effective_evidence_requirements,
    )
    render_inspection_required = combined_render_requirement(
        kb_render_required=kb_render_required,
        support_basis=effective_support_basis,
        answer_state=answer_state,
        support_manifest_path=effective_support_manifest_path,
    )
    status = combined_trace_status(
        answer_state=answer_state,
        support_basis=effective_support_basis,
        support_manifest_path=effective_support_manifest_path,
    )

    session_value = session_id or str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    result = {
        "recorded_at": recorded_at,
        "trace_id": trace_id,
        "session_id": session_value,
        "trace_mode": "answer-first",
        "target": target,
        "status": status,
        "answer_workflow_id": ANSWER_WORKFLOW_ID,
        "answer_state": answer_state,
        "kb_answer_state": kb_answer_state,
        "question_domain": effective_question_domain,
        "support_basis": effective_support_basis,
        "support_manifest_path": effective_support_manifest_path,
        "inspection_scope": published_evidence_plan.get("inspection_scope"),
        "preferred_channels": published_evidence_plan.get("preferred_channels", []),
        "used_published_channels": used_published_channels,
        "matched_published_channels": published_evidence_plan.get(
            "matched_published_channels",
            [],
        ),
        "published_artifacts_sufficient": published_evidence_plan.get(
            "published_artifacts_sufficient"
        ),
        "reference_resolution": effective_reference_resolution,
        "reference_resolution_summary": build_reference_resolution_summary(
            effective_reference_resolution
        ),
        "source_escalation_required": published_evidence_plan.get(
            "source_escalation_required"
        ),
        "source_escalation_reason": published_evidence_plan.get("source_escalation_reason"),
        "render_inspection_required": render_inspection_required,
        "supporting_source_ids": supporting_source_ids,
        "supporting_unit_ids": supporting_unit_ids,
        "answer_text": answer_text,
        "segments": segment_traces,
        "segment_count": len(segment_traces),
        "grounding_summary": {
            "grounded": sum(
                1 for segment in segment_traces if segment["grounding_status"] == "grounded"
            ),
            "partially_grounded": sum(
                1
                for segment in segment_traces
                if segment["grounding_status"] == "partially-grounded"
            ),
            "unresolved": sum(
                1 for segment in segment_traces if segment["grounding_status"] == "unresolved"
            ),
        },
    }
    result = _enrich_log_payload(
        result,
        log_context=effective_log_context,
        answer_file_path=answer_file_path,
        log_origin=effective_log_origin,
    )
    log_query_session(
        paths,
        session_id=session_value,
        command="trace",
        payload=_enrich_log_payload(
            {
                "recorded_at": recorded_at,
                "command": "trace",
                "status": status,
                "target": target,
                "session_id": session_value,
                "trace_id": trace_id,
                "answer_workflow_id": ANSWER_WORKFLOW_ID,
                "answer_state": answer_state,
                "kb_answer_state": kb_answer_state,
                "question_domain": effective_question_domain,
                "support_basis": effective_support_basis,
                "support_manifest_path": effective_support_manifest_path,
                "inspection_scope": published_evidence_plan.get("inspection_scope"),
                "preferred_channels": published_evidence_plan.get("preferred_channels", []),
                "used_published_channels": used_published_channels,
                "matched_published_channels": published_evidence_plan.get(
                    "matched_published_channels",
                    [],
                ),
                "published_artifacts_sufficient": published_evidence_plan.get(
                    "published_artifacts_sufficient"
                ),
                "reference_resolution": effective_reference_resolution,
                "reference_resolution_summary": build_reference_resolution_summary(
                    effective_reference_resolution
                ),
                "source_escalation_required": published_evidence_plan.get(
                    "source_escalation_required"
                ),
                "source_escalation_reason": published_evidence_plan.get(
                    "source_escalation_reason"
                ),
                "render_inspection_required": render_inspection_required,
                "supporting_source_ids": supporting_source_ids,
                "supporting_unit_ids": supporting_unit_ids,
                "final_answer": answer_text,
                "segment_traces": segment_traces,
                "consulted_results": consulted_results,
            },
            log_context=effective_log_context,
            answer_file_path=answer_file_path,
            log_origin=effective_log_origin,
        ),
    )
    log_trace_record(paths, trace_id=trace_id, payload=result)
    return result


def trace_answer_file(
    paths: WorkspacePaths,
    *,
    answer_file: Path,
    top: int,
    target: str = "current",
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
    declared_answer_state: str | None = None,
) -> dict[str, Any]:
    """Trace the contents of an answer file back to corpus evidence."""
    try:
        answer_file_reference = str(answer_file.relative_to(paths.root))
    except ValueError:
        answer_file_reference = str(answer_file)
    turn_record = _turn_record_from_answer_file(paths, answer_file_path=answer_file_reference)
    effective_log_context = _merge_log_context(
        explicit_log_context=log_context,
        fallback_record=turn_record,
    )
    return trace_answer_text(
        paths,
        answer_text=answer_file.read_text(encoding="utf-8"),
        top=top,
        target=target,
        log_context=effective_log_context,
        answer_file_path=answer_file_reference,
        log_origin=log_origin,
        question_domain=turn_record.get("question_domain")
        if isinstance(turn_record.get("question_domain"), str)
        else None,
        support_basis=turn_record.get("support_basis")
        if isinstance(turn_record.get("support_basis"), str)
        else None,
        support_manifest_path=turn_record.get("support_manifest_path")
        if isinstance(turn_record.get("support_manifest_path"), str)
        else None,
        evidence_requirements=(
            turn_record.get("semantic_analysis", {}).get("evidence_requirements")
            if isinstance(turn_record.get("semantic_analysis"), dict)
            else None
        ),
        preferred_channels=(
            turn_record.get("preferred_channels")
            if isinstance(turn_record.get("preferred_channels"), list)
            else None
        ),
        inspection_scope=turn_record.get("inspection_scope")
        if isinstance(turn_record.get("inspection_scope"), str)
        else None,
        reference_resolution=turn_record.get("reference_resolution")
        if isinstance(turn_record.get("reference_resolution"), dict)
        else None,
        declared_answer_state=declared_answer_state
        or (
            turn_record.get("answer_state")
            if isinstance(turn_record.get("answer_state"), str)
            else None
        ),
    )


def trace_session(
    paths: WorkspacePaths,
    *,
    session_id: str,
    top: int,
    target: str = "current",
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
    declared_answer_state: str | None = None,
) -> dict[str, Any]:
    """Trace a previously recorded answer session."""
    session_path = paths.query_sessions_dir / f"{session_id}.json"
    session_payload = read_json(session_path)
    if not session_payload:
        raise FileNotFoundError(session_path)
    effective_log_context = _merge_log_context(
        explicit_log_context=log_context or _log_context_from_env(),
        fallback_record=session_payload,
    )
    effective_log_origin = _effective_log_origin(
        log_context=effective_log_context,
        explicit_log_origin=log_origin or _log_origin_from_env(),
    )
    if isinstance(session_payload.get("segment_traces"), list) and session_payload.get(
        "final_answer"
    ):
        segment_traces = [
            segment for segment in session_payload["segment_traces"] if isinstance(segment, dict)
        ]
        answer_state = session_payload.get("answer_state")
        if not isinstance(answer_state, str):
            answer_state = final_answer_state(
                kb_answer_state=answer_state_from_segments(segment_traces),
                answer_text=str(session_payload.get("final_answer") or ""),
                support_basis=(
                    str(session_payload.get("support_basis"))
                    if isinstance(session_payload.get("support_basis"), str)
                    else None
                ),
                support_manifest_path=(
                    str(session_payload.get("support_manifest_path"))
                    if isinstance(session_payload.get("support_manifest_path"), str)
                    else None
                ),
                declared_answer_state=declared_answer_state,
            )
        kb_render_required = session_payload.get("render_inspection_required")
        if not isinstance(kb_render_required, bool):
            kb_render_required = render_inspection_required_from_segments(segment_traces)
        support_basis = (
            str(session_payload.get("support_basis"))
            if isinstance(session_payload.get("support_basis"), str)
            else None
        )
        support_manifest_path = (
            str(session_payload.get("support_manifest_path"))
            if isinstance(session_payload.get("support_manifest_path"), str)
            else None
        )
        render_inspection_required = combined_render_requirement(
            kb_render_required=kb_render_required,
            support_basis=support_basis,
            answer_state=answer_state,
            support_manifest_path=support_manifest_path,
        )
        effective_evidence_requirements = _effective_evidence_requirements(
            (
                session_payload.get("semantic_analysis", {}).get("evidence_requirements")
                if isinstance(session_payload.get("semantic_analysis"), dict)
                else {
                    "preferred_channels": session_payload.get("preferred_channels", []),
                    "inspection_scope": session_payload.get("inspection_scope"),
                }
            ),
            question_domain=session_payload.get("question_domain")
            if isinstance(session_payload.get("question_domain"), str)
            else None,
        )
        all_supports = [
            support
            for segment in segment_traces
            for support in segment.get("supporting_units", [])
            if isinstance(support, dict)
        ]
        used_published_channels = support_channels_from_supports(all_supports)
        published_evidence_plan = plan_published_evidence(
            results=[
                result
                for segment in segment_traces
                for result in segment.get("supporting_results", [])
                if isinstance(result, dict)
            ],
            evidence_requirements=effective_evidence_requirements,
        )
        supporting_source_ids = session_payload.get("supporting_source_ids")
        supporting_unit_ids = session_payload.get("supporting_unit_ids")
        if not isinstance(supporting_source_ids, list) or not isinstance(
            supporting_unit_ids,
            list,
        ):
            supporting_source_ids, supporting_unit_ids = supporting_ids_from_segments(
                segment_traces
            )
        trace_id = str(uuid.uuid4())
        result = {
            "recorded_at": utc_now(),
            "trace_id": trace_id,
            "session_id": session_id,
            "trace_mode": "answer-first",
            "target": target,
            "status": combined_trace_status(
                answer_state=answer_state,
                support_basis=support_basis,
                support_manifest_path=support_manifest_path,
            ),
            "answer_workflow_id": ANSWER_WORKFLOW_ID,
            "answer_state": answer_state,
            "kb_answer_state": answer_state_from_segments(segment_traces),
            "question_class": session_payload.get("question_class"),
            "question_domain": session_payload.get("question_domain"),
            "support_strategy": session_payload.get("support_strategy"),
            "analysis_origin": session_payload.get("analysis_origin"),
            "support_basis": support_basis,
            "support_manifest_path": support_manifest_path,
            "inspection_scope": published_evidence_plan.get("inspection_scope"),
            "preferred_channels": published_evidence_plan.get("preferred_channels", []),
            "used_published_channels": used_published_channels,
            "matched_published_channels": published_evidence_plan.get(
                "matched_published_channels",
                [],
            ),
            "published_artifacts_sufficient": published_evidence_plan.get(
                "published_artifacts_sufficient"
            ),
            "reference_resolution": (
                session_payload.get("reference_resolution")
                if isinstance(session_payload.get("reference_resolution"), dict)
                else None
            ),
            "reference_resolution_summary": build_reference_resolution_summary(
                session_payload.get("reference_resolution")
                if isinstance(session_payload.get("reference_resolution"), dict)
                else None
            ),
            "source_escalation_required": published_evidence_plan.get(
                "source_escalation_required"
            ),
            "source_escalation_reason": published_evidence_plan.get("source_escalation_reason"),
            "render_inspection_required": render_inspection_required,
            "supporting_source_ids": supporting_source_ids,
            "supporting_unit_ids": supporting_unit_ids,
            "answer_text": session_payload.get("final_answer"),
            "segments": segment_traces,
            "segment_count": len(segment_traces),
            "grounding_summary": {
                "grounded": sum(
                    1 for segment in segment_traces if segment.get("grounding_status") == "grounded"
                ),
                "partially_grounded": sum(
                    1
                    for segment in segment_traces
                    if segment.get("grounding_status") == "partially-grounded"
                ),
                "unresolved": sum(
                    1
                    for segment in segment_traces
                    if segment.get("grounding_status") == "unresolved"
                ),
            },
            "reused_session": True,
        }
        result = _enrich_log_payload(
            result,
            log_context=effective_log_context,
            log_origin=effective_log_origin,
        )
        log_trace_record(paths, trace_id=trace_id, payload=result)
        return result
    final_answer = session_payload.get("final_answer")
    if not isinstance(final_answer, str) or not final_answer.strip():
        raise ValueError(
            f"Session `{session_id}` does not contain a reusable final answer for tracing."
        )
    return trace_answer_text(
        paths,
        answer_text=final_answer,
        top=top,
        target=target,
        session_id=session_id,
        log_context=effective_log_context,
        answer_file_path=session_payload.get("answer_file_path"),
        log_origin=effective_log_origin,
        question_domain=session_payload.get("question_domain")
        if isinstance(session_payload.get("question_domain"), str)
        else None,
        support_basis=session_payload.get("support_basis")
        if isinstance(session_payload.get("support_basis"), str)
        else None,
        reference_resolution=session_payload.get("reference_resolution")
        if isinstance(session_payload.get("reference_resolution"), dict)
        else None,
        support_manifest_path=session_payload.get("support_manifest_path")
        if isinstance(session_payload.get("support_manifest_path"), str)
        else None,
        evidence_requirements=(
            session_payload.get("semantic_analysis", {}).get("evidence_requirements")
            if isinstance(session_payload.get("semantic_analysis"), dict)
            else None
        ),
        preferred_channels=(
            session_payload.get("preferred_channels")
            if isinstance(session_payload.get("preferred_channels"), list)
            else None
        ),
        inspection_scope=session_payload.get("inspection_scope")
        if isinstance(session_payload.get("inspection_scope"), str)
        else None,
        declared_answer_state=declared_answer_state
        or (
            session_payload.get("answer_state")
            if isinstance(session_payload.get("answer_state"), str)
            else None
        ),
    )

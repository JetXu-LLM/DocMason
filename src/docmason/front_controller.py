"""Reusable ask front-controller helpers for routing and artifact allocation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project import WorkspacePaths, read_json, write_json
from .routing import (
    normalize_question_analysis,
    tokenize_text,
)


def ensure_turn_bundle(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    question_class: str,
) -> list[str]:
    """Create the canonical composition bundle scaffold when the turn needs one."""
    if question_class != "composition":
        return []
    bundle_dir = paths.agent_work_dir / conversation_id / turn_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = bundle_dir / "bundle-manifest.json"
    notes_path = bundle_dir / "research-notes.md"
    if not manifest_path.exists():
        write_json(
            manifest_path,
            {
                "bundle_type": "grounded-composition",
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "artifacts": [
                    "bundle-manifest.json",
                    "research-notes.md",
                ],
                "draft_artifact": None,
            },
        )
    if not notes_path.exists():
        notes_path.write_text("# Research Notes\n\n", encoding="utf-8")
    return [str(bundle_dir.relative_to(paths.root))]


def support_manifest_path(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
) -> Path:
    """Return the canonical runtime path for one external support manifest."""
    return paths.agent_work_dir / conversation_id / turn_id / "external-support-manifest.json"


def hybrid_refresh_work_path(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
) -> Path:
    """Return the canonical runtime path for one narrowed hybrid-refresh packet."""
    return paths.agent_work_dir / conversation_id / turn_id / "hybrid_refresh_work.json"


def write_hybrid_refresh_work(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    query: str,
    source_ids: list[str],
    recommended_targets: list[dict[str, Any]] | None = None,
    target: str = "current",
) -> str:
    """Persist the narrowed Lane C work packet for the current ask turn."""
    from .hybrid import narrowed_hybrid_sources

    work_path = hybrid_refresh_work_path(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
    write_json(
        work_path,
        {
            "schema_version": 1,
            "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "target": target,
            "query": query,
            "selected_source_ids": [
                source_id for source_id in source_ids if isinstance(source_id, str) and source_id
            ],
            "recommended_targets": [
                target_item
                for target_item in (recommended_targets or [])
                if isinstance(target_item, dict)
            ],
            "sources": narrowed_hybrid_sources(
                paths,
                target=target,
                source_ids=source_ids,
            ),
            "operation_checklist": [
                "inspect_target_render_or_focus_render_assets_when_present",
                "write_additive_semantic_overlay_only",
                "record_render_inspection_used_and_assets_in_progress_summary",
                "settle_hidden_ask_progress_with_covered_or_blocked",
            ],
            "render_inspection_guidance": {
                "required_when_render_assets_present": True,
                "preferred_asset_fields": [
                    "target_focus_render_assets",
                    "focus_render_assets",
                    "target_render_assets",
                    "render_assets",
                    "render_refs",
                ],
                "audit_summary_fields": [
                    "render_inspection_used",
                    "inspected_render_assets",
                ],
                "gate": "guidance-only",
            },
            "settlement_summary_contract": {
                "expected_fields": [
                    "mode",
                    "work_path",
                    "selected_source_id",
                    "covered_unit_ids",
                    "overlay_paths",
                    "render_inspection_used",
                    "inspected_render_assets",
                    "summary",
                ],
                "status_values": ["covered", "blocked"],
            },
        },
    )
    return str(work_path.relative_to(paths.root))


def load_support_manifest(
    paths: WorkspacePaths,
    *,
    support_manifest_path_value: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
) -> dict[str, Any]:
    """Load an external-support manifest when one exists."""
    candidate: Path | None = None
    if isinstance(support_manifest_path_value, str) and support_manifest_path_value:
        candidate = Path(support_manifest_path_value)
        if not candidate.is_absolute():
            candidate = paths.root / candidate
    elif isinstance(conversation_id, str) and isinstance(turn_id, str):
        candidate = support_manifest_path(paths, conversation_id=conversation_id, turn_id=turn_id)
    if candidate is None or not candidate.exists():
        return {}
    return read_json(candidate)


def validate_support_manifest_binding(
    paths: WorkspacePaths,
    manifest: dict[str, Any] | None,
    *,
    conversation_id: str,
    turn_id: str,
    answer_file_path: str | None,
    support_basis: str | None,
) -> list[tuple[str, str]]:
    """Validate that one manifest is bound to this turn and exact answer bytes."""
    if not isinstance(manifest, dict) or not manifest:
        return [("support-manifest-missing", "The required support manifest is missing.")]
    issues: list[tuple[str, str]] = []
    if manifest.get("conversation_id") != conversation_id:
        issues.append(
            (
                "support-manifest-conversation-mismatch",
                "Support manifest belongs to another conversation.",
            )
        )
    if manifest.get("turn_id") != turn_id:
        issues.append(
            ("support-manifest-turn-mismatch", "Support manifest belongs to another turn.")
        )
    if isinstance(support_basis, str) and manifest.get("support_basis") != support_basis:
        issues.append(
            ("support-manifest-basis-mismatch", "Support manifest basis does not match the turn.")
        )

    expected_path: Path | None = None
    if isinstance(answer_file_path, str) and answer_file_path:
        expected_path = Path(answer_file_path)
        if not expected_path.is_absolute():
            expected_path = paths.root / expected_path
        expected_path = expected_path.resolve()
    manifest_path_value = manifest.get("answer_file_path")
    manifest_answer_path: Path | None = None
    if isinstance(manifest_path_value, str) and manifest_path_value:
        manifest_answer_path = Path(manifest_path_value)
        if not manifest_answer_path.is_absolute():
            manifest_answer_path = paths.root / manifest_answer_path
        manifest_answer_path = manifest_answer_path.resolve()
    if expected_path is None or manifest_answer_path != expected_path:
        issues.append(
            (
                "support-manifest-answer-path-mismatch",
                "Support manifest is not bound to this answer path.",
            )
        )
        return issues
    if not expected_path.is_file():
        issues.append(
            (
                "support-manifest-answer-missing",
                "The support-bound answer file is missing.",
            )
        )
        return issues

    answer_bytes = expected_path.read_bytes()
    digest = manifest.get("answer_digest")
    if not isinstance(digest, dict):
        issues.append(("support-manifest-digest-missing", "Support manifest has no answer digest."))
        return issues
    if digest.get("algorithm") != "sha256":
        issues.append(
            (
                "support-manifest-digest-invalid",
                "Support manifest uses an unsupported digest algorithm.",
            )
        )
    expected_hex = hashlib.sha256(answer_bytes).hexdigest()
    if digest.get("hex") != expected_hex or digest.get("byte_count") != len(answer_bytes):
        issues.append(
            (
                "support-manifest-digest-mismatch",
                "Support manifest does not match the exact answer bytes.",
            )
        )
    return issues


def write_external_support_manifest(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    answer_file_path: str,
    support_basis: str,
    sources: list[dict[str, Any]],
    key_assertions: list[str] | None = None,
    verification_notes: str | None = None,
) -> str:
    """Persist the lightweight external-support manifest for one answer turn."""
    path = support_manifest_path(paths, conversation_id=conversation_id, turn_id=turn_id)
    checked_at = datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    resolved_answer_path = Path(answer_file_path)
    if not resolved_answer_path.is_absolute():
        resolved_answer_path = paths.root / resolved_answer_path
    answer_bytes = resolved_answer_path.read_bytes() if resolved_answer_path.exists() else b""
    normalized_sources: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            continue
        normalized_sources.append(
            {
                "url": source.get("url"),
                "title": source.get("title"),
                "source_type": source.get("source_type") or "external-web",
                "checked_at": source.get("checked_at") or checked_at,
                "support_snippet": source.get("support_snippet"),
                "locator": source.get("locator"),
                "sha256": source.get("sha256"),
                "size_bytes": source.get("size_bytes"),
                "media_type": source.get("media_type"),
                "source_role": source.get("source_role"),
                "authority": source.get("authority"),
                "freshness": source.get("freshness"),
                "supersedes": source.get("supersedes", []),
                "supersession_basis": source.get("supersession_basis"),
            }
        )
    write_json(
        path,
        {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "support_basis": support_basis,
            "verified_at": checked_at,
            "answer_file_path": answer_file_path,
            "answer_digest": {
                "algorithm": "sha256",
                "hex": hashlib.sha256(answer_bytes).hexdigest(),
                "byte_count": len(answer_bytes),
            },
            "sources": normalized_sources,
            "key_assertions": [
                value for value in (key_assertions or []) if isinstance(value, str) and value
            ],
            "verification_notes": verification_notes or "",
        },
    )
    return str(path.relative_to(paths.root))


def _retrieval_corpus_hints(paths: WorkspacePaths) -> list[str]:
    records = read_json(paths.retrieval_source_records_path("current")).get("records", [])
    if not isinstance(records, list):
        records = []
    artifact_records = read_json(paths.retrieval_artifact_records_path("current")).get(
        "records", []
    )
    if not isinstance(artifact_records, list):
        artifact_records = []
    hints: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for field_name in ("title", "current_path", "summary_en", "summary_source"):
            value = record.get(field_name)
            if isinstance(value, str) and value.strip():
                hints.append(value)
        entities = record.get("entities", [])
        if isinstance(entities, list):
            hints.extend(value for value in entities if isinstance(value, str) and value.strip())
    for record in artifact_records:
        if not isinstance(record, dict):
            continue
        for field_name in ("title", "searchable_text", "linked_text"):
            value = record.get(field_name)
            if isinstance(value, str) and value.strip():
                hints.append(value)
    return hints


def _answer_history_similarity(question: str, candidate_question: str) -> int:
    if not question.strip() or not candidate_question.strip():
        return 0
    return len(set(tokenize_text(question)) & set(tokenize_text(candidate_question)))


def warm_start_evidence(
    paths: WorkspacePaths,
    *,
    question: str,
    question_domain: str,
    limit: int = 3,
) -> dict[str, Any]:
    """Return evidence pointers from similar historical answers without reusing answer text."""
    from .conversation import current_corpus_signature
    from .projections import load_answer_history_snapshot

    history = load_answer_history_snapshot(paths).get("records", [])
    if not isinstance(history, list):
        return {"matched_records": [], "session_ids": [], "trace_ids": [], "external_urls": []}
    current_signature = current_corpus_signature(paths)
    require_exact_corpus_match = question_domain in {"workspace-corpus", "composition"}
    if require_exact_corpus_match and not isinstance(current_signature, str):
        return {"matched_records": [], "session_ids": [], "trace_ids": [], "external_urls": []}
    scored: list[tuple[int, dict[str, Any]]] = []
    for record in history:
        if not isinstance(record, dict):
            continue
        if record.get("question_domain") != question_domain:
            continue
        if require_exact_corpus_match and record.get("corpus_signature") != current_signature:
            continue
        score = _answer_history_similarity(question, str(record.get("question_text", "")))
        if score <= 1:
            continue
        scored.append((score, record))
    scored.sort(key=lambda item: (item[0], str(item[1].get("recorded_at") or "")), reverse=True)
    selected = [record for _score, record in scored[:limit]]
    session_ids: list[str] = []
    trace_ids: list[str] = []
    external_urls: list[str] = []
    for record in selected:
        session_ids.extend(
            value for value in record.get("session_ids", []) if isinstance(value, str) and value
        )
        trace_ids.extend(
            value for value in record.get("trace_ids", []) if isinstance(value, str) and value
        )
        external_urls.extend(
            value for value in record.get("external_urls", []) if isinstance(value, str) and value
        )
    return {
        "matched_records": [
            {
                "conversation_id": record.get("conversation_id"),
                "turn_id": record.get("turn_id"),
                "recorded_at": record.get("recorded_at"),
                "question_text": record.get("question_text"),
                "question_class": record.get("question_class"),
                "question_domain": record.get("question_domain"),
                "support_strategy": record.get("support_strategy"),
                "analysis_origin": record.get("analysis_origin"),
                "support_basis": record.get("support_basis"),
                "corpus_signature": record.get("corpus_signature"),
                "published_snapshot_id": record.get("published_snapshot_id"),
                "session_ids": record.get("session_ids", []),
                "trace_ids": record.get("trace_ids", []),
                "external_urls": record.get("external_urls", []),
            }
            for record in selected
        ],
        "session_ids": list(dict.fromkeys(session_ids)),
        "trace_ids": list(dict.fromkeys(trace_ids)),
        "external_urls": list(dict.fromkeys(external_urls)),
    }


def question_execution_profile(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    question: str,
    semantic_analysis: dict[str, Any] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the shared routed execution profile for one ask-like turn."""
    normalized_analysis = normalize_question_analysis(
        question,
        semantic_analysis=semantic_analysis,
        corpus_hints=_retrieval_corpus_hints(paths),
        fallback_hints=fallback_hints,
    )
    question_class = str(normalized_analysis["question_class"])
    inner_workflow_id = str(normalized_analysis["inner_workflow_id"])
    route_reason = str(normalized_analysis["route_reason"])
    question_domain = str(normalized_analysis["question_domain"])
    support_strategy = str(normalized_analysis["support_strategy"])
    evidence_requirements = dict(normalized_analysis["evidence_requirements"])
    bundle_paths = ensure_turn_bundle(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        question_class=question_class,
    )
    return {
        "question_class": question_class,
        "question_domain": question_domain,
        "inner_workflow_id": inner_workflow_id,
        "route_reason": route_reason,
        "bundle_paths": bundle_paths,
        "support_strategy": support_strategy,
        "needs_latest_workspace_state": bool(normalized_analysis["needs_latest_workspace_state"]),
        "analysis_origin": str(normalized_analysis["analysis_origin"]),
        "evidence_requirements": evidence_requirements,
        "semantic_analysis": {
            "question_class": question_class,
            "question_domain": question_domain,
            "inner_workflow_id": inner_workflow_id,
            "support_strategy": support_strategy,
            "route_reason": route_reason,
            "needs_latest_workspace_state": bool(
                normalized_analysis["needs_latest_workspace_state"]
            ),
            "memory_query_profile": dict(normalized_analysis["memory_query_profile"]),
            "source_scope_intent": dict(normalized_analysis["source_scope_intent"]),
            "evidence_requirements": evidence_requirements,
        },
        "evidence_mode": (
            "kb-first-escalation"
            if question_domain == "composition"
            else ("web-first" if question_domain == "external-factual" else support_strategy)
        ),
        "research_depth": "deep" if question_class == "composition" else "standard",
        "memory_query_profile": dict(normalized_analysis["memory_query_profile"]),
        "preferred_channels": list(evidence_requirements.get("preferred_channels", [])),
        "inspection_scope": str(evidence_requirements.get("inspection_scope")),
        "prefer_published_artifacts": bool(
            evidence_requirements.get("prefer_published_artifacts", True)
        ),
        "warm_start_evidence": warm_start_evidence(
            paths,
            question=question,
            question_domain=question_domain,
        ),
    }

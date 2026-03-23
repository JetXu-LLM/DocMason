"""Shared helpers for additive semantic overlay sidecars."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project import read_json, write_json

CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1}
HARD_ARTIFACT_TYPES = frozenset(
    {
        "page-image",
        "chart",
        "table",
        "picture",
        "major-region",
        "group",
        "connector",
        "auto-shape",
    }
)
PRIORITY_REASON_WEIGHTS = {
    "image-only-page": 100,
    "scanned-page-like": 95,
    "connector-heavy-slide": 92,
    "grouped-diagram-slide": 90,
    "rendered-only-diagram-section": 88,
    "chart-slide": 84,
    "chart-sheet": 84,
    "picture-heavy-sheet": 82,
    "figure-heavy-section": 82,
    "dashboard-like-sheet": 80,
    "diagram-or-ui-page": 78,
    "diagram-or-ui-slide": 78,
    "image-section": 76,
    "table-section": 74,
    "multi-table-sheet": 72,
    "weak-label-slide": 70,
    "weak-section-confidence": 66,
    "procedure-page": 64,
    "continued-structure-page": 62,
}
PRIORITY_KIND_WEIGHTS = {
    "page-image": 100,
    "connector": 92,
    "group": 90,
    "chart": 86,
    "picture": 84,
    "major-region": 82,
    "table": 80,
    "auto-shape": 70,
    "unit-render": 60,
}


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def semantic_overlay_dir(source_dir: Path) -> Path:
    """Return the semantic overlay directory for one source."""
    return source_dir / "semantic_overlay"


def collect_semantic_overlay_assets(source_dir: Path) -> list[str]:
    """Return the published semantic overlay asset list for one source."""
    overlay_dir = semantic_overlay_dir(source_dir)
    if not overlay_dir.exists():
        return []
    return [
        str(path.relative_to(source_dir))
        for path in sorted(overlay_dir.glob("*.json"))
        if path.is_file()
    ]


def load_semantic_overlays(source_dir: Path) -> dict[str, dict[str, Any]]:
    """Load all semantic overlay sidecars for one source by unit id."""
    overlays: dict[str, dict[str, Any]] = {}
    for asset in collect_semantic_overlay_assets(source_dir):
        payload = read_json(source_dir / asset)
        unit_id = payload.get("unit_id")
        if isinstance(unit_id, str) and unit_id:
            overlays[unit_id] = payload
    return overlays


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def _normalized_page_span(value: Any) -> dict[str, int] | None:
    if (
        isinstance(value, dict)
        and isinstance(value.get("start"), int)
        and isinstance(value.get("end"), int)
    ):
        return {"start": int(value["start"]), "end": int(value["end"])}
    return None


def _unit_render_assets(unit: dict[str, Any]) -> list[str]:
    render_assets = unit.get("render_assets", [])
    if isinstance(render_assets, list):
        normalized = [value for value in render_assets if isinstance(value, str) and value.strip()]
        if normalized:
            return normalized
    rendered_asset = unit.get("rendered_asset")
    if isinstance(rendered_asset, str) and rendered_asset.strip():
        return [rendered_asset]
    return []


def _artifact_entries_by_unit(source_dir: Path) -> dict[str, list[dict[str, Any]]]:
    artifact_index = read_json(source_dir / "artifact_index.json")
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for artifact in artifact_index.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        unit_id = artifact.get("unit_id")
        if isinstance(unit_id, str) and unit_id:
            grouped[unit_id].append(artifact)
    return dict(grouped)


def _artifact_gap_hints(artifact: dict[str, Any]) -> list[str]:
    values = artifact.get("semantic_gap_hints", [])
    return [value for value in values if isinstance(value, str) and value.strip()]


def _candidate_priority(
    *,
    reasons: list[str],
    candidate_kinds: list[str],
    insufficiency_signals: list[str],
    target_artifact_count: int,
) -> int:
    weighted_candidates = [
        PRIORITY_REASON_WEIGHTS.get(value, 58) for value in reasons + insufficiency_signals
    ] + [PRIORITY_KIND_WEIGHTS.get(value, 56) for value in candidate_kinds]
    base = max(weighted_candidates or [50])
    return int(base + min(target_artifact_count, 4) * 2)


def validate_hybrid_work(
    payload: dict[str, Any],
    *,
    target: str,
    known_source_ids: set[str],
) -> list[str]:
    """Validate the staging hybrid-work queue."""
    errors: list[str] = []
    if payload.get("target") != target:
        errors.append("hybrid_work.json target does not match the validated KB target")
    sources = payload.get("sources", [])
    if not isinstance(sources, list):
        return ["hybrid_work.json must contain a `sources` list"]
    seen_source_ids: set[str] = set()
    source_required_string_fields = (
        "document_type",
        "source_path",
        "source_fingerprint",
        "source_hybrid_status",
    )
    unit_required_string_fields = (
        "eligible_reason",
        "unit_title",
        "unit_evidence_fingerprint",
        "coverage_status",
    )
    unit_required_list_fields = (
        "all_reasons",
        "candidate_kinds",
        "target_artifact_ids",
        "target_render_assets",
        "required_channels",
        "insufficiency_signals",
        "required_overlay_slots",
        "suggested_overlay_kinds",
        "target_focus_render_assets",
        "blocked_reasons",
        "covered_slots",
        "blocked_slots",
        "remaining_slots",
    )
    for source in sources:
        if not isinstance(source, dict):
            errors.append("hybrid_work.json sources entries must be objects")
            continue
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            errors.append("hybrid_work.json sources require a non-empty source_id")
            continue
        if source_id in seen_source_ids:
            errors.append(f"Duplicate source_id `{source_id}` in hybrid_work.json")
        seen_source_ids.add(source_id)
        if source_id not in known_source_ids:
            errors.append(f"hybrid_work.json references unknown source_id `{source_id}`")
        for field_name in source_required_string_fields:
            value = source.get(field_name)
            if not isinstance(value, str) or not value:
                errors.append(f"hybrid_work.json source `{source_id}` is missing `{field_name}`")
        source_status = source.get("source_hybrid_status")
        if source_status not in {"candidate-prepared", "partially-covered", "covered"}:
            errors.append(
                f"hybrid_work.json source `{source_id}` has invalid source_hybrid_status"
            )
        units = source.get("units", [])
        if not isinstance(units, list):
            errors.append(f"hybrid_work.json source `{source_id}` must expose a units list")
            continue
        expected_count = source.get("candidate_unit_count")
        if isinstance(expected_count, int) and expected_count != len(units):
            errors.append(
                f"hybrid_work.json source `{source_id}` candidate_unit_count does not match units"
            )
        for field_name in (
            "covered_candidate_count",
            "remaining_candidate_count",
            "blocked_candidate_count",
        ):
            if not isinstance(source.get(field_name), int):
                errors.append(
                    f"hybrid_work.json source `{source_id}` requires integer `{field_name}`"
                )
        seen_unit_ids: set[str] = set()
        for unit in units:
            if not isinstance(unit, dict):
                errors.append(
                    f"hybrid_work.json source `{source_id}` units entries must be objects"
                )
                continue
            unit_id = unit.get("unit_id")
            if not isinstance(unit_id, str) or not unit_id:
                errors.append(f"hybrid_work.json source `{source_id}` requires non-empty unit_id")
                continue
            if unit_id in seen_unit_ids:
                errors.append(
                    f"hybrid_work.json source `{source_id}` has duplicate unit_id `{unit_id}`"
                )
            seen_unit_ids.add(unit_id)
            for field_name in unit_required_string_fields:
                value = unit.get(field_name)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"hybrid_work.json unit `{source_id}:{unit_id}` is missing `{field_name}`"
                    )
            coverage_status = unit.get("coverage_status")
            if coverage_status not in {
                "candidate-prepared",
                "partially-covered",
                "covered",
                "blocked",
            }:
                errors.append(
                    f"hybrid_work.json unit `{source_id}:{unit_id}` has invalid coverage_status"
                )
            for field_name in unit_required_list_fields:
                value = unit.get(field_name)
                if not isinstance(value, list):
                    errors.append(
                        f"hybrid_work.json unit `{source_id}:{unit_id}` is missing `{field_name}`"
                    )
            priority = unit.get("priority")
            if not isinstance(priority, int):
                errors.append(
                    f"hybrid_work.json unit `{source_id}:{unit_id}` requires integer priority"
                )
            page_span = unit.get("target_render_page_span")
            if page_span is not None and _normalized_page_span(page_span) is None:
                errors.append(
                    "hybrid_work.json unit "
                    f"`{source_id}:{unit_id}` has an invalid target_render_page_span"
                )
    return errors


def overlay_confidence(payload: dict[str, Any]) -> str | None:
    """Return the strongest confidence label visible in one overlay payload."""
    seen: list[str] = []
    for field_name in ("semantic_labels", "artifact_annotations", "cross_region_relations"):
        items = payload.get(field_name, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            confidence = item.get("confidence")
            if isinstance(confidence, str) and confidence in CONFIDENCE_ORDER:
                seen.append(confidence)
    if not seen:
        return None
    return max(seen, key=lambda item: CONFIDENCE_ORDER[item])


def overlay_search_strings(payload: dict[str, Any]) -> list[str]:
    """Return compact search strings from one overlay payload."""
    texts: list[str] = []
    for field_name in ("semantic_labels", "artifact_annotations", "cross_region_relations"):
        items = payload.get(field_name, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("label", "text", "summary", "relation_type"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    texts.append(value.strip())
    uncertainty_notes = payload.get("uncertainty_notes", [])
    if isinstance(uncertainty_notes, list):
        texts.extend(
            value.strip() for value in uncertainty_notes if isinstance(value, str) and value.strip()
        )
    return _deduplicate_strings(texts)


def validate_semantic_overlay(
    payload: dict[str, Any],
    *,
    source_id: str,
    unit_ids: set[str],
    artifact_ids: set[str],
) -> list[str]:
    """Validate one semantic overlay sidecar against the current staged source."""
    errors: list[str] = []
    from .hybrid import HYBRID_OVERLAY_SLOTS, infer_overlay_slots

    if payload.get("artifact_type") != "semantic-overlay":
        errors.append("semantic overlay must declare artifact_type `semantic-overlay`")
    if payload.get("source_id") != source_id:
        errors.append("semantic overlay source_id does not match the source directory")
    unit_id = payload.get("unit_id")
    if unit_id not in unit_ids:
        errors.append(f"semantic overlay references unknown unit_id `{unit_id}`")
    derivation_mode = payload.get("derivation_mode")
    if derivation_mode not in {"agent-authored", "hybrid"}:
        errors.append("semantic overlay derivation_mode must be `agent-authored` or `hybrid`")
    origin = payload.get("origin")
    if origin not in {"sync-hybrid", "ask-hybrid"}:
        errors.append("semantic overlay origin must be `sync-hybrid` or `ask-hybrid`")
    source_fingerprint = payload.get("source_fingerprint")
    if not isinstance(source_fingerprint, str) or not source_fingerprint:
        errors.append("semantic overlay must include a non-empty source_fingerprint")
    unit_evidence_fingerprint = payload.get("unit_evidence_fingerprint")
    if not isinstance(unit_evidence_fingerprint, str) or not unit_evidence_fingerprint:
        errors.append("semantic overlay must include a non-empty unit_evidence_fingerprint")
    eligible_reason = payload.get("eligible_reason")
    if not isinstance(eligible_reason, str) or not eligible_reason.strip():
        errors.append("semantic overlay must include a non-empty eligible_reason")
    consumed_inputs = payload.get("consumed_inputs")
    if not isinstance(consumed_inputs, dict):
        errors.append("semantic overlay must include consumed_inputs")
    elif "focus_render_assets" in consumed_inputs and not isinstance(
        consumed_inputs.get("focus_render_assets"),
        list,
    ):
        errors.append("semantic overlay consumed_inputs.focus_render_assets must be a list")
    for field_name in (
        "semantic_labels",
        "artifact_annotations",
        "cross_region_relations",
        "uncertainty_notes",
        "covered_slots",
        "blocked_slots",
    ):
        if not isinstance(payload.get(field_name), list):
            errors.append(f"semantic overlay must include `{field_name}` as a list")
    for field_name in ("covered_slots", "blocked_slots"):
        values = payload.get(field_name, [])
        if isinstance(values, list):
            for slot in values:
                if not isinstance(slot, str) or slot not in HYBRID_OVERLAY_SLOTS:
                    errors.append(
                        f"semantic overlay `{field_name}` contains unknown slot `{slot}`"
                    )
    inferred_slots = infer_overlay_slots(payload, fallback_reason=eligible_reason)
    blocked_slots = payload.get("blocked_slots", [])
    if not inferred_slots and not (
        isinstance(blocked_slots, list)
        and any(isinstance(slot, str) and slot in HYBRID_OVERLAY_SLOTS for slot in blocked_slots)
    ):
        errors.append("semantic overlay must expose at least one covered or blocked slot")

    for field_name in ("semantic_labels", "artifact_annotations"):
        items = payload.get(field_name, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                errors.append(f"semantic overlay {field_name} entries must be objects")
                continue
            artifact_id = item.get("artifact_id")
            if artifact_id is not None and artifact_id not in artifact_ids:
                errors.append(
                    f"semantic overlay {field_name} references unknown artifact_id `{artifact_id}`"
                )
            artifact_id_list = item.get("artifact_ids", [])
            if isinstance(artifact_id_list, list):
                for candidate in artifact_id_list:
                    if isinstance(candidate, str) and candidate not in artifact_ids:
                        errors.append(
                            f"semantic overlay {field_name} references unknown "
                            f"artifact_id `{candidate}`"
                        )

    relations = payload.get("cross_region_relations", [])
    if isinstance(relations, list):
        for relation in relations:
            if not isinstance(relation, dict):
                errors.append("semantic overlay cross_region_relations entries must be objects")
                continue
            for key in ("from_artifact_id", "to_artifact_id"):
                value = relation.get(key)
                if value is not None and value not in artifact_ids:
                    errors.append(
                        "semantic overlay cross_region_relations references "
                        f"unknown {key} `{value}`"
                    )
    return errors


def write_semantic_overlay(source_dir: Path, payload: dict[str, Any]) -> str:
    """Persist one semantic overlay sidecar and return its relative asset path."""
    from .hybrid import compute_unit_evidence_fingerprint, infer_overlay_slots

    unit_id = payload.get("unit_id")
    if not isinstance(unit_id, str) or not unit_id:
        raise ValueError("semantic overlay payload requires a non-empty unit_id")
    overlay_dir = semantic_overlay_dir(source_dir)
    overlay_dir.mkdir(parents=True, exist_ok=True)
    overlay_path = overlay_dir / f"{unit_id}.json"
    source_manifest = read_json(source_dir / "source_manifest.json")
    source_fingerprint = str(source_manifest.get("source_fingerprint") or "")
    consumed_inputs = payload.get("consumed_inputs", {})
    if not isinstance(consumed_inputs, dict):
        consumed_inputs = {}
    if "focus_render_assets" not in consumed_inputs:
        render_assets = consumed_inputs.get("render_assets", [])
        consumed_inputs["focus_render_assets"] = (
            [value for value in render_assets if isinstance(value, str) and value]
            if isinstance(render_assets, list)
            else []
        )
    normalized_payload = {
        "origin": payload.get("origin") or "sync-hybrid",
        "source_id": payload.get("source_id") or source_manifest.get("source_id"),
        "unit_id": unit_id,
        "derivation_mode": payload.get("derivation_mode") or "hybrid",
        "eligible_reason": payload.get("eligible_reason") or "hybrid-enrichment",
        "source_fingerprint": payload.get("source_fingerprint") or source_fingerprint,
        "unit_evidence_fingerprint": payload.get("unit_evidence_fingerprint")
        or compute_unit_evidence_fingerprint(source_dir, unit_id),
        "covered_slots": payload.get("covered_slots")
        or infer_overlay_slots(
            payload,
            fallback_reason=str(payload.get("eligible_reason") or ""),
        ),
        "blocked_slots": payload.get("blocked_slots") or [],
        "consumed_inputs": consumed_inputs,
        "semantic_labels": payload.get("semantic_labels", []),
        "artifact_annotations": payload.get("artifact_annotations", []),
        "cross_region_relations": payload.get("cross_region_relations", []),
        "uncertainty_notes": payload.get("uncertainty_notes", []),
    }
    write_json(
        overlay_path,
        {
            "artifact_type": "semantic-overlay",
            "schema_version": 1,
            "generated_at": utc_now(),
            **normalized_payload,
        },
    )
    return str(overlay_path.relative_to(source_dir))


def semantic_overlay_candidates(
    source_dir: Path,
    *,
    evidence_manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return deterministic hybrid-enrichment candidates for one staged source."""
    document_type = str(evidence_manifest.get("document_type") or "unknown")
    overlays = load_semantic_overlays(source_dir)
    artifacts_by_unit = _artifact_entries_by_unit(source_dir)
    pdf_document = read_json(source_dir / "pdf_document.json")
    pdf_context_lookup = {
        str(item["unit_id"]): item
        for item in pdf_document.get("page_contexts", [])
        if isinstance(item, dict) and isinstance(item.get("unit_id"), str)
    }
    candidates: list[dict[str, Any]] = []
    for unit in evidence_manifest.get("units", []):
        if not isinstance(unit, dict) or not isinstance(unit.get("unit_id"), str):
            continue
        unit_id = str(unit["unit_id"])
        reasons: list[str] = []
        insufficiency_signals: list[str] = []
        visual_path = source_dir / "visual_layout" / f"{unit_id}.json"
        visual_layout = read_json(visual_path)
        role_hints = [
            str(value)
            for value in visual_layout.get("role_hints", [])
            if isinstance(value, str) and value
        ]
        unit_gap_hints = [
            str(value)
            for value in visual_layout.get("semantic_gap_hints", [])
            if isinstance(value, str) and value
        ]
        regions = [
            region for region in visual_layout.get("regions", []) if isinstance(region, dict)
        ]
        region_types = {str(region.get("artifact_type") or "") for region in regions}
        unit_artifacts = list(artifacts_by_unit.get(unit_id, []))
        render_assets = _unit_render_assets(unit)
        render_page_span = _normalized_page_span(unit.get("render_page_span"))
        hard_artifacts = [
            artifact
            for artifact in unit_artifacts
            if str(artifact.get("artifact_type") or "") in HARD_ARTIFACT_TYPES
        ]
        target_artifacts = [
            artifact for artifact in hard_artifacts if _artifact_gap_hints(artifact)
        ]
        insufficiency_signals.extend(unit_gap_hints)
        for artifact in hard_artifacts:
            insufficiency_signals.extend(_artifact_gap_hints(artifact))
        if document_type == "pdf":
            page_context = pdf_context_lookup.get(unit_id, {})
            text_layer_quality = str(page_context.get("text_layer_quality") or "")
            heading_candidates = page_context.get("heading_candidates", [])
            if not heading_candidates:
                reasons.append("weak-section-confidence")
            elif all(
                isinstance(candidate, dict) and candidate.get("confidence") == "low"
                for candidate in heading_candidates
            ):
                reasons.append("weak-section-confidence")
            if text_layer_quality in {"none", "weak"}:
                insufficiency_signals.append(f"{text_layer_quality}-text-layer")
            if any(
                hint in {"architecture-like", "flow-like", "ui-like", "comparison-like"}
                for hint in role_hints
            ):
                reasons.append("diagram-or-ui-page")
            if any(hint in {"kpi-like", "roadmap-like"} for hint in role_hints):
                reasons.append("chart-or-dashboard-page")
            if any(
                region_type in {"chart", "picture", "major-region"} for region_type in region_types
            ):
                reasons.append("visual-heavy-page")
            if page_context.get("procedure_spans"):
                reasons.append("procedure-page")
            if page_context.get("continuation_group_ids"):
                reasons.append("continued-structure-page")
            page_image_artifact_id = page_context.get("page_image_artifact_id")
            if isinstance(page_image_artifact_id, str) and page_image_artifact_id:
                target_artifacts = [
                    artifact
                    for artifact in hard_artifacts
                    if artifact.get("artifact_id") == page_image_artifact_id
                ] + target_artifacts
                insufficiency_signals.append("image-only-page")
        elif document_type == "pptx":
            if "connector" in region_types:
                reasons.append("connector-heavy-slide")
            if "group" in region_types:
                reasons.append("grouped-diagram-slide")
            if any(
                hint in {"architecture-like", "flow-like", "ui-like", "comparison-like"}
                for hint in role_hints
            ):
                reasons.append("diagram-or-ui-slide")
            if "chart" in region_types:
                reasons.append("chart-slide")
            picture_count = sum(
                1 for artifact in hard_artifacts if artifact.get("artifact_type") == "picture"
            )
            linked_text = " ".join(
                str(artifact.get("linked_text") or "")
                for artifact in hard_artifacts
                if isinstance(artifact.get("linked_text"), str)
            )
            if picture_count >= 2:
                reasons.append("picture-heavy-slide")
            if (
                any(
                    reason in {"diagram-or-ui-slide", "grouped-diagram-slide"} for reason in reasons
                )
                and len(linked_text.strip()) <= 120
            ):
                reasons.append("weak-label-slide")
        elif document_type == "xlsx":
            sheet_payload = read_json(source_dir / "spreadsheet_sheet" / f"{unit_id}.json")
            sheet_role_hints = [
                str(value)
                for value in sheet_payload.get("sheet_role_hints", [])
                if isinstance(value, str) and value
            ]
            if any(value in {"dashboard-like", "kpi-like"} for value in sheet_role_hints):
                reasons.append("dashboard-like-sheet")
            if sheet_payload.get("chart_registry"):
                reasons.append("chart-sheet")
            if len(sheet_payload.get("tabular_regions", [])) >= 2:
                reasons.append("multi-table-sheet")
            if any(artifact.get("artifact_type") == "picture" for artifact in hard_artifacts):
                reasons.append("picture-heavy-sheet")
            if sheet_payload.get("chart_registry") and any(
                artifact.get("artifact_type") == "table" for artifact in hard_artifacts
            ):
                insufficiency_signals.append("chart-table-semantic-gap")
        elif document_type == "docx":
            structure_path = unit.get("structure_asset")
            structure_payload = (
                read_json(source_dir / structure_path)
                if isinstance(structure_path, str) and structure_path
                else {}
            )
            blocks = structure_payload.get("blocks", [])
            if any(isinstance(block, dict) and block.get("kind") == "table" for block in blocks):
                reasons.append("table-section")
            image_count = sum(
                len(block.get("image_refs", []))
                for block in blocks
                if isinstance(block, dict) and isinstance(block.get("image_refs"), list)
            )
            if image_count:
                reasons.append("image-section")
            if image_count and any(
                artifact.get("artifact_type") == "picture" for artifact in hard_artifacts
            ):
                reasons.append("figure-heavy-section")
            if any(hint in {"flow-like", "ui-like", "comparison-like"} for hint in role_hints):
                reasons.append("layout-sensitive-section")
            if image_count and not any(
                isinstance(artifact.get("caption_text"), str) and artifact.get("caption_text")
                for artifact in hard_artifacts
            ):
                insufficiency_signals.append("rendered-only-diagram-section")
        reasons = _deduplicate_strings(reasons + unit_gap_hints)
        insufficiency_signals = _deduplicate_strings(insufficiency_signals)
        if not reasons:
            continue
        if not target_artifacts:
            target_artifacts = [
                artifact
                for artifact in hard_artifacts
                if str(artifact.get("artifact_type") or "")
                in {
                    "page-image",
                    "connector",
                    "group",
                    "chart",
                    "picture",
                    "major-region",
                    "table",
                    "auto-shape",
                }
            ]
        candidate_kinds = _deduplicate_strings(
            [
                str(artifact.get("artifact_type") or "")
                for artifact in target_artifacts
                if isinstance(artifact.get("artifact_type"), str)
            ]
            or ["unit-render"]
        )
        target_artifact_ids = _deduplicate_strings(
            [
                str(artifact.get("artifact_id"))
                for artifact in target_artifacts
                if isinstance(artifact.get("artifact_id"), str)
            ]
        )
        target_render_assets = _deduplicate_strings(
            [
                value
                for artifact in target_artifacts
                for value in artifact.get("render_assets", [])
                if isinstance(value, str) and value
            ]
            + render_assets
        )
        target_render_page_span = next(
            (
                _normalized_page_span(artifact.get("render_page_span"))
                for artifact in target_artifacts
                if _normalized_page_span(artifact.get("render_page_span")) is not None
            ),
            render_page_span,
        )
        required_channels = _deduplicate_strings(
            [
                "render",
                "structure",
                *[
                    channel
                    for artifact in target_artifacts
                    for channel in artifact.get("available_channels", [])
                    if isinstance(channel, str) and channel
                ],
            ]
        )
        candidates.append(
            {
                "unit_id": unit_id,
                "eligible_reason": reasons[0],
                "all_reasons": reasons,
                "candidate_kinds": candidate_kinds,
                "target_artifact_ids": target_artifact_ids,
                "target_render_assets": target_render_assets,
                "target_render_page_span": target_render_page_span,
                "required_channels": required_channels,
                "insufficiency_signals": insufficiency_signals,
                "priority": _candidate_priority(
                    reasons=reasons,
                    candidate_kinds=candidate_kinds,
                    insufficiency_signals=insufficiency_signals,
                    target_artifact_count=len(target_artifact_ids),
                ),
                "overlay_present": unit_id in overlays,
            }
        )
    return sorted(
        candidates,
        key=lambda item: (-int(item["priority"]), str(item["unit_id"])),
    )

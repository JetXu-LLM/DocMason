"""Derived evidence affordances for KB-native odd-question handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .project import read_json

PUBLISHED_EVIDENCE_CHANNELS = ("text", "render", "structure", "notes", "media")
INSPECTION_SCOPE_VALUES = {"source", "unit", "multi-unit"}
AFFORDANCE_DERIVATION_MODE_VALUES = {"deterministic", "agent-authored", "hybrid"}
AFFORDANCE_CONFIDENCE_VALUES = {"high", "medium", "low"}
DEFAULT_AFFORDANCE_FILENAME = "derived_affordances.json"
BLOCKING_HARD_ARTIFACT_GAP_HINTS = frozenset(
    {
        "image-only-page",
        "scanned-page-like",
        "weak-text-layer",
        "text-layer-mismatch",
        "rendered-only-picture",
        "rendered-only-diagram-section",
        "rendered-only-table",
        "chart-table-semantic-gap",
        "page-image-target",
    }
)


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _truncate(text: str, *, limit: int = 180) -> str:
    compact = " ".join(text.split()).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _normalize_channel_descriptors(value: Any) -> dict[str, list[str]]:
    descriptors: dict[str, list[str]] = {}
    if not isinstance(value, dict):
        value = {}
    for channel in PUBLISHED_EVIDENCE_CHANNELS:
        raw = value.get(channel, [])
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, list):
            items = [item for item in raw if isinstance(item, str)]
        else:
            items = []
        descriptors[channel] = _deduplicate_strings(
            [item.strip() for item in items if item.strip()]
        )
    return descriptors


def _normalize_evidence_refs(value: Any) -> dict[str, list[str]]:
    refs: dict[str, list[str]] = {}
    if not isinstance(value, dict):
        value = {}
    for channel in PUBLISHED_EVIDENCE_CHANNELS:
        raw = value.get(channel, [])
        if isinstance(raw, str):
            items = [raw]
        elif isinstance(raw, list):
            items = [item for item in raw if isinstance(item, str)]
        else:
            items = []
        refs[channel] = _deduplicate_strings([item.strip() for item in items if item.strip()])
    return refs


def _normalize_available_channels(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return _deduplicate_strings(
        [
            channel
            for channel in value
            if isinstance(channel, str) and channel in PUBLISHED_EVIDENCE_CHANNELS
        ]
    )


def _confidence_from_channel_count(channel_count: int) -> str:
    if channel_count >= 3:
        return "high"
    if channel_count >= 1:
        return "medium"
    return "low"


def normalize_evidence_requirements(
    value: dict[str, Any] | None,
    *,
    question_class: str,
    question_domain: str,
) -> dict[str, Any]:
    """Validate compact evidence requirements for odd-question routing."""
    raw = value if isinstance(value, dict) else {}
    preferred_channels = _normalize_available_channels(raw.get("preferred_channels", []))
    inspection_scope = raw.get("inspection_scope")
    if inspection_scope not in INSPECTION_SCOPE_VALUES:
        inspection_scope = (
            "multi-unit"
            if question_class == "composition" or question_domain == "composition"
            else "unit"
        )
    prefer_published_artifacts = raw.get("prefer_published_artifacts")
    if not isinstance(prefer_published_artifacts, bool):
        prefer_published_artifacts = True
    return {
        "preferred_channels": preferred_channels,
        "inspection_scope": inspection_scope,
        "prefer_published_artifacts": prefer_published_artifacts,
    }


def flatten_channel_descriptors(value: Any) -> str:
    """Collapse channel descriptors into searchable plain text."""
    descriptors = _normalize_channel_descriptors(value)
    return "\n".join(
        descriptor for channel in PUBLISHED_EVIDENCE_CHANNELS for descriptor in descriptors[channel]
    ).strip()


def _notes_text_from_structure(structure_data: dict[str, Any]) -> str:
    notes_text = structure_data.get("notes_text")
    if isinstance(notes_text, str) and notes_text.strip():
        return notes_text.strip()
    return ""


def _media_refs_from_structure(structure_data: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    embedded_media = structure_data.get("embedded_media")
    if isinstance(embedded_media, list):
        refs.extend(item for item in embedded_media if isinstance(item, str) and item)
    image_count = structure_data.get("image_count")
    if isinstance(image_count, int) and image_count > 0:
        refs.append(f"image-count:{image_count}")
    if structure_data.get("has_drawing"):
        refs.append("drawing")
    return _deduplicate_strings(refs)


def _structure_descriptors(
    unit: dict[str, Any],
    structure_data: dict[str, Any],
) -> list[str]:
    descriptors = ["Structured unit metadata is available."]
    unit_type = str(unit.get("unit_type") or "")
    if unit_type == "slide":
        visible_text = structure_data.get("visible_text", [])
        if isinstance(visible_text, list) and visible_text:
            descriptors.append(f"Slide structure includes {len(visible_text)} visible text blocks.")
        if structure_data.get("hidden"):
            descriptors.append("This slide is hidden.")
        if _notes_text_from_structure(structure_data):
            descriptors.append("Speaker or notes text is available for this slide.")
        if _media_refs_from_structure(structure_data):
            descriptors.append("Embedded media references are available for this slide.")
    elif unit_type == "page":
        if structure_data.get("text_excerpt"):
            descriptors.append("Page-level structure includes an extracted text excerpt.")
    elif unit_type == "section":
        blocks = structure_data.get("blocks", [])
        if isinstance(blocks, list) and blocks:
            descriptors.append(f"Section structure contains {len(blocks)} extracted blocks.")
        table_count = sum(
            1 for block in blocks if isinstance(block, dict) and str(block.get("kind")) == "table"
        )
        if table_count:
            descriptors.append(f"Section structure contains {table_count} table blocks.")
        headings = structure_data.get("headings", [])
        if isinstance(headings, list) and headings:
            descriptors.append(
                "Section headings include: " + ", ".join(str(value) for value in headings[:3])
            )
        procedure_spans = structure_data.get("procedure_spans", [])
        if isinstance(procedure_spans, list) and procedure_spans:
            descriptors.append(
                f"Section structure contains {len(procedure_spans)} procedure-like spans."
            )
        captions = structure_data.get("captions", [])
        if isinstance(captions, list) and captions:
            descriptors.append(
                "Section captions include: " + ", ".join(str(value) for value in captions[:2])
            )
    elif unit_type == "sheet":
        max_row = structure_data.get("max_row")
        max_column = structure_data.get("max_column")
        if isinstance(max_row, int) and isinstance(max_column, int):
            descriptors.append(
                f"Worksheet structure spans {max_row} rows and {max_column} columns."
            )
        tables = structure_data.get("tables", [])
        if isinstance(tables, list) and tables:
            descriptors.append(f"Worksheet structure lists {len(tables)} named tables.")
        if _media_refs_from_structure(structure_data):
            descriptors.append("Worksheet structure indicates image or drawing content.")
    return _deduplicate_strings(descriptors)


def _unit_affordance(
    *,
    unit: dict[str, Any],
    source_dir: Path,
) -> dict[str, Any]:
    text_asset = unit.get("text_asset")
    text = ""
    if isinstance(text_asset, str) and text_asset:
        text = _safe_read_text(source_dir / text_asset)
    structure_asset = unit.get("structure_asset")
    structure_data: dict[str, Any] = {}
    if isinstance(structure_asset, str) and structure_asset:
        structure_data = read_json(source_dir / structure_asset)
    render_refs: list[str] = []
    rendered_asset = unit.get("rendered_asset")
    if isinstance(rendered_asset, str) and rendered_asset:
        render_refs.append(rendered_asset)
    render_reference_ids = unit.get("render_reference_ids", [])
    if isinstance(render_reference_ids, list):
        render_refs.extend(item for item in render_reference_ids if isinstance(item, str) and item)
    render_refs = _deduplicate_strings(render_refs)
    media_refs = _deduplicate_strings(
        [item for item in unit.get("embedded_media", []) if isinstance(item, str) and item]
        + _media_refs_from_structure(structure_data)
    )
    notes_text = _notes_text_from_structure(structure_data)

    channel_descriptors: dict[str, list[str]] = {
        channel: [] for channel in PUBLISHED_EVIDENCE_CHANNELS
    }
    evidence_refs: dict[str, list[str]] = {channel: [] for channel in PUBLISHED_EVIDENCE_CHANNELS}
    available_channels: list[str] = []

    if text:
        available_channels.append("text")
        channel_descriptors["text"] = _deduplicate_strings(
            [
                "Extracted text is available for this unit.",
                _truncate(text),
            ]
        )
        if isinstance(text_asset, str) and text_asset:
            evidence_refs["text"] = [text_asset]

    if render_refs:
        available_channels.append("render")
        channel_descriptors["render"] = ["Published rendered evidence is available for this unit."]
        evidence_refs["render"] = render_refs

    if isinstance(structure_asset, str) and structure_asset:
        available_channels.append("structure")
        channel_descriptors["structure"] = _structure_descriptors(unit, structure_data)
        evidence_refs["structure"] = [structure_asset]

    if notes_text:
        available_channels.append("notes")
        channel_descriptors["notes"] = [
            "Notes text is available for this unit.",
            _truncate(notes_text),
        ]
        if isinstance(structure_asset, str) and structure_asset:
            evidence_refs["notes"] = [structure_asset]

    if media_refs:
        available_channels.append("media")
        channel_descriptors["media"] = [
            "Embedded media or drawing references are available for this unit."
        ]
        evidence_refs["media"] = media_refs

    available_channels = _deduplicate_strings(available_channels)
    return {
        "unit_id": unit.get("unit_id"),
        "available_channels": available_channels,
        "channel_descriptors": _normalize_channel_descriptors(channel_descriptors),
        "confidence": _confidence_from_channel_count(len(available_channels)),
        "derivation_mode": "deterministic",
        "evidence_refs": _normalize_evidence_refs(evidence_refs),
    }


def derive_source_affordances(
    *,
    source_manifest: dict[str, Any],
    evidence_manifest: dict[str, Any],
    source_dir: Path,
    knowledge: dict[str, Any] | None = None,
    summary_text: str = "",
) -> dict[str, Any]:
    """Build deterministic affordance descriptors from published artifacts."""
    unit_affordances: list[dict[str, Any]] = []
    for unit in evidence_manifest.get("units", []):
        if not isinstance(unit, dict):
            continue
        unit_affordance = _unit_affordance(unit=unit, source_dir=source_dir)
        if isinstance(unit_affordance.get("unit_id"), str):
            unit_affordances.append(unit_affordance)

    channel_descriptors: dict[str, list[str]] = {
        channel: [] for channel in PUBLISHED_EVIDENCE_CHANNELS
    }
    evidence_refs: dict[str, list[str]] = {channel: [] for channel in PUBLISHED_EVIDENCE_CHANNELS}

    title = str((knowledge or {}).get("title") or source_manifest.get("current_path") or "")
    summary_en = str((knowledge or {}).get("summary_en") or "").strip()
    summary_source = str((knowledge or {}).get("summary_source") or "").strip()
    if title or summary_en or summary_source or summary_text.strip():
        channel_descriptors["text"].extend(
            [
                item
                for item in [
                    title,
                    _truncate(summary_en) if summary_en else "",
                    (
                        _truncate(summary_source)
                        if summary_source and summary_source != summary_en
                        else ""
                    ),
                    _truncate(summary_text) if summary_text else "",
                ]
                if item
            ]
        )
    for unit_affordance in unit_affordances:
        unit_id = unit_affordance.get("unit_id")
        if not isinstance(unit_id, str):
            continue
        for channel in unit_affordance.get("available_channels", []):
            if channel not in PUBLISHED_EVIDENCE_CHANNELS:
                continue
            channel_descriptors[channel].extend(
                unit_affordance.get("channel_descriptors", {}).get(channel, [])
            )
            evidence_refs[channel].append(unit_id)

    document_renders = [
        item
        for item in evidence_manifest.get("document_renders", [])
        if isinstance(item, str) and item
    ]
    if document_renders:
        channel_descriptors["render"].insert(
            0,
            f"Published document-level renders are available for {len(document_renders)} views.",
        )
        evidence_refs["render"].extend(document_renders)

    structure_assets = [
        item
        for item in evidence_manifest.get("structure_assets", [])
        if isinstance(item, str) and item
    ]
    if structure_assets:
        channel_descriptors["structure"].insert(
            0,
            f"Structured sidecars are available for {len(structure_assets)} units.",
        )
        evidence_refs["structure"].extend(structure_assets)

    available_channels = [
        channel
        for channel in PUBLISHED_EVIDENCE_CHANNELS
        if channel_descriptors[channel] or evidence_refs[channel]
    ]
    if summary_en:
        evidence_refs["text"].append("summary.md")

    return {
        "schema_version": 1,
        "artifact_type": "derived-affordances",
        "source_id": source_manifest.get("source_id"),
        "source_fingerprint": source_manifest.get("source_fingerprint"),
        "current_path": source_manifest.get("current_path"),
        "document_type": source_manifest.get("document_type"),
        "generated_at": source_manifest.get("staging_generated_at") or "",
        "derivation_mode": "deterministic",
        "confidence": _confidence_from_channel_count(len(available_channels)),
        "source_affordances": {
            "available_channels": _deduplicate_strings(available_channels),
            "channel_descriptors": _normalize_channel_descriptors(channel_descriptors),
            "evidence_refs": _normalize_evidence_refs(evidence_refs),
        },
        "unit_affordances": unit_affordances,
    }


def merge_derived_affordances(
    baseline: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge a preserved affordance payload onto a deterministic baseline."""
    if not isinstance(existing, dict):
        return baseline
    if existing.get("source_id") != baseline.get("source_id"):
        return baseline
    if existing.get("source_fingerprint") != baseline.get("source_fingerprint"):
        return baseline

    merged = dict(baseline)
    existing_mode = existing.get("derivation_mode")
    if existing_mode in {"agent-authored", "hybrid"}:
        merged["derivation_mode"] = "hybrid"
    elif existing_mode in AFFORDANCE_DERIVATION_MODE_VALUES:
        merged["derivation_mode"] = existing_mode

    existing_confidence = existing.get("confidence")
    if existing_confidence in AFFORDANCE_CONFIDENCE_VALUES:
        merged["confidence"] = existing_confidence

    baseline_source = baseline.get("source_affordances", {})
    existing_source = existing.get("source_affordances", {})
    merged["source_affordances"] = {
        "available_channels": _deduplicate_strings(
            _normalize_available_channels(baseline_source.get("available_channels", []))
            + _normalize_available_channels(existing_source.get("available_channels", []))
        ),
        "channel_descriptors": {
            channel: _deduplicate_strings(
                _normalize_channel_descriptors(baseline_source.get("channel_descriptors", {})).get(
                    channel,
                    [],
                )
                + _normalize_channel_descriptors(
                    existing_source.get("channel_descriptors", {})
                ).get(channel, [])
            )
            for channel in PUBLISHED_EVIDENCE_CHANNELS
        },
        "evidence_refs": {
            channel: _deduplicate_strings(
                _normalize_evidence_refs(baseline_source.get("evidence_refs", {})).get(
                    channel,
                    [],
                )
                + _normalize_evidence_refs(existing_source.get("evidence_refs", {})).get(
                    channel,
                    [],
                )
            )
            for channel in PUBLISHED_EVIDENCE_CHANNELS
        },
    }

    existing_units = {
        str(item.get("unit_id")): item
        for item in existing.get("unit_affordances", [])
        if isinstance(item, dict) and isinstance(item.get("unit_id"), str)
    }
    merged_units: list[dict[str, Any]] = []
    for baseline_unit in baseline.get("unit_affordances", []):
        if not isinstance(baseline_unit, dict) or not isinstance(baseline_unit.get("unit_id"), str):
            continue
        unit_id = str(baseline_unit["unit_id"])
        existing_unit = existing_units.get(unit_id, {})
        unit_payload = dict(baseline_unit)
        existing_unit_mode = existing_unit.get("derivation_mode")
        if existing_unit_mode in {"agent-authored", "hybrid"}:
            unit_payload["derivation_mode"] = "hybrid"
        elif existing_unit_mode in AFFORDANCE_DERIVATION_MODE_VALUES:
            unit_payload["derivation_mode"] = existing_unit_mode
        existing_unit_confidence = existing_unit.get("confidence")
        if existing_unit_confidence in AFFORDANCE_CONFIDENCE_VALUES:
            unit_payload["confidence"] = existing_unit_confidence
        unit_payload["available_channels"] = _deduplicate_strings(
            _normalize_available_channels(baseline_unit.get("available_channels", []))
            + _normalize_available_channels(existing_unit.get("available_channels", []))
        )
        unit_payload["channel_descriptors"] = {
            channel: _deduplicate_strings(
                _normalize_channel_descriptors(baseline_unit.get("channel_descriptors", {})).get(
                    channel,
                    [],
                )
                + _normalize_channel_descriptors(existing_unit.get("channel_descriptors", {})).get(
                    channel, []
                )
            )
            for channel in PUBLISHED_EVIDENCE_CHANNELS
        }
        unit_payload["evidence_refs"] = {
            channel: _deduplicate_strings(
                _normalize_evidence_refs(baseline_unit.get("evidence_refs", {})).get(channel, [])
                + _normalize_evidence_refs(existing_unit.get("evidence_refs", {})).get(
                    channel,
                    [],
                )
            )
            for channel in PUBLISHED_EVIDENCE_CHANNELS
        }
        merged_units.append(unit_payload)
    merged["unit_affordances"] = merged_units
    return merged


def validate_derived_affordances(
    payload: dict[str, Any],
    *,
    source_manifest: dict[str, Any],
    evidence_manifest: dict[str, Any],
) -> list[str]:
    """Return validation errors for a derived-affordance payload."""
    errors: list[str] = []
    if payload.get("artifact_type") != "derived-affordances":
        errors.append("derived_affordances.json must declare artifact_type=derived-affordances")
    if payload.get("source_id") != source_manifest.get("source_id"):
        errors.append("derived_affordances.json source_id must match source_manifest.json")
    if payload.get("source_fingerprint") != source_manifest.get("source_fingerprint"):
        errors.append("derived_affordances.json source_fingerprint must match source_manifest.json")
    if payload.get("derivation_mode") not in AFFORDANCE_DERIVATION_MODE_VALUES:
        errors.append("derived_affordances.json must use a supported derivation_mode")
    if payload.get("confidence") not in AFFORDANCE_CONFIDENCE_VALUES:
        errors.append("derived_affordances.json must use a supported confidence value")

    source_affordances = payload.get("source_affordances", {})
    _normalize_available_channels(source_affordances.get("available_channels", []))
    _normalize_channel_descriptors(source_affordances.get("channel_descriptors", {}))
    _normalize_evidence_refs(source_affordances.get("evidence_refs", {}))

    unit_ids = {
        str(unit.get("unit_id"))
        for unit in evidence_manifest.get("units", [])
        if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str)
    }
    seen_units: set[str] = set()
    for item in payload.get("unit_affordances", []):
        if not isinstance(item, dict):
            errors.append("unit_affordances entries must be objects")
            continue
        unit_id = item.get("unit_id")
        if not isinstance(unit_id, str) or unit_id not in unit_ids:
            errors.append("unit_affordances entries must reference known unit ids")
            continue
        if unit_id in seen_units:
            errors.append(f"unit_affordances contains duplicate unit_id `{unit_id}`")
            continue
        seen_units.add(unit_id)
        if item.get("derivation_mode") not in AFFORDANCE_DERIVATION_MODE_VALUES:
            errors.append(f"{unit_id} must use a supported derivation_mode")
        if item.get("confidence") not in AFFORDANCE_CONFIDENCE_VALUES:
            errors.append(f"{unit_id} must use a supported confidence value")
        _normalize_available_channels(item.get("available_channels", []))
        _normalize_channel_descriptors(item.get("channel_descriptors", {}))
        _normalize_evidence_refs(item.get("evidence_refs", {}))
    return errors


def available_channels_from_record(record: dict[str, Any]) -> list[str]:
    """Return available published channels for one retrieval/trace record."""
    explicit = _normalize_available_channels(record.get("available_channels", []))
    if explicit:
        return explicit
    channels: list[str] = []
    if any(
        isinstance(record.get(field_name), str) and str(record.get(field_name)).strip()
        for field_name in (
            "text",
            "text_excerpt",
            "summary_en",
            "summary_source",
            "summary_markdown",
        )
    ):
        channels.append("text")
    if record.get("render_references") or record.get("render_paths"):
        channels.append("render")
    if (
        isinstance(record.get("structure_asset"), str)
        and str(record.get("structure_asset")).strip()
    ) or (
        isinstance(record.get("structure_summary"), str)
        and str(record.get("structure_summary")).strip()
    ):
        channels.append("structure")
    if record.get("notes_available") or record.get("notes_excerpt"):
        channels.append("notes")
    if record.get("embedded_media"):
        channels.append("media")
    return _deduplicate_strings(channels)


def channel_descriptors_from_record(record: dict[str, Any]) -> dict[str, list[str]]:
    """Return normalized channel descriptors for one retrieval/trace record."""
    explicit = _normalize_channel_descriptors(record.get("channel_descriptors", {}))
    if any(explicit[channel] for channel in PUBLISHED_EVIDENCE_CHANNELS):
        return explicit
    descriptors: dict[str, list[str]] = {channel: [] for channel in PUBLISHED_EVIDENCE_CHANNELS}
    title = str(record.get("title") or "").strip()
    if title:
        descriptors["text"].append(title)
    text_excerpt = str(record.get("text") or record.get("text_excerpt") or "").strip()
    if text_excerpt:
        descriptors["text"].append(_truncate(text_excerpt))
    if record.get("render_references") or record.get("render_paths"):
        descriptors["render"].append("Published rendered evidence is available.")
    structure_summary = str(record.get("structure_summary") or "").strip()
    if structure_summary:
        descriptors["structure"].append(_truncate(structure_summary))
    notes_excerpt = str(record.get("notes_excerpt") or "").strip()
    if notes_excerpt:
        descriptors["notes"].append(_truncate(notes_excerpt))
    embedded_media = record.get("embedded_media", [])
    if isinstance(embedded_media, list) and embedded_media:
        descriptors["media"].append("Embedded media or drawing references are available.")
    return _normalize_channel_descriptors(descriptors)


def confidence_from_record(record: dict[str, Any]) -> str:
    """Backfill affordance confidence from a record when needed."""
    explicit = record.get("affordance_confidence")
    if explicit in AFFORDANCE_CONFIDENCE_VALUES:
        return str(explicit)
    extraction_confidence = record.get("extraction_confidence")
    if extraction_confidence == "high":
        return "high"
    if extraction_confidence == "medium":
        return "medium"
    return _confidence_from_channel_count(len(available_channels_from_record(record)))


def support_channels_from_supports(supports: list[dict[str, Any]]) -> list[str]:
    """Collect the used published evidence channels from traced supports."""
    channels: list[str] = []
    for support in supports:
        if not isinstance(support, dict):
            continue
        channels.extend(available_channels_from_record(support))
    return _deduplicate_strings(channels)


def _channels_from_results(
    results: list[dict[str, Any]],
    *,
    inspection_scope: str,
) -> list[str]:
    channels: list[str] = []
    if inspection_scope == "source":
        for result in results:
            if isinstance(result, dict):
                channels.extend(available_channels_from_record(result))
                matched_artifacts = result.get("matched_artifacts", [])
                if isinstance(matched_artifacts, list):
                    for artifact in matched_artifacts:
                        if isinstance(artifact, dict):
                            channels.extend(available_channels_from_record(artifact))
                matched_units = result.get("matched_units", [])
                if isinstance(matched_units, list):
                    for unit in matched_units:
                        if isinstance(unit, dict):
                            channels.extend(available_channels_from_record(unit))
                            matched_artifacts = unit.get("matched_artifacts", [])
                            if isinstance(matched_artifacts, list):
                                for artifact in matched_artifacts:
                                    if isinstance(artifact, dict):
                                        channels.extend(available_channels_from_record(artifact))
        return _deduplicate_strings(channels)
    for result in results:
        if not isinstance(result, dict):
            continue
        matched_artifacts = result.get("matched_artifacts", [])
        if isinstance(matched_artifacts, list):
            for artifact in matched_artifacts:
                if isinstance(artifact, dict):
                    channels.extend(available_channels_from_record(artifact))
        matched_units = result.get("matched_units", [])
        if isinstance(matched_units, list) and matched_units:
            for unit in matched_units:
                if isinstance(unit, dict):
                    channels.extend(available_channels_from_record(unit))
                    matched_artifacts = unit.get("matched_artifacts", [])
                    if isinstance(matched_artifacts, list):
                        for artifact in matched_artifacts:
                            if isinstance(artifact, dict):
                                channels.extend(available_channels_from_record(artifact))
        else:
            channels.extend(available_channels_from_record(result))
    return _deduplicate_strings(channels)


def _semantic_gap_hints_from_record(record: dict[str, Any]) -> list[str]:
    value = record.get("semantic_gap_hints", [])
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _record_has_overlay_coverage(record: dict[str, Any]) -> bool:
    semantic_labels = record.get("semantic_labels", [])
    semantic_overlay_asset = record.get("semantic_overlay_asset")
    return bool(
        isinstance(semantic_labels, list)
        and any(isinstance(item, str) and item.strip() for item in semantic_labels)
    ) or bool(isinstance(semantic_overlay_asset, str) and semantic_overlay_asset.strip())


def _published_hard_artifact_gap_reason(results: list[dict[str, Any]]) -> str:
    messages: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        source_label = str(result.get("title") or result.get("source_id") or "source")
        matched_artifacts = result.get("matched_artifacts", [])
        if isinstance(matched_artifacts, list):
            for artifact in matched_artifacts:
                if not isinstance(artifact, dict):
                    continue
                gap_hints = [
                    hint
                    for hint in _semantic_gap_hints_from_record(artifact)
                    if hint in BLOCKING_HARD_ARTIFACT_GAP_HINTS
                ]
                if gap_hints and not _record_has_overlay_coverage(artifact):
                    messages.append(
                        f"{source_label} artifact "
                        f"{artifact.get('artifact_id') or artifact.get('title')}: "
                        + ", ".join(gap_hints[:3])
                    )
        matched_units = result.get("matched_units", [])
        if isinstance(matched_units, list):
            for unit in matched_units:
                if not isinstance(unit, dict):
                    continue
                gap_hints = [
                    hint
                    for hint in _semantic_gap_hints_from_record(unit)
                    if hint in BLOCKING_HARD_ARTIFACT_GAP_HINTS
                ]
                page_image_artifact_id = unit.get("page_image_artifact_id")
                if isinstance(page_image_artifact_id, str) and page_image_artifact_id:
                    gap_hints.append("page-image-target")
                gap_hints = _deduplicate_strings(gap_hints)
                if gap_hints and not _record_has_overlay_coverage(unit):
                    messages.append(
                        f"{source_label} unit {unit.get('unit_id')}: " + ", ".join(gap_hints[:3])
                    )
        if messages:
            break
    return "; ".join(messages[:2])


def plan_published_evidence(
    *,
    results: list[dict[str, Any]],
    evidence_requirements: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the KB-native published evidence plan for a retrieval step."""
    requirements = evidence_requirements if isinstance(evidence_requirements, dict) else {}
    preferred_channels = _normalize_available_channels(requirements.get("preferred_channels", []))
    inspection_scope = requirements.get("inspection_scope")
    if inspection_scope not in INSPECTION_SCOPE_VALUES:
        inspection_scope = "unit"
    prefer_published_artifacts = requirements.get("prefer_published_artifacts")
    if not isinstance(prefer_published_artifacts, bool):
        prefer_published_artifacts = True

    matched_channels = _channels_from_results(results, inspection_scope=inspection_scope)
    if preferred_channels:
        published_artifacts_sufficient = set(preferred_channels).issubset(set(matched_channels))
    else:
        published_artifacts_sufficient = bool(results)
    hard_artifact_gap_reason = _published_hard_artifact_gap_reason(results)
    if published_artifacts_sufficient and hard_artifact_gap_reason:
        published_artifacts_sufficient = False

    source_escalation_required = prefer_published_artifacts and not published_artifacts_sufficient
    if source_escalation_required and hard_artifact_gap_reason:
        reason = (
            "Published deterministic artifacts still expose unresolved "
            "hard-artifact semantic gaps. Run hybrid multimodal enrichment "
            "before relying on source-level fallback: "
            + hard_artifact_gap_reason
            + "."
        )
    elif results and preferred_channels and source_escalation_required:
        missing_channels = [
            channel for channel in preferred_channels if channel not in matched_channels
        ]
        reason = (
            "Published retrieval results did not expose the preferred evidence channels: "
            + ", ".join(missing_channels)
            + "."
        )
    elif not results and source_escalation_required:
        reason = "No published retrieval results were available for the requested evidence scope."
    else:
        reason = ""

    return {
        "preferred_channels": preferred_channels,
        "matched_published_channels": matched_channels,
        "inspection_scope": inspection_scope,
        "published_artifacts_sufficient": published_artifacts_sufficient,
        "source_escalation_required": source_escalation_required,
        "source_escalation_reason": reason,
    }

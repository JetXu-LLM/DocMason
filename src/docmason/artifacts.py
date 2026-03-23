"""Shared artifact contracts and helper utilities."""

from __future__ import annotations

import re
from typing import Any

ARTIFACT_GRAPH_PROMOTION_TYPES = frozenset({"table", "chart", "major-region"})


def deduplicate_strings(values: list[str]) -> list[str]:
    """Deduplicate non-empty strings while preserving order."""
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def stable_artifact_id(unit_id: str, artifact_type: str, ordinal: int) -> str:
    """Return a deterministic artifact identifier within one source."""
    return f"{unit_id}:{artifact_type}-{ordinal:03d}"


def artifact_graph_promoted(
    *,
    artifact_type: str,
    high_confidence: bool,
    explicit_relation: bool = False,
) -> bool:
    """Return whether an artifact is eligible for mixed-resolution graph participation."""
    if artifact_type not in ARTIFACT_GRAPH_PROMOTION_TYPES:
        return False
    return explicit_relation or high_confidence


def normalize_bbox(
    bbox: dict[str, float] | None,
    *,
    width: float | int | None,
    height: float | int | None,
) -> dict[str, float] | None:
    """Return a normalized bounding box when source dimensions are available."""
    if not isinstance(bbox, dict):
        return None
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return None
    if width <= 0 or height <= 0:
        return None
    try:
        x0 = float(bbox["x0"])
        y0 = float(bbox["y0"])
        x1 = float(bbox["x1"])
        y1 = float(bbox["y1"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "x0": round(x0 / float(width), 6),
        "y0": round(y0 / float(height), 6),
        "x1": round(x1 / float(width), 6),
        "y1": round(y1 / float(height), 6),
    }


def artifact_locator_aliases(
    *,
    artifact_type: str,
    title: str,
    unit_title: str | None = None,
    extra_aliases: list[str] | None = None,
) -> list[str]:
    """Return conservative human-facing aliases for one artifact."""
    aliases: list[str] = []
    clean_title = " ".join(title.split()).strip()
    if clean_title:
        aliases.append(clean_title)
        aliases.append(f"{artifact_type} {clean_title}")
    clean_unit_title = " ".join((unit_title or "").split()).strip()
    if clean_unit_title and clean_title:
        aliases.append(f"{clean_unit_title} {clean_title}")
        aliases.append(f"{clean_unit_title} {artifact_type}")
    if isinstance(extra_aliases, list):
        aliases.extend(alias for alias in extra_aliases if isinstance(alias, str) and alias.strip())
    return deduplicate_strings(aliases)


def artifact_title_from_text(
    text: str,
    *,
    artifact_type: str,
    fallback: str,
    limit: int = 96,
) -> str:
    """Return a short artifact title from extracted text when available."""
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return fallback
    if len(compact) <= limit:
        return compact
    truncated = compact[:limit].rsplit(" ", 1)[0].strip()
    return truncated or fallback


def validate_artifact_index(
    artifact_index: dict[str, Any],
    *,
    source_id: str,
) -> list[str]:
    """Validate the minimum Phase 3 artifact-index contract."""
    errors: list[str] = []
    if artifact_index.get("source_id") != source_id:
        errors.append("artifact_index.json source_id does not match the source directory")
    artifacts = artifact_index.get("artifacts", [])
    if not isinstance(artifacts, list):
        return ["artifact_index.json must contain an `artifacts` list"]
    seen_ids: set[str] = set()
    for item in artifacts:
        if not isinstance(item, dict):
            errors.append("artifact_index.json artifacts must be objects")
            continue
        artifact_id = item.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            errors.append("artifact_index.json artifacts require a non-empty artifact_id")
            continue
        if artifact_id in seen_ids:
            errors.append(f"Duplicate artifact_id `{artifact_id}` in artifact_index.json")
        seen_ids.add(artifact_id)
        for field_name in (
            "artifact_type",
            "unit_id",
            "title",
            "artifact_path",
        ):
            value = item.get(field_name)
            if not isinstance(value, str) or not value:
                errors.append(
                    f"artifact_index.json artifact `{artifact_id}` is missing `{field_name}`"
                )
        locator_aliases = item.get("locator_aliases", [])
        if not isinstance(locator_aliases, list):
            errors.append(
                f"artifact_index.json artifact `{artifact_id}` must expose `locator_aliases`"
            )
        available_channels = item.get("available_channels", [])
        if not isinstance(available_channels, list):
            errors.append(
                f"artifact_index.json artifact `{artifact_id}` must expose `available_channels`"
            )
        focus_render_assets = item.get("focus_render_assets")
        if focus_render_assets is not None and not isinstance(focus_render_assets, list):
            errors.append(
                "artifact_index.json artifact "
                f"`{artifact_id}` must expose `focus_render_assets` as a list"
            )
    return errors


def validate_pdf_document(
    pdf_document: dict[str, Any],
    *,
    source_id: str,
    unit_ids: set[str],
    artifact_ids: set[str],
) -> list[str]:
    """Validate the minimum deterministic PDF document sidecar contract."""
    errors: list[str] = []
    if pdf_document.get("source_id") != source_id:
        errors.append("pdf_document.json source_id does not match the source directory")
    if pdf_document.get("artifact_type") != "pdf-document":
        errors.append("pdf_document.json must declare artifact_type `pdf-document`")
    if pdf_document.get("derivation_mode") != "deterministic":
        errors.append("pdf_document.json must declare derivation_mode `deterministic`")

    for field_name in (
        "outline_nodes",
        "page_contexts",
        "caption_links",
        "continuation_links",
        "procedure_spans",
        "document_role_hints",
    ):
        if not isinstance(pdf_document.get(field_name), list):
            errors.append(f"pdf_document.json must include `{field_name}` as a list")

    for node in pdf_document.get("outline_nodes", []):
        if not isinstance(node, dict):
            errors.append("pdf_document.json outline_nodes entries must be objects")
            continue
        title = node.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append("pdf_document.json outline_nodes entries require a non-empty title")
        page_ordinal = node.get("page_ordinal")
        if page_ordinal is not None and not isinstance(page_ordinal, int):
            errors.append("pdf_document.json outline_nodes page_ordinal must be an integer")

    for context in pdf_document.get("page_contexts", []):
        if not isinstance(context, dict):
            errors.append("pdf_document.json page_contexts entries must be objects")
            continue
        unit_id = context.get("unit_id")
        if unit_id not in unit_ids:
            errors.append(f"pdf_document.json references unknown unit_id `{unit_id}`")
        section_path = context.get("section_path")
        if section_path is not None and not isinstance(section_path, list):
            errors.append("pdf_document.json page_contexts section_path must be a list")
        heading_candidates = context.get("heading_candidates", [])
        if not isinstance(heading_candidates, list):
            errors.append("pdf_document.json page_contexts heading_candidates must be a list")

    for link in pdf_document.get("caption_links", []):
        if not isinstance(link, dict):
            errors.append("pdf_document.json caption_links entries must be objects")
            continue
        target_artifact_id = link.get("target_artifact_id")
        if target_artifact_id not in artifact_ids:
            errors.append(
                "pdf_document.json caption_links references unknown target_artifact_id "
                f"`{target_artifact_id}`"
            )

    for link in pdf_document.get("continuation_links", []):
        if not isinstance(link, dict):
            errors.append("pdf_document.json continuation_links entries must be objects")
            continue
        for field_name in ("from_unit_id", "to_unit_id"):
            value = link.get(field_name)
            if value not in unit_ids:
                errors.append(
                    "pdf_document.json continuation_links references unknown "
                    f"{field_name} `{value}`"
                )
        for field_name in ("from_artifact_id", "to_artifact_id"):
            value = link.get(field_name)
            if value is not None and value not in artifact_ids:
                errors.append(
                    "pdf_document.json continuation_links references unknown "
                    f"{field_name} `{value}`"
                )

    for span in pdf_document.get("procedure_spans", []):
        if not isinstance(span, dict):
            errors.append("pdf_document.json procedure_spans entries must be objects")
            continue
        unit_id = span.get("unit_id")
        if unit_id not in unit_ids:
            errors.append(
                f"pdf_document.json procedure_spans references unknown unit_id `{unit_id}`"
            )
        artifact_id = span.get("artifact_id")
        if artifact_id is not None and artifact_id not in artifact_ids:
            errors.append(
                f"pdf_document.json procedure_spans references unknown artifact_id `{artifact_id}`"
            )
    return errors

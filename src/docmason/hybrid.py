"""Shared hybrid-enrichment governance and focus-render helpers."""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image

from .artifacts import deduplicate_strings
from .project import WorkspacePaths, read_json, write_json
from .semantic_overlays import load_semantic_overlays, semantic_overlay_candidates

HYBRID_OVERLAY_SLOTS = (
    "page-summary",
    "diagram-summary",
    "flow-steps",
    "ui-screen-summary",
    "chart-intent",
    "table-intent",
    "manual-step-sequence",
    "architecture-relationship-hints",
    "cross-region-relation",
)
HYBRID_HARD_ARTIFACT_TYPES = frozenset(
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
REASON_SLOT_HINTS = {
    "image-only-page": ("page-summary",),
    "scanned-page-like": ("page-summary",),
    "weak-section-confidence": ("page-summary",),
    "diagram-or-ui-page": ("diagram-summary",),
    "diagram-or-ui-slide": ("diagram-summary",),
    "grouped-diagram-slide": ("diagram-summary", "architecture-relationship-hints"),
    "connector-heavy-slide": ("diagram-summary", "flow-steps", "cross-region-relation"),
    "picture-heavy-slide": ("page-summary",),
    "chart-slide": ("chart-intent",),
    "chart-or-dashboard-page": ("chart-intent",),
    "visual-heavy-page": ("page-summary",),
    "procedure-page": ("manual-step-sequence",),
    "continued-structure-page": ("page-summary", "cross-region-relation"),
    "dashboard-like-sheet": ("chart-intent", "table-intent"),
    "chart-sheet": ("chart-intent",),
    "multi-table-sheet": ("table-intent", "cross-region-relation"),
    "picture-heavy-sheet": ("page-summary",),
    "table-section": ("table-intent",),
    "image-section": ("page-summary",),
    "figure-heavy-section": ("diagram-summary", "page-summary"),
    "layout-sensitive-section": ("diagram-summary",),
    "rendered-only-diagram-section": ("diagram-summary",),
}
KIND_SLOT_HINTS = {
    "page-image": ("page-summary",),
    "chart": ("chart-intent",),
    "table": ("table-intent",),
    "picture": ("page-summary",),
    "major-region": ("diagram-summary",),
    "group": ("diagram-summary", "architecture-relationship-hints"),
    "connector": ("flow-steps", "cross-region-relation"),
    "auto-shape": ("diagram-summary",),
    "unit-render": ("page-summary",),
}
GAP_SLOT_HINTS = {
    "weak-label-slide": ("diagram-summary",),
    "rendered-only-picture": ("page-summary",),
    "rendered-only-diagram-section": ("diagram-summary",),
    "chart-table-semantic-gap": ("chart-intent", "table-intent", "cross-region-relation"),
    "image-heavy-sheet": ("page-summary",),
    "picture-heavy-sheet": ("page-summary",),
    "none-text-layer": ("page-summary",),
    "weak-text-layer": ("page-summary",),
}
LANE_B_NORMAL_LIMITS = {
    "max_units": 12,
    "max_sources": 4,
    "max_units_per_source": 3,
}


def _sanitize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def _safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return _sanitize_text(path.read_text(encoding="utf-8"))


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def overlay_slots() -> list[str]:
    """Return the ordered overlay slot contract."""
    return list(HYBRID_OVERLAY_SLOTS)


def _normalized_page_span(value: Any) -> dict[str, int] | None:
    if (
        isinstance(value, dict)
        and isinstance(value.get("start"), int)
        and isinstance(value.get("end"), int)
    ):
        return {"start": int(value["start"]), "end": int(value["end"])}
    return None


def _unit_lookup(evidence_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(unit["unit_id"]): unit
        for unit in evidence_manifest.get("units", [])
        if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str)
    }


def _artifact_lookup(source_dir: Path) -> dict[str, dict[str, Any]]:
    artifact_index = read_json(source_dir / "artifact_index.json")
    return {
        str(artifact["artifact_id"]): artifact
        for artifact in artifact_index.get("artifacts", [])
        if isinstance(artifact, dict) and isinstance(artifact.get("artifact_id"), str)
    }


def _artifacts_by_unit(source_dir: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for artifact in _artifact_lookup(source_dir).values():
        unit_id = artifact.get("unit_id")
        if isinstance(unit_id, str) and unit_id:
            grouped[unit_id].append(artifact)
    return dict(grouped)


def _unit_render_assets(unit: dict[str, Any]) -> list[str]:
    render_assets = unit.get("render_assets", [])
    if isinstance(render_assets, list):
        normalized = [value for value in render_assets if isinstance(value, str) and value]
        if normalized:
            return normalized
    rendered_asset = unit.get("rendered_asset")
    if isinstance(rendered_asset, str) and rendered_asset:
        return [rendered_asset]
    return []


def _unit_render_page_span(unit: dict[str, Any]) -> dict[str, int] | None:
    return _normalized_page_span(unit.get("render_page_span"))


def infer_overlay_slots(
    payload: dict[str, Any],
    *,
    fallback_reason: str | None = None,
) -> list[str]:
    """Infer covered slots conservatively when the overlay omitted them."""
    explicit = payload.get("covered_slots")
    if isinstance(explicit, list):
        normalized = [
            slot
            for slot in explicit
            if isinstance(slot, str) and slot in HYBRID_OVERLAY_SLOTS
        ]
        if normalized:
            return deduplicate_strings(normalized)
    slots: list[str] = []
    for field_name in ("semantic_labels", "artifact_annotations"):
        items = payload.get(field_name, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for key in ("label", "summary", "text"):
                value = item.get(key)
                if not isinstance(value, str):
                    continue
                for slot in HYBRID_OVERLAY_SLOTS:
                    if slot in value:
                        slots.append(slot)
    if (
        isinstance(payload.get("cross_region_relations"), list)
        and payload["cross_region_relations"]
    ):
        slots.append("cross-region-relation")
    if fallback_reason:
        slots.extend(REASON_SLOT_HINTS.get(fallback_reason, ()))
    return deduplicate_strings(
        [slot for slot in slots if slot in HYBRID_OVERLAY_SLOTS]
    )


def required_overlay_slots(candidate: dict[str, Any]) -> list[str]:
    """Return the required overlay slots for one hybrid candidate."""
    slots: list[str] = []
    for reason in candidate.get("all_reasons", []):
        if isinstance(reason, str):
            slots.extend(REASON_SLOT_HINTS.get(reason, ()))
    for kind in candidate.get("candidate_kinds", []):
        if isinstance(kind, str):
            slots.extend(KIND_SLOT_HINTS.get(kind, ()))
    for hint in candidate.get("insufficiency_signals", []):
        if isinstance(hint, str):
            slots.extend(GAP_SLOT_HINTS.get(hint, ()))
    if len(
        [
            artifact_id
            for artifact_id in candidate.get("target_artifact_ids", [])
            if isinstance(artifact_id, str) and artifact_id
        ]
    ) >= 2:
        slots.append("cross-region-relation")
    normalized = deduplicate_strings(
        [slot for slot in slots if slot in HYBRID_OVERLAY_SLOTS]
    )
    return normalized or ["page-summary"]


def suggested_overlay_kinds(candidate: dict[str, Any]) -> list[str]:
    """Return the recommended authored overlay kinds for one candidate."""
    return required_overlay_slots(candidate)


def compute_unit_evidence_fingerprint(source_dir: Path, unit_id: str) -> str:
    """Return a stable unit evidence fingerprint for overlay freshness checks."""
    evidence_manifest = read_json(source_dir / "evidence_manifest.json")
    unit_lookup = _unit_lookup(evidence_manifest)
    unit = dict(unit_lookup.get(unit_id) or {})
    artifacts = [
        artifact
        for artifact in _artifact_lookup(source_dir).values()
        if artifact.get("unit_id") == unit_id
    ]
    text_asset = unit.get("text_asset")
    structure_asset = unit.get("structure_asset")
    payload = {
        "unit_id": unit_id,
        "title": unit.get("title"),
        "unit_type": unit.get("unit_type"),
        "ordinal": unit.get("ordinal"),
        "render_assets": _unit_render_assets(unit),
        "render_page_span": _unit_render_page_span(unit),
        "text": (
            _safe_read_text(source_dir / text_asset)
            if isinstance(text_asset, str) and text_asset
            else ""
        ),
        "structure": (
            read_json(source_dir / structure_asset)
            if isinstance(structure_asset, str) and structure_asset
            else {}
        ),
        "semantic_gap_hints": unit.get("semantic_gap_hints", []),
        "artifacts": [
            {
                "artifact_id": artifact.get("artifact_id"),
                "artifact_type": artifact.get("artifact_type"),
                "title": artifact.get("title"),
                "bbox": artifact.get("bbox"),
                "normalized_bbox": artifact.get("normalized_bbox"),
                "render_assets": artifact.get("render_assets", []),
                "render_page_span": artifact.get("render_page_span"),
                "focus_render_assets": artifact.get("focus_render_assets", []),
                "linked_text": artifact.get("linked_text"),
                "caption_text": artifact.get("caption_text"),
                "visual_hints": artifact.get("visual_hints", []),
                "semantic_gap_hints": artifact.get("semantic_gap_hints", []),
            }
            for artifact in sorted(
                artifacts,
                key=lambda item: (
                    str(item.get("artifact_type") or ""),
                    str(item.get("artifact_id") or ""),
                ),
            )
        ],
    }
    return _stable_hash(payload)


def overlay_is_fresh(
    payload: dict[str, Any],
    *,
    source_fingerprint: str,
    unit_evidence_fingerprint: str,
) -> bool:
    """Return whether one overlay still matches the current deterministic substrate."""
    return (
        payload.get("source_fingerprint") == source_fingerprint
        and payload.get("unit_evidence_fingerprint") == unit_evidence_fingerprint
    )


def evaluate_overlay_coverage(
    candidate: dict[str, Any],
    *,
    overlay_payload: dict[str, Any] | None,
    source_fingerprint: str,
    unit_evidence_fingerprint: str,
) -> dict[str, Any]:
    """Evaluate freshness and slot coverage for one candidate against its overlay."""
    required_slots = required_overlay_slots(candidate)
    if not isinstance(overlay_payload, dict) or not overlay_payload:
        return {
            "overlay_present": False,
            "overlay_fresh": False,
            "required_slots": required_slots,
            "covered_slots": [],
            "blocked_slots": [],
            "remaining_slots": required_slots,
            "coverage_status": "candidate-prepared",
            "blocked_reasons": [],
        }

    fresh = overlay_is_fresh(
        overlay_payload,
        source_fingerprint=source_fingerprint,
        unit_evidence_fingerprint=unit_evidence_fingerprint,
    )
    covered_slots = (
        infer_overlay_slots(
            overlay_payload,
            fallback_reason=str(candidate.get("eligible_reason") or ""),
        )
        if fresh
        else []
    )
    blocked_slots = (
        deduplicate_strings(
            [
                slot
                for slot in overlay_payload.get("blocked_slots", [])
                if isinstance(slot, str) and slot in HYBRID_OVERLAY_SLOTS
            ]
        )
        if fresh and isinstance(overlay_payload.get("blocked_slots"), list)
        else []
    )
    remaining_slots = [
        slot
        for slot in required_slots
        if slot not in covered_slots and slot not in blocked_slots
    ]
    uncertainty_notes = overlay_payload.get("uncertainty_notes", [])
    blocked_reasons = (
        [
            _sanitize_text(note)
            for note in uncertainty_notes
            if isinstance(note, str) and _sanitize_text(note)
        ]
        if isinstance(uncertainty_notes, list)
        else []
    )
    if not fresh:
        coverage_status = "candidate-prepared"
        blocked_reasons = []
        covered_slots = []
        blocked_slots = []
        remaining_slots = required_slots
    elif not remaining_slots:
        coverage_status = "covered" if covered_slots else "blocked"
    elif covered_slots or blocked_slots:
        coverage_status = "partially-covered"
    else:
        coverage_status = "candidate-prepared"
    if blocked_slots and not blocked_reasons:
        blocked_reasons = [
            f"Overlay explicitly marked `{slot}` as blocked." for slot in blocked_slots
        ]
    return {
        "overlay_present": True,
        "overlay_fresh": fresh,
        "required_slots": required_slots,
        "covered_slots": covered_slots,
        "blocked_slots": blocked_slots,
        "remaining_slots": remaining_slots,
        "coverage_status": coverage_status,
        "blocked_reasons": blocked_reasons,
    }


def focus_render_targets_for_unit(
    source_dir: Path,
    *,
    unit_id: str,
    target_artifact_ids: list[str] | None = None,
) -> list[str]:
    """Return ordered focus renders for one candidate unit."""
    evidence_manifest = read_json(source_dir / "evidence_manifest.json")
    unit = _unit_lookup(evidence_manifest).get(unit_id, {})
    artifact_lookup = _artifact_lookup(source_dir)
    focus_assets: list[str] = []
    artifact_ids = [
        artifact_id
        for artifact_id in (target_artifact_ids or [])
        if isinstance(artifact_id, str) and artifact_id
    ]
    if artifact_ids:
        for artifact_id in artifact_ids:
            artifact = artifact_lookup.get(artifact_id)
            if not isinstance(artifact, dict):
                continue
            focus_assets.extend(
                asset
                for asset in artifact.get("focus_render_assets", [])
                if isinstance(asset, str) and asset
            )
            focus_assets.extend(
                asset
                for asset in artifact.get("render_assets", [])
                if isinstance(asset, str) and asset
            )
    if not focus_assets:
        for artifact in artifact_lookup.values():
            if artifact.get("unit_id") != unit_id:
                continue
            focus_assets.extend(
                asset
                for asset in artifact.get("focus_render_assets", [])
                if isinstance(asset, str) and asset
            )
    if not focus_assets:
        focus_assets.extend(_unit_render_assets(unit))
    return deduplicate_strings(focus_assets)


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, str]:
    coverage_order = {
        "candidate-prepared": 0,
        "partially-covered": 1,
        "blocked": 2,
        "covered": 3,
    }
    return (
        coverage_order.get(str(candidate.get("coverage_status") or "candidate-prepared"), 0),
        -int(candidate.get("priority", 0)),
        str(candidate.get("unit_id") or ""),
    )


def build_source_hybrid_packet(
    source_dir: Path,
    *,
    evidence_manifest: dict[str, Any],
    source_manifest: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the source-level hybrid packet for one staged or published source."""
    base_candidates = semantic_overlay_candidates(source_dir, evidence_manifest=evidence_manifest)
    if not base_candidates:
        return None
    unit_lookup = _unit_lookup(evidence_manifest)
    overlays = load_semantic_overlays(source_dir)
    source_fingerprint = str(source_manifest.get("source_fingerprint") or "")
    candidates: list[dict[str, Any]] = []
    covered_count = 0
    blocked_count = 0
    remaining_count = 0
    for candidate in base_candidates:
        unit_id = str(candidate.get("unit_id") or "")
        if not unit_id:
            continue
        unit = unit_lookup.get(unit_id, {})
        unit_fingerprint = compute_unit_evidence_fingerprint(source_dir, unit_id)
        coverage = evaluate_overlay_coverage(
            candidate,
            overlay_payload=overlays.get(unit_id),
            source_fingerprint=source_fingerprint,
            unit_evidence_fingerprint=unit_fingerprint,
        )
        enriched = {
            **candidate,
            "unit_title": str(unit.get("title") or unit_id),
            "unit_evidence_fingerprint": unit_fingerprint,
            "coverage_status": coverage["coverage_status"],
            "required_overlay_slots": coverage["required_slots"],
            "suggested_overlay_kinds": suggested_overlay_kinds(candidate),
            "target_focus_render_assets": focus_render_targets_for_unit(
                source_dir,
                unit_id=unit_id,
                target_artifact_ids=candidate.get("target_artifact_ids", []),
            ),
            "blocked_reasons": coverage["blocked_reasons"],
            "covered_slots": coverage["covered_slots"],
            "blocked_slots": coverage["blocked_slots"],
            "remaining_slots": coverage["remaining_slots"],
            "overlay_present": coverage["overlay_present"],
            "overlay_fresh": coverage["overlay_fresh"],
        }
        candidates.append(enriched)
        if coverage["coverage_status"] == "covered":
            covered_count += 1
        elif coverage["coverage_status"] == "blocked":
            blocked_count += 1
        else:
            remaining_count += 1
    candidates.sort(key=_candidate_sort_key)
    remaining_priorities = [
        int(candidate.get("priority", 0))
        for candidate in candidates
        if candidate.get("coverage_status") in {"candidate-prepared", "partially-covered"}
    ]
    source_hybrid_status: str
    if remaining_count == 0 and blocked_count == 0:
        source_hybrid_status = "covered"
    elif covered_count == 0 and blocked_count == 0:
        source_hybrid_status = "candidate-prepared"
    else:
        source_hybrid_status = "partially-covered"
    return {
        "source_id": str(source_manifest.get("source_id") or source_dir.name),
        "document_type": str(source_manifest.get("document_type") or "unknown"),
        "source_path": str(source_manifest.get("current_path") or ""),
        "source_fingerprint": source_fingerprint,
        "source_hybrid_status": source_hybrid_status,
        "candidate_unit_count": len(candidates),
        "covered_candidate_count": covered_count,
        "remaining_candidate_count": remaining_count,
        "blocked_candidate_count": blocked_count,
        "units": candidates,
        "highest_remaining_priority": max(remaining_priorities or [0]),
    }


def summarize_hybrid_work(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarize source and unit coverage across one hybrid-work payload."""
    eligible_unit_count = 0
    covered_unit_count = 0
    blocked_unit_count = 0
    remaining_unit_count = 0
    overlay_unit_count = 0
    covered_source_count = 0
    remaining_source_count = 0
    sources_with_candidates = 0
    sources_with_overlays = 0
    candidate_units: list[dict[str, Any]] = []
    for source in payload.get("sources", []):
        if not isinstance(source, dict):
            continue
        units = [unit for unit in source.get("units", []) if isinstance(unit, dict)]
        if not units:
            continue
        sources_with_candidates += 1
        eligible_unit_count += len(units)
        if any(bool(unit.get("overlay_present")) for unit in units):
            sources_with_overlays += 1
        for unit in units:
            candidate_units.append({"source_id": source.get("source_id"), **unit})
            if unit.get("overlay_present"):
                overlay_unit_count += 1
            coverage_status = str(unit.get("coverage_status") or "candidate-prepared")
            if coverage_status == "covered":
                covered_unit_count += 1
            elif coverage_status == "blocked":
                blocked_unit_count += 1
            else:
                remaining_unit_count += 1
        if int(source.get("remaining_candidate_count", 0)) > 0:
            remaining_source_count += 1
        elif int(source.get("blocked_candidate_count", 0)) == 0:
            covered_source_count += 1
    if eligible_unit_count <= 0:
        mode = "not-needed"
        detail = "No high-value semantic overlay candidates were detected for this corpus state."
    elif remaining_unit_count == 0 and blocked_unit_count == 0:
        mode = "covered"
        detail = (
            "Semantic overlay coverage is present for all current hybrid candidates."
        )
    elif covered_unit_count == 0 and blocked_unit_count == 0:
        mode = "candidate-prepared"
        detail = (
            "Deterministic baseline is ready. A capable host multimodal workflow must "
            "consume hybrid_work.json and write additive semantic overlays."
        )
    else:
        mode = "partially-covered"
        detail = (
            "Hybrid overlay coverage is partial. Remaining candidate units still need "
            "source-scoped multimodal completion or explicit blocked-slot accounting."
        )
    return {
        "mode": mode,
        "eligible_unit_count": eligible_unit_count,
        "covered_unit_count": covered_unit_count,
        "blocked_unit_count": blocked_unit_count,
        "remaining_unit_count": remaining_unit_count,
        "overlay_unit_count": overlay_unit_count,
        "sources_with_candidates": sources_with_candidates,
        "sources_with_overlays": sources_with_overlays,
        "covered_source_count": covered_source_count,
        "remaining_source_count": remaining_source_count,
        "candidate_units": candidate_units,
        "detail": detail,
    }


def select_lane_b_batch(
    hybrid_work: dict[str, Any],
    *,
    max_units: int = LANE_B_NORMAL_LIMITS["max_units"],
    max_sources: int = LANE_B_NORMAL_LIMITS["max_sources"],
    max_units_per_source: int = LANE_B_NORMAL_LIMITS["max_units_per_source"],
) -> list[dict[str, Any]]:
    """Return the normal-mode Lane B source batch from one hybrid-work payload."""
    selected_sources: list[dict[str, Any]] = []
    selected_units = 0
    for source in sorted(
        [
            item
            for item in hybrid_work.get("sources", [])
            if isinstance(item, dict) and int(item.get("remaining_candidate_count", 0)) > 0
        ],
        key=lambda item: (
            -int(item.get("highest_remaining_priority", 0)),
            -int(item.get("remaining_candidate_count", 0)),
            str(item.get("source_path") or ""),
            str(item.get("source_id") or ""),
        ),
    ):
        if len(selected_sources) >= max_sources or selected_units >= max_units:
            break
        remaining = [
            unit
            for unit in source.get("units", [])
            if isinstance(unit, dict)
            and unit.get("coverage_status") in {"candidate-prepared", "partially-covered"}
        ]
        if not remaining:
            continue
        limit = min(max_units_per_source, max_units - selected_units)
        if limit <= 0:
            break
        chosen_units = remaining[:limit]
        selected_units += len(chosen_units)
        selected_sources.append(
            {
                "source_id": source.get("source_id"),
                "document_type": source.get("document_type"),
                "source_path": source.get("source_path"),
                "source_fingerprint": source.get("source_fingerprint"),
                "units": chosen_units,
            }
        )
    return selected_sources


def lane_b_batch_signature(selected_sources: list[dict[str, Any]]) -> str:
    """Return a stable signature for one bounded Lane B batch."""
    normalized_sources: list[dict[str, Any]] = []
    for source in selected_sources:
        if not isinstance(source, dict):
            continue
        normalized_units: list[dict[str, Any]] = []
        for unit in source.get("units", []):
            if not isinstance(unit, dict):
                continue
            normalized_units.append(
                {
                    "unit_id": unit.get("unit_id"),
                    "priority": unit.get("priority"),
                    "required_overlay_slots": sorted(
                        slot
                        for slot in unit.get("required_overlay_slots", [])
                        if isinstance(slot, str) and slot
                    ),
                    "target_artifact_ids": sorted(
                        artifact_id
                        for artifact_id in unit.get("target_artifact_ids", [])
                        if isinstance(artifact_id, str) and artifact_id
                    ),
                }
            )
        normalized_sources.append(
            {
                "source_id": source.get("source_id"),
                "source_fingerprint": source.get("source_fingerprint"),
                "units": normalized_units,
            }
        )
    return _stable_hash(normalized_sources)


def lane_b_work_path(paths: WorkspacePaths, *, job_id: str) -> Path:
    """Return the canonical work-packet path for one governed Lane B batch."""
    return paths.shared_jobs_dir / job_id / "lane_b_work.json"


def write_lane_b_work_packet(
    paths: WorkspacePaths,
    *,
    job_id: str,
    target: str,
    staging_source_signature: str,
    selected_sources: list[dict[str, Any]],
) -> str:
    """Persist the bounded staging-scoped Lane B work packet for a host agent."""
    work_path = lane_b_work_path(paths, job_id=job_id)
    write_json(
        work_path,
        {
            "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
            "job_id": job_id,
            "target": target,
            "staging_source_signature": staging_source_signature,
            "hybrid_work_path": str(paths.hybrid_work_path(target).relative_to(paths.root)),
            "selection_limits": dict(LANE_B_NORMAL_LIMITS),
            "selected_source_ids": [
                source_id
                for source_id in [source.get("source_id") for source in selected_sources]
                if isinstance(source_id, str) and source_id
            ],
            "sources": [source for source in selected_sources if isinstance(source, dict)],
        },
    )
    return str(work_path.relative_to(paths.root))


def lane_b_batch_progress(
    hybrid_work: dict[str, Any],
    *,
    selected_sources: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize whether one bounded Lane B batch is still unresolved."""
    unit_status_lookup: dict[tuple[str, str], str] = {}
    for source in hybrid_work.get("sources", []):
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "")
        if not source_id:
            continue
        for unit in source.get("units", []):
            if not isinstance(unit, dict):
                continue
            unit_id = str(unit.get("unit_id") or "")
            if not unit_id:
                continue
            unit_status_lookup[(source_id, unit_id)] = str(
                unit.get("coverage_status") or "candidate-prepared"
            )

    covered_unit_count = 0
    blocked_unit_count = 0
    remaining_unit_count = 0
    unresolved_units: list[dict[str, str]] = []
    selected_unit_count = 0
    for source in selected_sources:
        if not isinstance(source, dict):
            continue
        source_id = str(source.get("source_id") or "")
        if not source_id:
            continue
        for unit in source.get("units", []):
            if not isinstance(unit, dict):
                continue
            unit_id = str(unit.get("unit_id") or "")
            if not unit_id:
                continue
            selected_unit_count += 1
            coverage_status = unit_status_lookup.get((source_id, unit_id), "missing")
            if coverage_status == "covered":
                covered_unit_count += 1
            elif coverage_status == "blocked":
                blocked_unit_count += 1
            else:
                remaining_unit_count += 1
                unresolved_units.append(
                    {
                        "source_id": source_id,
                        "unit_id": unit_id,
                        "coverage_status": coverage_status,
                    }
                )
    return {
        "selected_unit_count": selected_unit_count,
        "covered_unit_count": covered_unit_count,
        "blocked_unit_count": blocked_unit_count,
        "remaining_unit_count": remaining_unit_count,
        "resolved": remaining_unit_count == 0,
        "unresolved_units": unresolved_units,
    }


def _artifact_render_asset_for_crop(
    source_dir: Path,
    artifact: dict[str, Any],
) -> tuple[Path | None, dict[str, float] | None]:
    normalized_bbox = artifact.get("normalized_bbox")
    if not isinstance(normalized_bbox, dict):
        return None, None
    render_assets = artifact.get("render_assets", [])
    page_span = _normalized_page_span(artifact.get("render_page_span"))
    if (
        not isinstance(render_assets, list)
        or not render_assets
        or page_span is None
        or page_span["start"] != page_span["end"]
    ):
        return None, normalized_bbox
    asset_path = source_dir / str(render_assets[0])
    if not asset_path.exists():
        return None, normalized_bbox
    return asset_path, normalized_bbox


def _crop_artifact_render(
    image_path: Path,
    *,
    normalized_bbox: dict[str, float] | None,
    output_path: Path,
) -> bool:
    with Image.open(image_path) as image:
        if normalized_bbox is None:
            return False
        width, height = image.size
        padding_x = max(int(width * 0.01), 8)
        padding_y = max(int(height * 0.01), 8)
        try:
            x0 = max(int(float(normalized_bbox["x0"]) * width) - padding_x, 0)
            y0 = max(int(float(normalized_bbox["y0"]) * height) - padding_y, 0)
            x1 = min(int(float(normalized_bbox["x1"]) * width) + padding_x, width)
            y1 = min(int(float(normalized_bbox["y1"]) * height) + padding_y, height)
        except (KeyError, TypeError, ValueError):
            return False
        if x1 <= x0 or y1 <= y0:
            return False
        crop_area = (x1 - x0) * (y1 - y0)
        if crop_area >= int(width * height * 0.9):
            return False
        cropped = image.crop((x0, y0, x1, y1))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(
            output_path,
            format="PNG",
            compress_level=1,
            optimize=False,
        )
    return True


def materialize_focus_render_assets(
    source_dir: Path,
    *,
    evidence_manifest: dict[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Create deterministic baseline focus renders from existing published renders."""
    artifact_index_path = source_dir / "artifact_index.json"
    artifact_index = read_json(artifact_index_path)
    resolved_evidence_manifest = evidence_manifest or read_json(
        source_dir / "evidence_manifest.json"
    )
    if not artifact_index or not resolved_evidence_manifest:
        return {}
    unit_lookup = _unit_lookup(resolved_evidence_manifest)
    artifact_focus_map: dict[str, list[str]] = {}
    changed = False
    for artifact in artifact_index.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        artifact_id = artifact.get("artifact_id")
        unit_id = artifact.get("unit_id")
        artifact_type = artifact.get("artifact_type")
        if not isinstance(artifact_id, str) or not artifact_id:
            continue
        if not isinstance(unit_id, str) or unit_id not in unit_lookup:
            continue
        render_assets = [
            asset for asset in artifact.get("render_assets", []) if isinstance(asset, str) and asset
        ]
        unit_render_assets = _unit_render_assets(unit_lookup[unit_id])
        hires_asset = (
            Path("artifact_renders") / unit_id / f"{artifact_id}--x4.png"
        )
        baseline_asset = Path("artifact_renders") / unit_id / f"{artifact_id}.png"
        existing_focus_assets = [
            asset
            for asset in artifact.get("focus_render_assets", [])
            if isinstance(asset, str) and asset and (source_dir / asset).exists()
        ]
        focus_assets: list[str] = []
        if (source_dir / hires_asset).exists():
            focus_assets.append(str(hires_asset))
        focus_assets.extend(
            asset
            for asset in existing_focus_assets
            if asset not in {str(hires_asset), str(baseline_asset)}
        )
        if artifact_type == "page-image":
            focus_assets.extend(render_assets or unit_render_assets)
        else:
            source_render_path, normalized_bbox = _artifact_render_asset_for_crop(
                source_dir,
                artifact,
            )
            if source_render_path is not None and normalized_bbox is not None:
                if _crop_artifact_render(
                    source_render_path,
                    normalized_bbox=normalized_bbox,
                    output_path=source_dir / baseline_asset,
                ):
                    focus_assets.append(str(baseline_asset))
            elif (source_dir / baseline_asset).exists():
                focus_assets.append(str(baseline_asset))
            focus_assets.extend(render_assets or unit_render_assets)
        normalized_focus = deduplicate_strings(focus_assets)
        if artifact.get("focus_render_assets") != normalized_focus:
            artifact["focus_render_assets"] = normalized_focus
            changed = True
        artifact_focus_map[artifact_id] = normalized_focus
    if changed:
        write_json(artifact_index_path, artifact_index)

    visual_dir = source_dir / "visual_layout"
    if visual_dir.exists():
        for layout_path in sorted(visual_dir.glob("*.json")):
            layout = read_json(layout_path)
            if not isinstance(layout, dict):
                continue
            layout_changed = False
            unit_id = str(layout.get("unit_id") or "")
            regions = layout.get("regions", [])
            if not isinstance(regions, list):
                regions = []
            aggregated_focus_assets: list[str] = []
            for region in regions:
                if not isinstance(region, dict):
                    continue
                artifact_id = region.get("artifact_id")
                if not isinstance(artifact_id, str) or artifact_id not in artifact_focus_map:
                    continue
                focus_assets = artifact_focus_map[artifact_id]
                aggregated_focus_assets.extend(focus_assets)
                if region.get("focus_render_assets") != focus_assets:
                    region["focus_render_assets"] = focus_assets
                    layout_changed = True
            if unit_id:
                unit_render_assets = _unit_render_assets(unit_lookup.get(unit_id, {}))
                aggregated_focus_assets.extend(unit_render_assets)
                normalized = deduplicate_strings(aggregated_focus_assets)
                if layout.get("focus_render_assets") != normalized:
                    layout["focus_render_assets"] = normalized
                    layout_changed = True
            if layout_changed:
                write_json(layout_path, layout)
    return artifact_focus_map


def focus_render_contract_complete(source_dir: Path) -> bool:
    """Return whether the current source already carries governed focus-render assets."""
    evidence_manifest = read_json(source_dir / "evidence_manifest.json")
    document_type = str(evidence_manifest.get("document_type") or "")
    artifact_index = read_json(source_dir / "artifact_index.json")
    if not artifact_index:
        return False
    artifact_lookup: dict[str, dict[str, Any]] = {}
    for artifact in artifact_index.get("artifacts", []):
        if not isinstance(artifact, dict):
            return False
        artifact_type = str(artifact.get("artifact_type") or "")
        artifact_id = str(artifact.get("artifact_id") or "")
        if artifact_id:
            artifact_lookup[artifact_id] = artifact
        if artifact_type not in HYBRID_HARD_ARTIFACT_TYPES and not artifact.get("graph_promoted"):
            continue
        focus_assets = artifact.get("focus_render_assets")
        if not isinstance(focus_assets, list) or not focus_assets:
            return False
        for asset in focus_assets:
            if not isinstance(asset, str) or not asset:
                return False
            if not (source_dir / asset).exists():
                return False
        if document_type == "xlsx" and artifact_type == "picture":
            image_ref = artifact.get("image_ref")
            if not isinstance(image_ref, str) or not image_ref:
                return False
        if (
            document_type in {"docx", "xlsx"}
            and artifact_type == "picture"
            and isinstance(artifact.get("image_ref"), str)
            and artifact.get("image_ref")
        ):
            render_assets = {
                asset
                for asset in artifact.get("render_assets", [])
                if isinstance(asset, str) and asset
            }
            if not any(asset not in render_assets for asset in focus_assets):
                return False
    if document_type in {"docx", "xlsx"}:
        visual_dir = source_dir / "visual_layout"
        for layout_path in sorted(visual_dir.glob("*.json")):
            layout = read_json(layout_path)
            for region in layout.get("regions", []):
                if not isinstance(region, dict):
                    continue
                if region.get("artifact_type") != "picture":
                    continue
                image_ref = region.get("image_ref")
                region_artifact_id = region.get("artifact_id")
                if not isinstance(image_ref, str) or not image_ref:
                    return False
                if (
                    not isinstance(region_artifact_id, str)
                    or region_artifact_id not in artifact_lookup
                ):
                    return False
                artifact = artifact_lookup[region_artifact_id]
                focus_assets = [
                    asset
                    for asset in artifact.get("focus_render_assets", [])
                    if isinstance(asset, str) and asset
                ]
                render_assets = {
                    asset
                    for asset in artifact.get("render_assets", [])
                    if isinstance(asset, str) and asset
                }
                if not focus_assets or not any(
                    asset not in render_assets for asset in focus_assets
                ):
                    return False
    return True


def _update_focus_assets_for_artifact(
    artifact: dict[str, Any],
    *,
    hires_asset: str | None,
) -> list[str]:
    existing = [
        asset
        for asset in artifact.get("focus_render_assets", [])
        if isinstance(asset, str) and asset
    ]
    render_assets = [
        asset for asset in artifact.get("render_assets", []) if isinstance(asset, str) and asset
    ]
    focus_assets = [hires_asset] if isinstance(hires_asset, str) and hires_asset else []
    if existing:
        focus_assets.extend(
            asset for asset in existing if not asset.endswith("--x4.png")
        )
    focus_assets.extend(render_assets)
    normalized = deduplicate_strings(focus_assets)
    artifact["focus_render_assets"] = normalized
    return normalized


def ensure_hires_focus_render(
    paths: WorkspacePaths,
    *,
    target: str,
    source_id: str,
    unit_id: str,
    artifact_id: str,
) -> list[str]:
    """Create an on-demand hi-res focus render for one artifact when possible."""
    source_dir = paths.knowledge_target_dir(target) / "sources" / source_id
    artifact_index_path = source_dir / "artifact_index.json"
    artifact_index = read_json(artifact_index_path)
    evidence_manifest = read_json(source_dir / "evidence_manifest.json")
    source_manifest = read_json(source_dir / "source_manifest.json")
    if not artifact_index or not evidence_manifest or not source_manifest:
        return []
    artifact = next(
        (
            item
            for item in artifact_index.get("artifacts", [])
            if isinstance(item, dict) and item.get("artifact_id") == artifact_id
        ),
        None,
    )
    unit = _unit_lookup(evidence_manifest).get(unit_id, {})
    if not isinstance(artifact, dict) or not unit:
        return []
    existing = [
        asset
        for asset in artifact.get("focus_render_assets", [])
        if isinstance(asset, str) and asset.endswith("--x4.png") and (source_dir / asset).exists()
    ]
    if existing:
        return deduplicate_strings(existing + artifact.get("focus_render_assets", []))
    normalized_bbox = artifact.get("normalized_bbox")
    render_page_span = _normalized_page_span(
        artifact.get("render_page_span")
    ) or _unit_render_page_span(unit)
    if not isinstance(normalized_bbox, dict) or render_page_span is None:
        return []
    if render_page_span["start"] != render_page_span["end"]:
        return []

    current_path = source_manifest.get("current_path")
    if not isinstance(current_path, str) or not current_path:
        return []
    source_path = paths.root / current_path
    if not source_path.exists():
        return []

    with tempfile.TemporaryDirectory() as tempdir_name:
        tempdir = Path(tempdir_name)
        pdf_path = source_path
        document_type = str(source_manifest.get("document_type") or "")
        if document_type != "pdf":
            from .knowledge import convert_office_to_pdf
            from .libreoffice_runtime import validate_soffice_binary

            office_state = validate_soffice_binary(None)
            office_binary = office_state.get("binary")
            if not office_state.get("ready") or not isinstance(office_binary, str):
                return []
            converted_pdf, failures = convert_office_to_pdf(source_path, tempdir, office_binary)
            if converted_pdf is None or failures:
                return []
            pdf_path = converted_pdf

        from .knowledge import import_pdf_modules

        pdfium, _reader = import_pdf_modules()
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            page_index = int(render_page_span["start"]) - 1
            if page_index < 0 or page_index >= len(document):
                return []
            page = document[page_index]
            bitmap = page.render(scale=8)
            image = bitmap.to_pil()
            hires_page_path = tempdir / f"{artifact_id}--page.png"
            image.save(
                hires_page_path,
                format="PNG",
                compress_level=1,
                optimize=False,
            )
        finally:
            document.close()

        hires_asset = Path("artifact_renders") / unit_id / f"{artifact_id}--x4.png"
        if not _crop_artifact_render(
            hires_page_path,
            normalized_bbox=normalized_bbox,
            output_path=source_dir / hires_asset,
        ):
            return []

    normalized_focus = _update_focus_assets_for_artifact(
        artifact,
        hires_asset=str(hires_asset),
    )
    write_json(artifact_index_path, artifact_index)
    visual_layout_path = source_dir / "visual_layout" / f"{unit_id}.json"
    visual_layout = read_json(visual_layout_path)
    if isinstance(visual_layout, dict) and isinstance(visual_layout.get("regions"), list):
        changed = False
        for region in visual_layout["regions"]:
            if not isinstance(region, dict) or region.get("artifact_id") != artifact_id:
                continue
            if region.get("focus_render_assets") != normalized_focus:
                region["focus_render_assets"] = normalized_focus
                changed = True
        if changed:
            aggregated = [
                asset
                for region in visual_layout["regions"]
                if isinstance(region, dict)
                for asset in region.get("focus_render_assets", [])
                if isinstance(asset, str) and asset
            ]
            aggregated.extend(_unit_render_assets(unit))
            visual_layout["focus_render_assets"] = deduplicate_strings(aggregated)
            write_json(visual_layout_path, visual_layout)
    return normalized_focus


def current_hybrid_work(paths: WorkspacePaths, *, target: str = "current") -> dict[str, Any]:
    """Load the current published hybrid-work payload when available."""
    payload = read_json(paths.hybrid_work_path(target))
    if isinstance(payload, dict):
        return payload
    return {"generated_at": None, "target": target, "sources": []}


def narrowed_hybrid_sources(
    paths: WorkspacePaths,
    *,
    target: str,
    source_ids: list[str],
) -> list[dict[str, Any]]:
    """Return the source-scoped slice of the hybrid-work payload for Lane C."""
    wanted = {source_id for source_id in source_ids if isinstance(source_id, str) and source_id}
    payload = current_hybrid_work(paths, target=target)
    return [
        source
        for source in payload.get("sources", [])
        if isinstance(source, dict) and str(source.get("source_id") or "") in wanted
    ]

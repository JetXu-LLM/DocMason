"""Lightweight workspace probes for readiness, source inventory, and source-change preview."""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from difflib import SequenceMatcher
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from .libreoffice_runtime import (
    LIBREOFFICE_PROBE_CONTRACT,
    validate_soffice_binary,
)
from .project import (
    WorkspacePaths,
    relative_paths,
    source_index,
    source_inventory_signature,
    source_type_definition_for_path,
    supported_source_documents,
    write_json,
)

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")


def utc_now() -> str:
    """Return the current UTC timestamp in ISO 8601 form."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def file_sha256(path: Path) -> str:
    """Return the SHA-256 hex digest for one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def module_available(module_name: str) -> bool:
    """Return whether one Python module is discoverable without importing it eagerly."""
    return find_spec(module_name) is not None


def pdf_renderer_snapshot() -> dict[str, Any]:
    """Describe PDF rendering dependency readiness without importing the heavy pipeline."""
    missing: list[str] = []
    for module_name in ("pypdfium2", "pypdf", "PIL"):
        if not module_available(module_name):
            missing.append(module_name)
    if not (module_available("pymupdf") or module_available("fitz")):
        missing.append("PyMuPDF")
    ready = not missing
    detail = "PDF rendering and extraction dependencies are available."
    if missing:
        detail = "Missing Python packages for PDF rendering/extraction: " + ", ".join(missing)
    return {"ready": ready, "detail": detail, "missing": missing}


def office_source_documents(paths: WorkspacePaths) -> list[Path]:
    """Return Office documents that require the LibreOffice rendering path."""
    return [
        path
        for path in supported_source_documents(paths)
        if (definition := source_type_definition_for_path(path)) is not None
        and definition.requires_office_renderer
    ]


def office_renderer_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Describe Office renderer readiness for the current workspace."""
    office_sources = office_source_documents(paths)
    required = bool(office_sources)
    if not required:
        detail = "LibreOffice is optional until PowerPoint, Word, or Excel sources are present."
        return {
            "ready": False,
            "required": False,
            "binary": None,
            "candidate_binary": None,
            "validation_detail": detail,
            "validated": False,
            "version": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "validation_launcher": None,
            "detected_but_unusable": False,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
            "failed_candidates": [],
            "detail": detail,
            "office_sources": [],
        }

    validation = validate_soffice_binary(None)
    soffice_binary = str(validation["binary"]) if validation["ready"] else None
    ready = soffice_binary is not None
    candidate_binary = (
        validation.get("candidate_binary") if not validation["ready"] else soffice_binary
    )
    blocked_by_host_access = bool(validation.get("blocked_by_host_access"))
    host_access_required = bool(validation.get("host_access_required"))
    host_access_guidance = validation.get("host_access_guidance")
    failed_candidates = [
        attempt.get("candidate_binary") or attempt.get("binary")
        for attempt in list(validation.get("candidate_failures") or [])
        if isinstance(attempt.get("candidate_binary") or attempt.get("binary"), str)
        and (attempt.get("candidate_binary") or attempt.get("binary"))
    ]
    detected_but_unusable = bool(
        required and not ready and candidate_binary and not blocked_by_host_access
    )
    if ready:
        version_suffix = (
            f" ({validation['version']})"
            if isinstance(validation.get("version"), str) and validation["version"]
            else ""
        )
        detail = f"LibreOffice rendering is available at {soffice_binary}{version_suffix}."
    else:
        if blocked_by_host_access:
            detail = (
                "LibreOffice `soffice` is required to render PowerPoint, Word, and Excel "
                f"sources. {validation['detail']}"
            )
        elif candidate_binary:
            detail = (
                "LibreOffice `soffice` is required to render PowerPoint, Word, and Excel "
                f"sources, but the detected candidate `{candidate_binary}` is not currently "
                f"usable. {validation['detail']}"
            )
        else:
            detail = (
                "LibreOffice `soffice` is required to render PowerPoint, Word, and Excel "
                "sources, but it is not available."
            )
    return {
        "ready": ready,
        "required": required,
        "binary": soffice_binary,
        "candidate_binary": candidate_binary,
        "validation_detail": validation["detail"],
        "validated": bool(validation["ready"]),
        "version": validation.get("version"),
        "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        "validation_launcher": validation.get("launcher"),
        "detected_but_unusable": detected_but_unusable,
        "blocked_by_host_access": blocked_by_host_access,
        "host_access_required": host_access_required,
        "host_access_guidance": host_access_guidance,
        "failed_candidates": failed_candidates,
        "detail": detail,
        "office_sources": relative_paths(paths, office_sources),
    }


def _normalize_filename_stem(value: str) -> str:
    return " ".join(token.lower() for token in TOKEN_PATTERN.findall(value))


def relocation_candidate_score(
    current_path: str,
    current_size: int,
    candidate_entry: dict[str, Any],
) -> float:
    """Return a deterministic score for relocation-heuristic identity reuse."""
    candidate_path = candidate_entry.get("current_path")
    if not isinstance(candidate_path, str):
        return 0.0
    current_name = _normalize_filename_stem(Path(current_path).stem)
    candidate_name = _normalize_filename_stem(Path(candidate_path).stem)
    if not current_name or not candidate_name:
        return 0.0
    name_score = SequenceMatcher(None, current_name, candidate_name).ratio()
    candidate_size = candidate_entry.get("file_size")
    if isinstance(candidate_size, int) and candidate_size > 0 and current_size > 0:
        size_score = min(candidate_size, current_size) / max(candidate_size, current_size)
    else:
        size_score = 0.9
    if name_score < 0.65 or size_score < 0.9:
        return 0.0
    return (0.7 * name_score) + (0.3 * size_score)


def append_unique_strings(values: Iterable[str]) -> list[str]:
    """Deduplicate strings while preserving order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def build_change_record(
    entry: dict[str, Any],
    *,
    previous_path: str | None,
) -> dict[str, Any]:
    """Build a compact change record for the latest sync state."""
    return {
        "source_id": entry.get("source_id"),
        "current_path": entry.get("current_path"),
        "previous_path": previous_path,
        "document_type": entry.get("document_type"),
        "change_classification": entry.get("change_classification"),
        "change_traits": entry.get("change_traits", []),
        "change_reason": entry.get("change_reason"),
        "identity_basis": entry.get("identity_basis", entry.get("identity_confidence")),
        "matched_source_ids": entry.get("matched_source_ids", []),
    }


def _initial_change_traits(
    existing_entry: dict[str, Any] | None,
    *,
    current_path: str,
    fingerprint: str,
) -> list[str]:
    """Return the initial additive change traits available before staging begins."""
    if existing_entry is None:
        return []
    traits: list[str] = []
    previous_path = existing_entry.get("current_path")
    previous_fingerprint = existing_entry.get("source_fingerprint")
    if isinstance(previous_path, str) and previous_path != current_path:
        traits.append("path_changed")
    if isinstance(previous_fingerprint, str) and previous_fingerprint != fingerprint:
        traits.append("binary_changed")
    return traits


def _initial_change_reason(
    *,
    change_classification: str,
    identity_basis: str,
    change_traits: list[str],
) -> str:
    """Return a concise operator-facing explanation for the detected source change."""
    if change_classification == "unchanged":
        return "Path and binary fingerprint matched the previously indexed source."
    if change_classification == "added":
        return "This source is new to the workspace corpus."
    if change_classification == "deleted":
        return "This previously indexed source is no longer present under `original_doc/`."
    if change_classification == "ambiguous":
        return (
            "Multiple historical sources could plausibly match this path; "
            "operator review is required."
        )
    if "path_changed" in change_traits and "binary_changed" in change_traits:
        return (
            "The source path changed and the binary fingerprint also changed; staged evidence must "
            "confirm whether semantic outputs can be reused."
        )
    if "path_changed" in change_traits:
        return (
            f"The source path changed and identity was preserved via `{identity_basis}` matching."
        )
    if "binary_changed" in change_traits:
        return "The source path is stable but the binary fingerprint changed."
    return f"The source was classified as `{change_classification}` via `{identity_basis}`."


def _compute_source_index(
    paths: WorkspacePaths,
    *,
    persist: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, dict[str, Any]]:
    """Compute the current source identity view, optionally persisting it."""
    now = utc_now()
    existing = source_index(paths)
    existing_sources = existing.get("sources", [])
    if not isinstance(existing_sources, list):
        existing_sources = []

    path_lookup: dict[str, dict[str, Any]] = {}
    all_fingerprint_lookup: dict[str, list[dict[str, Any]]] = {}
    active_existing: list[dict[str, Any]] = []
    inactive_existing: list[dict[str, Any]] = []
    for raw_entry in existing_sources:
        if not isinstance(raw_entry, dict):
            continue
        fingerprint = raw_entry.get("source_fingerprint")
        if isinstance(fingerprint, str):
            all_fingerprint_lookup.setdefault(fingerprint, []).append(raw_entry)
        if raw_entry.get("active", True):
            active_existing.append(raw_entry)
            current_path = raw_entry.get("current_path")
            if isinstance(current_path, str):
                path_lookup[current_path] = raw_entry
        else:
            inactive_existing.append(raw_entry)

    active_entries: list[dict[str, Any]] = []
    ambiguous_match = False
    seen_source_ids: set[str] = set()
    matched_existing_ids: set[str] = set()
    changes: list[dict[str, Any]] = []
    for path in supported_source_documents(paths):
        current_path = str(path.relative_to(paths.root))
        fingerprint = file_sha256(path)
        stat = path.stat()
        file_size = stat.st_size
        definition = source_type_definition_for_path(path)
        if definition is None:
            continue
        document_type = definition.document_type
        existing_entry = path_lookup.get(current_path)
        identity_basis = "path"
        matched_source_ids: list[str] = []
        change_classification = "unchanged"
        previous_path: str | None = None

        if existing_entry is None:
            matches = [
                entry
                for entry in all_fingerprint_lookup.get(fingerprint, [])
                if isinstance(entry.get("source_id"), str)
                and str(entry["source_id"]) not in matched_existing_ids
            ]
            matched_source_ids = [
                str(entry["source_id"])
                for entry in matches
                if isinstance(entry.get("source_id"), str)
            ]
            if len(matches) == 1:
                existing_entry = matches[0]
                identity_basis = "fingerprint"
                change_classification = "moved-or-renamed"
            elif len(matches) > 1:
                ambiguous_match = True
                identity_basis = "ambiguous"
                change_classification = "ambiguous"
            else:
                relocation_candidates = [
                    (candidate, relocation_candidate_score(current_path, file_size, candidate))
                    for candidate in active_existing
                    if candidate.get("active", True)
                    and isinstance(candidate.get("source_id"), str)
                    and str(candidate["source_id"]) not in matched_existing_ids
                    and candidate.get("document_type") == document_type
                ]
                relocation_candidates = [
                    (candidate, score) for candidate, score in relocation_candidates if score > 0
                ]
                relocation_candidates.sort(
                    key=lambda item: (-float(item[1]), str(item[0].get("source_id", "")))
                )
                if len(relocation_candidates) == 1:
                    existing_entry = relocation_candidates[0][0]
                    identity_basis = "relocation-heuristic"
                    change_classification = "moved-or-renamed"
                    matched_source_ids = [str(existing_entry["source_id"])]
                elif len(relocation_candidates) > 1:
                    ambiguous_match = True
                    identity_basis = "ambiguous"
                    change_classification = "ambiguous"
                    matched_source_ids = [
                        str(candidate["source_id"])
                        for candidate, _score in relocation_candidates
                        if isinstance(candidate.get("source_id"), str)
                    ]

        prior_paths: list[str] = []
        first_seen_at = now
        source_id = str(uuid.uuid4())
        change_traits: list[str] = []
        if existing_entry is not None:
            source_id = str(existing_entry.get("source_id", source_id))
            first_seen_at = str(existing_entry.get("first_seen_at", now))
            existing_prior = existing_entry.get("prior_paths", [])
            if isinstance(existing_prior, list):
                prior_paths.extend(str(value) for value in existing_prior)
            old_path = existing_entry.get("current_path")
            if isinstance(old_path, str) and old_path != current_path:
                prior_paths.append(old_path)
                previous_path = old_path
            elif isinstance(old_path, str):
                previous_path = old_path
            if identity_basis == "path":
                change_classification = (
                    "unchanged"
                    if existing_entry.get("source_fingerprint") == fingerprint
                    else "modified"
                )
            change_traits = _initial_change_traits(
                existing_entry,
                current_path=current_path,
                fingerprint=fingerprint,
            )
            matched_existing_ids.add(source_id)
        else:
            identity_basis = "new" if identity_basis != "ambiguous" else identity_basis
            change_classification = (
                "added" if change_classification != "ambiguous" else change_classification
            )

        path_history = (
            append_unique_strings(
                [
                    *(
                        value
                        for value in existing_entry.get("path_history", [])
                        if isinstance(existing_entry, dict)
                        and isinstance(existing_entry.get("path_history"), list)
                        and isinstance(value, str)
                    ),
                    *prior_paths,
                    current_path,
                ]
            )
            if existing_entry is not None
            else [current_path]
        )

        entry = {
            "source_id": source_id,
            "current_path": current_path,
            "prior_paths": append_unique_strings(prior_paths),
            "path_history": path_history,
            "source_fingerprint": fingerprint,
            "file_size": file_size,
            "first_seen_at": first_seen_at,
            "last_seen_at": now,
            "identity_confidence": identity_basis,
            "identity_basis": identity_basis,
            "change_classification": change_classification,
            "change_traits": change_traits,
            "change_reason": _initial_change_reason(
                change_classification=change_classification,
                identity_basis=identity_basis,
                change_traits=change_traits,
            ),
            "ambiguous_match": identity_basis == "ambiguous",
            "matched_source_ids": matched_source_ids,
            "document_type": document_type,
            "source_extension": definition.extension,
            "support_tier": definition.support_tier,
            "archived_at": None,
            "deleted_at": None,
            "active": True,
        }
        active_entries.append(entry)
        seen_source_ids.add(source_id)
        changes.append(build_change_record(entry, previous_path=previous_path))

    archived_entries: list[dict[str, Any]] = []
    for raw_entry in active_existing:
        if not isinstance(raw_entry, dict):
            continue
        source_id_value = raw_entry.get("source_id")
        if not isinstance(source_id_value, str) or source_id_value in seen_source_ids:
            continue
        archived_entry = dict(raw_entry)
        archived_entry["active"] = False
        archived_entry["change_classification"] = "deleted"
        archived_entry["change_traits"] = []
        archived_entry["change_reason"] = _initial_change_reason(
            change_classification="deleted",
            identity_basis=str(archived_entry.get("identity_basis") or "path"),
            change_traits=[],
        )
        archived_entry["archived_at"] = archived_entry.get("archived_at") or now
        archived_entry["deleted_at"] = archived_entry.get("deleted_at") or now
        archived_entries.append(archived_entry)
        changes.append(
            build_change_record(
                archived_entry,
                previous_path=raw_entry.get("current_path"),
            )
        )

    archived_entries.extend(inactive_existing)
    classification_counts = Counter(
        str(change["change_classification"])
        for change in changes
        if change.get("change_classification")
    )
    change_set = {
        "generated_at": now,
        "source_signature": source_inventory_signature(paths),
        "stats": {
            "unchanged": classification_counts.get("unchanged", 0),
            "added": classification_counts.get("added", 0),
            "modified": classification_counts.get("modified", 0),
            "moved_or_renamed": classification_counts.get("moved-or-renamed", 0),
            "deleted": classification_counts.get("deleted", 0),
            "ambiguous": classification_counts.get("ambiguous", 0),
        },
        "changes": changes,
    }

    payload = {
        "generated_at": now,
        "sources": sorted(active_entries, key=lambda item: str(item["current_path"]))
        + sorted(
            archived_entries,
            key=lambda item: (str(item.get("current_path", "")), str(item.get("source_id", ""))),
        ),
    }
    if persist:
        write_json(paths.source_index_path, payload)
    return payload, active_entries, ambiguous_match, change_set


def update_source_index(
    paths: WorkspacePaths,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, dict[str, Any]]:
    """Update and persist the stable source identity index plus the current change set."""
    return _compute_source_index(paths, persist=True)


def preview_source_changes(
    paths: WorkspacePaths,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, dict[str, Any]]:
    """Compute the current change set without mutating persisted source-index state."""
    return _compute_source_index(paths, persist=False)

"""Additive work-state helpers for canonical ask turns.

The agent still owns professional judgment and content strategy.  This module only
normalizes explicit intent, preserves linked-turn state, and records evidence and
decision legality for the governed runtime.
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project import WorkspacePaths

DECISION_GATE_CLASSES = frozenset(
    {"judgment-authority-gap", "high-cost-expression-choice"}
)
CONTINUATION_TYPES = frozenset(
    {"constraint-update", "evidence-refresh", "mixed", "decision-resolution"}
)


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _mapping(value: Any) -> dict[str, Any]:
    return deepcopy(value) if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _merge_mapping(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_mapping(dict(merged[key]), value)
        else:
            merged[key] = deepcopy(value)
    return merged


def normalize_work_brief(
    *,
    question: str,
    semantic_analysis: dict[str, Any] | None,
    previous_brief: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the agent-authored Work Brief for one turn.

    Content strategy remains agent-owned.  The deterministic runtime validates and
    merges the explicit projection; it must not replace professional framing with a
    keyword router.
    """
    _ = question  # The prompt is evidence for the agent, not a regex-routing surface.
    brief = _mapping(previous_brief)
    semantic = semantic_analysis if isinstance(semantic_analysis, dict) else {}
    brief = _merge_mapping(brief, _mapping(semantic.get("work_brief")))
    return brief


def resolve_continuation_anchor(
    prior_turns: list[dict[str, Any]],
    semantic_analysis: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Resolve an explicit continuation anchor without guessing from adjacency.

    A later message in the same host thread is not automatically a revision.  Normal
    revisions name ``revision_of``; decision replies name the exact open gate.  This
    keeps evidence reuse and human authority explicit and auditable.
    """
    semantic = semantic_analysis if isinstance(semantic_analysis, dict) else {}
    revision_of = str(semantic.get("revision_of") or "").strip()
    resolves_gate_id = str(semantic.get("resolves_gate_id") or "").strip()
    continuation_type = str(semantic.get("continuation_type") or "").strip()

    if continuation_type and continuation_type not in CONTINUATION_TYPES:
        return None, {
            "code": "invalid-continuation-type",
            "detail": f"Unsupported continuation_type `{continuation_type}`.",
        }
    if continuation_type == "decision-resolution" and not resolves_gate_id:
        return None, {
            "code": "decision-resolution-missing-gate-id",
            "detail": "Decision resolution requires the exact resolves_gate_id.",
        }
    if continuation_type and continuation_type != "decision-resolution" and not revision_of:
        return None, {
            "code": "continuation-missing-revision-anchor",
            "detail": "A revision continuation requires the exact revision_of turn id.",
        }
    if semantic.get("decision_resolution") is not None and not resolves_gate_id:
        return None, {
            "code": "decision-resolution-missing-gate-id",
            "detail": "decision_resolution cannot be applied without resolves_gate_id.",
        }

    revision_turn = next(
        (turn for turn in prior_turns if str(turn.get("turn_id") or "") == revision_of),
        None,
    ) if revision_of else None
    if revision_of and revision_turn is None:
        return None, {
            "code": "unknown-revision-anchor",
            "detail": f"revision_of `{revision_of}` is not a turn in this conversation.",
        }

    gate_turn: dict[str, Any] | None = None
    if resolves_gate_id:
        matching_gate_turns = [
            turn
            for turn in prior_turns
            if isinstance(turn.get("decision_frontier"), dict)
            and str(turn["decision_frontier"].get("gate_id") or "") == resolves_gate_id
        ]
        if not matching_gate_turns:
            return None, {
                "code": "unknown-decision-gate",
                "detail": (
                    f"resolves_gate_id `{resolves_gate_id}` is not known in this "
                    "conversation."
                ),
            }
        gate_turn = matching_gate_turns[-1]
        gate = normalize_decision_gate(gate_turn.get("decision_frontier"))
        if gate is None or gate.get("status") != "open":
            return None, {
                "code": "decision-gate-not-open",
                "detail": f"Decision gate `{resolves_gate_id}` is not open.",
            }
        resolution = semantic.get("decision_resolution")
        if resolution is None or resolution == "" or resolution == {}:
            return None, {
                "code": "decision-resolution-missing-value",
                "detail": "An open decision gate requires an explicit decision_resolution.",
            }
        if normalize_decision_resolution(gate, resolution) is None:
            return None, {
                "code": "invalid-decision-resolution",
                "detail": (
                    "decision_resolution must select an option from the referenced gate "
                    "or provide an explicit free-form decision."
                ),
            }

    if revision_turn is not None and gate_turn is not None:
        if revision_turn.get("turn_id") != gate_turn.get("turn_id"):
            return None, {
                "code": "continuation-anchor-conflict",
                "detail": "revision_of and resolves_gate_id point to different turns.",
            }
    return gate_turn or revision_turn, None


def normalize_decision_gate(value: Any) -> dict[str, Any] | None:
    """Validate a material decision gate without inventing user authority."""
    gate = _mapping(value)
    gate_class = str(gate.get("class") or "")
    question = str(gate.get("question") or "").strip()
    why_user_is_required = str(gate.get("why_user_is_required") or "").strip()
    evidence_boundary = str(gate.get("evidence_boundary") or "").strip()
    affected_outputs = _string_list(gate.get("affected_outputs"))
    if (
        gate_class not in DECISION_GATE_CLASSES
        or not question
        or not why_user_is_required
        or not evidence_boundary
        or not affected_outputs
    ):
        return None
    raw_options = [dict(item) for item in gate.get("options", []) if isinstance(item, dict)]
    if not 2 <= len(raw_options) <= 3:
        return None
    options: list[dict[str, Any]] = []
    for option in raw_options:
        if not all(
            isinstance(option.get(field), str) and str(option.get(field)).strip()
            for field in ("id", "label")
        ):
            return None
        impact = option.get("impact") or option.get("description")
        if not isinstance(impact, str) or not impact.strip():
            return None
        options.append(
            {
                **option,
                "id": str(option["id"]).strip(),
                "label": str(option["label"]).strip(),
                "impact": impact.strip(),
                "recommended": option.get("recommended") is True,
            }
        )
    option_ids = [str(option["id"]).strip() for option in options]
    if len(set(option_ids)) != len(option_ids):
        return None
    if sum(option.get("recommended") is True for option in options) != 1:
        return None
    return {
        **gate,
        "gate_id": str(gate.get("gate_id") or uuid.uuid4()),
        "status": (
            "resolved" if str(gate.get("status") or "") == "resolved" else "open"
        ),
        "class": gate_class,
        "question": question,
        "why_user_is_required": why_user_is_required,
        "evidence_boundary": evidence_boundary,
        "options": options,
        "affected_outputs": affected_outputs,
        "invalidates": _string_list(gate.get("invalidates")),
        "opened_at": str(gate.get("opened_at") or _utc_now()),
        "resolved_by_turn": gate.get("resolved_by_turn"),
    }


def normalize_new_decision_gate(value: Any) -> dict[str, Any] | None:
    """Normalize a newly requested gate while reserving identity for the runtime."""
    gate = _mapping(value)
    for field_name in (
        "gate_id",
        "status",
        "opened_at",
        "resolved_by_turn",
        "resolved_at",
    ):
        gate.pop(field_name, None)
    return normalize_decision_gate(gate)


def normalize_decision_resolution(
    gate: dict[str, Any],
    value: Any,
) -> dict[str, str] | None:
    """Return one explicit legal resolution for a persisted material gate.

    A structured selection must name an option that actually belongs to the gate.
    Free-form authority remains legal, but it must be an explicit non-empty decision
    rather than an arbitrary mapping that happens to be truthy.
    """
    if isinstance(value, str) and value.strip():
        return {"free_form": value.strip()}
    if not isinstance(value, dict):
        return None
    option_id = str(value.get("option_id") or "").strip()
    free_form = value.get("free_form")
    if option_id and isinstance(free_form, str) and free_form.strip():
        return None
    if option_id:
        legal_option_ids = {
            str(option.get("id") or "").strip()
            for option in gate.get("options", [])
            if isinstance(option, dict)
        }
        if option_id not in legal_option_ids:
            return None
        return {"option_id": option_id}
    if isinstance(free_form, str) and free_form.strip():
        return {"free_form": free_form.strip()}
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_evidence_item(
    paths: WorkspacePaths,
    value: dict[str, Any],
) -> dict[str, Any] | None:
    locator = value.get("locator") or value.get("path") or value.get("host_locator")
    if not isinstance(locator, str) or not locator:
        return None
    path = Path(locator).expanduser()
    if not path.is_absolute() and not locator.startswith("host:"):
        path = paths.root / path
    source_role = str(value.get("source_role") or "primary")
    authority = str(value.get("authority") or "user-provided-current")
    if source_role in {"reference", "exemplar"} and "authority" not in value:
        authority = "reference-only"
    available = path.is_file() if not locator.startswith("host:") else bool(value.get("sha256"))
    explicit_source_type = str(value.get("source_type") or "").strip()
    if explicit_source_type:
        source_type = explicit_source_type
    elif locator.startswith("host:"):
        source_type = "host-attachment"
    else:
        try:
            path.resolve().relative_to(paths.source_dir.resolve())
        except ValueError:
            source_type = "turn-local-file"
        else:
            source_type = "workspace-corpus"
    item = {
        "locator": str(path) if not locator.startswith("host:") else locator,
        "source_type": source_type,
        "sha256": str(value.get("sha256") or ""),
        "size_bytes": int(value.get("size_bytes") or 0),
        "mtime": str(value.get("mtime") or ""),
        "media_type": str(value.get("media_type") or value.get("mime_type") or ""),
        "source_role": source_role,
        "authority": authority,
        "freshness": str(value.get("freshness") or "current-for-turn"),
        "supersedes": _string_list(value.get("supersedes")),
        "supersession_basis": str(value.get("supersession_basis") or ""),
        "availability": "available" if available else "unavailable",
    }
    if available and not locator.startswith("host:"):
        stat = path.stat()
        item.update(
            {
                "sha256": _sha256(path),
                "size_bytes": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                "media_type": item["media_type"]
                or mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            }
        )
    return item


def build_evidence_packet(
    paths: WorkspacePaths,
    *,
    semantic_analysis: dict[str, Any] | None,
    previous_packet: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build a private, hash-bound packet for named local files or host attachments."""
    semantic = semantic_analysis if isinstance(semantic_analysis, dict) else {}
    packet_input = _mapping(semantic.get("evidence_packet"))
    raw_items: list[Any] = []
    for candidate in (
        packet_input.get("items"),
        semantic.get("evidence_items"),
        semantic.get("attachments"),
    ):
        if isinstance(candidate, list):
            raw_items.extend(candidate)

    inherited_items = [
        dict(item)
        for item in _mapping(previous_packet).get("items", [])
        if isinstance(item, dict)
    ]
    items_by_locator: dict[str, dict[str, Any]] = {
        str(item.get("locator")): item
        for item in inherited_items
        if isinstance(item.get("locator"), str)
    }
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item = _normalized_evidence_item(paths, raw_item)
        if item is not None:
            items_by_locator[item["locator"]] = item

    # Revalidate inherited local evidence before legal reuse.
    for locator, inherited in list(items_by_locator.items()):
        if locator.startswith("host:"):
            continue
        refreshed = _normalized_evidence_item(paths, inherited)
        if refreshed is None:
            continue
        if inherited.get("sha256") and refreshed.get("sha256") != inherited.get("sha256"):
            refreshed["availability"] = "changed"
        items_by_locator[locator] = refreshed

    previous_packet_id = str(_mapping(previous_packet).get("packet_id") or "")
    return {
        "packet_id": str(packet_input.get("packet_id") or previous_packet_id or uuid.uuid4()),
        "items": list(items_by_locator.values()),
    }


def _normalize_scopes(value: Any) -> list[dict[str, Any]]:
    scopes: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return scopes
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("scope_id"), str):
            continue
        scopes.append(
            {
                **item,
                "status": str(item.get("status") or "accepted"),
                "accepted_at": str(item.get("accepted_at") or _utc_now()),
                "dependencies": _string_list(item.get("dependencies")),
            }
        )
    return scopes


def _resolved_artifact_digest(
    paths: WorkspacePaths,
    item: dict[str, Any],
) -> tuple[str | None, str | None]:
    locator = item.get("artifact_locator")
    if isinstance(locator, str) and locator:
        artifact_path = Path(locator).expanduser()
        if not artifact_path.is_absolute():
            artifact_path = paths.root / artifact_path
        if not artifact_path.is_file():
            return None, "accepted-scope-artifact-missing"
        return _sha256(artifact_path), None
    digest = item.get("artifact_digest")
    if isinstance(digest, str) and digest.strip():
        return digest.strip(), None
    return None, "accepted-scope-digest-missing"


def merge_accepted_scopes(
    paths: WorkspacePaths,
    previous: Any,
    updates: Any,
    *,
    invalidates: list[str],
    turn_id: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Preserve explicit acceptance and reject silent replacement of locked scopes."""
    scopes_by_id = {
        str(item["scope_id"]): item for item in _normalize_scopes(previous)
    }
    inherited_scopes_by_id = deepcopy(scopes_by_id)
    issues: list[str] = []
    if updates is not None and not isinstance(updates, list):
        issues.append("accepted-scope-update-invalid")
    raw_updates = updates if isinstance(updates, list) else []
    for raw_item in raw_updates:
        if not isinstance(raw_item, dict):
            issues.append("accepted-scope-update-invalid")
            continue
        scope_id = str(raw_item.get("scope_id") or "").strip()
        if not scope_id:
            issues.append("accepted-scope-update-invalid")
            continue
        event = str(raw_item.get("event") or "").strip()
        if event == "reopen":
            existing = scopes_by_id.get(scope_id)
            if existing is None:
                issues.append("accepted-scope-reopen-unknown")
                continue
            if raw_item.get("reopened_by_user") is not True:
                issues.append("accepted-scope-reopen-missing-user-authority")
                continue
            scopes_by_id[scope_id] = {
                **existing,
                "status": "reopened",
                "reopened_by_turn": turn_id,
                "reopened_by_user": True,
                "reopened_at": _utc_now(),
                "reopen_reason": str(raw_item.get("reason") or ""),
            }
            continue
        if event not in {"", "accept"}:
            issues.append("accepted-scope-event-invalid")
            continue
        if raw_item.get("accepted_by_user") is not True:
            issues.append("accepted-scope-missing-user-acceptance")
            continue
        digest, digest_issue = _resolved_artifact_digest(paths, raw_item)
        if digest_issue is not None:
            issues.append(digest_issue)
            continue
        existing = scopes_by_id.get(scope_id)
        if (
            isinstance(existing, dict)
            and existing.get("status") == "accepted"
            and existing.get("artifact_digest") != digest
        ):
            issues.append("accepted-scope-replacement-requires-reopen")
            continue
        scopes_by_id[scope_id] = {
            **raw_item,
            "scope_id": scope_id,
            "status": "accepted",
            "artifact_digest": digest,
            "accepted_by_turn": str(raw_item.get("accepted_by_turn") or turn_id),
            "accepted_by_user": True,
            "acceptance_basis": str(
                raw_item.get("acceptance_basis") or "explicit-user-confirmation"
            ),
            "accepted_at": str(raw_item.get("accepted_at") or _utc_now()),
            "dependencies": _string_list(raw_item.get("dependencies")),
        }
    if issues:
        # Acceptance mutations are one legal-state transition.  Do not persist a
        # valid subset from a request whose sibling update is unauthorized or
        # malformed; the user/host can retry the complete update explicitly.
        scopes_by_id = inherited_scopes_by_id
    invalidated = set(invalidates)
    for scope in scopes_by_id.values():
        if scope.get("status") != "accepted":
            continue
        dependencies = set(_string_list(scope.get("dependencies")))
        if dependencies & invalidated:
            scope["status"] = "at-risk"
            scope["reason"] = "A recorded dependency changed; explicit reopening is required."
            continue
        locator = scope.get("artifact_locator")
        if isinstance(locator, str) and locator:
            current_digest, digest_issue = _resolved_artifact_digest(paths, scope)
            if digest_issue is not None or current_digest != scope.get("artifact_digest"):
                scope["status"] = "at-risk"
                scope["reason"] = "The accepted artifact changed or is unavailable."
    return list(scopes_by_id.values()), list(dict.fromkeys(issues))


def accepted_scope_integrity_issues(
    paths: WorkspacePaths,
    scopes: Any,
) -> list[str]:
    """Return stable issue codes for acceptance locks that cannot be honored."""
    issues: list[str] = []
    for scope in _normalize_scopes(scopes):
        if scope.get("status") == "at-risk":
            issues.append("accepted-scope-at-risk")
            continue
        if scope.get("status") != "accepted":
            continue
        if scope.get("accepted_by_user") is not True:
            issues.append("accepted-scope-missing-user-acceptance")
        digest, digest_issue = _resolved_artifact_digest(paths, scope)
        if digest_issue is not None:
            issues.append(digest_issue)
        elif digest != scope.get("artifact_digest"):
            issues.append("accepted-scope-artifact-changed")
    return list(dict.fromkeys(issues))


def build_turn_work_state(
    paths: WorkspacePaths,
    *,
    question: str,
    semantic_analysis: dict[str, Any] | None,
    previous_turn: dict[str, Any] | None,
    turn_id: str,
) -> dict[str, Any]:
    """Build additive linked-turn state without performing answer-critical work."""
    semantic = semantic_analysis if isinstance(semantic_analysis, dict) else {}
    previous = previous_turn if isinstance(previous_turn, dict) else {}
    explicit_continuation = str(semantic.get("continuation_type") or "")
    resolves_gate_id = str(semantic.get("resolves_gate_id") or "") or None
    explicit_revision_of = str(semantic.get("revision_of") or "") or None
    previous_gate = normalize_decision_gate(previous.get("decision_frontier"))
    normalized_resolution: dict[str, str] | None = None
    work_state_issue_code: str | None = None
    work_state_issue_detail: str | None = None
    if explicit_continuation and explicit_continuation not in CONTINUATION_TYPES:
        work_state_issue_code = "invalid-continuation-type"
        work_state_issue_detail = f"Unsupported continuation_type `{explicit_continuation}`."
    elif resolves_gate_id is not None:
        resolution = semantic.get("decision_resolution")
        if previous_gate is None or previous_gate.get("status") != "open":
            work_state_issue_code = "decision-gate-not-open"
            work_state_issue_detail = "The referenced decision gate is not open."
        elif str(previous_gate.get("gate_id") or "") != resolves_gate_id:
            work_state_issue_code = "decision-gate-id-mismatch"
            work_state_issue_detail = "resolves_gate_id does not match the linked open gate."
        elif resolution is None or resolution == "" or resolution == {}:
            work_state_issue_code = "decision-resolution-missing-value"
            work_state_issue_detail = "An explicit decision_resolution is required."
        else:
            normalized_resolution = normalize_decision_resolution(previous_gate, resolution)
            if normalized_resolution is None:
                work_state_issue_code = "invalid-decision-resolution"
                work_state_issue_detail = (
                    "decision_resolution must select an option from the referenced gate "
                    "or provide an explicit free-form decision."
                )
    elif explicit_continuation and not explicit_revision_of:
        work_state_issue_code = "continuation-missing-revision-anchor"
        work_state_issue_detail = "A revision continuation requires revision_of."
    elif explicit_revision_of and str(previous.get("turn_id") or "") != explicit_revision_of:
        work_state_issue_code = "revision-anchor-mismatch"
        work_state_issue_detail = "revision_of does not match the linked prior turn."
    linked_previous = bool(
        previous
        and (
            resolves_gate_id is not None
            or (
                explicit_revision_of is not None
                and str(previous.get("turn_id") or "") == explicit_revision_of
            )
        )
    )
    inherited_previous = previous if linked_previous else {}
    if resolves_gate_id is not None:
        continuation_type = "decision-resolution"
    elif explicit_continuation in CONTINUATION_TYPES:
        continuation_type = explicit_continuation
    elif explicit_revision_of and previous:
        has_evidence_delta = bool(
            semantic.get("evidence_delta")
            or semantic.get("evidence_items")
            or semantic.get("attachments")
        )
        has_constraint_delta = bool(
            semantic.get("revision_scope")
            or semantic.get("work_brief")
            or semantic.get("affected_outputs")
        )
        continuation_type = (
            "mixed"
            if has_evidence_delta and has_constraint_delta
            else "evidence-refresh"
            if has_evidence_delta
            else "constraint-update"
        )
    else:
        continuation_type = None

    affected_outputs = _string_list(semantic.get("affected_outputs"))
    invalidates = _string_list(semantic.get("invalidates"))
    decision_ledger = [
        dict(item)
        for item in inherited_previous.get("decision_ledger", [])
        if isinstance(item, dict)
    ]
    decision_frontier: dict[str, Any] | None = None
    if (
        resolves_gate_id is not None
        and previous_gate is not None
        and normalized_resolution is not None
        and work_state_issue_code is None
    ):
        decision_frontier = {
            **previous_gate,
            "status": "resolved",
            "resolved_by_turn": turn_id,
        }
        decision_ledger.append(
            {
                "gate_id": resolves_gate_id,
                "resolved_by_turn": turn_id,
                "resolution": normalized_resolution,
                "resolved_at": _utc_now(),
            }
        )
    elif "decision_frontier" in semantic:
        decision_frontier = normalize_new_decision_gate(
            semantic.get("decision_frontier")
        )
        if decision_frontier is None and work_state_issue_code is None:
            work_state_issue_code = "invalid-decision-gate-contract"
            work_state_issue_detail = (
                "A material decision gate requires an evidence boundary, two or three "
                "real options with impacts, exactly one recommendation, and affected "
                "outputs."
            )
    elif continuation_type and linked_previous and previous_gate is not None:
        decision_frontier = previous_gate

    packet = build_evidence_packet(
        paths,
        semantic_analysis=semantic,
        previous_packet=_mapping(inherited_previous.get("evidence_packet")),
    )
    inherited_evidence_drift = [
        str(item.get("locator"))
        for item in packet.get("items", [])
        if isinstance(item, dict)
        and item.get("availability") in {"changed", "unavailable"}
    ]
    if linked_previous and inherited_evidence_drift and continuation_type in {
        "constraint-update",
        "decision-resolution",
    }:
        continuation_type = "mixed"
    accepted_scopes, accepted_scope_issues = merge_accepted_scopes(
        paths,
        inherited_previous.get("accepted_scopes"),
        semantic.get("accepted_scopes"),
        invalidates=[*invalidates, *_string_list(semantic.get("evidence_delta"))],
        turn_id=turn_id,
    )
    if work_state_issue_code is None and accepted_scope_issues:
        work_state_issue_code = accepted_scope_issues[0]
        work_state_issue_detail = (
            "Accepted-scope updates require explicit user acceptance, a bound artifact "
            "digest, and an explicit reopen before replacement."
        )
    if work_state_issue_code is not None and normalized_resolution is not None:
        # A compound continuation is transactional.  Do not let a blocked child turn
        # retain a resolved projection of the same gate: that would shadow the still-open
        # owner turn and make a corrected user response impossible to anchor legally.
        decision_frontier = None
        decision_ledger = [
            dict(item)
            for item in inherited_previous.get("decision_ledger", [])
            if isinstance(item, dict)
        ]
    revision_of = explicit_revision_of or (
        str(previous.get("turn_id") or "") if resolves_gate_id is not None and previous else None
    )
    reuse_evidence = continuation_type in {"constraint-update", "decision-resolution"}
    effective_evidence_delta = deepcopy(semantic.get("evidence_delta") or [])
    if inherited_evidence_drift:
        effective_evidence_delta = [
            *effective_evidence_delta,
            *(
                {
                    "locator": locator,
                    "reason": "inherited-evidence-changed-or-unavailable",
                }
                for locator in inherited_evidence_drift
            ),
        ]
    return {
        "work_brief": normalize_work_brief(
            question=question,
            semantic_analysis=semantic,
            previous_brief=_mapping(inherited_previous.get("work_brief")),
        ),
        "revision_of": revision_of,
        "continuation_type": continuation_type,
        "revision_scope": _mapping(semantic.get("revision_scope")),
        "evidence_delta": effective_evidence_delta,
        "decision_frontier": decision_frontier,
        "decision_ledger": decision_ledger,
        "resolves_gate_id": resolves_gate_id,
        "accepted_scopes": accepted_scopes,
        "affected_outputs": affected_outputs,
        "evidence_packet": packet,
        "reused_previous_evidence": bool(reuse_evidence and linked_previous),
        "new_retrieval_executed": False,
        "new_trace_executed": False,
        "scope_freshness": (
            "no-relevant-delta"
            if reuse_evidence
            else "relevant-delta"
            if inherited_evidence_drift
            else None
        ),
        "accepted_scope_issues": accepted_scope_issues,
        "work_state_issue_code": work_state_issue_code,
        "work_state_issue_detail": work_state_issue_detail,
    }


def evidence_packet_support_sources(packet: Any) -> list[dict[str, Any]]:
    """Project available packet items into the private support-manifest source shape."""
    return [
        {
            "locator": item.get("locator"),
            "source_type": item.get("source_type"),
            "sha256": item.get("sha256"),
            "size_bytes": item.get("size_bytes"),
            "media_type": item.get("media_type"),
            "source_role": item.get("source_role"),
            "authority": item.get("authority"),
            "freshness": item.get("freshness"),
            "supersedes": item.get("supersedes", []),
            "supersession_basis": item.get("supersession_basis", ""),
        }
        for item in _mapping(packet).get("items", [])
        if isinstance(item, dict)
        and item.get("availability") == "available"
        and item.get("authority") not in {"untrusted", "reference-only"}
    ]

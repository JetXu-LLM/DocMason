"""Structured support-contract helpers for canonical ask execution."""

from __future__ import annotations

from typing import Any

TARGETED_SCOPE_MODES = frozenset({"source-scoped-soft", "source-scoped-hard", "compare"})
BOUNDARY_SUPPORT_BASES = frozenset({"governed-boundary"})
REPAIRABLE_GAP_TYPES = (
    "source-scoped-target-support",
    "compare-coverage",
    "preferred-channel-grounding",
)


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _deduplicate_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in values if isinstance(item, str) and item.strip())
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _int_or_default(value: Any, default: int) -> int:
    return value if isinstance(value, int) else default


def build_support_contract(
    *,
    question_domain: str,
    support_strategy: str,
    evidence_requirements: dict[str, Any] | None,
    inspection_scope: str | None,
    preferred_channels: list[str] | None,
    reference_resolution: dict[str, Any] | None,
    reference_resolution_summary: str | None,
    source_scope_policy: dict[str, Any] | None,
    prefer_published_artifacts: bool,
) -> dict[str, Any]:
    """Return the persisted support contract for one canonical ask turn."""
    requirements = dict(evidence_requirements) if isinstance(evidence_requirements, dict) else {}
    reference = _mapping(reference_resolution)
    policy = _mapping(source_scope_policy)
    scope_mode = str(policy.get("scope_mode") or "global")
    compare_expected_source_count = _int_or_default(
        policy.get("compare_expected_source_count"),
        2 if scope_mode == "compare" else 0,
    )
    return {
        "schema_version": 1,
        "question_domain": question_domain,
        "support_strategy": support_strategy,
        "inspection_scope": _nonempty_string(inspection_scope) or "unit",
        "preferred_channels": _deduplicate_strings(
            preferred_channels
            if isinstance(preferred_channels, list)
            else requirements.get("preferred_channels", [])
        ),
        "prefer_published_artifacts": bool(prefer_published_artifacts),
        "reference_honesty": {
            "required": bool(reference),
            "resolution_summary": _nonempty_string(reference_resolution_summary),
            "status": _nonempty_string(reference.get("status")) or "none",
            "source_match_status": _nonempty_string(reference.get("source_match_status")) or "none",
            "unit_match_status": _nonempty_string(reference.get("unit_match_status")) or "none",
            "target_source_ref": _nonempty_string(reference.get("target_source_ref")),
            "unresolved_reason": _nonempty_string(reference.get("unresolved_reason")),
        },
        "source_scope": {
            "scope_mode": scope_mode,
            "target_source_id": _nonempty_string(policy.get("target_source_id")),
            "target_source_ref": _nonempty_string(policy.get("target_source_ref")),
            "require_target_source_in_final_support": bool(
                policy.get("require_target_source_in_final_support")
            ),
            "compare_target_source_ids": _deduplicate_strings(
                policy.get("compare_target_source_ids", [])
            ),
            "compare_target_source_refs": _deduplicate_strings(
                policy.get("compare_target_source_refs", [])
            ),
            "compare_expected_source_count": compare_expected_source_count,
            "compare_resolution_status": _nonempty_string(policy.get("compare_resolution_status")),
        },
        "repair_policy": {
            "max_repair_attempts": 1,
            "repairable_gap_types": list(REPAIRABLE_GAP_TYPES),
        },
    }


WORKFLOW_OUTCOME_FIELDS = frozenset(
    {
        "support_basis",
        "support_manifest_path",
        "support_manifest_sources",
        "support_manifest_key_assertions",
        "support_manifest_notes",
        "session_ids",
        "trace_ids",
        "bundle_paths",
        "source_escalation_used",
        "status",
    }
)


def normalize_workflow_outcome(value: Any) -> dict[str, Any] | None:
    """Validate and normalize one optional workflow-owned finalize handoff."""
    if not isinstance(value, dict):
        return None
    normalized: dict[str, Any] = {}
    for field_name in WORKFLOW_OUTCOME_FIELDS:
        if field_name not in value:
            continue
        raw_value = value[field_name]
        if field_name in {
            "support_basis",
            "support_manifest_path",
            "support_manifest_notes",
            "status",
        }:
            string_value = _nonempty_string(raw_value)
            if string_value is not None:
                normalized[field_name] = string_value
        elif field_name in {"session_ids", "trace_ids", "bundle_paths"}:
            normalized[field_name] = _deduplicate_strings(raw_value)
        elif field_name == "source_escalation_used":
            bool_value = _bool_or_none(raw_value)
            if bool_value is not None:
                normalized[field_name] = bool_value
        elif field_name in {"support_manifest_sources", "support_manifest_key_assertions"}:
            if isinstance(raw_value, list):
                normalized[field_name] = list(raw_value)
    return normalized or None


def build_support_fulfillment(
    *,
    support_contract: dict[str, Any] | None,
    trace_payload: dict[str, Any] | None,
    answer_state: str | None,
    support_basis: str | None,
    repair_attempt_count: int = 0,
) -> dict[str, Any]:
    """Return the support-fulfillment diff for one final trace attempt."""
    contract = dict(support_contract) if isinstance(support_contract, dict) else {}
    trace = dict(trace_payload) if isinstance(trace_payload, dict) else {}
    reference_contract = _mapping(contract.get("reference_honesty"))
    scope_contract = _mapping(contract.get("source_scope"))
    repair_policy = _mapping(contract.get("repair_policy"))
    max_repair_attempts = _int_or_default(repair_policy.get("max_repair_attempts"), 1)

    trace_reference_summary = _nonempty_string(trace.get("reference_resolution_summary"))
    trace_scope_summary = _mapping(trace.get("canonical_support_summary"))
    supporting_source_ids = _deduplicate_strings(
        trace_scope_summary.get("supporting_source_ids", [])
    )
    issue_codes = _deduplicate_strings(trace.get("issue_codes", []))
    required_channels = _deduplicate_strings(contract.get("preferred_channels", []))
    matched_channels = _deduplicate_strings(trace.get("matched_published_channels", []))
    used_channels = _deduplicate_strings(trace.get("used_published_channels", []))
    source_scope_satisfied = _bool_or_none(trace.get("source_scope_satisfied"))
    published_artifacts_sufficient = _bool_or_none(trace.get("published_artifacts_sufficient"))
    source_escalation_required = _bool_or_none(trace.get("source_escalation_required"))
    render_inspection_required = _bool_or_none(trace.get("render_inspection_required"))

    reference_required = bool(reference_contract.get("required"))
    reference_satisfied = (
        not reference_required
        or trace_reference_summary
        == _nonempty_string(reference_contract.get("resolution_summary"))
    )

    scope_mode = _nonempty_string(scope_contract.get("scope_mode")) or "global"
    target_source_required = scope_mode in {"source-scoped-soft", "source-scoped-hard"} and bool(
        scope_contract.get("require_target_source_in_final_support")
    )
    compare_required = scope_mode == "compare"
    compare_target_source_ids = _deduplicate_strings(
        scope_contract.get("compare_target_source_ids", [])
    )
    compare_expected_source_count = _int_or_default(
        scope_contract.get("compare_expected_source_count"),
        2 if compare_required else 0,
    )
    covered_compare_source_ids = (
        [
            source_id
            for source_id in supporting_source_ids
            if source_id in set(compare_target_source_ids)
        ]
        if compare_target_source_ids
        else list(supporting_source_ids)
    )
    compare_coverage_satisfied = (
        True
        if not compare_required
        else bool(source_scope_satisfied)
    )
    target_source_satisfied = (
        True
        if not target_source_required
        else bool(source_scope_satisfied)
    )

    preferred_channels_required = bool(required_channels)
    preferred_channels_satisfied = (
        True
        if not preferred_channels_required
        else set(required_channels).issubset(set(used_channels))
    )
    available_but_unused_channels = [
        channel
        for channel in required_channels
        if channel in set(matched_channels) and channel not in set(used_channels)
    ]
    missing_used_channels = [
        channel for channel in required_channels if channel not in set(used_channels)
    ]

    blocker_reason: str | None = None
    if support_basis in BOUNDARY_SUPPORT_BASES:
        blocker_reason = "nonrepairable-support-basis"
    elif answer_state == "abstained":
        blocker_reason = "abstained-answer"
    elif _nonempty_string(reference_contract.get("unresolved_reason")) == "missing-source":
        blocker_reason = "missing-source-boundary"
    elif source_escalation_required:
        blocker_reason = "source-escalation-required"
    elif render_inspection_required and published_artifacts_sufficient is not True:
        blocker_reason = "render-inspection-required"

    repairable_gap_types: list[str] = []
    if blocker_reason is None and target_source_required and not target_source_satisfied:
        repairable_gap_types.append("source-scoped-target-support")
    if blocker_reason is None and compare_required and not compare_coverage_satisfied:
        repairable_gap_types.append("compare-coverage")
    if (
        blocker_reason is None
        and preferred_channels_required
        and not preferred_channels_satisfied
        and bool(published_artifacts_sufficient)
        and bool(available_but_unused_channels)
    ):
        repairable_gap_types.append("preferred-channel-grounding")

    all_required_obligations = {
        "reference_honesty": reference_required,
        "target_source_support": target_source_required,
        "compare_coverage": compare_required,
        "preferred_channels": preferred_channels_required,
    }
    all_satisfied = (
        reference_satisfied
        and target_source_satisfied
        and compare_coverage_satisfied
        and preferred_channels_satisfied
    )
    repair_permitted = bool(repairable_gap_types) and repair_attempt_count < max_repair_attempts
    if blocker_reason is not None:
        status = "blocked-gap"
    elif all_satisfied:
        status = "satisfied"
    elif repair_permitted:
        status = "repairable-gap"
    else:
        status = "honest-close-required"
    primary_gap_type = repairable_gap_types[0] if repairable_gap_types else blocker_reason

    return {
        "schema_version": 1,
        "status": status,
        "answer_state": answer_state,
        "support_basis": support_basis,
        "repair_attempt_count": repair_attempt_count,
        "max_repair_attempts": max_repair_attempts,
        "repair_permitted": repair_permitted,
        "repairable_gap_types": repairable_gap_types,
        "primary_gap_type": primary_gap_type,
        "primary_issue_code": _nonempty_string(trace.get("primary_issue_code")),
        "issue_codes": issue_codes,
        "blocking_reason": blocker_reason,
        "obligations": {
            "reference_honesty": {
                "required": reference_required,
                "satisfied": reference_satisfied,
                "expected_resolution_summary": _nonempty_string(
                    reference_contract.get("resolution_summary")
                ),
                "actual_resolution_summary": trace_reference_summary,
            },
            "target_source_support": {
                "required": target_source_required,
                "satisfied": target_source_satisfied,
                "scope_mode": scope_mode,
                "target_source_id": _nonempty_string(scope_contract.get("target_source_id")),
                "supporting_source_ids": supporting_source_ids,
            },
            "compare_coverage": {
                "required": compare_required,
                "satisfied": compare_coverage_satisfied,
                "expected_source_count": compare_expected_source_count,
                "covered_source_ids": covered_compare_source_ids,
                "compare_target_source_ids": compare_target_source_ids,
            },
            "preferred_channels": {
                "required": preferred_channels_required,
                "satisfied": preferred_channels_satisfied,
                "required_channels": required_channels,
                "matched_channels": matched_channels,
                "used_channels": used_channels,
                "missing_used_channels": missing_used_channels,
                "available_but_unused_channels": available_but_unused_channels,
                "published_artifacts_sufficient": published_artifacts_sufficient,
            },
        },
        "nonrepairable_obligations": [
            obligation for obligation, required in all_required_obligations.items() if required
        ]
        if blocker_reason is not None
        else [],
    }


def support_fulfillment_notice(
    support_fulfillment: dict[str, Any] | None,
) -> str | None:
    """Return a short host-facing explanation derived from support fulfillment."""
    fulfillment = dict(support_fulfillment) if isinstance(support_fulfillment, dict) else {}
    status = _nonempty_string(fulfillment.get("status"))
    primary_gap_type = _nonempty_string(fulfillment.get("primary_gap_type"))
    if status == "satisfied":
        return None
    if primary_gap_type == "source-scoped-target-support":
        return "Final support did not preserve the requested source boundary."
    if primary_gap_type == "compare-coverage":
        return "Final support still collapsed onto fewer comparison sources than required."
    if primary_gap_type == "preferred-channel-grounding":
        return (
            "Relevant published evidence existed, but final support did not "
            "preserve the required channels."
        )
    if primary_gap_type == "source-escalation-required":
        return (
            "Published artifacts still need governed source escalation before "
            "the ask contract can close."
        )
    if primary_gap_type == "render-inspection-required":
        return "Render inspection is still required before the ask contract can close."
    if primary_gap_type == "missing-source-boundary":
        return "The requested source could not be resolved, so the ask stopped at that boundary."
    if primary_gap_type == "nonrepairable-support-basis":
        return (
            "This outcome closed under a non-KB support basis instead of a "
            "repairable KB contract gap."
        )
    return None

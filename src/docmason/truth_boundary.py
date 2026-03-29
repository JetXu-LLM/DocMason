"""Wave 3 truth-boundary helpers for source scope and canonical support."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

SOURCE_SCOPED_MODES = frozenset({"source-scoped-soft", "source-scoped-hard"})
COMPARE_HINT_PATTERN = re.compile(r"\b(compare|comparison|versus|vs\.?|difference|between)\b", re.I)
SINGLE_SOURCE_HINT_PATTERN = re.compile(
    (
        r"\b(using only|use only|only the document|only the deck|only the file|"
        r"do not use any other source|don't use any other source|no other source|"
        r"single document|single source)\b"
    ),
    re.I,
)
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+|[A-Za-z]:\\(?:[^\\\s]+\\)+[^\\\s]+)"
)


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(
        dict.fromkeys(
            value.strip() for value in values if isinstance(value, str) and value.strip()
        )
    )


def normalize_repo_relative_source_path(path: str | None) -> str | None:
    """Return a legal user-visible repo-relative source path when available."""
    if not isinstance(path, str):
        return None
    normalized = path.strip()
    if not normalized:
        return None
    if Path(normalized).is_absolute():
        return None
    if not normalized.startswith("original_doc/"):
        return None
    return normalized


def format_user_visible_source_ref(*, title: str | None, current_path: str | None) -> str | None:
    """Render the canonical user-visible source reference string for Wave 3."""
    relative_path = normalize_repo_relative_source_path(current_path)
    if relative_path is None:
        return None
    basename = Path(relative_path).name
    normalized_title = _nonempty_string(title)
    if normalized_title is None:
        return f"{basename} - {relative_path}"
    if normalized_title == basename or Path(normalized_title).stem == Path(basename).stem:
        return f"{basename} - {relative_path}"
    return f"{normalized_title} ({basename}) - {relative_path}"


def has_single_source_constraint(question: str) -> bool:
    """Return whether the question text explicitly requests a single source."""
    if not isinstance(question, str) or not question.strip():
        return False
    return bool(SINGLE_SOURCE_HINT_PATTERN.search(question))


def is_compare_scope(question: str) -> bool:
    """Return whether the question is explicitly comparative."""
    return bool(isinstance(question, str) and COMPARE_HINT_PATTERN.search(question))


def build_source_scope_policy(
    *,
    question: str,
    question_class: str | None,
    question_domain: str | None,
    reference_resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    """Derive the persisted source-scope policy for one turn or trace."""
    resolution = dict(reference_resolution) if isinstance(reference_resolution, dict) else {}
    target_source_id = _nonempty_string(resolution.get("resolved_source_id"))
    target_source_ref = _nonempty_string(resolution.get("target_source_ref"))
    hard_boundary = bool(resolution.get("hard_boundary"))
    explicit_single_source = has_single_source_constraint(question)
    compare_scope = is_compare_scope(question)
    source_match_status = str(resolution.get("source_match_status") or "none")
    source_narrowing_allowed = bool(resolution.get("source_narrowing_allowed"))

    if compare_scope:
        scope_mode = "compare"
    elif hard_boundary or explicit_single_source:
        scope_mode = "source-scoped-hard"
    elif target_source_id and source_narrowing_allowed and source_match_status in {
        "exact",
        "approximate",
    }:
        scope_mode = "source-scoped-soft"
    else:
        scope_mode = "global"

    return {
        "scope_mode": scope_mode,
        "target_source_id": target_source_id,
        "target_source_ref": target_source_ref,
        "hard_boundary_on_missing_source": bool(
            hard_boundary and target_source_id is None and scope_mode == "source-scoped-hard"
        ),
        "require_target_source_per_supported_segment": scope_mode in SOURCE_SCOPED_MODES,
        "require_target_source_in_final_support": scope_mode in SOURCE_SCOPED_MODES,
        "allow_pending_interaction_direct_support": scope_mode not in SOURCE_SCOPED_MODES,
    }


def apply_machine_semantic_guard(
    *,
    question: str,
    question_domain: str,
    support_strategy: str,
    reference_resolution: dict[str, Any] | None,
) -> tuple[str, str, bool]:
    """Guard critical routing fields when the ask is clearly source-scoped."""
    policy = build_source_scope_policy(
        question=question,
        question_class=None,
        question_domain=question_domain,
        reference_resolution=reference_resolution,
    )
    if policy["scope_mode"] == "global":
        return question_domain, support_strategy, False
    guarded_domain = (
        "composition" if question_domain == "composition" else "workspace-corpus"
    )
    guarded_strategy = "kb-first"
    changed = guarded_domain != question_domain or guarded_strategy != support_strategy
    return guarded_domain, guarded_strategy, changed


def result_direct_support_score(result: dict[str, Any]) -> float:
    """Return the direct evidence score used by canonical support admission."""
    score = result.get("score")
    if not isinstance(score, dict):
        return 0.0
    lexical_total = (
        float(score.get("lexical_source", 0.0))
        + float(score.get("lexical_units", 0.0))
        + float(score.get("lexical_artifacts", 0.0))
    )
    reference_bonus = float(score.get("reference_bonus", 0.0))
    if lexical_total > 0:
        return lexical_total
    return reference_bonus if reference_bonus >= 6.0 else 0.0


def result_is_canonical_support(
    result: dict[str, Any],
    *,
    source_scope_policy: dict[str, Any] | None,
) -> bool:
    """Return whether one retrieval result may enter canonical KB support."""
    source_family = str(result.get("source_family") or "corpus")
    policy = dict(source_scope_policy) if isinstance(source_scope_policy, dict) else {}
    if source_family != "corpus":
        return False
    if result_direct_support_score(result) <= 0:
        return False
    scope_mode = str(policy.get("scope_mode") or "global")
    target_source_id = _nonempty_string(policy.get("target_source_id"))
    if scope_mode in SOURCE_SCOPED_MODES and target_source_id is not None:
        return str(result.get("source_id") or "") == target_source_id
    return True


def segment_scope_satisfied(
    *,
    source_scope_policy: dict[str, Any] | None,
    supporting_source_ids: list[str] | None,
    grounding_status: str,
) -> bool:
    """Return whether one segment satisfies the turn-level source scope."""
    policy = dict(source_scope_policy) if isinstance(source_scope_policy, dict) else {}
    scope_mode = str(policy.get("scope_mode") or "global")
    if scope_mode not in SOURCE_SCOPED_MODES:
        return True
    if grounding_status == "abstained":
        return True
    target_source_id = _nonempty_string(policy.get("target_source_id"))
    if target_source_id is None:
        return False
    return target_source_id in {
        value for value in (supporting_source_ids or []) if isinstance(value, str) and value
    }


def build_canonical_support_summary(
    *,
    source_scope_policy: dict[str, Any] | None,
    segment_traces: list[dict[str, Any]],
    support_basis: str | None,
) -> dict[str, Any]:
    """Aggregate canonical support truth across trace segments."""
    policy = dict(source_scope_policy) if isinstance(source_scope_policy, dict) else {}
    supporting_source_ids: list[str] = []
    supporting_unit_ids: list[str] = []
    supporting_artifact_ids: list[str] = []
    support_layers_present: list[str] = []
    mixed_support_explainable = support_basis != "mixed"
    grounded = 0
    partially_grounded = 0
    unresolved = 0
    scope_satisfied = True
    for segment in segment_traces:
        if not isinstance(segment, dict):
            continue
        grounding_status = str(segment.get("grounding_status") or "unresolved")
        if grounding_status == "grounded":
            grounded += 1
        elif grounding_status == "partially-grounded":
            partially_grounded += 1
        else:
            unresolved += 1
        supporting_source_ids.extend(
            value
            for value in segment.get("supporting_source_ids", [])
            if isinstance(value, str) and value
        )
        supporting_unit_ids.extend(
            value
            for value in segment.get("supporting_unit_ids", [])
            if isinstance(value, str) and value
        )
        supporting_artifact_ids.extend(
            value
            for value in segment.get("supporting_artifact_ids", [])
            if isinstance(value, str) and value
        )
        support_lanes = segment.get("support_lanes")
        if isinstance(support_lanes, dict):
            for lane_name in ("kb", "interaction", "external"):
                lane_items = support_lanes.get(lane_name, [])
                if isinstance(lane_items, list) and lane_items:
                    support_layers_present.append(lane_name)
            if support_basis == "mixed":
                mixed_support_explainable = mixed_support_explainable or bool(
                    support_lanes.get("kb")
                    or support_lanes.get("interaction")
                    or support_lanes.get("external")
                )
        scope_satisfied = scope_satisfied and segment_scope_satisfied(
            source_scope_policy=policy,
            supporting_source_ids=segment.get("supporting_source_ids", []),
            grounding_status=grounding_status,
        )
    return {
        "scope_mode": str(policy.get("scope_mode") or "global"),
        "target_source_id": _nonempty_string(policy.get("target_source_id")),
        "target_source_ref": _nonempty_string(policy.get("target_source_ref")),
        "source_scope_satisfied": scope_satisfied,
        "support_layers_present": _deduplicate_strings(support_layers_present),
        "supporting_source_ids": _deduplicate_strings(supporting_source_ids),
        "supporting_unit_ids": _deduplicate_strings(supporting_unit_ids),
        "supporting_artifact_ids": _deduplicate_strings(supporting_artifact_ids),
        "segment_truth_counts": {
            "grounded": grounded,
            "partially_grounded": partially_grounded,
            "unresolved": unresolved,
        },
        "mixed_support_explainable": mixed_support_explainable,
    }


def trace_issue_codes(
    *,
    answer_state: str,
    canonical_support_summary: dict[str, Any] | None,
    published_artifacts_sufficient: bool | None,
    source_escalation_required: bool | None,
    support_basis: str | None,
    support_manifest_path: str | None,
) -> list[str]:
    """Return trace-level issue codes aligned with Wave 3 commit legality."""
    summary = (
        dict(canonical_support_summary)
        if isinstance(canonical_support_summary, dict)
        else {}
    )
    issue_codes: list[str] = []
    if published_artifacts_sufficient is False:
        issue_codes.append("published-artifacts-gap")
    if source_escalation_required is True:
        issue_codes.append("source-escalation-required")
    if (
        str(summary.get("scope_mode") or "global") in SOURCE_SCOPED_MODES
        and not bool(summary.get("source_scope_satisfied"))
    ):
        issue_codes.append("source-scope-missing-target-support")
    segment_truth_counts = summary.get("segment_truth_counts")
    if (
        answer_state == "grounded"
        and support_basis in {"kb-grounded", "mixed", None}
        and isinstance(segment_truth_counts, dict)
        and int(segment_truth_counts.get("unresolved", 0)) > 0
    ):
        issue_codes.append("trace-answer-state-mismatch")
    if support_basis == "mixed" and not support_manifest_path and not bool(
        summary.get("mixed_support_explainable")
    ):
        issue_codes.append("mixed-support-unexplained")
    return _deduplicate_strings(issue_codes)


def answer_mentions_illegal_machine_path(answer_text: str) -> bool:
    """Return whether answer text exposes a disallowed absolute machine path."""
    if not isinstance(answer_text, str) or not answer_text:
        return False
    for match in ABSOLUTE_PATH_PATTERN.finditer(answer_text):
        candidate = match.group(0)
        prefix = answer_text[max(0, match.start() - 8) : match.start()].lower()
        if prefix.endswith("http://") or prefix.endswith("https://"):
            continue
        if candidate.startswith("original_doc/"):
            continue
        if candidate.startswith("knowledge_base/"):
            continue
        if candidate.startswith("runtime/"):
            continue
        if Path(candidate).is_absolute() or re.match(r"^[A-Za-z]:\\", candidate):
            return True
    return False


def support_manifest_is_local_corpus(
    support_manifest: dict[str, Any] | None,
    *,
    support_manifest_sources: list[dict[str, Any]] | None = None,
) -> bool:
    """Return whether one support manifest actually points at local corpus sources."""
    candidate_sources: list[dict[str, Any]] = []
    if isinstance(support_manifest, dict):
        candidate_sources.extend(
            item for item in support_manifest.get("sources", []) if isinstance(item, dict)
        )
    if isinstance(support_manifest_sources, list):
        candidate_sources.extend(
            item for item in support_manifest_sources if isinstance(item, dict)
        )
    for source in candidate_sources:
        source_type = _nonempty_string(source.get("source_type"))
        if source_type in {
            "local-file",
            "workspace-corpus",
            "original-doc",
            "kb-source",
            "published-corpus",
        }:
            return True
        for field_name in ("url", "title", "path", "source_path", "current_path"):
            value = _nonempty_string(source.get(field_name))
            if value is None:
                continue
            if normalize_repo_relative_source_path(value) is not None:
                return True
    return False

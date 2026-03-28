"""Local evaluation, benchmarking, baseline, and feedback helpers."""

from __future__ import annotations

import hashlib
import json
import os
import statistics
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .contracts import ANSWER_STATES, SUPPORT_BASIS_VALUES
from .conversation import LOG_ORIGIN_EVALUATION_SUITE
from .project import WorkspacePaths, read_json, write_json
from .retrieval import (
    ANSWER_WORKFLOW_ID,
    RETRIEVAL_STRATEGY_ID,
    retrieve_corpus,
    trace_answer_text,
    trace_source,
    utc_now,
)

EVALUATION_SCHEMA_VERSION = 1
RUBRIC_SCHEMA_VERSION = 1
JUDGE_TRIALS_SCHEMA_VERSION = 1
BASELINE_SCHEMA_VERSION = 1
FEEDBACK_SCHEMA_VERSION = 1
DEFAULT_TRIAL_COUNT = 3
RUBRIC_DIMENSIONS = (
    "factual_alignment",
    "coverage",
    "source_discipline",
    "uncertainty_discipline",
    "visual_evidence_handling",
)
FEEDBACK_TAXONOMY = (
    "retrieval_miss",
    "wrong_source_chosen",
    "incomplete_citation",
    "unsupported_synthesis",
    "should_abstain",
    "render_required",
    "contradiction_missed",
    "user_corrected_fact",
    "alternate_format_double_count",
    "coverage_gap",
)
RUN_STATUS_ORDER = {"passed": 0, "degraded": 1, "failed": 2, "incompatible": 3}
EVALUATION_EXECUTION_MODES = ("retrieve", "trace-source", "trace-answer", "ask-turn")
ASK_REPLAY_SOURCE_KINDS = frozenset({"manual-suite"})
ASK_TURN_COMPLETION_OVERRIDE_FIELDS = frozenset(
    {"inner_workflow_id", "status", "support_basis", "support_manifest_path"}
)
ASK_TURN_HYBRID_COMPLETION_STATUSES = frozenset({"covered", "blocked"})
ASK_TURN_EXPECTATION_KEYS = frozenset(
    {
        "final_turn_status",
        "reused_turn",
        "auto_prepare_triggered",
        "auto_sync_triggered",
        "hybrid_refresh_triggered",
        "hybrid_refresh_completion_status",
        "query_session_count",
        "trace_count",
        "required_run_events",
    }
)


class EvaluationConfigurationError(ValueError):
    """Raised when a private evaluation artifact is invalid."""


class FeedbackValidationError(ValueError):
    """Raised when a feedback record is invalid."""


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationConfigurationError(f"`{field_name}` must be a non-empty string.")
    return value.strip()


def _require_optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise EvaluationConfigurationError(f"`{field_name}` must be a non-empty string when set.")
    return value.strip()


def _require_bool_or_none(value: Any, field_name: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EvaluationConfigurationError(f"`{field_name}` must be a boolean when set.")
    return value


def _require_optional_enum(value: Any, field_name: str, *, allowed: tuple[str, ...]) -> str | None:
    if value is None:
        return None
    normalized = _require_optional_string(value, field_name)
    if normalized is None:
        return None
    if normalized not in allowed:
        raise EvaluationConfigurationError(
            f"`{field_name}` must be one of {', '.join(allowed)} when set."
        )
    return normalized


def _require_int(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or value < minimum:
        raise EvaluationConfigurationError(
            f"`{field_name}` must be an integer greater than or equal to {minimum}."
        )
    return value


def _require_string_list(
    value: Any,
    field_name: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list):
        raise EvaluationConfigurationError(f"`{field_name}` must be a list of non-empty strings.")
    invalid_items = any(not isinstance(item, str) or not item.strip() for item in value)
    if invalid_items:
        raise EvaluationConfigurationError(f"`{field_name}` must be a list of non-empty strings.")
    normalized = [item.strip() for item in value]
    if not allow_empty and not normalized:
        raise EvaluationConfigurationError(f"`{field_name}` may not be empty.")
    return normalized


def _require_string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise EvaluationConfigurationError(f"`{field_name}` must be an object.")
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise EvaluationConfigurationError(f"`{field_name}` keys must be non-empty strings.")
        normalized[key.strip()] = _require_string(item, f"{field_name}.{key}")
    return normalized


def _require_mapping(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EvaluationConfigurationError(f"`{field_name}` must be an object.")
    return dict(value)


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _load_required_payload(path: Path, *, field_name: str) -> dict[str, Any]:
    payload = read_json(path)
    if not payload:
        raise EvaluationConfigurationError(f"Missing `{field_name}` at `{path}`.")
    return payload


def _resolve_workspace_path(paths: WorkspacePaths, path: Path | None) -> Path | None:
    """Resolve a private planning or runtime path relative to the workspace root."""
    if path is None:
        return None
    return path if path.is_absolute() else paths.root / path


def _resolve_required_workspace_path(paths: WorkspacePaths, path: Path) -> Path:
    """Resolve a required private path relative to the workspace root."""
    resolved = _resolve_workspace_path(paths, path)
    assert resolved is not None
    return resolved


def _normalize_ask_turn_case(case_id: str, payload: Any) -> dict[str, Any]:
    """Validate and normalize the ask-turn replay contract for one case."""
    ask_replay = _require_mapping(payload, f"{case_id}.ask_replay")
    replay_source = _require_mapping(
        ask_replay.get("replay_source"),
        f"{case_id}.ask_replay.replay_source",
    )
    replay_source_kind = _require_string(
        replay_source.get("kind"),
        f"{case_id}.ask_replay.replay_source.kind",
    )
    if replay_source_kind not in ASK_REPLAY_SOURCE_KINDS:
        raise EvaluationConfigurationError(
            f"`{case_id}.ask_replay.replay_source.kind` must be one of "
            f"{', '.join(sorted(ASK_REPLAY_SOURCE_KINDS))}."
        )
    normalized_replay_source: dict[str, Any] = {"kind": replay_source_kind}
    for field_name in ("candidate_id", "conversation_id", "turn_id", "recorded_at"):
        value = _require_optional_string(
            replay_source.get(field_name),
            f"{case_id}.ask_replay.replay_source.{field_name}",
        )
        if value is not None:
            normalized_replay_source[field_name] = value

    semantic_analysis = _require_mapping(
        ask_replay.get("semantic_analysis"),
        f"{case_id}.ask_replay.semantic_analysis",
    )
    if not semantic_analysis:
        raise EvaluationConfigurationError(
            f"`{case_id}.ask_replay.semantic_analysis` must not be empty."
        )

    host_thread_ref = _require_optional_string(
        ask_replay.get("host_thread_ref"),
        f"{case_id}.ask_replay.host_thread_ref",
    )

    continuations_payload = ask_replay.get("continuations", [])
    if continuations_payload is None:
        continuations_payload = []
    if not isinstance(continuations_payload, list):
        raise EvaluationConfigurationError(f"`{case_id}.ask_replay.continuations` must be a list.")
    normalized_continuations: list[dict[str, Any]] = []
    for index, continuation in enumerate(continuations_payload, start=1):
        continuation_payload = _require_mapping(
            continuation,
            f"{case_id}.ask_replay.continuations[{index}]",
        )
        continuation_message = _require_string(
            continuation_payload.get("message"),
            f"{case_id}.ask_replay.continuations[{index}].message",
        )
        continuation_semantic_analysis = continuation_payload.get("semantic_analysis")
        normalized_continuation: dict[str, Any] = {"message": continuation_message}
        if continuation_semantic_analysis is not None:
            normalized_semantic_analysis = _require_mapping(
                continuation_semantic_analysis,
                f"{case_id}.ask_replay.continuations[{index}].semantic_analysis",
            )
            if not normalized_semantic_analysis:
                raise EvaluationConfigurationError(
                    f"`{case_id}.ask_replay.continuations[{index}].semantic_analysis` "
                    "must not be empty."
                )
            normalized_continuation["semantic_analysis"] = normalized_semantic_analysis
        normalized_continuations.append(normalized_continuation)

    answer_plan_payload = ask_replay.get("answer_plan")
    normalized_answer_plan: dict[str, Any] | None = None
    if answer_plan_payload is not None:
        answer_plan = _require_mapping(answer_plan_payload, f"{case_id}.ask_replay.answer_plan")
        completion_overrides_payload = answer_plan.get("completion_overrides", {})
        if completion_overrides_payload is None:
            completion_overrides_payload = {}
        completion_overrides = _require_mapping(
            completion_overrides_payload,
            f"{case_id}.ask_replay.answer_plan.completion_overrides",
        )
        unknown_completion_override_fields = sorted(
            set(completion_overrides) - ASK_TURN_COMPLETION_OVERRIDE_FIELDS
        )
        if unknown_completion_override_fields:
            raise EvaluationConfigurationError(
                f"`{case_id}.ask_replay.answer_plan.completion_overrides` contains unsupported "
                "fields: " + ", ".join(unknown_completion_override_fields)
            )
        normalized_completion_overrides: dict[str, str] = {}
        if "inner_workflow_id" in completion_overrides:
            normalized_completion_overrides["inner_workflow_id"] = _require_string(
                completion_overrides.get("inner_workflow_id"),
                f"{case_id}.ask_replay.answer_plan.completion_overrides.inner_workflow_id",
            )
        if "status" in completion_overrides:
            normalized_completion_overrides["status"] = _require_string(
                completion_overrides.get("status"),
                f"{case_id}.ask_replay.answer_plan.completion_overrides.status",
            )
        if "support_basis" in completion_overrides:
            normalized_completion_overrides["support_basis"] = _require_optional_enum(
                completion_overrides.get("support_basis"),
                f"{case_id}.ask_replay.answer_plan.completion_overrides.support_basis",
                allowed=SUPPORT_BASIS_VALUES,
            ) or ""
            if not normalized_completion_overrides["support_basis"]:
                raise EvaluationConfigurationError(
                    f"`{case_id}.ask_replay.answer_plan.completion_overrides."
                    "support_basis` must be set when present."
                )
        if "support_manifest_path" in completion_overrides:
            normalized_completion_overrides["support_manifest_path"] = _require_string(
                completion_overrides.get("support_manifest_path"),
                f"{case_id}.ask_replay.answer_plan.completion_overrides.support_manifest_path",
            )
        normalized_answer_plan = {
            "answer_text": _require_string(
                answer_plan.get("answer_text"),
                f"{case_id}.ask_replay.answer_plan.answer_text",
            ),
            "trace_top": _require_int(
                answer_plan.get("trace_top", 3),
                f"{case_id}.ask_replay.answer_plan.trace_top",
                minimum=1,
            ),
            "completion_overrides": normalized_completion_overrides,
        }

    hybrid_refresh_payload = ask_replay.get("hybrid_refresh")
    normalized_hybrid_refresh: dict[str, Any] | None = None
    if hybrid_refresh_payload is not None:
        hybrid_refresh = _require_mapping(
            hybrid_refresh_payload,
            f"{case_id}.ask_replay.hybrid_refresh",
        )
        completion_status = _require_string(
            hybrid_refresh.get("completion_status"),
            f"{case_id}.ask_replay.hybrid_refresh.completion_status",
        )
        if completion_status not in ASK_TURN_HYBRID_COMPLETION_STATUSES:
            raise EvaluationConfigurationError(
                f"`{case_id}.ask_replay.hybrid_refresh.completion_status` must be one of "
                f"{', '.join(sorted(ASK_TURN_HYBRID_COMPLETION_STATUSES))}."
            )
        normalized_hybrid_refresh = {
            "completion_status": completion_status,
            "summary": _require_mapping(
                hybrid_refresh.get("summary", {}),
                f"{case_id}.ask_replay.hybrid_refresh.summary",
            ),
        }
        if completion_status == "covered":
            normalized_hybrid_refresh["post_refresh_answer_text"] = _require_string(
                hybrid_refresh.get("post_refresh_answer_text"),
                f"{case_id}.ask_replay.hybrid_refresh.post_refresh_answer_text",
            )
            normalized_hybrid_refresh["post_refresh_trace_top"] = _require_int(
                hybrid_refresh.get("post_refresh_trace_top", 3),
                f"{case_id}.ask_replay.hybrid_refresh.post_refresh_trace_top",
                minimum=1,
            )

    expectations_payload = ask_replay.get("expectations", {})
    if expectations_payload is None:
        expectations_payload = {}
    expectations = _require_mapping(
        expectations_payload,
        f"{case_id}.ask_replay.expectations",
    )
    unknown_expectation_keys = sorted(set(expectations) - ASK_TURN_EXPECTATION_KEYS)
    if unknown_expectation_keys:
        raise EvaluationConfigurationError(
            f"`{case_id}.ask_replay.expectations` contains unsupported fields: "
            + ", ".join(unknown_expectation_keys)
        )
    normalized_expectations: dict[str, Any] = {}
    if "final_turn_status" in expectations:
        normalized_expectations["final_turn_status"] = _require_string(
            expectations.get("final_turn_status"),
            f"{case_id}.ask_replay.expectations.final_turn_status",
        )
    for field_name in (
        "reused_turn",
        "auto_prepare_triggered",
        "auto_sync_triggered",
        "hybrid_refresh_triggered",
    ):
        if field_name in expectations:
            normalized_expectations[field_name] = _require_bool_or_none(
                expectations.get(field_name),
                f"{case_id}.ask_replay.expectations.{field_name}",
            )
    if "hybrid_refresh_completion_status" in expectations:
        normalized_expectations["hybrid_refresh_completion_status"] = _require_string(
            expectations.get("hybrid_refresh_completion_status"),
            f"{case_id}.ask_replay.expectations.hybrid_refresh_completion_status",
        )
    for field_name in ("query_session_count", "trace_count"):
        if field_name in expectations:
            normalized_expectations[field_name] = _require_int(
                expectations.get(field_name),
                f"{case_id}.ask_replay.expectations.{field_name}",
                minimum=0,
            )
    if "required_run_events" in expectations:
        normalized_expectations["required_run_events"] = _require_string_list(
            expectations.get("required_run_events"),
            f"{case_id}.ask_replay.expectations.required_run_events",
            allow_empty=False,
        )

    return {
        "replay_source": normalized_replay_source,
        "host_thread_ref": host_thread_ref,
        "semantic_analysis": semantic_analysis,
        "continuations": normalized_continuations,
        "answer_plan": normalized_answer_plan,
        "hybrid_refresh": normalized_hybrid_refresh,
        "expectations": normalized_expectations,
    }


def load_rubric_definition(path: Path) -> dict[str, Any]:
    """Load and validate a private evaluation rubric definition."""
    payload = _load_required_payload(path, field_name="rubric definition")
    schema_version = payload.get("schema_version")
    if schema_version != RUBRIC_SCHEMA_VERSION:
        raise EvaluationConfigurationError(
            f"`{path}` has unsupported rubric schema_version `{schema_version}`."
        )
    rubric_id = _require_string(payload.get("rubric_id"), "rubric_id")
    title = _require_string(payload.get("title"), "title")
    dimensions_payload = payload.get("dimensions")
    if not isinstance(dimensions_payload, dict) or not dimensions_payload:
        raise EvaluationConfigurationError("`dimensions` must be a non-empty object.")
    dimensions: dict[str, dict[str, str]] = {}
    for dimension in RUBRIC_DIMENSIONS:
        dimension_payload = dimensions_payload.get(dimension)
        if not isinstance(dimension_payload, dict):
            raise EvaluationConfigurationError(f"Missing rubric dimension `{dimension}`.")
        dimensions[dimension] = _require_string_mapping(
            dimension_payload,
            f"dimensions.{dimension}",
        )
        for required_key in ("description", "score_0", "score_1", "score_2"):
            if required_key not in dimensions[dimension]:
                raise EvaluationConfigurationError(
                    f"`dimensions.{dimension}` must define `{required_key}`."
                )
    acceptance = payload.get("acceptance_thresholds")
    if not isinstance(acceptance, dict):
        raise EvaluationConfigurationError("`acceptance_thresholds` must be an object.")
    trial_count = _require_int(
        payload.get("trial_count", DEFAULT_TRIAL_COUNT),
        "trial_count",
        minimum=1,
    )
    if trial_count != DEFAULT_TRIAL_COUNT:
        raise EvaluationConfigurationError("Phase 5 currently requires exactly three judge trials.")
    deterministic_pass_rate = acceptance.get("deterministic_pass_rate")
    answer_mean_score = acceptance.get("answer_mean_score")
    aggregate_rubric_regression_limit = acceptance.get("aggregate_rubric_regression_limit")
    if not isinstance(deterministic_pass_rate, (int, float)):
        raise EvaluationConfigurationError(
            "`acceptance_thresholds.deterministic_pass_rate` must be numeric."
        )
    if not isinstance(answer_mean_score, (int, float)):
        raise EvaluationConfigurationError(
            "`acceptance_thresholds.answer_mean_score` must be numeric."
        )
    if not isinstance(aggregate_rubric_regression_limit, (int, float)):
        raise EvaluationConfigurationError(
            "`acceptance_thresholds.aggregate_rubric_regression_limit` must be numeric."
        )
    judge_instructions = _require_string_list(
        payload.get("judge_instructions", []),
        "judge_instructions",
        allow_empty=False,
    )
    return {
        "schema_version": RUBRIC_SCHEMA_VERSION,
        "rubric_id": rubric_id,
        "title": title,
        "trial_count": trial_count,
        "dimensions": dimensions,
        "acceptance_thresholds": {
            "deterministic_pass_rate": float(deterministic_pass_rate),
            "answer_mean_score": float(answer_mean_score),
            "aggregate_rubric_regression_limit": float(aggregate_rubric_regression_limit),
        },
        "judge_instructions": judge_instructions,
    }


def load_evaluation_suite(path: Path, *, rubric: dict[str, Any]) -> dict[str, Any]:
    """Load and validate a private evaluation suite definition."""
    payload = _load_required_payload(path, field_name="evaluation suite")
    schema_version = payload.get("schema_version")
    if schema_version != EVALUATION_SCHEMA_VERSION:
        raise EvaluationConfigurationError(
            f"`{path}` has unsupported evaluation schema_version `{schema_version}`."
        )
    suite_id = _require_string(payload.get("suite_id"), "suite_id")
    title = _require_string(payload.get("title"), "title")
    description = _require_string(payload.get("description"), "description")
    target = _require_string(payload.get("target", "current"), "target")
    if target != "current":
        raise EvaluationConfigurationError(
            "Phase 5 private suites currently target `current` only."
        )
    corpus_signature = _require_string(payload.get("corpus_signature"), "corpus_signature")
    retrieval_strategy_id = _require_string(
        payload.get("retrieval_strategy_id"),
        "retrieval_strategy_id",
    )
    answer_workflow_id = _require_string(
        payload.get("answer_workflow_id"),
        "answer_workflow_id",
    )
    if retrieval_strategy_id != RETRIEVAL_STRATEGY_ID:
        raise EvaluationConfigurationError(
            f"Suite expects retrieval strategy `{retrieval_strategy_id}`, "
            f"but the implementation exposes `{RETRIEVAL_STRATEGY_ID}`."
        )
    if answer_workflow_id != ANSWER_WORKFLOW_ID:
        raise EvaluationConfigurationError(
            f"Suite expects answer workflow `{answer_workflow_id}`, "
            f"but the implementation exposes `{ANSWER_WORKFLOW_ID}`."
        )
    cases_payload = payload.get("cases")
    if not isinstance(cases_payload, list) or not cases_payload:
        raise EvaluationConfigurationError("`cases` must be a non-empty list.")
    rubric_dimensions = set(rubric["dimensions"])
    seen_case_ids: set[str] = set()
    normalized_cases: list[dict[str, Any]] = []
    for item in cases_payload:
        if not isinstance(item, dict):
            raise EvaluationConfigurationError("Each evaluation case must be an object.")
        case_id = _require_string(item.get("case_id"), "case_id")
        if case_id in seen_case_ids:
            raise EvaluationConfigurationError(f"Duplicate evaluation case `{case_id}`.")
        seen_case_ids.add(case_id)
        execution_mode = _require_string(item.get("execution_mode"), "execution_mode")
        if execution_mode not in EVALUATION_EXECUTION_MODES:
            raise EvaluationConfigurationError(
                f"`execution_mode` for `{case_id}` must be one of "
                + ", ".join(f"`{mode}`" for mode in EVALUATION_EXECUTION_MODES)
                + "."
            )
        if execution_mode == "ask-turn":
            ask_replay = _normalize_ask_turn_case(case_id, item.get("ask_replay"))
        else:
            ask_replay = None
            if item.get("ask_replay") is not None:
                raise EvaluationConfigurationError(
                    f"Case `{case_id}` may define `ask_replay` only when "
                    "`execution_mode` is `ask-turn`."
                )
        active_dimensions = _require_string_list(
            item.get("active_rubric_dimensions", []),
            f"{case_id}.active_rubric_dimensions",
        )
        unknown_dimensions = sorted(set(active_dimensions) - rubric_dimensions)
        if unknown_dimensions:
            raise EvaluationConfigurationError(
                f"Case `{case_id}` references unknown rubric dimensions: "
                + ", ".join(unknown_dimensions)
            )
        feedback_tags = _require_string_list(
            item.get("feedback_tags", []),
            f"{case_id}.feedback_tags",
            allow_empty=False,
        )
        unknown_feedback_tags = sorted(set(feedback_tags) - set(FEEDBACK_TAXONOMY))
        if unknown_feedback_tags:
            raise EvaluationConfigurationError(
                f"Case `{case_id}` references unknown feedback tags: "
                + ", ".join(unknown_feedback_tags)
            )
        expected_answer_state = _require_optional_string(
            item.get("expected_answer_state"),
            f"{case_id}.expected_answer_state",
        )
        if expected_answer_state is not None and expected_answer_state not in ANSWER_STATES:
            raise EvaluationConfigurationError(
                f"Case `{case_id}` has unsupported expected_answer_state `{expected_answer_state}`."
            )
        expected_support_basis = _require_optional_enum(
            item.get("expected_support_basis"),
            f"{case_id}.expected_support_basis",
            allowed=SUPPORT_BASIS_VALUES,
        )
        declared_answer_state = _require_optional_enum(
            item.get("declared_answer_state"),
            f"{case_id}.declared_answer_state",
            allowed=tuple(sorted(ANSWER_STATES)),
        )
        execution_support_basis = _require_optional_enum(
            item.get("execution_support_basis"),
            f"{case_id}.execution_support_basis",
            allowed=SUPPORT_BASIS_VALUES,
        )
        execution_inner_workflow_id = _require_optional_enum(
            item.get("execution_inner_workflow_id"),
            f"{case_id}.execution_inner_workflow_id",
            allowed=("grounded-answer", "grounded-composition"),
        )
        expected_status = _require_string(item.get("expected_status"), f"{case_id}.expected_status")
        if expected_status not in {"ready", "degraded", "no-results"}:
            raise EvaluationConfigurationError(
                f"Case `{case_id}` has unsupported expected_status `{expected_status}`."
            )
        required_sources_or_units = _require_string_list(
            item.get("required_sources_or_units", []),
            f"{case_id}.required_sources_or_units",
        )
        minimum_support_overlap = _require_int(
            item.get("minimum_support_overlap", len(required_sources_or_units)),
            f"{case_id}.minimum_support_overlap",
            minimum=0,
        )
        if minimum_support_overlap > len(required_sources_or_units):
            raise EvaluationConfigurationError(
                f"Case `{case_id}` requests minimum_support_overlap `{minimum_support_overlap}` "
                f"but only defines {len(required_sources_or_units)} required support identifiers."
            )
        normalized_cases.append(
            {
                "case_id": case_id,
                "family": _require_string(item.get("family"), f"{case_id}.family"),
                "execution_mode": execution_mode,
                "query_or_prompt": _require_string(
                    item.get("query_or_prompt"),
                    f"{case_id}.query_or_prompt",
                ),
                "expected_primary_sources": _require_string_list(
                    item.get("expected_primary_sources", []),
                    f"{case_id}.expected_primary_sources",
                ),
                "required_sources_or_units": required_sources_or_units,
                "minimum_support_overlap": minimum_support_overlap,
                "forbidden_sources_or_units": _require_string_list(
                    item.get("forbidden_sources_or_units", []),
                    f"{case_id}.forbidden_sources_or_units",
                ),
                "expected_status": expected_status,
                "expected_answer_state": expected_answer_state,
                "expected_support_basis": expected_support_basis,
                "expected_render_inspection_required": _require_bool_or_none(
                    item.get("expected_render_inspection_required"),
                    f"{case_id}.expected_render_inspection_required",
                ),
                "reference_facts": _require_string_list(
                    item.get("reference_facts", []),
                    f"{case_id}.reference_facts",
                    allow_empty=False,
                ),
                "active_rubric_dimensions": active_dimensions,
                "feedback_tags": feedback_tags,
                "critical": bool(item.get("critical", False)),
                "top": _require_int(item.get("top", 3), f"{case_id}.top", minimum=1),
                "graph_hops": _require_int(
                    item.get("graph_hops", 1),
                    f"{case_id}.graph_hops",
                    minimum=0,
                ),
                "include_renders": bool(item.get("include_renders", True)),
                "unit_id": _require_optional_string(item.get("unit_id"), f"{case_id}.unit_id"),
                "declared_answer_state": declared_answer_state,
                "execution_support_basis": execution_support_basis,
                "execution_support_manifest_path": _require_optional_string(
                    item.get("execution_support_manifest_path"),
                    f"{case_id}.execution_support_manifest_path",
                ),
                "execution_inner_workflow_id": execution_inner_workflow_id,
                "ask_replay": ask_replay,
            }
        )
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "suite_id": suite_id,
        "title": title,
        "description": description,
        "target": target,
        "corpus_signature": corpus_signature,
        "retrieval_strategy_id": retrieval_strategy_id,
        "answer_workflow_id": answer_workflow_id,
        "cases": normalized_cases,
    }


def load_judge_trials(
    path: Path,
    *,
    suite: dict[str, Any],
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Load and validate private judge-trial inputs for answer cases."""
    payload = _load_required_payload(path, field_name="judge trials")
    schema_version = payload.get("schema_version")
    if schema_version != JUDGE_TRIALS_SCHEMA_VERSION:
        raise EvaluationConfigurationError(
            f"`{path}` has unsupported judge-trials schema_version `{schema_version}`."
        )
    suite_id = _require_string(payload.get("suite_id"), "suite_id")
    if suite_id != suite["suite_id"]:
        raise EvaluationConfigurationError(
            f"Judge trials target suite `{suite_id}`, expected `{suite['suite_id']}`."
        )
    judge_profile_payload = payload.get("judge_profile")
    if not isinstance(judge_profile_payload, dict):
        raise EvaluationConfigurationError("`judge_profile` must be an object.")
    judge_profile = {
        "mode": _require_string(judge_profile_payload.get("mode"), "judge_profile.mode"),
        "agent_name": _require_string(
            judge_profile_payload.get("agent_name"),
            "judge_profile.agent_name",
        ),
        "model_name": _require_optional_string(
            judge_profile_payload.get("model_name"),
            "judge_profile.model_name",
        ),
        "workflow_id": _require_string(
            judge_profile_payload.get("workflow_id"),
            "judge_profile.workflow_id",
        ),
        "trial_count": _require_int(
            judge_profile_payload.get("trial_count", DEFAULT_TRIAL_COUNT),
            "judge_profile.trial_count",
            minimum=1,
        ),
    }
    if judge_profile["trial_count"] != rubric["trial_count"]:
        raise EvaluationConfigurationError(
            "Judge profile trial_count does not match the rubric trial_count."
        )
    trials_payload = payload.get("trials_by_case")
    if not isinstance(trials_payload, dict):
        raise EvaluationConfigurationError("`trials_by_case` must be an object.")
    normalized_trials: dict[str, list[dict[str, Any]]] = {}
    case_lookup = {case["case_id"]: case for case in suite["cases"]}
    for case_id, trials in trials_payload.items():
        if case_id not in case_lookup:
            raise EvaluationConfigurationError(f"Judge trials reference unknown case `{case_id}`.")
        case = case_lookup[case_id]
        if not isinstance(trials, list):
            raise EvaluationConfigurationError(f"Trials for case `{case_id}` must be a list.")
        if case["active_rubric_dimensions"] and len(trials) != rubric["trial_count"]:
            raise EvaluationConfigurationError(
                f"Case `{case_id}` requires exactly {rubric['trial_count']} judge trials."
            )
        normalized_case_trials: list[dict[str, Any]] = []
        for index, trial in enumerate(trials, start=1):
            if not isinstance(trial, dict):
                raise EvaluationConfigurationError(
                    f"Case `{case_id}` trial `{index}` must be an object."
                )
            dimension_scores = trial.get("dimension_scores")
            if not isinstance(dimension_scores, dict):
                raise EvaluationConfigurationError(
                    f"Case `{case_id}` trial `{index}` must define `dimension_scores`."
                )
            normalized_scores: dict[str, int] = {}
            for dimension in case["active_rubric_dimensions"]:
                score = dimension_scores.get(dimension)
                if not isinstance(score, int) or score not in {0, 1, 2}:
                    raise EvaluationConfigurationError(
                        f"Case `{case_id}` trial `{index}` must score `{dimension}` "
                        "with 0, 1, or 2."
                    )
                normalized_scores[dimension] = score
            normalized_case_trials.append(
                {
                    "trial_id": _require_string(
                        trial.get("trial_id", f"trial-{index}"),
                        f"{case_id}.trial_id",
                    ),
                    "dimension_scores": normalized_scores,
                    "notes": _require_string(trial.get("notes"), f"{case_id}.notes"),
                    "feedback_tags": _require_string_list(
                        trial.get("feedback_tags", []),
                        f"{case_id}.feedback_tags",
                    ),
                }
            )
            unknown_feedback_tags = sorted(
                set(normalized_case_trials[-1]["feedback_tags"]) - set(FEEDBACK_TAXONOMY)
            )
            if unknown_feedback_tags:
                raise EvaluationConfigurationError(
                    f"Case `{case_id}` trial `{index}` references unknown feedback tags: "
                    + ", ".join(unknown_feedback_tags)
                )
        normalized_trials[case_id] = normalized_case_trials
    for case in suite["cases"]:
        if case["active_rubric_dimensions"] and case["case_id"] not in normalized_trials:
            raise EvaluationConfigurationError(
                f"Missing judge trials for rubric-scored case `{case['case_id']}`."
            )
    return {
        "schema_version": JUDGE_TRIALS_SCHEMA_VERSION,
        "suite_id": suite_id,
        "judge_profile": judge_profile,
        "trials_by_case": normalized_trials,
    }


def load_evaluation_baseline(path: Path) -> dict[str, Any]:
    """Load a frozen evaluation baseline when present."""
    payload = _load_required_payload(path, field_name="evaluation baseline")
    schema_version = payload.get("schema_version")
    if schema_version != BASELINE_SCHEMA_VERSION:
        raise EvaluationConfigurationError(
            f"`{path}` has unsupported baseline schema_version `{schema_version}`."
        )
    return payload


def _case_primary_source_ids(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    if case["execution_mode"] == "retrieve":
        return [
            item["source_id"]
            for item in result.get("results", [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
    if case["execution_mode"] in {"trace-answer", "ask-turn"}:
        return _deduplicate_strings(
            [item for item in result.get("supporting_source_ids", []) if isinstance(item, str)]
        )
    source = result.get("source", {})
    relations = source.get("relations", {})
    return [
        item["related_source_id"]
        for item in relations.get("outgoing", [])
        if isinstance(item, dict) and isinstance(item.get("related_source_id"), str)
    ]


def _case_source_ids(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    if case["execution_mode"] in {"retrieve", "trace-answer", "ask-turn"}:
        return _case_primary_source_ids(case, result)
    source = result.get("source", {})
    relations = source.get("relations", {})
    return _deduplicate_strings(
        _case_primary_source_ids(case, result)
        + [
            item["source_id"]
            for direction in ("incoming", "outgoing")
            for item in relations.get(direction, [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
    )


def _case_unit_ids(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    if case["execution_mode"] == "retrieve":
        unit_ids: list[str] = []
        for item in result.get("results", []):
            if not isinstance(item, dict) or not isinstance(item.get("source_id"), str):
                continue
            source_id = item["source_id"]
            for unit in item.get("matched_units", []):
                if isinstance(unit, dict) and isinstance(unit.get("unit_id"), str):
                    unit_ids.append(f"{source_id}:{unit['unit_id']}")
        return _deduplicate_strings(unit_ids)
    if case["execution_mode"] in {"trace-answer", "ask-turn"}:
        trace_unit_ids = [
            item for item in result.get("supporting_unit_ids", []) if isinstance(item, str)
        ]
        return _deduplicate_strings(trace_unit_ids)
    source = result.get("source", {})
    source_id = source.get("source_id")
    source_unit_ids: list[str] = []
    if isinstance(source_id, str):
        source_unit_ids.extend(
            f"{source_id}:{item}"
            for item in source.get("cited_unit_ids", [])
            if isinstance(item, str)
        )
    return _deduplicate_strings(source_unit_ids)


def _case_render_paths(case: dict[str, Any], result: dict[str, Any]) -> list[str]:
    if case["execution_mode"] == "retrieve":
        render_paths: list[str] = []
        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue
            render_paths.extend(
                reference
                for reference in item.get("render_references", [])
                if isinstance(reference, str)
            )
        return _deduplicate_strings(render_paths)
    if case["execution_mode"] in {"trace-answer", "ask-turn"}:
        trace_render_paths: list[str] = []
        for segment in result.get("segments", []):
            if not isinstance(segment, dict):
                continue
            for support in segment.get("supporting_units", []):
                if not isinstance(support, dict):
                    continue
                trace_render_paths.extend(
                    reference
                    for reference in support.get("render_references", [])
                    if isinstance(reference, str)
                )
        return _deduplicate_strings(trace_render_paths)
    source = result.get("source", {})
    return _deduplicate_strings(
        [item for item in source.get("render_paths", []) if isinstance(item, str)]
    )


@contextmanager
def _temporary_codex_thread(thread_ref: str) -> Iterator[None]:
    """Bind one stable temporary Codex thread id for ask-turn replay."""
    previous_thread_ref = os.environ.get("CODEX_THREAD_ID")
    os.environ["CODEX_THREAD_ID"] = thread_ref
    try:
        yield
    finally:
        if previous_thread_ref is None:
            os.environ.pop("CODEX_THREAD_ID", None)
        else:
            os.environ["CODEX_THREAD_ID"] = previous_thread_ref


def _ask_turn_thread_ref(case: dict[str, Any], *, run_scope_id: str) -> str:
    """Return the stable host thread ref for one ask-turn evaluation case."""
    ask_replay = case.get("ask_replay", {})
    explicit = ask_replay.get("host_thread_ref") if isinstance(ask_replay, dict) else None
    if isinstance(explicit, str) and explicit:
        return explicit
    return f"eval-ask-turn-{run_scope_id}-{case['case_id']}"


def _load_latest_runtime_payload(
    paths: WorkspacePaths,
    directory: Path,
    artifact_ids: list[str],
) -> dict[str, Any]:
    """Load the latest runtime payload for one ordered artifact-id list."""
    for artifact_id in reversed([item for item in artifact_ids if isinstance(item, str) and item]):
        payload = read_json(directory / f"{artifact_id}.json")
        if payload:
            return payload
    return {}


def _response_excerpt(text: str, *, limit: int = 200) -> str:
    """Return a compact single-line response excerpt."""
    cleaned = " ".join(text.strip().split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _write_answer_text(paths: WorkspacePaths, *, answer_file_path: str, answer_text: str) -> Path:
    """Write one canonical answer file for ask-turn replay."""
    answer_path = paths.root / answer_file_path
    answer_path.parent.mkdir(parents=True, exist_ok=True)
    answer_path.write_text(answer_text, encoding="utf-8")
    return answer_path


def _turn_is_settled(turn_payload: dict[str, Any]) -> bool:
    """Return whether one ask turn payload is already settled."""
    return str(turn_payload.get("status") or "") in {"answered", "completed"}


def _ask_turn_result_payload(paths: WorkspacePaths, *, turn: dict[str, Any]) -> dict[str, Any]:
    """Build one trace-shaped evaluation result from final canonical ask truth."""
    session_ids = [
        value for value in turn.get("session_ids", []) if isinstance(value, str) and value
    ]
    trace_ids = [
        value for value in turn.get("trace_ids", []) if isinstance(value, str) and value
    ]
    latest_trace = _load_latest_runtime_payload(paths, paths.retrieval_traces_dir, trace_ids)
    latest_session = _load_latest_runtime_payload(paths, paths.query_sessions_dir, session_ids)
    answer_state = (
        str(turn.get("answer_state"))
        if isinstance(turn.get("answer_state"), str) and turn.get("answer_state")
        else (
            str(latest_trace.get("answer_state"))
            if (
                isinstance(latest_trace.get("answer_state"), str)
                and latest_trace.get("answer_state")
            )
            else None
        )
    )
    support_basis = (
        str(turn.get("support_basis"))
        if isinstance(turn.get("support_basis"), str) and turn.get("support_basis")
        else (
            str(latest_trace.get("support_basis"))
            if (
                isinstance(latest_trace.get("support_basis"), str)
                and latest_trace.get("support_basis")
            )
            else None
        )
    )
    render_inspection_required = (
        bool(turn.get("render_inspection_required"))
        if isinstance(turn.get("render_inspection_required"), bool)
        else (
            bool(latest_trace.get("render_inspection_required"))
            if isinstance(latest_trace.get("render_inspection_required"), bool)
            else None
        )
    )
    status = latest_trace.get("status") or latest_session.get("status")
    if isinstance(answer_state, str):
        from .retrieval import combined_trace_status

        effective_status = combined_trace_status(
            answer_state=answer_state,
            support_basis=support_basis,
            support_manifest_path=(
                str(turn.get("support_manifest_path"))
                if isinstance(turn.get("support_manifest_path"), str)
                else None
            ),
        )
        latest_trace_answer_state = (
            str(latest_trace.get("answer_state"))
            if (
                isinstance(latest_trace.get("answer_state"), str)
                and latest_trace.get("answer_state")
            )
            else None
        )
        latest_trace_support_basis = (
            str(latest_trace.get("support_basis"))
            if (
                isinstance(latest_trace.get("support_basis"), str)
                and latest_trace.get("support_basis")
            )
            else None
        )
        if (
            not isinstance(status, str)
            or not status
            or latest_trace_answer_state != answer_state
            or latest_trace_support_basis != support_basis
        ):
            status = effective_status
    elif not isinstance(status, str) or not status:
        status = "degraded"

    result = dict(latest_trace or latest_session or {})
    result.update(
        {
            "status": status,
            "answer_state": answer_state,
            "support_basis": support_basis,
            "render_inspection_required": render_inspection_required,
            "conversation_id": turn.get("conversation_id"),
            "turn_id": turn.get("turn_id"),
            "run_id": turn.get("active_run_id") or turn.get("committed_run_id"),
            "turn_status": turn.get("status"),
            "front_door_state": turn.get("front_door_state"),
            "session_ids": session_ids,
            "trace_ids": trace_ids,
            "auto_prepare_triggered": bool(turn.get("auto_prepare_triggered")),
            "auto_sync_triggered": bool(turn.get("auto_sync_triggered")),
            "hybrid_refresh_triggered": bool(turn.get("hybrid_refresh_triggered")),
            "hybrid_refresh_completion_status": turn.get("hybrid_refresh_completion_status"),
            "answer_file_path": turn.get("answer_file_path"),
        }
    )
    return result


def _ask_turn_artifact_paths(paths: WorkspacePaths, *, turn: dict[str, Any]) -> dict[str, str]:
    """Collect the runtime artifact chain produced by one ask-turn replay."""
    artifacts: dict[str, str] = {}
    conversation_id = (
        str(turn.get("conversation_id"))
        if isinstance(turn.get("conversation_id"), str) and turn.get("conversation_id")
        else None
    )
    run_id = (
        str(turn.get("active_run_id") or turn.get("committed_run_id"))
        if isinstance(turn.get("active_run_id") or turn.get("committed_run_id"), str)
        and (turn.get("active_run_id") or turn.get("committed_run_id"))
        else None
    )
    if conversation_id:
        conversation_path = paths.conversations_dir / f"{conversation_id}.json"
        if conversation_path.exists():
            artifacts["canonical_conversation"] = str(conversation_path.relative_to(paths.root))
        projection_path = paths.conversation_projections_dir / f"{conversation_id}.json"
        if projection_path.exists():
            artifacts["conversation_projection"] = str(projection_path.relative_to(paths.root))
    if run_id:
        from .run_control import run_commit_path, run_journal_path, run_state_path

        state_path = run_state_path(paths, run_id)
        journal_path = run_journal_path(paths, run_id)
        commit_path = run_commit_path(paths, run_id)
        if state_path.exists():
            artifacts["run_state"] = str(state_path.relative_to(paths.root))
        if journal_path.exists():
            artifacts["run_journal"] = str(journal_path.relative_to(paths.root))
        if commit_path.exists():
            artifacts["run_commit"] = str(commit_path.relative_to(paths.root))
    answer_file_path = (
        str(turn.get("answer_file_path"))
        if isinstance(turn.get("answer_file_path"), str) and turn.get("answer_file_path")
        else None
    )
    if answer_file_path and (paths.root / answer_file_path).exists():
        artifacts["answer_file"] = answer_file_path
    for index, session_id in enumerate(
        [value for value in turn.get("session_ids", []) if isinstance(value, str) and value],
        start=1,
    ):
        session_path = paths.query_sessions_dir / f"{session_id}.json"
        if session_path.exists():
            artifacts[f"query_session_{index:02d}"] = str(session_path.relative_to(paths.root))
    for index, trace_id in enumerate(
        [value for value in turn.get("trace_ids", []) if isinstance(value, str) and value],
        start=1,
    ):
        trace_path = paths.retrieval_traces_dir / f"{trace_id}.json"
        if trace_path.exists():
            artifacts[f"retrieval_trace_{index:02d}"] = str(trace_path.relative_to(paths.root))
    for key, path in (
        ("review_summary", paths.review_summary_path),
        ("benchmark_candidates", paths.benchmark_candidates_path),
        ("answer_history_index", paths.answer_history_index_path),
        ("projection_state", paths.projection_state_path),
    ):
        if path.exists():
            artifacts[key] = str(path.relative_to(paths.root))
    raw_hybrid_refresh_summary = turn.get("hybrid_refresh_summary")
    hybrid_refresh_summary: dict[str, Any] = (
        dict(raw_hybrid_refresh_summary)
        if isinstance(raw_hybrid_refresh_summary, dict)
        else {}
    )
    work_path = hybrid_refresh_summary.get("work_path")
    if isinstance(work_path, str) and work_path and (paths.root / work_path).exists():
        artifacts["hybrid_refresh_work"] = work_path
    shared_job_ids = _deduplicate_strings(
        [
            *[
                value
                for value in turn.get("attached_shared_job_ids", [])
                if isinstance(value, str) and value
            ],
            *[
                value
                for value in turn.get("hybrid_refresh_job_ids", [])
                if isinstance(value, str) and value
            ],
        ]
    )
    for index, job_id in enumerate(shared_job_ids, start=1):
        base_dir = paths.shared_jobs_dir / job_id
        for suffix, filename in (
            ("manifest", "manifest.json"),
            ("journal", "journal.jsonl"),
            ("result", "result.json"),
        ):
            candidate = base_dir / filename
            if candidate.exists():
                artifacts[f"shared_job_{index:02d}_{suffix}"] = str(
                    candidate.relative_to(paths.root)
                )
    return artifacts


def _run_event_types(paths: WorkspacePaths, run_id: str | None) -> list[str]:
    """Return the ordered event types recorded in one run journal."""
    return [
        event_type
        for payload in _run_journal_entries(paths, run_id)
        if isinstance((event_type := payload.get("event_type")), str) and event_type
    ]


def _run_journal_entries(paths: WorkspacePaths, run_id: str | None) -> list[dict[str, Any]]:
    """Return the ordered run-journal entries for one run id."""
    if not isinstance(run_id, str) or not run_id:
        return []
    from .run_control import run_journal_path

    journal_path = run_journal_path(paths, run_id)
    if not journal_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            entries.append(payload)
    return entries


def _ordered_event_subsequence(expected_events: list[str], actual_events: list[str]) -> list[str]:
    """Return the ordered subsequence of expected events found in the actual journal."""
    matched: list[str] = []
    actual_index = 0
    for expected_event in expected_events:
        while actual_index < len(actual_events):
            if actual_events[actual_index] == expected_event:
                matched.append(expected_event)
                actual_index += 1
                break
            actual_index += 1
    return matched


def _ask_turn_structural_checks(
    paths: WorkspacePaths,
    *,
    execution: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return non-optional closure checks for ask-turn replay truth."""
    from .control_plane import (
        load_shared_job,
        resolved_attached_shared_job_ids,
        shared_job_is_settled,
    )
    from .run_control import load_run_state

    turn_record = execution.get("turn_record")
    if not isinstance(turn_record, dict):
        turn_record = {}
    run_id = (
        execution.get("run_id")
        if isinstance(execution.get("run_id"), str)
        else (
            turn_record.get("active_run_id") or turn_record.get("committed_run_id")
            if isinstance(
                turn_record.get("active_run_id") or turn_record.get("committed_run_id"),
                str,
            )
            else None
        )
    )
    journal_entries = _run_journal_entries(paths, run_id)
    commit_index: int | None = None
    waiting_job_ids: list[tuple[int, str]] = []
    for index, payload in enumerate(journal_entries):
        event_type = payload.get("event_type")
        if commit_index is None and event_type == "turn-committed":
            commit_index = index
        if event_type != "shared-job-waiting":
            continue
        event_payload = payload.get("payload")
        job_id = (
            event_payload.get("job_id")
            if isinstance(event_payload, dict) and isinstance(event_payload.get("job_id"), str)
            else None
        )
        if isinstance(job_id, str) and job_id:
            waiting_job_ids.append((index, job_id))
    waiting_jobs_missing_settlement: list[str] = []
    if commit_index is not None:
        for waiting_index, job_id in waiting_job_ids:
            found_settlement = False
            for payload in journal_entries[waiting_index + 1 : commit_index]:
                if payload.get("event_type") != "shared-job-settled":
                    continue
                event_payload = payload.get("payload")
                settled_job_id = (
                    event_payload.get("job_id")
                    if isinstance(event_payload, dict)
                    and isinstance(event_payload.get("job_id"), str)
                    else None
                )
                if settled_job_id == job_id:
                    found_settlement = True
                    break
            if not found_settlement:
                waiting_jobs_missing_settlement.append(job_id)
    run_state = load_run_state(paths, run_id) if isinstance(run_id, str) and run_id else {}
    attached_job_ids = (
        resolved_attached_shared_job_ids(turn=turn_record, run_state=run_state)
        if isinstance(run_id, str) and run_id
        else []
    )
    unresolved_answer_critical_jobs: list[str] = []
    if commit_index is not None:
        for job_id in attached_job_ids:
            manifest = load_shared_job(paths, job_id)
            if not manifest:
                unresolved_answer_critical_jobs.append(job_id)
                continue
            if manifest.get("criticality") != "answer-critical":
                continue
            if not shared_job_is_settled(manifest):
                unresolved_answer_critical_jobs.append(job_id)
    return [
        {
            "name": "shared_job_wait_closure",
            "expected": [],
            "actual": waiting_jobs_missing_settlement,
            "passed": not waiting_jobs_missing_settlement,
        },
        {
            "name": "answer_critical_shared_jobs_settled",
            "expected": [],
            "actual": unresolved_answer_critical_jobs,
            "passed": not unresolved_answer_critical_jobs,
        },
    ]


def _execute_ask_turn_case(
    paths: WorkspacePaths,
    case: dict[str, Any],
    *,
    run_scope_id: str,
    answer_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Replay one manual ask-path case through the canonical ask front door."""
    from .ask import complete_ask_turn, prepare_ask_turn, settle_lane_c_shared_refresh
    from .conversation import load_turn_record
    from .retrieval import trace_answer_file

    ask_replay = dict(case.get("ask_replay") or {})
    thread_ref = _ask_turn_thread_ref(case, run_scope_id=run_scope_id)
    initial_response: dict[str, Any] | None = None
    current_response: dict[str, Any] | None = None
    reused_turn = False
    answer_override = answer_overrides.get(case["case_id"]) if answer_overrides else None

    with _temporary_codex_thread(thread_ref):
        initial_response = prepare_ask_turn(
            paths,
            question=case["query_or_prompt"],
            semantic_analysis=dict(ask_replay["semantic_analysis"]),
            log_origin=LOG_ORIGIN_EVALUATION_SUITE,
        )
        current_response = initial_response
        initial_turn_key = (
            str(initial_response.get("conversation_id") or ""),
            str(initial_response.get("turn_id") or ""),
        )
        for continuation in ask_replay.get("continuations", []):
            current_response = prepare_ask_turn(
                paths,
                question=str(continuation["message"]),
                semantic_analysis=(
                    dict(continuation["semantic_analysis"])
                    if isinstance(continuation.get("semantic_analysis"), dict)
                    else None
                ),
                log_origin=LOG_ORIGIN_EVALUATION_SUITE,
            )
            current_turn_key = (
                str(current_response.get("conversation_id") or ""),
                str(current_response.get("turn_id") or ""),
            )
            reused_turn = reused_turn or current_turn_key == initial_turn_key

        assert current_response is not None
        conversation_id = _require_string(
            current_response.get("conversation_id"),
            "conversation_id",
        )
        turn_id = _require_string(current_response.get("turn_id"), "turn_id")
        current_turn = {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            **load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id),
        }
        if _turn_is_settled(current_turn):
            final_turn = current_turn
        else:
            answer_plan = ask_replay.get("answer_plan")
            current_turn_status = str(current_turn.get("status") or "")
            if current_turn_status == "awaiting-confirmation":
                raise EvaluationConfigurationError(
                    f"Ask-turn case `{case['case_id']}` is still awaiting confirmation. "
                    "Add an explicit continuation such as `yes` or `no` before answer completion."
                )
            if not isinstance(answer_plan, dict):
                raise EvaluationConfigurationError(
                    f"Ask-turn case `{case['case_id']}` requires `ask_replay.answer_plan` because "
                    "the replay path did not settle before canonical answer completion."
                )
            completion_overrides = dict(answer_plan.get("completion_overrides") or {})
            answer_text = str(answer_override or answer_plan["answer_text"])
            answer_file_path = _require_string(
                current_turn.get("answer_file_path"),
                f"{case['case_id']}.answer_file_path",
            )
            answer_file = _write_answer_text(
                paths,
                answer_file_path=answer_file_path,
                answer_text=answer_text,
            )
            raw_log_context = current_response.get("log_context")
            log_context = (
                {
                    str(key): str(value)
                    for key, value in raw_log_context.items()
                    if isinstance(key, str) and key and isinstance(value, str) and value
                }
                if isinstance(raw_log_context, dict)
                else None
            )
            trace = trace_answer_file(
                paths,
                answer_file=answer_file,
                top=int(answer_plan["trace_top"]),
                log_context=log_context,
                log_origin=LOG_ORIGIN_EVALUATION_SUITE,
            )
            completed = complete_ask_turn(
                paths,
                conversation_id=conversation_id,
                turn_id=turn_id,
                inner_workflow_id=str(
                    completion_overrides.get("inner_workflow_id")
                    or current_response.get("inner_workflow_id")
                    or "grounded-answer"
                ),
                session_ids=[str(trace["session_id"])],
                trace_ids=[str(trace["trace_id"])],
                answer_file_path=answer_file_path,
                response_excerpt=_response_excerpt(answer_text),
                status=str(completion_overrides.get("status") or "answered"),
                support_basis=(
                    str(completion_overrides["support_basis"])
                    if isinstance(completion_overrides.get("support_basis"), str)
                    else None
                ),
                support_manifest_path=(
                    str(completion_overrides["support_manifest_path"])
                    if isinstance(completion_overrides.get("support_manifest_path"), str)
                    else None
                ),
                log_origin=LOG_ORIGIN_EVALUATION_SUITE,
            )
            final_turn = {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                **load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id),
            }
            hybrid_refresh = ask_replay.get("hybrid_refresh")
            if str(completed.get("status") or "") == "waiting-shared-job":
                if not isinstance(hybrid_refresh, dict):
                    raise EvaluationConfigurationError(
                        f"Ask-turn case `{case['case_id']}` entered governed multimodal refresh "
                        "but does not define `ask_replay.hybrid_refresh`."
                    )
                completed_hybrid_summary = completed.get("hybrid_refresh_summary")
                hybrid_summary: dict[str, Any] = (
                    dict(completed_hybrid_summary)
                    if isinstance(completed_hybrid_summary, dict)
                    else {}
                )
                hybrid_summary.update(dict(hybrid_refresh.get("summary") or {}))
                hybrid_job_ids = [
                    value
                    for value in completed.get("hybrid_refresh_job_ids", [])
                    if isinstance(value, str) and value
                ]
                if not hybrid_job_ids:
                    raise EvaluationConfigurationError(
                        f"Ask-turn case `{case['case_id']}` entered governed "
                        "multimodal refresh without a shared job id."
                    )
                settle_lane_c_shared_refresh(
                    paths,
                    conversation_id=conversation_id,
                    turn_id=turn_id,
                    job_id=hybrid_job_ids[0],
                    completion_status=str(hybrid_refresh["completion_status"]),
                    summary=hybrid_summary,
                )
                final_turn = {
                    "conversation_id": conversation_id,
                    "turn_id": turn_id,
                    **load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id),
                }
                if str(hybrid_refresh["completion_status"]) == "covered":
                    post_refresh_answer_text = str(
                        answer_override or hybrid_refresh["post_refresh_answer_text"]
                    )
                    post_refresh_trace_top = int(hybrid_refresh["post_refresh_trace_top"])
                    post_refresh_answer_file_path = _require_string(
                        final_turn.get("answer_file_path"),
                        f"{case['case_id']}.post_refresh_answer_file_path",
                    )
                    post_refresh_answer_file = _write_answer_text(
                        paths,
                        answer_file_path=post_refresh_answer_file_path,
                        answer_text=post_refresh_answer_text,
                    )
                    post_refresh_trace = trace_answer_file(
                        paths,
                        answer_file=post_refresh_answer_file,
                        top=post_refresh_trace_top,
                        log_context=log_context,
                        log_origin=LOG_ORIGIN_EVALUATION_SUITE,
                    )
                    all_session_ids = _deduplicate_strings(
                        [
                            *[
                                value
                                for value in final_turn.get("session_ids", [])
                                if isinstance(value, str) and value
                            ],
                            str(post_refresh_trace["session_id"]),
                        ]
                    )
                    all_trace_ids = _deduplicate_strings(
                        [
                            *[
                                value
                                for value in final_turn.get("trace_ids", [])
                                if isinstance(value, str) and value
                            ],
                            str(post_refresh_trace["trace_id"]),
                        ]
                    )
                    complete_ask_turn(
                        paths,
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        inner_workflow_id=str(
                            completion_overrides.get("inner_workflow_id")
                            or current_response.get("inner_workflow_id")
                            or "grounded-answer"
                        ),
                        session_ids=all_session_ids,
                        trace_ids=all_trace_ids,
                        answer_file_path=post_refresh_answer_file_path,
                        response_excerpt=_response_excerpt(post_refresh_answer_text),
                        status=str(completion_overrides.get("status") or "answered"),
                        support_basis=(
                            str(completion_overrides["support_basis"])
                            if isinstance(completion_overrides.get("support_basis"), str)
                            else None
                        ),
                        support_manifest_path=(
                            str(completion_overrides["support_manifest_path"])
                            if isinstance(completion_overrides.get("support_manifest_path"), str)
                            else None
                        ),
                        log_origin=LOG_ORIGIN_EVALUATION_SUITE,
                    )
                    final_turn = {
                        "conversation_id": conversation_id,
                        "turn_id": turn_id,
                        **load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id),
                    }

    final_answer_file_path: str | None = (
        str(final_turn.get("answer_file_path"))
        if (
            isinstance(final_turn.get("answer_file_path"), str)
            and final_turn.get("answer_file_path")
        )
        else None
    )
    final_answer_text: str | None = None
    if final_answer_file_path and (paths.root / final_answer_file_path).exists():
        final_answer_text = (paths.root / final_answer_file_path).read_text(encoding="utf-8")
    run_id = (
        str(final_turn.get("active_run_id") or final_turn.get("committed_run_id"))
        if isinstance(final_turn.get("active_run_id") or final_turn.get("committed_run_id"), str)
        and (final_turn.get("active_run_id") or final_turn.get("committed_run_id"))
        else None
    )
    session_ids = [
        value for value in final_turn.get("session_ids", []) if isinstance(value, str) and value
    ]
    trace_ids = [
        value for value in final_turn.get("trace_ids", []) if isinstance(value, str) and value
    ]
    return {
        "execution_mode": case["execution_mode"],
        "result": _ask_turn_result_payload(paths, turn=final_turn),
        "session_id": session_ids[-1] if session_ids else None,
        "trace_id": trace_ids[-1] if trace_ids else None,
        "answer_text": final_answer_text,
        "answer_file_path": final_answer_file_path,
        "turn_record": final_turn,
        "reused_turn": reused_turn,
        "run_id": run_id,
        "run_event_types": _run_event_types(paths, run_id),
        "runtime_artifact_paths": _ask_turn_artifact_paths(paths, turn=final_turn),
    }


def _execute_case(
    paths: WorkspacePaths,
    case: dict[str, Any],
    *,
    answer_overrides: dict[str, str] | None = None,
    run_scope_id: str,
) -> dict[str, Any]:
    target = case.get("target", "current")
    if case["execution_mode"] == "ask-turn":
        return _execute_ask_turn_case(
            paths,
            case,
            run_scope_id=run_scope_id,
            answer_overrides=answer_overrides,
        )
    log_context: dict[str, str] | None = None
    if case.get("execution_inner_workflow_id") or case.get("execution_support_basis"):
        log_context = {
            "entry_workflow_id": "operator-eval",
            "inner_workflow_id": str(case.get("execution_inner_workflow_id") or "grounded-answer"),
        }
        if isinstance(case.get("execution_support_basis"), str):
            log_context["support_basis"] = case["execution_support_basis"]
    if case["execution_mode"] == "retrieve":
        payload = retrieve_corpus(
            paths,
            query=case["query_or_prompt"],
            top=case["top"],
            graph_hops=case["graph_hops"],
            document_types=None,
            source_ids=None,
            include_renders=case["include_renders"],
            target=target,
            write_logs=True,
            log_origin="evaluation-suite",
            log_context=log_context,
        )
        return {
            "execution_mode": case["execution_mode"],
            "result": payload,
            "session_id": payload.get("session_id"),
            "trace_id": None,
            "answer_text": None,
            "answer_file_path": None,
        }
    if case["execution_mode"] == "trace-source":
        payload = trace_source(
            paths,
            source_id=case["query_or_prompt"],
            unit_id=case.get("unit_id"),
            target=target,
            log_context=log_context,
            log_origin="evaluation-suite",
        )
        return {
            "execution_mode": case["execution_mode"],
            "result": payload,
            "session_id": None,
            "trace_id": payload.get("trace_id"),
            "answer_text": None,
            "answer_file_path": None,
        }
    answer_text = (
        answer_overrides.get(case["case_id"], case["query_or_prompt"])
        if answer_overrides
        else case["query_or_prompt"]
    )
    payload = trace_answer_text(
        paths,
        answer_text=answer_text,
        top=case["top"],
        target=target,
        log_origin="evaluation-suite",
        log_context=log_context,
        support_basis=case.get("execution_support_basis"),
        support_manifest_path=case.get("execution_support_manifest_path"),
        declared_answer_state=case.get("declared_answer_state"),
    )
    return {
        "execution_mode": case["execution_mode"],
        "result": payload,
        "session_id": payload.get("session_id"),
        "trace_id": payload.get("trace_id"),
        "answer_text": answer_text,
        "answer_file_path": payload.get("answer_file_path"),
    }


def _execution_artifacts(paths: WorkspacePaths, execution: dict[str, Any]) -> dict[str, str]:
    """Return relative runtime artifact paths for one evaluation execution."""
    artifacts: dict[str, str] = {}
    session_id = execution.get("session_id")
    trace_id = execution.get("trace_id")
    answer_file_path = execution.get("answer_file_path")
    if isinstance(session_id, str) and session_id:
        artifacts["query_session"] = str(
            (paths.query_sessions_dir / f"{session_id}.json").relative_to(paths.root)
        )
    if isinstance(trace_id, str) and trace_id:
        artifacts["retrieval_trace"] = str(
            (paths.retrieval_traces_dir / f"{trace_id}.json").relative_to(paths.root)
        )
    if isinstance(answer_file_path, str) and answer_file_path:
        artifacts["answer_file"] = answer_file_path
    runtime_artifact_paths = execution.get("runtime_artifact_paths")
    if isinstance(runtime_artifact_paths, dict):
        for key, value in runtime_artifact_paths.items():
            if isinstance(key, str) and key and isinstance(value, str) and value:
                artifacts[key] = value
    return artifacts


def _case_deterministic_checks(
    paths: WorkspacePaths,
    case: dict[str, Any],
    execution: dict[str, Any],
) -> list[dict[str, Any]]:
    result = execution["result"]
    actual_status = result.get("status")
    actual_answer_state = result.get("answer_state")
    actual_render_required = result.get("render_inspection_required")
    primary_source_ids = _case_primary_source_ids(case, result)
    source_ids = _case_source_ids(case, result)
    unit_ids = _case_unit_ids(case, result)
    available_identifiers = set(source_ids) | set(unit_ids)
    checks: list[dict[str, Any]] = [
        {
            "name": "status",
            "expected": case["expected_status"],
            "actual": actual_status,
            "passed": actual_status == case["expected_status"],
        }
    ]
    if case.get("expected_support_basis") is not None:
        checks.append(
            {
                "name": "support_basis",
                "expected": case["expected_support_basis"],
                "actual": result.get("support_basis"),
                "passed": result.get("support_basis") == case["expected_support_basis"],
            }
        )
    if case["expected_answer_state"] is not None:
        checks.append(
            {
                "name": "answer_state",
                "expected": case["expected_answer_state"],
                "actual": actual_answer_state,
                "passed": actual_answer_state == case["expected_answer_state"],
            }
        )
    if case["expected_render_inspection_required"] is not None:
        checks.append(
            {
                "name": "render_inspection_required",
                "expected": case["expected_render_inspection_required"],
                "actual": actual_render_required,
                "passed": actual_render_required == case["expected_render_inspection_required"],
            }
        )
    if case["expected_primary_sources"]:
        expected_prefix = case["expected_primary_sources"]
        actual_prefix = primary_source_ids[: len(expected_prefix)]
        checks.append(
            {
                "name": "primary_sources",
                "expected": expected_prefix,
                "actual": actual_prefix,
                "passed": actual_prefix == expected_prefix,
            }
        )
    if case["required_sources_or_units"]:
        overlap = sorted(
            identifier
            for identifier in case["required_sources_or_units"]
            if identifier in available_identifiers
        )
        checks.append(
            {
                "name": "required_support_overlap",
                "expected": case["minimum_support_overlap"],
                "actual": len(overlap),
                "matched_identifiers": overlap,
                "passed": len(overlap) >= case["minimum_support_overlap"],
            }
        )
    if case["forbidden_sources_or_units"]:
        forbidden_hits = sorted(
            identifier
            for identifier in case["forbidden_sources_or_units"]
            if identifier in available_identifiers
        )
        checks.append(
            {
                "name": "forbidden_sources_or_units",
                "expected": [],
                "actual": forbidden_hits,
                "passed": not forbidden_hits,
            }
        )
    if case["execution_mode"] == "ask-turn":
        ask_replay = case.get("ask_replay", {})
        expectations_payload = (
            ask_replay.get("expectations")
            if isinstance(ask_replay, dict)
            else None
        )
        expectations: dict[str, Any] = (
            dict(expectations_payload)
            if isinstance(expectations_payload, dict)
            else {}
        )
        turn_record = execution.get("turn_record")
        if not isinstance(turn_record, dict):
            turn_record = {}
        if "final_turn_status" in expectations:
            checks.append(
                {
                    "name": "final_turn_status",
                    "expected": expectations["final_turn_status"],
                    "actual": turn_record.get("status"),
                    "passed": turn_record.get("status") == expectations["final_turn_status"],
                }
            )
        for field_name in (
            "reused_turn",
            "auto_prepare_triggered",
            "auto_sync_triggered",
            "hybrid_refresh_triggered",
        ):
            if field_name in expectations:
                if field_name == "reused_turn":
                    actual = (
                        execution.get(field_name)
                        if isinstance(execution.get(field_name), bool)
                        else None
                    )
                else:
                    actual = (
                        result.get(field_name)
                        if isinstance(result.get(field_name), bool)
                        else None
                    )
                checks.append(
                    {
                        "name": field_name,
                        "expected": expectations[field_name],
                        "actual": actual,
                        "passed": actual == expectations[field_name],
                    }
                )
        if "hybrid_refresh_completion_status" in expectations:
            checks.append(
                {
                    "name": "hybrid_refresh_completion_status",
                    "expected": expectations["hybrid_refresh_completion_status"],
                    "actual": result.get("hybrid_refresh_completion_status"),
                    "passed": result.get("hybrid_refresh_completion_status")
                    == expectations["hybrid_refresh_completion_status"],
                }
            )
        if "query_session_count" in expectations:
            actual_query_session_count = len(
                [
                    value
                    for value in result.get("session_ids", [])
                    if isinstance(value, str) and value
                ]
            )
            checks.append(
                {
                    "name": "query_session_count",
                    "expected": expectations["query_session_count"],
                    "actual": actual_query_session_count,
                    "passed": actual_query_session_count == expectations["query_session_count"],
                }
            )
        if "trace_count" in expectations:
            actual_trace_count = len(
                [value for value in result.get("trace_ids", []) if isinstance(value, str) and value]
            )
            checks.append(
                {
                    "name": "trace_count",
                    "expected": expectations["trace_count"],
                    "actual": actual_trace_count,
                    "passed": actual_trace_count == expectations["trace_count"],
                }
            )
        if "required_run_events" in expectations:
            actual_events = execution.get("run_event_types", [])
            if not isinstance(actual_events, list):
                actual_events = []
            matched_events = _ordered_event_subsequence(
                expectations["required_run_events"],
                actual_events,
            )
            checks.append(
                {
                    "name": "required_run_events",
                    "expected": expectations["required_run_events"],
                    "actual": matched_events,
                    "passed": len(matched_events) == len(expectations["required_run_events"]),
                }
            )
        checks.extend(_ask_turn_structural_checks(paths, execution=execution))
    return checks


def aggregate_case_rubric(
    case: dict[str, Any],
    *,
    rubric: dict[str, Any],
    judge_trials: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Aggregate judge trials into a stable rubric score for one case."""
    active_dimensions = case["active_rubric_dimensions"]
    if not active_dimensions:
        return None
    trials = judge_trials.get(case["case_id"], [])
    if len(trials) != rubric["trial_count"]:
        raise EvaluationConfigurationError(
            f"Case `{case['case_id']}` requires exactly {rubric['trial_count']} judge trials."
        )
    dimension_values: dict[str, list[int]] = {}
    dimension_spread: dict[str, int] = {}
    dimension_scores: dict[str, int] = {}
    for dimension in active_dimensions:
        values = [trial["dimension_scores"][dimension] for trial in trials]
        dimension_values[dimension] = values
        dimension_spread[dimension] = max(values) - min(values)
        dimension_scores[dimension] = int(statistics.median(values))
    mean_score = round(
        sum(dimension_scores.values()) / len(active_dimensions),
        3,
    )
    review_recommended = any(spread > 1 for spread in dimension_spread.values())
    return {
        "active_dimensions": active_dimensions,
        "trial_count": len(trials),
        "dimension_trial_values": dimension_values,
        "dimension_spread": dimension_spread,
        "dimension_scores": dimension_scores,
        "mean_score": mean_score,
        "review_recommended": review_recommended,
        "trial_notes": [trial["notes"] for trial in trials],
        "trial_feedback_tags": [
            sorted(
                {
                    tag
                    for trial in trials
                    for tag in trial.get("feedback_tags", [])
                    if isinstance(tag, str)
                }
            )
        ][0],
    }


def _build_judge_packet(
    case: dict[str, Any],
    execution: dict[str, Any],
    *,
    rubric: dict[str, Any],
) -> dict[str, Any] | None:
    if not case["active_rubric_dimensions"]:
        return None
    result = execution["result"]
    return {
        "case_id": case["case_id"],
        "family": case["family"],
        "execution_mode": case["execution_mode"],
        "answer_text": execution.get("answer_text"),
        "reference_facts": case["reference_facts"],
        "active_rubric_dimensions": case["active_rubric_dimensions"],
        "feedback_tags": case["feedback_tags"],
        "judge_instructions": rubric["judge_instructions"],
        "actual_status": result.get("status"),
        "actual_answer_state": result.get("answer_state"),
        "actual_support_basis": result.get("support_basis"),
        "actual_render_inspection_required": result.get("render_inspection_required"),
        "actual_primary_source_ids": _case_primary_source_ids(case, result),
        "actual_source_ids": _case_source_ids(case, result),
        "actual_unit_ids": _case_unit_ids(case, result),
        "actual_render_paths": _case_render_paths(case, result),
    }


def _artifact_fingerprints(paths: WorkspacePaths) -> dict[str, str | None]:
    relevant_paths = [
        paths.root / "src" / "docmason" / "ask.py",
        paths.root / "src" / "docmason" / "run_control.py",
        paths.root / "src" / "docmason" / "control_plane.py",
        paths.root / "src" / "docmason" / "admissibility.py",
        paths.root / "src" / "docmason" / "projections.py",
        paths.root / "src" / "docmason" / "review.py",
        paths.root / "src" / "docmason" / "conversation.py",
        paths.root / "src" / "docmason" / "retrieval.py",
        paths.root / "src" / "docmason" / "evaluation.py",
        paths.root / "src" / "docmason" / "operator_eval.py",
        paths.root / "skills" / "canonical" / "ask" / "SKILL.md",
        paths.root / "skills" / "canonical" / "ask" / "workflow.json",
        paths.root / "skills" / "canonical" / "retrieval-workflow" / "SKILL.md",
        paths.root / "skills" / "canonical" / "retrieval-workflow" / "workflow.json",
        paths.root / "skills" / "canonical" / "provenance-trace" / "SKILL.md",
        paths.root / "skills" / "canonical" / "provenance-trace" / "workflow.json",
        paths.root / "skills" / "canonical" / "grounded-answer" / "SKILL.md",
        paths.root / "skills" / "canonical" / "grounded-answer" / "workflow.json",
        paths.root / "skills" / "canonical" / "grounded-composition" / "SKILL.md",
        paths.root / "skills" / "canonical" / "grounded-composition" / "workflow.json",
        paths.root / "skills" / "operator" / "operator-eval" / "workflow.json",
    ]
    return {str(path.relative_to(paths.root)): _sha256_file(path) for path in relevant_paths}


def build_version_context(
    paths: WorkspacePaths,
    *,
    suite_path: Path,
    rubric_path: Path,
    judge_trials_path: Path | None,
    baseline_path: Path | None,
    judge_profile: dict[str, Any] | None,
    suite: dict[str, Any],
) -> dict[str, Any]:
    """Capture the stable version and fingerprint context for an evaluation run."""
    retrieval_manifest = read_json(paths.retrieval_manifest_path("current"))
    corpus_signature = retrieval_manifest.get("source_signature")
    if not isinstance(corpus_signature, str) or not corpus_signature:
        raise EvaluationConfigurationError(
            "Current retrieval artifacts are missing a source_signature. Rerun `docmason sync`."
        )
    suite_fingerprint = _sha256_file(suite_path)
    rubric_fingerprint = _sha256_file(rubric_path)
    judge_trials_fingerprint = _sha256_file(judge_trials_path) if judge_trials_path else None
    baseline_fingerprint = _sha256_file(baseline_path) if baseline_path else None
    return {
        "captured_at": utc_now(),
        "corpus_signature": corpus_signature,
        "suite_id": suite["suite_id"],
        "suite_fingerprint": suite_fingerprint,
        "rubric_fingerprint": rubric_fingerprint,
        "judge_trials_fingerprint": judge_trials_fingerprint,
        "baseline_fingerprint": baseline_fingerprint,
        "retrieval_strategy_id": RETRIEVAL_STRATEGY_ID,
        "answer_workflow_id": ANSWER_WORKFLOW_ID,
        "canonical_artifact_fingerprints": _artifact_fingerprints(paths),
        "judge_profile": judge_profile,
    }


def _case_outcome(
    *,
    deterministic_passed: bool,
    rubric_result: dict[str, Any] | None,
) -> str:
    if not deterministic_passed:
        return "failed"
    if rubric_result and rubric_result["review_recommended"]:
        return "review-recommended"
    return "passed"


def _summarize_cases(
    cases: list[dict[str, Any]],
    *,
    acceptance_thresholds: dict[str, float],
) -> dict[str, Any]:
    deterministic_passed = sum(1 for case in cases if case["deterministic_passed"])
    rubric_means = [
        float(case["rubric"]["mean_score"])
        for case in cases
        if isinstance(case.get("rubric"), dict)
    ]
    answer_mean_score = round(sum(rubric_means) / len(rubric_means), 3) if rubric_means else None
    failed_cases = [case["case_id"] for case in cases if case["outcome"] == "failed"]
    review_cases = [case["case_id"] for case in cases if case["outcome"] == "review-recommended"]
    deterministic_pass_rate = round(deterministic_passed / len(cases), 3)
    overall_status = "passed"
    if deterministic_pass_rate < acceptance_thresholds["deterministic_pass_rate"]:
        overall_status = "failed"
    elif (
        answer_mean_score is not None
        and answer_mean_score < acceptance_thresholds["answer_mean_score"]
    ):
        overall_status = "failed"
    elif review_cases:
        overall_status = "degraded"
    return {
        "case_count": len(cases),
        "deterministic_pass_rate": deterministic_pass_rate,
        "answer_mean_score": answer_mean_score,
        "failed_cases": failed_cases,
        "review_recommended_cases": review_cases,
        "critical_case_ids": [case["case_id"] for case in cases if case["critical"]],
        "overall_status": overall_status,
    }


def compare_against_baseline(
    run_payload: dict[str, Any],
    baseline_payload: dict[str, Any],
    *,
    rubric: dict[str, Any],
) -> dict[str, Any]:
    """Compare the current run against a frozen baseline."""
    run_context = run_payload["version_context"]
    baseline_context = baseline_payload.get("version_context", {})
    if baseline_payload.get("suite_id") != run_payload["suite_id"]:
        return {"status": "incompatible", "detail": "Baseline suite_id does not match the run."}
    if baseline_context.get("corpus_signature") != run_context["corpus_signature"]:
        return {
            "status": "incompatible",
            "detail": "Baseline corpus_signature does not match the current published corpus.",
        }
    baseline_cases = {
        item["case_id"]: item
        for item in baseline_payload.get("cases", [])
        if isinstance(item, dict)
    }
    critical_regressions: list[dict[str, Any]] = []
    rubric_regressions: list[dict[str, Any]] = []
    for case in run_payload["cases"]:
        baseline_case = baseline_cases.get(case["case_id"])
        if not isinstance(baseline_case, dict):
            continue
        if baseline_case.get("deterministic_passed") and not case["deterministic_passed"]:
            regression = {
                "case_id": case["case_id"],
                "reason": "deterministic regression",
            }
            rubric_regressions.append(regression)
            if case["critical"]:
                critical_regressions.append(regression)
        baseline_mean = baseline_case.get("rubric_mean_score")
        current_mean = case.get("rubric", {}).get("mean_score") if case.get("rubric") else None
        if isinstance(baseline_mean, (int, float)) and isinstance(current_mean, (int, float)):
            score_drop = round(float(baseline_mean) - float(current_mean), 3)
            if score_drop > 0:
                regression = {
                    "case_id": case["case_id"],
                    "reason": "rubric score drop",
                    "score_drop": score_drop,
                }
                rubric_regressions.append(regression)
                if case["critical"]:
                    critical_regressions.append(regression)
    current_mean = run_payload["summary"].get("answer_mean_score")
    baseline_mean = baseline_payload.get("summary", {}).get("answer_mean_score")
    aggregate_rubric_drop = 0.0
    if isinstance(current_mean, (int, float)) and isinstance(baseline_mean, (int, float)):
        aggregate_rubric_drop = round(float(baseline_mean) - float(current_mean), 3)
    status = "passed"
    if critical_regressions:
        status = "failed"
    elif (
        aggregate_rubric_drop > rubric["acceptance_thresholds"]["aggregate_rubric_regression_limit"]
    ):
        status = "degraded"
    elif rubric_regressions:
        status = "degraded"
    return {
        "status": status,
        "critical_regressions": critical_regressions,
        "rubric_regressions": rubric_regressions,
        "aggregate_rubric_drop": aggregate_rubric_drop,
    }


def _render_scorecard_markdown(run_payload: dict[str, Any]) -> str:
    """Render a compact Markdown scorecard for a private evaluation run."""
    summary = run_payload["summary"]
    comparison = run_payload["baseline_comparison"]
    lines = [
        f"# {run_payload['title']}",
        "",
        f"- Run ID: `{run_payload['run_id']}`",
        f"- Suite ID: `{run_payload['suite_id']}`",
        f"- Overall status: `{summary['overall_status']}`",
        f"- Deterministic pass rate: `{summary['deterministic_pass_rate']}`",
    ]
    if summary.get("answer_mean_score") is not None:
        lines.append(f"- Mean answer rubric score: `{summary['answer_mean_score']}`")
    lines.extend(
        [
            f"- Corpus signature: `{run_payload['version_context']['corpus_signature']}`",
            f"- Retrieval strategy: `{run_payload['version_context']['retrieval_strategy_id']}`",
            f"- Answer workflow: `{run_payload['version_context']['answer_workflow_id']}`",
            "",
            "## Baseline Comparison",
            "",
            f"- Status: `{comparison['status']}`",
        ]
    )
    if comparison.get("detail"):
        lines.append(f"- Detail: {comparison['detail']}")
    if comparison.get("aggregate_rubric_drop") is not None:
        lines.append(f"- Aggregate rubric drop: `{comparison['aggregate_rubric_drop']}`")
    if comparison.get("critical_regressions"):
        lines.append(
            "- Critical regressions: "
            + ", ".join(item["case_id"] for item in comparison["critical_regressions"])
        )
    lines.extend(["", "## Failures", ""])
    failed_checks = [
        (case["case_id"], check)
        for case in run_payload["cases"]
        for check in case["deterministic_checks"]
        if not check["passed"]
    ]
    if failed_checks:
        for case_id, check in failed_checks:
            lines.append(
                f"- `{case_id}` failed `{check['name']}`: expected `{check['expected']}`, "
                f"actual `{check['actual']}`"
            )
    else:
        lines.append("- No deterministic failures.")
    review_cases = [
        case["case_id"] for case in run_payload["cases"] if case["outcome"] == "review-recommended"
    ]
    if review_cases:
        lines.append(
            "- Review recommended: " + ", ".join(f"`{case_id}`" for case_id in review_cases)
        )
    lines.extend(
        [
            "",
            "## Cases",
            "",
            "| Case | Mode | Deterministic | Answer State | Render | "
            "Rubric Mean | Outcome | Artifacts |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for case in run_payload["cases"]:
        result = case["execution"]["result"]
        rubric_mean = case["rubric"]["mean_score"] if case["rubric"] else "-"
        artifact_links = case.get("artifact_paths", {})
        artifact_summary = (
            ", ".join(f"{name}: `{path}`" for name, path in sorted(artifact_links.items())) or "-"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    case["case_id"],
                    case["execution_mode"],
                    "pass" if case["deterministic_passed"] else "fail",
                    str(result.get("answer_state", "-")),
                    str(result.get("render_inspection_required", "-")).lower(),
                    str(rubric_mean),
                    case["outcome"],
                    artifact_summary,
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def run_evaluation_suite(
    paths: WorkspacePaths,
    *,
    suite_path: Path,
    rubric_path: Path,
    judge_trials_path: Path | None = None,
    baseline_path: Path | None = None,
    answer_overrides: dict[str, str] | None = None,
    run_label: str | None = None,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run a private replayable evaluation suite over the current published corpus."""
    suite_path = _resolve_required_workspace_path(paths, suite_path)
    rubric_path = _resolve_required_workspace_path(paths, rubric_path)
    judge_trials_path = _resolve_workspace_path(paths, judge_trials_path)
    baseline_path = _resolve_workspace_path(paths, baseline_path)
    rubric = load_rubric_definition(rubric_path)
    suite = load_evaluation_suite(suite_path, rubric=rubric)
    if case_ids:
        selected_case_ids = set(case_ids)
        suite = {
            **suite,
            "cases": [case for case in suite["cases"] if case["case_id"] in selected_case_ids],
        }
        if not suite["cases"]:
            raise EvaluationConfigurationError(
                "No evaluation cases matched the requested target_ids."
            )
    judge_trials_payload = (
        load_judge_trials(judge_trials_path, suite=suite, rubric=rubric)
        if judge_trials_path is not None
        else {
            "schema_version": JUDGE_TRIALS_SCHEMA_VERSION,
            "suite_id": suite["suite_id"],
            "judge_profile": None,
            "trials_by_case": {},
        }
    )
    version_context = build_version_context(
        paths,
        suite_path=suite_path,
        rubric_path=rubric_path,
        judge_trials_path=judge_trials_path,
        baseline_path=baseline_path,
        judge_profile=judge_trials_payload.get("judge_profile"),
        suite=suite,
    )
    if version_context["corpus_signature"] != suite["corpus_signature"]:
        raise EvaluationConfigurationError(
            f"Suite corpus_signature `{suite['corpus_signature']}` does not match the current "
            f"published corpus `{version_context['corpus_signature']}`."
        )
    paths.evaluation_runs_dir.mkdir(parents=True, exist_ok=True)
    paths.user_feedback_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    case_results: list[dict[str, Any]] = []
    for case in suite["cases"]:
        execution = _execute_case(
            paths,
            case,
            answer_overrides=answer_overrides,
            run_scope_id=run_id,
        )
        checks = _case_deterministic_checks(paths, case, execution)
        deterministic_passed = all(check["passed"] for check in checks)
        rubric_result = aggregate_case_rubric(
            case,
            rubric=rubric,
            judge_trials=judge_trials_payload["trials_by_case"],
        )
        case_results.append(
            {
                **case,
                "execution": execution,
                "artifact_paths": _execution_artifacts(paths, execution),
                "deterministic_checks": checks,
                "deterministic_passed": deterministic_passed,
                "rubric": rubric_result,
                "outcome": _case_outcome(
                    deterministic_passed=deterministic_passed,
                    rubric_result=rubric_result,
                ),
                "judge_packet": _build_judge_packet(case, execution, rubric=rubric),
                "rubric_mean_score": rubric_result["mean_score"] if rubric_result else None,
            }
        )
    summary = _summarize_cases(case_results, acceptance_thresholds=rubric["acceptance_thresholds"])
    run_payload = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "run_id": run_id,
        "recorded_at": utc_now(),
        "title": run_label or suite["title"],
        "suite_id": suite["suite_id"],
        "description": suite["description"],
        "version_context": version_context,
        "summary": summary,
        "cases": case_results,
        "baseline_comparison": {"status": "not-provided"},
    }
    if baseline_path is not None and baseline_path.exists():
        baseline_payload = load_evaluation_baseline(baseline_path)
        run_payload["baseline_comparison"] = compare_against_baseline(
            run_payload,
            baseline_payload,
            rubric=rubric,
        )
        comparison_status = run_payload["baseline_comparison"]["status"]
        current_status = run_payload["summary"]["overall_status"]
        if RUN_STATUS_ORDER[comparison_status] > RUN_STATUS_ORDER[current_status]:
            run_payload["summary"]["overall_status"] = comparison_status
        elif comparison_status == "degraded" and current_status == "passed":
            run_payload["summary"]["overall_status"] = "degraded"

    run_dir = paths.evaluation_runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    run_json_path = run_dir / "run.json"
    scorecard_path = run_dir / "scorecard.md"
    write_json(run_json_path, run_payload)
    scorecard_path.write_text(_render_scorecard_markdown(run_payload), encoding="utf-8")
    run_payload["artifacts"] = {
        "run_json": str(run_json_path.relative_to(paths.root)),
        "scorecard_markdown": str(scorecard_path.relative_to(paths.root)),
    }
    write_json(run_json_path, run_payload)
    return run_payload


def freeze_baseline_from_run(
    run_payload: dict[str, Any],
    *,
    baseline_path: Path,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """Freeze a baseline payload from a completed evaluation run."""
    base_root = workspace_root or Path.cwd()
    baseline_path = baseline_path if baseline_path.is_absolute() else base_root / baseline_path
    baseline_payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "suite_id": run_payload["suite_id"],
        "frozen_at": utc_now(),
        "run_id": run_payload["run_id"],
        "version_context": run_payload["version_context"],
        "summary": run_payload["summary"],
        "cases": [
            {
                "case_id": case["case_id"],
                "critical": case["critical"],
                "deterministic_passed": case["deterministic_passed"],
                "answer_state": case["execution"]["result"].get("answer_state"),
                "support_basis": case["execution"]["result"].get("support_basis"),
                "render_inspection_required": case["execution"]["result"].get(
                    "render_inspection_required"
                ),
                "primary_source_ids": _case_primary_source_ids(case, case["execution"]["result"]),
                "rubric_mean_score": case["rubric"]["mean_score"] if case["rubric"] else None,
                "rubric_dimension_scores": (
                    case["rubric"]["dimension_scores"] if case["rubric"] else {}
                ),
            }
            for case in run_payload["cases"]
        ],
    }
    write_json(baseline_path, baseline_payload)
    return baseline_payload


def validate_feedback_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a structured feedback record against the Phase 5 taxonomy."""
    try:
        schema_version = payload.get("schema_version", FEEDBACK_SCHEMA_VERSION)
        if schema_version != FEEDBACK_SCHEMA_VERSION:
            raise FeedbackValidationError(
                f"Unsupported feedback schema_version `{schema_version}`."
            )
        feedback_tags = _require_string_list(
            payload.get("feedback_tags", []),
            "feedback_tags",
            allow_empty=False,
        )
        unknown_feedback_tags = sorted(set(feedback_tags) - set(FEEDBACK_TAXONOMY))
        if unknown_feedback_tags:
            raise FeedbackValidationError(
                "Unknown feedback tags: " + ", ".join(unknown_feedback_tags)
            )
        corrected_fact = payload.get("corrected_fact")
        if corrected_fact is not None and not isinstance(corrected_fact, dict):
            raise FeedbackValidationError("`corrected_fact` must be an object when provided.")
        return {
            "schema_version": FEEDBACK_SCHEMA_VERSION,
            "feedback_id": str(payload.get("feedback_id") or uuid.uuid4()),
            "recorded_at": str(payload.get("recorded_at") or utc_now()),
            "case_id": _require_string(payload.get("case_id"), "case_id"),
            "run_id": _require_string(payload.get("run_id"), "run_id"),
            "session_id": _require_optional_string(payload.get("session_id"), "session_id"),
            "trace_id": _require_optional_string(payload.get("trace_id"), "trace_id"),
            "feedback_tags": feedback_tags,
            "corrected_text": _require_optional_string(
                payload.get("corrected_text"),
                "corrected_text",
            ),
            "corrected_fact": corrected_fact,
            "notes": _require_optional_string(payload.get("notes"), "notes"),
        }
    except EvaluationConfigurationError as exc:
        raise FeedbackValidationError(str(exc)) from exc


def write_feedback_record(paths: WorkspacePaths, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist a private structured feedback record under runtime logs."""
    record = validate_feedback_record(payload)
    destination = paths.user_feedback_dir / f"{record['feedback_id']}.json"
    write_json(destination, record)
    record["path"] = str(destination.relative_to(paths.root))
    return record

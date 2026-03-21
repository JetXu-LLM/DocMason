"""Operator-only evaluation workflow helpers for Phase 6b1."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .contracts import ANSWER_STATES
from .evaluation import (
    EVALUATION_SCHEMA_VERSION,
    EvaluationConfigurationError,
    freeze_baseline_from_run,
    run_evaluation_suite,
)
from .project import WorkspacePaths, read_json, write_json
from .projections import refresh_runtime_projections
from .retrieval import utc_now

OPERATOR_REQUEST_SCHEMA_VERSION = 1
OPERATOR_ACTIONS = ("run-suite", "review-regressions", "promote-candidate", "freeze-baseline")
EVAL_SUITES = ("broad", "regression")


class OperatorEvalRequestError(ValueError):
    """Raised when an operator-eval request is missing or invalid."""


def _require_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OperatorEvalRequestError(f"`{field_name}` must be a non-empty string.")
    return value.strip()


def _require_string_list(value: Any, field_name: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise OperatorEvalRequestError(f"`{field_name}` must be a list of non-empty strings.")
    normalized = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise OperatorEvalRequestError(f"`{field_name}` must be a list of non-empty strings.")
        normalized.append(item.strip())
    if not allow_empty and not normalized:
        raise OperatorEvalRequestError(f"`{field_name}` may not be empty.")
    return normalized


def load_operator_request(path: Path) -> dict[str, Any]:
    """Load and validate one operator-eval request file."""
    payload = read_json(path)
    if not payload:
        raise OperatorEvalRequestError(f"Missing operator request at `{path}`.")
    schema_version = payload.get("schema_version")
    if schema_version != OPERATOR_REQUEST_SCHEMA_VERSION:
        raise OperatorEvalRequestError(
            f"Unsupported operator request schema_version `{schema_version}`."
        )
    action = _require_string(payload.get("action"), "action")
    if action not in OPERATOR_ACTIONS:
        raise OperatorEvalRequestError(
            f"`action` must be one of {', '.join(OPERATOR_ACTIONS)}."
        )
    suite = _require_string(payload.get("suite"), "suite")
    if suite not in EVAL_SUITES:
        raise OperatorEvalRequestError(f"`suite` must be one of {', '.join(EVAL_SUITES)}.")
    return {
        "schema_version": OPERATOR_REQUEST_SCHEMA_VERSION,
        "action": action,
        "suite": suite,
        "target_ids": _require_string_list(
            payload.get("target_ids", []),
            "target_ids",
            allow_empty=True,
        ),
        "run_label": (
            _require_string(payload.get("run_label"), "run_label")
            if payload.get("run_label") is not None
            else None
        ),
        "operator_notes": (
            _require_string(payload.get("operator_notes"), "operator_notes")
            if payload.get("operator_notes") is not None
            else None
        ),
    }


def ensure_eval_layout(paths: WorkspacePaths) -> None:
    """Ensure the runtime/eval directory structure exists."""
    directories = [
        paths.eval_dir,
        paths.eval_benchmarks_dir,
        paths.eval_broad_benchmark_dir,
        paths.eval_regression_benchmark_dir,
        paths.eval_candidate_drafts_dir,
        paths.evaluation_runs_dir,
        paths.eval_reviews_dir,
        paths.user_feedback_dir,
        paths.eval_requests_dir,
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


def _suite_payload(paths: WorkspacePaths, suite: str) -> dict[str, Any]:
    payload = read_json(paths.eval_suite_path(suite))
    if not payload:
        raise OperatorEvalRequestError(
            f"Missing `{suite}` suite definition at `{paths.eval_suite_path(suite)}`."
        )
    return payload


def _load_run_payloads(paths: WorkspacePaths, *, suite: str) -> list[dict[str, Any]]:
    suite_id = _suite_payload(paths, suite).get("suite_id")
    run_payloads: list[dict[str, Any]] = []
    for path in sorted(paths.evaluation_runs_dir.glob("*/run.json")):
        payload = read_json(path)
        if not payload:
            continue
        if suite_id is not None and payload.get("suite_id") != suite_id:
            continue
        payload["run_json_path"] = str(path.relative_to(paths.root))
        run_payloads.append(payload)
    run_payloads.sort(key=lambda item: str(item.get("recorded_at") or ""), reverse=True)
    return run_payloads


def _select_run_payload(paths: WorkspacePaths, *, suite: str, target_ids: list[str]) -> dict[str, Any]:
    run_payloads = _load_run_payloads(paths, suite=suite)
    if not run_payloads:
        raise OperatorEvalRequestError(f"No evaluation runs exist yet for suite `{suite}`.")
    if not target_ids:
        return run_payloads[0]
    run_lookup = {str(payload.get("run_id")): payload for payload in run_payloads}
    run_id = target_ids[0]
    if run_id not in run_lookup:
        raise OperatorEvalRequestError(f"Unknown evaluation run `{run_id}` for suite `{suite}`.")
    return run_lookup[run_id]


def _candidate_lookup(paths: WorkspacePaths) -> dict[str, dict[str, Any]]:
    refresh_runtime_projections(paths)
    payload = read_json(paths.benchmark_candidates_path)
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        return {}
    lookup: dict[str, dict[str, Any]] = {}
    for item in candidates:
        if not isinstance(item, dict):
            continue
        candidate_id = item.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            lookup[candidate_id] = item
    return lookup


def _ensure_regression_scaffold(paths: WorkspacePaths) -> None:
    ensure_eval_layout(paths)
    suite_path = paths.eval_suite_path("regression")
    rubric_path = paths.eval_rubric_path("regression")
    judge_trials_path = paths.eval_judge_trials_path("regression")
    if not suite_path.exists():
        broad_suite = _suite_payload(paths, "broad")
        write_json(
            suite_path,
            {
                "schema_version": EVALUATION_SCHEMA_VERSION,
                "suite_id": "phase-6b1-regression-suite",
                "title": "Phase 6b1 Regression Suite",
                "description": "Promoted runtime regression cases.",
                "target": broad_suite.get("target", "current"),
                "corpus_signature": broad_suite.get("corpus_signature"),
                "retrieval_strategy_id": broad_suite.get("retrieval_strategy_id"),
                "answer_workflow_id": broad_suite.get("answer_workflow_id"),
                "cases": [],
            },
        )
    if not rubric_path.exists():
        broad_rubric = read_json(paths.eval_rubric_path("broad"))
        if not broad_rubric:
            raise OperatorEvalRequestError("Cannot scaffold regression rubric without a broad rubric.")
        write_json(rubric_path, broad_rubric)
    if not judge_trials_path.exists():
        broad_trials = read_json(paths.eval_judge_trials_path("broad"))
        if not broad_trials:
            raise OperatorEvalRequestError(
                "Cannot scaffold regression judge trials without broad judge trials."
            )
        write_json(
            judge_trials_path,
            {
                "schema_version": broad_trials.get("schema_version", 1),
                "suite_id": "phase-6b1-regression-suite",
                "judge_profile": broad_trials.get("judge_profile"),
                "trials_by_case": {},
            },
        )


def _candidate_case_id(candidate_id: str) -> str:
    return candidate_id.removeprefix("candidate-").replace("/", "-")


def _candidate_execution_mode(candidate: dict[str, Any]) -> str:
    answer_file_path = candidate.get("answer_file_path")
    if isinstance(answer_file_path, str) and answer_file_path:
        return "trace-answer"
    return "retrieve"


def _candidate_query_or_prompt(paths: WorkspacePaths, candidate: dict[str, Any]) -> str:
    answer_file_path = candidate.get("answer_file_path")
    if isinstance(answer_file_path, str) and answer_file_path:
        answer_path = paths.root / answer_file_path
        if answer_path.exists():
            text = answer_path.read_text(encoding="utf-8").strip()
            if text:
                return text
    question = candidate.get("original_user_question")
    if isinstance(question, str) and question.strip():
        return question.strip()
    raise OperatorEvalRequestError(
        f"Candidate `{candidate.get('candidate_id')}` does not contain replayable text."
    )


def _candidate_reference_facts(candidate: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    reason = candidate.get("reason")
    if isinstance(reason, str) and reason:
        facts.append(reason)
    question = candidate.get("original_user_question")
    if isinstance(question, str) and question:
        facts.append(f"Original question: {question}")
    if not facts:
        facts.append("Promoted runtime regression case.")
    return facts


def _draft_case_from_candidate(paths: WorkspacePaths, candidate: dict[str, Any]) -> dict[str, Any]:
    suggested_answer_state = candidate.get("suggested_expected_answer_state")
    expected_answer_state = (
        suggested_answer_state
        if isinstance(suggested_answer_state, str) and suggested_answer_state in ANSWER_STATES
        else None
    )
    support_basis = (
        candidate.get("support_basis")
        if isinstance(candidate.get("support_basis"), str)
        else None
    )
    support_manifest_path = (
        candidate.get("support_manifest_path")
        if isinstance(candidate.get("support_manifest_path"), str)
        else None
    )
    inner_workflow_id = (
        candidate.get("inner_workflow_id")
        if candidate.get("inner_workflow_id") in {"grounded-answer", "grounded-composition"}
        else None
    )
    query_or_prompt = _candidate_query_or_prompt(paths, candidate)
    execution_mode = _candidate_execution_mode(candidate)
    case_id = _candidate_case_id(str(candidate["candidate_id"]))
    draft_case = {
        "case_id": case_id,
        "family": str(candidate.get("suggested_benchmark_family") or "runtime-regression"),
        "execution_mode": execution_mode,
        "query_or_prompt": query_or_prompt,
        "expected_primary_sources": [],
        "required_sources_or_units": [],
        "minimum_support_overlap": 0,
        "forbidden_sources_or_units": [],
        "expected_status": str(candidate.get("suggested_expected_status") or "degraded"),
        "expected_answer_state": expected_answer_state,
        "expected_support_basis": support_basis,
        "expected_render_inspection_required": bool(candidate.get("requires_render_inspection")),
        "reference_facts": _candidate_reference_facts(candidate),
        "active_rubric_dimensions": [],
        "feedback_tags": list(candidate.get("suggested_feedback_tags") or ["coverage_gap"]),
        "critical": bool(candidate.get("candidate_priority") == "high"),
        "top": 3,
        "graph_hops": 1,
        "include_renders": True,
        "declared_answer_state": expected_answer_state,
        "execution_support_basis": support_basis,
        "execution_inner_workflow_id": inner_workflow_id,
    }
    if support_manifest_path:
        draft_case["execution_support_manifest_path"] = support_manifest_path
    return draft_case


def _render_review_markdown(review_payload: dict[str, Any]) -> str:
    lines = [
        f"# {review_payload['title']}",
        "",
        f"- Review ID: `{review_payload['review_id']}`",
        f"- Suite: `{review_payload['suite']}`",
        f"- Generated at: `{review_payload['generated_at']}`",
        f"- Latest run ID: `{review_payload['latest_run']['run_id']}`",
        f"- Latest overall status: `{review_payload['latest_run']['overall_status']}`",
        f"- Baseline comparison: `{review_payload['latest_run']['baseline_status']}`",
        f"- Candidate count: `{review_payload['candidate_summary']['candidate_count']}`",
        "",
        "## Latest Run",
        "",
        f"- Failed cases: {', '.join(review_payload['latest_run']['failed_cases']) or '(none)'}",
        "- Review recommended: "
        + (", ".join(review_payload["latest_run"]["review_recommended_cases"]) or "(none)"),
        "",
        "## Candidate Highlights",
        "",
    ]
    for candidate in review_payload["candidate_summary"]["top_candidates"]:
        lines.append(
            "- "
            f"`{candidate['candidate_id']}` "
            f"priority=`{candidate['candidate_priority']}` "
            f"family=`{candidate['suggested_benchmark_family']}`"
        )
    if not review_payload["candidate_summary"]["top_candidates"]:
        lines.append("- No candidate highlights.")
    lines.append("")
    return "\n".join(lines)


def _write_review_pack(paths: WorkspacePaths, review_payload: dict[str, Any]) -> dict[str, str]:
    review_id = review_payload["review_id"]
    json_path = paths.eval_review_json_path(review_id)
    markdown_path = paths.eval_review_markdown_path(review_id)
    write_json(json_path, review_payload)
    markdown_path.write_text(_render_review_markdown(review_payload), encoding="utf-8")
    return {
        "review_json": str(json_path.relative_to(paths.root)),
        "review_markdown": str(markdown_path.relative_to(paths.root)),
    }


def _run_suite_action(paths: WorkspacePaths, request: dict[str, Any]) -> dict[str, Any]:
    suite = request["suite"]
    run_payload = run_evaluation_suite(
        paths,
        suite_path=paths.eval_suite_path(suite),
        rubric_path=paths.eval_rubric_path(suite),
        judge_trials_path=(
            paths.eval_judge_trials_path(suite)
            if paths.eval_judge_trials_path(suite).exists()
            else None
        ),
        baseline_path=(
            paths.eval_baseline_path(suite) if paths.eval_baseline_path(suite).exists() else None
        ),
        run_label=request.get("run_label") or f"{suite} operator evaluation",
        case_ids=request["target_ids"] or None,
    )
    overall_status = str(run_payload["summary"]["overall_status"])
    status = "ready"
    if overall_status == "degraded":
        status = "degraded"
    elif overall_status != "passed":
        status = "action-required"
    return {
        "status": status,
        "action": request["action"],
        "suite": suite,
        "request": request,
        "run_id": run_payload["run_id"],
        "artifacts": run_payload["artifacts"],
        "summary": run_payload["summary"],
        "baseline_comparison": run_payload["baseline_comparison"],
    }


def _review_regressions_action(paths: WorkspacePaths, request: dict[str, Any]) -> dict[str, Any]:
    latest_run = _select_run_payload(paths, suite=request["suite"], target_ids=request["target_ids"])
    candidates = _candidate_lookup(paths)
    top_candidates = list(candidates.values())[:10]
    review_payload = {
        "review_id": str(uuid.uuid4()),
        "generated_at": utc_now(),
        "title": request.get("run_label") or f"{request['suite']} regression review",
        "suite": request["suite"],
        "request": request,
        "latest_run": {
            "run_id": latest_run.get("run_id"),
            "recorded_at": latest_run.get("recorded_at"),
            "overall_status": latest_run.get("summary", {}).get("overall_status"),
            "baseline_status": latest_run.get("baseline_comparison", {}).get("status"),
            "failed_cases": latest_run.get("summary", {}).get("failed_cases", []),
            "review_recommended_cases": latest_run.get("summary", {}).get(
                "review_recommended_cases",
                [],
            ),
            "run_json_path": latest_run.get("run_json_path"),
        },
        "candidate_summary": {
            "candidate_count": len(candidates),
            "top_candidates": top_candidates,
        },
        "operator_notes": request.get("operator_notes"),
    }
    review_payload["artifacts"] = _write_review_pack(paths, review_payload)
    return {
        "status": "ready",
        "action": request["action"],
        "suite": request["suite"],
        "request": request,
        "review_id": review_payload["review_id"],
        "artifacts": review_payload["artifacts"],
        "latest_run": review_payload["latest_run"],
        "candidate_summary": review_payload["candidate_summary"],
    }


def _promote_candidate_action(paths: WorkspacePaths, request: dict[str, Any]) -> dict[str, Any]:
    if not request["target_ids"]:
        raise OperatorEvalRequestError("`promote-candidate` requires at least one candidate id.")
    _ensure_regression_scaffold(paths)
    candidate_lookup = _candidate_lookup(paths)
    suite_payload = _suite_payload(paths, "regression")
    existing_cases = suite_payload.get("cases", [])
    if not isinstance(existing_cases, list):
        raise OperatorEvalRequestError("Regression suite `cases` must be a list.")
    existing_case_ids = {
        item.get("case_id") for item in existing_cases if isinstance(item, dict)
    }
    promoted: list[str] = []
    skipped: list[str] = []
    draft_paths: list[str] = []
    for candidate_id in request["target_ids"]:
        candidate = candidate_lookup.get(candidate_id)
        if candidate is None:
            raise OperatorEvalRequestError(f"Unknown benchmark candidate `{candidate_id}`.")
        draft_payload = {
            "schema_version": 1,
            "draft_id": candidate_id,
            "generated_at": utc_now(),
            "suite": "regression",
            "candidate": candidate,
            "proposed_case": _draft_case_from_candidate(paths, candidate),
            "operator_notes": request.get("operator_notes"),
        }
        draft_path = paths.eval_candidate_draft_path(candidate_id)
        write_json(draft_path, draft_payload)
        draft_paths.append(str(draft_path.relative_to(paths.root)))
        case_id = draft_payload["proposed_case"]["case_id"]
        if case_id in existing_case_ids:
            skipped.append(candidate_id)
            continue
        existing_cases.append(draft_payload["proposed_case"])
        existing_case_ids.add(case_id)
        promoted.append(candidate_id)
    suite_payload["cases"] = existing_cases
    write_json(paths.eval_suite_path("regression"), suite_payload)
    return {
        "status": "ready",
        "action": request["action"],
        "suite": request["suite"],
        "request": request,
        "promoted_candidate_ids": promoted,
        "skipped_candidate_ids": skipped,
        "draft_paths": draft_paths,
        "regression_suite_path": str(paths.eval_suite_path("regression").relative_to(paths.root)),
    }


def _freeze_baseline_action(paths: WorkspacePaths, request: dict[str, Any]) -> dict[str, Any]:
    run_payload = _select_run_payload(paths, suite=request["suite"], target_ids=request["target_ids"])
    baseline_path = paths.eval_baseline_path(request["suite"])
    baseline_payload = freeze_baseline_from_run(run_payload, baseline_path=baseline_path)
    return {
        "status": "ready",
        "action": request["action"],
        "suite": request["suite"],
        "request": request,
        "run_id": run_payload.get("run_id"),
        "baseline_path": str(baseline_path.relative_to(paths.root)),
        "baseline_summary": baseline_payload.get("summary", {}),
    }


def run_operator_eval(paths: WorkspacePaths, *, request_path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    """Execute the operator-only evaluation workflow from its request file."""
    ensure_eval_layout(paths)
    resolved_request_path = request_path or paths.eval_request_path
    request = load_operator_request(resolved_request_path)
    action = request["action"]
    try:
        if action == "run-suite":
            payload = _run_suite_action(paths, request)
        elif action == "review-regressions":
            payload = _review_regressions_action(paths, request)
        elif action == "promote-candidate":
            payload = _promote_candidate_action(paths, request)
        elif action == "freeze-baseline":
            payload = _freeze_baseline_action(paths, request)
        else:  # pragma: no cover - protected by validation
            raise OperatorEvalRequestError(f"Unsupported operator action `{action}`.")
    except (OperatorEvalRequestError, EvaluationConfigurationError, FileNotFoundError) as exc:
        payload = {
            "status": "action-required",
            "workflow_id": "operator-eval",
            "request_path": str(resolved_request_path.relative_to(paths.root)),
            "detail": str(exc),
            "request": request,
        }
        lines = [
            "Workflow status: action-required",
            "Workflow: operator-eval",
            str(exc),
        ]
        return payload, lines

    payload.update(
        {
            "workflow_id": "operator-eval",
            "request_path": str(resolved_request_path.relative_to(paths.root)),
        }
    )
    lines = [
        f"Workflow status: {payload['status']}",
        "Workflow: operator-eval",
        f"Action: {action}",
        f"Suite: {request['suite']}",
    ]
    if action == "run-suite":
        lines.append(f"Run ID: {payload['run_id']}")
    elif action == "review-regressions":
        lines.append(f"Review ID: {payload['review_id']}")
    elif action == "promote-candidate":
        lines.append(f"Promoted candidates: {len(payload['promoted_candidate_ids'])}")
    elif action == "freeze-baseline":
        lines.append(f"Baseline: {payload['baseline_path']}")
    return payload, lines

"""Derived ask-result explanation contracts.

These helpers translate existing ask truth into stable product-facing metadata.
They do not adjudicate truth and must not override trace, support, or
admissibility law.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

NEXT_STEP_BY_STATUS = {
    "execute": "continue-inner-workflow",
    "awaiting-confirmation": "wait-for-user-confirmation",
    "waiting-shared-job": "wait-for-shared-job",
    "completed": "return-final-answer",
    "boundary": "return-boundary-answer",
    "blocked": "do-not-return-final-answer",
}


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip())
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _status_from_turn(turn: dict[str, Any], explicit_status: str | None) -> str:
    status = _nonempty_string(explicit_status)
    if status:
        return status
    support_basis = _nonempty_string(turn.get("support_basis"))
    answer_state = _nonempty_string(turn.get("answer_state"))
    committed_run_id = _nonempty_string(turn.get("committed_run_id"))
    turn_state = _nonempty_string(turn.get("turn_state") or turn.get("status"))
    if committed_run_id is not None:
        if support_basis == "governed-boundary" or answer_state == "abstained":
            return "boundary"
        return "completed"
    if turn_state in {"prepared", "execute"}:
        return "execute"
    if turn_state in {"awaiting-confirmation", "waiting-shared-job", "completed", "boundary"}:
        return turn_state
    return "blocked"


def _support_gap_reason_codes(support_fulfillment: dict[str, Any]) -> list[str]:
    status = _nonempty_string(support_fulfillment.get("status"))
    if status in {None, "satisfied"}:
        return []
    codes = [f"support-fulfillment-{status}"]
    primary_gap_type = _nonempty_string(support_fulfillment.get("primary_gap_type"))
    if primary_gap_type is not None:
        codes.append(primary_gap_type)
    codes.extend(_string_list(support_fulfillment.get("issue_codes")))
    return codes


def _reason_codes(
    *,
    status: str,
    answer_state: str | None,
    support_basis: str | None,
    turn: dict[str, Any],
) -> list[str]:
    support_fulfillment = _mapping(turn.get("support_fulfillment"))
    codes: list[str] = []
    if status == "blocked":
        codes.append("blocked")
    if status == "boundary" or support_basis == "governed-boundary":
        codes.append("governed-boundary")
    if status == "completed" and answer_state and answer_state != "grounded":
        codes.append(f"answer-state-{answer_state}")
    if support_basis:
        codes.append(f"support-basis-{support_basis}")
    codes.extend(_string_list(turn.get("issue_codes")))
    primary_issue_code = _nonempty_string(turn.get("primary_issue_code"))
    if primary_issue_code is not None:
        codes.append(primary_issue_code)
    codes.extend(_support_gap_reason_codes(support_fulfillment))
    if bool(turn.get("source_escalation_required")):
        codes.append("source-escalation-required")
    if bool(turn.get("render_inspection_required")):
        codes.append("render-inspection-required")
    if support_basis == "mixed" and turn.get("mixed_support_explainable") is False:
        codes.append("mixed-support-unexplained")
    return list(dict.fromkeys(codes))


def _has_unsatisfied_support(turn: dict[str, Any]) -> bool:
    fulfillment_status = _nonempty_string(_mapping(turn.get("support_fulfillment")).get("status"))
    return fulfillment_status not in {None, "satisfied"}


def _show_to_user(
    *,
    status: str,
    answer_state: str | None,
    turn: dict[str, Any],
    reason_codes: list[str],
) -> bool:
    if status == "boundary" or status == "blocked":
        return True
    if status != "completed":
        return False
    if answer_state and answer_state != "grounded":
        return True
    if _has_unsatisfied_support(turn):
        return True
    return bool(
        reason_codes
        and any(code for code in reason_codes if not code.startswith("support-basis-"))
    )


def _summary(status: str, answer_state: str | None, show_to_user: bool) -> str:
    if not show_to_user:
        if status == "execute":
            return "The ask is ready for inner workflow execution."
        if status == "awaiting-confirmation":
            return "The ask is waiting for confirmation."
        if status == "waiting-shared-job":
            return "The ask is waiting for shared evidence work."
        return "The ask completed with a grounded answer."
    if status == "boundary":
        return "The ask closed at a governed boundary."
    if status == "blocked":
        return "The ask did not produce a returnable final answer."
    if answer_state == "partially-grounded":
        return "The ask completed with a partially grounded answer."
    if answer_state == "unresolved":
        return "The ask completed without enough support to ground the answer."
    if answer_state == "abstained":
        return "The ask completed with an explicit abstention."
    return "The ask completed with a support or admissibility notice."


def _why(
    *,
    status: str,
    answer_state: str | None,
    turn: dict[str, Any],
    detail: str | None,
    support_notice: str | None,
) -> str:
    detail_candidate = detail if status in {"boundary", "blocked"} else None
    for candidate in (
        support_notice,
        detail_candidate,
        _nonempty_string(turn.get("response_excerpt")) if status == "boundary" else None,
        _nonempty_string(turn.get("primary_issue_code")),
    ):
        if candidate is not None:
            return candidate
    if status == "boundary":
        return "DocMason reached a governed evidence or workflow boundary for this ask."
    if status == "blocked":
        return "DocMason could not lawfully commit a final answer for this ask."
    if answer_state == "partially-grounded":
        return (
            "At least one final answer segment remained only partially supported by "
            "the selected evidence."
        )
    if answer_state == "unresolved":
        return "The selected evidence did not support the final answer strongly enough."
    if answer_state == "abstained":
        return "The workflow explicitly abstained instead of presenting unsupported content."
    return "The result reflects derived runtime support and admissibility checks."


def build_result_explanation(
    turn: dict[str, Any] | None,
    *,
    status: str | None = None,
    next_step: str | None = None,
    detail: str | None = None,
    support_notice: str | None = None,
) -> dict[str, Any]:
    """Return the derived terminal-result explanation for one ask payload or turn."""
    payload = dict(turn) if isinstance(turn, dict) else {}
    effective_status = _status_from_turn(payload, status)
    answer_state = _nonempty_string(payload.get("answer_state"))
    support_basis = _nonempty_string(payload.get("support_basis"))
    reason_codes = _reason_codes(
        status=effective_status,
        answer_state=answer_state,
        support_basis=support_basis,
        turn=payload,
    )
    show_to_user = _show_to_user(
        status=effective_status,
        answer_state=answer_state,
        turn=payload,
        reason_codes=reason_codes,
    )
    effective_next_step = _nonempty_string(next_step) or NEXT_STEP_BY_STATUS.get(
        effective_status,
        "do-not-return-final-answer",
    )
    return {
        "schema_version": 1,
        "show_to_user": show_to_user,
        "status": effective_status,
        "answer_state": answer_state,
        "support_basis": support_basis,
        "reason_codes": reason_codes,
        "summary": _summary(effective_status, answer_state, show_to_user),
        "why": _why(
            status=effective_status,
            answer_state=answer_state,
            turn=payload,
            detail=_nonempty_string(detail),
            support_notice=_nonempty_string(support_notice),
        ),
        "next_step": effective_next_step,
        "source": "derived-runtime",
    }


def _answer_digest(workspace_root: Path, answer_file_path: str | None) -> dict[str, Any] | None:
    path_value = _nonempty_string(answer_file_path)
    if path_value is None:
        return None
    answer_path = Path(path_value)
    if not answer_path.is_absolute():
        answer_path = workspace_root / answer_path
    if not answer_path.exists() or not answer_path.is_file():
        return None
    data = answer_path.read_bytes()
    return {
        "algorithm": "sha256",
        "hex": hashlib.sha256(data).hexdigest(),
        "byte_count": len(data),
    }


def _repair_action(issue_codes: list[str]) -> str:
    issue_set = set(issue_codes)
    has_path_issue = "illegal-source-citation-path" in issue_set
    has_mixed_issue = "mixed-support-unexplained" in issue_set
    if has_path_issue and has_mixed_issue:
        return "rewrite-answer-and-provide-or-remove-mixed-support"
    if has_path_issue:
        return "rewrite-answer-without-machine-local-paths"
    if has_mixed_issue:
        return "provide-support-manifest-or-demote-support-basis"
    return "repair-final-answer-and-retrace"


def build_admissibility_repair(
    *,
    workspace_root: Path,
    turn: dict[str, Any] | None,
    answer_file_path: str | None,
    primary_issue_code: str | None,
    issue_codes: list[str],
    detail: str | None = None,
) -> dict[str, Any]:
    """Return lightweight same-turn repair metadata for finalize failures."""
    payload = dict(turn) if isinstance(turn, dict) else {}
    normalized_issue_codes = _string_list(issue_codes)
    normalized_primary = _nonempty_string(primary_issue_code) or (
        normalized_issue_codes[0] if normalized_issue_codes else None
    )
    effective_answer_file_path = _nonempty_string(answer_file_path) or _nonempty_string(
        payload.get("answer_file_path")
    )
    return {
        "schema_version": 1,
        "status": "repairable",
        "issue_codes": normalized_issue_codes,
        "primary_issue_code": normalized_primary,
        "suggested_action": _repair_action(normalized_issue_codes),
        "detail": _nonempty_string(detail),
        "failed_answer_digest": _answer_digest(workspace_root, effective_answer_file_path),
        "stores_failed_answer_text": False,
        "source": "derived-runtime",
    }

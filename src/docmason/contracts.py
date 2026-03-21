"""Shared runtime contracts for DocMason turn, answer, and memory semantics."""

from __future__ import annotations

from typing import Any

ANSWER_STATES = frozenset({"grounded", "partially-grounded", "unresolved", "abstained"})
SUPPORT_BASIS_VALUES = (
    "kb-grounded",
    "external-source-verified",
    "model-knowledge",
    "mixed",
)
TURN_STATES = frozenset({"opened", "prepared", "reconciled", "committed", "completed"})
MEMORY_KIND_VALUES = frozenset(
    {
        "working-note",
        "constraint",
        "clarification",
        "operator-intent",
        "preference",
        "fact",
    }
)


def validate_answer_state(answer_state: str | None) -> str | None:
    """Validate one answer-state value."""
    if answer_state is None:
        return None
    if answer_state not in ANSWER_STATES:
        raise ValueError(f"Unsupported answer_state `{answer_state}`.")
    return answer_state


def validate_support_basis(support_basis: str | None) -> str | None:
    """Validate one support-basis value."""
    if support_basis is None:
        return None
    if support_basis not in SUPPORT_BASIS_VALUES:
        raise ValueError(f"Unsupported support_basis `{support_basis}`.")
    return support_basis


def validate_turn_state(turn_state: str | None) -> str | None:
    """Validate one turn-state value."""
    if turn_state is None:
        return None
    if turn_state not in TURN_STATES:
        raise ValueError(f"Unsupported turn_state `{turn_state}`.")
    return turn_state


def validate_version_context(version_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate the minimum shape of one version-context payload."""
    if version_context is None:
        return None
    if not isinstance(version_context, dict):
        raise ValueError("version_context must be a mapping.")
    normalized = dict(version_context)
    if not isinstance(normalized.get("captured_at"), str) or not normalized.get("captured_at"):
        raise ValueError("version_context.captured_at is required.")
    return normalized


def validate_commit_contract(
    *,
    answer_state: str | None,
    support_basis: str | None,
    support_manifest_path: str | None,
    version_context: dict[str, Any] | None,
) -> None:
    """Validate one final answer and support contract before turn commit."""
    validated_answer_state = validate_answer_state(answer_state)
    validated_support_basis = validate_support_basis(support_basis)
    validate_version_context(version_context)
    if validated_answer_state is None:
        raise ValueError("Committed turns require an explicit answer_state.")
    if validated_support_basis is None:
        raise ValueError("Committed turns require an explicit support_basis.")
    if validated_support_basis in {"external-source-verified", "mixed"}:
        if not isinstance(support_manifest_path, str) or not support_manifest_path:
            raise ValueError(
                f"support_basis `{validated_support_basis}` requires a support_manifest_path."
            )

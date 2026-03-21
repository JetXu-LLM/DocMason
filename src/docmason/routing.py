"""Semantic-routing normalization helpers for ask, retrieval, and memory governance.

The primary routing decision for ordinary user questions should come from the agent's own
reasoning, not from growing keyword tables inside the repository. This module therefore focuses on:

- validating and normalizing agent-supplied semantic analysis
- deriving deterministic defaults from already-known structured state
- providing a deliberately small repair/backstop path when semantic analysis is unavailable

The fallback path is intentionally conservative. It is not meant to compete with a capable agent's
semantic judgment, and it should not grow into a multilingual keyword-classification system.
"""

from __future__ import annotations

import re
from typing import Any

from .affordances import normalize_evidence_requirements

TOKEN_PATTERN = re.compile(r"[0-9A-Za-z]+|[\u4e00-\u9fff]+")

QUESTION_CLASS_VALUES = {
    "answer",
    "composition",
    "retrieval",
    "provenance",
    "runtime-review",
}
QUESTION_DOMAIN_VALUES = {
    "workspace-corpus",
    "external-factual",
    "general-stable",
    "composition",
}
SUPPORT_STRATEGY_VALUES = {
    "kb-first",
    "web-first",
    "model-first",
    "kb-first-escalation",
}
QUESTION_ANALYSIS_ORIGIN_VALUES = {
    "agent-supplied",
    "repair-backstop",
}
MEMORY_MODE_VALUES = {"minimal", "contextual", "strong"}

MEMORY_KIND_VALUES = {
    "constraint",
    "preference",
    "stakeholder-context",
    "political-context",
    "clarification",
    "correction",
    "operator-intent",
    "working-note",
}
DURABILITY_VALUES = {"durable", "situational", "ephemeral"}
UNCERTAINTY_VALUES = {"confirmed", "stated-uncertain", "inferred"}
ANSWER_USE_POLICY_VALUES = {"direct-support", "contextual-only"}
RETRIEVAL_RANK_PRIOR_VALUES = {"high", "medium", "low"}

QUESTION_CLASS_TO_WORKFLOW = {
    "answer": "grounded-answer",
    "composition": "grounded-composition",
    "retrieval": "retrieval-workflow",
    "provenance": "provenance-trace",
    "runtime-review": "runtime-log-review",
}
COMPOSITION_MEMORY_KINDS = [
    "constraint",
    "preference",
    "stakeholder-context",
    "political-context",
    "clarification",
    "correction",
    "working-note",
]


def tokenize_text(text: str) -> list[str]:
    """Return normalized lexical tokens."""
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def normalized_searchable_text(text: str) -> str:
    """Return a normalized searchable form."""
    lowered = text.lower()
    normalized = " ".join(tokenize_text(text))
    return f"{lowered} {normalized}".strip()


def _token_set(text: str) -> set[str]:
    return set(tokenize_text(text))


def _valid_choice(value: Any, allowed_values: set[str]) -> str | None:
    if isinstance(value, str) and value in allowed_values:
        return value
    return None


def _valid_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _valid_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _deduplicate_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def corpus_hint_overlap_score(question: str, corpus_hints: list[str] | None) -> int:
    """Return the strongest lexical overlap between the question and any corpus hint text."""
    if not corpus_hints:
        return 0
    query_tokens = _token_set(question)
    if not query_tokens:
        return 0
    best = 0
    for hint in corpus_hints:
        if not isinstance(hint, str) or not hint.strip():
            continue
        overlap = len(query_tokens & _token_set(hint))
        if overlap > best:
            best = overlap
    return best


def support_strategy_for_question_domain(question_domain: str) -> str:
    """Return the preferred first-pass evidence strategy for the domain."""
    if question_domain == "workspace-corpus":
        return "kb-first"
    if question_domain == "external-factual":
        return "web-first"
    if question_domain == "composition":
        return "kb-first-escalation"
    return "model-first"


def _default_inner_workflow(question_class: str) -> str:
    return QUESTION_CLASS_TO_WORKFLOW.get(question_class, "grounded-answer")


def _default_memory_query_profile(
    *,
    question_class: str,
    question_domain: str,
) -> dict[str, Any]:
    if question_class == "composition" or question_domain == "composition":
        return {
            "mode": "strong",
            "relevant_memory_kinds": COMPOSITION_MEMORY_KINDS,
            "allow_contextual_only": True,
        }
    return {
        "mode": "minimal",
        "relevant_memory_kinds": [],
        "allow_contextual_only": False,
    }


def normalize_memory_query_profile(
    profile: dict[str, Any] | None,
    *,
    question_class: str,
    question_domain: str,
) -> dict[str, Any]:
    """Return a validated memory-query participation profile."""
    default = _default_memory_query_profile(
        question_class=question_class,
        question_domain=question_domain,
    )
    raw = profile if isinstance(profile, dict) else {}
    mode = _valid_choice(raw.get("mode"), MEMORY_MODE_VALUES) or str(default["mode"])
    relevant_memory_kinds = [
        kind
        for kind in raw.get("relevant_memory_kinds", [])
        if isinstance(kind, str) and kind in MEMORY_KIND_VALUES
    ]
    if not relevant_memory_kinds:
        relevant_memory_kinds = list(default["relevant_memory_kinds"])
    allow_contextual_only = _valid_bool(raw.get("allow_contextual_only"))
    if allow_contextual_only is None:
        allow_contextual_only = bool(default["allow_contextual_only"])
    return {
        "mode": mode,
        "relevant_memory_kinds": _deduplicate_strings(relevant_memory_kinds),
        "allow_contextual_only": allow_contextual_only,
    }


def _fallback_question_class(fallback_hints: dict[str, Any] | None) -> str:
    if isinstance(fallback_hints, dict):
        choice = _valid_choice(fallback_hints.get("question_class"), QUESTION_CLASS_VALUES)
        if choice is not None:
            return choice
        inner_workflow_id = _valid_string(fallback_hints.get("inner_workflow_id"))
        if inner_workflow_id is not None:
            for question_class, workflow_id in QUESTION_CLASS_TO_WORKFLOW.items():
                if workflow_id == inner_workflow_id:
                    return question_class
        bundle_paths = fallback_hints.get("bundle_paths", [])
        if isinstance(bundle_paths, list) and bundle_paths:
            return "composition"
    return "answer"


def _fallback_question_domain(
    question: str,
    *,
    question_class: str,
    corpus_hints: list[str] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> str:
    if isinstance(fallback_hints, dict):
        choice = _valid_choice(fallback_hints.get("question_domain"), QUESTION_DOMAIN_VALUES)
        if choice is not None:
            return choice
    if question_class == "composition":
        return "composition"
    if question_class in {"retrieval", "provenance", "runtime-review"}:
        return "workspace-corpus"
    if corpus_hint_overlap_score(question, corpus_hints) >= 2:
        return "workspace-corpus"
    return "general-stable"


def normalize_question_analysis(
    question: str,
    *,
    semantic_analysis: dict[str, Any] | None = None,
    corpus_hints: list[str] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize semantic analysis for one ask-like question.

    Primary path:
    - trust agent-supplied structured semantics when provided and valid

    Repair/backstop path:
    - reuse existing structured turn state when available
    - otherwise fall back to a deliberately small, conservative default
    """
    raw = semantic_analysis if isinstance(semantic_analysis, dict) else {}
    question_class = _valid_choice(raw.get("question_class"), QUESTION_CLASS_VALUES)
    if question_class is None:
        question_class = _fallback_question_class(fallback_hints)

    question_domain = _valid_choice(raw.get("question_domain"), QUESTION_DOMAIN_VALUES)
    if question_domain is None:
        question_domain = _fallback_question_domain(
            question,
            question_class=question_class,
            corpus_hints=corpus_hints,
            fallback_hints=fallback_hints,
        )

    support_strategy = _valid_choice(raw.get("support_strategy"), SUPPORT_STRATEGY_VALUES)
    if support_strategy is None:
        support_strategy = support_strategy_for_question_domain(question_domain)

    inner_workflow_id = _valid_string(raw.get("inner_workflow_id"))
    if inner_workflow_id is None:
        inner_workflow_id = _default_inner_workflow(question_class)

    route_reason = _valid_string(raw.get("route_reason"))
    if route_reason is None:
        if raw:
            route_reason = (
                f"Agent-supplied semantic analysis classified the question as "
                f"`{question_class}` with domain `{question_domain}`."
            )
        else:
            route_reason = (
                "No agent-supplied semantic analysis was provided, so the repo used the "
                "minimal repair/backstop defaults."
            )

    needs_latest_workspace_state = _valid_bool(raw.get("needs_latest_workspace_state"))
    if needs_latest_workspace_state is None:
        if isinstance(fallback_hints, dict):
            needs_latest_workspace_state = bool(fallback_hints.get("needs_latest_workspace_state"))
        else:
            needs_latest_workspace_state = False

    memory_query_profile = normalize_memory_query_profile(
        raw.get("memory_query_profile"),
        question_class=question_class,
        question_domain=question_domain,
    )
    evidence_requirements = normalize_evidence_requirements(
        raw.get("evidence_requirements"),
        question_class=question_class,
        question_domain=question_domain,
    )

    return {
        "question_class": question_class,
        "question_domain": question_domain,
        "inner_workflow_id": inner_workflow_id,
        "support_strategy": support_strategy,
        "route_reason": route_reason,
        "needs_latest_workspace_state": needs_latest_workspace_state,
        "memory_query_profile": memory_query_profile,
        "evidence_requirements": evidence_requirements,
        "analysis_origin": "agent-supplied" if raw else "repair-backstop",
    }


def infer_question_class(
    question: str,
    *,
    semantic_analysis: dict[str, Any] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> tuple[str, str, str]:
    """Return question class, workflow id, and route reason."""
    normalized = normalize_question_analysis(
        question,
        semantic_analysis=semantic_analysis,
        fallback_hints=fallback_hints,
    )
    return (
        str(normalized["question_class"]),
        str(normalized["inner_workflow_id"]),
        str(normalized["route_reason"]),
    )


def infer_question_domain(
    question: str,
    *,
    question_class: str | None = None,
    semantic_analysis: dict[str, Any] | None = None,
    corpus_hints: list[str] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> str:
    """Return the main evidence domain for one question."""
    normalized = normalize_question_analysis(
        question,
        semantic_analysis=semantic_analysis,
        corpus_hints=corpus_hints,
        fallback_hints={
            **(fallback_hints or {}),
            **({"question_class": question_class} if question_class else {}),
        },
    )
    return str(normalized["question_domain"])


def infer_memory_query_profile(
    question: str,
    *,
    question_class: str | None = None,
    question_domain: str | None = None,
    semantic_profile: dict[str, Any] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the memory-participation profile for one question."""
    normalized = normalize_question_analysis(
        question,
        semantic_analysis={
            "question_class": question_class,
            "question_domain": question_domain,
            "memory_query_profile": semantic_profile,
        },
        fallback_hints=fallback_hints,
    )
    return dict(normalized["memory_query_profile"])


def question_mentions_latest_docs(
    question: str,
    *,
    semantic_analysis: dict[str, Any] | None = None,
    fallback_hints: dict[str, Any] | None = None,
) -> bool:
    """Return whether the current ask turn requires the latest local workspace state."""
    normalized = normalize_question_analysis(
        question,
        semantic_analysis=semantic_analysis,
        fallback_hints=fallback_hints,
    )
    return bool(normalized["needs_latest_workspace_state"])


def infer_entry_semantics(
    *,
    user_text: str,
    continuation_type: str | None = None,
    tool_use_audit: dict[str, Any] | None = None,
    semantic_hints: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Infer durable semantic metadata for interaction memory and overlay entries.

    Primary path:
    - honor explicit semantic hints when the caller provides them

    Fallback path:
    - use only small structured signals such as continuation type and operator workflow activity
    """
    raw = semantic_hints if isinstance(semantic_hints, dict) else {}
    explicit_memory_kind = _valid_choice(raw.get("memory_kind"), MEMORY_KIND_VALUES)
    explicit_durability = _valid_choice(raw.get("durability"), DURABILITY_VALUES)
    explicit_uncertainty = _valid_choice(raw.get("uncertainty"), UNCERTAINTY_VALUES)
    explicit_answer_use_policy = _valid_choice(
        raw.get("answer_use_policy"),
        ANSWER_USE_POLICY_VALUES,
    )
    explicit_rank_prior = _valid_choice(
        raw.get("retrieval_rank_prior"),
        RETRIEVAL_RANK_PRIOR_VALUES,
    )

    if explicit_memory_kind is not None:
        memory_kind = explicit_memory_kind
    elif continuation_type == "constraint-update":
        memory_kind = "constraint"
    elif isinstance(tool_use_audit, dict) and tool_use_audit.get("docmason_commands"):
        memory_kind = "operator-intent"
    else:
        memory_kind = "working-note"

    if explicit_durability is not None:
        durability = explicit_durability
    elif memory_kind in {"constraint", "preference", "stakeholder-context", "political-context"}:
        durability = "durable"
    elif memory_kind in {"clarification", "correction", "operator-intent"}:
        durability = "situational"
    else:
        durability = "ephemeral"

    if explicit_uncertainty is not None:
        uncertainty = explicit_uncertainty
    elif memory_kind == "working-note":
        uncertainty = "inferred"
    else:
        uncertainty = "confirmed"

    if explicit_answer_use_policy is not None:
        answer_use_policy = explicit_answer_use_policy
    elif memory_kind in {"constraint", "preference", "clarification", "correction"}:
        answer_use_policy = "direct-support"
    else:
        answer_use_policy = "contextual-only"

    if explicit_rank_prior is not None:
        retrieval_rank_prior = explicit_rank_prior
    elif memory_kind in {"constraint", "preference", "correction"}:
        retrieval_rank_prior = "high"
    elif memory_kind in {"clarification", "stakeholder-context", "political-context"}:
        retrieval_rank_prior = "medium"
    else:
        retrieval_rank_prior = "low"

    return {
        "memory_kind": memory_kind,
        "durability": durability,
        "uncertainty": uncertainty,
        "answer_use_policy": answer_use_policy,
        "retrieval_rank_prior": retrieval_rank_prior,
    }


def normalize_memory_semantics(
    semantics: dict[str, Any] | None,
    *,
    fallback_text: str = "",
    continuation_type: str | None = None,
    tool_use_audit: dict[str, Any] | None = None,
    semantic_hints: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Return a complete valid memory-semantics object with deterministic backfill."""
    inferred = infer_entry_semantics(
        user_text=fallback_text,
        continuation_type=continuation_type,
        tool_use_audit=tool_use_audit,
        semantic_hints=semantic_hints,
    )
    raw = semantics if isinstance(semantics, dict) else {}
    normalized: dict[str, str] = {}
    field_values = {
        "memory_kind": MEMORY_KIND_VALUES,
        "durability": DURABILITY_VALUES,
        "uncertainty": UNCERTAINTY_VALUES,
        "answer_use_policy": ANSWER_USE_POLICY_VALUES,
        "retrieval_rank_prior": RETRIEVAL_RANK_PRIOR_VALUES,
    }
    for field_name, allowed_values in field_values.items():
        candidate = raw.get(field_name)
        if isinstance(candidate, str) and candidate in allowed_values:
            normalized[field_name] = candidate
        else:
            normalized[field_name] = inferred[field_name]
    return normalized

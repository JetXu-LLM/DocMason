"""Canonical workflow metadata helpers for skills and adapter routing."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project import WorkspacePaths

WORKFLOW_SCHEMA_VERSION = 1
WORKFLOW_CATEGORIES = (
    "foundation",
    "adapter",
    "knowledge-base",
    "evidence-access",
    "answer",
    "review",
)
WORKFLOW_MUTABILITIES = ("read-only", "workspace-write")
WORKFLOW_PARALLELISM = ("none", "read-only-safe", "per-source-safe")
WORKFLOW_SHORTCUT_POLICIES = ("best-effort", "none")
CATEGORY_TITLES = {
    "foundation": "Foundation Workflows",
    "adapter": "Adapter Workflows",
    "knowledge-base": "Knowledge-Base Workflows",
    "evidence-access": "Evidence-Access Workflows",
    "answer": "Answer Workflows",
    "review": "Review Workflows",
}


class WorkflowMetadataError(ValueError):
    """Raised when canonical workflow metadata is missing or invalid."""


@dataclass(frozen=True)
class WorkflowMetadata:
    """Validated metadata for one canonical workflow."""

    workflow_id: str
    category: str
    entry_intents: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    defaults: dict[str, Any]
    execution_hints: dict[str, Any]
    handoff: dict[str, Any]
    user_entry: dict[str, Any] | None
    skill_path: Path
    metadata_path: Path


def _require_string_list(value: Any, field_name: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise WorkflowMetadataError(f"`{field_name}` must be a list of non-empty strings.")
    if not allow_empty and not value:
        raise WorkflowMetadataError(f"`{field_name}` must not be empty.")
    return value


def load_workflow_metadata_file(skill_path: Path, metadata_path: Path) -> WorkflowMetadata:
    """Load and validate one canonical workflow metadata sidecar."""
    if not skill_path.exists():
        raise WorkflowMetadataError(f"Missing canonical skill file: {skill_path}")
    if not metadata_path.exists():
        raise WorkflowMetadataError(f"Missing workflow metadata sidecar: {metadata_path}")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - json library detail
        raise WorkflowMetadataError(
            f"Invalid JSON in workflow metadata `{metadata_path}`: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise WorkflowMetadataError(f"`{metadata_path}` must contain a JSON object.")

    required_fields = (
        "schema_version",
        "workflow_id",
        "category",
        "entry_intents",
        "required_capabilities",
        "defaults",
        "execution_hints",
        "handoff",
    )
    missing = [field for field in required_fields if field not in payload]
    if missing:
        raise WorkflowMetadataError(
            f"`{metadata_path}` is missing required fields: {', '.join(missing)}."
        )

    schema_version = payload["schema_version"]
    if schema_version != WORKFLOW_SCHEMA_VERSION:
        raise WorkflowMetadataError(
            f"`{metadata_path}` has unsupported schema_version `{schema_version}`."
        )

    workflow_id = payload["workflow_id"]
    if not isinstance(workflow_id, str) or not workflow_id:
        raise WorkflowMetadataError(
            f"`workflow_id` in `{metadata_path}` must be a non-empty string."
        )
    expected_workflow_id = skill_path.parent.name
    if workflow_id != expected_workflow_id:
        raise WorkflowMetadataError(
            "`workflow_id` "
            f"`{workflow_id}` does not match skill directory `{expected_workflow_id}`."
        )

    category = payload["category"]
    if category not in WORKFLOW_CATEGORIES:
        raise WorkflowMetadataError(
            f"`category` in `{metadata_path}` must be one of {', '.join(WORKFLOW_CATEGORIES)}."
        )

    entry_intents = tuple(_require_string_list(payload["entry_intents"], "entry_intents"))
    required_capabilities = tuple(
        _require_string_list(payload["required_capabilities"], "required_capabilities")
    )

    defaults = payload["defaults"]
    if not isinstance(defaults, dict):
        raise WorkflowMetadataError(f"`defaults` in `{metadata_path}` must be a JSON object.")
    for field_name in ("default_target", "default_mode"):
        if field_name in defaults and not isinstance(defaults[field_name], str):
            raise WorkflowMetadataError(
                f"`defaults.{field_name}` in `{metadata_path}` must be a string when present."
            )

    execution_hints = payload["execution_hints"]
    if not isinstance(execution_hints, dict):
        raise WorkflowMetadataError(
            f"`execution_hints` in `{metadata_path}` must be a JSON object."
        )
    for field_name in (
        "mutability",
        "parallelism",
        "background_commands",
        "must_return_to_main_agent",
    ):
        if field_name not in execution_hints:
            raise WorkflowMetadataError(
                f"`execution_hints` in `{metadata_path}` is missing `{field_name}`."
            )
    mutability = execution_hints["mutability"]
    if mutability not in WORKFLOW_MUTABILITIES:
        raise WorkflowMetadataError(
            f"`execution_hints.mutability` in `{metadata_path}` must be one of "
            f"{', '.join(WORKFLOW_MUTABILITIES)}."
        )
    parallelism = execution_hints["parallelism"]
    if parallelism not in WORKFLOW_PARALLELISM:
        raise WorkflowMetadataError(
            f"`execution_hints.parallelism` in `{metadata_path}` must be one of "
            f"{', '.join(WORKFLOW_PARALLELISM)}."
        )
    background_commands = _require_string_list(
        execution_hints["background_commands"],
        "execution_hints.background_commands",
        allow_empty=True,
    )
    if not all(command.startswith("docmason ") for command in background_commands):
        raise WorkflowMetadataError(
            f"`execution_hints.background_commands` in `{metadata_path}` must use literal "
            "repository `docmason ...` commands."
        )
    if not isinstance(execution_hints["must_return_to_main_agent"], bool):
        raise WorkflowMetadataError(
            f"`execution_hints.must_return_to_main_agent` in `{metadata_path}` must be boolean."
        )

    handoff = payload["handoff"]
    if not isinstance(handoff, dict):
        raise WorkflowMetadataError(f"`handoff` in `{metadata_path}` must be a JSON object.")
    completion_signal = handoff.get("completion_signal")
    if not isinstance(completion_signal, str) or not completion_signal:
        raise WorkflowMetadataError(
            f"`handoff.completion_signal` in `{metadata_path}` must be a non-empty string."
        )
    if "artifacts" in handoff:
        _require_string_list(handoff["artifacts"], "handoff.artifacts", allow_empty=True)
    if "follow_up" in handoff:
        _require_string_list(handoff["follow_up"], "handoff.follow_up", allow_empty=True)

    user_entry_payload = payload.get("user_entry")
    user_entry: dict[str, Any] | None = None
    if user_entry_payload is not None:
        if not isinstance(user_entry_payload, dict):
            raise WorkflowMetadataError(
                f"`user_entry` in `{metadata_path}` must be a JSON object when present."
            )
        primary_user_label = user_entry_payload.get("primary_user_label")
        if not isinstance(primary_user_label, str) or not primary_user_label:
            raise WorkflowMetadataError(
                f"`user_entry.primary_user_label` in `{metadata_path}` must be a non-empty string."
            )
        user_aliases = _require_string_list(
            user_entry_payload.get("user_aliases", []),
            "user_entry.user_aliases",
            allow_empty=True,
        )
        supports_natural_routing = user_entry_payload.get("supports_natural_routing")
        if not isinstance(supports_natural_routing, bool):
            raise WorkflowMetadataError(
                f"`user_entry.supports_natural_routing` in `{metadata_path}` must be boolean."
            )
        shortcut_policy = user_entry_payload.get("platform_shortcut_policy")
        if shortcut_policy not in WORKFLOW_SHORTCUT_POLICIES:
            raise WorkflowMetadataError(
                f"`user_entry.platform_shortcut_policy` in `{metadata_path}` must be one of "
                f"{', '.join(WORKFLOW_SHORTCUT_POLICIES)}."
            )
        natural_entry_examples = _require_string_list(
            user_entry_payload.get("natural_entry_examples", []),
            "user_entry.natural_entry_examples",
            allow_empty=False,
        )
        user_entry = {
            "primary_user_label": primary_user_label,
            "user_aliases": user_aliases,
            "supports_natural_routing": supports_natural_routing,
            "platform_shortcut_policy": shortcut_policy,
            "natural_entry_examples": natural_entry_examples,
        }

    return WorkflowMetadata(
        workflow_id=workflow_id,
        category=category,
        entry_intents=entry_intents,
        required_capabilities=required_capabilities,
        defaults=defaults,
        execution_hints=execution_hints,
        handoff=handoff,
        user_entry=user_entry,
        skill_path=skill_path,
        metadata_path=metadata_path,
    )


def load_workflow_metadata(
    paths: WorkspacePaths,
    *,
    include_operator: bool = False,
) -> list[WorkflowMetadata]:
    """Load workflow metadata in routing order.

    Canonical first-contact routing continues to use `skills/canonical/` only.
    Advanced workflow execution may additionally load `skills/operator/`.
    """
    workflows = [
        load_workflow_metadata_file(directory / "SKILL.md", directory / "workflow.json")
        for directory in paths.workflow_skill_directories(include_operator=include_operator)
    ]
    category_order = {category: index for index, category in enumerate(WORKFLOW_CATEGORIES)}
    return sorted(
        workflows,
        key=lambda workflow: (category_order[workflow.category], workflow.workflow_id),
    )


def render_workflow_routing_markdown(workflows: list[WorkflowMetadata]) -> str:
    """Render the Claude workflow-routing summary from canonical metadata."""
    lines = [
        "# DocMason Workflow Routing",
        "",
        "This file is generated by `docmason sync-adapters --target claude`.",
        "Do not edit it manually. Regenerate it from canonical committed sources.",
        "",
        "It summarizes the execution hints derived from `AGENTS.md`, canonical skills, and",
        "per-workflow `workflow.json` metadata.",
        "",
        "## Execution Policy",
        "",
        "- The main agent owns critical-path reasoning, shared-state mutation, publication, "
        "final answers, and final operator-facing conclusions.",
        "- Deterministic repository commands should run as main-agent or background command "
        "steps once parameters are known.",
        "- Bounded delegation is allowed only for read-only analysis or disjoint per-source work.",
        "- Do not delegate sync publication, validation sign-off, adapter regeneration sign-off, "
        "or final answer integration.",
        "",
    ]

    for category in WORKFLOW_CATEGORIES:
        grouped = [workflow for workflow in workflows if workflow.category == category]
        if not grouped:
            continue
        lines.append(f"## {CATEGORY_TITLES[category]}")
        lines.append("")
        for workflow in grouped:
            default_target = workflow.defaults.get("default_target", "n/a")
            default_mode = workflow.defaults.get("default_mode", "n/a")
            background_commands = workflow.execution_hints["background_commands"] or ["(none)"]
            follow_up = workflow.handoff.get("follow_up", [])
            artifacts = workflow.handoff.get("artifacts", [])
            lines.extend(
                [
                    f"### `{workflow.workflow_id}`",
                    "",
                    f"- Entry intents: {', '.join(workflow.entry_intents)}",
                    f"- Required capabilities: {', '.join(workflow.required_capabilities)}",
                    f"- Mutability: `{workflow.execution_hints['mutability']}`",
                    f"- Parallelism: `{workflow.execution_hints['parallelism']}`",
                    "- Background commands: "
                    + ", ".join(f"`{command}`" for command in background_commands),
                    f"- Default target: `{default_target}`",
                    f"- Default mode: `{default_mode}`",
                    "- Return to main agent: "
                    f"`{workflow.execution_hints['must_return_to_main_agent']}`",
                    f"- Completion signal: {workflow.handoff['completion_signal']}",
                ]
            )
            if workflow.user_entry is not None:
                aliases = workflow.user_entry["user_aliases"] or ["(none)"]
                lines.extend(
                    [
                        f"- Primary user label: `{workflow.user_entry['primary_user_label']}`",
                        "- User aliases: " + ", ".join(f"`{alias}`" for alias in aliases),
                        "- Supports natural routing: "
                        f"`{workflow.user_entry['supports_natural_routing']}`",
                        f"- Shortcut policy: `{workflow.user_entry['platform_shortcut_policy']}`",
                        "- Natural entry examples: "
                        + "; ".join(workflow.user_entry["natural_entry_examples"]),
                    ]
                )
            if artifacts:
                lines.append("- Handoff artifacts: " + ", ".join(f"`{path}`" for path in artifacts))
            if follow_up:
                lines.append(
                    "- Follow-up workflows: " + ", ".join(f"`{item}`" for item in follow_up)
                )
            lines.append("")
    return "\n".join(lines)

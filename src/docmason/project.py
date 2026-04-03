"""Workspace discovery and filesystem-state helpers for DocMason."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

MINIMUM_PYTHON = (3, 11)
BOOTSTRAP_STATE_SCHEMA_VERSION = 5
BOOTSTRAP_STATE_FULL_COMPAT_SCHEMA_FLOOR = 4
MANUAL_WORKSPACE_RECOVERY_DOC = "docs/setup/manual-workspace-recovery.md"


@dataclass(frozen=True)
class SourceTypeDefinition:
    """Shared contract for one supported source-file extension."""

    extension: str
    document_type: str
    support_tier: str
    input_family: str
    requires_pdf_renderer: bool = False
    requires_office_renderer: bool = False


SOURCE_TYPE_DEFINITIONS = (
    SourceTypeDefinition(
        extension="pdf",
        document_type="pdf",
        support_tier="first-class",
        input_family="office-pdf",
        requires_pdf_renderer=True,
    ),
    SourceTypeDefinition(
        extension="pptx",
        document_type="pptx",
        support_tier="first-class",
        input_family="office-pdf",
        requires_office_renderer=True,
    ),
    SourceTypeDefinition(
        extension="ppt",
        document_type="pptx",
        support_tier="first-class",
        input_family="office-pdf",
        requires_office_renderer=True,
    ),
    SourceTypeDefinition(
        extension="docx",
        document_type="docx",
        support_tier="first-class",
        input_family="office-pdf",
        requires_office_renderer=True,
    ),
    SourceTypeDefinition(
        extension="doc",
        document_type="docx",
        support_tier="first-class",
        input_family="office-pdf",
        requires_office_renderer=True,
    ),
    SourceTypeDefinition(
        extension="xlsx",
        document_type="xlsx",
        support_tier="first-class",
        input_family="office-pdf",
        requires_office_renderer=True,
    ),
    SourceTypeDefinition(
        extension="xls",
        document_type="xlsx",
        support_tier="first-class",
        input_family="office-pdf",
        requires_office_renderer=True,
    ),
    SourceTypeDefinition(
        extension="md",
        document_type="markdown",
        support_tier="first-class",
        input_family="first-class-text",
    ),
    SourceTypeDefinition(
        extension="markdown",
        document_type="markdown",
        support_tier="first-class",
        input_family="first-class-text",
    ),
    SourceTypeDefinition(
        extension="txt",
        document_type="plaintext",
        support_tier="first-class",
        input_family="first-class-text",
    ),
    SourceTypeDefinition(
        extension="eml",
        document_type="email",
        support_tier="first-class",
        input_family="first-class-email",
    ),
    SourceTypeDefinition(
        extension="mdx",
        document_type="mdx",
        support_tier="lightweight-compatible",
        input_family="lightweight-text",
    ),
    SourceTypeDefinition(
        extension="yaml",
        document_type="yaml",
        support_tier="lightweight-compatible",
        input_family="lightweight-text",
    ),
    SourceTypeDefinition(
        extension="yml",
        document_type="yaml",
        support_tier="lightweight-compatible",
        input_family="lightweight-text",
    ),
    SourceTypeDefinition(
        extension="tex",
        document_type="tex",
        support_tier="lightweight-compatible",
        input_family="lightweight-text",
    ),
    SourceTypeDefinition(
        extension="csv",
        document_type="csv",
        support_tier="lightweight-compatible",
        input_family="lightweight-text",
    ),
    SourceTypeDefinition(
        extension="tsv",
        document_type="tsv",
        support_tier="lightweight-compatible",
        input_family="lightweight-text",
    ),
)
SOURCE_TYPE_DEFINITIONS_BY_EXTENSION = {
    definition.extension: definition for definition in SOURCE_TYPE_DEFINITIONS
}
SUPPORTED_INPUTS = tuple(definition.extension for definition in SOURCE_TYPE_DEFINITIONS)
SUPPORTED_DOCUMENT_TYPES = tuple(
    sorted(
        {
            definition.document_type
            for definition in SOURCE_TYPE_DEFINITIONS
            if definition.document_type != "interaction"
        }
    )
)
OFFICE_PDF_INPUTS = tuple(
    definition.extension
    for definition in SOURCE_TYPE_DEFINITIONS
    if definition.input_family == "office-pdf"
)
FIRST_CLASS_TEXT_INPUTS = tuple(
    definition.extension
    for definition in SOURCE_TYPE_DEFINITIONS
    if definition.input_family == "first-class-text"
)
LIGHTWEIGHT_TEXT_INPUTS = tuple(
    definition.extension
    for definition in SOURCE_TYPE_DEFINITIONS
    if definition.input_family == "lightweight-text"
)
FIRST_CLASS_EMAIL_INPUTS = tuple(
    definition.extension
    for definition in SOURCE_TYPE_DEFINITIONS
    if definition.input_family == "first-class-email"
)
SUPPORTED_STAGES = (
    "foundation-only",
    "workspace-bootstrapped",
    "adapter-ready",
    "control-plane-pending-confirmation",
    "knowledge-base-invalid",
    "knowledge-base-present",
    "knowledge-base-stale",
)


@dataclass(frozen=True)
class WorkspacePaths:
    """Canonical filesystem locations for a DocMason workspace."""

    root: Path

    @property
    def distribution_manifest_path(self) -> Path:
        return self.root / "distribution-manifest.json"

    @property
    def planning_dir(self) -> Path:
        return self.root / "planning"

    @property
    def source_dir(self) -> Path:
        return self.root / "original_doc"

    @property
    def knowledge_base_dir(self) -> Path:
        return self.root / "knowledge_base"

    @property
    def knowledge_base_staging_dir(self) -> Path:
        return self.knowledge_base_dir / "staging"

    @property
    def knowledge_base_current_dir(self) -> Path:
        return self.knowledge_base_dir / "current"

    @property
    def knowledge_base_published_dir(self) -> Path:
        return self.knowledge_base_dir / ".published"

    @property
    def knowledge_base_versions_dir(self) -> Path:
        return self.knowledge_base_dir / "versions"

    @property
    def runtime_dir(self) -> Path:
        return self.root / "runtime"

    @property
    def adapters_dir(self) -> Path:
        return self.root / "adapters"

    @property
    def canonical_skills_dir(self) -> Path:
        return self.root / "skills" / "canonical"

    @property
    def operator_skills_dir(self) -> Path:
        return self.root / "skills" / "operator"

    @property
    def optional_skills_dir(self) -> Path:
        return self.root / "skills" / "optional"

    @property
    def docmason_dir(self) -> Path:
        return self.root / ".docmason"

    @property
    def toolchain_dir(self) -> Path:
        return self.docmason_dir / "toolchain"

    @property
    def toolchain_python_dir(self) -> Path:
        return self.toolchain_dir / "python"

    @property
    def toolchain_python_installs_dir(self) -> Path:
        return self.toolchain_python_dir / "installs"

    @property
    def toolchain_python_current_dir(self) -> Path:
        return self.toolchain_python_dir / "current"

    @property
    def toolchain_cache_dir(self) -> Path:
        return self.toolchain_dir / "cache"

    @property
    def toolchain_uv_cache_dir(self) -> Path:
        return self.toolchain_cache_dir / "uv"

    @property
    def toolchain_pip_cache_dir(self) -> Path:
        return self.toolchain_cache_dir / "pip"

    @property
    def toolchain_bootstrap_dir(self) -> Path:
        return self.toolchain_dir / "bootstrap"

    @property
    def toolchain_bootstrap_venv_dir(self) -> Path:
        return self.toolchain_bootstrap_dir / "venv"

    @property
    def toolchain_bootstrap_python(self) -> Path:
        return self.toolchain_bootstrap_venv_dir / "bin" / "python"

    @property
    def toolchain_bootstrap_uv(self) -> Path:
        return self.toolchain_bootstrap_venv_dir / "bin" / "uv"

    @property
    def toolchain_state_dir(self) -> Path:
        return self.toolchain_dir / "state"

    @property
    def toolchain_manifest_path(self) -> Path:
        return self.toolchain_state_dir / "toolchain.json"

    @property
    def toolchain_repair_history_path(self) -> Path:
        return self.toolchain_state_dir / "repair-history.jsonl"

    @property
    def venv_dir(self) -> Path:
        return self.root / ".venv"

    @property
    def venv_python(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def venv_docmason(self) -> Path:
        return self.venv_dir / "bin" / "docmason"

    @property
    def venv_pyvenv_cfg(self) -> Path:
        return self.venv_dir / "pyvenv.cfg"

    @property
    def bootstrap_state_path(self) -> Path:
        return self.runtime_dir / "bootstrap_state.json"

    @property
    def source_index_path(self) -> Path:
        return self.runtime_dir / "source_index.json"

    @property
    def sync_state_path(self) -> Path:
        return self.runtime_dir / "sync_state.json"

    @property
    def dependency_state_path(self) -> Path:
        return self.runtime_dir / "dependency_state.json"

    @property
    def control_plane_dir(self) -> Path:
        return self.runtime_dir / "control_plane"

    @property
    def workspace_state_path(self) -> Path:
        return self.control_plane_dir / "workspace_state.json"

    @property
    def shared_jobs_dir(self) -> Path:
        return self.control_plane_dir / "shared_jobs"

    @property
    def shared_jobs_index_path(self) -> Path:
        return self.shared_jobs_dir / "index.json"

    @property
    def snapshot_pins_path(self) -> Path:
        return self.control_plane_dir / "snapshot_pins.json"

    @property
    def snapshot_retention_state_path(self) -> Path:
        return self.control_plane_dir / "snapshot_retention.json"

    @property
    def publish_ledger_path(self) -> Path:
        return self.control_plane_dir / "publish_ledger.jsonl"

    @property
    def projection_state_path(self) -> Path:
        return self.control_plane_dir / "projection_state.json"

    @property
    def runs_dir(self) -> Path:
        return self.runtime_dir / "runs"

    @property
    def coordination_dir(self) -> Path:
        return self.runtime_dir / "coordination"

    @property
    def state_dir(self) -> Path:
        return self.runtime_dir / "state"

    @property
    def live_conversations_dir(self) -> Path:
        return self.state_dir / "conversations"

    @property
    def native_ledger_dir(self) -> Path:
        return self.state_dir / "native-ledger"

    @property
    def host_identity_bindings_path(self) -> Path:
        return self.state_dir / "host-identity-bindings.json"

    @property
    def release_client_state_path(self) -> Path:
        return self.state_dir / "release-client.json"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def query_sessions_dir(self) -> Path:
        return self.logs_dir / "query-sessions"

    @property
    def retrieval_traces_dir(self) -> Path:
        return self.logs_dir / "retrieval-traces"

    @property
    def turn_artifact_index_dir(self) -> Path:
        return self.logs_dir / "turn-artifact-index"

    @property
    def conversations_dir(self) -> Path:
        return self.live_conversations_dir

    @property
    def conversation_projections_dir(self) -> Path:
        return self.logs_dir / "conversations"

    @property
    def review_logs_dir(self) -> Path:
        return self.logs_dir / "review"

    @property
    def review_requests_dir(self) -> Path:
        return self.review_logs_dir / "requests"

    @property
    def review_summary_path(self) -> Path:
        return self.review_logs_dir / "summary.json"

    @property
    def benchmark_candidates_path(self) -> Path:
        return self.review_logs_dir / "benchmark-candidates.json"

    @property
    def answer_history_index_path(self) -> Path:
        return self.review_logs_dir / "answer-history-index.json"

    @property
    def eval_dir(self) -> Path:
        return self.runtime_dir / "eval"

    @property
    def eval_benchmarks_dir(self) -> Path:
        return self.eval_dir / "benchmarks"

    @property
    def eval_broad_benchmark_dir(self) -> Path:
        return self.eval_benchmarks_dir / "broad"

    @property
    def eval_regression_benchmark_dir(self) -> Path:
        return self.eval_benchmarks_dir / "regression"

    @property
    def eval_drafts_dir(self) -> Path:
        return self.eval_dir / "drafts"

    @property
    def eval_candidate_drafts_dir(self) -> Path:
        return self.eval_drafts_dir / "candidates"

    @property
    def evaluation_runs_dir(self) -> Path:
        return self.eval_dir / "runs"

    @property
    def user_feedback_dir(self) -> Path:
        return self.eval_dir / "feedback"

    @property
    def eval_reviews_dir(self) -> Path:
        return self.eval_dir / "reviews"

    @property
    def eval_requests_dir(self) -> Path:
        return self.eval_dir / "requests"

    @property
    def eval_request_path(self) -> Path:
        return self.eval_requests_dir / "current.json"

    @property
    def answers_dir(self) -> Path:
        return self.runtime_dir / "answers"

    @property
    def agent_work_dir(self) -> Path:
        return self.runtime_dir / "agent-work"

    @property
    def active_conversation_path(self) -> Path:
        return self.state_dir / "active_conversation.json"

    @property
    def legacy_active_conversation_path(self) -> Path:
        return self.runtime_dir / "active_conversation.json"

    @property
    def interaction_ingest_dir(self) -> Path:
        return self.runtime_dir / "interaction-ingest"

    @property
    def interaction_connectors_dir(self) -> Path:
        return self.interaction_ingest_dir / "connectors"

    @property
    def interaction_connector_manifest_path(self) -> Path:
        return self.interaction_connectors_dir / "manifest.json"

    @property
    def codex_connector_manifest_path(self) -> Path:
        return self.interaction_connectors_dir / "codex.json"

    @property
    def claude_code_connector_manifest_path(self) -> Path:
        return self.interaction_connectors_dir / "claude-code.json"

    @property
    def claude_code_mirror_root(self) -> Path:
        return self.interaction_ingest_dir / "claude-code"

    def claude_code_mirror_path(self, session_id: str) -> Path:
        """Return the JSONL mirror file path for a Claude Code session."""
        return self.claude_code_mirror_root / f"{session_id}.jsonl"

    @property
    def interaction_attachments_dir(self) -> Path:
        return self.interaction_ingest_dir / "attachments"

    @property
    def interaction_entries_dir(self) -> Path:
        return self.interaction_ingest_dir / "entries"

    @property
    def interaction_overlay_dir(self) -> Path:
        return self.interaction_ingest_dir / "overlay"

    @property
    def interaction_overlay_manifest_path(self) -> Path:
        return self.interaction_overlay_dir / "manifest.json"

    @property
    def interaction_overlay_source_records_path(self) -> Path:
        return self.interaction_overlay_dir / "source_records.json"

    @property
    def interaction_overlay_unit_records_path(self) -> Path:
        return self.interaction_overlay_dir / "unit_records.json"

    @property
    def interaction_overlay_graph_edges_path(self) -> Path:
        return self.interaction_overlay_dir / "graph_edges.json"

    @property
    def interaction_overlay_source_provenance_path(self) -> Path:
        return self.interaction_overlay_dir / "source_provenance.json"

    @property
    def interaction_overlay_unit_provenance_path(self) -> Path:
        return self.interaction_overlay_dir / "unit_provenance.json"

    @property
    def interaction_overlay_relation_index_path(self) -> Path:
        return self.interaction_overlay_dir / "relation_index.json"

    @property
    def interaction_overlay_knowledge_consumers_path(self) -> Path:
        return self.interaction_overlay_dir / "knowledge_consumers.json"

    @property
    def interaction_promotion_queue_path(self) -> Path:
        return self.interaction_ingest_dir / "promotion-queue.json"

    @property
    def interaction_reconciliation_state_path(self) -> Path:
        return self.interaction_ingest_dir / "reconciliation-state.json"

    @property
    def usage_history_path(self) -> Path:
        return self.logs_dir / "usage-history.jsonl"

    @property
    def agents_dir(self) -> Path:
        return self.root / ".agents"

    @property
    def repo_skill_shim_dir(self) -> Path:
        return self.agents_dir / "skills"

    @property
    def agents_path(self) -> Path:
        return self.root / "AGENTS.md"

    @property
    def claude_dir(self) -> Path:
        return self.root / ".claude"

    @property
    def claude_skill_shim_dir(self) -> Path:
        return self.claude_dir / "skills"

    @property
    def claude_adapter_dir(self) -> Path:
        return self.adapters_dir / "claude"

    @property
    def claude_project_memory_path(self) -> Path:
        return self.claude_adapter_dir / "project-memory.md"

    @property
    def claude_workflow_routing_path(self) -> Path:
        return self.claude_adapter_dir / "workflow-routing.md"

    def canonical_skill_directories(self) -> list[Path]:
        if not self.canonical_skills_dir.exists():
            return []
        return sorted(
            path.parent for path in self.canonical_skills_dir.rglob("SKILL.md") if path.is_file()
        )

    def operator_skill_directories(self) -> list[Path]:
        if not self.operator_skills_dir.exists():
            return []
        return sorted(
            path.parent for path in self.operator_skills_dir.rglob("SKILL.md") if path.is_file()
        )

    def optional_skill_directories(self) -> list[Path]:
        manifest = self.root / "sample_corpus" / "ico-gcs" / "manifest.json"
        skill_dir = self.optional_skills_dir / "public-sample-workspace"
        skill_path = skill_dir / "SKILL.md"
        if skill_path.exists() and manifest.exists():
            return [skill_dir]
        return []

    def agent_skill_directories(
        self,
        *,
        include_operator: bool = False,
        include_optional: bool = False,
    ) -> list[Path]:
        directories = list(self.canonical_skill_directories())
        if include_operator:
            directories.extend(self.operator_skill_directories())
        if include_optional:
            directories.extend(self.optional_skill_directories())
        return sorted(dict.fromkeys(directories))

    def workflow_skill_directories(self, *, include_operator: bool = False) -> list[Path]:
        return self.agent_skill_directories(include_operator=include_operator)

    def canonical_skill_files(self) -> list[Path]:
        return [directory / "SKILL.md" for directory in self.canonical_skill_directories()]

    def canonical_workflow_metadata_files(self) -> list[Path]:
        return [directory / "workflow.json" for directory in self.canonical_skill_directories()]

    def canonical_skill_reference_files(self) -> list[Path]:
        files: list[Path] = []
        for directory in self.canonical_skill_directories():
            references_dir = directory / "references"
            if not references_dir.exists():
                continue
            files.extend(path for path in references_dir.rglob("*") if path.is_file())
        return sorted(files)

    def operator_skill_files(self) -> list[Path]:
        return [directory / "SKILL.md" for directory in self.operator_skill_directories()]

    def operator_workflow_metadata_files(self) -> list[Path]:
        return [directory / "workflow.json" for directory in self.operator_skill_directories()]

    def optional_skill_files(self) -> list[Path]:
        return [directory / "SKILL.md" for directory in self.optional_skill_directories()]

    def claude_adapter_source_inputs(self) -> list[Path]:
        return [
            *self.canonical_skill_files(),
            *self.canonical_workflow_metadata_files(),
            *self.canonical_skill_reference_files(),
        ]

    def generated_claude_core_files(self) -> list[Path]:
        return [
            self.claude_project_memory_path,
            self.claude_workflow_routing_path,
        ]

    def generated_claude_files(self) -> list[Path]:
        return [
            *self.generated_claude_core_files(),
            self.claude_skill_shim_dir,
        ]

    @property
    def staging_catalog_path(self) -> Path:
        return self.knowledge_base_staging_dir / "catalog.json"

    @property
    def staging_coverage_manifest_path(self) -> Path:
        return self.knowledge_base_staging_dir / "coverage_manifest.json"

    @property
    def staging_graph_edges_path(self) -> Path:
        return self.knowledge_base_staging_dir / "graph_edges.json"

    @property
    def staging_pending_work_path(self) -> Path:
        return self.knowledge_base_staging_dir / "pending_work.json"

    @property
    def staging_hybrid_work_path(self) -> Path:
        return self.knowledge_base_staging_dir / "hybrid_work.json"

    @property
    def staging_validation_report_path(self) -> Path:
        return self.knowledge_base_staging_dir / "validation_report.json"

    @property
    def staging_publish_manifest_path(self) -> Path:
        return self.knowledge_base_staging_dir / "publish_manifest.json"

    @property
    def current_catalog_path(self) -> Path:
        return self.knowledge_base_current_dir / "catalog.json"

    @property
    def current_coverage_manifest_path(self) -> Path:
        return self.knowledge_base_current_dir / "coverage_manifest.json"

    @property
    def current_graph_edges_path(self) -> Path:
        return self.knowledge_base_current_dir / "graph_edges.json"

    @property
    def current_validation_report_path(self) -> Path:
        return self.knowledge_base_current_dir / "validation_report.json"

    @property
    def current_publish_manifest_path(self) -> Path:
        return self.knowledge_base_current_dir / "publish_manifest.json"

    @property
    def current_publish_pointer_path(self) -> Path:
        return self.knowledge_base_dir / "current-pointer.json"

    def knowledge_published_root_dir(self, snapshot_id: str) -> Path:
        return self.knowledge_base_published_dir / snapshot_id

    def knowledge_version_dir(self, snapshot_id: str) -> Path:
        return self.knowledge_base_versions_dir / snapshot_id

    def knowledge_target_dir(self, target: str) -> Path:
        if target == "staging":
            return self.knowledge_base_staging_dir
        if target == "current":
            return self.knowledge_base_current_dir
        raise ValueError(f"Unsupported knowledge-base target: {target}")

    def retrieval_dir(self, target: str) -> Path:
        return self.knowledge_target_dir(target) / "retrieval"

    def hybrid_work_path(self, target: str) -> Path:
        return self.knowledge_target_dir(target) / "hybrid_work.json"

    def retrieval_manifest_path(self, target: str) -> Path:
        return self.retrieval_dir(target) / "manifest.json"

    def retrieval_source_records_path(self, target: str) -> Path:
        return self.retrieval_dir(target) / "source_records.json"

    def retrieval_unit_records_path(self, target: str) -> Path:
        return self.retrieval_dir(target) / "unit_records.json"

    def retrieval_artifact_records_path(self, target: str) -> Path:
        return self.retrieval_dir(target) / "artifact_records.json"

    def trace_dir(self, target: str) -> Path:
        return self.knowledge_target_dir(target) / "trace"

    def trace_manifest_path(self, target: str) -> Path:
        return self.trace_dir(target) / "manifest.json"

    def trace_source_provenance_path(self, target: str) -> Path:
        return self.trace_dir(target) / "source_provenance.json"

    def trace_unit_provenance_path(self, target: str) -> Path:
        return self.trace_dir(target) / "unit_provenance.json"

    def trace_relation_index_path(self, target: str) -> Path:
        return self.trace_dir(target) / "relation_index.json"

    def trace_knowledge_consumers_path(self, target: str) -> Path:
        return self.trace_dir(target) / "knowledge_consumers.json"

    def interaction_target_dir(self, target: str) -> Path:
        return self.knowledge_target_dir(target) / "interaction"

    def interaction_manifest_path(self, target: str) -> Path:
        return self.interaction_target_dir(target) / "manifest.json"

    def interaction_memories_dir(self, target: str) -> Path:
        return self.interaction_target_dir(target) / "memories"

    def eval_benchmark_dir(self, suite: str) -> Path:
        if suite == "broad":
            return self.eval_broad_benchmark_dir
        if suite == "regression":
            return self.eval_regression_benchmark_dir
        raise ValueError(f"Unsupported evaluation suite `{suite}`.")

    def eval_suite_path(self, suite: str) -> Path:
        return self.eval_benchmark_dir(suite) / "suite.json"

    def eval_rubric_path(self, suite: str) -> Path:
        return self.eval_benchmark_dir(suite) / "rubric.json"

    def eval_judge_trials_path(self, suite: str) -> Path:
        return self.eval_benchmark_dir(suite) / "judge-trials.json"

    def eval_baseline_path(self, suite: str) -> Path:
        return self.eval_benchmark_dir(suite) / "baseline.json"

    def eval_candidate_draft_path(self, candidate_id: str) -> Path:
        return self.eval_candidate_drafts_dir / f"{candidate_id}.json"

    def eval_review_json_path(self, review_id: str) -> Path:
        return self.eval_reviews_dir / f"{review_id}.json"

    def eval_review_markdown_path(self, review_id: str) -> Path:
        return self.eval_reviews_dir / f"{review_id}.md"


def locate_workspace(start: Path | None = None) -> WorkspacePaths:
    """Locate the repository root from the current directory or one of its parents."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists() and (candidate / "docmason.yaml").exists():
            return WorkspacePaths(root=candidate)
    raise FileNotFoundError(
        "Could not locate a DocMason workspace. Expected pyproject.toml and docmason.yaml."
    )


def ensure_json_parent(path: Path) -> None:
    """Ensure that the parent directory for a JSON state file exists."""
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk and reject non-object payloads."""
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return cast(dict[str, Any], payload)
    raise ValueError(f"Expected a JSON object in {path}.")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Persist a JSON object with stable formatting."""
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def atomic_write_text(path: Path, content: str) -> None:
    """Persist one text payload atomically using same-directory replace semantics."""
    ensure_json_parent(path)
    temp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON payload to a JSONL log file with atomic append semantics."""
    ensure_json_parent(path)
    encoded = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)


def isoformat_timestamp(timestamp: float | None) -> str | None:
    """Convert a UNIX timestamp into a UTC ISO 8601 string."""
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


def is_visible_file(path: Path) -> bool:
    """Return ``True`` for non-hidden files that should count as workspace content."""
    return path.is_file() and not any(part.startswith(".") for part in path.parts)


def list_visible_files(directory: Path) -> list[Path]:
    """List non-hidden files beneath a directory in deterministic order."""
    if not directory.exists():
        return []
    return sorted(path for path in directory.rglob("*") if is_visible_file(path))


def enumerate_live_corpus_paths(paths: WorkspacePaths) -> list[Path]:
    """Enumerate live private corpus files under `original_doc/` without relying on VCS state."""
    return list_visible_files(paths.source_dir)


def source_type_definition(extension: str | None) -> SourceTypeDefinition | None:
    """Return the source-type definition for one file extension."""
    if extension is None:
        return None
    return SOURCE_TYPE_DEFINITIONS_BY_EXTENSION.get(extension.lower().lstrip("."))


def ignored_live_corpus_path(path: Path) -> bool:
    """Return whether one live corpus path should be ignored before source typing."""
    definition = source_type_definition(path.suffix)
    if definition is None:
        return False
    return bool(definition.requires_office_renderer and path.name.startswith("~$"))


def source_type_definition_for_path(path: Path) -> SourceTypeDefinition | None:
    """Return the source-type definition for one filesystem path."""
    if ignored_live_corpus_path(path):
        return None
    return source_type_definition(path.suffix)


def supported_input_tiers() -> dict[str, list[str]]:
    """Return the public tiered supported-input summary."""
    return {
        "office_pdf": list(OFFICE_PDF_INPUTS),
        "first_class_text": list(FIRST_CLASS_TEXT_INPUTS),
        "first_class_email": list(FIRST_CLASS_EMAIL_INPUTS),
        "lightweight_text": list(LIGHTWEIGHT_TEXT_INPUTS),
    }


def supported_source_documents(paths: WorkspacePaths) -> list[Path]:
    """Return source documents that match the current supported input set."""
    return [
        path
        for path in enumerate_live_corpus_paths(paths)
        if source_type_definition_for_path(path) is not None
    ]


def count_source_documents(paths: WorkspacePaths) -> dict[str, int]:
    """Count supported source documents by file extension."""
    counts = {suffix: 0 for suffix in SUPPORTED_INPUTS}
    for path in supported_source_documents(paths):
        counts[path.suffix.lower().lstrip(".")] += 1
    return counts


def latest_mtime(file_paths: Iterable[Path]) -> float | None:
    """Return the newest modification time among the provided existing files."""
    mtimes = [path.stat().st_mtime for path in file_paths if path.exists()]
    return max(mtimes) if mtimes else None


def latest_mtime_ns(file_paths: Iterable[Path]) -> int | None:
    """Return the newest nanosecond modification time among the provided existing files."""
    mtimes = [path.stat().st_mtime_ns for path in file_paths if path.exists()]
    return max(mtimes) if mtimes else None


def source_inventory_signature(paths: WorkspacePaths) -> str:
    """Return a deterministic signature of the current supported source inventory."""
    digest = hashlib.sha256()
    for path in supported_source_documents(paths):
        relative = str(path.relative_to(paths.root))
        stat = path.stat()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def source_index(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the persisted source identity index for the workspace."""
    return read_json(paths.source_index_path)


def sync_state(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the persisted sync state for the workspace."""
    return read_json(paths.sync_state_path)


def dependency_state(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the persisted dependency state for the workspace."""
    return read_json(paths.dependency_state_path)


def _resolved_lane_b_follow_up_summary(
    paths: WorkspacePaths,
    state: dict[str, Any],
) -> dict[str, Any]:
    def _apply_result_counts(summary_payload: dict[str, Any], result: dict[str, Any]) -> None:
        summary_payload["covered_unit_count"] = int(
            result.get("covered_unit_count", summary_payload.get("covered_unit_count", 0)) or 0
        )
        summary_payload["blocked_unit_count"] = int(
            result.get("blocked_unit_count", summary_payload.get("blocked_unit_count", 0)) or 0
        )
        summary_payload["remaining_unit_count"] = int(
            result.get("remaining_unit_count", summary_payload.get("remaining_unit_count", 0)) or 0
        )
        summary_payload["selected_unit_count"] = int(
            result.get("selected_unit_count", summary_payload.get("selected_unit_count", 0)) or 0
        )

    raw_summary = state.get("lane_b_follow_up_summary")
    if not isinstance(raw_summary, dict) or not raw_summary:
        return {}
    summary = dict(raw_summary)
    job_id = summary.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        return summary

    from .control_plane import (
        load_shared_job,
        load_shared_job_result,
        shared_job_inactive_owner_reason,
        shared_job_is_active,
        shared_job_is_settled,
    )

    manifest = load_shared_job(paths, job_id)
    if not manifest:
        summary["state"] = "missing-shared-job"
        return summary
    if shared_job_is_active(manifest):
        if shared_job_inactive_owner_reason(paths, manifest) is not None:
            summary["state"] = "blocked"
            return summary
        summary["state"] = "running"
        return summary

    status = str(manifest.get("status") or "")
    if shared_job_is_settled(manifest):
        result_payload = load_shared_job_result(paths, job_id)
        result = (
            dict(result_payload.get("result", {}))
            if isinstance(result_payload.get("result"), dict)
            else {}
        )
        if result:
            _apply_result_counts(summary, result)
        if status == "completed":
            summary["state"] = "covered"
        elif status:
            summary["state"] = status
        return summary

    if status:
        summary["state"] = status
    return summary


def knowledge_base_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Summarize knowledge-base presence and freshness from the filesystem."""
    current_files = list_visible_files(paths.knowledge_base_current_dir)
    staging_files = list_visible_files(paths.knowledge_base_staging_dir)
    state = sync_state(paths)
    current_pointer = read_json(paths.current_publish_pointer_path)
    current_manifest = read_json(paths.current_publish_manifest_path)
    from .versioning import publish_storage_summary, storage_lifecycle_summary

    publish_storage = publish_storage_summary(paths)
    storage_lifecycle = storage_lifecycle_summary(paths)
    validation_report = read_json(paths.current_validation_report_path)
    if not validation_report:
        validation_report = read_json(paths.staging_validation_report_path)
    validation_status = validation_report.get("status") or state.get(
        "last_validation_status", "not-run"
    )
    present = bool(current_files)
    current_signature = source_inventory_signature(paths)
    published_signature = state.get("published_source_signature") or current_manifest.get(
        "published_source_signature"
    )
    stale = bool(present and published_signature and published_signature != current_signature)
    stale_reason = "source-drift" if stale else None
    return {
        "path": str(paths.knowledge_base_current_dir.relative_to(paths.root)),
        "present": present,
        "last_updated": isoformat_timestamp(latest_mtime(current_files)),
        "staging_present": bool(staging_files),
        "staging_path": str(paths.knowledge_base_staging_dir.relative_to(paths.root)),
        "validation_status": validation_status,
        "last_sync_at": state.get("last_sync_at"),
        "last_publish_at": state.get("last_publish_at") or current_manifest.get("published_at"),
        "stale": stale,
        "stale_reason": stale_reason,
        "source_inventory_signature": current_signature,
        "published_source_signature": published_signature,
        "publish_model": publish_storage.get("publish_model", "single-current"),
        "current_snapshot_id": current_manifest.get("snapshot_id")
        or current_pointer.get("snapshot_id"),
        "current_publish_root_path": current_pointer.get("published_root_path"),
        "published_root_count": publish_storage.get("published_root_count", 0),
        "publish_ledger_count": publish_storage.get("publish_ledger_count", 0),
        "recent_publish_snapshot_ids": publish_storage.get("recent_publish_snapshot_ids", []),
        "legacy_archive_detected": publish_storage.get("legacy_archive_detected", False),
        "legacy_archive_version_count": publish_storage.get("legacy_archive_version_count", 0),
        "legacy_runtime_files": publish_storage.get("legacy_runtime_files", []),
        "legacy_archive_note": publish_storage.get("legacy_archive_note"),
        "storage_lifecycle": storage_lifecycle,
        "last_sync_rebuild_telemetry": (
            dict(state.get("rebuild_telemetry", {}))
            if isinstance(state.get("rebuild_telemetry"), dict)
            else {}
        ),
        "lane_b_follow_up": _resolved_lane_b_follow_up_summary(paths, state),
    }


def adapter_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Summarize generated adapter presence and freshness from canonical inputs."""
    generated_files = paths.generated_claude_core_files()
    present = all(path.exists() for path in generated_files)
    source_inputs = [path for path in paths.claude_adapter_source_inputs() if path.exists()]
    newest_source = latest_mtime(source_inputs)
    oldest_generated = (
        min(path.stat().st_mtime for path in generated_files if path.exists()) if present else None
    )
    last_updated = latest_mtime(generated_files)
    stale = bool(
        present and newest_source and oldest_generated and newest_source > oldest_generated
    )
    skill_shims_present = paths.claude_skill_shim_dir.exists() and any(
        paths.claude_skill_shim_dir.iterdir()
    )
    return {
        "claude": {
            "path": str(paths.claude_project_memory_path.relative_to(paths.root)),
            "present": present,
            "last_updated": isoformat_timestamp(last_updated),
            "stale": stale,
            "generated_files": [str(path.relative_to(paths.root)) for path in generated_files],
            "skill_shims_path": str(paths.claude_skill_shim_dir.relative_to(paths.root)),
            "skill_shims_present": skill_shims_present,
            "skill_shims_required": False,
        }
    }


def bootstrap_state(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the persisted bootstrap state for the workspace."""
    return read_json(paths.bootstrap_state_path)


def manual_workspace_recovery_doc() -> str:
    """Return the tracked deep-fallback document for manual workspace setup and repair."""
    return MANUAL_WORKSPACE_RECOVERY_DOC


def source_runtime_requirements(paths: WorkspacePaths) -> dict[str, bool]:
    """Summarize which renderer capabilities the current source corpus requires."""
    requires_office_renderer = False
    requires_pdf_renderer = False
    for path in supported_source_documents(paths):
        definition = source_type_definition_for_path(path)
        if definition is None:
            continue
        requires_office_renderer = requires_office_renderer or definition.requires_office_renderer
        requires_pdf_renderer = requires_pdf_renderer or definition.requires_pdf_renderer
    return {
        "requires_office_renderer": requires_office_renderer,
        "requires_pdf_renderer": requires_pdf_renderer,
    }


def cached_bootstrap_readiness(
    paths: WorkspacePaths,
    *,
    require_sync_capability: bool = False,
) -> dict[str, Any]:
    """Evaluate the lightweight cached bootstrap marker without running deep diagnostics."""
    from .toolchain import inspect_toolchain, toolchain_repair_detail

    state = bootstrap_state(paths)
    manual_doc = manual_workspace_recovery_doc()
    if not state:
        return {
            "ready": False,
            "reason": "missing-bootstrap-state",
            "detail": "No cached bootstrap marker is recorded yet.",
            "manual_recovery_doc": manual_doc,
            "state": {},
        }

    schema_version = int(state.get("schema_version", 0) or 0)
    if schema_version < BOOTSTRAP_STATE_FULL_COMPAT_SCHEMA_FLOOR:
        return {
            "ready": False,
            "reason": (
                "legacy-bootstrap-state-sync-capability-unknown"
                if require_sync_capability
                else "legacy-bootstrap-state"
            ),
            "detail": (
                "The cached bootstrap marker predates the current toolchain and readiness "
                "contract. Rebuild or repair the repo-local toolchain before continuing."
            ),
            "manual_recovery_doc": manual_doc,
            "state": state,
        }

    expected_root = str(paths.root.resolve())
    recorded_root = str(state.get("workspace_root") or "")
    if recorded_root != expected_root:
        return {
            "ready": False,
            "reason": "workspace-root-drift",
            "detail": (
                "The cached bootstrap marker belongs to a different workspace root and must be "
                "repaired after the repository move."
            ),
            "manual_recovery_doc": manual_doc,
            "state": state,
        }

    toolchain = inspect_toolchain(
        paths,
        bootstrap_state=state,
        editable_install=(
            bool(state.get("editable_install"))
            if isinstance(state.get("editable_install"), bool)
            else None
        ),
    )
    venv_health = str(toolchain.get("venv_health") or "")
    if venv_health == "missing":
        return {
            "ready": False,
            "reason": "missing-venv",
            "detail": "The repo-local virtual environment interpreter is missing.",
            "manual_recovery_doc": manual_doc,
            "state": state,
            "toolchain": toolchain,
        }
    if venv_health == "broken-symlink":
        return {
            "ready": False,
            "reason": "broken-venv-symlink",
            "detail": toolchain_repair_detail(toolchain),
            "manual_recovery_doc": manual_doc,
            "state": state,
            "toolchain": toolchain,
        }

    if toolchain.get("isolation_grade") != "self-contained":
        return {
            "ready": False,
            "reason": str(toolchain.get("repair_reason") or "environment-not-ready"),
            "detail": toolchain_repair_detail(toolchain),
            "manual_recovery_doc": manual_doc,
            "state": state,
            "toolchain": toolchain,
        }

    machine_baseline_ready = bool(state.get("machine_baseline_ready", True))
    machine_baseline_status = str(state.get("machine_baseline_status") or "")
    if not machine_baseline_ready and machine_baseline_status not in {"", "not-applicable"}:
        return {
            "ready": False,
            "reason": machine_baseline_status,
            "detail": (
                str(state.get("host_access_guidance") or "")
                or str(state.get("machine_baseline_status") or "")
                or "The native machine baseline is not ready yet."
            ),
            "manual_recovery_doc": manual_doc,
            "state": state,
            "toolchain": toolchain,
        }

    if require_sync_capability:
        requirements = source_runtime_requirements(paths)
        if requirements["requires_office_renderer"] and not bool(
            state.get("office_renderer_ready")
        ):
            return {
                "ready": False,
                "reason": "office-renderer-required",
                "detail": (
                    "The current source corpus needs LibreOffice-backed rendering before sync can "
                    "run safely."
                ),
                "manual_recovery_doc": manual_doc,
                "state": state,
                "toolchain": toolchain,
            }
        if requirements["requires_pdf_renderer"] and not bool(state.get("pdf_renderer_ready")):
            return {
                "ready": False,
                "reason": "pdf-renderer-required",
                "detail": (
                    "The current source corpus needs the configured PDF renderer before sync can "
                    "run safely."
                ),
                "manual_recovery_doc": manual_doc,
                "state": state,
                "toolchain": toolchain,
            }

    return {
        "ready": True,
        "reason": "cached-ready",
        "detail": "The cached bootstrap marker is valid for the current workspace root.",
        "manual_recovery_doc": manual_doc,
        "state": state,
        "toolchain": toolchain,
    }


def bootstrap_state_summary(
    paths: WorkspacePaths,
    *,
    require_sync_capability: bool = False,
) -> dict[str, Any]:
    """Summarize bootstrap-marker visibility for status and doctor surfaces."""
    state = bootstrap_state(paths)
    readiness = cached_bootstrap_readiness(
        paths,
        require_sync_capability=require_sync_capability,
    )
    toolchain_value = readiness.get("toolchain")
    return {
        "present": bool(state),
        "schema_version": int(state.get("schema_version", 0) or 0) if state else None,
        "cached_ready": bool(readiness.get("ready")),
        "reason": readiness.get("reason"),
        "detail": readiness.get("detail"),
        "workspace_runtime_ready": bool(state.get("workspace_runtime_ready"))
        if state
        else False,
        "machine_baseline_ready": bool(state.get("machine_baseline_ready", True))
        if state
        else False,
        "machine_baseline_status": state.get("machine_baseline_status") if state else None,
        "bootstrap_source": state.get("bootstrap_source") if state else None,
        "host_access_required": bool(state.get("host_access_required")) if state else False,
        "host_access_guidance": state.get("host_access_guidance") if state else None,
        "host_access_reasons": (
            list(state.get("host_access_reasons", []))
            if state and isinstance(state.get("host_access_reasons"), list)
            else []
        ),
        "toolchain": dict(toolchain_value) if isinstance(toolchain_value, dict) else {},
        "manual_recovery_doc": readiness.get("manual_recovery_doc")
        or manual_workspace_recovery_doc(),
    }


def relative_paths(paths: WorkspacePaths, values: Iterable[Path]) -> list[str]:
    """Convert absolute workspace paths into repository-relative paths."""
    return [str(value.relative_to(paths.root)) for value in values]

"""Ask routing, workflow metadata, and conversation logging tests."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import complete_ask_turn, prepare_ask_turn
from docmason.commands import ACTION_REQUIRED, CommandReport
from docmason.control_plane import (
    complete_shared_job,
    ensure_shared_job,
    workspace_state_snapshot,
)
from docmason.conversation import open_conversation_turn, update_conversation_turn
from docmason.coordination import workspace_lease
from docmason.project import WorkspacePaths, read_json, source_inventory_signature, write_json
from docmason.retrieval import retrieve_corpus, trace_answer_file
from docmason.review import refresh_log_review_summary
from docmason.workflows import load_workflow_metadata_file, render_workflow_routing_markdown
from tests.support_ready_workspace import (
    seed_degraded_broken_venv_bootstrap_state,
    seed_mixed_external_venv_bootstrap_state,
    seed_self_contained_bootstrap_state,
)

ROOT = Path(__file__).resolve().parents[1]


class AskRoutingAndCompositionTests(unittest.TestCase):
    """Cover ask routing, conversation linkage, and composition-facing metadata."""

    def semantic_analysis(
        self,
        *,
        question_class: str,
        question_domain: str,
        route_reason: str | None = None,
        needs_latest_workspace_state: bool = False,
        memory_mode: str | None = None,
        relevant_memory_kinds: list[str] | None = None,
        evidence_requirements: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "question_class": question_class,
            "question_domain": question_domain,
            "route_reason": route_reason
            or f"Test analysis classified the question as {question_class}/{question_domain}.",
            "needs_latest_workspace_state": needs_latest_workspace_state,
        }
        if memory_mode is not None or relevant_memory_kinds is not None:
            payload["memory_query_profile"] = {
                "mode": memory_mode or "minimal",
                "relevant_memory_kinds": relevant_memory_kinds or [],
            }
        if evidence_requirements is not None:
            payload["evidence_requirements"] = evidence_requirements
        return payload

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        (root / "skills" / "canonical" / "workspace-bootstrap").mkdir(parents=True)
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "runtime").mkdir()
        (root / "planning").mkdir()
        (root / "pyproject.toml").write_text(
            "[project]\nname = 'docmason'\nversion = '0.0.0'\n",
            encoding="utf-8",
        )
        (root / "docmason.yaml").write_text(
            "workspace:\n  source_dir: original_doc\n",
            encoding="utf-8",
        )
        (root / "src" / "docmason" / "__init__.py").write_text(
            "__version__ = '0.0.0'\n",
            encoding="utf-8",
        )
        (root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        (root / "skills" / "canonical" / "workspace-bootstrap" / "SKILL.md").write_text(
            "# Workspace Bootstrap\n",
            encoding="utf-8",
        )
        return WorkspacePaths(root=root)

    def mark_environment_ready(self, workspace: WorkspacePaths) -> None:
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-16T00:00:00Z",
        )

    def create_pdf(self, path: Path, *, page_count: int = 1) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for index in range(page_count):
            writer.add_blank_page(width=144 + index, height=144 + index)
        with path.open("wb") as handle:
            writer.write(handle)

    def build_seeded_knowledge(
        self,
        source_dir: Path,
        *,
        title: str,
        summary: str,
        key_point: str,
        claim: str,
        related_sources: list[dict[str, object]] | None = None,
    ) -> None:
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        knowledge = {
            "source_id": source_manifest["source_id"],
            "source_fingerprint": source_manifest["source_fingerprint"],
            "title": title,
            "source_language": "en",
            "summary_en": summary,
            "summary_source": summary,
            "document_type": source_manifest["document_type"],
            "key_points": [
                {
                    "text_en": key_point,
                    "text_source": key_point,
                    "citations": [{"unit_id": first_unit_id, "support": "key point"}],
                }
            ],
            "entities": [{"name": title, "type": "test artifact"}],
            "claims": [
                {
                    "statement_en": claim,
                    "statement_source": claim,
                    "citations": [{"unit_id": first_unit_id, "support": "claim"}],
                }
            ],
            "known_gaps": [],
            "ambiguities": [],
            "confidence": {
                "level": "high",
                "notes_en": "Ask routing test fixture.",
                "notes_source": "Ask routing test fixture.",
            },
            "citations": [{"unit_id": first_unit_id, "support": "summary support"}],
            "related_sources": related_sources or [],
        }
        write_json(source_dir / "knowledge.json", knowledge)
        summary_md = "\n".join(
            [
                f"# {title}",
                "",
                f"Source ID: {source_manifest['source_id']}",
                "",
                "## English Summary",
                summary,
                "",
                "## Source-Language Summary",
                summary,
                "",
            ]
        )
        (source_dir / "summary.md").write_text(summary_md, encoding="utf-8")

    def publish_seeded_corpus(self, workspace: WorkspacePaths) -> list[str]:
        from docmason.commands import sync_workspace

        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = [item["source_id"] for item in pending.payload["pending_sources"]]
        self.assertEqual(len(source_ids), 2)

        source_a = workspace.knowledge_base_staging_dir / "sources" / source_ids[0]
        source_b = workspace.knowledge_base_staging_dir / "sources" / source_ids[1]
        self.build_seeded_knowledge(
            source_a,
            title="Project Planning Brief",
            summary="A planning brief about a project outline and work plan.",
            key_point="The outline defines a practical work plan.",
            claim="The project outline connects planning to implementation.",
            related_sources=[
                {
                    "source_id": source_ids[1],
                    "relation_type": "schedule-companion",
                    "strength": "high",
                    "status": "supported",
                    "citation_unit_ids": ["page-001"],
                }
            ],
        )
        self.build_seeded_knowledge(
            source_b,
            title="Project Timeline Notes",
            summary="A timeline note and companion planning document.",
            key_point="The timeline explains key milestones.",
            claim="The timeline complements the project outline.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def seed_published_kb_stub(self, workspace: WorkspacePaths) -> None:
        artifact = workspace.knowledge_base_current_dir / "artifact.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-21T00:05:00Z",
                "last_sync_at": "2026-03-21T00:05:00Z",
            },
        )

    def test_ask_workflow_metadata_exposes_user_entry_details(self) -> None:
        skill_path = ROOT / "skills" / "canonical" / "ask" / "SKILL.md"
        metadata_path = ROOT / "skills" / "canonical" / "ask" / "workflow.json"
        workflow = load_workflow_metadata_file(skill_path, metadata_path)
        self.assertEqual(workflow.user_entry["primary_user_label"], "ask")
        rendered = render_workflow_routing_markdown([workflow])
        self.assertIn("Primary user label: `ask`", rendered)
        self.assertIn("User aliases: `answer`, `ask-doc`", rendered)
        self.assertIn("Supports natural routing: `True`", rendered)

    def test_grounded_composition_workflow_metadata_exists(self) -> None:
        skill_path = ROOT / "skills" / "canonical" / "grounded-composition" / "SKILL.md"
        metadata_path = ROOT / "skills" / "canonical" / "grounded-composition" / "workflow.json"
        workflow = load_workflow_metadata_file(skill_path, metadata_path)
        self.assertEqual(workflow.workflow_id, "grounded-composition")
        self.assertEqual(workflow.category, "answer")

    def test_prepare_ask_turn_routes_common_intents(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.create_pdf(workspace.source_dir / "companion.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-1"}, clear=False):
            answer_turn = prepare_ask_turn(
                workspace,
                question="What does the project outline actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            retrieval_turn = prepare_ask_turn(
                workspace,
                question="Which documents mention the project outline?",
                semantic_analysis=self.semantic_analysis(
                    question_class="retrieval",
                    question_domain="workspace-corpus",
                ),
            )
            provenance_turn = prepare_ask_turn(
                workspace,
                question="Which source supports this answer? Please trace the citation.",
                semantic_analysis=self.semantic_analysis(
                    question_class="provenance",
                    question_domain="workspace-corpus",
                ),
            )
            review_turn = prepare_ask_turn(
                workspace,
                question="Please review recent degraded traces and no-result logs.",
                semantic_analysis=self.semantic_analysis(
                    question_class="runtime-review",
                    question_domain="workspace-corpus",
                ),
            )
            composition_turn = prepare_ask_turn(
                workspace,
                question="Help me draft the project exec summary wording for this deck.",
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                ),
            )

        self.assertEqual(answer_turn["inner_workflow_id"], "grounded-answer")
        self.assertEqual(retrieval_turn["inner_workflow_id"], "retrieval-workflow")
        self.assertEqual(provenance_turn["inner_workflow_id"], "provenance-trace")
        self.assertEqual(review_turn["inner_workflow_id"], "runtime-log-review")
        self.assertEqual(composition_turn["inner_workflow_id"], "grounded-composition")
        self.assertEqual(composition_turn["question_class"], "composition")
        self.assertEqual(composition_turn["research_depth"], "deep")
        bundle_dir = workspace.root / composition_turn["bundle_paths"][0]
        self.assertTrue((bundle_dir / "bundle-manifest.json").exists())
        self.assertTrue((bundle_dir / "research-notes.md").exists())
        self.assertEqual(answer_turn["conversation_id"], retrieval_turn["conversation_id"])

    def test_prepare_ask_turn_flags_missing_and_stale_knowledge_base(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "example.pdf")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-missing"}, clear=False):
            missing = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        self.assertEqual(missing["status"], "awaiting-confirmation")
        self.assertTrue(missing["auto_sync_triggered"])
        self.assertEqual(missing["auto_sync_summary"]["status"], "awaiting-confirmation")
        self.assertTrue(missing["knowledge_base_missing"])
        self.assertTrue(missing["attached_shared_job_ids"])
        self.assertEqual(missing["confirmation_kind"], "material-sync")

        artifact = workspace.knowledge_base_current_dir / "artifact.md"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-21T00:05:00Z",
                "last_sync_at": "2026-03-21T00:05:00Z",
            },
        )

        self.create_pdf(workspace.source_dir / "companion.pdf")
        source_path = workspace.source_dir / "example.pdf"
        source_path.write_bytes(source_path.read_bytes())
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-stale"}, clear=False):
            stale = prepare_ask_turn(
                workspace,
                question="What do the latest documents say after the newest local updates?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )
        self.assertEqual(stale["status"], "awaiting-confirmation")
        self.assertTrue(stale["knowledge_base_stale"])
        self.assertTrue(stale["auto_sync_triggered"])
        self.assertEqual(stale["auto_sync_summary"]["status"], "awaiting-confirmation")
        self.assertTrue(stale["sync_suggested"])
        self.assertTrue(stale["prefer_sync_before_answer"])

    def test_prepare_ask_turn_auto_prepares_workspace_before_auto_sync(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")

        def fake_launcher(_paths: WorkspacePaths) -> CommandReport:
            seed_self_contained_bootstrap_state(
                workspace,
                prepared_at="2026-03-21T00:00:00Z",
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "detail": "Launcher prepared the workspace successfully.",
                    "actions_performed": ["Created .venv."],
                    "actions_skipped": [],
                    "next_steps": [],
                    "launcher_delegated": True,
                    "launcher_command": "./scripts/bootstrap-workspace.sh --yes --json",
                    "environment": {
                        "package_manager": "uv",
                        "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    },
                },
                [],
            )

        def fake_sync(_paths: WorkspacePaths, assume_yes: bool = False) -> CommandReport:
            del assume_yes
            artifact = workspace.knowledge_base_current_dir / "artifact.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("compiled knowledge\n", encoding="utf-8")
            write_json(
                workspace.current_publish_manifest_path,
                {
                    "snapshot_id": "snapshot-auto-prepare",
                    "published_at": "2026-03-21T00:05:00Z",
                },
            )
            write_json(
                workspace.sync_state_path,
                {
                    "published_source_signature": source_inventory_signature(workspace),
                    "last_publish_at": "2026-03-21T00:05:00Z",
                    "last_sync_at": "2026-03-21T00:05:00Z",
                },
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "sync_status": "valid",
                    "detail": "Published.",
                    "published": True,
                    "change_set": {"stats": {}},
                    "auto_repairs": {"repair_count": 0},
                    "auto_authoring": {"authored_count": 0},
                    "autonomous_steps": [],
                },
                [],
            )

        with (
            mock.patch("docmason.ask.bootstrap_workspace_with_launcher", side_effect=fake_launcher)
            as launcher_mock,
            mock.patch("docmason.ask.prepare_workspace") as prepare_mock,
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-auto-prepare"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "prepared")
        self.assertTrue(turn["auto_prepare_triggered"])
        self.assertEqual(turn["auto_prepare_summary"]["status"], "ready")
        self.assertTrue(turn["auto_prepare_summary"]["launcher_delegated"])
        launcher_mock.assert_called_once_with(workspace)
        prepare_mock.assert_not_called()
        self.assertTrue(turn["auto_sync_triggered"])
        self.assertFalse(turn["knowledge_base_missing"])

    def test_prepare_ask_turn_delegates_to_launcher_when_prepare_reports_missing_python(
        self,
    ) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")

        failed_prepare = type(
            "PrepareReport",
            (),
            {
                "payload": {
                    "status": ACTION_REQUIRED,
                    "detail": "Python 3.10 is below the supported minimum.",
                    "actions_performed": [],
                    "actions_skipped": [],
                    "next_steps": ["Install Python 3.11 or newer and rerun `docmason prepare`."],
                    "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    "environment": {
                        "package_manager": "uv",
                        "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    },
                }
            },
        )()
        launcher_report = CommandReport(
            0,
            {
                "status": "ready",
                "detail": "Launcher prepared the workspace successfully.",
                "actions_performed": ["Provisioned repo-local managed Python 3.13."],
                "actions_skipped": [],
                "next_steps": [],
                "launcher_delegated": True,
                "launcher_command": "./scripts/bootstrap-workspace.sh --yes --json",
                "environment": {
                    "package_manager": "uv",
                    "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                },
            },
            [],
        )

        def fake_sync(_paths: WorkspacePaths, assume_yes: bool = False) -> CommandReport:
            del assume_yes
            write_json(
                workspace.current_publish_manifest_path,
                {
                    "snapshot_id": "snapshot-launcher-path",
                    "published_at": "2026-03-21T00:05:00Z",
                },
            )
            write_json(
                workspace.sync_state_path,
                {
                    "published_source_signature": source_inventory_signature(workspace),
                    "last_publish_at": "2026-03-21T00:05:00Z",
                    "last_sync_at": "2026-03-21T00:05:00Z",
                },
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "sync_status": "valid",
                    "detail": "Published.",
                    "published": True,
                    "change_set": {"stats": {}},
                    "auto_repairs": {"repair_count": 0},
                    "auto_authoring": {"authored_count": 0},
                    "autonomous_steps": [],
                },
                [],
            )

        readiness_states = [
            {
                "ready": False,
                "detail": "The cached bootstrap marker is missing or invalid.",
                "reason": "legacy-bootstrap-state",
            },
            {
                "ready": False,
                "detail": "The cached bootstrap marker is missing or invalid.",
                "reason": "legacy-bootstrap-state",
            },
            {
                "ready": False,
                "detail": "The workspace environment is still not ready.",
                "reason": "environment-not-ready",
            },
            {
                "ready": True,
                "detail": "The cached bootstrap marker is valid for the current workspace root.",
                "reason": "cached-ready",
            },
            {
                "ready": True,
                "detail": "The cached bootstrap marker is valid for the current workspace root.",
                "reason": "cached-ready",
            },
        ]

        with (
            mock.patch("docmason.ask.prepare_workspace", return_value=failed_prepare),
            mock.patch(
                "docmason.ask.bootstrap_workspace_with_launcher",
                return_value=launcher_report,
            ) as launcher_mock,
            mock.patch("docmason.ask.cached_bootstrap_readiness", side_effect=readiness_states),
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
            mock.patch.dict(
                os.environ,
                {"CODEX_THREAD_ID": "thread-launcher-delegate"},
                clear=False,
            ),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "prepared")
        self.assertTrue(turn["auto_prepare_triggered"])
        self.assertTrue(turn["auto_prepare_summary"]["launcher_delegated"])
        launcher_mock.assert_called_once_with(workspace)

    def test_prepare_ask_turn_reuses_valid_cached_marker_without_auto_prepare(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.create_pdf(workspace.source_dir / "companion.pdf")
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-21T00:00:00Z",
        )
        self.publish_seeded_corpus(workspace)

        with (
            mock.patch(
                "docmason.ask.prepare_workspace",
                side_effect=AssertionError("prepare should not run"),
            ),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-cached-ready"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the project outline actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "prepared")
        self.assertFalse(turn["auto_prepare_triggered"])
        self.assertFalse(turn["auto_sync_triggered"])

    def test_prepare_ask_turn_keeps_published_office_kb_answerable_under_default_permissions(
        self,
    ) -> None:
        workspace = self.make_workspace()
        (workspace.source_dir / "brief.docx").write_text("docx placeholder\n", encoding="utf-8")
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-21T00:00:00Z",
        )
        self.seed_published_kb_stub(workspace)

        with (
            mock.patch(
                "docmason.ask.prepare_workspace",
                side_effect=AssertionError("prepare should not run"),
            ),
            mock.patch(
                "docmason.ask.bootstrap_workspace_with_launcher",
                side_effect=AssertionError("launcher should not run"),
            ),
            mock.patch(
                "docmason.ask.run_sync_command",
                side_effect=AssertionError("sync should not run"),
            ),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "default-permissions",
                    "CODEX_THREAD_ID": "thread-office-kb-default",
                },
                clear=False,
            ),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "prepared")
        self.assertEqual(turn["inner_workflow_id"], "grounded-answer")
        self.assertFalse(turn["auto_prepare_triggered"])
        self.assertFalse(turn["auto_sync_triggered"])

    def test_prepare_ask_turn_reuses_healthy_schema4_marker_without_auto_prepare(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.create_pdf(workspace.source_dir / "companion.pdf")
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-21T00:00:00Z",
        )
        state = read_json(workspace.bootstrap_state_path)
        state["schema_version"] = 4
        write_json(workspace.bootstrap_state_path, state)
        self.publish_seeded_corpus(workspace)

        with (
            mock.patch(
                "docmason.ask.prepare_workspace",
                side_effect=AssertionError("prepare should not run"),
            ),
            mock.patch(
                "docmason.ask.bootstrap_workspace_with_launcher",
                side_effect=AssertionError("launcher should not run"),
            ),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-schema4-ready"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the project outline actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "prepared")
        self.assertFalse(turn["auto_prepare_triggered"])
        self.assertFalse(turn["auto_sync_triggered"])

    def test_prepare_ask_turn_pauses_for_native_codex_host_access_upgrade(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.seed_published_kb_stub(workspace)
        launcher_report = CommandReport(
            1,
            {
                "status": ACTION_REQUIRED,
                "detail": (
                    "Native Codex machine baseline is missing LibreOffice for the current "
                    "Office corpus."
                ),
                "control_plane": {
                    "state": "awaiting-confirmation",
                    "confirmation_kind": "host-access-upgrade",
                    "confirmation_prompt": (
                        "DocMason is currently running in Codex `Default permissions`."
                    ),
                    "confirmation_reason": "machine-baseline; runtime-downloads",
                },
                "host_access_required": True,
                "host_access_guidance": (
                    "Switch this Codex thread to `Full access`, then continue the same task."
                ),
                "next_steps": [
                    "Switch Codex to `Full access`, then continue the same task."
                ],
            },
            [],
        )

        with (
            mock.patch("docmason.ask.prepare_workspace") as prepare_mock,
            mock.patch("docmason.ask.cached_bootstrap_readiness", side_effect=[
                {
                    "ready": False,
                    "detail": "No cached bootstrap marker is recorded yet.",
                    "reason": "missing-bootstrap-state",
                },
                {
                    "ready": False,
                    "detail": "No cached bootstrap marker is recorded yet.",
                    "reason": "missing-bootstrap-state",
                },
                {
                    "ready": False,
                    "detail": "No cached bootstrap marker is recorded yet.",
                    "reason": "missing-bootstrap-state",
                },
            ]),
            mock.patch(
                "docmason.ask.bootstrap_workspace_with_launcher",
                return_value=launcher_report,
            ) as launcher_mock,
            mock.patch.dict(
                os.environ,
                {"CODEX_THREAD_ID": "thread-host-access-upgrade"},
                clear=False,
            ),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        prepare_mock.assert_not_called()
        launcher_mock.assert_called_once_with(workspace)
        self.assertEqual(turn["status"], "awaiting-confirmation")
        self.assertEqual(turn["confirmation_kind"], "host-access-upgrade")
        self.assertIn("Default permissions", str(turn["confirmation_prompt"]))
        self.assertTrue(turn["auto_prepare_triggered"])

    def test_prepare_ask_turn_refreshes_legacy_marker_before_auto_sync(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "prepared_at": "2026-03-16T00:00:00Z",
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
            },
        )

        def fake_prepare(*args: object, **kwargs: object) -> object:
            del args, kwargs
            seed_self_contained_bootstrap_state(
                workspace,
                prepared_at="2026-03-21T00:00:00Z",
            )
            return type(
                "PrepareReport",
                (),
                {
                    "payload": {
                        "status": "ready",
                        "actions_performed": ["Refreshed bootstrap marker."],
                        "actions_skipped": [],
                        "next_steps": [],
                        "environment": {
                            "package_manager": "uv",
                            "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                        },
                    }
                },
            )()

        def fake_sync(_paths: WorkspacePaths, assume_yes: bool = False) -> CommandReport:
            del assume_yes
            artifact = workspace.knowledge_base_current_dir / "artifact.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("compiled knowledge\n", encoding="utf-8")
            write_json(
                workspace.sync_state_path,
                {
                    "published_source_signature": source_inventory_signature(workspace),
                    "last_publish_at": "2026-03-21T00:05:00Z",
                    "last_sync_at": "2026-03-21T00:05:00Z",
                },
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "sync_status": "valid",
                    "detail": "Published.",
                    "published": True,
                    "change_set": {"stats": {}},
                    "auto_repairs": {"repair_count": 0},
                    "auto_authoring": {"authored_count": 0},
                    "autonomous_steps": [],
                },
                [],
            )

        with (
            mock.patch(
                "docmason.ask.prepare_workspace",
                side_effect=fake_prepare,
            ) as mocked_prepare,
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-legacy-sync"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(mocked_prepare.call_count, 1)
        self.assertEqual(turn["status"], "prepared")
        self.assertTrue(turn["auto_prepare_triggered"])
        self.assertTrue(turn["auto_sync_triggered"])
        self.assertFalse(turn["knowledge_base_missing"])

    def test_prepare_ask_turn_blocks_workspace_corpus_on_mixed_environment(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.seed_published_kb_stub(workspace)
        seed_mixed_external_venv_bootstrap_state(
            workspace,
            prepared_at="2026-03-21T00:00:00Z",
        )

        with (
            mock.patch(
                "docmason.ask.prepare_workspace",
                return_value=CommandReport(
                    1,
                    {
                        "status": "action-required",
                        "actions_performed": [],
                        "actions_skipped": [],
                        "next_steps": ["Repair the workspace toolchain."],
                        "environment": {
                            "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                        },
                    },
                    [],
                ),
            ),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-mixed-env"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "action-required")
        self.assertEqual(turn["inner_workflow_id"], "workspace-bootstrap")
        self.assertTrue(turn["auto_prepare_triggered"])
        self.assertIn("external interpreter", str(turn["freshness_notice"]))

    def test_prepare_ask_turn_blocks_composition_on_degraded_environment(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.seed_published_kb_stub(workspace)
        seed_degraded_broken_venv_bootstrap_state(
            workspace,
            prepared_at="2026-03-21T00:00:00Z",
        )

        with (
            mock.patch(
                "docmason.ask.prepare_workspace",
                return_value=CommandReport(
                    1,
                    {
                        "status": "action-required",
                        "actions_performed": [],
                        "actions_skipped": [],
                        "next_steps": ["Repair the workspace toolchain."],
                        "environment": {
                            "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                        },
                    },
                    [],
                ),
            ),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-degraded-env"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="Draft a composition plan from the workspace corpus.",
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                ),
            )

        self.assertEqual(turn["status"], "action-required")
        self.assertEqual(turn["inner_workflow_id"], "workspace-bootstrap")
        self.assertTrue(turn["auto_prepare_triggered"])
        self.assertIn("broken interpreter path", str(turn["freshness_notice"]))

    def test_prepare_ask_turn_refreshes_run_and_turn_version_truth_after_auto_sync(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.create_pdf(workspace.source_dir / "companion.pdf")

        def fake_sync(_paths: WorkspacePaths, assume_yes: bool = False) -> CommandReport:
            del assume_yes
            artifact = workspace.knowledge_base_current_dir / "artifact.md"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("compiled knowledge\n", encoding="utf-8")
            write_json(
                workspace.current_publish_manifest_path,
                {
                    "snapshot_id": "snapshot-auto-sync",
                    "published_at": "2026-03-21T00:05:00Z",
                },
            )
            write_json(
                workspace.sync_state_path,
                {
                    "published_source_signature": source_inventory_signature(workspace),
                    "last_publish_at": "2026-03-21T00:05:00Z",
                    "last_sync_at": "2026-03-21T00:05:00Z",
                },
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "sync_status": "valid",
                    "detail": "Published.",
                    "published": True,
                    "change_set": {"stats": {}},
                    "auto_repairs": {"repair_count": 0},
                    "auto_authoring": {"authored_count": 0},
                    "autonomous_steps": [],
                },
                [],
            )

        def fake_launcher(_paths: WorkspacePaths) -> CommandReport:
            seed_self_contained_bootstrap_state(
                workspace,
                prepared_at="2026-03-21T00:00:00Z",
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "detail": "Launcher prepared the workspace successfully.",
                    "actions_performed": ["Created .venv."],
                    "actions_skipped": [],
                    "next_steps": [],
                    "launcher_delegated": True,
                    "launcher_command": "./scripts/bootstrap-workspace.sh --yes --json",
                    "environment": {
                        "package_manager": "uv",
                        "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    },
                },
                [],
            )

        with (
            mock.patch("docmason.ask.bootstrap_workspace_with_launcher", side_effect=fake_launcher),
            mock.patch("docmason.ask.prepare_workspace") as prepare_mock,
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
            mock.patch.dict(
                os.environ,
                {"CODEX_THREAD_ID": "thread-auto-sync-version"},
                clear=False,
            ),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        prepare_mock.assert_not_called()

        conversation = read_json(
            workspace.conversations_dir / f"{turn['conversation_id']}.json"
        )
        live_turn = conversation["turns"][0]
        run_state = read_json(workspace.runs_dir / turn["run_id"] / "state.json")
        self.assertEqual(
            live_turn["version_context"]["published_snapshot_id"],
            "snapshot-auto-sync",
        )
        self.assertEqual(
            run_state["version_context"]["published_snapshot_id"],
            "snapshot-auto-sync",
        )

    def test_workspace_state_snapshot_repairs_stale_answer_critical_owner_run(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        job = ensure_shared_job(
            workspace,
            job_key="sync:stale-owner-run",
            job_family="sync",
            criticality="answer-critical",
            scope={"workspace_root": str(workspace.root)},
            input_signature="sync:stale-owner-run",
            owner={"kind": "run", "id": "run-missing"},
            run_id="run-missing",
        )
        snapshot = workspace_state_snapshot(workspace)
        self.assertFalse(snapshot["active_answer_critical_jobs"])
        self.assertTrue(snapshot["repair_actions"])
        self.assertEqual(
            snapshot["repair_actions"][0]["kind"],
            "blocked-missing-owner-run",
        )
        settled = read_json(
            workspace.shared_jobs_dir / job["manifest"]["job_id"] / "result.json"
        )
        self.assertEqual(settled["status"], "blocked")

    def test_workspace_state_snapshot_repairs_stale_command_owner_process(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        job = ensure_shared_job(
            workspace,
            job_key="sync:stale-command-owner",
            job_family="sync",
            criticality="answer-critical",
            scope={"workspace_root": str(workspace.root)},
            input_signature="sync:stale-command-owner",
            owner={"kind": "command", "id": "sync-command:stale", "pid": 999999},
        )
        snapshot = workspace_state_snapshot(workspace)
        self.assertFalse(snapshot["active_answer_critical_jobs"])
        self.assertTrue(snapshot["repair_actions"])
        self.assertEqual(
            snapshot["repair_actions"][0]["kind"],
            "blocked-inactive-owner-process",
        )
        settled = read_json(
            workspace.shared_jobs_dir / job["manifest"]["job_id"] / "result.json"
        )
        self.assertEqual(settled["status"], "blocked")

    def test_workspace_state_snapshot_settles_equivalent_completed_sync_job(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        stale_job = ensure_shared_job(
            workspace,
            job_key="sync:stale-confirmation",
            job_family="sync",
            criticality="answer-critical",
            scope={
                "target": "current",
                "strong_source_fingerprint_signature": "fingerprint-shared",
            },
            input_signature="sync:stale-confirmation",
            owner={"kind": "run", "id": "run-stale"},
            run_id="run-stale",
            requires_confirmation=True,
            confirmation_kind="material-sync",
            confirmation_prompt="Approve stale confirmation job.",
            confirmation_reason="test fixture stale confirmation",
        )
        equivalent_job = ensure_shared_job(
            workspace,
            job_key="sync:equivalent-completed",
            job_family="sync",
            criticality="answer-critical",
            scope={
                "target": "current",
                "strong_source_fingerprint_signature": "fingerprint-shared",
            },
            input_signature="sync:equivalent-completed",
            owner={"kind": "command", "id": "sync-command:equivalent", "pid": os.getpid()},
        )
        complete_shared_job(
            workspace,
            str(equivalent_job["manifest"]["job_id"]),
            result={
                "status": "valid",
                "detail": "Published truth was already current, so final publication was skipped.",
                "published": False,
            },
        )

        snapshot = workspace_state_snapshot(workspace)

        self.assertFalse(snapshot["active_answer_critical_jobs"])
        self.assertTrue(snapshot["repair_actions"])
        self.assertEqual(
            snapshot["repair_actions"][0]["kind"],
            "settled-equivalent-sync-completion",
        )
        settled = read_json(
            workspace.shared_jobs_dir / stale_job["manifest"]["job_id"] / "result.json"
        )
        self.assertEqual(settled["status"], "completed")
        self.assertEqual(
            settled["result"]["detail"],
            "Stale confirmation-only sync job settled after equivalent sync completion.",
        )

    def test_workspace_state_snapshot_repairs_stale_answer_critical_lane_c_snapshot(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        job = ensure_shared_job(
            workspace,
            job_key="lane-c:stale-published-snapshot",
            job_family="lane-c",
            criticality="answer-critical",
            scope={
                "target": "current",
                "published_snapshot_id": "snapshot-old",
                "source_id": "source-old",
            },
            input_signature="lane-c:stale-published-snapshot",
            owner={"kind": "command", "id": "lane-c-command:live", "pid": os.getpid()},
        )

        snapshot = workspace_state_snapshot(workspace)

        self.assertFalse(snapshot["active_answer_critical_jobs"])
        self.assertTrue(snapshot["repair_actions"])
        self.assertEqual(
            snapshot["repair_actions"][0]["kind"],
            "blocked-stale-answer-critical-snapshot",
        )
        settled = read_json(
            workspace.shared_jobs_dir / job["manifest"]["job_id"] / "result.json"
        )
        self.assertEqual(settled["status"], "blocked")
        self.assertIn("older published snapshot", settled["result"]["reason"])

    def test_workspace_state_snapshot_repairs_stale_lane_c_owner_run_without_progress(
        self,
    ) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        publish_manifest = read_json(workspace.current_publish_manifest_path)
        run_dir = workspace.runs_dir / "run-lane-c-stale"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            run_dir / "state.json",
            {
                "run_id": "run-lane-c-stale",
                "status": "active",
                "opened_at": "2026-03-20T00:00:00Z",
                "updated_at": "2026-03-20T00:00:00Z",
                "attached_shared_job_ids": [],
                "execution_cost_profile": {},
                "preanswer_governance_state": None,
            },
        )
        (run_dir / "journal.jsonl").write_text("", encoding="utf-8")

        job = ensure_shared_job(
            workspace,
            job_key="lane-c:stale-owner-run",
            job_family="lane-c",
            criticality="answer-critical",
            scope={
                "target": "current",
                "published_snapshot_id": str(publish_manifest["snapshot_id"]),
                "source_id": "source-current",
            },
            input_signature="lane-c:stale-owner-run",
            owner={"kind": "run", "id": "run-lane-c-stale"},
            run_id="run-lane-c-stale",
        )
        manifest_path = (
            workspace.shared_jobs_dir / str(job["manifest"]["job_id"]) / "manifest.json"
        )
        stale_manifest = read_json(manifest_path)
        stale_manifest["updated_at"] = "2026-03-20T00:00:00Z"
        write_json(manifest_path, stale_manifest)
        stale_run_state = read_json(run_dir / "state.json")
        stale_run_state["updated_at"] = "2026-03-20T00:00:00Z"
        write_json(run_dir / "state.json", stale_run_state)

        snapshot = workspace_state_snapshot(workspace)
        repaired_state = read_json(run_dir / "state.json")
        settled = read_json(
            workspace.shared_jobs_dir / str(job["manifest"]["job_id"]) / "result.json"
        )

        self.assertFalse(snapshot["active_answer_critical_jobs"])
        self.assertTrue(
            any(
                action["kind"] == "blocked-stale-owner-run"
                for action in snapshot["repair_actions"]
            )
        )
        self.assertTrue(
            any(
                action["kind"] == "abandoned-stale-active-run"
                for action in snapshot["repair_actions"]
            )
        )
        self.assertEqual(settled["status"], "blocked")
        self.assertIn("stopped making progress", settled["result"]["reason"])
        self.assertEqual(repaired_state["status"], "abandoned")

    def test_workspace_state_snapshot_abandons_stale_active_run_without_commit_or_live_jobs(
        self,
    ) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        run_dir = workspace.runs_dir / "run-stale"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            run_dir / "state.json",
            {
                "run_id": "run-stale",
                "status": "active",
                "opened_at": "2026-03-20T00:00:00Z",
                "updated_at": "2026-03-20T00:00:00Z",
                "attached_shared_job_ids": [],
                "execution_cost_profile": {},
                "preanswer_governance_state": None,
            },
        )
        (run_dir / "journal.jsonl").write_text("", encoding="utf-8")

        snapshot = workspace_state_snapshot(workspace)
        repaired_state = read_json(run_dir / "state.json")
        journal_text = (run_dir / "journal.jsonl").read_text(encoding="utf-8")

        self.assertEqual(repaired_state["status"], "abandoned")
        self.assertEqual(repaired_state["abandon_reason"], "stale-active-run-repair")
        self.assertTrue(
            any(
                action["kind"] == "abandoned-stale-active-run"
                for action in snapshot["repair_actions"]
            )
        )
        self.assertIn("stale-active-run-abandoned", journal_text)

    def test_prepare_ask_turn_reports_bootstrap_blocker_when_auto_prepare_fails(self) -> None:
        workspace = self.make_workspace()

        failed_report = type(
            "PrepareReport",
            (),
            {
                "payload": {
                    "status": "action-required",
                    "actions_performed": [],
                    "actions_skipped": [],
                    "next_steps": [
                        "Use macOS or Linux for the supported DocMason workflow.",
                        (
                            "Follow `docs/setup/manual-workspace-recovery.md` for the "
                            "manual workspace bootstrap and repair fallback."
                        ),
                    ],
                    "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    "environment": {
                        "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    },
                }
            },
        )()

        with (
            mock.patch("docmason.ask.prepare_workspace", return_value=failed_report),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-prepare-fail"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "action-required")
        self.assertEqual(turn["inner_workflow_id"], "workspace-bootstrap")
        self.assertTrue(turn["auto_prepare_triggered"])
        self.assertEqual(
            turn["auto_prepare_summary"]["manual_recovery_doc"],
            "docs/setup/manual-workspace-recovery.md",
        )

    def test_conversation_turn_links_retrieval_and_trace_logs(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-conv"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the project outline actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            retrieval = retrieve_corpus(
                workspace,
                query="project outline",
                top=2,
                graph_hops=1,
                document_types=None,
                source_ids=None,
                include_renders=True,
                log_context=turn["log_context"],
            )
            answer_path = workspace.root / turn["answer_file_path"]
            answer_path.write_text(
                "The project outline connects the work plan to implementation.",
                encoding="utf-8",
            )
            trace = trace_answer_file(
                workspace,
                answer_file=answer_path,
                top=2,
                log_context=turn["log_context"],
            )
            completed = complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                session_ids=[retrieval["session_id"], trace["session_id"]],
                trace_ids=[trace["trace_id"]],
                answer_state=trace["answer_state"],
                render_inspection_required=trace["render_inspection_required"],
                answer_file_path=turn["answer_file_path"],
                response_excerpt=(
                    "The project outline connects the work plan to implementation."
                ),
                status="answered",
            )

        conversation = read_json(workspace.conversations_dir / f"{turn['conversation_id']}.json")
        self.assertEqual(len(conversation["turns"]), 1)
        self.assertEqual(completed["answer_state"], "grounded")
        query_session = read_json(workspace.query_sessions_dir / f"{retrieval['session_id']}.json")
        trace_record = read_json(workspace.retrieval_traces_dir / f"{trace['trace_id']}.json")
        self.assertEqual(query_session["conversation_id"], turn["conversation_id"])
        self.assertEqual(query_session["turn_id"], turn["turn_id"])
        self.assertEqual(query_session["entry_workflow_id"], "ask")
        self.assertEqual(query_session["question_class"], "answer")
        self.assertEqual(query_session["support_strategy"], "kb-first")
        self.assertEqual(query_session["analysis_origin"], "agent-supplied")
        self.assertEqual(trace_record["conversation_id"], turn["conversation_id"])
        self.assertEqual(trace_record["answer_file_path"], turn["answer_file_path"])
        self.assertEqual(trace_record["question_class"], "answer")
        self.assertEqual(trace_record["support_strategy"], "kb-first")
        self.assertEqual(trace_record["analysis_origin"], "agent-supplied")
        self.assertEqual(len(completed["session_ids"]), 2)
        self.assertEqual(len(completed["trace_ids"]), 1)

    def test_benchmark_candidates_follow_conversation_turns(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-candidate"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Reference Deck 2 slide 34 visual detail.",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            answer_path = workspace.root / turn["answer_file_path"]
            answer_path.write_text(
                "\n\n".join(
                    [
                        "The project outline connects the work plan to implementation.",
                        "Zyzzyva quasar nebulae orthonormal frabjous snark.",
                    ]
                ),
                encoding="utf-8",
            )
            trace = trace_answer_file(
                workspace,
                answer_file=answer_path,
                top=2,
                log_context=turn["log_context"],
            )
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                session_ids=[trace["session_id"]],
                trace_ids=[trace["trace_id"]],
                answer_state=trace["answer_state"],
                render_inspection_required=trace["render_inspection_required"],
                answer_file_path=turn["answer_file_path"],
                response_excerpt="Mixed grounded and unresolved answer.",
                status="answered",
            )

        refresh_log_review_summary(workspace)
        candidates = read_json(workspace.benchmark_candidates_path)
        self.assertTrue(candidates["candidates"])
        first = candidates["candidates"][0]
        self.assertEqual(first["conversation_id"], turn["conversation_id"])
        self.assertEqual(first["turn_id"], turn["turn_id"])
        self.assertTrue(first["requires_render_inspection"])
        self.assertEqual(first["candidate_priority"], "high")
        self.assertEqual(first["log_origin"], "interactive-ask")
        self.assertIn(
            first["suggested_benchmark_family"],
            {"render-required-visual-evidence", "degraded-grounded-answer"},
        )
        self.assertEqual(first["supporting_source_ids"], trace["supporting_source_ids"])

    def test_answer_history_and_review_summary_anchor_to_final_trace_support(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-history-anchor"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does Project Planning Brief say about the project outline?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The project outline connects the work plan to implementation.\n",
            encoding="utf-8",
        )
        first_trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        first_trace_path = workspace.retrieval_traces_dir / f"{first_trace['trace_id']}.json"
        first_trace_payload = read_json(first_trace_path)
        first_trace_payload["supporting_source_ids"] = [source_ids[0], source_ids[1]]
        write_json(first_trace_path, first_trace_payload)

        answer_path.write_text(
            "The delivery timeline complements the project outline.\n",
            encoding="utf-8",
        )
        second_trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        second_trace_path = workspace.retrieval_traces_dir / f"{second_trace['trace_id']}.json"
        second_trace_payload = read_json(second_trace_path)
        second_trace_payload["supporting_source_ids"] = [source_ids[1]]
        second_trace_payload["supporting_unit_ids"] = ["unit-final"]
        second_trace_payload["supporting_artifact_ids"] = ["artifact:final"]
        write_json(second_trace_path, second_trace_payload)

        complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[str(first_trace["session_id"]), str(second_trace["session_id"])],
            trace_ids=[str(second_trace["trace_id"])],
            answer_state="grounded",
            render_inspection_required=second_trace["render_inspection_required"],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Anchored to final trace.",
            status="answered",
        )

        summary = refresh_log_review_summary(workspace)
        answer_history = read_json(workspace.answer_history_index_path)
        record = answer_history["records"][0]
        self.assertEqual(record["kb_source_ids"], [source_ids[1]])
        self.assertEqual(record["session_ids"], [second_trace["session_id"]])
        self.assertEqual(record["trace_ids"], [second_trace["trace_id"]])
        committed_turn = summary["committed_turns"]["recent"][0]
        self.assertEqual(
            committed_turn["canonical_support"]["supporting_source_ids"],
            [source_ids[1]],
        )
        candidates = read_json(workspace.benchmark_candidates_path)
        if candidates["candidate_count"]:
            self.assertNotIn(
                source_ids[0],
                candidates["candidates"][0].get("supporting_source_ids", []),
            )

    def test_open_conversation_turn_waits_for_conversation_lease(self) -> None:
        workspace = self.make_workspace()
        result: dict[str, object] = {}
        finished = threading.Event()

        def open_turn() -> None:
            result["payload"] = open_conversation_turn(
                workspace,
                user_question="What changed in the proposal?",
            )
            finished.set()

        with workspace_lease(workspace, "conversation:test-thread", timeout_seconds=1.0):
            with mock.patch(
                "docmason.conversation.resolve_conversation_id",
                return_value=("test-thread", "native"),
            ):
                thread = threading.Thread(target=open_turn)
                thread.start()
                time.sleep(0.2)
                self.assertFalse(
                    finished.is_set(),
                    "Opening a turn should wait while the conversation lease is active.",
                )
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        payload = result["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["conversation_id"], "test-thread")

    def test_update_conversation_turn_waits_for_conversation_lease(self) -> None:
        workspace = self.make_workspace()
        with mock.patch(
            "docmason.conversation.resolve_conversation_id",
            return_value=("test-thread", "native"),
        ):
            opened = open_conversation_turn(workspace, user_question="Need a summary.")

        finished = threading.Event()
        result: dict[str, object] = {}

        def update_turn() -> None:
            result["payload"] = update_conversation_turn(
                workspace,
                conversation_id="test-thread",
                turn_id=str(opened["turn_id"]),
                updates={"status": "completed"},
            )
            finished.set()

        with workspace_lease(workspace, "conversation:test-thread", timeout_seconds=1.0):
            thread = threading.Thread(target=update_turn)
            thread.start()
            time.sleep(0.2)
            self.assertFalse(
                finished.is_set(),
                "Updating a turn should wait while the conversation lease is active.",
            )
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        payload = result["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["status"], "completed")


if __name__ == "__main__":
    unittest.main()

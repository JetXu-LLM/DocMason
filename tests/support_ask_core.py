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
from docmason.commands import CommandReport
from docmason.conversation import open_conversation_turn, update_conversation_turn
from docmason.coordination import workspace_lease
from docmason.project import WorkspacePaths, read_json, source_inventory_signature, write_json
from docmason.retrieval import retrieve_corpus, trace_answer_file
from tests.support_ready_workspace import (
    seed_degraded_broken_venv_bootstrap_state,
    seed_mixed_external_venv_bootstrap_state,
    seed_self_contained_bootstrap_state,
)
from docmason.workflows import load_workflow_metadata_file, render_workflow_routing_markdown

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
            title="Campaign Planning Brief",
            summary="A strategy deck about architecture and operating model.",
            key_point="The strategy defines an architecture operating model.",
            claim="The architecture deck connects strategy to implementation.",
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
            title="Campaign Evaluation Plan",
            summary="A delivery timeline and companion planning document.",
            key_point="The timeline explains rollout milestones.",
            claim="The timeline complements the architecture strategy.",
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
                question="What does the architecture strategy actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            retrieval_turn = prepare_ask_turn(
                workspace,
                question="Which documents mention the architecture strategy?",
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
                        "actions_performed": ["Created .venv."],
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
            mock.patch("docmason.ask.prepare_workspace", side_effect=fake_prepare),
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
        self.assertTrue(turn["auto_sync_triggered"])
        self.assertFalse(turn["knowledge_base_missing"])

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
            mock.patch("docmason.ask.prepare_workspace", side_effect=AssertionError("prepare should not run")),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-cached-ready"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the architecture strategy actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["status"], "prepared")
        self.assertFalse(turn["auto_prepare_triggered"])
        self.assertFalse(turn["auto_sync_triggered"])

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
            mock.patch("docmason.ask.prepare_workspace", side_effect=fake_prepare) as mocked_prepare,
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
                        "actions_performed": ["Created .venv."],
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

        with (
            mock.patch("docmason.ask.prepare_workspace", side_effect=fake_prepare),
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-auto-sync-version"}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What do the documents say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

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
                question="What does the architecture strategy actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            retrieval = retrieve_corpus(
                workspace,
                query="architecture strategy",
                top=2,
                graph_hops=1,
                document_types=None,
                source_ids=None,
                include_renders=True,
                log_context=turn["log_context"],
            )
            answer_path = workspace.root / turn["answer_file_path"]
            answer_path.write_text(
                "The architecture strategy connects the operating model to implementation.",
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
                    "The architecture strategy connects the operating model to implementation."
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
                question="WIP Form G2 slide 34 visual detail.",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            answer_path = workspace.root / turn["answer_file_path"]
            answer_path.write_text(
                "\n\n".join(
                    [
                        "The architecture strategy connects the operating model to implementation.",
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

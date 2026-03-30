# ruff: noqa: E501
"""Ask-path hardening, workflow runner, and front-controller tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import (
    begin_lane_c_shared_refresh,
    complete_ask_turn,
    prepare_ask_turn,
    settle_lane_c_shared_refresh,
)
from docmason.commands import (
    ACTION_REQUIRED,
    READY,
    CommandReport,
    review_runtime_logs,
    run_workflow,
    sync_workspace,
    trace_knowledge,
)
from docmason.control_plane import complete_shared_job as complete_control_plane_job
from docmason.control_plane import ensure_shared_job, lane_c_job_key, load_shared_job
from docmason.conversation import load_turn_record, update_conversation_turn
from docmason.front_controller import write_hybrid_refresh_work
from docmason.host_integration import handle_hidden_ask_request
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import (
    _recommended_hybrid_targets,
    retrieve_corpus,
    trace_answer_file,
    trace_session,
)
from docmason.review import refresh_log_review_summary
from docmason.run_control import (
    load_run_state,
    record_shared_job_settled_once,
    run_journal_path,
    update_run_state,
)
from tests.support_ready_workspace import seed_self_contained_bootstrap_state

ROOT = Path(__file__).resolve().parents[1]


class AskHardeningTests(unittest.TestCase):
    """Cover workflow execution, composition scaffolding, and review refresh."""

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
        shutil.copytree(ROOT / "skills" / "canonical", root / "skills" / "canonical")
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
        return WorkspacePaths(root=root)

    def mark_environment_ready(self, workspace: WorkspacePaths) -> None:
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-17T00:00:00Z",
        )

    def seed_release_bundle(
        self,
        workspace: WorkspacePaths,
        *,
        distribution_channel: str = "clean",
        source_version: str = "v0.1.0",
        update_service_url: str = "https://updates.example.invalid/v1/check",
    ) -> None:
        write_json(
            workspace.distribution_manifest_path,
            {
                "distribution_channel": distribution_channel,
                "asset_name": "DocMason-clean.zip",
                "source_version": source_version,
                "source_repo": "example/DocMason",
                "release_entry": {
                    "schema_version": 1,
                    "update_service_url": update_service_url,
                    "distribution_channel": distribution_channel,
                    "automatic_check_scope": "canonical-ask",
                    "automatic_check_cooldown_hours": 20,
                    "automatic_check_enabled_by_default": True,
                    "asset_name": "DocMason-clean.zip",
                },
            },
        )
        write_json(
            workspace.release_client_state_path,
            {
                "schema_version": 1,
                "automatic_check_enabled": True,
                "installation_hash": None,
                "created_at": None,
                "last_check_attempted_at": None,
                "next_eligible_at": None,
                "last_known_latest_version": None,
                "last_notified_version": None,
                "last_check_status": None,
            },
        )

    def run_host_snippet(
        self,
        workspace: WorkspacePaths,
        *,
        script: str,
        env_overrides: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.update(env_overrides)
        current_pythonpath = env.get("PYTHONPATH")
        repo_source_root = str(ROOT / "src")
        env["PYTHONPATH"] = (
            repo_source_root
            if not current_pythonpath
            else f"{repo_source_root}{os.pathsep}{current_pythonpath}"
        )
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=workspace.root,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def load_conversation(
        self,
        workspace: WorkspacePaths,
        *,
        conversation_id: str,
    ) -> dict[str, object]:
        return read_json(workspace.conversations_dir / f"{conversation_id}.json")

    def create_pdf(self, path: Path, *, page_count: int = 1) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for index in range(page_count):
            writer.add_blank_page(width=144 + index, height=144 + index)
        with path.open("wb") as handle:
            writer.write(handle)

    def create_pdf_with_full_page_image(self, path: Path) -> None:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - compatibility import
            import fitz as pymupdf  # type: ignore[import-not-found]
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tempdir_name:
            image_path = Path(tempdir_name) / "page.png"
            image = Image.new("RGB", (1200, 1600), color=(245, 245, 245))
            draw = ImageDraw.Draw(image)
            draw.rectangle((80, 120, 1120, 1480), outline=(32, 64, 128), width=14)
            draw.rectangle((170, 300, 1030, 540), fill=(210, 225, 245))
            draw.rectangle((170, 660, 1030, 1180), fill=(225, 235, 250))
            draw.rectangle((170, 1230, 760, 1380), fill=(235, 240, 250))
            image.save(image_path)

            document = pymupdf.open()
            page = document.new_page(width=595, height=842)
            page.insert_image(page.rect, filename=str(image_path))
            document.save(path)
            document.close()

    def build_seeded_knowledge(
        self,
        source_dir: Path,
        *,
        title: str,
        summary: str,
        key_point: str,
        claim: str,
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
                "notes_en": "Ask hardening test fixture.",
                "notes_source": "Ask hardening test fixture.",
            },
            "citations": [{"unit_id": first_unit_id, "support": "summary support"}],
            "related_sources": [],
        }
        write_json(source_dir / "knowledge.json", knowledge)
        (source_dir / "summary.md").write_text(
            "\n".join(
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
            ),
            encoding="utf-8",
        )

    def publish_seeded_corpus(self, workspace: WorkspacePaths) -> list[str]:
        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = [item["source_id"] for item in pending.payload["pending_sources"]]
        self.assertEqual(len(source_ids), 2)

        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[0],
            title="Campaign Planning Brief",
            summary="A strategy deck about architecture and operating model.",
            key_point="The strategy defines an architecture operating model.",
            claim="The architecture deck connects strategy to implementation.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[1],
            title="Campaign Evaluation Plan",
            summary="A delivery timeline and companion planning document.",
            key_point="The timeline explains rollout milestones.",
            claim="The timeline complements the architecture strategy.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def test_workflow_runner_reports_pending_authoring_for_sync(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")

        report = run_workflow("knowledge-base-sync", paths=workspace)

        self.assertIn(report.payload["status"], {"ready", "degraded", ACTION_REQUIRED})
        if report.payload["final_report"]["sync_status"] == "awaiting-confirmation":
            self.assertEqual(report.payload["status"], ACTION_REQUIRED)
            self.assertEqual(report.payload["workflow_status"], "needs-confirmation")
            self.assertIn("docmason sync --yes", " ".join(report.payload["next_steps"]))
            return

        self.assertIn(report.payload["final_report"]["sync_status"], {"valid", "warnings"})
        hybrid_mode = report.payload["final_report"]["hybrid_enrichment"]["mode"]
        if hybrid_mode == "candidate-prepared":
            self.assertEqual(report.payload["workflow_status"], "needs-hybrid-enrichment")
            self.assertIn("knowledge-construction", report.payload["next_workflows"])
        else:
            self.assertEqual(report.payload["workflow_status"], "completed")
            self.assertEqual(report.payload["next_workflows"], [])

    def test_workflow_runner_rejects_ask_as_public_execution_target(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        report = run_workflow("ask", paths=workspace)

        self.assertEqual(report.payload["status"], "action-required")
        self.assertIn("only natural-language question entry surface", report.payload["detail"])

    def test_complete_ask_turn_refreshes_review_summary(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-compose"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Help me draft the project exec summary wording for this deck.",
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text("Draft summary.\n", encoding="utf-8")
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
            inner_workflow_id="grounded-composition",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Draft summary.",
            source_escalation_used=False,
            evidence_mode=turn["evidence_mode"],
            research_depth=turn["research_depth"],
            bundle_paths=turn["bundle_paths"],
            status="answered",
        )

        summary = refresh_log_review_summary(workspace)
        recent_conversations = summary.get("conversations", {}).get("recent", [])
        self.assertEqual(len(recent_conversations), 1)
        review_report = review_runtime_logs(workspace)
        self.assertEqual(review_report.payload["status"], "ready")

    def test_prepare_ask_turn_suppresses_external_and_general_notices(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-external"}, clear=False):
            external_turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )
        self.assertEqual(external_turn["question_domain"], "external-factual")
        self.assertEqual(external_turn["status"], "prepared")
        self.assertFalse(external_turn["sync_suggested"])
        self.assertFalse(external_turn["interaction_sync_suggested"])
        self.assertIsNone(external_turn["freshness_notice"])

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-general"}, clear=False):
            general_turn = prepare_ask_turn(
                workspace,
                question="What is EBITDA?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="general-stable",
                ),
            )
        self.assertEqual(general_turn["question_domain"], "general-stable")
        self.assertEqual(general_turn["status"], "prepared")
        self.assertFalse(general_turn["sync_suggested"])
        self.assertIsNone(general_turn["freshness_notice"])

    def test_review_runtime_logs_writes_request_artifact_for_direct_workflow(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        report = run_workflow("runtime-log-review", paths=workspace)

        self.assertEqual(report.payload["status"], "degraded")
        request_path = workspace.root / str(report.payload["final_report"]["review_request_path"])
        artifact = read_json(request_path)
        self.assertEqual(artifact["entry_surface"], "workflow/runtime-log-review")
        self.assertIsNone(artifact["request_text"])
        self.assertEqual(artifact["final_status"], "degraded")
        self.assertTrue(artifact["stable_summary"])
        self.assertIn("runtime/logs/review/summary.json", artifact["derived_output_paths"])

    def test_review_runtime_logs_writes_request_artifact_for_ask_routed_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-review-request"}, clear=False):
            answered = prepare_ask_turn(
                workspace,
                question="What does the architecture strategy actually say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        answer_path = workspace.root / answered["answer_file_path"]
        answer_path.write_text("The architecture strategy defines the operating model.\n", encoding="utf-8")
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=answered["log_context"],
        )
        complete_ask_turn(
            workspace,
            conversation_id=answered["conversation_id"],
            turn_id=answered["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=answered["answer_file_path"],
            response_excerpt="Answered summary.",
            status="answered",
        )

        review_question = (
            "Please review the two most recent canonical ask turns and summarize their failure points."
        )
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-review-request"}, clear=False):
            opened = handle_hidden_ask_request(
                {
                    "action": "open",
                    "question": review_question,
                    "semantic_analysis": self.semantic_analysis(
                        question_class="runtime-review",
                        question_domain="workspace-corpus",
                    ),
                    "host_provider": "codex",
                    "host_thread_ref": "thread-review-request",
                    "host_identity_source": "codex_thread_id",
                },
                paths=workspace,
            )
            report = review_runtime_logs(workspace)

        self.assertEqual(opened["status"], "execute")
        request_path = workspace.root / str(report.payload["review_request_path"])
        artifact = read_json(request_path)
        self.assertEqual(artifact["entry_surface"], "ask/runtime-log-review")
        self.assertEqual(artifact["request_text"], review_question)
        self.assertEqual(artifact["conversation_id"], opened["conversation_id"])
        self.assertEqual(artifact["turn_id"], opened["turn_id"])
        self.assertIn(answered["conversation_id"], artifact["consulted_conversation_ids"])

    def test_visual_odd_question_prefers_published_render_and_structure(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-odd"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question=(
                    "What visual style and layout rhythm does the architecture "
                    "strategy deck use?"
                ),
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        retrieval = retrieve_corpus(
            workspace,
            query="architecture strategy visual style layout rhythm",
            top=2,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=True,
            log_context=turn["log_context"],
            question_domain=turn["question_domain"],
            evidence_requirements=turn["evidence_requirements"],
        )
        self.assertEqual(retrieval["preferred_channels"], ["render", "structure"])
        self.assertTrue(retrieval["published_artifacts_sufficient"])
        self.assertFalse(retrieval["source_escalation_required"])
        self.assertTrue(retrieval["results"])
        self.assertIn("render", retrieval["results"][0]["available_channels"])
        self.assertIn("structure", retrieval["results"][0]["available_channels"])
        self.assertIn("render", retrieval["results"][0]["matched_units"][0]["available_channels"])
        self.assertIn(
            "structure",
            retrieval["results"][0]["matched_units"][0]["available_channels"],
        )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            (
                "The deck uses a published slide-style visual presentation with "
                "structured page metadata."
            ),
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        self.assertEqual(trace["preferred_channels"], ["render", "structure"])
        self.assertIn("render", trace["used_published_channels"])
        self.assertIn("structure", trace["used_published_channels"])
        self.assertTrue(trace["published_artifacts_sufficient"])
        self.assertFalse(trace["source_escalation_required"])
        self.assertTrue(trace["render_inspection_required"])

    def test_complete_ask_turn_autohydrates_affordance_fields_from_trace(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-autohydrate"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question=(
                    "What visual style and layout rhythm does the architecture "
                    "strategy deck use?"
                ),
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            (
                "The deck uses a published slide-style visual presentation with "
                "structured page metadata."
            ),
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
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Published odd-question answer.",
            status="answered",
        )

        self.assertEqual(completed["inspection_scope"], "unit")
        self.assertEqual(completed["preferred_channels"], ["render", "structure"])
        self.assertIn("render", completed["used_published_channels"])
        self.assertTrue(completed["published_artifacts_sufficient"])
        self.assertFalse(completed["source_escalation_required"])
        self.assertEqual(completed["answer_state"], trace["answer_state"])
        self.assertEqual(
            completed["render_inspection_required"],
            trace["render_inspection_required"],
        )

        query_session = read_json(workspace.query_sessions_dir / f"{trace['session_id']}.json")
        self.assertEqual(query_session["inspection_scope"], "unit")
        self.assertEqual(query_session["preferred_channels"], ["render", "structure"])
        self.assertTrue(query_session["published_artifacts_sufficient"])

        trace_report = trace_knowledge(
            answer_file=str(answer_path),
            top=2,
            paths=workspace,
        )
        self.assertEqual(trace_report.payload["preferred_channels"], ["render", "structure"])
        self.assertTrue(
            any("Published evidence;" in line for line in trace_report.lines),
        )
        journal_events = [
            json.loads(line)["event_type"]
            for line in run_journal_path(workspace, turn["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertIn("trace-completed", journal_events)
        self.assertIn("admissibility-passed", journal_events)
        self.assertIn("projection-enqueued", journal_events)

    def test_write_hybrid_refresh_work_persists_narrowed_source_packet(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "thread-hybrid-refresh"},
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What is shown on the scanned workflow page image?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        retrieval = retrieve_corpus(
            workspace,
            query="scanned workflow page image",
            top=2,
            graph_hops=0,
            document_types=None,
            source_ids=None,
            include_renders=True,
            log_context=turn["log_context"],
            question_domain=turn["question_domain"],
            evidence_requirements=turn["evidence_requirements"],
        )
        source_ids = [
            item["source_id"]
            for item in retrieval["recommended_hybrid_targets"]
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        refresh_path = write_hybrid_refresh_work(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            query="scanned workflow page image",
            source_ids=source_ids[:1],
            recommended_targets=retrieval["recommended_hybrid_targets"][:1],
        )
        payload = read_json(workspace.root / refresh_path)
        self.assertEqual(payload["query"], "scanned workflow page image")
        self.assertEqual(payload["selected_source_ids"], source_ids[:1])
        self.assertEqual(len(payload["sources"]), 1)
        self.assertTrue(payload["sources"][0]["units"])

    def test_complete_ask_turn_persists_hybrid_refresh_fields(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-hybrid-state"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        (workspace.root / turn["answer_file_path"]).write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=workspace.root / turn["answer_file_path"],
            top=2,
            log_context=turn["log_context"],
        )
        update_conversation_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            updates={
                "session_ids": [trace["session_id"]],
                "trace_ids": [trace["trace_id"]],
            },
        )
        live_turn = self.load_conversation(
            workspace,
            conversation_id=turn["conversation_id"],
        )["turns"][0]
        snapshot_id = live_turn["version_context"]["published_snapshot_id"]
        lane_c_source_id = source_ids[0]
        lane_c_job = ensure_shared_job(
            workspace,
            job_key=lane_c_job_key(
                published_snapshot_id=snapshot_id,
                source_id=lane_c_source_id,
            ),
            job_family="lane-c",
            criticality="answer-critical",
            scope={
                "target": "current",
                "published_snapshot_id": snapshot_id,
                "source_id": lane_c_source_id,
            },
            input_signature=lane_c_job_key(
                published_snapshot_id=snapshot_id,
                source_id=lane_c_source_id,
            ),
            owner={"kind": "run", "id": turn["run_id"]},
            run_id=turn["run_id"],
        )["manifest"]
        complete_control_plane_job(
            workspace,
            str(lane_c_job["job_id"]),
            result={"status": "covered"},
        )
        refreshed_trace = trace_answer_file(
            workspace,
            answer_file=workspace.root / turn["answer_file_path"],
            top=2,
            log_context=turn["log_context"],
        )

        completed = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[refreshed_trace["session_id"]],
            trace_ids=[refreshed_trace["trace_id"]],
            response_excerpt="Hybrid refresh completed for the selected source.",
            status="answered",
            hybrid_refresh_triggered=True,
            hybrid_refresh_sources=[lane_c_source_id],
            hybrid_refresh_completion_status="covered",
            hybrid_refresh_summary={
                "mode": "ask-hybrid",
                "covered_source_count": 1,
            },
            hybrid_refresh_snapshot_id=snapshot_id,
            hybrid_refresh_job_ids=[str(lane_c_job["job_id"])],
        )
        self.assertTrue(completed["hybrid_refresh_triggered"])
        self.assertEqual(completed["hybrid_refresh_sources"], [lane_c_source_id])
        self.assertEqual(completed["hybrid_refresh_completion_status"], "covered")
        self.assertEqual(
            completed["hybrid_refresh_summary"]["covered_source_count"],
            1,
        )
        self.assertEqual(completed["session_ids"][-1], refreshed_trace["session_id"])
        self.assertEqual(completed["trace_ids"][-1], refreshed_trace["trace_id"])
        self.assertIsNone(completed["freshness_notice"])

    def test_complete_ask_turn_rejects_pre_refresh_trace_for_covered_hybrid_refresh(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-hybrid-pre-refresh"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        (workspace.root / turn["answer_file_path"]).write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=workspace.root / turn["answer_file_path"],
            top=2,
            log_context=turn["log_context"],
        )
        update_conversation_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            updates={
                "session_ids": [trace["session_id"]],
                "trace_ids": [trace["trace_id"]],
            },
        )
        live_turn = self.load_conversation(
            workspace,
            conversation_id=turn["conversation_id"],
        )["turns"][0]
        snapshot_id = live_turn["version_context"]["published_snapshot_id"]
        lane_c_source_id = source_ids[0]
        lane_c_job = ensure_shared_job(
            workspace,
            job_key=lane_c_job_key(
                published_snapshot_id=snapshot_id,
                source_id=lane_c_source_id,
            ),
            job_family="lane-c",
            criticality="answer-critical",
            scope={
                "target": "current",
                "published_snapshot_id": snapshot_id,
                "source_id": lane_c_source_id,
            },
            input_signature=lane_c_job_key(
                published_snapshot_id=snapshot_id,
                source_id=lane_c_source_id,
            ),
            owner={"kind": "run", "id": turn["run_id"]},
            run_id=turn["run_id"],
        )["manifest"]
        complete_control_plane_job(
            workspace,
            str(lane_c_job["job_id"]),
            result={"status": "covered"},
        )

        with self.assertRaisesRegex(ValueError, "post-refresh retrieve session recorded after the shared job settled"):
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                session_ids=[trace["session_id"]],
                trace_ids=[trace["trace_id"]],
                response_excerpt="Old pre-refresh evidence should not satisfy covered refresh.",
                status="answered",
                hybrid_refresh_triggered=True,
                hybrid_refresh_sources=[lane_c_source_id],
                hybrid_refresh_completion_status="covered",
                hybrid_refresh_summary={
                    "mode": "ask-hybrid",
                    "covered_source_count": 1,
                },
                hybrid_refresh_snapshot_id=snapshot_id,
                hybrid_refresh_job_ids=[str(lane_c_job["job_id"])],
            )

    def test_trace_answer_file_persists_version_context(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-version-trace"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        live_turn = self.load_conversation(
            workspace,
            conversation_id=turn["conversation_id"],
        )["turns"][0]
        query_session = read_json(workspace.query_sessions_dir / f"{trace['session_id']}.json")
        self.assertEqual(
            trace["version_context"]["published_snapshot_id"],
            live_turn["version_context"]["published_snapshot_id"],
        )
        self.assertEqual(
            trace["version_context"]["published_source_signature"],
            live_turn["version_context"]["published_source_signature"],
        )
        self.assertEqual(
            query_session["version_context"]["published_snapshot_id"],
            live_turn["version_context"]["published_snapshot_id"],
        )

    def test_complete_ask_turn_rejects_missing_trace_version_truth(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-missing-trace-version"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        trace_path = workspace.retrieval_traces_dir / f"{trace['trace_id']}.json"
        trace_payload = read_json(trace_path)
        trace_payload.pop("version_context", None)
        write_json(trace_path, trace_payload)

        with self.assertRaisesRegex(ValueError, "trace version truth"):
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                session_ids=[trace["session_id"]],
                trace_ids=[trace["trace_id"]],
                answer_file_path=turn["answer_file_path"],
                response_excerpt="The planning brief connects strategy to implementation.",
                status="answered",
            )

    def test_complete_ask_turn_rejects_flattening_unresolved_trace_gap(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/scan.pdf"],
            title="Scanned Workflow Page",
            summary="A scanned workflow page with limited extracted text.",
            key_point="The published baseline preserves the rendered page but not enough semantic detail.",
            claim="This page requires multimodal follow-up before confident semantic use.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/control.pdf"],
            title="Control Page",
            summary="A control page with no scan-specific signals.",
            key_point="The control page is intentionally generic.",
            claim="The control page should rank lower for scanned-page queries.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-flat-gap"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What is shown on the scanned workflow page image?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The current published artifacts are not sufficient for a reliable semantic answer.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )

        with self.assertRaisesRegex(ValueError, "latest ask-owned trace still records"):
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                session_ids=[trace["session_id"]],
                trace_ids=[trace["trace_id"]],
                answer_file_path=turn["answer_file_path"],
                response_excerpt="Incorrectly flattened answer state.",
                published_artifacts_sufficient=True,
                source_escalation_required=False,
                status="answered",
            )

    def test_complete_ask_turn_requires_trace_for_kb_grounded_commit(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-trace-required"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        retrieve_corpus(
            workspace,
            query="campaign planning brief",
            top=2,
            graph_hops=0,
            document_types=None,
            source_ids=None,
            include_renders=False,
            log_context=turn["log_context"],
            question_domain=turn["question_domain"],
            evidence_requirements=turn["evidence_requirements"],
        )
        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "ask-owned trace"):
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                answer_file_path=turn["answer_file_path"],
                response_excerpt="The planning brief connects strategy to implementation.",
                status="answered",
            )

    def test_complete_ask_turn_rejects_flattening_retrieve_only_gap_without_trace(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/scan.pdf"],
            title="Scanned Workflow Page",
            summary="A scanned workflow page with limited extracted text.",
            key_point="The published baseline preserves the rendered page but not enough semantic detail.",
            claim="This page requires multimodal follow-up before confident semantic use.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/control.pdf"],
            title="Control Page",
            summary="A control page with no scan-specific signals.",
            key_point="The control page is intentionally generic.",
            claim="The control page should rank lower for scanned-page queries.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-retrieve-gap"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What is shown on the scanned workflow page image?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        retrieval = retrieve_corpus(
            workspace,
            query="scanned workflow page image",
            top=2,
            graph_hops=0,
            document_types=None,
            source_ids=None,
            include_renders=True,
            log_context=turn["log_context"],
            question_domain=turn["question_domain"],
            evidence_requirements=turn["evidence_requirements"],
        )
        self.assertFalse(retrieval["published_artifacts_sufficient"])
        self.assertTrue(retrieval["source_escalation_required"])
        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The page definitely shows an approved workflow without needing more inspection.",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "latest ask-owned query session"):
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                answer_file_path=turn["answer_file_path"],
                response_excerpt="Incorrectly flattened retrieve-only state.",
                published_artifacts_sufficient=True,
                source_escalation_required=False,
                status="answered",
            )
        
        blocked_turn = load_turn_record(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
        )
        self.assertEqual(blocked_turn["primary_issue_code"], "published-artifacts-gap")
        self.assertEqual(
            blocked_turn["issue_codes"][:2],
            ["published-artifacts-gap", "source-escalation-required"],
        )
        self.assertIn("missing-ask-owned-trace", blocked_turn["issue_codes"])

    def test_complete_ask_turn_autolinks_unique_runtime_artifacts_before_commit(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-autolink"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
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
            answer_file_path=turn["answer_file_path"],
            response_excerpt="The planning brief connects strategy to implementation.",
            support_basis="external-source-verified",
            support_manifest_sources=[
                {
                    "url": "https://example.com/planning-brief",
                    "title": "Campaign Planning Brief",
                    "source_type": "official-doc",
                    "support_snippet": "The planning brief connects strategy to implementation.",
                }
            ],
            support_manifest_key_assertions=[
                "The planning brief connects strategy to implementation."
            ],
            support_manifest_notes="Wrong-run trace must not be rebound to the canonical turn.",
            status="answered",
        )

        self.assertEqual(completed["session_ids"], [trace["session_id"]])
        self.assertEqual(completed["trace_ids"], [trace["trace_id"]])
        query_session = read_json(workspace.query_sessions_dir / f"{trace['session_id']}.json")
        trace_payload = read_json(workspace.retrieval_traces_dir / f"{trace['trace_id']}.json")
        self.assertEqual(query_session["run_id"], turn["run_id"])
        self.assertEqual(trace_payload["run_id"], turn["run_id"])
        self.assertEqual(query_session["conversation_id"], turn["conversation_id"])
        self.assertEqual(trace_payload["conversation_id"], turn["conversation_id"])
        self.assertEqual(query_session["turn_id"], turn["turn_id"])
        self.assertEqual(trace_payload["turn_id"], turn["turn_id"])
        self.assertEqual(query_session["front_door_state"], "canonical-ask")
        self.assertEqual(trace_payload["front_door_state"], "canonical-ask")
        self.assertEqual(query_session["support_basis"], completed["support_basis"])
        self.assertEqual(trace_payload["support_basis"], completed["support_basis"])
        journal_events = [
            json.loads(line)["event_type"]
            for line in run_journal_path(workspace, turn["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(journal_events.count("turn-committed"), 1)

    def test_complete_ask_turn_does_not_guess_ambiguous_runtime_artifacts(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-ambiguous-link"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text("Yes. Aliyun SMS supports HTTPS API access.", encoding="utf-8")
        for _ in range(2):
            retrieve_corpus(
                workspace,
                query="Aliyun SMS HTTPS API",
                top=2,
                graph_hops=0,
                document_types=None,
                source_ids=None,
                include_renders=False,
                log_context=turn["log_context"],
                question_domain=turn["question_domain"],
                evidence_requirements=turn["evidence_requirements"],
            )
            trace_answer_file(
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
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Aliyun SMS supports HTTPS API access.",
            question_domain=turn["question_domain"],
            support_basis="external-source-verified",
            support_manifest_sources=[
                {
                    "url": "https://example.com/aliyun-sms-https",
                    "title": "Aliyun SMS HTTPS API",
                    "source_type": "official-doc",
                    "support_snippet": "HTTPS access is documented explicitly.",
                }
            ],
            support_manifest_key_assertions=["Aliyun SMS supports HTTPS API access."],
            support_manifest_notes="Explicit external verification for ambiguity test.",
            status="answered",
        )

        self.assertEqual(completed["session_ids"], [])
        self.assertEqual(completed["trace_ids"], [])

    def test_runtime_logs_with_wrong_run_id_are_demoted_from_canonical_binding(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-wrong-run"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        wrong_log_context = {**turn["log_context"], "run_id": "run-does-not-exist"}
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=wrong_log_context,
        )

        completed = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            answer_file_path=turn["answer_file_path"],
            response_excerpt="The planning brief connects strategy to implementation.",
            support_basis="external-source-verified",
            support_manifest_sources=[
                {
                    "url": "https://example.com/planning-brief",
                    "title": "Campaign Planning Brief",
                    "source_type": "official-doc",
                    "support_snippet": "The planning brief connects strategy to implementation.",
                }
            ],
            support_manifest_key_assertions=[
                "The planning brief connects strategy to implementation."
            ],
            support_manifest_notes="Wrong-run trace must be demoted instead of rebound.",
            status="answered",
        )

        self.assertEqual(completed["session_ids"], [])
        self.assertEqual(completed["trace_ids"], [])
        query_session = read_json(workspace.query_sessions_dir / f"{trace['session_id']}.json")
        trace_payload = read_json(workspace.retrieval_traces_dir / f"{trace['trace_id']}.json")
        self.assertEqual(query_session["canonical_binding_status"], "demoted")
        self.assertEqual(trace_payload["canonical_binding_status"], "demoted")
        self.assertNotIn("conversation_id", query_session)
        self.assertNotIn("turn_id", query_session)
        self.assertNotIn("run_id", query_session)
        self.assertNotIn("conversation_id", trace_payload)

    def test_local_corpus_support_manifest_cannot_commit_as_external(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-local-external"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace_answer_file(
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
            answer_file_path=turn["answer_file_path"],
            response_excerpt="The planning brief connects strategy to implementation.",
            support_basis="external-source-verified",
            support_manifest_sources=[
                {
                    "url": "original_doc/a.pdf",
                    "title": "Campaign Planning Brief",
                    "source_type": "local-file",
                    "support_snippet": "The planning brief connects strategy to implementation.",
                }
            ],
            status="answered",
        )
        self.assertEqual(completed["support_basis"], "kb-grounded")

    def test_post_commit_trace_replay_is_demoted_from_canonical_binding(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-post-commit"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        first_trace = trace_answer_file(
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
            session_ids=[first_trace["session_id"]],
            trace_ids=[first_trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="The planning brief connects strategy to implementation.",
            status="answered",
        )

        replay_trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        replay_query_session = read_json(
            workspace.query_sessions_dir / f"{replay_trace['session_id']}.json"
        )
        replay_trace_payload = read_json(
            workspace.retrieval_traces_dir / f"{replay_trace['trace_id']}.json"
        )
        self.assertEqual(replay_query_session["canonical_binding_status"], "demoted")
        self.assertEqual(replay_trace_payload["canonical_binding_status"], "demoted")
        self.assertNotIn("conversation_id", replay_query_session)
        self.assertNotIn("turn_id", replay_query_session)
        self.assertNotIn("run_id", replay_query_session)
        self.assertNotIn("conversation_id", replay_trace_payload)
        self.assertNotIn("turn_id", replay_trace_payload)
        self.assertNotIn("run_id", replay_trace_payload)

    def test_commit_run_prefers_refreshed_run_version_truth(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-run-version"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        run_id = turn["run_id"]
        stale_turn_context = {
            "captured_at": "2026-03-25T00:00:00Z",
            "corpus_signature": "sig-old",
            "published_source_signature": "sig-old",
            "published_snapshot_id": "snapshot-old",
            "published_at": "2026-03-25T00:00:00Z",
            "answer_workflow_version": "phase-1-run-control",
        }
        refreshed_run_context = {
            "captured_at": "2026-03-25T00:05:00Z",
            "corpus_signature": "sig-new",
            "published_source_signature": "sig-new",
            "published_snapshot_id": "snapshot-new",
            "published_at": "2026-03-25T00:05:00Z",
            "answer_workflow_version": "phase-1-run-control",
        }
        update_conversation_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            updates={"version_context": stale_turn_context},
        )
        update_run_state(
            workspace,
            run_id=run_id,
            updates={"version_context": refreshed_run_context},
        )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        trace_path = workspace.retrieval_traces_dir / f"{trace['trace_id']}.json"
        trace_payload = read_json(trace_path)
        trace_payload["version_context"] = refreshed_run_context
        write_json(trace_path, trace_payload)

        completed = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="The planning brief connects strategy to implementation.",
            status="answered",
        )
        run_commit = read_json(workspace.runs_dir / run_id / "commit.json")
        self.assertEqual(run_commit["version_context"]["published_snapshot_id"], "snapshot-new")
        self.assertEqual(completed["version_context"]["published_snapshot_id"], "snapshot-new")

    def test_begin_lane_c_shared_refresh_reuses_shared_job_for_waiter(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        recommended_target = {
            "source_id": source_ids[0],
            "required_overlay_slots": ["diagram-summary"],
            "target_artifact_ids": [],
        }

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-lane-c-1"}, clear=False):
            first_turn = prepare_ask_turn(
                workspace,
                question="What does the image-heavy page mean?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-lane-c-2"}, clear=False):
            second_turn = prepare_ask_turn(
                workspace,
                question="What does the image-heavy page mean?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        first = begin_lane_c_shared_refresh(
            workspace,
            conversation_id=first_turn["conversation_id"],
            turn_id=first_turn["turn_id"],
            run_id=first_turn["run_id"],
            query="What does the image-heavy page mean?",
            recommended_targets=[recommended_target],
        )
        second = begin_lane_c_shared_refresh(
            workspace,
            conversation_id=second_turn["conversation_id"],
            turn_id=second_turn["turn_id"],
            run_id=second_turn["run_id"],
            query="What does the image-heavy page mean?",
            recommended_targets=[recommended_target],
        )

        self.assertEqual(first["caller_role"], "owner")
        self.assertEqual(second["caller_role"], "waiter")
        self.assertEqual(first["job_id"], second["job_id"])
        manifest = load_shared_job(workspace, str(first["job_id"]))
        self.assertCountEqual(
            manifest["attached_run_ids"],
            [first_turn["run_id"], second_turn["run_id"]],
        )
        owner_turn = self.load_conversation(
            workspace,
            conversation_id=first_turn["conversation_id"],
        )["turns"][0]
        self.assertEqual(owner_turn["status"], "waiting-shared-job")
        waiting_turn = self.load_conversation(
            workspace,
            conversation_id=second_turn["conversation_id"],
        )["turns"][0]
        self.assertEqual(waiting_turn["status"], "waiting-shared-job")
        settled = settle_lane_c_shared_refresh(
            workspace,
            conversation_id=first_turn["conversation_id"],
            turn_id=first_turn["turn_id"],
            job_id=str(first["job_id"]),
            completion_status="covered",
            summary={"covered_source_count": 1},
        )
        self.assertEqual(settled["manifest"]["status"], "completed")
        covered_turns = {
            turn["turn_id"]: turn for turn in settled["turns"] if isinstance(turn, dict)
        }
        self.assertEqual(covered_turns[first_turn["turn_id"]]["status"], "prepared")
        self.assertEqual(covered_turns[second_turn["turn_id"]]["status"], "prepared")
        second_live_turn = self.load_conversation(
            workspace,
            conversation_id=second_turn["conversation_id"],
        )["turns"][0]
        self.assertEqual(second_live_turn["status"], "prepared")
        self.assertEqual(
            second_live_turn["freshness_notice"],
            "The governed multimodal refresh finished. Rerun retrieval and trace before committing the answer.",
        )

    def test_settle_lane_c_shared_refresh_blocked_commits_boundary_for_waiters(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        recommended_target = {
            "source_id": source_ids[0],
            "required_overlay_slots": ["diagram-summary"],
            "target_artifact_ids": [],
        }

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-lane-c-block-1"}, clear=False):
            first_turn = prepare_ask_turn(
                workspace,
                question="What does the image-heavy page mean?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-lane-c-block-2"}, clear=False):
            second_turn = prepare_ask_turn(
                workspace,
                question="What does the image-heavy page mean?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        first = begin_lane_c_shared_refresh(
            workspace,
            conversation_id=first_turn["conversation_id"],
            turn_id=first_turn["turn_id"],
            run_id=first_turn["run_id"],
            query="What does the image-heavy page mean?",
            recommended_targets=[recommended_target],
        )
        begin_lane_c_shared_refresh(
            workspace,
            conversation_id=second_turn["conversation_id"],
            turn_id=second_turn["turn_id"],
            run_id=second_turn["run_id"],
            query="What does the image-heavy page mean?",
            recommended_targets=[recommended_target],
        )
        settled = settle_lane_c_shared_refresh(
            workspace,
            conversation_id=first_turn["conversation_id"],
            turn_id=first_turn["turn_id"],
            job_id=str(first["job_id"]),
            completion_status="blocked",
            summary={"detail": "The hybrid refresh could not continue safely."},
        )
        self.assertEqual(settled["manifest"]["status"], "blocked")
        for conversation_id, turn_id in [
            (first_turn["conversation_id"], first_turn["turn_id"]),
            (second_turn["conversation_id"], second_turn["turn_id"]),
        ]:
            live_turn = load_turn_record(
                workspace,
                conversation_id=conversation_id,
                turn_id=turn_id,
            )
            self.assertEqual(live_turn["status"], "completed")
            self.assertEqual(live_turn["support_basis"], "governed-boundary")
            self.assertEqual(live_turn["hybrid_refresh_completion_status"], "blocked")

    def test_complete_ask_turn_autogoverns_lane_c_from_trace_insufficiency(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/scan.pdf"],
            title="Scanned Workflow Page",
            summary="A scanned workflow page with limited extracted text.",
            key_point="The published baseline preserves the rendered page but not enough semantic detail.",
            claim="This page requires multimodal follow-up before confident semantic use.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/control.pdf"],
            title="Control Page",
            summary="A control page with no scan-specific signals.",
            key_point="The control page is intentionally generic.",
            claim="The control page should rank lower for scanned-page queries.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-lane-c-mainline"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What is shown on the scanned workflow page image?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The scanned workflow page appears to show a process diagram, but the current published artifacts are insufficient for a reliable semantic answer.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        self.assertTrue(trace["source_escalation_required"])
        self.assertFalse(trace["published_artifacts_sufficient"])
        self.assertTrue(trace["recommended_hybrid_targets"])

        transitioned = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt=(
                "The scanned workflow page needs a governed multimodal refresh before a final answer."
            ),
            status="answered",
        )
        self.assertEqual(transitioned["status"], "waiting-shared-job")
        self.assertTrue(transitioned["hybrid_refresh_triggered"])
        self.assertTrue(transitioned["hybrid_refresh_job_ids"])
        work_path = transitioned["hybrid_refresh_summary"]["work_path"]
        self.assertTrue((workspace.root / work_path).exists())
        lane_c_job = load_shared_job(workspace, transitioned["hybrid_refresh_job_ids"][0])
        self.assertEqual(lane_c_job["job_family"], "lane-c")
        self.assertEqual(lane_c_job["status"], "running")
        self.assertEqual(
            self.load_conversation(
                workspace,
                conversation_id=turn["conversation_id"],
            )["turns"][0]["status"],
            "waiting-shared-job",
        )
        self.assertFalse((workspace.runs_dir / turn["run_id"] / "commit.json").exists())

    def test_workspace_corpus_warm_start_requires_exact_corpus_signature_match(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-warm-start-sig"}, clear=False):
            first_turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
        answer_path = workspace.root / first_turn["answer_file_path"]
        answer_path.write_text(
            "The planning brief connects strategy to implementation.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=first_turn["log_context"],
        )
        complete_ask_turn(
            workspace,
            conversation_id=first_turn["conversation_id"],
            turn_id=first_turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=first_turn["answer_file_path"],
            response_excerpt="The planning brief connects strategy to implementation.",
            status="answered",
        )
        write_json(
            workspace.sync_state_path,
            {
                **read_json(workspace.sync_state_path),
                "published_source_signature": "sig-different",
            },
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-warm-start-sig"}, clear=False):
            second_turn = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertFalse(second_turn["warm_start_evidence"]["matched_records"])

    def test_source_scope_sufficiency_backfills_from_matched_units_for_legacy_records(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        source_records = read_json(workspace.retrieval_source_records_path("current"))
        for record in source_records.get("records", []):
            if not isinstance(record, dict):
                continue
            record.pop("available_channels", None)
            record.pop("channel_descriptors", None)
            record.pop("affordance_confidence", None)
            record.pop("affordance_derivation_mode", None)
        write_json(workspace.retrieval_source_records_path("current"), source_records)

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "thread-source-scope-backfill"},
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question=(
                    "What tone and rhetorical posture does the architecture "
                    "strategy document use?"
                ),
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["text", "structure"],
                        "inspection_scope": "source",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        retrieval = retrieve_corpus(
            workspace,
            query="architecture strategy tone rhetorical posture",
            top=2,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=True,
            log_context=turn["log_context"],
            question_domain=turn["question_domain"],
            evidence_requirements=turn["evidence_requirements"],
        )

        self.assertEqual(retrieval["preferred_channels"], ["text", "structure"])
        self.assertIn("structure", retrieval["matched_published_channels"])
        self.assertTrue(retrieval["published_artifacts_sufficient"])
        self.assertFalse(retrieval["source_escalation_required"])

    def test_prepare_ask_turn_reuses_same_live_question_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-reuse"}, clear=False):
            first = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )
            second = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(first["turn_id"], second["turn_id"])
        conversation = self.load_conversation(
            workspace,
            conversation_id=first["conversation_id"],
        )
        self.assertEqual(len(conversation["turns"]), 1)

    def test_prepare_ask_turn_reuses_same_run_preanswer_governance_after_auto_sync(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        self.create_pdf(workspace.source_dir / "fresh.pdf")

        def fake_sync(*args: object, **kwargs: object) -> CommandReport:
            return sync_workspace(
                workspace,
                assume_yes=True,
                owner=kwargs.get("owner"),
                run_id=kwargs.get("run_id"),
            )

        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-governance-reuse"}, clear=False),
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
        ):
            first = prepare_ask_turn(
                workspace,
                question="What do the latest workspace documents say about the architecture strategy?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )
            second = prepare_ask_turn(
                workspace,
                question="What do the latest workspace documents say about the architecture strategy?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )

        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(first["run_id"], second["run_id"])
        journal_entries = [
            json.loads(line)
            for line in run_journal_path(workspace, first["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_types = [entry["event_type"] for entry in journal_entries]
        self.assertEqual(event_types.count("preanswer-governance-started"), 1)
        self.assertEqual(event_types.count("ask-prepared"), 1)
        self.assertIn("preanswer-governance-reused", event_types)
        sync_job_ids = [
            entry["payload"]["job_id"]
            for entry in journal_entries
            if entry["event_type"] == "shared-job-attached"
            and isinstance(entry.get("payload"), dict)
            and isinstance(entry["payload"].get("job_id"), str)
        ]
        self.assertEqual(len(set(sync_job_ids)), 1)
        run_state = load_run_state(workspace, first["run_id"])
        self.assertEqual(
            run_state["execution_cost_profile"]["phase_counts"].get("preanswer_governance"),
            1,
        )
        self.assertEqual(run_state["preanswer_governance_state"]["turn_status"], "prepared")
        self.assertTrue(second["auto_sync_triggered"])

    def test_prepare_ask_turn_reuses_awaiting_confirmation_without_restarting_governance(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")

        sync_report = CommandReport(
            1,
            {
                "status": ACTION_REQUIRED,
                "sync_status": "awaiting-confirmation",
                "detail": "Material sync confirmation is required.",
                "published": False,
                "control_plane": {
                    "state": "awaiting-confirmation",
                    "shared_job_id": "job-sync-001",
                    "shared_job_key": "sync:test",
                    "job_family": "sync",
                    "confirmation_kind": "material-sync",
                    "confirmation_prompt": (
                        "A large unpublished workspace change set was detected. "
                        "Build or refresh the knowledge base now before continuing this question?"
                    ),
                    "confirmation_reason": "changed_total=12 >= 12",
                    "attached_run_count": 1,
                    "next_command": "docmason sync --yes",
                },
            },
            [],
        )

        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-confirm-reuse"}, clear=False),
            mock.patch("docmason.ask.run_sync_command", return_value=sync_report) as sync_mock,
        ):
            first = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say about the proposal?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )
            second = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say about the proposal?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )

        self.assertEqual(sync_mock.call_count, 1)
        self.assertEqual(first["status"], "awaiting-confirmation")
        self.assertEqual(second["status"], "awaiting-confirmation")
        self.assertEqual(first["run_id"], second["run_id"])
        journal_events = [
            json.loads(line)["event_type"]
            for line in run_journal_path(workspace, first["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(journal_events.count("preanswer-governance-started"), 1)
        self.assertIn("preanswer-governance-reused", journal_events)

    def test_prepare_ask_turn_reuses_waiting_shared_job_without_restarting_governance(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")

        sync_report = CommandReport(
            0,
            {
                "status": "degraded",
                "sync_status": "waiting-shared-job",
                "detail": "A matching shared sync job is already running.",
                "published": False,
                "control_plane": {
                    "state": "waiting-shared-job",
                    "shared_job_id": "job-sync-waiting-001",
                    "shared_job_key": "sync:test:waiting",
                    "job_family": "sync",
                    "attached_run_count": 1,
                },
            },
            [],
        )

        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-waiting-reuse"}, clear=False),
            mock.patch("docmason.ask.run_sync_command", return_value=sync_report) as sync_mock,
        ):
            first = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say about the proposal?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )
            second = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say about the proposal?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )

        self.assertEqual(sync_mock.call_count, 1)
        self.assertEqual(first["status"], "waiting-shared-job")
        self.assertEqual(second["status"], "waiting-shared-job")
        journal_events = [
            json.loads(line)["event_type"]
            for line in run_journal_path(workspace, first["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(journal_events.count("preanswer-governance-started"), 1)
        self.assertIn("preanswer-governance-reused", journal_events)

    def test_prepare_ask_turn_invalidates_same_run_governance_when_semantic_profile_changes(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-governance-invalidate-semantic"}, clear=False):
            first = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                    route_reason="First routing decision.",
                ),
            )
            second = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="general-stable",
                    route_reason="Second routing decision.",
                ),
            )

        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(first["turn_id"], second["turn_id"])
        self.assertEqual(first["run_id"], second["run_id"])
        journal_entries = [
            json.loads(line)
            for line in run_journal_path(workspace, first["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        event_types = [entry["event_type"] for entry in journal_entries]
        self.assertEqual(event_types.count("preanswer-governance-started"), 2)
        self.assertIn("preanswer-governance-invalidated", event_types)
        invalidation_payload = next(
            entry["payload"]
            for entry in journal_entries
            if entry["event_type"] == "preanswer-governance-invalidated"
        )
        self.assertIn("profile-changed", invalidation_payload["reasons"])

    def test_prepare_ask_turn_invalidates_same_run_governance_when_publish_truth_changes(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-governance-invalidate-publish"}, clear=False):
            first = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )
            write_json(
                workspace.current_publish_manifest_path,
                {
                    "snapshot_id": "snapshot-updated-for-invalidation",
                    "published_at": "2026-03-29T00:00:00Z",
                },
            )
            second = prepare_ask_turn(
                workspace,
                question="What does the campaign planning brief say?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(first["run_id"], second["run_id"])
        journal_entries = [
            json.loads(line)
            for line in run_journal_path(workspace, first["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        invalidation_payload = next(
            entry["payload"]
            for entry in journal_entries
            if entry["event_type"] == "preanswer-governance-invalidated"
        )
        self.assertIn("published-snapshot-changed", invalidation_payload["reasons"])

    def test_prepare_ask_turn_surfaces_shared_sync_confirmation_state(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")

        with (
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-sync-confirm"}, clear=False),
            mock.patch(
                "docmason.ask.run_sync_command",
                return_value=CommandReport(
                    1,
                    {
                        "status": ACTION_REQUIRED,
                        "sync_status": "awaiting-confirmation",
                        "detail": "Material sync confirmation is required.",
                        "published": False,
                        "control_plane": {
                            "state": "awaiting-confirmation",
                            "shared_job_id": "job-sync-001",
                            "shared_job_key": "sync:test",
                            "job_family": "sync",
                            "confirmation_kind": "material-sync",
                            "confirmation_prompt": (
                                "A large unpublished workspace change set was detected. "
                                "Build or refresh the knowledge base now before continuing this question?"
                            ),
                            "confirmation_reason": "changed_total=12 >= 12",
                            "attached_run_count": 1,
                            "next_command": "docmason sync --yes",
                        },
                    },
                    [],
                ),
            ),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="What does the workspace corpus say about the proposal?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    needs_latest_workspace_state=True,
                ),
            )

        self.assertEqual(turn["status"], "awaiting-confirmation")
        self.assertEqual(turn["confirmation_kind"], "material-sync")
        self.assertEqual(turn["attached_shared_job_ids"], ["job-sync-001"])
        conversation = self.load_conversation(
            workspace,
            conversation_id=turn["conversation_id"],
        )
        self.assertEqual(conversation["turns"][0]["turn_state"], "awaiting-confirmation")
        self.assertEqual(
            conversation["turns"][0]["attached_shared_job_ids"],
            ["job-sync-001"],
        )
        journal_events = [
            json.loads(line)["event_type"]
            for line in run_journal_path(workspace, turn["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertIn("preanswer-governance-started", journal_events)
        self.assertIn("shared-job-attached", journal_events)
        self.assertIn("shared-job-waiting", journal_events)

    def test_same_session_confirmation_decline_commits_governed_boundary(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-confirm-no"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )
            job = ensure_shared_job(
                workspace,
                job_key="prepare:test:high-intrusion:cap",
                job_family="prepare",
                criticality="answer-critical",
                scope={"workspace_root": str(workspace.root)},
                input_signature="cap",
                owner={"kind": "command", "id": "prepare-command"},
                run_id=turn["run_id"],
                requires_confirmation=True,
                confirmation_kind="high-intrusion-prepare",
                confirmation_prompt=(
                    "This question requires additional local dependencies before it can continue "
                    "safely. Prepare the workspace now?"
                ),
                confirmation_reason="office-rendering",
            )["manifest"]
            updated = update_conversation_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                updates={
                    "turn_state": "awaiting-confirmation",
                    "status": "awaiting-confirmation",
                    "attached_shared_job_ids": [job["job_id"]],
                    "confirmation_kind": "high-intrusion-prepare",
                    "confirmation_prompt": job["confirmation_prompt"],
                    "confirmation_reason": job["confirmation_reason"],
                },
            )

            declined = prepare_ask_turn(workspace, question="no")

        self.assertEqual(declined["conversation_id"], turn["conversation_id"])
        self.assertEqual(declined["turn_id"], turn["turn_id"])
        self.assertEqual(declined["answer_state"], "abstained")
        self.assertEqual(declined["support_basis"], "governed-boundary")
        self.assertEqual(declined["status"], "completed")
        answer_path = workspace.root / updated["answer_file_path"]
        self.assertTrue(answer_path.read_text(encoding="utf-8").strip())
        journal_events = [
            json.loads(line)["event_type"]
            for line in run_journal_path(workspace, turn["run_id"]).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertIn("preanswer-governance-started", journal_events)
        self.assertIn("shared-job-declined", journal_events)
        self.assertIn("shared-job-settled", journal_events)
        self.assertIn("projection-enqueued", journal_events)

    def test_same_session_confirmation_approve_reuses_same_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-confirm-yes"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )
            job = ensure_shared_job(
                workspace,
                job_key="prepare:test:high-intrusion:cap",
                job_family="prepare",
                criticality="answer-critical",
                scope={"workspace_root": str(workspace.root)},
                input_signature="cap",
                owner={"kind": "command", "id": "prepare-command"},
                run_id=turn["run_id"],
                requires_confirmation=True,
                confirmation_kind="high-intrusion-prepare",
                confirmation_prompt=(
                    "This question requires additional local dependencies before it can continue "
                    "safely. Prepare the workspace now?"
                ),
                confirmation_reason="office-rendering",
            )["manifest"]
            update_conversation_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                updates={
                    "turn_state": "awaiting-confirmation",
                    "status": "awaiting-confirmation",
                    "attached_shared_job_ids": [job["job_id"]],
                    "confirmation_kind": "high-intrusion-prepare",
                    "confirmation_prompt": job["confirmation_prompt"],
                    "confirmation_reason": job["confirmation_reason"],
                },
            )
            with (
                mock.patch(
                    "docmason.ask.prepare_workspace",
                    return_value=CommandReport(
                        0,
                        {"status": READY, "prepare_status": READY, "control_plane": {}},
                        [],
                    ),
                ),
                mock.patch("docmason.ask.maybe_reconcile_active_thread") as reconcile_mock,
            ):
                resumed = prepare_ask_turn(workspace, question="yes")
                reconcile_mock.assert_not_called()

        self.assertEqual(resumed["conversation_id"], turn["conversation_id"])
        self.assertEqual(resumed["turn_id"], turn["turn_id"])
        self.assertEqual(resumed["status"], "prepared")

    def test_legacy_active_conversation_and_projection_backfill_to_live_state(self) -> None:
        workspace = self.make_workspace()
        legacy_conversation_id = "legacy-thread"
        write_json(
            workspace.legacy_active_conversation_path,
            {
                "conversation_id": legacy_conversation_id,
                "agent_surface": "unknown-agent",
                "updated_at": "2026-03-24T00:00:00Z",
            },
        )
        write_json(
            workspace.conversation_projections_dir / f"{legacy_conversation_id}.json",
            {
                "conversation_id": legacy_conversation_id,
                "conversation_id_source": "workspace-active-fallback",
                "agent_surface": "unknown-agent",
                "opened_at": "2026-03-24T00:00:00Z",
                "updated_at": "2026-03-24T00:00:00Z",
                "workspace_snapshot": {
                    "captured_at": "2026-03-24T00:00:00Z",
                    "knowledge_base": {
                        "present": False,
                        "stale": False,
                        "validation_status": "not-run",
                        "last_publish_at": None,
                    },
                    "corpus_signature": None,
                    "source_inventory_signature": None,
                },
                "turns": [
                    {
                        "turn_id": "turn-001",
                        "user_question": "Does Aliyun SMS support HTTPS API?",
                        "entry_workflow_id": "ask",
                        "answer_file_path": f"runtime/answers/{legacy_conversation_id}/turn-001.md",
                        "status": "opened",
                        "updated_at": "2026-03-24T00:00:00Z",
                    }
                ],
            },
        )

        with mock.patch.dict(
            os.environ,
            {
                "DOCMASON_AGENT_SURFACE": "unknown-agent",
                "CODEX_THREAD_ID": "",
                "DOCMASON_CONVERSATION_ID": "",
                "CLAUDE_CONVERSATION_ID": "",
                "CLAUDE_SESSION_ID": "",
                "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "",
            },
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        self.assertEqual(turn["conversation_id"], legacy_conversation_id)
        self.assertTrue((workspace.conversations_dir / f"{legacy_conversation_id}.json").exists())

    def test_legacy_native_only_host_conversation_is_not_rebound_as_canonical_truth(self) -> None:
        workspace = self.make_workspace()
        legacy_thread_id = "thread-legacy-native-only"
        write_json(
            workspace.conversations_dir / f"{legacy_thread_id}.json",
            {
                "conversation_id": legacy_thread_id,
                "conversation_id_source": "codex_thread_id",
                "agent_surface": "codex",
                "opened_at": "2026-03-24T00:00:00Z",
                "updated_at": "2026-03-24T00:00:00Z",
                "workspace_snapshot": {},
                "turns": [
                    {
                        "turn_id": "turn-001",
                        "native_turn_id": "native-turn-001",
                        "user_question": "Legacy native-only prompt",
                        "entry_workflow_id": "ask",
                        "front_door_state": "native-reconciled-only",
                        "status": "completed",
                        "turn_state": "completed",
                        "updated_at": "2026-03-24T00:00:00Z",
                    }
                ],
            },
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": legacy_thread_id}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        self.assertNotEqual(turn["conversation_id"], legacy_thread_id)
        current_conversation = self.load_conversation(
            workspace,
            conversation_id=turn["conversation_id"],
        )
        self.assertEqual(current_conversation["turns"][0]["front_door_state"], "canonical-ask")
        legacy_conversation = self.load_conversation(
            workspace,
            conversation_id=legacy_thread_id,
        )
        self.assertEqual(legacy_conversation["turns"][0]["front_door_state"], "native-reconciled-only")

    def test_claude_project_dir_weak_identity_upgrades_to_session_binding_without_split(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        weak_env = {
            "DOCMASON_AGENT_SURFACE": "claude-code",
            "CODEX_THREAD_ID": "",
            "CLAUDE_PROJECT_DIR": str(workspace.root),
            "CLAUDE_SESSION_ID": "",
            "CLAUDE_CONVERSATION_ID": "",
        }
        with mock.patch.dict(os.environ, weak_env, clear=False):
            weak_turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        weak_conversation = self.load_conversation(
            workspace,
            conversation_id=weak_turn["conversation_id"],
        )
        self.assertEqual(
            weak_conversation["host_identity"]["host_identity_source"],
            "claude_project_dir",
        )
        self.assertIsNone(weak_conversation["host_identity_key"])

        strong_env = {
            **weak_env,
            "CLAUDE_SESSION_ID": "session-1",
        }
        with mock.patch.dict(os.environ, strong_env, clear=False):
            strong_turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        self.assertEqual(strong_turn["conversation_id"], weak_turn["conversation_id"])
        self.assertEqual(strong_turn["turn_id"], weak_turn["turn_id"])
        upgraded_conversation = self.load_conversation(
            workspace,
            conversation_id=strong_turn["conversation_id"],
        )
        self.assertEqual(
            upgraded_conversation["host_identity"]["host_identity_source"],
            "claude_session_id",
        )
        self.assertIsInstance(upgraded_conversation["host_identity_key"], str)
        bindings = read_json(workspace.host_identity_bindings_path)
        self.assertEqual(len(bindings["bindings"]), 1)

    def test_external_support_manifest_drives_trace_and_history_without_answer_cache(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-support"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "支持。阿里云短信服务支持 HTTPS API，请使用 HTTPS 端口和官方 endpoint。",
            encoding="utf-8",
        )
        first_completion = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            answer_file_path=turn["answer_file_path"],
            response_excerpt="支持 HTTPS API。",
            question_domain=turn["question_domain"],
            support_basis="external-source-verified",
            support_manifest_sources=[
                {
                    "url": "https://help.aliyun.com/zh/sms/getting-started/use-sms-api",
                    "title": "Aliyun SMS API",
                    "source_type": "official-doc",
                    "support_snippet": "HTTP 80 and HTTPS 443 are both documented.",
                }
            ],
            support_manifest_key_assertions=["Aliyun SMS supports HTTPS API access."],
            support_manifest_notes="Verified from official documentation.",
            status="answered",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        with self.assertRaisesRegex(ValueError, "already-committed-canonical-turn"):
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
                response_excerpt="支持 HTTPS API。",
                question_domain=turn["question_domain"],
                support_basis="external-source-verified",
                support_manifest_path=first_completion["support_manifest_path"],
                status="answered",
            )

        self.assertEqual(trace["status"], "ready")
        self.assertEqual(trace["answer_state"], "grounded")
        self.assertEqual(trace["support_basis"], "external-source-verified")
        self.assertFalse(trace["render_inspection_required"])
        self.assertEqual(
            trace["support_manifest_path"],
            first_completion["support_manifest_path"],
        )

        refresh_log_review_summary(workspace)
        benchmark_candidates = read_json(workspace.benchmark_candidates_path)
        self.assertEqual(benchmark_candidates["candidate_count"], 0)
        answer_history = read_json(workspace.answer_history_index_path)
        self.assertEqual(answer_history["record_count"], 1)
        record = answer_history["records"][0]
        self.assertEqual(record["question_class"], "answer")
        self.assertEqual(record["question_domain"], "external-factual")
        self.assertEqual(record["support_strategy"], "web-first")
        self.assertEqual(record["analysis_origin"], "agent-supplied")
        self.assertEqual(record["support_basis"], "external-source-verified")
        self.assertEqual(
            record["external_urls"],
            ["https://help.aliyun.com/zh/sms/getting-started/use-sms-api"],
        )
        self.assertNotIn("answer_text", record)
        summary = refresh_log_review_summary(workspace)
        self.assertEqual(summary["query_sessions"]["recent"][0]["question_class"], "answer")
        self.assertEqual(summary["query_sessions"]["recent"][0]["support_strategy"], "web-first")
        self.assertEqual(
            summary["retrieval_traces"]["recent"][0]["analysis_origin"],
            "agent-supplied",
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-support-2"}, clear=False):
            warm = prepare_ask_turn(
                workspace,
                question="Aliyun SMS 是否支持 HTTPS 接口？",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )
        self.assertTrue(warm["warm_start_evidence"]["matched_records"])
        self.assertIn(
            "https://help.aliyun.com/zh/sms/getting-started/use-sms-api",
            warm["warm_start_evidence"]["external_urls"],
        )
        self.assertEqual(
            warm["warm_start_evidence"]["matched_records"][0]["support_strategy"],
            "web-first",
        )

    def test_trace_reuse_paths_preserve_semantic_contract(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-trace-reuse"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the architecture strategy connect to?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The architecture strategy connects the operating model to implementation.",
            encoding="utf-8",
        )
        first_trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
        )
        reused_trace = trace_session(
            workspace,
            session_id=first_trace["session_id"],
            top=2,
        )
        self.assertEqual(first_trace["question_class"], "answer")
        self.assertEqual(first_trace["support_strategy"], "kb-first")
        self.assertEqual(first_trace["analysis_origin"], "agent-supplied")
        self.assertEqual(reused_trace["question_class"], "answer")
        self.assertEqual(reused_trace["support_strategy"], "kb-first")
        self.assertEqual(reused_trace["analysis_origin"], "agent-supplied")

    def test_composition_trace_records_phase_costs_and_reuses_unchanged_answer(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "thread-compose-phase"},
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question="Help me draft an executive summary for the current deck.",
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text("Draft summary one.\n", encoding="utf-8")
        first_trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        update_conversation_turn(
            workspace,
            conversation_id=str(turn["conversation_id"]),
            turn_id=str(turn["turn_id"]),
            updates={
                "session_ids": [str(first_trace["session_id"])],
                "trace_ids": [str(first_trace["trace_id"])],
            },
        )
        trace_files_before = sorted(workspace.retrieval_traces_dir.glob("*.json"))
        original_glob = Path.glob

        def guarded_glob(path: Path, pattern: str):  # type: ignore[override]
            if path == workspace.retrieval_traces_dir:
                raise AssertionError("composition trace reuse must not full-scan retrieval traces")
            return original_glob(path, pattern)

        with mock.patch("pathlib.Path.glob", side_effect=guarded_glob):
            reused_trace = trace_answer_file(
                workspace,
                answer_file=answer_path,
                top=2,
                log_context=turn["log_context"],
            )
        trace_files_after = sorted(workspace.retrieval_traces_dir.glob("*.json"))
        self.assertTrue(reused_trace["reused_trace"])
        self.assertEqual(first_trace["trace_id"], reused_trace["trace_id"])
        self.assertEqual(len(trace_files_before), len(trace_files_after))

        answer_path.write_text("Draft summary two.\n", encoding="utf-8")
        second_trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-composition",
            session_ids=[str(second_trace["session_id"])],
            trace_ids=[str(second_trace["trace_id"])],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Draft summary two.",
            evidence_mode=turn["evidence_mode"],
            research_depth=turn["research_depth"],
            bundle_paths=turn["bundle_paths"],
            status="answered",
        )

        run_state = load_run_state(workspace, str(turn["run_id"]))
        profile = run_state["execution_cost_profile"]
        self.assertGreaterEqual(profile["phase_counts"].get("draft", 0), 1)
        self.assertGreaterEqual(profile["phase_counts"].get("retrace", 0), 1)
        self.assertGreaterEqual(profile["phase_counts"].get("rewrite", 0), 1)

    def test_composition_trace_without_candidates_fresh_traces_without_full_scan(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "thread-compose-no-full-scan"},
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question="Help me draft an executive summary for the current deck.",
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text("Draft summary one.\n", encoding="utf-8")
        original_glob = Path.glob

        def guarded_glob(path: Path, pattern: str):  # type: ignore[override]
            if path == workspace.retrieval_traces_dir:
                raise AssertionError("composition fresh trace must not full-scan retrieval traces")
            return original_glob(path, pattern)

        with mock.patch("pathlib.Path.glob", side_effect=guarded_glob):
            first_trace = trace_answer_file(
                workspace,
                answer_file=answer_path,
                top=2,
                log_context=turn["log_context"],
            )

        self.assertFalse(bool(first_trace.get("reused_trace")))
        journal_entries = [
            json.loads(line)
            for line in run_journal_path(workspace, str(turn["run_id"])).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        phase_events = [
            entry["event_type"]
            for entry in journal_entries
            if entry.get("stage") == "execution"
        ]
        self.assertIn("phase-start", phase_events)
        self.assertIn("phase-finish", phase_events)
        run_state = load_run_state(workspace, str(turn["run_id"]))
        self.assertIn("execution_cost_profile", run_state)
        self.assertGreaterEqual(
            run_state["execution_cost_profile"]["phase_counts"].get("draft", 0),
            1,
        )

    def test_shared_job_settlement_is_idempotent_and_run_journal_once_only(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "thread-shared-job"},
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question="Does Aliyun SMS support HTTPS API?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                ),
            )

        job = ensure_shared_job(
            workspace,
            job_key="sync:test:idempotent",
            job_family="sync",
            criticality="answer-critical",
            scope={"workspace_root": str(workspace.root)},
            input_signature="sync:test:idempotent",
            owner={"kind": "command", "id": "sync-command"},
            run_id=str(turn["run_id"]),
        )["manifest"]
        settled = complete_control_plane_job(
            workspace,
            str(job["job_id"]),
            result={"status": "valid", "detail": "Settled once."},
        )
        settled_again = complete_control_plane_job(
            workspace,
            str(job["job_id"]),
            result={"status": "valid", "detail": "Settled once."},
        )
        self.assertEqual(settled["updated_at"], settled_again["updated_at"])
        with self.assertRaises(ValueError):
            complete_control_plane_job(
                workspace,
                str(job["job_id"]),
                result={"status": "valid", "detail": "Mutated later."},
            )
        record_shared_job_settled_once(
            workspace,
            run_ids=[str(turn["run_id"])],
            job_id=str(job["job_id"]),
            status="completed",
        )
        record_shared_job_settled_once(
            workspace,
            run_ids=[str(turn["run_id"])],
            job_id=str(job["job_id"]),
            status="completed",
        )
        journal_entries = [
            json.loads(line)
            for line in run_journal_path(workspace, str(turn["run_id"]))
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        settled_events = [
            entry
            for entry in journal_entries
            if entry.get("event_type") == "shared-job-settled"
            and isinstance(entry.get("payload"), dict)
            and entry["payload"].get("job_id") == str(job["job_id"])
        ]
        self.assertEqual(len(settled_events), 1)
        job_journal_entries = [
            json.loads(line)
            for line in (
                workspace.shared_jobs_dir / str(job["job_id"]) / "journal.jsonl"
            )
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        self.assertEqual(
            len(
                [
                    entry
                    for entry in job_journal_entries
                    if entry.get("event_type") == "job-settled"
                ]
            ),
            1,
        )

    def test_agent_semantic_analysis_routes_non_english_questions(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-jp"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="アリババクラウドの SMS API は HTTPS に対応していますか？",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="external-factual",
                    route_reason="Agent semantic analysis handled the Japanese question directly.",
                ),
            )

        self.assertEqual(turn["question_domain"], "external-factual")
        self.assertEqual(turn["inner_workflow_id"], "grounded-answer")
        self.assertEqual(turn["analysis_origin"], "agent-supplied")

    def test_prepare_ask_turn_blocks_compatible_host_python_snippet_entry(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        script = "\n".join(
            [
                "import json",
                "from pathlib import Path",
                "from docmason.ask import prepare_ask_turn",
                "from docmason.project import WorkspacePaths",
                f"workspace = WorkspacePaths(root=Path({str(workspace.root)!r}))",
                "payload = prepare_ask_turn(",
                "    workspace,",
                f"    question={'What does Campaign Planning Brief say about architecture?'!r},",
                f"    semantic_analysis={self.semantic_analysis(question_class='answer', question_domain='workspace-corpus')!r},",
                ")",
                "print(json.dumps(payload))",
            ]
        )
        completed = self.run_host_snippet(
            workspace,
            script=script,
            env_overrides={"CODEX_THREAD_ID": "thread-snippet-prepare"},
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout.strip())
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(
            payload["primary_issue_code"],
            "noncanonical-host-lifecycle-helper-direct",
        )
        self.assertIn("hidden canonical ask integration path", payload["recommended_action"])
        self.assertEqual(list(workspace.conversations_dir.glob("*.json")), [])

    def test_complete_ask_turn_blocks_compatible_host_python_snippet_entry(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        turn = prepare_ask_turn(
            workspace,
            question="What does Campaign Planning Brief say about architecture?",
            semantic_analysis=self.semantic_analysis(
                question_class="answer",
                question_domain="workspace-corpus",
            ),
        )

        script = "\n".join(
            [
                "from pathlib import Path",
                "from docmason.ask import complete_ask_turn",
                "from docmason.project import WorkspacePaths",
                f"workspace = WorkspacePaths(root=Path({str(workspace.root)!r}))",
                "complete_ask_turn(",
                "    workspace,",
                f"    conversation_id={str(turn['conversation_id'])!r},",
                f"    turn_id={str(turn['turn_id'])!r},",
                "    inner_workflow_id='grounded-answer',",
                ")",
            ]
        )
        completed = self.run_host_snippet(
            workspace,
            script=script,
            env_overrides={"CLAUDE_SESSION_ID": "thread-snippet-complete"},
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            "noncanonical-host-lifecycle-helper-direct",
            completed.stderr,
        )
        self.assertIn("hidden ask integration path", completed.stderr)
        blocked_turn = load_turn_record(
            workspace,
            conversation_id=str(turn["conversation_id"]),
            turn_id=str(turn["turn_id"]),
        )
        self.assertIsNone(blocked_turn["committed_run_id"])
        self.assertEqual(blocked_turn["status"], "prepared")

    def test_hidden_ask_open_commits_missing_source_boundary(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-hidden-boundary"}, clear=False):
            payload = handle_hidden_ask_request(
                {
                    "action": "open",
                    "question": (
                        "Using only the document 'Missing Campaign Brief', summarize the "
                        "architecture strategy in 3 bullet points. Do not use any other source."
                    ),
                    "host_provider": "codex",
                    "host_thread_ref": "thread-hidden-boundary",
                    "host_identity_source": "codex_thread_id",
                },
                paths=workspace,
            )

        self.assertEqual(payload["status"], "boundary")
        self.assertTrue(payload["user_reply_allowed"])
        self.assertIn("stopping at", payload["answer_text"])
        turn = load_turn_record(
            workspace,
            conversation_id=str(payload["conversation_id"]),
            turn_id=str(payload["turn_id"]),
        )
        self.assertEqual(turn["support_basis"], "governed-boundary")
        self.assertEqual(turn["answer_state"], "abstained")
        self.assertEqual(turn["host_provider"], "codex")
        self.assertEqual(turn["host_thread_ref"], "thread-hidden-boundary")

    def test_hidden_ask_finalize_quarantines_noncanonical_answer_file(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-hidden-fail"}, clear=False):
            opened = handle_hidden_ask_request(
                {
                    "action": "open",
                    "question": (
                        'Using only the document "Campaign Planning Brief", summarize the '
                        "architecture strategy in 3 bullet points."
                    ),
                    "host_provider": "codex",
                    "host_thread_ref": "thread-hidden-fail",
                    "host_identity_source": "codex_thread_id",
                },
                paths=workspace,
            )

        self.assertEqual(opened["status"], "execute")
        self.assertFalse(opened["user_reply_allowed"])
        answer_path = workspace.root / str(opened["answer_file_path"])
        answer_path.write_text(
            "The architecture strategy defines the operating model.\n",
            encoding="utf-8",
        )

        failed = handle_hidden_ask_request(
            {
                "action": "finalize",
                "conversation_id": opened["conversation_id"],
                "turn_id": opened["turn_id"],
                "answer_file_path": opened["answer_file_path"],
            },
            paths=workspace,
        )

        self.assertEqual(failed["status"], "blocked")
        self.assertFalse(failed["user_reply_allowed"])
        quarantined_path = workspace.root / str(failed["noncanonical_answer_file_path"])
        self.assertTrue(quarantined_path.exists())
        self.assertFalse(answer_path.exists())
        turn = load_turn_record(
            workspace,
            conversation_id=str(opened["conversation_id"]),
            turn_id=str(opened["turn_id"]),
        )
        self.assertEqual(
            turn["noncanonical_answer_file_path"],
            failed["noncanonical_answer_file_path"],
        )

    def test_hidden_ask_finalize_rejects_second_finalize_after_commit(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-hidden-second"}, clear=False):
            opened = handle_hidden_ask_request(
                {
                    "action": "open",
                    "question": (
                        "Using only the document 'Missing Campaign Brief', summarize the "
                        "architecture strategy in 3 bullet points. Do not use any other source."
                    ),
                },
                paths=workspace,
            )

        self.assertEqual(opened["status"], "boundary")
        failed = handle_hidden_ask_request(
            {
                "action": "finalize",
                "conversation_id": opened["conversation_id"],
                "turn_id": opened["turn_id"],
            },
            paths=workspace,
        )
        self.assertEqual(failed["status"], "blocked")
        self.assertEqual(
            failed["primary_issue_code"],
            "already-committed-canonical-turn",
        )
        self.assertFalse(failed["user_reply_allowed"])

    def test_hidden_ask_open_does_not_run_release_entry_check(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_release_bundle(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch(
            "docmason.host_integration.maybe_run_release_entry_check"
        ) as release_entry_check:
            opened = handle_hidden_ask_request(
                {
                    "action": "open",
                    "question": (
                        'Using only the document "Campaign Planning Brief", summarize the '
                        "architecture strategy in 3 bullet points."
                    ),
                    "host_provider": "codex",
                    "host_thread_ref": "thread-hidden-release-open",
                    "host_identity_source": "codex_thread_id",
                },
                paths=workspace,
            )

        self.assertEqual(opened["status"], "execute")
        release_entry_check.assert_not_called()

    def test_hidden_ask_finalize_appends_release_entry_notice_without_mutating_answer_file(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_release_bundle(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        opened = handle_hidden_ask_request(
            {
                "action": "open",
                "question": (
                    'Using only the document "Campaign Planning Brief", summarize the '
                    "architecture strategy in 3 bullet points."
                ),
                "host_provider": "codex",
                "host_thread_ref": "thread-hidden-release-finalize",
                "host_identity_source": "codex_thread_id",
            },
            paths=workspace,
        )

        self.assertEqual(opened["status"], "execute")
        answer_path = workspace.root / str(opened["answer_file_path"])
        answer_path.write_text(
            "The architecture strategy defines the operating model.\n",
            encoding="utf-8",
        )

        def fake_complete(*args: object, **kwargs: object) -> dict[str, object]:
            del args
            update_conversation_turn(
                workspace,
                conversation_id=str(kwargs["conversation_id"]),
                turn_id=str(kwargs["turn_id"]),
                updates={
                    "committed_run_id": str(opened["run_id"]),
                    "answer_file_path": str(opened["answer_file_path"]),
                    "answer_state": "grounded",
                    "support_basis": "kb-grounded",
                    "response_excerpt": "The architecture strategy defines the operating model.",
                    "session_ids": [],
                    "trace_ids": [],
                },
            )
            return {"committed_run_id": str(opened["run_id"])}

        with mock.patch(
            "docmason.host_integration.complete_ask_turn",
            side_effect=fake_complete,
        ) as complete_turn:
            with mock.patch(
                "docmason.host_integration.maybe_run_release_entry_check",
                return_value={
                    "notice": "DocMason update available: v0.2.0.",
                    "release_entry_status": {
                        "bundle_detected": True,
                        "effective_enabled": True,
                        "distribution_channel": "clean",
                    },
                },
            ) as release_entry_check:
                completed = handle_hidden_ask_request(
                    {
                        "action": "finalize",
                        "conversation_id": opened["conversation_id"],
                        "turn_id": opened["turn_id"],
                        "answer_file_path": opened["answer_file_path"],
                        "response_excerpt": "The architecture strategy defines the operating model.",
                    },
                    paths=workspace,
                )

        complete_turn.assert_called_once()
        release_entry_check.assert_called_once()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            answer_path.read_text(encoding="utf-8"),
            "The architecture strategy defines the operating model.\n",
        )
        self.assertIn("DocMason update available", completed["answer_text"])
        self.assertEqual(completed["release_entry_notice"], "DocMason update available: v0.2.0.")
        self.assertEqual(completed["release_entry_status"]["distribution_channel"], "clean")

    def test_recommended_hybrid_targets_do_not_fall_back_to_unmatched_units(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        hybrid_work_path = workspace.hybrid_work_path("current")
        hybrid_work_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(
            hybrid_work_path,
            {
                "generated_at": "2026-03-30T00:00:00Z",
                "target": "current",
                "sources": [
                    {
                        "source_id": "source-page-scope",
                        "units": [
                            {
                                "unit_id": "page-020",
                                "coverage_status": "candidate-prepared",
                                "target_artifact_ids": ["artifact-020"],
                                "required_overlay_slots": ["ocr"],
                                "target_focus_render_assets": [],
                                "target_render_assets": [],
                            },
                            {
                                "unit_id": "page-039",
                                "coverage_status": "candidate-prepared",
                                "target_artifact_ids": ["artifact-039"],
                                "required_overlay_slots": ["ocr"],
                                "target_focus_render_assets": [],
                                "target_render_assets": [],
                            },
                        ],
                    }
                ],
            },
        )

        targets = _recommended_hybrid_targets(
            workspace,
            target="current",
            results=[
                {
                    "source_id": "source-page-scope",
                    "matched_units": [{"unit_id": "page-039"}],
                    "matched_artifacts": [],
                }
            ],
            reference_resolution={
                "resolved_source_id": "source-page-scope",
                "resolved_unit_id": "page-039",
                "unit_match_status": "exact",
            },
            source_scope_policy={
                "scope_mode": "source-scoped-soft",
                "target_source_id": "source-page-scope",
            },
        )

        self.assertEqual([item["unit_id"] for item in targets], ["page-039"])

    def test_retrieve_corpus_compare_scopes_to_declared_sources_only(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.create_pdf(workspace.source_dir / "c.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = [item["source_id"] for item in pending.payload["pending_sources"]]
        self.assertEqual(len(source_ids), 3)
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[0],
            title="Campaign Planning Brief",
            summary="A strategy deck about architecture and operating model.",
            key_point="The strategy defines an architecture operating model.",
            claim="The architecture deck connects strategy to implementation.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[1],
            title="Campaign Evaluation Plan",
            summary="A delivery timeline and companion planning document.",
            key_point="The timeline explains rollout milestones.",
            claim="The timeline complements the architecture strategy.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[2],
            title="Regional Budget Memo",
            summary="A finance memo about regional cost controls.",
            key_point="The memo focuses on budget controls.",
            claim="The memo does not discuss architecture strategy.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        retrieval = retrieve_corpus(
            workspace,
            query=(
                'Compare "Campaign Planning Brief" versus '
                '"Campaign Evaluation Plan" on architecture strategy.'
            ),
            top=5,
            graph_hops=0,
            document_types=None,
            source_ids=None,
            include_renders=True,
            question_domain="workspace-corpus",
        )

        self.assertEqual(retrieval["source_scope_policy"]["scope_mode"], "compare")
        self.assertEqual(retrieval["reference_resolution"]["compare_resolution_status"], "exact")
        self.assertCountEqual(
            retrieval["source_scope_policy"]["compare_target_source_ids"],
            source_ids[:2],
        )
        self.assertTrue(retrieval["results"])
        self.assertTrue(
            all(result["source_id"] in set(source_ids[:2]) for result in retrieval["results"])
        )

    def test_trace_answer_file_marks_unresolved_compare_scope_without_fake_exactness(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        with mock.patch.dict(
            os.environ,
            {"CODEX_THREAD_ID": "thread-compare-unresolved"},
            clear=False,
        ):
            turn = prepare_ask_turn(
                workspace,
                question='Compare "Campaign Planning Brief" versus "Zebra Ledger".',
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            (
                "Campaign Planning Brief contains architecture guidance, but the second "
                "requested comparison source could not be verified in the published corpus."
            ),
            encoding="utf-8",
        )

        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=3,
            log_context=turn["log_context"],
        )

        self.assertEqual(trace["source_scope_policy"]["scope_mode"], "compare")
        self.assertEqual(trace["reference_resolution"]["compare_resolution_status"], "unresolved")
        self.assertFalse(trace["canonical_support_summary"]["source_scope_satisfied"])
        self.assertEqual(trace["answer_state"], "unresolved")
        self.assertIn("compare-source-unresolved", trace["issue_codes"])

    def test_hidden_ask_finalize_returns_waiting_state_without_release_entry(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_release_bundle(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        opened = handle_hidden_ask_request(
            {
                "action": "open",
                "question": "What does the campaign planning brief say?",
                "host_provider": "codex",
                "host_thread_ref": "thread-hidden-waiting",
                "host_identity_source": "codex_thread_id",
            },
            paths=workspace,
        )

        def fake_complete(*args: object, **kwargs: object) -> dict[str, object]:
            del args
            update_conversation_turn(
                workspace,
                conversation_id=str(kwargs["conversation_id"]),
                turn_id=str(kwargs["turn_id"]),
                updates={
                    "status": "waiting-shared-job",
                    "turn_state": "waiting-shared-job",
                    "freshness_notice": "The ask is waiting on a governed multimodal refresh.",
                    "hybrid_refresh_triggered": True,
                    "hybrid_refresh_job_ids": ["job-hidden-wait"],
                    "attached_shared_job_ids": ["job-hidden-wait"],
                    "hybrid_refresh_summary": {"mode": "ask-hybrid"},
                },
            )
            return {"status": "waiting-shared-job"}

        with mock.patch(
            "docmason.host_integration.complete_ask_turn",
            side_effect=fake_complete,
        ) as complete_turn:
            with mock.patch(
                "docmason.host_integration.maybe_run_release_entry_check"
            ) as release_entry_check:
                waiting = handle_hidden_ask_request(
                    {
                        "action": "finalize",
                        "conversation_id": opened["conversation_id"],
                        "turn_id": opened["turn_id"],
                        "answer_file_path": opened["answer_file_path"],
                        "response_excerpt": "The ask is waiting on a governed multimodal refresh.",
                    },
                    paths=workspace,
                )

        complete_turn.assert_called_once()
        release_entry_check.assert_not_called()
        self.assertEqual(waiting["status"], "waiting-shared-job")
        self.assertFalse(waiting["user_reply_allowed"])
        self.assertIsNone(waiting["answer_text"])
        self.assertEqual(waiting["next_step"], "wait-for-shared-job")
        self.assertEqual(waiting["canonical_turn_state"], "waiting-shared-job")
        self.assertEqual(waiting["log_context"]["conversation_id"], opened["conversation_id"])

    def test_hidden_ask_progress_settles_waiting_turn_and_restores_execute_state(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        opened = handle_hidden_ask_request(
            {
                "action": "open",
                "question": "What does the campaign planning brief say?",
                "host_provider": "codex",
                "host_thread_ref": "thread-hidden-progress",
                "host_identity_source": "codex_thread_id",
            },
            paths=workspace,
        )

        begin_lane_c_shared_refresh(
            workspace,
            conversation_id=str(opened["conversation_id"]),
            turn_id=str(opened["turn_id"]),
            run_id=str(opened["run_id"]),
            query="What does the campaign planning brief say?",
            recommended_targets=[
                {
                    "source_id": source_ids[0],
                    "required_overlay_slots": ["diagram-summary"],
                    "target_artifact_ids": [],
                }
            ],
        )

        progressed = handle_hidden_ask_request(
            {
                "action": "progress",
                "conversation_id": opened["conversation_id"],
                "turn_id": opened["turn_id"],
                "completion_status": "covered",
                "hybrid_refresh_summary": {"covered_source_count": 1},
            },
            paths=workspace,
        )

        self.assertEqual(progressed["status"], "execute")
        self.assertFalse(progressed["user_reply_allowed"])
        self.assertEqual(progressed["next_step"], "continue-inner-workflow")
        self.assertEqual(progressed["canonical_turn_state"], "prepared")
        self.assertEqual(progressed["log_context"]["turn_id"], opened["turn_id"])
        live_turn = load_turn_record(
            workspace,
            conversation_id=str(opened["conversation_id"]),
            turn_id=str(opened["turn_id"]),
        )
        self.assertEqual(live_turn["status"], "prepared")
        self.assertEqual(live_turn["hybrid_refresh_completion_status"], "covered")


if __name__ == "__main__":
    unittest.main()

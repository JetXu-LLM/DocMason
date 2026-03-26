"""Ask-path hardening, workflow runner, and front-controller tests."""

from __future__ import annotations

import json
import os
import shutil
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
from docmason.conversation import load_turn_record, update_conversation_turn
from docmason.control_plane import ensure_shared_job, load_shared_job
from docmason.control_plane import complete_shared_job as complete_control_plane_job
from docmason.control_plane import lane_c_job_key
from docmason.front_controller import write_hybrid_refresh_work
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import retrieve_corpus, trace_answer_file, trace_session
from docmason.run_control import load_run_state, run_journal_path, update_run_state
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
        complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-composition",
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Draft summary.",
            source_escalation_used=False,
            evidence_mode=turn["evidence_mode"],
            research_depth=turn["research_depth"],
            bundle_paths=turn["bundle_paths"],
            status="answered",
        )

        summary = read_json(workspace.review_summary_path)
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
        self.assertIn("projection-refreshed", journal_events)

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
        live_turn = read_json(workspace.conversations_dir / "thread-hybrid-state.json")["turns"][0]
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

        completed = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
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
        self.assertEqual(completed["session_ids"], [trace["session_id"]])

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
        live_turn = read_json(workspace.conversations_dir / "thread-version-trace.json")["turns"][0]
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
        owner_turn = read_json(workspace.conversations_dir / "thread-lane-c-1.json")["turns"][0]
        self.assertEqual(owner_turn["status"], "waiting-shared-job")
        waiting_turn = read_json(workspace.conversations_dir / "thread-lane-c-2.json")["turns"][0]
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
        second_live_turn = read_json(workspace.conversations_dir / "thread-lane-c-2.json")["turns"][0]
        self.assertEqual(second_live_turn["status"], "prepared")
        self.assertEqual(
            second_live_turn["freshness_notice"],
            "Lane C settled. Reretrieve and retrace before committing the answer.",
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
            response_excerpt="The scanned workflow page needs governed Lane C before a final answer.",
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
            read_json(workspace.conversations_dir / "thread-lane-c-mainline.json")["turns"][0]["status"],
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
        conversation = read_json(workspace.conversations_dir / "thread-reuse.json")
        self.assertEqual(len(conversation["turns"]), 1)

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
        conversation = read_json(workspace.conversations_dir / "thread-sync-confirm.json")
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
        self.assertIn("projection-refreshed", journal_events)

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
            with mock.patch(
                "docmason.ask.prepare_workspace",
                return_value=CommandReport(
                    0,
                    {"status": READY, "prepare_status": READY, "control_plane": {}},
                    [],
                ),
            ):
                resumed = prepare_ask_turn(workspace, question="yes")

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
        summary = read_json(workspace.review_summary_path)
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


if __name__ == "__main__":
    unittest.main()

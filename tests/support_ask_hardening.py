"""Ask-path hardening, workflow runner, and front-controller tests."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import complete_ask_turn, prepare_ask_turn
from docmason.commands import (
    review_runtime_logs,
    run_workflow,
    sync_workspace,
    trace_knowledge,
)
from docmason.front_controller import write_hybrid_refresh_work
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import retrieve_corpus, trace_answer_file, trace_session

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
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "prepared_at": "2026-03-17T00:00:00Z",
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
            },
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

        self.assertIn(report.payload["status"], {"ready", "degraded"})
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

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-hybrid-state"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the image-heavy page mean?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="general-stable",
                ),
            )
        (workspace.root / turn["answer_file_path"]).write_text(
            "The image-heavy page needs multimodal evidence refresh before interpretation.",
            encoding="utf-8",
        )

        completed = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            response_excerpt="Hybrid refresh completed for the selected source.",
            status="answered",
            hybrid_refresh_triggered=True,
            hybrid_refresh_sources=["source-001"],
            hybrid_refresh_completion_status="covered",
            hybrid_refresh_summary={
                "mode": "ask-hybrid",
                "covered_source_count": 1,
            },
        )
        self.assertTrue(completed["hybrid_refresh_triggered"])
        self.assertEqual(completed["hybrid_refresh_sources"], ["source-001"])
        self.assertEqual(completed["hybrid_refresh_completion_status"], "covered")
        self.assertEqual(
            completed["hybrid_refresh_summary"]["covered_source_count"],
            1,
        )

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

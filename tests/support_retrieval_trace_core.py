"""Retrieval, trace, and incremental corpus maintenance tests."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.commands import (
    DEGRADED,
    READY,
    CommandReport,
    retrieve_knowledge,
    run_workflow,
    sync_workspace,
    trace_knowledge,
)
from docmason.knowledge import update_source_index
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.semantic_overlays import write_semantic_overlay
from tests.support_ready_workspace import seed_self_contained_bootstrap_state

ROOT = Path(__file__).resolve().parents[1]


class RetrievalTraceCoreTests(unittest.TestCase):
    """Cover incremental maintenance, retrieval, and trace behavior."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        shutil.copytree(ROOT / "skills" / "canonical", root / "skills" / "canonical")
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "runtime").mkdir()
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
            prepared_at="2026-03-16T00:00:00Z",
        )

    def seed_active_thread_turn(
        self,
        workspace: WorkspacePaths,
        *,
        front_door_state: str,
        thread_id: str = "thread-operator",
    ) -> None:
        workspace.conversations_dir.mkdir(parents=True, exist_ok=True)
        answer_path = workspace.answers_dir / thread_id / "turn-001.md"
        answer_path.parent.mkdir(parents=True, exist_ok=True)
        answer_path.write_text("Operator answer placeholder.\n", encoding="utf-8")
        write_json(
            workspace.conversations_dir / f"{thread_id}.json",
            {
                "conversation_id": thread_id,
                "conversation_id_source": "codex_thread_id",
                "agent_surface": "codex",
                "opened_at": "2026-03-26T00:00:00Z",
                "updated_at": "2026-03-26T00:00:00Z",
                "workspace_snapshot": {},
                "turns": [
                    {
                        "turn_id": "turn-001",
                        "native_turn_id": "native-turn-001",
                        "active_run_id": "run-001",
                        "committed_run_id": None,
                        "turn_state": "prepared",
                        "status": "prepared",
                        "user_question": "Need evidence for the current workspace question.",
                        "entry_workflow_id": "ask",
                        "inner_workflow_id": "grounded-answer",
                        "question_class": "answer",
                        "question_domain": "workspace-corpus",
                        "support_strategy": "kb-first",
                        "analysis_origin": "agent-supplied",
                        "front_door_state": front_door_state,
                        "front_door_opened_at": (
                            "2026-03-26T00:00:00Z" if front_door_state == "canonical-ask" else None
                        ),
                        "front_door_run_id": (
                            "run-001" if front_door_state == "canonical-ask" else None
                        ),
                        "answer_file_path": str(answer_path.relative_to(workspace.root)),
                    }
                ],
            },
        )

    def create_pdf(self, path: Path, *, page_count: int = 1) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for index in range(page_count):
            writer.add_blank_page(width=144 + index, height=144 + index)
        with path.open("wb") as handle:
            writer.write(handle)

    def create_pdf_with_chart(self, path: Path) -> None:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - compatibility import
            import fitz as pymupdf  # type: ignore[import-not-found]

        document = pymupdf.open()
        page = document.new_page(width=420, height=320)
        page.insert_text((48, 36), "Quarterly Revenue Chart")
        page.insert_text((48, 58), "Q1 Q2 Q3 Q4 Actual Budget")
        page.draw_line((48, 260), (360, 260), color=(0, 0, 0), width=1.2)
        page.draw_line((48, 84), (48, 260), color=(0, 0, 0), width=1.2)
        for index, height in enumerate((44, 86, 62, 118), start=0):
            left = 84 + (index * 56)
            page.draw_rect(
                (left, 260 - height, left + 28, 260),
                color=(0.1, 0.2, 0.8),
                fill=(0.1, 0.2, 0.8),
            )
            page.insert_text((left - 2, 278), f"Q{index + 1}")
        page.insert_text((300, 92), "Revenue")
        document.save(path)
        document.close()

    def create_pdf_with_sections_and_tables(self, path: Path) -> None:
        try:
            import pymupdf  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - compatibility import
            import fitz as pymupdf  # type: ignore[import-not-found]

        document = pymupdf.open()
        for page_index in range(2):
            page = document.new_page(width=520, height=720)
            page.insert_text((48, 48), "1. Executive Summary", fontsize=18)
            page.insert_text(
                (48, 78),
                "The document summarises KPI trends and supporting evidence for review.",
                fontsize=11,
            )
            page.insert_text((48, 118), "Table 1. KPI Summary", fontsize=12)
            top = 150
            left = 48
            width = 320
            row_height = 30
            col_width = 100
            for row in range(4):
                y = top + (row * row_height)
                page.draw_line((left, y), (left + width, y), color=(0, 0, 0), width=1)
            for col in range(4):
                x = left + (col * col_width)
                page.draw_line((x, top), (x, top + (3 * row_height)), color=(0, 0, 0), width=1)
            headers = ["Quarter", "Actual", "Budget"]
            values = [
                ["Q1", "10", "12"],
                [
                    "Q2" if page_index == 0 else "Q3",
                    "15" if page_index == 0 else "18",
                    "14" if page_index == 0 else "17",
                ],
            ]
            for col, header in enumerate(headers):
                page.insert_text((left + 8 + (col * col_width), top + 18), header, fontsize=10)
            for row, row_values in enumerate(values, start=1):
                for col, value in enumerate(row_values):
                    page.insert_text(
                        (left + 8 + (col * col_width), top + 18 + (row * row_height)),
                        value,
                        fontsize=10,
                    )
        document.save(path)
        document.close()

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
                "notes_en": "Retrieval trace test fixture.",
                "notes_source": "Retrieval trace test fixture.",
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
        self.assertTrue(workspace.current_publish_manifest_path.exists())
        return source_ids

    def test_same_content_move_preserves_source_id_and_updates_path_trust(self) -> None:
        from docmason.commands import sync_workspace

        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        source_path = workspace.source_dir / "example.pdf"
        self.create_pdf(source_path)
        self.create_pdf(workspace.source_dir / "companion.pdf")
        self.publish_seeded_corpus(workspace)
        original_catalog = read_json(workspace.current_catalog_path)
        original_source_id = next(
            item["source_id"]
            for item in original_catalog["sources"]
            if item["current_path"] == "original_doc/example.pdf"
        )

        moved_dir = workspace.source_dir / "moved"
        moved_dir.mkdir()
        moved_path = moved_dir / "renamed-example.pdf"
        source_path.rename(moved_path)

        result = sync_workspace(workspace, autonomous=False)
        self.assertEqual(result.payload["sync_status"], "valid")
        self.assertEqual(result.payload["change_set"]["stats"]["moved_or_renamed"], 1)
        current_catalog = read_json(workspace.current_catalog_path)
        moved_entry = next(
            item for item in current_catalog["sources"] if item["source_id"] == original_source_id
        )
        self.assertEqual(moved_entry["current_path"], "original_doc/moved/renamed-example.pdf")
        moved_manifest = read_json(
            workspace.knowledge_base_current_dir
            / "sources"
            / original_source_id
            / "source_manifest.json"
        )
        self.assertEqual(moved_manifest["identity_basis"], "fingerprint")
        self.assertEqual(moved_manifest["relative_path_lineage"], ["moved"])
        self.assertEqual(moved_manifest["trust_prior"]["first_level_subtree"], "moved")

    def test_reused_unchanged_source_refreshes_phase_four_manifest_fields(self) -> None:
        from docmason.commands import sync_workspace

        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        if workspace.knowledge_base_staging_dir.exists():
            import shutil

            shutil.rmtree(workspace.knowledge_base_staging_dir)

        result = sync_workspace(workspace, autonomous=False)
        self.assertEqual(result.payload["sync_status"], "valid")
        self.assertEqual(result.payload["build_stats"]["reused_sources"], 2)

        source_manifest = read_json(
            workspace.knowledge_base_current_dir
            / "sources"
            / source_ids[0]
            / "source_manifest.json"
        )
        self.assertEqual(source_manifest["change_classification"], "unchanged")
        self.assertEqual(source_manifest["identity_basis"], "path")
        self.assertIn("path_history", source_manifest)
        self.assertTrue(source_manifest["staging_generated_at"])

    def test_no_rebuild_sync_refreshes_legacy_staging_source_manifest_fields(self) -> None:
        from docmason.commands import sync_workspace

        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        legacy_manifest_path = (
            workspace.knowledge_base_staging_dir
            / "sources"
            / source_ids[0]
            / "source_manifest.json"
        )
        legacy_manifest = read_json(legacy_manifest_path)
        legacy_manifest.pop("change_classification", None)
        legacy_manifest.pop("identity_basis", None)
        legacy_manifest.pop("path_history", None)
        write_json(legacy_manifest_path, legacy_manifest)

        result = sync_workspace(workspace, autonomous=False)
        self.assertEqual(result.payload["sync_status"], "valid")
        self.assertFalse(result.payload["rebuilt"])

        refreshed_manifest = read_json(
            workspace.knowledge_base_current_dir
            / "sources"
            / source_ids[0]
            / "source_manifest.json"
        )
        self.assertEqual(refreshed_manifest["change_classification"], "unchanged")
        self.assertEqual(refreshed_manifest["identity_basis"], "path")
        self.assertIn("path_history", refreshed_manifest)
        coverage_manifest = read_json(workspace.current_coverage_manifest_path)
        self.assertEqual(coverage_manifest["sources"][0]["change_classification"], "unchanged")
        catalog = read_json(workspace.current_catalog_path)
        self.assertEqual(catalog["sources"][0]["change_classification"], "unchanged")

    def test_modified_source_preserves_semantic_files_but_marks_them_stale(self) -> None:
        from docmason.commands import sync_workspace

        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        source_path = workspace.source_dir / "example.pdf"
        self.create_pdf(source_path)
        self.create_pdf(workspace.source_dir / "companion.pdf")
        self.publish_seeded_corpus(workspace)
        original_catalog = read_json(workspace.current_catalog_path)
        target_source_id = next(
            item["source_id"]
            for item in original_catalog["sources"]
            if item["current_path"] == "original_doc/example.pdf"
        )

        self.create_pdf(source_path, page_count=2)
        result = sync_workspace(workspace, autonomous=False)

        self.assertEqual(result.payload["sync_status"], "pending-synthesis")
        self.assertEqual(result.payload["change_set"]["stats"]["modified"], 1)
        pending_source = next(
            item
            for item in result.payload["pending_sources"]
            if item["source_id"] == target_source_id
        )
        self.assertEqual(pending_source["reason"], "missing")
        source_dir = workspace.knowledge_base_staging_dir / "sources" / target_source_id
        self.assertFalse((source_dir / "knowledge.json").exists())
        self.assertFalse((source_dir / "summary.md").exists())

    def test_deleted_source_is_archived_and_removed_from_published_catalog(self) -> None:
        from docmason.commands import sync_workspace

        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        first = workspace.source_dir / "a.pdf"
        second = workspace.source_dir / "b.pdf"
        self.create_pdf(first)
        self.create_pdf(second)
        source_ids = self.publish_seeded_corpus(workspace)
        catalog = read_json(workspace.current_catalog_path)
        source_by_id = {item["source_id"]: item for item in catalog["sources"]}

        deleted_path = workspace.root / source_by_id[source_ids[0]]["current_path"]
        deleted_path.unlink()
        preview = sync_workspace(workspace)
        self.assertEqual(preview.payload["sync_status"], "awaiting-confirmation")
        result = sync_workspace(workspace, assume_yes=True)

        self.assertEqual(result.payload["sync_status"], "valid")
        self.assertEqual(result.payload["change_set"]["stats"]["deleted"], 1)
        catalog = read_json(workspace.current_catalog_path)
        self.assertEqual(catalog["source_count"], 1)
        index = read_json(workspace.source_index_path)
        archived = next(item for item in index["sources"] if item["source_id"] == source_ids[0])
        self.assertFalse(archived["active"])
        self.assertEqual(archived["change_classification"], "deleted")
        self.assertTrue(archived["deleted_at"])

    def test_retrieve_returns_ranked_bundles_graph_expansions_and_logs(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        report = retrieve_knowledge(
            query="architecture strategy",
            top=2,
            graph_hops=1,
            include_renders=True,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertEqual(report.payload["results"][0]["source_id"], source_ids[0])
        self.assertGreaterEqual(len(report.payload["results"]), 2)
        second = report.payload["results"][1]
        self.assertGreater(second["score"]["graph_bonus"], 0)
        session_path = workspace.query_sessions_dir / f"{report.payload['session_id']}.json"
        self.assertTrue(session_path.exists())
        usage_history = workspace.usage_history_path.read_text(encoding="utf-8")
        self.assertIn(report.payload["session_id"], usage_history)

    def test_pdf_artifact_query_surfaces_matched_artifacts_and_trace_supports(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_chart(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/a.pdf"],
            title="Operational Overview",
            summary="A conservative seeded summary without explicit chart vocabulary.",
            key_point="The document preserves supporting business evidence.",
            claim="The document can support follow-up analysis.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/b.pdf"],
            title="Plain Companion",
            summary="A plain companion source.",
            key_point="This source is intentionally generic.",
            claim="This source should rank below the chart-bearing source for chart hints.",
        )

        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        report = retrieve_knowledge(
            query="quarterly revenue chart q4",
            top=2,
            graph_hops=0,
            include_renders=True,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertTrue(report.payload["results"][0]["matched_artifacts"])
        self.assertTrue(report.payload["results"][0]["matched_artifact_ids"])
        self.assertTrue(
            any(
                item["artifact_type"] in {"chart", "major-region"}
                for item in report.payload["results"][0]["matched_artifacts"]
            )
        )

        answer_file = workspace.root / "answer.txt"
        answer_file.write_text(
            "The quarterly revenue chart highlights Q4 as the strongest quarter.",
            encoding="utf-8",
        )
        trace = trace_knowledge(answer_file=str(answer_file), top=2, paths=workspace)
        self.assertTrue(trace.payload["supporting_artifact_ids"])
        self.assertTrue(trace.payload["segments"][0]["supporting_artifact_ids"])
        self.assertTrue(trace.payload["segments"][0]["artifact_supports"])
        self.assertEqual(
            trace.payload["segments"][0]["artifact_supports"][0]["artifact_id"],
            trace.payload["segments"][0]["supporting_artifact_ids"][0].split(":", 1)[1],
        )

    def test_pdf_document_context_query_surfaces_section_and_caption_matches(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_sections_and_tables(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/a.pdf"],
            title="Operations Packet",
            summary="A seeded summary that does not mention the table caption explicitly.",
            key_point="The packet preserves operating evidence.",
            claim="The packet can support evidence tracing.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/b.pdf"],
            title="Plain Packet",
            summary="A plain control document.",
            key_point="This control document is intentionally generic.",
            claim="This control document should not outrank the structured packet.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        report = retrieve_knowledge(
            query="executive summary kpi summary table",
            top=2,
            graph_hops=0,
            include_renders=True,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["results"][0]["title"], "Operations Packet")
        matched_artifact = report.payload["results"][0]["matched_artifacts"][0]
        self.assertEqual(matched_artifact["caption_text"], "Table 1. KPI Summary")
        self.assertIn("1. Executive Summary", matched_artifact["section_path"])

    def test_sync_image_only_pdf_emits_page_image_artifact_and_hybrid_work(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        hybrid = pending.payload["hybrid_enrichment"]
        self.assertEqual(hybrid["mode"], "candidate-prepared")
        self.assertTrue(hybrid["workflow_auto_supported"])
        self.assertTrue(hybrid["hybrid_work_path"])
        self.assertTrue(hybrid["capability_gap_reason"])

        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        scan_source_dir = (
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/scan.pdf"]
        )
        artifact_index = read_json(scan_source_dir / "artifact_index.json")
        page_image = next(
            item
            for item in artifact_index["artifacts"]
            if item.get("artifact_type") == "page-image"
        )
        pdf_document = read_json(scan_source_dir / "pdf_document.json")
        self.assertEqual(
            pdf_document["page_contexts"][0]["page_image_artifact_id"], page_image["artifact_id"]
        )
        self.assertIn(pdf_document["page_contexts"][0]["text_layer_quality"], {"none", "weak"})

        hybrid_work = read_json(workspace.knowledge_base_staging_dir / "hybrid_work.json")
        source_work = next(
            item
            for item in hybrid_work["sources"]
            if item.get("source_id") == by_path["original_doc/scan.pdf"]
        )
        unit_work = source_work["units"][0]
        self.assertIn("page-image", unit_work["candidate_kinds"])
        self.assertIn(page_image["artifact_id"], unit_work["target_artifact_ids"])

    def test_workflow_runner_surfaces_hybrid_enrichment_gap_after_valid_sync(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        sync_report = CommandReport(
            exit_code=0,
            payload={
                "status": READY,
                "sync_status": "valid",
                "hybrid_enrichment": {
                    "mode": "candidate-prepared",
                    "eligible_unit_count": 4,
                    "overlay_unit_count": 0,
                    "hybrid_work_path": "knowledge_base/staging/hybrid_work.json",
                },
            },
            lines=[],
        )
        status_report = CommandReport(
            exit_code=0,
            payload={"status": READY},
            lines=[],
        )
        with (
            mock.patch("docmason.commands.status_workspace", return_value=status_report),
            mock.patch("docmason.commands.sync_workspace", return_value=sync_report),
        ):
            report = run_workflow("knowledge-base-sync", paths=workspace)

        self.assertEqual(report.payload["status"], DEGRADED)
        self.assertEqual(report.payload["workflow_status"], "needs-hybrid-enrichment")
        self.assertEqual(report.payload["final_report"]["sync_status"], "valid")
        self.assertEqual(
            report.payload["final_report"]["hybrid_enrichment"]["mode"],
            "candidate-prepared",
        )
        self.assertIn("knowledge-construction", report.payload["next_workflows"])
        self.assertIn("hybrid_work.json", report.payload["next_steps"][0])

    def test_page_image_results_mark_published_artifacts_insufficient_without_overlay(self) -> None:
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
            key_point=(
                "The published baseline preserves the rendered page but not "
                "enough semantic detail."
            ),
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

        report = retrieve_knowledge(
            query="scanned workflow page image",
            top=2,
            graph_hops=0,
            include_renders=True,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertFalse(report.payload["published_artifacts_sufficient"])
        self.assertTrue(report.payload["source_escalation_required"])
        self.assertIn("hybrid multimodal enrichment", report.payload["source_escalation_reason"])
        first_result = report.payload["results"][0]
        self.assertTrue(first_result["matched_units"])
        self.assertIn(first_result["matched_units"][0]["text_layer_quality"], {"none", "weak"})
        self.assertTrue(first_result["matched_units"][0]["page_image_artifact_id"])
        self.assertTrue(report.payload["recommended_hybrid_targets"])
        recommended = report.payload["recommended_hybrid_targets"][0]
        self.assertEqual(recommended["source_id"], first_result["source_id"])
        self.assertTrue(recommended["required_overlay_slots"])
        self.assertTrue(recommended["target_focus_render_assets"])

    def test_semantic_overlay_enriches_retrieve_and_trace_outputs(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        first_source = (
            workspace.knowledge_base_staging_dir / "sources" / pending_sources[0]["source_id"]
        )
        second_source = (
            workspace.knowledge_base_staging_dir / "sources" / pending_sources[1]["source_id"]
        )
        self.build_seeded_knowledge(
            first_source,
            title="Visual Review",
            summary="A neutral seeded summary.",
            key_point="The source preserves evidence for follow-up.",
            claim="The source can support review work.",
        )
        self.build_seeded_knowledge(
            second_source,
            title="Control Review",
            summary="A control seeded summary.",
            key_point="The control source is generic.",
            claim="The control source should rank lower for overlay-only hints.",
        )
        write_semantic_overlay(
            first_source,
            {
                "source_id": pending_sources[0]["source_id"],
                "unit_id": "page-001",
                "derivation_mode": "hybrid",
                "eligible_reason": "diagram-or-ui-page",
                "consumed_inputs": {
                    "render_assets": ["renders/page-001.png"],
                    "artifact_ids": [],
                },
                "semantic_labels": [
                    {
                        "label": "diagram-summary",
                        "text": "Approval flow between intake and review teams.",
                        "confidence": "high",
                    }
                ],
                "artifact_annotations": [],
                "cross_region_relations": [],
                "uncertainty_notes": [],
            },
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        report = retrieve_knowledge(
            query="approval flow intake review teams",
            top=2,
            graph_hops=0,
            include_renders=True,
            paths=workspace,
        )
        self.assertEqual(report.exit_code, 0)
        self.assertTrue(report.payload["results"][0]["matched_overlay_unit_ids"])
        self.assertTrue(report.payload["results"][0]["matched_units"][0]["semantic_labels"])

        answer_file = workspace.root / "overlay-answer.txt"
        answer_file.write_text(
            "The approval flow connects intake and review teams.",
            encoding="utf-8",
        )
        trace = trace_knowledge(answer_file=str(answer_file), top=2, paths=workspace)
        self.assertTrue(trace.payload["supporting_overlay_unit_ids"])
        self.assertTrue(trace.payload["segments"][0]["supporting_overlay_unit_ids"])
        self.assertTrue(trace.payload["segments"][0]["semantic_supports"])
        self.assertEqual(
            trace.payload["segments"][0]["semantic_supports"][0]["semantic_overlay_asset"],
            "semantic_overlay/page-001.json",
        )

    def test_write_semantic_overlay_backfills_lane_bc_contract_fields(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        first_source = (
            workspace.knowledge_base_staging_dir / "sources" / pending_sources[0]["source_id"]
        )
        asset = write_semantic_overlay(
            first_source,
            {
                "source_id": pending_sources[0]["source_id"],
                "unit_id": "page-001",
                "eligible_reason": "diagram-or-ui-page",
                "consumed_inputs": {
                    "render_assets": ["renders/page-001.png"],
                },
                "semantic_labels": [
                    {
                        "label": "diagram-summary",
                        "text": "Approval flow between intake and review teams.",
                        "confidence": "high",
                    }
                ],
                "artifact_annotations": [],
                "cross_region_relations": [],
                "uncertainty_notes": [],
            },
        )

        overlay = read_json(first_source / asset)
        self.assertEqual(overlay["origin"], "sync-hybrid")
        self.assertTrue(overlay["source_fingerprint"])
        self.assertTrue(overlay["unit_evidence_fingerprint"])
        self.assertIn("diagram-summary", overlay["covered_slots"])
        self.assertEqual(
            overlay["consumed_inputs"]["focus_render_assets"],
            ["renders/page-001.png"],
        )

    def test_compare_query_applies_coverage_bonus_to_multiple_sources(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        report = retrieve_knowledge(
            query="compare campaign planning brief and campaign evaluation plan",
            top=2,
            graph_hops=0,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertGreaterEqual(len(report.payload["results"]), 2)
        self.assertGreater(report.payload["results"][1]["score"]["compare_coverage_bonus"], 0)

    def test_sync_backfills_phase_three_artifacts_for_legacy_unchanged_sources(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        for source_id in source_ids:
            for base_dir in (
                workspace.knowledge_base_current_dir / "sources" / source_id,
                workspace.knowledge_base_staging_dir / "sources" / source_id,
            ):
                artifact_index = base_dir / "artifact_index.json"
                if artifact_index.exists():
                    artifact_index.unlink()
                visual_dir = base_dir / "visual_layout"
                if visual_dir.exists():
                    import shutil

                    shutil.rmtree(visual_dir)
                evidence_manifest_path = base_dir / "evidence_manifest.json"
                evidence_manifest = read_json(evidence_manifest_path)
                evidence_manifest.pop("artifact_index_asset", None)
                evidence_manifest.pop("visual_layout_assets", None)
                write_json(evidence_manifest_path, evidence_manifest)

        result = sync_workspace(workspace)

        self.assertEqual(result.payload["sync_status"], "valid")
        self.assertGreaterEqual(result.payload["build_stats"]["rebuilt_sources"], 2)
        rebuilt_source_dir = workspace.knowledge_base_current_dir / "sources" / source_ids[0]
        self.assertTrue((rebuilt_source_dir / "artifact_index.json").exists())
        rebuilt_evidence = read_json(rebuilt_source_dir / "evidence_manifest.json")
        self.assertTrue(rebuilt_evidence["visual_layout_assets"])

    def test_citation_first_trace_returns_source_and_unit_provenance(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        source_report = trace_knowledge(source_id=source_ids[0], paths=workspace)
        self.assertEqual(source_report.exit_code, 0)
        self.assertEqual(source_report.payload["source"]["source_id"], source_ids[0])
        self.assertTrue(source_report.payload["source"]["relations"]["outgoing"])

        unit_report = trace_knowledge(source_id=source_ids[0], unit_id="page-001", paths=workspace)
        self.assertEqual(unit_report.exit_code, 0)
        self.assertEqual(unit_report.payload["unit"]["unit_id"], "page-001")
        self.assertTrue(unit_report.payload["unit"]["consumers"])

    def test_answer_file_and_session_trace_capture_grounding_and_reuse_logs(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        answer_file = workspace.root / "answer.txt"
        answer_file.write_text(
            "\n\n".join(
                [
                    "The architecture strategy connects the operating model to implementation.",
                    "Zyzzyva quasar nebulae orthonormal frabjous snark.",
                ]
            ),
            encoding="utf-8",
        )

        report = trace_knowledge(answer_file=str(answer_file), top=2, paths=workspace)
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.payload["status"], DEGRADED)
        self.assertEqual(report.payload["answer_state"], "partially-grounded")
        self.assertTrue(report.payload["render_inspection_required"])
        self.assertTrue(report.payload["supporting_source_ids"])
        self.assertTrue(report.payload["supporting_unit_ids"])
        self.assertEqual(report.payload["segments"][0]["grounding_status"], "grounded")
        self.assertEqual(report.payload["segments"][1]["grounding_status"], "unresolved")
        session_id = report.payload["session_id"]
        trace_log = workspace.retrieval_traces_dir / f"{report.payload['trace_id']}.json"
        self.assertTrue(trace_log.exists())

        reused = trace_knowledge(session_id=session_id, top=2, paths=workspace)
        self.assertEqual(reused.exit_code, 2)
        self.assertEqual(reused.payload["answer_state"], "partially-grounded")
        self.assertTrue(reused.payload["render_inspection_required"])
        self.assertTrue(reused.payload["supporting_source_ids"])
        self.assertTrue(reused.payload["supporting_unit_ids"])
        self.assertTrue(reused.payload["reused_session"])

    def test_lexically_overlapping_but_unsupported_answer_does_not_trace_as_grounded(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        answer_file = workspace.root / "unsupported-answer.txt"
        answer_file.write_text(
            (
                "The campaign planning brief says DocMason already ships watch mode "
                "and requires a database service."
            ),
            encoding="utf-8",
        )

        report = trace_knowledge(answer_file=str(answer_file), top=2, paths=workspace)
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.payload["status"], DEGRADED)
        self.assertEqual(report.payload["answer_state"], "unresolved")
        self.assertEqual(report.payload["segments"][0]["grounding_status"], "unresolved")

    def test_review_summary_tracks_no_result_queries_and_degraded_answer_traces(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)

        retrieval = retrieve_knowledge(
            query="Zyzzyva quasar nebulae orthonormal frabjous snark",
            top=2,
            graph_hops=1,
            paths=workspace,
        )
        self.assertEqual(retrieval.exit_code, 2)
        self.assertEqual(retrieval.payload["retrieve_status"], "no-results")

        answer_file = workspace.root / "degraded-answer.txt"
        answer_file.write_text(
            "\n\n".join(
                [
                    "The architecture strategy connects the operating model to implementation.",
                    "Zyzzyva quasar nebulae orthonormal frabjous snark.",
                ]
            ),
            encoding="utf-8",
        )
        trace = trace_knowledge(answer_file=str(answer_file), top=2, paths=workspace)
        self.assertEqual(trace.exit_code, 2)

        summary = read_json(workspace.review_summary_path)
        self.assertEqual(summary["query_sessions"]["total"], 2)
        self.assertEqual(summary["retrieval_traces"]["total"], 1)
        self.assertEqual(len(summary["query_sessions"]["no_results"]), 1)
        self.assertEqual(len(summary["query_sessions"]["degraded_answer_runs"]), 1)
        self.assertTrue(summary["query_sessions"]["frequent_sources"])
        patterns = {item["pattern"] for item in summary["query_sessions"]["failure_patterns"]}
        self.assertIn("no-results-retrieval", patterns)
        self.assertIn("degraded-answer-run", patterns)
        candidate_case_types = {
            item["case_type"] for item in summary["query_sessions"]["candidate_cases"]
        }
        self.assertIn("no-results", candidate_case_types)
        self.assertIn("degraded-answer-trace", candidate_case_types)

    def test_retrieve_warns_when_active_thread_is_not_canonical_ask(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        self.seed_active_thread_turn(workspace, front_door_state="native-reconciled-only")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-operator"}, clear=False):
            report = retrieve_knowledge(
                query="architecture strategy",
                top=2,
                graph_hops=1,
                paths=workspace,
            )

        self.assertEqual(
            report.payload["front_door"]["warning"]["code"],
            "noncanonical-operator-direct",
        )
        session_path = workspace.query_sessions_dir / f"{report.payload['session_id']}.json"
        session_payload = read_json(session_path)
        self.assertEqual(session_payload["log_origin"], "operator-direct")

    def test_retrieve_uses_interactive_origin_when_active_thread_is_canonical_ask(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        self.seed_active_thread_turn(workspace, front_door_state="canonical-ask")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-operator"}, clear=False):
            report = retrieve_knowledge(
                query="architecture strategy",
                top=2,
                graph_hops=1,
                paths=workspace,
            )

        self.assertIsNone(report.payload["front_door"]["warning"])
        session_path = workspace.query_sessions_dir / f"{report.payload['session_id']}.json"
        session_payload = read_json(session_path)
        self.assertEqual(session_payload["log_origin"], "interactive-ask")

    def test_trace_warns_when_active_thread_is_not_canonical_ask(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        self.seed_active_thread_turn(workspace, front_door_state="native-reconciled-only")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-operator"}, clear=False):
            report = trace_knowledge(source_id=source_ids[0], paths=workspace)

        self.assertEqual(
            report.payload["front_door"]["warning"]["code"],
            "noncanonical-operator-direct",
        )
        trace_path = workspace.retrieval_traces_dir / f"{report.payload['trace_id']}.json"
        trace_payload = read_json(trace_path)
        self.assertEqual(trace_payload["log_origin"], "operator-direct")

    def test_retrieve_and_trace_survive_reconciliation_lease_conflict_with_warning(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)

        from docmason.coordination import LeaseConflictError

        with mock.patch(
            "docmason.commands._maybe_reconcile_active_thread",
            side_effect=LeaseConflictError(
                "Could not acquire workspace lease for `conversation:thread-operator` within 10.0s."
            ),
        ):
            retrieve_report = retrieve_knowledge(
                query="architecture strategy",
                top=2,
                graph_hops=1,
                paths=workspace,
            )
            trace_report = trace_knowledge(source_id=source_ids[0], paths=workspace)

        self.assertEqual(retrieve_report.payload["coordination"]["state"], "warning")
        self.assertEqual(trace_report.payload["coordination"]["state"], "warning")
        self.assertTrue(retrieve_report.payload["results"])
        self.assertEqual(trace_report.payload["status"], READY)

    def test_ambiguous_relocation_is_marked_without_guessing_identity(self) -> None:
        workspace = self.make_workspace()
        first = workspace.source_dir / "proposal-alpha.pdf"
        second = workspace.source_dir / "proposal-beta.pdf"
        self.create_pdf(first)
        self.create_pdf(second)

        _payload, active_sources, ambiguous_match, _change_set = update_source_index(workspace)
        self.assertFalse(ambiguous_match)
        self.assertEqual(len(active_sources), 2)

        first.unlink()
        second.unlink()
        self.create_pdf(workspace.source_dir / "proposal.pdf")

        _payload, active_sources, ambiguous_match, change_set = update_source_index(workspace)
        self.assertTrue(ambiguous_match)
        self.assertEqual(change_set["stats"]["ambiguous"], 1)
        self.assertEqual(active_sources[0]["change_classification"], "ambiguous")
        self.assertEqual(len(active_sources[0]["matched_source_ids"]), 2)


if __name__ == "__main__":
    unittest.main()

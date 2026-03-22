"""Tests for the user-native source reference resolution layer."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from docmason.ask import complete_ask_turn, prepare_ask_turn
from docmason.cli import build_parser
from docmason.commands import retrieve_knowledge, sync_workspace, trace_knowledge
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import _effective_source_ids_from_reference, run_retrieval_query
from docmason.source_references import (
    build_reference_resolution_summary,
    resolve_reference_query,
)

ROOT = Path(__file__).resolve().parents[1]


class ReferenceResolutionTests(unittest.TestCase):
    """Cover exact, approximate, and legacy-fallback source reference resolution."""

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
                "prepared_at": "2026-03-18T00:00:00Z",
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
            },
        )

    def create_pdf(self, path: Path, *, page_count: int = 1) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for _index in range(page_count):
            writer.add_blank_page(width=144, height=144)
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
                "notes_en": "Reference-resolution test fixture.",
                "notes_source": "Reference-resolution test fixture.",
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

    def publish_seeded_pdf_corpus(self, workspace: WorkspacePaths) -> dict[str, str]:
        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        pending_sources = [
            item
            for item in pending.payload["pending_sources"]
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        self.assertEqual(len(pending_sources), 2)
        pending_by_path = {
            str(item["current_path"]): str(item["source_id"])
            for item in pending_sources
            if isinstance(item.get("current_path"), str)
        }
        a_source_id = pending_by_path.get("original_doc/a.pdf")
        b_source_id = pending_by_path.get("original_doc/b.pdf")
        ordered_source_ids = [
            source_id for source_id in (a_source_id, b_source_id) if isinstance(source_id, str)
        ]
        if len(ordered_source_ids) != 2:
            ordered_source_ids = sorted(
                [str(item["source_id"]) for item in pending_sources],
                key=lambda source_id: str(
                    next(
                        (
                            item.get("current_path")
                            for item in pending_sources
                            if item.get("source_id") == source_id
                        ),
                        "",
                    )
                ),
            )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / ordered_source_ids[0],
            title="Campaign Planning Brief",
            summary="A strategy deck about architecture and operating model.",
            key_point="The strategy defines an architecture operating model.",
            claim="The architecture deck connects strategy to implementation.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / ordered_source_ids[1],
            title="Architecture Strategy Companion",
            summary="A companion planning artifact with similar domain language.",
            key_point="The companion document adds rollout notes.",
            claim="The companion document should not steal an exact source reference.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return {
            str(item["current_path"]): str(item["source_id"])
            for item in pending_sources
            if isinstance(item.get("current_path"), str)
        }

    def test_exact_source_reference_hard_filters_retrieval(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        deck_source_id = source_ids["original_doc/a.pdf"]

        report = retrieve_knowledge(
            query="Campaign Planning Brief page 1 operating model",
            top=5,
            graph_hops=2,
            include_renders=True,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertIn("Reference resolution: exact", report.lines)
        self.assertEqual(report.payload["filters"]["source_ids"], [deck_source_id])
        self.assertEqual(
            report.payload["reference_resolution"]["resolved_source_id"], deck_source_id
        )
        self.assertEqual(report.payload["results"][0]["source_id"], deck_source_id)
        self.assertEqual(len(report.payload["results"]), 1)

    def test_exact_unit_locator_prefers_targeted_page(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf", page_count=2)
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        deck_source_id = source_ids["original_doc/a.pdf"]

        report = retrieve_knowledge(
            query="Campaign Planning Brief page 2 visual detail",
            top=3,
            graph_hops=1,
            include_renders=True,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(
            report.payload["reference_resolution"]["resolved_source_id"], deck_source_id
        )
        self.assertEqual(report.payload["reference_resolution"]["resolved_unit_id"], "page-002")
        self.assertEqual(report.payload["results"][0]["matched_units"][0]["unit_id"], "page-002")

    def test_approximate_reference_continues_with_notice(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_pdf_corpus(workspace)

        report = retrieve_knowledge(
            query="Architecture Strategy page 1",
            top=3,
            graph_hops=1,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["reference_resolution"]["status"], "approximate")
        self.assertTrue(report.payload["reference_resolution"]["continued_with_best_effort"])
        resolved_source_id = report.payload["reference_resolution"]["resolved_source_id"]
        self.assertEqual(report.payload["filters"]["source_ids"], [resolved_source_id])
        self.assertEqual(report.payload["results"][0]["source_id"], resolved_source_id)
        self.assertTrue(any(line.startswith("Reference notice:") for line in report.lines))

    def test_exact_source_with_soft_locator_still_filters_to_source_only(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf", page_count=2)
        self.create_pdf(workspace.source_dir / "b.pdf", page_count=2)
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        deck_source_id = source_ids["original_doc/a.pdf"]

        report = retrieve_knowledge(
            query="Campaign Planning Brief page 9",
            top=5,
            graph_hops=2,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["reference_resolution"]["status"], "approximate")
        self.assertEqual(report.payload["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(report.payload["reference_resolution"]["unit_match_status"], "unresolved")
        self.assertEqual(report.payload["filters"]["source_ids"], [deck_source_id])
        self.assertTrue(
            all(item["source_id"] == deck_source_id for item in report.payload["results"])
        )

    def test_prior_path_still_resolves_after_rename(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "example.pdf")
        self.create_pdf(workspace.source_dir / "companion.pdf")
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        original_source_id = source_ids["original_doc/example.pdf"]
        moved_dir = workspace.source_dir / "moved"
        moved_dir.mkdir()
        (workspace.source_dir / "example.pdf").rename(moved_dir / "renamed-example.pdf")

        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        report = retrieve_knowledge(
            query="original_doc/example.pdf page 1",
            top=2,
            graph_hops=1,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["reference_resolution"]["status"], "exact")
        self.assertEqual(
            report.payload["reference_resolution"]["resolved_source_id"], original_source_id
        )
        self.assertEqual(
            report.payload["results"][0]["current_path"],
            "original_doc/moved/renamed-example.pdf",
        )

    def test_hidden_slide_render_alias_resolves_legacy_records(self) -> None:
        source_records = [
            {
                "source_id": "deck-1",
                "current_path": "original_doc/design/Test Design Deck.pptx",
                "document_type": "pptx",
                "source_family": "corpus",
                "title": "Test Design Deck",
            }
        ]
        unit_records = [
            {
                "source_id": "deck-1",
                "current_path": "original_doc/design/Test Design Deck.pptx",
                "document_type": "pptx",
                "source_family": "corpus",
                "unit_id": "slide-003",
                "unit_type": "slide",
                "ordinal": 3,
                "title": "Slide 3",
                "render_references": ["renders/page-002.png"],
                "text": "",
                "structure_summary": '{"visible_text": ["Detail Architecture"], "ordinal": 3}',
            }
        ]

        resolution = resolve_reference_query(
            "Test Design Deck page 2 detail architecture",
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(resolution["status"], "exact")
        self.assertEqual(resolution["resolved_source_id"], "deck-1")
        self.assertEqual(resolution["resolved_unit_id"], "slide-003")
        self.assertEqual(build_reference_resolution_summary(resolution), "exact-reference")

    def test_semantic_page_alias_prefers_best_matching_unit(self) -> None:
        source_records = [
            {
                "source_id": "deck-a",
                "current_path": "original_doc/design/Search Platform Architecture Review.pptx",
                "document_type": "pptx",
                "source_family": "corpus",
                "title": "Search Platform Architecture Review",
            },
            {
                "source_id": "deck-b",
                "current_path": "original_doc/design/Analytics Platform Architecture Review.pptx",
                "document_type": "pptx",
                "source_family": "corpus",
                "title": "Analytics Platform Architecture Review",
            },
        ]
        unit_records = [
            {
                "source_id": "deck-a",
                "current_path": source_records[0]["current_path"],
                "document_type": "pptx",
                "source_family": "corpus",
                "unit_id": "slide-004",
                "unit_type": "slide",
                "ordinal": 4,
                "title": "Slide 4",
                "text": "Concept Architecture",
                "structure_summary": '{"visible_text": ["Concept Architecture"]}',
            },
            {
                "source_id": "deck-b",
                "current_path": source_records[1]["current_path"],
                "document_type": "pptx",
                "source_family": "corpus",
                "unit_id": "slide-005",
                "unit_type": "slide",
                "ordinal": 5,
                "title": "Slide 5",
                "text": "Detail Architecture",
                "structure_summary": '{"visible_text": ["Detail Architecture"]}',
            },
        ]

        resolution = resolve_reference_query(
            "detail architecture page in the Design ppt",
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(resolution["status"], "approximate")
        self.assertEqual(resolution["resolved_source_id"], "deck-b")
        self.assertEqual(resolution["resolved_unit_id"], "slide-005")
        self.assertTrue(resolution["notice_text"])

    def test_xlsx_sheet_name_and_cell_hint_resolve_to_sheet(self) -> None:
        source_records = [
            {
                "source_id": "sheet-book",
                "current_path": "original_doc/Capability Landscape Workbook.xlsx",
                "document_type": "xlsx",
                "source_family": "corpus",
                "title": "Capability Landscape Workbook",
            }
        ]
        unit_records = [
            {
                "source_id": "sheet-book",
                "current_path": source_records[0]["current_path"],
                "document_type": "xlsx",
                "source_family": "corpus",
                "unit_id": "sheet-002",
                "unit_type": "sheet",
                "ordinal": 2,
                "title": "Channel Capabilities",
                "text": "A14: Sample Value",
                "structure_summary": '{"sheet_name": "Channel Capabilities"}',
            }
        ]

        resolution = resolve_reference_query(
            "Capability Landscape Workbook sheet Channel Capabilities A14",
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(resolution["resolved_source_id"], "sheet-book")
        self.assertEqual(resolution["resolved_unit_id"], "sheet-002")
        self.assertIn("A14", str(resolution["parsed_locator_ref"]))

    def test_docx_heading_alias_is_best_effort(self) -> None:
        source_records = [
            {
                "source_id": "rfi-doc",
                "current_path": "original_doc/platform/Architecture-Request-1.1.docx",
                "document_type": "docx",
                "source_family": "corpus",
                "title": "Architecture Request 1.1",
            }
        ]
        unit_records = [
            {
                "source_id": "rfi-doc",
                "current_path": source_records[0]["current_path"],
                "document_type": "docx",
                "source_family": "corpus",
                "unit_id": "section-001",
                "unit_type": "section",
                "ordinal": 1,
                "title": "Section 1",
                "text": "Background\nThe high-level enterprise architecture",
                "structure_summary": (
                    '{"blocks": [{"kind": "paragraph", "text": "Background"}, '
                    '{"kind": "paragraph", "text": "The high-level enterprise architecture"}]}'
                ),
            }
        ]

        resolution = resolve_reference_query(
            "Architecture Request 1.1 background section",
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(resolution["resolved_source_id"], "rfi-doc")
        self.assertEqual(resolution["resolved_unit_id"], "section-001")
        self.assertEqual(resolution["status"], "approximate")
        self.assertTrue(resolution["notice_text"])

    def test_approximate_resolved_unit_is_reranked_first_within_source(self) -> None:
        rules_engine_path = (
            "original_doc/Architecture documents/Modernize Business Rules Workflow.pdf"
        )
        rules_engine_searchable = (
            "Modernize Business Rules Workflow current state pain points"
        )
        retrieval_data = {
            "manifest": {"source_signature": "reference-resolution-test"},
            "source_records": [
                {
                    "source_id": "deck-1",
                    "current_path": rules_engine_path,
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "title": "Modernize Business Rules Workflow",
                    "summary_en": "Business rules workflow design document.",
                    "summary_source": "Business rules workflow design document.",
                    "searchable_text": rules_engine_searchable,
                    "available_channels": ["text", "render", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "trust_prior": {},
                }
            ],
            "unit_records": [
                {
                    "source_id": "deck-1",
                    "current_path": rules_engine_path,
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "unit_id": "page-001",
                    "unit_type": "page",
                    "ordinal": 1,
                    "title": "Page 1",
                    "text": "Modernize Business Rules Workflow introduction and overview",
                    "structure_summary": '{"ordinal": 1, "text_excerpt": "Overview"}',
                },
                {
                    "source_id": "deck-1",
                    "current_path": rules_engine_path,
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "unit_id": "page-011",
                    "unit_type": "page",
                    "ordinal": 11,
                    "title": "Page 11",
                    "text": "Current State Pain Points for the business rules workflow",
                    "structure_summary": (
                        '{"ordinal": 11, "text_excerpt": "Current State Pain Points"}'
                    ),
                },
            ],
            "graph_edges": [],
        }

        result = run_retrieval_query(
            retrieval_data,
            query="current state pain points page in business rules workflow document",
            top=1,
            graph_hops=0,
            document_types=None,
            source_ids=["deck-1"],
            include_renders=False,
            reference_resolution={
                "status": "approximate",
                "resolved_source_id": "deck-1",
                "resolved_unit_id": "page-011",
            },
        )

        self.assertEqual(result["results"][0]["matched_units"][0]["unit_id"], "page-011")

    def test_approximate_source_plus_exact_unit_hard_filters_resolved_source(self) -> None:
        interaction_title = (
            "Interaction Memory for Architecture Review page-by-page authoring constraints"
        )
        interaction_summary = (
            "A related interaction memory with stronger broad lexical overlap."
        )
        interaction_searchable = (
            "WIP Gate 2 Platform Review slide 35 page by page authoring constraints"
        )
        retrieval_data = {
            "manifest": {"source_signature": "reference-resolution-test"},
            "source_records": [
                {
                    "source_id": "deck-1",
                    "current_path": "original_doc/WIP_Gate_2_Platform_Review.pptx",
                    "document_type": "pptx",
                    "source_family": "corpus",
                    "title": "Platform Operations Gate 2 Review",
                    "summary_en": "Gate 2 review deck for platform operations.",
                    "summary_source": "Gate 2 review deck for platform operations.",
                    "searchable_text": "WIP Gate 2 Platform Review slide 35 target deck",
                    "available_channels": ["text", "render", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "trust_prior": {},
                },
                {
                    "source_id": "memory-1",
                    "current_path": "interaction/interaction-memory-1",
                    "document_type": "interaction",
                    "source_family": "interaction-memory",
                    "title": interaction_title,
                    "summary_en": interaction_summary,
                    "summary_source": interaction_summary,
                    "searchable_text": interaction_searchable,
                    "available_channels": ["text", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "trust_prior": {},
                    "memory_kind": "constraint",
                    "answer_use_policy": "direct-support",
                    "durability": "durable",
                    "retrieval_rank_prior": "high",
                },
            ],
            "unit_records": [
                {
                    "source_id": "deck-1",
                    "current_path": "original_doc/WIP_Gate_2_Platform_Review.pptx",
                    "document_type": "pptx",
                    "source_family": "corpus",
                    "unit_id": "slide-035",
                    "unit_type": "slide",
                    "ordinal": 35,
                    "logical_ordinal": 35,
                    "render_ordinal": 32,
                    "title": "Slide 35",
                    "text": "WIP Gate 2 Platform Review target slide detail",
                    "structure_summary": '{"ordinal": 35, "text_excerpt": "Gate 2 detail"}',
                },
                {
                    "source_id": "memory-1",
                    "current_path": "interaction/interaction-memory-1",
                    "document_type": "interaction",
                    "source_family": "interaction-memory",
                    "unit_id": "turn-011",
                    "unit_type": "interaction-turn",
                    "ordinal": 11,
                    "title": "Conversation turn 11",
                    "text": "WIP Gate 2 Platform Review page by page authoring constraints",
                    "structure_summary": (
                        '{"ordinal": 11, "text_excerpt": '
                        '"page by page authoring constraints"}'
                    ),
                },
            ],
            "graph_edges": [],
        }

        effective_source_ids = _effective_source_ids_from_reference(
            None,
            {
                "status": "approximate",
                "source_match_status": "approximate",
                "unit_match_status": "exact",
                "resolved_source_id": "deck-1",
                "resolved_unit_id": "slide-035",
            },
        )
        result = run_retrieval_query(
            retrieval_data,
            query="WIP Gate 2 Platform Review slide 35",
            top=5,
            graph_hops=0,
            document_types=None,
            source_ids=effective_source_ids,
            include_renders=False,
            reference_resolution={
                "status": "approximate",
                "source_match_status": "approximate",
                "unit_match_status": "exact",
                "resolved_source_id": "deck-1",
                "resolved_unit_id": "slide-035",
            },
        )

        self.assertEqual(effective_source_ids, ["deck-1"])
        self.assertEqual(result["results"][0]["source_id"], "deck-1")
        self.assertEqual(result["results"][0]["matched_units"][0]["unit_id"], "slide-035")

    def test_prepare_turn_and_answer_trace_keep_reference_resolution(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf", page_count=2)
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_pdf_corpus(workspace)

        turn = prepare_ask_turn(
            workspace,
            question="Campaign Planning Brief page 2 visual detail",
            semantic_analysis={
                "question_class": "answer",
                "question_domain": "workspace-corpus",
                "route_reason": "Reference-resolution propagation test.",
            },
        )
        self.assertEqual(turn["reference_resolution"]["status"], "exact")
        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The exact visual detail remains unresolved without inspecting the render.\n",
            encoding="utf-8",
        )

        trace_report = trace_knowledge(
            answer_file=turn["answer_file_path"],
            top=1,
            paths=workspace,
        )
        self.assertIn("reference_resolution", trace_report.payload)
        self.assertEqual(
            trace_report.payload["reference_resolution"]["resolved_unit_id"], "page-002"
        )

        updated = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            trace_ids=[trace_report.payload["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt=(
                "The exact visual detail remains unresolved without inspecting the render."
            ),
            status="answered",
        )
        self.assertEqual(updated["reference_resolution"]["resolved_unit_id"], "page-002")
        self.assertEqual(updated["reference_resolution_summary"], "exact-reference")

    def test_trace_cli_surface_remains_id_first(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["trace", "--source-ref", "Platform Review slide 35"])

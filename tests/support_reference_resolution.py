"""Tests for the user-native source reference resolution layer."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import complete_ask_turn, prepare_ask_turn
from docmason.cli import build_parser
from docmason.cli import main as docmason_main
from docmason.commands import retrieve_knowledge, sync_workspace, trace_knowledge
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import _effective_source_ids_from_reference, run_retrieval_query
from docmason.source_references import (
    build_reference_resolution_summary,
    build_source_reference_fields,
    build_unit_reference_fields,
    resolve_reference_query,
)
from tests.support_ready_workspace import seed_self_contained_bootstrap_state

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
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-18T00:00:00Z",
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
            title="Project Planning Brief",
            summary="A planning brief about a project outline and work plan.",
            key_point="The outline defines a practical work plan.",
            claim="The project outline connects planning to implementation.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / ordered_source_ids[1],
            title="Project Outline Companion",
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
            query="Project Planning Brief page 1 work plan",
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
            query="Project Planning Brief page 2 visual detail",
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
            query="Project Outline page 1",
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

    def test_missing_explicit_source_hard_stops_retrieval(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_pdf_corpus(workspace)

        report = retrieve_knowledge(
            query=(
                "Using only the document 'Missing Project Brief', summarize the project "
                "outline in 3 bullet points. Do not use any other source."
            ),
            top=5,
            graph_hops=2,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.payload["results"], [])
        self.assertEqual(report.payload["filters"]["source_ids"], [])
        self.assertEqual(report.payload["reference_resolution"]["status"], "unresolved")
        self.assertEqual(
            report.payload["reference_resolution"]["unresolved_reason"],
            "missing-source",
        )
        self.assertTrue(report.payload["reference_resolution"]["hard_boundary"])
        self.assertFalse(report.payload["reference_resolution"]["continued_with_best_effort"])
        self.assertIsNone(report.payload["reference_resolution"]["target_source_ref"])

    def test_repo_relative_path_with_spaces_resolves_as_one_source_text(self) -> None:
        source_records = [
            {
                "source_id": "source-001",
                "source_family": "corpus",
                "current_path": (
                    "original_doc/Project notes/"
                    "Regional Process Overview.pptx"
                ),
                "title": "Regional Process Overview",
                "prior_paths": [],
                "path_history": [],
            }
        ]

        result = resolve_reference_query(
            (
                "Using only original_doc/Project notes/Regional Process Overview.pptx, "
                "give 5 bullets on process scope."
            ),
            source_records=source_records,
            unit_records=[],
        )

        self.assertEqual(result["status"], "exact")
        self.assertEqual(
            result["requested_source_text"],
            "original_doc/Project notes/Regional Process Overview.pptx",
        )
        self.assertEqual(result["resolved_source_id"], "source-001")

    def test_exact_source_title_does_not_invent_locator_approximation(self) -> None:
        source_records = [
            {
                "source_id": "source-001",
                "source_family": "corpus",
                "current_path": "original_doc/Project notes/overview.pptx",
                "title": "Regional Process Overview",
                "prior_paths": [],
                "path_history": [],
            }
        ]
        unit_records = [
            {
                "source_id": "source-001",
                "source_family": "corpus",
                "unit_id": "slide-001",
                "unit_type": "slide",
                "logical_ordinal": 1,
                "render_ordinal": 1,
                "title": "Process Scope",
                "locator_aliases": ["Process Scope"],
            }
        ]

        result = resolve_reference_query(
            (
                'Using only the document "Regional Process Overview", '
                "give 5 bullets on process scope."
            ),
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["source_match_status"], "exact")
        self.assertEqual(result["unit_match_status"], "none")
        self.assertIsNone(result["resolved_unit_id"])
        self.assertIsNone(result["parsed_locator_ref"])
        self.assertEqual(build_reference_resolution_summary(result), "exact-reference")

    def test_sibling_exact_title_collision_stays_unresolved(self) -> None:
        source_records = [
            {
                "source_id": "source-one-page",
                "source_family": "corpus",
                "current_path": "original_doc/notes/Project Snapshot 2025 one page.pdf",
                "title": "Project Snapshot 2025 one page",
                "prior_paths": [],
                "path_history": [],
            },
            {
                "source_id": "source-one",
                "source_family": "corpus",
                "current_path": "original_doc/notes/Project Snapshot 2025 1.pdf",
                "title": "Project Snapshot 2025 one",
                "prior_paths": [],
                "path_history": [],
            },
        ]

        result = resolve_reference_query(
            (
                'Using only the document "Project Snapshot 2025 one page", extract 4 bullet '
                'points. Do not use "Project Snapshot 2025 1" or any other source.'
            ),
            source_records=source_records,
            unit_records=[],
        )

        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["source_match_status"], "unresolved")
        self.assertEqual(result["unresolved_reason"], "ambiguous-source")
        self.assertTrue(result["hard_boundary"])
        self.assertIsNone(result["resolved_source_id"])

    def test_prepare_turn_machine_guard_hardens_source_scoped_question(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_pdf_corpus(workspace)

        turn = prepare_ask_turn(
            workspace,
            question=(
                "Using only the document 'Project Planning Brief', summarize the project "
                "outline. Do not use any other source."
            ),
            semantic_analysis={
                "question_class": "answer",
                "question_domain": "external-factual",
                "support_strategy": "web-first",
                "route_reason": "Deliberately wrong host hint for Wave 3 guard coverage.",
            },
        )

        self.assertEqual(turn["question_domain"], "workspace-corpus")
        self.assertEqual(turn["support_strategy"], "kb-first")
        self.assertTrue(turn["reference_resolution"]["analysis_guard_applied"])
        self.assertEqual(turn["source_scope_policy"]["scope_mode"], "source-scoped-hard")

    def test_exact_source_with_soft_locator_still_filters_to_source_only(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf", page_count=2)
        self.create_pdf(workspace.source_dir / "b.pdf", page_count=2)
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        deck_source_id = source_ids["original_doc/a.pdf"]

        report = retrieve_knowledge(
            query="Project Planning Brief page 9",
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

    def test_compare_query_preserves_declared_source_records_from_unquoted_titles(self) -> None:
        source_records = [
            {
                "source_id": "source-001",
                "source_family": "corpus",
                "current_path": "original_doc/a.pdf",
                "title": "Project Planning Brief",
                "prior_paths": [],
                "path_history": [],
            },
            {
                "source_id": "source-002",
                "source_family": "corpus",
                "current_path": "original_doc/b.pdf",
                "title": "Project Outline Companion",
                "prior_paths": [],
                "path_history": [],
            },
        ]

        result = resolve_reference_query(
            (
                "compare Project Planning Brief and Project Outline Companion on "
                "project outline"
            ),
            source_records=source_records,
            unit_records=[],
        )

        self.assertEqual(result["scope_mode"], "compare")
        self.assertEqual(result["compare_resolution_status"], "exact")
        self.assertEqual(result["declared_compare_expected_count"], 2)
        self.assertEqual(
            [item["requested_source_text"] for item in result["declared_compare_sources"]],
            ["Project Planning Brief", "Project Outline Companion"],
        )
        self.assertEqual(
            [item["source_match_status"] for item in result["declared_compare_sources"]],
            ["exact", "exact"],
        )
        self.assertEqual(
            [item["resolved_source_id"] for item in result["declared_compare_sources"]],
            ["source-001", "source-002"],
        )
        self.assertEqual(result["declared_compare_source_ids"], ["source-001", "source-002"])

    def test_compare_query_preserves_unresolved_declared_source_record(self) -> None:
        source_records = [
            {
                "source_id": "source-001",
                "source_family": "corpus",
                "current_path": "original_doc/a.pdf",
                "title": "Project Planning Brief",
                "prior_paths": [],
                "path_history": [],
            },
            {
                "source_id": "source-002",
                "source_family": "corpus",
                "current_path": "original_doc/b.pdf",
                "title": "Project Outline Companion",
                "prior_paths": [],
                "path_history": [],
            },
        ]

        result = resolve_reference_query(
            'Compare "Project Planning Brief" versus "Zebra Ledger" on project outline.',
            source_records=source_records,
            unit_records=[],
        )

        self.assertEqual(result["scope_mode"], "compare")
        self.assertEqual(result["compare_resolution_status"], "unresolved")
        self.assertEqual(result["declared_compare_expected_count"], 2)
        self.assertEqual(result["declared_compare_missing_count"], 1)
        self.assertEqual(result["declared_compare_source_ids"], ["source-001"])
        self.assertEqual(
            [item["requested_source_text"] for item in result["declared_compare_sources"]],
            ["Project Planning Brief", "Zebra Ledger"],
        )
        self.assertEqual(
            [item["source_match_status"] for item in result["declared_compare_sources"]],
            ["exact", "unresolved"],
        )
        self.assertEqual(
            [item["resolved_source_id"] for item in result["declared_compare_sources"]],
            ["source-001", None],
        )
        self.assertEqual(
            result["declared_compare_sources"][0]["candidate_source_ids"],
            ["source-001"],
        )
        self.assertEqual(
            result["declared_compare_sources"][1]["candidate_source_ids"],
            [],
        )
        self.assertTrue(
            str(result["declared_compare_sources"][0]["target_source_ref"]).startswith(
                "Project Planning Brief"
            )
        )
        self.assertIsNone(result["declared_compare_sources"][1]["target_source_ref"])

    def test_compare_query_does_not_hard_filter_to_one_resolved_source(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_pdf_corpus(workspace)

        report = retrieve_knowledge(
            query="compare Project Planning Brief and Project Outline Companion",
            top=5,
            graph_hops=1,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(report.payload["filters"]["source_ids"]), 2)
        self.assertCountEqual(
            report.payload["filters"]["source_ids"],
            report.payload["reference_resolution"]["declared_compare_source_ids"],
        )
        self.assertFalse(report.payload["reference_resolution"]["source_narrowing_allowed"])
        self.assertGreaterEqual(len(report.payload["results"]), 2)

    def test_compare_query_with_document_hints_still_avoids_source_narrowing(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_pdf_corpus(workspace)

        report = retrieve_knowledge(
            query="compare Project Planning Brief pdf and Project Outline Companion docx",
            top=5,
            graph_hops=1,
            include_renders=False,
            paths=workspace,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(len(report.payload["filters"]["source_ids"]), 2)
        self.assertCountEqual(
            report.payload["filters"]["source_ids"],
            report.payload["reference_resolution"]["declared_compare_source_ids"],
        )
        self.assertFalse(report.payload["reference_resolution"]["source_narrowing_allowed"])
        self.assertGreaterEqual(len(report.payload["results"]), 2)

    def test_artifact_hint_query_does_not_over_narrow_to_soft_source_alias(self) -> None:
        retrieval_data = {
            "manifest": {"source_signature": "reference-resolution-test"},
            "source_records": [
                {
                    "source_id": "md-1",
                    "current_path": "original_doc/gcs/evaluation-cycle.md",
                    "document_type": "markdown",
                    "source_family": "corpus",
                    "title": "GCS Evaluation Cycle",
                    "summary_en": "A markdown landing page.",
                    "summary_source": "A markdown landing page.",
                    "searchable_text": "GCS Evaluation Cycle landing page",
                    "available_channels": ["text", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "trust_prior": {},
                },
                {
                    "source_id": "pdf-1",
                    "current_path": "original_doc/gcs/gcs-evaluation-cycle-2024.pdf",
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "title": "GCS Evaluation Cycle February 2024",
                    "summary_en": "Diagram-rich PDF guide.",
                    "summary_source": "Diagram-rich PDF guide.",
                    "searchable_text": "Diagram A illustrates the GCS Evaluation Cycle",
                    "available_channels": ["text", "render", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "trust_prior": {},
                },
            ],
            "unit_records": [
                {
                    "source_id": "md-1",
                    "current_path": "original_doc/gcs/evaluation-cycle.md",
                    "document_type": "markdown",
                    "source_family": "corpus",
                    "unit_id": "section-001",
                    "unit_type": "section",
                    "ordinal": 1,
                    "title": "GCS Evaluation Cycle",
                    "text": "Landing page overview",
                    "structure_summary": '{"heading": "GCS Evaluation Cycle"}',
                    "available_channels": ["text", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "locator_aliases": ["GCS Evaluation Cycle"],
                },
                {
                    "source_id": "pdf-1",
                    "current_path": "original_doc/gcs/gcs-evaluation-cycle-2024.pdf",
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "unit_id": "page-009",
                    "unit_type": "page",
                    "ordinal": 9,
                    "title": "Page 9",
                    "text": "Diagram A illustrates the GCS Evaluation Cycle",
                    "structure_summary": (
                        '{"ordinal": 9, "text_excerpt": '
                        '"Diagram A illustrates the GCS Evaluation Cycle"}'
                    ),
                    "available_channels": ["text", "render", "structure"],
                    "channel_descriptors": {},
                    "citation_density": 0,
                    "locator_aliases": ["Page 9", "Diagram A"],
                },
            ],
            "artifact_records": [
                {
                    "source_id": "pdf-1",
                    "current_path": "original_doc/gcs/gcs-evaluation-cycle-2024.pdf",
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "artifact_id": "page-009:chart-001",
                    "artifact_type": "chart",
                    "unit_id": "page-009",
                    "title": "Diagram A chart",
                    "artifact_path": "visual_layout/page-009.json",
                    "locator_aliases": ["Diagram A", "GCS Evaluation Cycle chart"],
                    "available_channels": ["text", "render", "structure"],
                    "render_references": ["renders/page-009.png"],
                    "render_page_span": {"start": 9, "end": 9},
                    "bbox": None,
                    "normalized_bbox": None,
                    "graph_promoted": True,
                    "visual_hints": [],
                    "linked_text": "Diagram A illustrates the GCS Evaluation Cycle",
                    "section_path": ["The GCS Evaluation Cycle"],
                    "caption_text": "Diagram A illustrates the GCS Evaluation Cycle",
                    "continuation_group_ids": [],
                    "procedure_hints": [],
                    "semantic_labels": [],
                    "semantic_confidence": None,
                    "derivation_mode": "deterministic",
                    "semantic_overlay_asset": None,
                    "searchable_text": "Diagram A illustrates the GCS Evaluation Cycle",
                }
            ],
            "graph_edges": [],
        }

        reference_resolution = resolve_reference_query(
            "Diagram A GCS evaluation cycle",
            source_records=retrieval_data["source_records"],
            unit_records=retrieval_data["unit_records"],
        )
        self.assertFalse(reference_resolution["source_narrowing_allowed"])
        filtered_source_ids = _effective_source_ids_from_reference([], reference_resolution)
        self.assertEqual(filtered_source_ids, [])

        result = run_retrieval_query(
            retrieval_data,
            query="Diagram A GCS evaluation cycle",
            top=2,
            graph_hops=0,
            document_types=[],
            source_ids=filtered_source_ids,
            include_renders=False,
            reference_resolution=reference_resolution,
        )

        self.assertEqual(result["results"][0]["source_id"], "pdf-1")
        self.assertTrue(result["results"][0]["matched_artifact_ids"])

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

        preview = sync_workspace(workspace)
        self.assertEqual(preview.payload["sync_status"], "awaiting-confirmation")
        published = sync_workspace(workspace, assume_yes=True)
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
                "structure_summary": '{"visible_text": ["Detail Workflow"], "ordinal": 3}',
            }
        ]

        resolution = resolve_reference_query(
            "Test Design Deck page 2 detail workflow",
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
                "current_path": "original_doc/design/Search Feature Design Notes.pptx",
                "document_type": "pptx",
                "source_family": "corpus",
                "title": "Search Feature Design Notes",
            },
            {
                "source_id": "deck-b",
                "current_path": "original_doc/design/Analytics Feature Design Notes.pptx",
                "document_type": "pptx",
                "source_family": "corpus",
                "title": "Analytics Feature Design Notes",
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
                "text": "Concept Workflow",
                "structure_summary": '{"visible_text": ["Concept Workflow"]}',
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
                "text": "Detail Workflow",
                "structure_summary": '{"visible_text": ["Detail Workflow"]}',
            },
        ]

        resolution = resolve_reference_query(
            "detail workflow page in the Design ppt",
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

    def test_xlsx_reverse_sheet_alias_narrows_retrieval_to_resolved_source(self) -> None:
        workbook_source_manifest = {
            "source_id": "workbook-1",
            "current_path": (
                "original_doc/reviews/round1/Evaluation Score Card_Round 1_Bravo.xlsx"
            ),
            "document_type": "xlsx",
            "source_family": "corpus",
            "title": "Evaluation Score Card Round 1 Bravo",
        }
        distractor_source_manifest = {
            "source_id": "other-book",
            "current_path": "original_doc/other/legend-deck.pdf",
            "document_type": "pdf",
            "source_family": "corpus",
            "title": "Legend Deck",
        }
        workbook_unit = {
            "source_id": "workbook-1",
            "current_path": workbook_source_manifest["current_path"],
            "document_type": "xlsx",
            "source_family": "corpus",
            "unit_id": "sheet-001",
            "unit_type": "sheet",
            "ordinal": 1,
            "title": "Notice",
            "sheet_name": "Notice",
            "text": "Legend ranging from 0 to 5.",
            "structure_summary": '{"sheet_name": "Notice"}',
        }
        distractor_unit = {
            "source_id": "other-book",
            "current_path": distractor_source_manifest["current_path"],
            "document_type": "pdf",
            "source_family": "corpus",
            "unit_id": "page-001",
            "unit_type": "page",
            "ordinal": 1,
            "title": "Legend Page",
            "text": "Red yellow green face icons.",
            "structure_summary": '{"ordinal": 1}',
        }
        source_records = [
            {
                **workbook_source_manifest,
                **build_source_reference_fields(
                    workbook_source_manifest,
                    title=str(workbook_source_manifest["title"]),
                ),
            },
            {
                **distractor_source_manifest,
                **build_source_reference_fields(
                    distractor_source_manifest,
                    title=str(distractor_source_manifest["title"]),
                ),
            },
        ]
        unit_records = [
            {
                **workbook_unit,
                **build_unit_reference_fields(
                    workbook_source_manifest,
                    workbook_unit,
                    structure_data={"sheet_name": "Notice"},
                    text_content=str(workbook_unit["text"]),
                ),
            },
            {
                **distractor_unit,
                **build_unit_reference_fields(
                    distractor_source_manifest,
                    distractor_unit,
                    structure_data={"ordinal": 1},
                    text_content=str(distractor_unit["text"]),
                ),
            },
        ]

        resolution = resolve_reference_query(
            (
                "In the Bravo evaluation score card notice sheet, what do the red, "
                "yellow, and green face icons mean?"
            ),
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(resolution["status"], "approximate")
        self.assertEqual(resolution["resolved_source_id"], "workbook-1")
        self.assertEqual(resolution["resolved_unit_id"], "sheet-001")
        self.assertEqual(resolution["unit_match_status"], "exact")

        filtered_source_ids = _effective_source_ids_from_reference([], resolution)
        self.assertEqual(filtered_source_ids, ["workbook-1"])

        result = run_retrieval_query(
            {
                "manifest": {"source_signature": "reference-resolution-xlsx-sheet"},
                "source_records": source_records,
                "unit_records": unit_records,
                "artifact_records": [],
                "graph_edges": [],
            },
            query=(
                "In the Bravo evaluation score card notice sheet, what do the red, "
                "yellow, and green face icons mean?"
            ),
            top=3,
            graph_hops=0,
            document_types=[],
            source_ids=filtered_source_ids,
            include_renders=False,
            reference_resolution=resolution,
        )
        self.assertEqual(result["results"][0]["source_id"], "workbook-1")

    def test_docx_heading_alias_is_best_effort(self) -> None:
        source_records = [
            {
                "source_id": "rfi-doc",
                "current_path": "original_doc/platform/Revision-Request-1.1.docx",
                "document_type": "docx",
                "source_family": "corpus",
                "title": "Revision Request 1.1",
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
                "text": "Background\nThe high-level project overview",
                "structure_summary": (
                    '{"blocks": [{"kind": "paragraph", "text": "Background"}, '
                    '{"kind": "paragraph", "text": "The high-level project overview"}]}'
                ),
            }
        ]

        resolution = resolve_reference_query(
            "Revision Request 1.1 background section",
            source_records=source_records,
            unit_records=unit_records,
        )

        self.assertEqual(resolution["resolved_source_id"], "rfi-doc")
        self.assertEqual(resolution["resolved_unit_id"], "section-001")
        self.assertEqual(resolution["status"], "approximate")
        self.assertTrue(resolution["notice_text"])

    def test_approximate_resolved_unit_is_reranked_first_within_source(self) -> None:
        rules_engine_path = (
            "original_doc/Process documents/Update Review Workflow.pdf"
        )
        rules_engine_searchable = "Update Review Workflow current state pain points"
        retrieval_data = {
            "manifest": {"source_signature": "reference-resolution-test"},
            "source_records": [
                {
                    "source_id": "deck-1",
                    "current_path": rules_engine_path,
                    "document_type": "pdf",
                    "source_family": "corpus",
                    "title": "Update Review Workflow",
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
                    "text": "Update Review Workflow introduction and overview",
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
                    "text": "Current State Pain Points for the review workflow",
                    "structure_summary": (
                        '{"ordinal": 11, "text_excerpt": "Current State Pain Points"}'
                    ),
                },
            ],
            "graph_edges": [],
        }

        result = run_retrieval_query(
            retrieval_data,
            query="current state pain points page in review workflow document",
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
            "Interaction Memory for Draft Project Review slide-by-slide authoring constraints"
        )
        interaction_summary = "A related interaction memory with stronger broad lexical overlap."
        interaction_searchable = (
            "Draft Project Review slide 35 slide-by-slide authoring constraints"
        )
        retrieval_data = {
            "manifest": {"source_signature": "reference-resolution-test"},
            "source_records": [
                {
                    "source_id": "deck-1",
                    "current_path": "original_doc/Draft_Project_Review.pptx",
                    "document_type": "pptx",
                    "source_family": "corpus",
                    "title": "Project Operations Review",
                    "summary_en": "Draft review deck for project coordination.",
                    "summary_source": "Draft review deck for project coordination.",
                    "searchable_text": "Draft Project Review slide 35 target deck",
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
                    "current_path": "original_doc/Draft_Project_Review.pptx",
                    "document_type": "pptx",
                    "source_family": "corpus",
                    "unit_id": "slide-035",
                    "unit_type": "slide",
                    "ordinal": 35,
                    "logical_ordinal": 35,
                    "render_ordinal": 32,
                    "title": "Slide 35",
                    "text": "Draft Project Review target slide detail",
                    "structure_summary": '{"ordinal": 35, "text_excerpt": "Review detail"}',
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
                    "text": "Draft Project Review slide-by-slide authoring constraints",
                    "structure_summary": (
                        '{"ordinal": 11, "text_excerpt": "slide-by-slide authoring constraints"}'
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
            query="Draft Project Review slide 35",
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
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        deck_source_id = source_ids["original_doc/a.pdf"]

        turn = prepare_ask_turn(
            workspace,
            question="Project Planning Brief page 2 visual detail",
            semantic_analysis={
                "question_class": "answer",
                "question_domain": "workspace-corpus",
                "route_reason": "Reference-resolution propagation test.",
            },
        )
        self.assertEqual(turn["reference_resolution"]["status"], "exact")
        self.assertEqual(turn["source_scope_policy"]["scope_mode"], "source-scoped-soft")
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
        self.assertEqual(
            trace_report.payload["source_scope_policy"]["target_source_id"],
            deck_source_id,
        )
        self.assertEqual(
            trace_report.payload["supporting_source_ids"],
            [deck_source_id],
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

    def test_commit_rejects_grounded_source_scope_without_target_support(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_pdf_corpus(workspace)
        target_source_id = source_ids["original_doc/a.pdf"]
        distractor_source_id = source_ids["original_doc/b.pdf"]

        turn = prepare_ask_turn(
            workspace,
            question=(
                "Using only the document 'Project Planning Brief', summarize the architecture "
                "strategy. Do not use any other source."
            ),
            semantic_analysis={
                "question_class": "answer",
                "question_domain": "workspace-corpus",
                "route_reason": "Wave 3 commit-guard test.",
            },
        )
        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The project outline connects the work plan to implementation.\n",
            encoding="utf-8",
        )
        trace_report = trace_knowledge(
            answer_file=turn["answer_file_path"],
            top=1,
            paths=workspace,
        )
        trace_path = workspace.retrieval_traces_dir / f"{trace_report.payload['trace_id']}.json"
        trace_payload = read_json(trace_path)
        trace_payload["answer_state"] = "grounded"
        trace_payload["kb_answer_state"] = "grounded"
        trace_payload["supporting_source_ids"] = [distractor_source_id]
        trace_payload["canonical_support_summary"] = {
            "scope_mode": "source-scoped-hard",
            "target_source_id": target_source_id,
            "target_source_ref": trace_payload["reference_resolution"]["target_source_ref"],
            "source_scope_satisfied": False,
            "support_layers_present": ["kb"],
            "supporting_source_ids": [distractor_source_id],
            "supporting_unit_ids": [],
            "supporting_artifact_ids": [],
            "segment_truth_counts": {
                "grounded": 1,
                "partially_grounded": 0,
                "unresolved": 0,
            },
            "mixed_support_explainable": True,
        }
        write_json(trace_path, trace_payload)

        with self.assertRaisesRegex(ValueError, "source scope"):
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                trace_ids=[trace_report.payload["trace_id"]],
                answer_file_path=turn["answer_file_path"],
                response_excerpt=(
                    "The project outline connects the work plan to implementation."
                ),
                status="answered",
            )

    def test_trace_cli_surface_remains_id_first(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["trace", "--source-ref", "Platform Review slide 35"])

    def test_cli_dispatches_hidden_ask_subcommand(self) -> None:
        with (
            mock.patch(
                "docmason.host_integration.run_hidden_ask_cli",
                return_value=0,
            ) as hidden_ask,
            mock.patch("sys.stdin", io.StringIO("")),
        ):
            result = docmason_main(["_ask"])

        hidden_ask.assert_called_once_with("")
        self.assertEqual(result, 0)

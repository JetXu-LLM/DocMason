"""Tests for the tiered text-source support."""

from __future__ import annotations

import base64
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import prepare_ask_turn
from docmason.cli import build_parser
from docmason.commands import doctor_workspace, status_workspace, sync_workspace
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import retrieve_corpus, trace_source
from docmason.text_sources import parse_text_source
from tests.support_ready_workspace import seed_self_contained_bootstrap_state

ROOT = Path(__file__).resolve().parents[1]


class SourceBuildTextTests(unittest.TestCase):
    """Cover text-source sync, retrieval, trace, and status contracts."""

    def semantic_analysis(
        self,
        *,
        question_class: str,
        question_domain: str,
    ) -> dict[str, object]:
        return {
            "question_class": question_class,
            "question_domain": question_domain,
            "route_reason": (
                "Test analysis classified the question as "
                f"{question_class}/{question_domain}."
            ),
            "needs_latest_workspace_state": False,
        }

    def test_retrieve_cli_accepts_text_document_types(self) -> None:
        parser = build_parser()
        parsed = parser.parse_args(["retrieve", "architecture", "--document-type", "markdown"])
        self.assertEqual(parsed.document_type, ["markdown"])
        parsed = parser.parse_args(["retrieve", "architecture", "--document-type", "csv"])
        self.assertEqual(parsed.document_type, ["csv"])

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
            prepared_at="2026-03-19T00:00:00Z",
        )

    def write_png(self, path: Path) -> None:
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1EAAAAASUVORK5CYII="
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def seed_text_sources(self, workspace: WorkspacePaths) -> None:
        self.write_png(workspace.source_dir / "assets" / "diagram.png")
        (workspace.source_dir / "architecture.md").write_text(
            "\n".join(
                [
                    "---",
                    "title: Architecture Spec",
                    "owner: Communications Team",
                    "---",
                    "",
                    "# Overview",
                    "",
                    (
                        "This section links to [notes](notes.txt) and embeds "
                        "![diagram](assets/diagram.png)."
                    ),
                    "",
                    "## Data Flow",
                    "",
                    "| Step | Detail |",
                    "| --- | --- |",
                    "| Ingest | Parse source text |",
                    "| Publish | Preserve evidence |",
                    "",
                    "```python",
                    "print('hello')",
                    "```",
                    "",
                    "```mermaid",
                    "graph TD",
                    "A-->B",
                    "```",
                    "",
                    '<Widget prop="value" />',
                    "",
                    "![external](https://example.com/remote.png)",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (workspace.source_dir / "notes.txt").write_text(
            "\n".join(
                [
                    "The first paragraph anchors the operating constraint.",
                    "Latency should remain under one second.",
                    "",
                    "The second paragraph captures fallback guidance.",
                    "If evidence is weak, the agent should continue honestly.",
                ]
            ),
            encoding="utf-8",
        )
        (workspace.source_dir / "metrics.csv").write_text(
            "\n".join(
                [
                    "\ufeffName,Revenue,Region",
                    "Alice,10,CN",
                    "Bob,20,JP",
                ]
            ),
            encoding="utf-8",
        )
        (workspace.source_dir / "config.yaml").write_text(
            "\n".join(
                [
                    "service:",
                    "  owner: platform",
                    "",
                    "limits:",
                    "  timeout: 30",
                ]
            ),
            encoding="utf-8",
        )
        (workspace.source_dir / "guide.tex").write_text(
            "\n".join(
                [
                    "\\section{Introduction}",
                    "This TeX note documents the fallback guidance.",
                    "",
                    "\\subsection{Constraints}",
                    "The workflow should remain automatic.",
                ]
            ),
            encoding="utf-8",
        )
        (workspace.source_dir / "component.mdx").write_text(
            "\n".join(
                [
                    "# MDX Example",
                    "",
                    "This MDX document preserves lightweight text support.",
                    "",
                    '<Component variant="hero" />',
                ]
            ),
            encoding="utf-8",
        )

    def seed_agent_outputs(self, source_dir: Path) -> None:
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        title = str(source_manifest.get("title") or Path(source_manifest["current_path"]).stem)
        summary = f"{title} is a seeded text-source fixture."
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
                    "text_en": f"{title} preserves published evidence units.",
                    "text_source": f"{title} preserves published evidence units.",
                    "citations": [{"unit_id": first_unit_id, "support": "key point"}],
                }
            ],
            "entities": [{"name": title, "type": "text fixture"}],
            "claims": [
                {
                    "statement_en": f"{title} participates in retrieval and trace.",
                    "statement_source": f"{title} participates in retrieval and trace.",
                    "citations": [{"unit_id": first_unit_id, "support": "claim"}],
                }
            ],
            "known_gaps": [],
            "ambiguities": [],
            "confidence": {
                "level": "medium",
                "notes_en": "Text source test fixture.",
                "notes_source": "Text source test fixture.",
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

    def publish_seeded_text_corpus(self, workspace: WorkspacePaths) -> dict[str, str]:
        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = {
            item["current_path"]: item["source_id"] for item in pending.payload["pending_sources"]
        }
        for source_id in source_ids.values():
            self.seed_agent_outputs(workspace.knowledge_base_staging_dir / "sources" / source_id)
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def test_sync_builds_tiered_text_sources_and_preserves_markdown_structure(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)

        pending = sync_workspace(workspace, autonomous=False)

        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        self.assertEqual(len(pending.payload["pending_sources"]), 6)
        source_ids = {
            item["current_path"]: item["source_id"] for item in pending.payload["pending_sources"]
        }

        markdown_dir = (
            workspace.knowledge_base_staging_dir
            / "sources"
            / source_ids["original_doc/architecture.md"]
        )
        markdown_manifest = read_json(markdown_dir / "source_manifest.json")
        markdown_evidence = read_json(markdown_dir / "evidence_manifest.json")
        data_flow_unit = next(
            unit for unit in markdown_evidence["units"] if unit.get("slug_anchor") == "data-flow"
        )
        self.assertEqual(markdown_manifest["document_type"], "markdown")
        self.assertEqual(markdown_manifest["support_tier"], "first-class")
        self.assertIn("media/001-diagram.png", markdown_evidence["embedded_media"])
        self.assertTrue(markdown_evidence["warnings"])
        self.assertEqual(data_flow_unit["slug_anchor"], "data-flow")
        structure = read_json(markdown_dir / data_flow_unit["structure_asset"])
        block_kinds = [block["kind"] for block in structure["blocks"]]
        self.assertIn("table", block_kinds)
        self.assertIn("code_fence", block_kinds)
        self.assertIn("mermaid", block_kinds)
        self.assertIn("raw_html_or_unsupported", block_kinds)

        csv_dir = (
            workspace.knowledge_base_staging_dir
            / "sources"
            / source_ids["original_doc/metrics.csv"]
        )
        csv_manifest = read_json(csv_dir / "source_manifest.json")
        csv_evidence = read_json(csv_dir / "evidence_manifest.json")
        self.assertEqual(csv_manifest["document_type"], "csv")
        self.assertEqual(csv_manifest["support_tier"], "lightweight-compatible")
        self.assertEqual(csv_evidence["units"][0]["unit_type"], "sheet")
        self.assertEqual(csv_evidence["units"][0]["header_names"], ["Name", "Revenue", "Region"])
        self.assertEqual(csv_evidence["units"][0]["row_count"], 2)

        mdx_dir = (
            workspace.knowledge_base_staging_dir
            / "sources"
            / source_ids["original_doc/component.mdx"]
        )
        mdx_manifest = read_json(mdx_dir / "source_manifest.json")
        mdx_evidence = read_json(mdx_dir / "evidence_manifest.json")
        self.assertEqual(mdx_manifest["support_tier"], "lightweight-compatible")
        self.assertTrue(mdx_evidence["warnings"])

    def test_parser_prefers_meaningful_titles_for_plaintext_yaml_and_tex(self) -> None:
        workspace = self.make_workspace()

        plain_path = workspace.source_dir / "Real Title.txt"
        plain_path.write_text(
            "\n".join(
                [
                    "PDF To Markdown Converter",
                    "",
                    "# Real Title",
                    "",
                    "```",
                    "Status IN PROGRESS",
                    "```",
                ]
            ),
            encoding="utf-8",
        )
        yaml_path = workspace.source_dir / "Project Launch.yaml"
        yaml_path.write_text(
            "\n".join(
                [
                    "---",
                    "document:",
                    "  metadata:",
                    "    owner: platform",
                    "",
                    "cover:",
                    '  title: "Project Launch"',
                ]
            ),
            encoding="utf-8",
        )
        tex_path = workspace.source_dir / "recommendation-system-fundamentals.tex"
        tex_path.write_text(
            "\n".join(
                [
                    "\\documentclass{ctexart}",
                    "",
                    "\\title{Recommendation Systems in Practice}",
                    "\\author{DocMason}",
                    "",
                    "\\section{Introduction}",
                ]
            ),
            encoding="utf-8",
        )

        plain = parse_text_source(plain_path, document_type="plaintext")
        yaml = parse_text_source(yaml_path, document_type="yaml")
        tex = parse_text_source(tex_path, document_type="tex")

        self.assertEqual(plain.source_title, "Real Title")
        self.assertEqual(plain.units[1].title, "Real Title")
        self.assertEqual(plain.units[2].title, "Status IN PROGRESS")
        self.assertEqual(yaml.source_title, "Project Launch")
        self.assertEqual(tex.source_title, "Recommendation Systems in Practice")

    def test_status_and_doctor_report_tiered_input_contract(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)

        status = status_workspace(workspace)
        doctor = doctor_workspace(workspace)

        self.assertEqual(status.payload["source_documents"]["tiers"]["office_pdf"]["total"], 0)
        self.assertEqual(
            status.payload["source_documents"]["tiers"]["first_class_text"]["total"], 2
        )
        self.assertEqual(
            status.payload["source_documents"]["tiers"]["lightweight_text"]["total"], 4
        )
        self.assertIn("markdown", doctor.payload["supported_inputs"])
        self.assertIn("txt", doctor.payload["supported_inputs"])
        self.assertEqual(
            doctor.payload["supported_input_tiers"]["lightweight_text"],
            ["mdx", "yaml", "yml", "tex", "csv", "tsv"],
        )

    def test_current_commands_degrade_honestly_when_runtime_overlay_json_is_invalid(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)
        self.publish_seeded_text_corpus(workspace)

        write_json(workspace.interaction_connector_manifest_path, {"connectors": []})
        write_json(
            workspace.interaction_overlay_manifest_path,
            {
                "generated_at": "2026-03-19T00:00:00Z",
                "pending_entry_count": 1,
                "source_count": 1,
                "unit_count": 1,
                "graph_edge_count": 0,
            },
        )
        workspace.interaction_overlay_source_records_path.parent.mkdir(parents=True, exist_ok=True)
        workspace.interaction_overlay_source_records_path.write_text("{\n", encoding="utf-8")
        write_json(workspace.interaction_overlay_unit_records_path, {"records": []})
        write_json(workspace.interaction_overlay_graph_edges_path, {"edges": []})
        write_json(workspace.interaction_overlay_source_provenance_path, {})
        write_json(workspace.interaction_overlay_unit_provenance_path, {})
        write_json(workspace.interaction_overlay_relation_index_path, {})
        write_json(workspace.interaction_overlay_knowledge_consumers_path, {})
        write_json(
            workspace.interaction_promotion_queue_path,
            {
                "generated_at": "2026-03-19T00:00:00Z",
                "pending_promotion_count": 1,
                "entries": [],
            },
        )
        write_json(
            workspace.interaction_reconciliation_state_path,
            {"last_reconciled_at": "2026-03-19T00:00:00Z"},
        )

        status = status_workspace(workspace)
        doctor = doctor_workspace(workspace)
        retrieval = retrieve_corpus(
            workspace,
            query="Architecture Spec data flow",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        turn = prepare_ask_turn(
            workspace,
            question="What does architecture.md say about data flow?",
            semantic_analysis=self.semantic_analysis(
                question_class="answer",
                question_domain="workspace-corpus",
            ),
        )

        self.assertEqual(status.payload["interaction_ingest"]["pending_promotion_count"], 1)
        self.assertTrue(status.payload["interaction_ingest"]["load_warnings"])
        interaction_check = next(
            check for check in doctor.payload["checks"] if check["name"] == "interaction-ingest"
        )
        self.assertEqual(interaction_check["status"], "degraded")
        self.assertIn("partially readable", interaction_check["detail"])
        self.assertTrue(retrieval["results"])
        self.assertEqual(retrieval["results"][0]["source_family"], "corpus")
        self.assertFalse(turn["auto_sync_triggered"])
        self.assertTrue(turn["interaction_sync_suggested"])
        self.assertIn(
            "Pending interaction-derived runtime state could not be read completely",
            turn["freshness_notice"],
        )
        self.assertEqual(turn["pending_interaction_count"], 1)

    def test_sync_restages_missing_pending_text_source_directories(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)

        pending = sync_workspace(workspace, autonomous=False)
        source_ids = {
            item["current_path"]: item["source_id"] for item in pending.payload["pending_sources"]
        }
        markdown_source_id = source_ids["original_doc/architecture.md"]
        shutil.rmtree(workspace.knowledge_base_staging_dir / "sources" / markdown_source_id)

        restaged = sync_workspace(workspace, autonomous=False)

        self.assertEqual(restaged.payload["sync_status"], "pending-synthesis")
        self.assertTrue(
            (workspace.knowledge_base_staging_dir / "sources" / markdown_source_id).exists()
        )
        self.assertIn(
            "original_doc/architecture.md",
            {item["current_path"] for item in restaged.payload["pending_sources"]},
        )

    def test_retrieve_resolves_markdown_headings_text_lines_and_csv_hints(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)
        self.publish_seeded_text_corpus(workspace)

        markdown = retrieve_corpus(
            workspace,
            query="architecture.md #data-flow",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertEqual(markdown["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(markdown["reference_resolution"]["unit_match_status"], "exact")
        self.assertEqual(markdown["results"][0]["document_type"], "markdown")
        self.assertEqual(markdown["results"][0]["matched_units"][0]["slug_anchor"], "data-flow")

        plain = retrieve_corpus(
            workspace,
            query="notes.txt line 2",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertEqual(plain["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(plain["reference_resolution"]["unit_match_status"], "exact")
        self.assertEqual(plain["results"][0]["document_type"], "plaintext")
        self.assertEqual(plain["results"][0]["matched_units"][0]["line_start"], 1)

        csv = retrieve_corpus(
            workspace,
            query="metrics.csv row 2 header Revenue",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertEqual(csv["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(csv["reference_resolution"]["unit_match_status"], "approximate")
        self.assertEqual(csv["results"][0]["support_tier"], "lightweight-compatible")
        self.assertEqual(
            csv["results"][0]["matched_units"][0]["header_names"], ["Name", "Revenue", "Region"]
        )

        invalid_row = retrieve_corpus(
            workspace,
            query="metrics.csv row 999",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertNotEqual(invalid_row["reference_resolution"]["unit_match_status"], "approximate")

    def test_ask_and_trace_surface_text_locator_metadata(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)
        source_ids = self.publish_seeded_text_corpus(workspace)
        markdown_source_id = source_ids["original_doc/architecture.md"]

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-text-source"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="In architecture.md #data-flow, what happens in the pipeline?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertEqual(turn["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(turn["reference_resolution"]["unit_match_status"], "exact")
        self.assertEqual(turn["reference_resolution_summary"], "exact-reference")

        trace = trace_source(
            workspace,
            source_id=markdown_source_id,
            unit_id="section-003",
            target="current",
        )
        self.assertEqual(trace["source"]["support_tier"], "first-class")
        self.assertEqual(trace["unit"]["slug_anchor"], "data-flow")
        self.assertGreaterEqual(trace["unit"]["line_start"], 1)
        self.assertGreaterEqual(trace["unit"]["line_end"], trace["unit"]["line_start"])

    def test_ask_auto_sync_recomputes_reference_resolution_against_fresh_current(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)
        for index in range(1, 4):
            (workspace.source_dir / f"extra-{index}.txt").write_text(
                f"Extra supporting note {index}.\n",
                encoding="utf-8",
            )
        source_ids = self.publish_seeded_text_corpus(workspace)
        markdown_source_id = source_ids["original_doc/architecture.md"]

        moved_dir = workspace.source_dir / "moved"
        moved_dir.mkdir()
        (workspace.source_dir / "architecture.md").rename(moved_dir / "architecture.md")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-text-move"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does moved/architecture.md say about data flow?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                ),
            )

        self.assertTrue(turn["auto_sync_triggered"])
        self.assertFalse(turn["knowledge_base_stale"])
        self.assertEqual(turn["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(turn["reference_resolution"]["resolved_source_id"], markdown_source_id)

    def test_warning_only_text_degradation_stays_valid_but_visible(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.seed_text_sources(workspace)
        source_ids = self.publish_seeded_text_corpus(workspace)
        architecture_source_id = source_ids["original_doc/architecture.md"]

        validation = read_json(workspace.knowledge_base_current_dir / "validation_report.json")
        architecture_report = next(
            report
            for report in validation["source_reports"]
            if report["current_path"] == "original_doc/architecture.md"
        )
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(architecture_report["warnings"])

        retrieval = retrieve_corpus(
            workspace,
            query="architecture.md #data-flow",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertTrue(retrieval["results"][0]["warnings"])
        self.assertTrue(retrieval["results"][0]["matched_units"][0]["warnings"])

        trace = trace_source(
            workspace,
            source_id=architecture_source_id,
            unit_id="section-003",
            target="current",
        )
        self.assertTrue(trace["source"]["warnings"])
        self.assertTrue(trace["unit"]["warnings"])


if __name__ == "__main__":
    unittest.main()

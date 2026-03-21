"""Tests for the DocMason Phase 4 incremental sync, retrieval, and trace workflow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docmason.commands import DEGRADED, READY, retrieve_knowledge, trace_knowledge
from docmason.knowledge import update_source_index
from docmason.project import WorkspacePaths, read_json, write_json


class PhaseFourRuntimeTests(unittest.TestCase):
    """Cover Phase 4 incremental maintenance, retrieval, and trace behavior."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        (root / "skills" / "canonical" / "workspace-bootstrap").mkdir(parents=True)
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
        (root / "skills" / "canonical" / "workspace-bootstrap" / "SKILL.md").write_text(
            "# Workspace Bootstrap\n",
            encoding="utf-8",
        )
        return WorkspacePaths(root=root)

    def mark_environment_ready(self, workspace: WorkspacePaths) -> None:
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
                "notes_en": "Phase 4 test fixture.",
                "notes_source": "Phase 4 test fixture.",
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
            title="Architecture Strategy Deck",
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
            title="Delivery Timeline Plan",
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
        result = sync_workspace(workspace)

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
                "The architecture strategy deck says DocMason already ships watch mode "
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

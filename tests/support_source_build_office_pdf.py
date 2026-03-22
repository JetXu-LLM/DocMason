"""Office, spreadsheet, and PDF source-build tests."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from docmason.commands import (
    ACTION_REQUIRED,
    DEGRADED,
    status_workspace,
    sync_workspace,
    validate_knowledge_base,
)
from docmason.coordination import workspace_lease
from docmason.knowledge import (
    build_docx_source,
    build_pptx_source,
    build_xlsx_source,
    sanitize_text,
)
from docmason.project import WorkspacePaths, read_json, write_json


class SourceBuildOfficePdfTests(unittest.TestCase):
    """Cover office, spreadsheet, and PDF build behavior."""

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

    def ready_probe(self, _workspace: WorkspacePaths) -> tuple[bool, str]:
        return True, "Editable install resolves to the workspace source tree."

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

    def create_pdf(self, path: Path) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=144, height=144)
        with path.open("wb") as handle:
            writer.write(handle)

    def create_pptx(self, path: Path) -> None:
        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "Test Deck"
        slide.placeholders[1].text = "Hello from Office source build."
        presentation.save(path)

    def create_pptx_with_hidden_slide(self, path: Path) -> None:
        from pptx import Presentation

        presentation = Presentation()
        for index in range(3):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = f"Slide {index + 1}"
            slide.placeholders[1].text = f"Body {index + 1}"
            if index == 1:
                slide._element.set("show", "0")
        presentation.save(path)

    def create_docx(self, path: Path) -> None:
        from docx import Document

        document = Document()
        document.add_heading("Legacy Doc", level=1)
        document.add_paragraph("Legacy Word content.")
        document.save(path)

    def create_xlsx(self, path: Path) -> None:
        from openpyxl import Workbook

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Sheet 1"
        worksheet["A1"] = "Metric"
        worksheet["B1"] = "Value"
        worksheet["A2"] = "Budget"
        worksheet["B2"] = 42
        workbook.save(path)

    def create_xlsx_with_table(self, path: Path) -> None:
        from openpyxl import Workbook
        from openpyxl.worksheet.table import Table, TableStyleInfo

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = "Sheet 1"
        worksheet["A1"] = "Metric"
        worksheet["B1"] = "Value"
        worksheet["A2"] = "Budget"
        worksheet["B2"] = 42
        table = Table(displayName="MetricsTable", ref="A1:B2")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2",
            showFirstColumn=False,
            showLastColumn=False,
            showRowStripes=True,
            showColumnStripes=False,
        )
        worksheet.add_table(table)
        workbook.save(path)

    def seed_agent_outputs(self, source_dir: Path) -> None:
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        knowledge = {
            "source_id": source_manifest["source_id"],
            "source_fingerprint": source_manifest["source_fingerprint"],
            "title": "Seeded knowledge object",
            "source_language": "en",
            "summary_en": "A short English summary for the staged source.",
            "summary_source": "A short English summary for the staged source.",
            "document_type": source_manifest["document_type"],
            "key_points": [
                {
                    "text_en": "The document contains one staged evidence unit.",
                    "text_source": "The document contains one staged evidence unit.",
                    "citations": [{"unit_id": first_unit_id, "support": "primary"}],
                }
            ],
            "entities": [],
            "claims": [
                {
                    "statement_en": "The sync pipeline produced evidence for the source.",
                    "statement_source": "The sync pipeline produced evidence for the source.",
                    "citations": [{"unit_id": first_unit_id, "support": "primary"}],
                }
            ],
            "known_gaps": [],
            "ambiguities": [],
            "confidence": {
                "level": "medium",
                "notes_en": "Based on a minimal test fixture.",
                "notes_source": "Based on a minimal test fixture.",
            },
            "citations": [{"unit_id": first_unit_id, "support": "primary"}],
            "related_sources": [],
        }
        write_json(source_dir / "knowledge.json", knowledge)
        summary = "\n".join(
            [
                "# Seeded knowledge object",
                "",
                f"Source ID: {source_manifest['source_id']}",
                "",
                "## English Summary",
                "A short English summary for the staged source.",
                "",
                "## Source-Language Summary",
                "A short English summary for the staged source.",
                "",
            ]
        )
        (source_dir / "summary.md").write_text(summary, encoding="utf-8")

    def test_sync_pdf_only_creates_staging_and_pending_synthesis(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")

        report = sync_workspace(workspace, autonomous=False)

        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.payload["status"], DEGRADED)
        self.assertEqual(report.payload["sync_status"], "pending-synthesis")
        self.assertTrue(workspace.staging_catalog_path.exists())
        self.assertTrue(workspace.source_index_path.exists())
        self.assertEqual(len(report.payload["pending_sources"]), 1)
        evidence_manifest = read_json(
            workspace.knowledge_base_staging_dir
            / "sources"
            / report.payload["pending_sources"][0]["source_id"]
            / "evidence_manifest.json"
        )
        affordances = read_json(
            workspace.knowledge_base_staging_dir
            / "sources"
            / report.payload["pending_sources"][0]["source_id"]
            / "derived_affordances.json"
        )
        self.assertEqual(evidence_manifest["document_type"], "pdf")
        self.assertTrue(evidence_manifest["units"])
        self.assertEqual(affordances["artifact_type"], "derived-affordances")
        self.assertTrue(affordances["source_affordances"]["available_channels"])

    def test_sync_requires_office_renderer_for_office_sources(self) -> None:
        workspace = self.make_workspace()
        self.create_pptx(workspace.source_dir / "deck.pptx")

        with mock.patch(
            "docmason.knowledge.validate_soffice_binary",
            return_value={
                "ready": False,
                "binary": None,
                "version": None,
                "detail": "No LibreOffice command candidate was detected.",
            },
        ):
            report = sync_workspace(workspace)

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["sync_status"], "action-required")

    def test_hidden_pptx_slides_do_not_consume_visible_render_slots(self) -> None:
        workspace = self.make_workspace()
        source_path = workspace.source_dir / "hidden-deck.pptx"
        source_dir = workspace.knowledge_base_staging_dir / "sources" / "source-1"
        source_dir.mkdir(parents=True, exist_ok=True)
        self.create_pptx_with_hidden_slide(source_path)

        with (
            mock.patch(
                "docmason.knowledge.convert_office_to_pdf",
                return_value=(Path("dummy.pdf"), []),
            ),
            mock.patch(
                "docmason.knowledge.render_pdf_document",
                return_value=(["renders/page-001.png", "renders/page-002.png"], []),
            ),
        ):
            _source_manifest, evidence_manifest = build_pptx_source(
                workspace,
                source_path,
                {
                    "source_id": "source-1",
                    "source_fingerprint": "fingerprint-1",
                    "prior_paths": [],
                    "document_type": "pptx",
                    "first_seen_at": "2026-03-16T00:00:00Z",
                    "last_seen_at": "2026-03-16T00:00:00Z",
                    "identity_confidence": "new",
                },
                source_dir,
                soffice_binary="soffice",
            )

        self.assertEqual(evidence_manifest["units"][0]["rendered_asset"], "renders/page-001.png")
        self.assertTrue(evidence_manifest["units"][1]["hidden"])
        self.assertIsNone(evidence_manifest["units"][1]["rendered_asset"])
        self.assertEqual(evidence_manifest["units"][2]["rendered_asset"], "renders/page-002.png")

    def test_legacy_ppt_reuses_pptx_builder_via_libreoffice_normalization(self) -> None:
        workspace = self.make_workspace()
        source_path = workspace.source_dir / "legacy-deck.ppt"
        normalized_path = workspace.source_dir / "normalized-deck.pptx"
        source_dir = workspace.knowledge_base_staging_dir / "sources" / "source-ppt"
        source_dir.mkdir(parents=True, exist_ok=True)
        self.create_pptx(normalized_path)
        source_path.write_bytes(normalized_path.read_bytes())

        with (
            mock.patch(
                "docmason.knowledge.normalize_legacy_office_source",
                return_value=(normalized_path, []),
            ),
            mock.patch(
                "docmason.knowledge.convert_office_to_pdf",
                return_value=(Path("dummy.pdf"), []),
            ),
            mock.patch(
                "docmason.knowledge.render_pdf_document",
                return_value=(["renders/page-001.png"], []),
            ),
        ):
            _source_manifest, evidence_manifest = build_pptx_source(
                workspace,
                source_path,
                {
                    "source_id": "source-ppt",
                    "source_fingerprint": "fingerprint-ppt",
                    "prior_paths": [],
                    "document_type": "pptx",
                    "source_extension": "ppt",
                    "first_seen_at": "2026-03-19T00:00:00Z",
                    "last_seen_at": "2026-03-19T00:00:00Z",
                    "identity_confidence": "new",
                },
                source_dir,
                soffice_binary="soffice",
            )

        self.assertEqual(evidence_manifest["document_type"], "pptx")
        self.assertTrue(evidence_manifest["units"])

    def test_legacy_doc_reuses_docx_builder_via_libreoffice_normalization(self) -> None:
        workspace = self.make_workspace()
        source_path = workspace.source_dir / "legacy-doc.doc"
        normalized_path = workspace.source_dir / "normalized-doc.docx"
        source_dir = workspace.knowledge_base_staging_dir / "sources" / "source-doc"
        source_dir.mkdir(parents=True, exist_ok=True)
        self.create_docx(normalized_path)
        source_path.write_bytes(normalized_path.read_bytes())

        with (
            mock.patch(
                "docmason.knowledge.normalize_legacy_office_source",
                return_value=(normalized_path, []),
            ),
            mock.patch(
                "docmason.knowledge.convert_office_to_pdf",
                return_value=(Path("dummy.pdf"), []),
            ),
            mock.patch(
                "docmason.knowledge.render_pdf_document",
                return_value=(["renders/page-001.png"], []),
            ),
        ):
            _source_manifest, evidence_manifest = build_docx_source(
                workspace,
                source_path,
                {
                    "source_id": "source-doc",
                    "source_fingerprint": "fingerprint-doc",
                    "prior_paths": [],
                    "document_type": "docx",
                    "source_extension": "doc",
                    "first_seen_at": "2026-03-19T00:00:00Z",
                    "last_seen_at": "2026-03-19T00:00:00Z",
                    "identity_confidence": "new",
                },
                source_dir,
                soffice_binary="soffice",
            )

        self.assertEqual(evidence_manifest["document_type"], "docx")
        self.assertTrue(evidence_manifest["units"])

    def test_legacy_xls_reuses_xlsx_builder_via_libreoffice_normalization(self) -> None:
        workspace = self.make_workspace()
        source_path = workspace.source_dir / "legacy-sheet.xls"
        normalized_path = workspace.source_dir / "normalized-sheet.xlsx"
        source_dir = workspace.knowledge_base_staging_dir / "sources" / "source-xls"
        source_dir.mkdir(parents=True, exist_ok=True)
        self.create_xlsx(normalized_path)
        source_path.write_bytes(normalized_path.read_bytes())

        with (
            mock.patch(
                "docmason.knowledge.normalize_legacy_office_source",
                return_value=(normalized_path, []),
            ),
            mock.patch(
                "docmason.knowledge.convert_office_to_pdf",
                return_value=(Path("dummy.pdf"), []),
            ),
            mock.patch(
                "docmason.knowledge.render_pdf_document",
                return_value=(["renders/page-001.png"], []),
            ),
        ):
            _source_manifest, evidence_manifest = build_xlsx_source(
                workspace,
                source_path,
                {
                    "source_id": "source-xls",
                    "source_fingerprint": "fingerprint-xls",
                    "prior_paths": [],
                    "document_type": "xlsx",
                    "source_extension": "xls",
                    "first_seen_at": "2026-03-19T00:00:00Z",
                    "last_seen_at": "2026-03-19T00:00:00Z",
                    "identity_confidence": "new",
                },
                source_dir,
                soffice_binary="soffice",
            )

        self.assertEqual(evidence_manifest["document_type"], "xlsx")
        self.assertTrue(evidence_manifest["units"])

    def test_build_xlsx_source_reads_table_refs_across_openpyxl_variants(self) -> None:
        workspace = self.make_workspace()
        source_path = workspace.source_dir / "table-sheet.xlsx"
        source_dir = workspace.knowledge_base_staging_dir / "sources" / "source-table"
        source_dir.mkdir(parents=True, exist_ok=True)
        self.create_xlsx_with_table(source_path)

        with (
            mock.patch(
                "docmason.knowledge.convert_office_to_pdf",
                return_value=(Path("dummy.pdf"), []),
            ),
            mock.patch(
                "docmason.knowledge.render_pdf_document",
                return_value=(["renders/page-001.png"], []),
            ),
        ):
            _source_manifest, evidence_manifest = build_xlsx_source(
                workspace,
                source_path,
                {
                    "source_id": "source-table",
                    "source_fingerprint": "fingerprint-table",
                    "prior_paths": [],
                    "document_type": "xlsx",
                    "first_seen_at": "2026-03-20T00:00:00Z",
                    "last_seen_at": "2026-03-20T00:00:00Z",
                    "identity_confidence": "new",
                },
                source_dir,
                soffice_binary="soffice",
            )

        structure_path = source_dir / evidence_manifest["units"][0]["structure_asset"]
        structure = read_json(structure_path)
        self.assertEqual(structure["tables"], [{"name": "MetricsTable", "ref": "A1:B2"}])

    def test_sanitize_text_replaces_unpaired_surrogates(self) -> None:
        self.assertEqual(sanitize_text("ok\ud835bad"), "ok?bad")

    def test_status_reports_invalid_stage_after_pending_sync(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "example.pdf")

        report = sync_workspace(workspace, autonomous=False)
        self.assertEqual(report.payload["sync_status"], "pending-synthesis")

        status = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(status.payload["stage"], "knowledge-base-invalid")
        self.assertEqual(status.payload["knowledge_base"]["validation_status"], "pending-synthesis")

    def test_validate_and_publish_after_seeded_agent_outputs(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "example.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        source_id = pending.payload["pending_sources"][0]["source_id"]
        source_dir = workspace.knowledge_base_staging_dir / "sources" / source_id
        self.seed_agent_outputs(source_dir)

        validation = validate_knowledge_base(workspace, target="staging")
        self.assertEqual(validation.exit_code, 0)
        self.assertEqual(validation.payload["validation_status"], "valid")

        published = sync_workspace(workspace)
        self.assertEqual(published.exit_code, 0)
        self.assertEqual(published.payload["sync_status"], "valid")
        self.assertTrue(workspace.current_publish_manifest_path.exists())
        self.assertTrue((workspace.knowledge_base_dir / "current-pointer.json").exists())
        self.assertTrue(workspace.knowledge_base_current_dir.is_symlink())
        self.assertTrue(
            (
                workspace.knowledge_base_current_dir / "sources" / source_id / "knowledge.json"
            ).exists()
        )
        self.assertTrue(
            (
                workspace.knowledge_base_current_dir
                / "sources"
                / source_id
                / "derived_affordances.json"
            ).exists()
        )
        current_publish_manifest = read_json(workspace.current_publish_manifest_path)
        current_validation = read_json(workspace.current_validation_report_path)
        self.assertIsInstance(current_publish_manifest.get("snapshot_id"), str)
        self.assertFalse(str(current_publish_manifest.get("snapshot_id")).startswith("unknown-"))
        self.assertEqual(
            current_validation.get("source_signature"),
            current_publish_manifest.get("source_signature"),
        )

        status = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(status.payload["stage"], "knowledge-base-present")

    def test_validate_rejects_placeholder_knowledge(self) -> None:
        workspace = self.make_workspace()
        self.create_pdf(workspace.source_dir / "example.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        source_id = pending.payload["pending_sources"][0]["source_id"]
        source_dir = workspace.knowledge_base_staging_dir / "sources" / source_id
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        write_json(
            source_dir / "knowledge.json",
            {
                "source_id": source_manifest["source_id"],
                "source_fingerprint": source_manifest["source_fingerprint"],
                "title": "TODO",
                "source_language": "en",
                "summary_en": "TODO",
                "summary_source": "TODO",
                "document_type": source_manifest["document_type"],
                "key_points": [],
                "entities": [],
                "claims": [{"citations": [{"unit_id": first_unit_id, "support": "primary"}]}],
                "known_gaps": [],
                "ambiguities": [],
                "confidence": {"level": "low"},
                "citations": [{"unit_id": first_unit_id, "support": "primary"}],
                "related_sources": [],
            },
        )
        (source_dir / "summary.md").write_text(
            "\n".join(
                [
                    "# TODO",
                    "",
                    f"Source ID: {source_manifest['source_id']}",
                    "",
                    "## English Summary",
                    "TODO",
                    "",
                    "## Source-Language Summary",
                    "TODO",
                    "",
                ]
            ),
            encoding="utf-8",
        )

        validation = validate_knowledge_base(workspace, target="staging")

        self.assertEqual(validation.exit_code, 1)
        self.assertEqual(validation.payload["status"], ACTION_REQUIRED)
        self.assertEqual(validation.payload["validation_status"], "blocking-errors")
        report = json.dumps(validation.payload["validation"], sort_keys=True)
        self.assertIn("placeholder", report.lower())

    def test_sync_waits_for_sync_lease_before_mutating_shared_state(self) -> None:
        workspace = self.make_workspace()
        result: dict[str, object] = {}
        finished = threading.Event()

        def run_sync() -> None:
            result["payload"] = sync_workspace(workspace, autonomous=False)
            finished.set()

        with workspace_lease(workspace, "sync", timeout_seconds=1.0):
            thread = threading.Thread(target=run_sync)
            thread.start()
            time.sleep(0.2)
            self.assertFalse(
                finished.is_set(),
                "Sync should wait while another sync lease is active.",
            )
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        payload = result["payload"]
        self.assertEqual(payload.payload["sync_status"], "valid")


if __name__ == "__main__":
    unittest.main()

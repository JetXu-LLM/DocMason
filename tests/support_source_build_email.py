"""Tests for first-class `.eml` sources with derived attachment sources."""

from __future__ import annotations

import base64
import shutil
import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path

from docmason.commands import doctor_workspace, status_workspace, sync_workspace
from docmason.email_sources import parse_email_source
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import retrieve_corpus, trace_source
from tests.support_ready_workspace import seed_self_contained_bootstrap_state

ROOT = Path(__file__).resolve().parents[1]


def tiny_png_bytes() -> bytes:
    """Return a tiny valid PNG fixture for inline-image tests."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1EAAAAASUVORK5CYII="
    )


class SourceBuildEmailTests(unittest.TestCase):
    """Cover `.eml` staging, derived attachments, and runtime contracts."""

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

    def make_email(
        self,
        *,
        subject: str,
        plain_body: str,
        html_body: str | None = None,
        inline_image_cid: str | None = None,
        attachments: list[tuple[bytes, str, str, str]] | None = None,
    ) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = "DocMason <docmason@example.com>"
        msg["To"] = "Operator <operator@example.com>"
        msg["Date"] = "Fri, 19 Mar 2026 09:00:00 +0000"
        msg["Message-ID"] = f"<{subject.lower().replace(' ', '-') or 'mail'}@example.com>"
        msg.set_content(plain_body)
        if html_body is not None:
            msg.add_alternative(html_body, subtype="html")
            if inline_image_cid is not None:
                html_part = msg.get_body(preferencelist=("html",))
                self.assertIsNotNone(html_part)
                html_part.add_related(
                    tiny_png_bytes(),
                    maintype="image",
                    subtype="png",
                    cid=f"<{inline_image_cid}>",
                    filename="chart.png",
                )
        for payload, maintype, subtype, filename in attachments or []:
            msg.add_attachment(payload, maintype=maintype, subtype=subtype, filename=filename)
        return msg

    def write_email_fixture(
        self,
        path: Path,
        *,
        plain_body: str | None = None,
        html_body: str | None = None,
    ) -> None:
        nested = self.make_email(
            subject="Forwarded Budget Email",
            plain_body="Budget line one.\n\nBudget line two.",
            attachments=[(b"Metric,Value\nBudget,42\n", "text", "csv", "budget.csv")],
        )
        root = self.make_email(
            subject="Delivery Kickoff Email",
            plain_body=plain_body
            or (
                "Roadmap decisions are tracked in the attached note.\n"
                "Please review the forwarded budget mail as well."
            ),
            html_body=html_body
            or (
                "<p>Roadmap decisions are tracked in the attached note.</p>"
                '<p><img src="cid:chart-1" /></p>'
            ),
            inline_image_cid="chart-1",
            attachments=[
                (
                    b"Roadmap note line 1.\nRoadmap note line 2.\n",
                    "text",
                    "plain",
                    "roadmap-notes.txt",
                ),
                (b"\x00\x01\x02", "application", "octet-stream", "raw.bin"),
                (nested.as_bytes(policy=policy.default), "message", "rfc822", "forwarded.eml"),
            ],
        )
        path.write_bytes(root.as_bytes(policy=policy.default))

    def write_depth_limit_fixture(self, path: Path) -> None:
        level_three = self.make_email(
            subject="Too Deep Email",
            plain_body="This level should stay raw-only.",
        )
        level_two = self.make_email(
            subject="Second Level Email",
            plain_body="Second level body.",
            attachments=[
                (
                    level_three.as_bytes(policy=policy.default),
                    "message",
                    "rfc822",
                    "level-three.eml",
                )
            ],
        )
        level_one = self.make_email(
            subject="First Level Email",
            plain_body="First level body.",
            attachments=[
                (
                    level_two.as_bytes(policy=policy.default),
                    "message",
                    "rfc822",
                    "level-two.eml",
                )
            ],
        )
        root = self.make_email(
            subject="Depth Root Email",
            plain_body="Root body.",
            attachments=[
                (
                    level_one.as_bytes(policy=policy.default),
                    "message",
                    "rfc822",
                    "level-one.eml",
                )
            ],
        )
        path.write_bytes(root.as_bytes(policy=policy.default))

    def seed_agent_outputs(self, source_dir: Path) -> None:
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        title = str(source_manifest.get("title") or Path(source_manifest["current_path"]).stem)
        summary = f"{title} is a seeded email-source fixture."
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
            "entities": [{"name": title, "type": "email fixture"}],
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
                "notes_en": "Email source test fixture.",
                "notes_source": "Email source test fixture.",
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

    def publish_seeded_sources(self, workspace: WorkspacePaths) -> dict[str, str]:
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

    def test_parse_email_source_preserves_headers_body_html_and_attachment_inventory(self) -> None:
        workspace = self.make_workspace()
        source_path = workspace.source_dir / "delivery.eml"
        self.write_email_fixture(source_path)

        parsed = parse_email_source(source_path)

        self.assertEqual(parsed.document_type, "email")
        self.assertEqual(parsed.source_title, "Delivery Kickoff Email")
        self.assertTrue(parsed.html_body)
        self.assertEqual(len(parsed.attachments), 4)
        self.assertEqual(parsed.attachments[1].filename, "roadmap-notes.txt")
        self.assertEqual(parsed.attachments[1].document_type, "plaintext")
        self.assertEqual(parsed.attachments[3].document_type, "email")
        self.assertIn("cid_references", parsed.mime_structure)
        self.assertIn("header-001", {unit.unit_id for unit in parsed.units})
        self.assertIn("email-section", {unit.unit_type for unit in parsed.units})
        self.assertIn("email-attachment", {unit.unit_type for unit in parsed.units})

    def test_status_and_doctor_include_first_class_email_input_tier(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.write_email_fixture(workspace.source_dir / "delivery.eml")

        status = status_workspace(workspace)
        doctor = doctor_workspace(workspace)

        self.assertEqual(
            status.payload["source_documents"]["tiers"]["first_class_email"]["total"], 1
        )
        self.assertEqual(
            doctor.payload["supported_input_tiers"]["first_class_email"],
            ["eml"],
        )
        self.assertIn("eml", doctor.payload["supported_inputs"])

    def test_sync_builds_email_parent_and_derived_attachments_with_graph_and_trace(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.write_email_fixture(workspace.source_dir / "delivery.eml")

        source_ids = self.publish_seeded_sources(workspace)

        parent_source_id = source_ids["original_doc/delivery.eml"]
        child_paths = {path for path in source_ids if path != "original_doc/delivery.eml"}
        self.assertIn("original_doc/delivery.eml#attachment/002-roadmap-notes.txt", child_paths)
        self.assertIn("original_doc/delivery.eml#attachment/004-forwarded.eml", child_paths)
        self.assertIn(
            "original_doc/delivery.eml#attachment/004-forwarded.eml#attachment/001-budget.csv",
            child_paths,
        )

        catalog = read_json(workspace.current_catalog_path)
        parent_catalog = next(
            source for source in catalog["sources"] if source["source_id"] == parent_source_id
        )
        self.assertEqual(parent_catalog["source_origin"], "original-document")
        child_catalog = next(
            source
            for source in catalog["sources"]
            if source["current_path"]
            == "original_doc/delivery.eml#attachment/002-roadmap-notes.txt"
        )
        self.assertEqual(child_catalog["source_origin"], "derived-attachment")
        self.assertEqual(child_catalog["parent_source_id"], parent_source_id)

        parent_dir = workspace.knowledge_base_current_dir / "sources" / parent_source_id
        parent_manifest = read_json(parent_dir / "source_manifest.json")
        parent_evidence = read_json(parent_dir / "evidence_manifest.json")
        self.assertEqual(parent_manifest["document_type"], "email")
        self.assertTrue(parent_manifest["child_source_ids"])
        self.assertTrue(parent_manifest["published_attachment_assets"])
        self.assertEqual(
            parent_evidence["attachments"][1]["child_source_id"], child_catalog["source_id"]
        )
        self.assertIn("media/001-chart.png", parent_evidence["embedded_media"])
        self.assertIn("attachments/003-raw.bin", parent_manifest["published_attachment_assets"])
        self.assertTrue(parent_evidence["deterministic_linked_sources"])

        graph_edges = read_json(workspace.current_graph_edges_path)["edges"]
        self.assertTrue(
            any(
                edge["source_id"] == parent_source_id
                and edge["related_source_id"] == child_catalog["source_id"]
                and edge["relation_type"] == "email-attachment"
                for edge in graph_edges
            )
        )

        retrieval = retrieve_corpus(
            workspace,
            query="delivery.eml attachment roadmap-notes.txt",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertEqual(retrieval["reference_resolution"]["source_match_status"], "exact")
        self.assertEqual(retrieval["results"][0]["document_type"], "email")
        self.assertEqual(
            retrieval["results"][0]["matched_units"][0]["unit_type"], "email-attachment"
        )
        self.assertEqual(
            retrieval["results"][0]["matched_units"][0]["child_source_id"],
            child_catalog["source_id"],
        )

        trace = trace_source(
            workspace,
            source_id=child_catalog["source_id"],
            unit_id=None,
            target="current",
        )
        self.assertEqual(trace["source"]["source_origin"], "derived-attachment")
        self.assertEqual(trace["source"]["parent_source_id"], parent_source_id)
        self.assertEqual(trace["source"]["root_email_source_id"], parent_source_id)

    def test_nested_eml_depth_limit_warns_without_blocking_publish(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.write_depth_limit_fixture(workspace.source_dir / "depth.eml")

        source_ids = self.publish_seeded_sources(workspace)

        self.assertIn("original_doc/depth.eml", source_ids)
        self.assertIn("original_doc/depth.eml#attachment/001-level-one.eml", source_ids)
        self.assertIn(
            "original_doc/depth.eml#attachment/001-level-one.eml#attachment/001-level-two.eml",
            source_ids,
        )
        self.assertNotIn(
            "original_doc/depth.eml#attachment/001-level-one.eml#attachment/001-level-two.eml#attachment/001-level-three.eml",
            source_ids,
        )

        level_two_source_id = source_ids[
            "original_doc/depth.eml#attachment/001-level-one.eml#attachment/001-level-two.eml"
        ]
        level_two_dir = workspace.knowledge_base_current_dir / "sources" / level_two_source_id
        level_two_evidence = read_json(level_two_dir / "evidence_manifest.json")
        warnings = "\n".join(level_two_evidence["warnings"])
        self.assertIn("Nested `.eml` depth exceeded", warnings)

        validation = read_json(workspace.current_validation_report_path)
        self.assertEqual(validation["status"], "valid")

    def test_sync_rebuilds_email_tree_when_previous_child_artifact_contract_is_broken(
        self,
    ) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        source_path = workspace.source_dir / "delivery.eml"
        self.write_email_fixture(source_path)

        source_ids = self.publish_seeded_sources(workspace)
        child_source_id = source_ids["original_doc/delivery.eml#attachment/002-roadmap-notes.txt"]
        broken_child_dir = workspace.knowledge_base_current_dir / "sources" / child_source_id
        (broken_child_dir / "artifact_index.json").unlink()
        if workspace.knowledge_base_staging_dir.exists():
            shutil.rmtree(workspace.knowledge_base_staging_dir)

        rebuilt = sync_workspace(workspace, autonomous=False)

        self.assertEqual(rebuilt.payload["sync_status"], "valid")
        self.assertEqual(rebuilt.payload["build_stats"]["rebuilt_sources"], 1)
        self.assertTrue(
            (
                workspace.knowledge_base_current_dir
                / "sources"
                / child_source_id
                / "artifact_index.json"
            ).exists()
        )

    def test_sync_rebuilds_unchanged_email_child_when_parent_changes_and_previous_child_is_broken(
        self,
    ) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        source_path = workspace.source_dir / "delivery.eml"
        self.write_email_fixture(source_path)

        source_ids = self.publish_seeded_sources(workspace)
        child_source_id = source_ids["original_doc/delivery.eml#attachment/002-roadmap-notes.txt"]
        broken_child_dir = workspace.knowledge_base_current_dir / "sources" / child_source_id
        (broken_child_dir / "artifact_index.json").unlink()
        self.write_email_fixture(
            source_path,
            plain_body=(
                "Updated roadmap commentary stays in the email body.\n"
                "The attachments are unchanged and should keep the same derived source IDs."
            ),
            html_body=(
                "<p>Updated roadmap commentary stays in the email body.</p>"
                '<p><img src="cid:chart-1" /></p>'
            ),
        )
        if workspace.knowledge_base_staging_dir.exists():
            shutil.rmtree(workspace.knowledge_base_staging_dir)

        pending = sync_workspace(workspace, autonomous=False)

        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        self.assertEqual(pending.payload["build_stats"]["rebuilt_sources"], 1)
        staged_child_dir = workspace.knowledge_base_staging_dir / "sources" / child_source_id
        self.assertTrue((staged_child_dir / "artifact_index.json").exists())
        staged_child_manifest = read_json(staged_child_dir / "source_manifest.json")
        self.assertEqual(
            staged_child_manifest["current_path"],
            "original_doc/delivery.eml#attachment/002-roadmap-notes.txt",
        )

        for pending_source in pending.payload["pending_sources"]:
            self.seed_agent_outputs(
                workspace.knowledge_base_staging_dir / "sources" / pending_source["source_id"]
            )
        published = sync_workspace(workspace)

        self.assertEqual(published.payload["sync_status"], "valid")
        self.assertTrue(
            (
                workspace.knowledge_base_current_dir
                / "sources"
                / child_source_id
                / "artifact_index.json"
            ).exists()
        )


if __name__ == "__main__":
    unittest.main()

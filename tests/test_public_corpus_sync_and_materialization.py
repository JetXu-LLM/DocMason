"""Public sample corpus boundary and materialization tests."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support_public_corpus import build_public_markdown_workspace

ROOT = Path(__file__).resolve().parents[1]


def _safe_python_executable() -> str:
    framework_python = (
        Path(sys.exec_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    )
    if framework_python.exists():
        return str(framework_python)
    return sys.executable


PYTHON = _safe_python_executable()


class PublicCorpusSyncAndMaterializationTests(unittest.TestCase):
    """Cover tracked public corpus integrity and workspace materialization."""

    def test_public_manifest_records_official_outputs(self) -> None:
        manifest = json.loads(
            (ROOT / "sample_corpus" / "ico-gcs" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["corpus_id"], "ico-gcs")
        self.assertEqual(manifest["license"]["name"], "Open Government Licence v3.0")
        source_ids = {entry["source_id"] for entry in manifest["sources"]}
        self.assertIn("ico-ai-risk-toolkit", source_ids)
        self.assertIn("gcs-oasis", source_ids)
        managed_paths = {
            output["local_path"]
            for entry in manifest["sources"]
            for output in entry["managed_outputs"]
        }
        self.assertIn(
            "ico/ico-ai-risk-toolkit--ai-and-data-protection-risk-toolkit-v11.xlsx", managed_paths
        )
        self.assertIn(
            "gcs/gcs-oasis--guide-to-campaign-planning-oasis-framework.pdf", managed_paths
        )
        self.assertIn(
            "gcs/gcs-modern-media-operations--modern-media-operation-guide-word-accessible.docx",
            managed_paths,
        )

    def test_sample_corpus_contains_supported_multiformat_public_inputs(self) -> None:
        corpus_root = ROOT / "sample_corpus" / "ico-gcs"
        expected = [
            corpus_root / "ico" / "about-the-ico.md",
            corpus_root
            / "ico"
            / "ico-ai-risk-toolkit--ai-and-data-protection-risk-toolkit-v11.xlsx",
            corpus_root / "gcs" / "gcs-oasis--oasis-template.pptx",
            corpus_root
            / "gcs"
            / "gcs-evaluation-cycle--2024-02-13-gcs-evaluation-cycle-final-official.pdf",
            corpus_root
            / "gcs"
            / "gcs-modern-media-operations--modern-media-operation-guide-word-accessible.docx",
        ]
        for path in expected:
            self.assertTrue(path.exists(), f"Expected public sample artifact to exist: {path}")

    def test_use_sample_corpus_materializes_public_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            target = tempdir / "original_doc"
            target.mkdir()
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "use-sample-corpus.py"),
                    "--repo-root",
                    str(ROOT),
                    "--target",
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((target / "ico" / "about-the-ico.md").exists())
            campaign_template_name = (
                "gcs-evaluation-cycle--2024-08-22-"
                "evaluation-cycle-campaign-template-off-sen.pptx"
            )
            self.assertTrue(
                (
                    target
                    / "gcs"
                    / campaign_template_name
                ).exists()
            )
            marker = json.loads((target / ".docmason-sample.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["preset"], "ico-gcs")

    def test_use_sample_corpus_refuses_to_overwrite_nonempty_workspace_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            target = tempdir / "original_doc"
            target.mkdir()
            (target / "private.txt").write_text("secret\n", encoding="utf-8")
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "use-sample-corpus.py"),
                    "--repo-root",
                    str(ROOT),
                    "--target",
                    str(target),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Refusing to overwrite", result.stderr)

    def test_public_markdown_subset_syncs_into_a_valid_temp_kb(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            workspace, sync_payload = build_public_markdown_workspace(Path(tempdir_name))
            self.assertEqual(sync_payload["sync_status"], "valid")
            self.assertTrue(workspace.current_catalog_path.exists())
            catalog = json.loads(workspace.current_catalog_path.read_text(encoding="utf-8"))
            current_paths = {item["current_path"] for item in catalog["sources"]}
            self.assertIn("original_doc/ico/about-the-ico.md", current_paths)
            self.assertIn("original_doc/gcs/oasis-campaign-planning.md", current_paths)


if __name__ == "__main__":
    unittest.main()

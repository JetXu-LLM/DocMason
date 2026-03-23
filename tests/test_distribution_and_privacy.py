"""Distribution, privacy, and local-update tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DistributionAndPrivacyTests(unittest.TestCase):
    """Cover the public sample corpus, privacy guardrails, and generated bundles."""

    def test_use_sample_corpus_materializes_public_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            target = tempdir / "original_doc"
            target.mkdir()
            result = subprocess.run(
                [
                    "python3",
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
            self.assertTrue((target / "gcs" / "oasis-campaign-planning.md").exists())
            marker = json.loads((target / ".docmason-sample.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["preset"], "ico-gcs")

    def test_build_distributions_outputs_clean_and_demo_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            result = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "build-distributions.py"),
                    "--repo-root",
                    str(ROOT),
                    "--output-dir",
                    str(tempdir),
                    "--version",
                    "test-build",
                    "--github-repo",
                    "example/DocMason",
                    "--source-commit",
                    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    "--source-ref",
                    "refs/tags/test-build",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            clean_zip = tempdir / "DocMason-clean.zip"
            demo_zip = tempdir / "DocMason-demo-ico-gcs.zip"
            self.assertTrue(clean_zip.exists())
            self.assertTrue(demo_zip.exists())
            self.assertTrue((tempdir / "DocMason-clean.zip.sha256").exists())
            self.assertTrue((tempdir / "DocMason-demo-ico-gcs.zip.sha256").exists())

            with zipfile.ZipFile(clean_zip) as archive:
                names = set(archive.namelist())
                clean_manifest = json.loads(
                    archive.read("distribution-manifest.json").decode("utf-8")
                )
            self.assertIn("README.md", names)
            self.assertIn(".github/copilot-instructions.md", names)
            self.assertIn("distribution-manifest.json", names)
            self.assertIn("original_doc/.gitkeep", names)
            self.assertNotIn(".github/workflows/release-distributions.yml", names)
            self.assertNotIn("tests/test_foundation_and_contracts.py", names)
            self.assertNotIn("sample_corpus/README.md", names)
            self.assertNotIn("skills/optional/public-sample-workspace/SKILL.md", names)
            self.assertEqual(clean_manifest["distribution_channel"], "clean")
            self.assertEqual(clean_manifest["asset_name"], "DocMason-clean.zip")
            self.assertEqual(clean_manifest["source_repo"], "example/DocMason")
            self.assertEqual(
                clean_manifest["source_commit"],
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
            self.assertEqual(clean_manifest["source_ref"], "refs/tags/test-build")

            with zipfile.ZipFile(demo_zip) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("distribution-manifest.json").decode("utf-8"))
            self.assertEqual(manifest["distribution_channel"], "demo-ico-gcs")
            self.assertEqual(manifest["asset_name"], "DocMason-demo-ico-gcs.zip")
            self.assertEqual(manifest["source_version"], "test-build")
            self.assertEqual(
                manifest["source_commit"],
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
            self.assertEqual(manifest["source_ref"], "refs/tags/test-build")
            self.assertIn(".github/copilot-instructions.md", names)
            self.assertNotIn(".github/workflows/release-distributions.yml", names)
            self.assertIn("original_doc/ico/about-the-ico.md", names)
            self.assertIn("original_doc/gcs/oasis-campaign-planning.md", names)
            self.assertIn(
                "original_doc/gcs/gcs-oasis--guide-to-campaign-planning-oasis-framework.pdf",
                names,
            )
            self.assertNotIn("tests/test_foundation_and_contracts.py", names)
            self.assertNotIn("sample_corpus/README.md", names)
            self.assertNotIn("skills/optional/public-sample-workspace/SKILL.md", names)

    def test_update_docmason_core_preserves_local_workspace_data(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = tempdir / "workspace"
            workspace.mkdir()
            (workspace / "README.md").write_text("old\n", encoding="utf-8")
            (workspace / "original_doc").mkdir()
            (workspace / "original_doc" / "private.txt").write_text("secret\n", encoding="utf-8")
            (workspace / "knowledge_base").mkdir()
            (workspace / "runtime").mkdir()
            (workspace / "adapters").mkdir()
            (workspace / "distribution-manifest.json").write_text(
                json.dumps(
                    {
                        "distribution_channel": "clean",
                        "source_repo": "example/DocMason",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            bundle_root = tempdir / "bundle"
            bundle_root.mkdir()
            (bundle_root / "README.md").write_text("new\n", encoding="utf-8")
            (bundle_root / "distribution-manifest.json").write_text(
                json.dumps(
                    {
                        "distribution_channel": "clean",
                        "source_repo": "example/DocMason",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (bundle_root / "scripts").mkdir()
            (bundle_root / "scripts" / "bootstrap-workspace.sh").write_text(
                "#!/bin/sh\n",
                encoding="utf-8",
            )
            bundle_zip = tempdir / "bundle.zip"
            with zipfile.ZipFile(bundle_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(bundle_root.rglob("*")):
                    archive.write(path, path.relative_to(bundle_root))

            result = subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts" / "update-docmason-core.py"),
                    "--workspace",
                    str(workspace),
                    "--bundle",
                    str(bundle_zip),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((workspace / "README.md").read_text(encoding="utf-8"), "new\n")
            self.assertEqual(
                (workspace / "original_doc" / "private.txt").read_text(encoding="utf-8"),
                "secret\n",
            )

    def test_repo_safety_check_passes_for_current_tree(self) -> None:
        result = subprocess.run(
            ["python3", str(ROOT / "scripts" / "check-repo-safety.py"), "--repo-root", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gitignore_covers_untracked_sensitive_local_artifacts(self) -> None:
        candidates = [
            "scripts/private/sync-public-sample-corpus.py",
            "evals/local-run.json",
            ".env.local",
            ".python-version",
            ".direnv/env",
            ".envrc",
            "bundle.download",
            "bundle.part",
            ".coverage.local",
            "htmlcov/index.html",
            ".hypothesis/examples.db",
            ".tox/py311/log.txt",
            ".nox/session.log",
            ".cache/state.json",
            "merge.orig",
            "merge.rej",
            "merge.bak",
        ]
        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="\n".join(candidates) + "\n",
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        ignored = {line.strip() for line in result.stdout.splitlines() if line.strip()}
        self.assertEqual(set(candidates), ignored)

    def test_copilot_workspace_instructions_remain_tracked(self) -> None:
        result = subprocess.run(
            ["git", "check-ignore", ".github/copilot-instructions.md"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout or result.stderr)


if __name__ == "__main__":
    unittest.main()

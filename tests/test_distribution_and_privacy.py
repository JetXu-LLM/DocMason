"""Distribution, privacy, and local-update tests."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from docmason.commands import DEGRADED, update_core_workspace
from docmason.project import WorkspacePaths
from docmason.release_entry import RELEASE_ENTRY_USER_AGENT, release_entry_snapshot
from docmason.release_version import read_project_version, release_tag_for_version
from docmason.update_core import CLEAN_ASSET_NAME, UpdateCoreError, perform_update_core

ROOT = Path(__file__).resolve().parents[1]
CLEAN_RELEASE_DOWNLOAD_URL = (
    "https://github.com/example/DocMason/releases/download/v0.2.0/DocMason-clean.zip"
)
CLEAN_RELEASE_SHA_URL = CLEAN_RELEASE_DOWNLOAD_URL + ".sha256"


def _safe_python_executable() -> str:
    framework_python = (
        Path(sys.exec_prefix) / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    )
    if framework_python.exists():
        return str(framework_python)
    return sys.executable


PYTHON = _safe_python_executable()


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._buffer = io.BytesIO(payload)

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self._buffer.close()

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)


class DistributionAndPrivacyTests(unittest.TestCase):
    """Cover the public sample corpus, privacy guardrails, and generated bundles."""

    def make_bundle_workspace(
        self,
        tempdir: Path,
        *,
        distribution_channel: str = "clean",
        source_version: str = "v0.1.0",
        automatic_check_enabled: bool = True,
    ) -> WorkspacePaths:
        workspace_root = tempdir / "workspace"
        workspace_root.mkdir()
        (workspace_root / "README.md").write_text("old\n", encoding="utf-8")
        (workspace_root / "OLD_ONLY.txt").write_text("stale\n", encoding="utf-8")
        (workspace_root / ".docmason").mkdir()
        (workspace_root / ".docmason" / "toolchain.txt").write_text("managed\n", encoding="utf-8")
        (workspace_root / ".agents").mkdir()
        (workspace_root / ".agents" / "skills.txt").write_text("shim\n", encoding="utf-8")
        (workspace_root / "original_doc").mkdir()
        (workspace_root / "original_doc" / "private.txt").write_text("secret\n", encoding="utf-8")
        (workspace_root / "knowledge_base").mkdir()
        (workspace_root / "knowledge_base" / "index.txt").write_text("kb\n", encoding="utf-8")
        (workspace_root / "runtime").mkdir()
        (workspace_root / "runtime" / "state").mkdir(parents=True, exist_ok=True)
        (workspace_root / "adapters").mkdir()
        (workspace_root / "adapters" / "local.txt").write_text("adapter\n", encoding="utf-8")
        (workspace_root / "pyproject.toml").write_text(
            "[project]\nname='docmason'\nversion='0.0.0'\n",
            encoding="utf-8",
        )
        (workspace_root / "docmason.yaml").write_text(
            "workspace:\n  source_dir: original_doc\n",
            encoding="utf-8",
        )
        (workspace_root / "distribution-manifest.json").write_text(
            json.dumps(
                {
                    "distribution_channel": distribution_channel,
                    "asset_name": (
                        "DocMason-clean.zip"
                        if distribution_channel == "clean"
                        else "DocMason-demo-ico-gcs.zip"
                    ),
                    "source_version": source_version,
                    "source_repo": "example/DocMason",
                    "release_entry": {
                        "schema_version": 1,
                        "update_service_url": "https://updates.example.invalid/v1/check",
                        "distribution_channel": distribution_channel,
                        "automatic_check_scope": "canonical-ask",
                        "automatic_check_cooldown_hours": 20,
                        "automatic_check_enabled_by_default": True,
                        "asset_name": (
                            "DocMason-clean.zip"
                            if distribution_channel == "clean"
                            else "DocMason-demo-ico-gcs.zip"
                        ),
                    },
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (workspace_root / "runtime" / "state" / "release-client.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "automatic_check_enabled": automatic_check_enabled,
                    "installation_hash": None,
                    "created_at": None,
                    "last_check_attempted_at": None,
                    "next_eligible_at": None,
                    "last_known_latest_version": None,
                    "last_notified_version": None,
                    "last_check_status": None,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return WorkspacePaths(root=workspace_root)

    def make_clean_bundle_zip(self, tempdir: Path, *, version: str = "v0.2.0") -> Path:
        bundle_root = tempdir / "bundle"
        bundle_root.mkdir()
        (bundle_root / "README.md").write_text("new\n", encoding="utf-8")
        (bundle_root / "NEW_ONLY.txt").write_text("fresh\n", encoding="utf-8")
        (bundle_root / "distribution-manifest.json").write_text(
            json.dumps(
                {
                    "distribution_channel": "clean",
                    "asset_name": CLEAN_ASSET_NAME,
                    "source_version": version,
                    "source_repo": "example/DocMason",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        (bundle_root / "scripts").mkdir()
        (bundle_root / "scripts" / "bootstrap-workspace.sh").write_text(
            "#!/bin/sh\n",
            encoding="utf-8",
        )
        bundle_zip = tempdir / CLEAN_ASSET_NAME
        with zipfile.ZipFile(bundle_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(bundle_root.rglob("*")):
                archive.write(path, path.relative_to(bundle_root))
        return bundle_zip

    def fake_urlopen(
        self,
        *,
        service_url: str,
        service_payload: dict[str, object],
        downloads: dict[str, bytes],
    ):
        def _urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del timeout
            url = getattr(request, "full_url", request)
            if url == service_url:
                self.assertEqual(request.headers.get("User-agent"), RELEASE_ENTRY_USER_AGENT)
                payload = json.loads(request.data.decode("utf-8"))
                self.assertEqual(payload["trigger"], "update-core")
                self.assertNotIn("source_version", payload)
                return _FakeResponse(
                    (json.dumps(service_payload, sort_keys=True) + "\n").encode("utf-8")
                )
            if url in downloads:
                return _FakeResponse(downloads[str(url)])
            raise AssertionError(f"Unexpected download URL: {url}")

        return _urlopen

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
            self.assertTrue((target / "gcs" / "oasis-campaign-planning.md").exists())
            marker = json.loads((target / ".docmason-sample.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["preset"], "ico-gcs")

    def test_build_distributions_outputs_clean_and_demo_bundles(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            expected_tag = release_tag_for_version(read_project_version(ROOT / "pyproject.toml"))
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "build-distributions.py"),
                    "--repo-root",
                    str(ROOT),
                    "--output-dir",
                    str(tempdir),
                    "--version",
                    expected_tag,
                    "--github-repo",
                    "example/DocMason",
                    "--update-service-url",
                    "https://updates.example.invalid/v1/check",
                    "--source-commit",
                    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    "--source-ref",
                    f"refs/tags/{expected_tag}",
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
            self.assertIn("runtime/state/release-client.json", names)
            self.assertNotIn("ops/release-entry/worker.js", names)
            self.assertNotIn(".github/workflows/release-distributions.yml", names)
            self.assertNotIn("tests/test_foundation_and_contracts.py", names)
            self.assertNotIn("sample_corpus/README.md", names)
            self.assertNotIn("skills/optional/public-sample-workspace/SKILL.md", names)
            self.assertEqual(clean_manifest["distribution_channel"], "clean")
            self.assertEqual(clean_manifest["asset_name"], "DocMason-clean.zip")
            self.assertEqual(clean_manifest["source_repo"], "example/DocMason")
            self.assertEqual(
                clean_manifest["release_entry"]["update_service_url"],
                "https://updates.example.invalid/v1/check",
            )
            self.assertEqual(
                clean_manifest["release_entry"]["automatic_check_scope"],
                "canonical-ask",
            )
            self.assertEqual(
                clean_manifest["release_entry"]["automatic_check_cooldown_hours"],
                20,
            )
            self.assertTrue(clean_manifest["release_entry"]["automatic_check_enabled_by_default"])
            self.assertEqual(
                clean_manifest["source_commit"],
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
            self.assertEqual(clean_manifest["source_ref"], f"refs/tags/{expected_tag}")

            with zipfile.ZipFile(demo_zip) as archive:
                names = set(archive.namelist())
                manifest = json.loads(archive.read("distribution-manifest.json").decode("utf-8"))
            self.assertEqual(manifest["distribution_channel"], "demo-ico-gcs")
            self.assertEqual(manifest["asset_name"], "DocMason-demo-ico-gcs.zip")
            self.assertEqual(manifest["source_version"], expected_tag)
            self.assertEqual(
                manifest["release_entry"]["distribution_channel"],
                "demo-ico-gcs",
            )
            self.assertEqual(
                manifest["source_commit"],
                "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            )
            self.assertEqual(manifest["source_ref"], f"refs/tags/{expected_tag}")
            self.assertIn(".github/copilot-instructions.md", names)
            self.assertNotIn(".github/workflows/release-distributions.yml", names)
            self.assertIn("original_doc/ico/about-the-ico.md", names)
            self.assertIn("original_doc/gcs/oasis-campaign-planning.md", names)
            self.assertIn(
                "original_doc/gcs/gcs-oasis--guide-to-campaign-planning-oasis-framework.pdf",
                names,
            )
            self.assertNotIn("ops/release-entry/worker.js", names)
            self.assertNotIn("tests/test_foundation_and_contracts.py", names)
            self.assertNotIn("sample_corpus/README.md", names)
            self.assertNotIn("skills/optional/public-sample-workspace/SKILL.md", names)

    def test_build_distributions_excludes_tracked_ops_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            repo_root = tempdir / "repo"
            repo_root.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
            (repo_root / "README.md").write_text("repo\n", encoding="utf-8")
            (repo_root / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (repo_root / "pyproject.toml").write_text(
                "[project]\nname='docmason'\nversion='0.0.0'\n",
                encoding="utf-8",
            )
            (repo_root / "docmason.yaml").write_text(
                "workspace:\n  source_dir: original_doc\n",
                encoding="utf-8",
            )
            (repo_root / "original_doc").mkdir()
            (repo_root / "original_doc" / ".gitkeep").write_text("", encoding="utf-8")
            (repo_root / "runtime").mkdir()
            (repo_root / "knowledge_base").mkdir()
            (repo_root / "adapters").mkdir()
            (repo_root / "sample_corpus" / "ico-gcs" / "ico").mkdir(parents=True)
            (repo_root / "sample_corpus" / "ico-gcs" / "gcs").mkdir(parents=True)
            (repo_root / "sample_corpus" / "ico-gcs" / "ico" / "fixture.md").write_text(
                "fixture\n",
                encoding="utf-8",
            )
            (repo_root / "sample_corpus" / "ico-gcs" / "gcs" / "fixture.md").write_text(
                "fixture\n",
                encoding="utf-8",
            )
            (repo_root / "ops" / "release-entry").mkdir(parents=True)
            (repo_root / "ops" / "release-entry" / "worker.js").write_text(
                "export default {}\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", "."], cwd=repo_root, check=True)

            output_dir = tempdir / "dist"
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "build-distributions.py"),
                    "--repo-root",
                    str(repo_root),
                    "--output-dir",
                    str(output_dir),
                    "--version",
                    "v0.0.0",
                    "--github-repo",
                    "example/DocMason",
                    "--update-service-url",
                    "https://updates.example.invalid/v1/check",
                    "--source-commit",
                    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    "--source-ref",
                    "refs/tags/v0.0.0",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(output_dir / "DocMason-clean.zip") as archive:
                names = set(archive.namelist())
            self.assertNotIn("ops/release-entry/worker.js", names)

    def test_build_distributions_non_git_fallback_skips_symlinked_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            repo_root = tempdir / "repo"
            repo_root.mkdir()
            outside_file = tempdir / "outside-secret.txt"
            outside_file.write_text("do not package\n", encoding="utf-8")

            (repo_root / "README.md").write_text("repo\n", encoding="utf-8")
            (repo_root / "AGENTS.md").write_text("agents\n", encoding="utf-8")
            (repo_root / "pyproject.toml").write_text(
                "[project]\nname='docmason'\nversion='0.0.0'\n",
                encoding="utf-8",
            )
            (repo_root / "docmason.yaml").write_text(
                "workspace:\n  source_dir: original_doc\n",
                encoding="utf-8",
            )
            (repo_root / "original_doc").mkdir()
            (repo_root / "original_doc" / ".gitkeep").write_text("", encoding="utf-8")
            (repo_root / "runtime").mkdir()
            (repo_root / "knowledge_base").mkdir()
            (repo_root / "adapters").mkdir()
            (repo_root / "sample_corpus" / "ico-gcs" / "ico").mkdir(parents=True)
            (repo_root / "sample_corpus" / "ico-gcs" / "gcs").mkdir(parents=True)
            (repo_root / "sample_corpus" / "ico-gcs" / "ico" / "fixture.md").write_text(
                "fixture\n",
                encoding="utf-8",
            )
            (repo_root / "sample_corpus" / "ico-gcs" / "gcs" / "fixture.md").write_text(
                "fixture\n",
                encoding="utf-8",
            )
            (repo_root / "leak.txt").symlink_to(outside_file)

            output_dir = tempdir / "dist"
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "build-distributions.py"),
                    "--repo-root",
                    str(repo_root),
                    "--output-dir",
                    str(output_dir),
                    "--version",
                    "v0.0.0",
                    "--github-repo",
                    "example/DocMason",
                    "--update-service-url",
                    "https://updates.example.invalid/v1/check",
                    "--source-commit",
                    "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                    "--source-ref",
                    "refs/tags/v0.0.0",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with zipfile.ZipFile(output_dir / "DocMason-clean.zip") as archive:
                names = set(archive.namelist())
            self.assertNotIn("leak.txt", names)

    def test_build_distributions_rejects_mismatched_release_tag(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "build-distributions.py"),
                    "--repo-root",
                    str(ROOT),
                    "--output-dir",
                    str(tempdir),
                    "--version",
                    "v9.9.9",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match committed project version", result.stderr)

    def test_source_repo_release_entry_remains_disabled_by_default(self) -> None:
        snapshot = release_entry_snapshot(WorkspacePaths(root=ROOT))
        self.assertFalse(snapshot["bundle_detected"])
        self.assertFalse(snapshot["effective_enabled"])
        self.assertEqual(snapshot["disabled_reason"], "source-repo")

    def test_update_core_rejects_source_repo(self) -> None:
        with self.assertRaises(UpdateCoreError) as raised:
            perform_update_core(WorkspacePaths(root=ROOT))
        self.assertEqual(raised.exception.code, "unsupported-workspace")

    def test_update_core_reports_already_current_without_replacing_files(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.2.0")
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": (
                        "https://github.com/example/DocMason/releases/download/v0.2.0/"
                        "DocMason-clean.zip"
                    ),
                    "asset_name": "DocMason-clean.zip",
                },
            }
            result = perform_update_core(
                workspace,
                now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                urlopen=self.fake_urlopen(
                    service_url="https://updates.example.invalid/v1/check",
                    service_payload=response,
                    downloads={},
                ),
            )
            self.assertEqual(result["update_core_status"], "already-current")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "old\n")
            state = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_known_latest_version"], "v0.2.0")
            self.assertEqual(state["last_check_status"], "manual-ok-no-update")

    def test_update_core_rejects_release_asset_outside_trusted_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.1.0")
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": (
                        "https://github.com/evil/DocMason/releases/download/v0.2.0/"
                        "DocMason-clean.zip"
                    ),
                    "asset_name": "DocMason-clean.zip",
                },
            }

            with self.assertRaises(UpdateCoreError) as raised:
                perform_update_core(
                    workspace,
                    now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                    urlopen=self.fake_urlopen(
                        service_url="https://updates.example.invalid/v1/check",
                        service_payload=response,
                        downloads={},
                    ),
                )

            self.assertEqual(raised.exception.code, "invalid-release-entry-response")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "old\n")

    def test_update_core_network_update_works_when_dnt_and_local_disable_are_set(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(
                tempdir,
                source_version="v0.1.0",
                automatic_check_enabled=False,
            )
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")
            digest = bundle_zip.read_bytes()
            sha_text = f"{hashlib.sha256(digest).hexdigest()}  {CLEAN_ASSET_NAME}\n"
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": (
                        "https://github.com/example/DocMason/releases/download/v0.2.0/"
                        "DocMason-clean.zip"
                    ),
                    "asset_name": "DocMason-clean.zip",
                },
            }
            downloads = {
                CLEAN_RELEASE_DOWNLOAD_URL: digest,
                CLEAN_RELEASE_SHA_URL: sha_text.encode("utf-8"),
            }
            with mock.patch.dict("os.environ", {"DO_NOT_TRACK": "1"}, clear=False):
                result = perform_update_core(
                    workspace,
                    now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                    urlopen=self.fake_urlopen(
                        service_url="https://updates.example.invalid/v1/check",
                        service_payload=response,
                        downloads=downloads,
                    ),
                )
            self.assertEqual(result["update_core_status"], "updated")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "new\n")
            self.assertFalse((workspace.root / "OLD_ONLY.txt").exists())
            self.assertTrue((workspace.root / "NEW_ONLY.txt").exists())
            self.assertEqual(
                (workspace.root / "original_doc" / "private.txt").read_text(encoding="utf-8"),
                "secret\n",
            )
            self.assertEqual(
                (workspace.root / ".docmason" / "toolchain.txt").read_text(encoding="utf-8"),
                "managed\n",
            )
            self.assertEqual(
                json.loads(
                    (workspace.root / "distribution-manifest.json").read_text(
                        encoding="utf-8"
                    )
                )["source_version"],
                "v0.2.0",
            )
            state = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
            self.assertFalse(state["automatic_check_enabled"])
            self.assertEqual(state["last_check_status"], "manual-updated")
            self.assertEqual(state["last_known_latest_version"], "v0.2.0")

    def test_update_core_on_demo_bundle_converges_to_clean_core(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(
                tempdir,
                distribution_channel="demo-ico-gcs",
                source_version="v0.1.0",
            )
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")
            bundle_bytes = bundle_zip.read_bytes()
            sha_text = f"{hashlib.sha256(bundle_bytes).hexdigest()}  {CLEAN_ASSET_NAME}\n"
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "demo-ico-gcs",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": (
                        "https://github.com/example/DocMason/releases/download/v0.2.0/"
                        "DocMason-demo-ico-gcs.zip"
                    ),
                    "asset_name": "DocMason-demo-ico-gcs.zip",
                },
            }
            downloads = {
                CLEAN_RELEASE_DOWNLOAD_URL: bundle_bytes,
                CLEAN_RELEASE_SHA_URL: sha_text.encode("utf-8"),
            }
            perform_update_core(
                workspace,
                now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                urlopen=self.fake_urlopen(
                    service_url="https://updates.example.invalid/v1/check",
                    service_payload=response,
                    downloads=downloads,
                ),
            )
            manifest = json.loads(
                (workspace.root / "distribution-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["distribution_channel"], "clean")
            self.assertEqual(manifest["source_version"], "v0.2.0")
            self.assertEqual(
                (workspace.root / "original_doc" / "private.txt").read_text(encoding="utf-8"),
                "secret\n",
            )

    def test_update_core_checksum_mismatch_keeps_workspace_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.1.0")
            original_manifest = (workspace.root / "distribution-manifest.json").read_text(
                encoding="utf-8"
            )
            original_state = workspace.release_client_state_path.read_text(encoding="utf-8")
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": (
                        "https://github.com/example/DocMason/releases/download/v0.2.0/"
                        "DocMason-clean.zip"
                    ),
                    "asset_name": "DocMason-clean.zip",
                },
            }
            downloads = {
                CLEAN_RELEASE_DOWNLOAD_URL: bundle_zip.read_bytes(),
                CLEAN_RELEASE_SHA_URL: ("0" * 64 + f"  {CLEAN_ASSET_NAME}\n").encode("utf-8"),
            }
            with self.assertRaises(UpdateCoreError) as raised:
                perform_update_core(
                    workspace,
                    now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                    urlopen=self.fake_urlopen(
                        service_url="https://updates.example.invalid/v1/check",
                        service_payload=response,
                        downloads=downloads,
                    ),
                )
            self.assertEqual(raised.exception.code, "checksum-mismatch")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "old\n")
            self.assertEqual(
                (workspace.root / "distribution-manifest.json").read_text(encoding="utf-8"),
                original_manifest,
            )
            self.assertEqual(
                workspace.release_client_state_path.read_text(encoding="utf-8"),
                original_state,
            )

    def test_update_core_accepts_multiline_checksum_file_when_later_line_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.1.0")
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")
            bundle_bytes = bundle_zip.read_bytes()
            sha_text = (
                "0" * 64
                + "  Some-Other-Asset.zip\n"
                + f"{hashlib.sha256(bundle_bytes).hexdigest()}  {CLEAN_ASSET_NAME}\n"
            )
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": CLEAN_RELEASE_DOWNLOAD_URL,
                    "asset_name": "DocMason-clean.zip",
                },
            }
            downloads = {
                CLEAN_RELEASE_DOWNLOAD_URL: bundle_bytes,
                CLEAN_RELEASE_SHA_URL: sha_text.encode("utf-8"),
            }

            result = perform_update_core(
                workspace,
                now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                urlopen=self.fake_urlopen(
                    service_url="https://updates.example.invalid/v1/check",
                    service_payload=response,
                    downloads=downloads,
                ),
            )

            self.assertEqual(result["update_core_status"], "updated")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "new\n")

    def test_update_core_download_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.1.0")
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": CLEAN_RELEASE_DOWNLOAD_URL,
                    "asset_name": "DocMason-clean.zip",
                },
            }

            def failing_urlopen(request, timeout):  # type: ignore[no-untyped-def]
                url = getattr(request, "full_url", request)
                del timeout
                if url == "https://updates.example.invalid/v1/check":
                    return _FakeResponse(
                        (json.dumps(response, sort_keys=True) + "\n").encode("utf-8")
                    )
                raise OSError("download unavailable")

            with self.assertRaises(UpdateCoreError) as raised:
                perform_update_core(
                    workspace,
                    now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                    urlopen=failing_urlopen,
                )
            self.assertEqual(raised.exception.code, "download-failed")

    def test_update_core_state_sync_failure_reports_degraded_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir)
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")
            with mock.patch(
                "docmason.update_core.persist_release_client_state",
                side_effect=OSError("read-only state dir"),
            ):
                report = update_core_workspace(workspace, bundle=bundle_zip)
            self.assertEqual(report.payload["status"], DEGRADED)
            self.assertEqual(report.payload["update_core_status"], "state-sync-failed")
            self.assertTrue(report.payload["core_updated"])
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "new\n")

    def test_update_core_rolls_back_when_apply_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.1.0")
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")
            bundle_bytes = bundle_zip.read_bytes()
            sha_text = f"{hashlib.sha256(bundle_bytes).hexdigest()}  {CLEAN_ASSET_NAME}\n"
            response = {
                "schema_version": 1,
                "current_release": {
                    "distribution_channel": "clean",
                    "latest_version": "v0.2.0",
                    "published_at": "2026-03-30T12:00:00Z",
                    "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                    "asset_url": (
                        "https://github.com/example/DocMason/releases/download/v0.2.0/"
                        "DocMason-clean.zip"
                    ),
                    "asset_name": "DocMason-clean.zip",
                },
            }
            downloads = {
                CLEAN_RELEASE_DOWNLOAD_URL: bundle_bytes,
                CLEAN_RELEASE_SHA_URL: sha_text.encode("utf-8"),
            }
            real_move = shutil.move
            failed = {"done": False}

            def flaky_move(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
                source = Path(src)
                if (
                    not failed["done"]
                    and source.name == "README.md"
                    and source.parent.name == "bundle"
                ):
                    failed["done"] = True
                    raise OSError("simulated apply failure")
                return real_move(src, dst, *args, **kwargs)

            with mock.patch("docmason.update_core.shutil.move", side_effect=flaky_move):
                with self.assertRaises(UpdateCoreError) as raised:
                    perform_update_core(
                        workspace,
                        now=datetime(2026, 3, 30, 12, 0, tzinfo=UTC),
                        urlopen=self.fake_urlopen(
                            service_url="https://updates.example.invalid/v1/check",
                            service_payload=response,
                            downloads=downloads,
                        ),
                    )
            self.assertEqual(raised.exception.code, "apply-failed")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "old\n")
            self.assertTrue((workspace.root / "OLD_ONLY.txt").exists())
            self.assertFalse((workspace.root / "NEW_ONLY.txt").exists())

    def test_update_core_rejects_zip_with_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir, source_version="v0.1.0")
            bundle_zip = tempdir / CLEAN_ASSET_NAME
            with zipfile.ZipFile(bundle_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "distribution-manifest.json",
                    json.dumps(
                        {
                            "distribution_channel": "clean",
                            "asset_name": CLEAN_ASSET_NAME,
                            "source_version": "v0.2.0",
                            "source_repo": "example/DocMason",
                        },
                        sort_keys=True,
                    )
                    + "\n",
                )
                archive.writestr("README.md", "new\n")
                archive.writestr("../../escaped.txt", "owned\n")

            with self.assertRaises(UpdateCoreError) as raised:
                perform_update_core(workspace, bundle_path=bundle_zip)

            self.assertEqual(raised.exception.code, "invalid-bundle")
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "old\n")
            self.assertFalse((workspace.root / "NEW_ONLY.txt").exists())

    def test_update_docmason_core_preserves_local_workspace_data(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            tempdir = Path(tempdir_name)
            workspace = self.make_bundle_workspace(tempdir)
            bundle_zip = self.make_clean_bundle_zip(tempdir, version="v0.2.0")

            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "update-docmason-core.py"),
                    "--workspace",
                    str(workspace.root),
                    "--bundle",
                    str(bundle_zip),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((workspace.root / "README.md").read_text(encoding="utf-8"), "new\n")
            self.assertFalse((workspace.root / "OLD_ONLY.txt").exists())
            self.assertTrue((workspace.root / "NEW_ONLY.txt").exists())
            self.assertEqual(
                (workspace.root / "original_doc" / "private.txt").read_text(encoding="utf-8"),
                "secret\n",
            )
            self.assertEqual(
                (workspace.root / ".docmason" / "toolchain.txt").read_text(encoding="utf-8"),
                "managed\n",
            )

    def test_repo_safety_check_passes_for_current_tree(self) -> None:
        result = subprocess.run(
            [PYTHON, str(ROOT / "scripts" / "check-repo-safety.py"), "--repo-root", str(ROOT)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repo_safety_check_skips_non_git_bundle_root(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            bundle_root = Path(tempdir_name)
            (bundle_root / "README.md").write_text("bundle\n", encoding="utf-8")
            (bundle_root / "original_doc").mkdir(parents=True, exist_ok=True)
            (bundle_root / "original_doc" / "demo.txt").write_text("demo\n", encoding="utf-8")

            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts" / "check-repo-safety.py"),
                    "--repo-root",
                    str(bundle_root),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not a Git checkout", result.stdout)

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

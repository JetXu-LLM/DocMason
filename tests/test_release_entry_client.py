"""Release-entry client, status, and doctor contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from docmason.commands import READY, doctor_workspace, status_workspace
from docmason.project import WorkspacePaths, write_json
from docmason.release_entry import (
    RELEASE_ENTRY_USER_AGENT,
    maybe_run_release_entry_check,
    release_entry_snapshot,
)
from tests.support_ready_workspace import seed_self_contained_bootstrap_state


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb

    def read(self) -> bytes:
        return (json.dumps(self._payload, sort_keys=True) + "\n").encode("utf-8")


class ReleaseEntryClientTests(unittest.TestCase):
    """Exercise bundle-only release-entry state, gating, and update checks."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "src" / "docmason").mkdir(parents=True)
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "runtime").mkdir()
        (root / "adapters").mkdir()
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

    def ready_probe(self, workspace: WorkspacePaths) -> tuple[bool, str]:
        del workspace
        return True, "Editable install resolves to the workspace source tree."

    def missing_probe(self, workspace: WorkspacePaths) -> tuple[bool, str]:
        del workspace
        return False, "DocMason is not installed in editable mode inside `.venv`."

    def seed_bundle(
        self,
        workspace: WorkspacePaths,
        *,
        distribution_channel: str = "clean",
        source_version: str = "v0.1.0",
        update_service_url: str | None = "https://updates.example.invalid/v1/check",
        automatic_check_enabled: bool = True,
    ) -> None:
        write_json(
            workspace.distribution_manifest_path,
            {
                "distribution_channel": distribution_channel,
                "asset_name": "DocMason-clean.zip",
                "source_version": source_version,
                "source_repo": "example/DocMason",
                "release_entry": {
                    "schema_version": 1,
                    "update_service_url": update_service_url,
                    "distribution_channel": distribution_channel,
                    "automatic_check_scope": "canonical-ask",
                    "automatic_check_cooldown_hours": 20,
                    "automatic_check_enabled_by_default": True,
                    "asset_name": "DocMason-clean.zip",
                },
            },
        )
        write_json(
            workspace.release_client_state_path,
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
        )

    def fake_urlopen(self, latest_version: str):
        def _urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del timeout
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.headers.get("User-agent"), RELEASE_ENTRY_USER_AGENT)
            payload = json.loads(request.data.decode("utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["trigger"], "ask-auto")
            self.assertIn("installation_hash", payload)
            return _FakeResponse(
                {
                    "schema_version": 1,
                    "current_release": {
                        "distribution_channel": payload["distribution_channel"],
                        "latest_version": latest_version,
                        "published_at": "2026-03-30T12:00:00Z",
                        "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                        "asset_url": "https://github.com/example/DocMason/releases/download/v0.2.0/DocMason-clean.zip",
                        "asset_name": "DocMason-clean.zip",
                    },
                }
            )

        return _urlopen

    def test_source_repo_release_entry_stays_disabled_by_default(self) -> None:
        workspace = self.make_workspace()
        snapshot = release_entry_snapshot(workspace)
        self.assertFalse(snapshot["bundle_detected"])
        self.assertFalse(snapshot["effective_enabled"])
        self.assertEqual(snapshot["disabled_reason"], "source-repo")

    def test_bundle_snapshot_reports_enabled_release_entry_state(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)
        snapshot = release_entry_snapshot(workspace)
        self.assertTrue(snapshot["bundle_detected"])
        self.assertTrue(snapshot["bundle_configured"])
        self.assertTrue(snapshot["effective_enabled"])
        self.assertIsNone(snapshot["disabled_reason"])

    def test_bundle_snapshot_reports_dnt_override(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)
        with mock.patch.dict("os.environ", {"DO_NOT_TRACK": "1"}, clear=False):
            snapshot = release_entry_snapshot(workspace)
        self.assertFalse(snapshot["effective_enabled"])
        self.assertEqual(snapshot["disabled_reason"], "dnt")

    def test_bundle_snapshot_reports_local_disable(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace, automatic_check_enabled=False)
        snapshot = release_entry_snapshot(workspace)
        self.assertFalse(snapshot["effective_enabled"])
        self.assertEqual(snapshot["disabled_reason"], "local-config")

    def test_release_entry_check_respects_dnt_override(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)
        called = False

        def failing_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del request, timeout
            nonlocal called
            called = True
            raise AssertionError("urlopen should not run when DNT is active")

        with mock.patch.dict("os.environ", {"DO_NOT_TRACK": "1"}, clear=False):
            result = maybe_run_release_entry_check(
                workspace,
                now=datetime(2026, 3, 30, 1, 0, tzinfo=UTC),
                urlopen=failing_urlopen,
            )
        self.assertFalse(called)
        self.assertFalse(result["attempted"])
        self.assertEqual(result["release_entry_status"]["disabled_reason"], "dnt")

    def test_release_entry_check_respects_local_disable(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace, automatic_check_enabled=False)
        called = False

        def failing_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del request, timeout
            nonlocal called
            called = True
            raise AssertionError("urlopen should not run when local config disables checks")

        result = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 30, 1, 0, tzinfo=UTC),
            urlopen=failing_urlopen,
        )
        self.assertFalse(called)
        self.assertFalse(result["attempted"])
        self.assertEqual(result["release_entry_status"]["disabled_reason"], "local-config")

    def test_release_entry_check_rejects_non_https_service_url(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace, update_service_url="http://updates.example.invalid/v1/check")
        called = False

        def failing_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del request, timeout
            nonlocal called
            called = True
            raise AssertionError("urlopen should not run for a non-HTTPS release-entry URL")

        result = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 30, 1, 0, tzinfo=UTC),
            urlopen=failing_urlopen,
        )

        self.assertFalse(called)
        self.assertTrue(result["attempted"])
        persisted = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["last_check_status"], "network-error")

    def test_release_entry_check_respects_cooldown(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)
        state = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
        state["next_eligible_at"] = "2026-03-31T12:00:00Z"
        write_json(workspace.release_client_state_path, state)
        called = False

        def failing_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del request, timeout
            nonlocal called
            called = True
            raise AssertionError("urlopen should not run before the cooldown expires")

        result = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 30, 2, 0, tzinfo=UTC),
            urlopen=failing_urlopen,
        )
        self.assertFalse(called)
        self.assertFalse(result["attempted"])
        self.assertFalse(result["release_entry_status"]["eligible_now"])

    def test_successful_check_updates_state_and_emits_one_notice(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace, source_version="v0.1.0")
        first = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 30, 3, 0, tzinfo=UTC),
            urlopen=self.fake_urlopen("v0.2.0"),
        )
        self.assertTrue(first["attempted"])
        self.assertIn("DocMason update available", first["notice"])
        first_state = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
        self.assertEqual(first_state["last_known_latest_version"], "v0.2.0")
        self.assertEqual(first_state["last_notified_version"], "v0.2.0")
        self.assertEqual(first_state["last_check_status"], "ok-update-available")
        self.assertIsNotNone(first_state["installation_hash"])

        second = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 30, 22, 0, tzinfo=UTC),
            urlopen=self.fake_urlopen("v0.2.0"),
        )
        self.assertFalse(second["attempted"])
        self.assertIsNone(second["notice"])

        third = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 31, 23, 30, tzinfo=UTC),
            urlopen=self.fake_urlopen("v0.2.0"),
        )
        self.assertTrue(third["attempted"])
        self.assertIsNone(third["notice"])
        third_state = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
        self.assertEqual(third_state["last_notified_version"], "v0.2.0")

    def test_network_error_updates_last_check_status(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)

        def failing_urlopen(request, timeout):  # type: ignore[no-untyped-def]
            del request, timeout
            raise OSError("network down")

        result = maybe_run_release_entry_check(
            workspace,
            now=datetime(2026, 3, 30, 5, 0, tzinfo=UTC),
            urlopen=failing_urlopen,
        )
        self.assertTrue(result["attempted"])
        state = json.loads(workspace.release_client_state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_check_status"], "network-error")

    def test_status_and_doctor_expose_enabled_release_entry_state(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)
        seed_self_contained_bootstrap_state(workspace)

        status_report = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertTrue(status_report.payload["release_entry"]["effective_enabled"])
        self.assertIn("Release entry: enabled", "\n".join(status_report.lines))

        doctor_report = doctor_workspace(workspace, editable_install_probe=self.ready_probe)
        release_check = next(
            check for check in doctor_report.payload["checks"] if check["name"] == "release-entry"
        )
        self.assertEqual(release_check["status"], READY)

    def test_status_and_doctor_expose_disabled_release_entry_state(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace, automatic_check_enabled=False)
        seed_self_contained_bootstrap_state(workspace)

        status_report = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(status_report.payload["release_entry"]["disabled_reason"], "local-config")
        self.assertIn("reason=local-config", "\n".join(status_report.lines))

        doctor_report = doctor_workspace(workspace, editable_install_probe=self.ready_probe)
        release_check = next(
            check for check in doctor_report.payload["checks"] if check["name"] == "release-entry"
        )
        self.assertEqual(release_check["status"], READY)

    def test_status_and_doctor_expose_dnt_disabled_release_entry_state(self) -> None:
        workspace = self.make_workspace()
        self.seed_bundle(workspace)
        seed_self_contained_bootstrap_state(workspace)

        with mock.patch.dict("os.environ", {"DO_NOT_TRACK": "1"}, clear=False):
            status_report = status_workspace(workspace, editable_install_probe=self.ready_probe)
            doctor_report = doctor_workspace(workspace, editable_install_probe=self.ready_probe)

        self.assertEqual(status_report.payload["release_entry"]["disabled_reason"], "dnt")
        release_check = next(
            check for check in doctor_report.payload["checks"] if check["name"] == "release-entry"
        )
        self.assertEqual(release_check["status"], READY)


if __name__ == "__main__":
    unittest.main()

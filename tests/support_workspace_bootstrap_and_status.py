"""Workspace command surface tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.cli import build_parser, main
from docmason.commands import (
    ACTION_REQUIRED,
    DEGRADED,
    READY,
    CommandExecution,
    CommandReport,
    doctor_workspace,
    prepare_workspace,
    status_workspace,
    sync_workspace,
    sync_adapters,
)
from docmason.control_plane import ensure_shared_job, load_shared_job
from docmason.project import (
    WorkspacePaths,
    cached_bootstrap_readiness,
    source_inventory_signature,
    write_json,
)


class WorkspaceBootstrapAndStatusTests(unittest.TestCase):
    """Cover the CLI, workspace bootstrap, and adapter behavior."""

    def seed_workflow_metadata(
        self,
        skill_dir: Path,
        *,
        workflow_id: str,
        category: str,
        mutability: str,
        parallelism: str,
        background_commands: list[str],
    ) -> None:
        payload = {
            "schema_version": 1,
            "workflow_id": workflow_id,
            "category": category,
            "entry_intents": [f"{workflow_id} intent"],
            "required_capabilities": ["local file access"],
            "defaults": {
                "default_target": "workspace",
                "default_mode": "test",
            },
            "execution_hints": {
                "mutability": mutability,
                "parallelism": parallelism,
                "background_commands": background_commands,
                "must_return_to_main_agent": True,
            },
            "handoff": {
                "completion_signal": f"Return {workflow_id} to the main agent.",
                "artifacts": [],
                "follow_up": [],
            },
        }
        (skill_dir / "workflow.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        (root / "skills" / "canonical" / "workspace-bootstrap").mkdir(parents=True)
        (root / "original_doc").mkdir()
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
        self.seed_workflow_metadata(
            root / "skills" / "canonical" / "workspace-bootstrap",
            workflow_id="workspace-bootstrap",
            category="foundation",
            mutability="workspace-write",
            parallelism="none",
            background_commands=["docmason prepare --json"],
        )
        return WorkspacePaths(root=root)

    def fake_prepare_runner(self, workspace: WorkspacePaths):
        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            if "venv" in command:
                workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
                workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            return CommandExecution(exit_code=0)

        return runner

    def ready_probe(self, _workspace: WorkspacePaths) -> tuple[bool, str]:
        return True, "Editable install resolves to the workspace source tree."

    def missing_probe(self, workspace: WorkspacePaths) -> tuple[bool, str]:
        return False, f"Missing virtual environment interpreter at {workspace.venv_python}."

    def test_cli_parser_supports_phase_two_and_three_commands(self) -> None:
        parser = build_parser()
        command_arguments = {
            "prepare": ["prepare", "--json", "--yes"],
            "doctor": ["doctor", "--json"],
            "status": ["status", "--json"],
            "sync": ["sync", "--json", "--yes"],
            "retrieve": ["retrieve", "architecture", "--json"],
            "trace": ["trace", "--source-id", "source-1", "--json"],
            "validate-kb": ["validate-kb", "--json", "--target", "staging"],
            "sync-adapters": ["sync-adapters", "--json", "--target", "claude"],
        }
        for command, argv in command_arguments.items():
            with self.subTest(command=command):
                namespace = parser.parse_args(argv)
                self.assertEqual(namespace.command, command)
                self.assertTrue(namespace.json)

    def test_cli_returns_command_exit_codes(self) -> None:
        reports = {
            "prepare": CommandReport(2, {"status": DEGRADED}, []),
            "doctor": CommandReport(1, {"status": ACTION_REQUIRED}, []),
            "status": CommandReport(2, {"stage": "adapter-ready"}, []),
            "sync": CommandReport(2, {"status": DEGRADED, "sync_status": "pending-synthesis"}, []),
            "retrieve": CommandReport(0, {"status": READY, "retrieve_status": READY}, []),
            "trace": CommandReport(2, {"status": DEGRADED, "trace_mode": "answer-first"}, []),
            "validate-kb": CommandReport(0, {"status": READY, "validation_status": "valid"}, []),
            "sync-adapters": CommandReport(0, {"status": READY, "target": "claude"}, []),
        }
        patch_targets = {
            "prepare": "docmason.cli.prepare_workspace",
            "doctor": "docmason.cli.doctor_workspace",
            "status": "docmason.cli.status_workspace",
            "sync": "docmason.cli.sync_workspace",
            "retrieve": "docmason.cli.retrieve_knowledge",
            "trace": "docmason.cli.trace_knowledge",
            "validate-kb": "docmason.cli.validate_knowledge_base",
            "sync-adapters": "docmason.cli.sync_adapters",
        }
        arguments = {
            "prepare": ["prepare", "--json", "--yes"],
            "doctor": ["doctor", "--json"],
            "status": ["status", "--json"],
            "sync": ["sync", "--json", "--yes"],
            "retrieve": ["retrieve", "architecture", "--json"],
            "trace": ["trace", "--source-id", "source-1", "--json"],
            "validate-kb": ["validate-kb", "--json"],
            "sync-adapters": ["sync-adapters", "--json"],
        }
        for command, target in patch_targets.items():
            with self.subTest(command=command), mock.patch(target, return_value=reports[command]):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(arguments[command])
                self.assertEqual(exit_code, reports[command].exit_code)
                self.assertTrue(buffer.getvalue().strip())

    def test_prepare_uses_pip_fallback_when_uv_is_missing(self) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch("docmason.commands.find_uv_binary", return_value=None),
            mock.patch("docmason.commands.find_brew_binary", return_value=None),
        ):
            report = prepare_workspace(
                workspace,
                assume_yes=False,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )
        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.payload["status"], DEGRADED)
        self.assertEqual(report.payload["environment"]["package_manager"], "pip")
        self.assertTrue(workspace.bootstrap_state_path.exists())
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["environment_ready"])
        self.assertEqual(state["package_manager"], "pip")
        self.assertEqual(state["workspace_root"], str(workspace.root.resolve()))
        self.assertEqual(state["manual_recovery_doc"], "docs/setup/manual-workspace-recovery.md")
        self.assertIn("pip install --user uv", report.payload["next_steps"][0])

    def test_prepare_uses_uv_when_available(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            del cwd
            command_list = list(command)
            seen_commands.append(command_list)
            if "venv" in command_list:
                workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
                workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            return CommandExecution(exit_code=0)

        with mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"):
            report = prepare_workspace(
                workspace,
                command_runner=runner,
                editable_install_probe=self.ready_probe,
                interactive=False,
            )
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertEqual(report.payload["environment"]["package_manager"], "uv")
        self.assertIn("--allow-existing", seen_commands[0])
        self.assertTrue(workspace.agent_work_dir.exists())
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 2)
        self.assertTrue(state["environment_ready"])
        self.assertEqual(state["package_manager"], "uv")
        self.assertEqual(state["workspace_root"], str(workspace.root.resolve()))

    def test_cached_bootstrap_readiness_detects_workspace_root_drift(self) -> None:
        workspace = self.make_workspace()
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "schema_version": 2,
                "status": "ready",
                "environment_ready": True,
                "workspace_root": "/tmp/old-docmason-root",
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
                "editable_install_detail": (
                    "Editable install resolves to the workspace source tree."
                ),
                "office_renderer_ready": True,
                "pdf_renderer_ready": True,
                "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
            },
        )

        readiness = cached_bootstrap_readiness(workspace)
        self.assertFalse(readiness["ready"])
        self.assertEqual(readiness["reason"], "workspace-root-drift")

    def test_cached_bootstrap_readiness_requires_current_contract_for_sync_capability(self) -> None:
        workspace = self.make_workspace()
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        (workspace.source_dir / "example.docx").write_text("docx placeholder\n", encoding="utf-8")
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

        ordinary = cached_bootstrap_readiness(workspace)
        sync_ready = cached_bootstrap_readiness(workspace, require_sync_capability=True)
        self.assertTrue(ordinary["ready"])
        self.assertFalse(sync_ready["ready"])
        self.assertEqual(sync_ready["reason"], "legacy-bootstrap-state-sync-capability-unknown")

    def test_prepare_prefers_homebrew_for_uv_on_macos(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            del cwd
            command_list = list(command)
            seen_commands.append(command_list)
            if command_list[:3] == ["/opt/homebrew/bin/brew", "install", "uv"]:
                return CommandExecution(exit_code=0)
            if "venv" in command_list:
                workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
                workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            return CommandExecution(exit_code=0)

        with (
            mock.patch(
                "docmason.commands.find_uv_binary",
                side_effect=[None, "/opt/homebrew/bin/uv"],
            ),
            mock.patch(
                "docmason.commands.find_brew_binary",
                return_value="/opt/homebrew/bin/brew",
            ),
            mock.patch("docmason.commands.sys.platform", "darwin"),
        ):
            report = prepare_workspace(
                workspace,
                assume_yes=True,
                command_runner=runner,
                editable_install_probe=self.ready_probe,
                interactive=False,
            )
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertIn(["/opt/homebrew/bin/brew", "install", "uv"], seen_commands)

    def test_prepare_fails_on_unsupported_environment(self) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch("docmason.commands.platform_supported", return_value=False),
            mock.patch("docmason.commands.python_supported", return_value=False),
        ):
            report = prepare_workspace(
                workspace,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)

    def test_prepare_reports_office_renderer_follow_up_without_brew(self) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                return_value={
                    "required": True,
                    "ready": False,
                    "detail": "LibreOffice `soffice` is required but unavailable.",
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value=None),
            mock.patch("docmason.commands.sys.platform", "darwin"),
        ):
            report = prepare_workspace(
                workspace,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["prepare_status"], "awaiting-confirmation")
        self.assertEqual(
            report.payload["control_plane"]["confirmation_kind"],
            "high-intrusion-prepare",
        )
        self.assertIn("是否现在开始准备环境", report.payload["control_plane"]["confirmation_prompt"])
        self.assertEqual(
            report.payload["next_steps"][0],
            "Run `docmason prepare --yes` to approve and continue.",
        )

    def test_prepare_auto_installs_libreoffice_with_brew_when_assume_yes(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            del cwd
            command_list = list(command)
            seen_commands.append(command_list)
            if "venv" in command_list:
                workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
                workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            return CommandExecution(exit_code=0)

        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                side_effect=[
                    {
                        "required": True,
                        "ready": False,
                        "detail": "LibreOffice `soffice` is required but unavailable.",
                    },
                    {
                        "required": True,
                        "ready": True,
                        "detail": "LibreOffice rendering is available.",
                    },
                ],
            ),
            mock.patch(
                "docmason.commands.find_brew_binary",
                return_value="/opt/homebrew/bin/brew",
            ),
            mock.patch("docmason.commands.sys.platform", "darwin"),
        ):
            report = prepare_workspace(
                workspace,
                assume_yes=True,
                command_runner=runner,
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertIn(
            ["/opt/homebrew/bin/brew", "install", "--cask", "libreoffice-still"],
            seen_commands,
        )

    def test_prepare_generates_repo_local_skill_shims(self) -> None:
        workspace = self.make_workspace()

        report = prepare_workspace(
            workspace,
            command_runner=self.fake_prepare_runner(workspace),
            editable_install_probe=self.ready_probe,
            interactive=False,
        )

        self.assertIn(
            "Refreshed repo-local skill shims under .agents/skills and .claude/skills.",
            report.payload["actions_performed"],
        )
        codex_shim = workspace.repo_skill_shim_dir / "workspace-bootstrap"
        claude_shim = workspace.claude_skill_shim_dir / "workspace-bootstrap"
        self.assertTrue(codex_shim.is_symlink())
        self.assertTrue(claude_shim.is_symlink())
        expected = (workspace.canonical_skills_dir / "workspace-bootstrap").resolve()
        self.assertEqual(codex_shim.resolve(), expected)
        self.assertEqual(claude_shim.resolve(), expected)

    def test_prepare_replaces_legacy_claude_skills_symlink(self) -> None:
        workspace = self.make_workspace()
        workspace.claude_skill_shim_dir.parent.mkdir(parents=True, exist_ok=True)
        os.symlink("../skills", workspace.claude_skill_shim_dir)

        report = prepare_workspace(
            workspace,
            command_runner=self.fake_prepare_runner(workspace),
            editable_install_probe=self.ready_probe,
            interactive=False,
        )

        self.assertEqual(report.exit_code, 0)
        self.assertFalse(workspace.claude_skill_shim_dir.is_symlink())
        self.assertTrue((workspace.claude_skill_shim_dir / "workspace-bootstrap").is_symlink())

    def test_prepare_exposes_optional_public_sample_skill_only_when_manifest_exists(self) -> None:
        workspace = self.make_workspace()
        sample_manifest = workspace.root / "sample_corpus" / "ico-gcs" / "manifest.json"
        sample_manifest.parent.mkdir(parents=True, exist_ok=True)
        sample_manifest.write_text('{"corpus_id": "ico-gcs"}\n', encoding="utf-8")
        optional_skill = (
            workspace.root / "skills" / "optional" / "public-sample-workspace" / "SKILL.md"
        )
        optional_skill.parent.mkdir(parents=True, exist_ok=True)
        optional_skill.write_text("# Public Sample Workspace\n", encoding="utf-8")

        report = prepare_workspace(
            workspace,
            command_runner=self.fake_prepare_runner(workspace),
            editable_install_probe=self.ready_probe,
            interactive=False,
        )

        self.assertEqual(report.exit_code, 0)
        codex_shim = workspace.repo_skill_shim_dir / "public-sample-workspace"
        claude_shim = workspace.claude_skill_shim_dir / "public-sample-workspace"
        self.assertTrue(codex_shim.is_symlink())
        self.assertTrue(claude_shim.is_symlink())
        self.assertEqual(codex_shim.resolve(), optional_skill.parent.resolve())

    def test_prepare_auto_installs_homebrew_before_managed_libreoffice_when_feasible(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            del cwd
            command_list = list(command)
            seen_commands.append(command_list)
            if "venv" in command_list:
                workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
                workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            return CommandExecution(exit_code=0)

        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                side_effect=[
                    {
                        "required": True,
                        "ready": False,
                        "detail": "LibreOffice `soffice` is required but unavailable.",
                    },
                    {
                        "required": True,
                        "ready": True,
                        "detail": "LibreOffice rendering is available.",
                    },
                ],
            ),
            mock.patch(
                "docmason.commands.homebrew_auto_install_plan",
                return_value={
                    "feasible": True,
                    "detail": "The official unattended Homebrew install path is available.",
                    "expected_brew": "/opt/homebrew/bin/brew",
                    "install_command": [
                        "/usr/bin/env",
                        "NONINTERACTIVE=1",
                        "/bin/bash",
                        "-c",
                        '"$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                    ],
                    "install_display": "official install",
                },
            ),
            mock.patch(
                "docmason.commands.refresh_brew_binary_after_install",
                return_value="/opt/homebrew/bin/brew",
            ),
            mock.patch(
                "docmason.commands.preferred_libreoffice_install_command",
                return_value=(
                    ["/opt/homebrew/bin/brew", "install", "--cask", "libreoffice-still"],
                    "`brew install --cask libreoffice-still`",
                ),
            ),
            mock.patch(
                "docmason.commands.find_brew_binary",
                return_value=None,
            ),
            mock.patch("docmason.commands.sys.platform", "darwin"),
        ):
            report = prepare_workspace(
                workspace,
                assume_yes=True,
                command_runner=runner,
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertIn(
            [
                "/usr/bin/env",
                "NONINTERACTIVE=1",
                "/bin/bash",
                "-c",
                '"$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
            ],
            seen_commands,
        )
        self.assertIn(
            ["/opt/homebrew/bin/brew", "install", "--cask", "libreoffice-still"],
            seen_commands,
        )

    def test_doctor_reports_blockers_before_prepare(self) -> None:
        workspace = self.make_workspace()
        report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        statuses = {check["name"]: check["status"] for check in report.payload["checks"]}
        self.assertEqual(statuses["bootstrap-state"], DEGRADED)
        self.assertEqual(statuses["venv"], ACTION_REQUIRED)
        self.assertEqual(statuses["editable-install"], ACTION_REQUIRED)
        self.assertEqual(statuses["source-corpus"], DEGRADED)
        self.assertEqual(statuses["claude-adapter"], READY)

    def test_doctor_keeps_manual_recovery_hidden_for_routine_prepare_issues(self) -> None:
        workspace = self.make_workspace()
        report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertNotIn(
            (
                "Follow `docs/setup/manual-workspace-recovery.md` for the "
                "manual workspace bootstrap and repair fallback."
            ),
            report.payload["next_steps"],
        )

    def test_doctor_offers_official_libreoffice_install_path_without_brew(self) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                return_value={
                    "required": True,
                    "ready": False,
                    "detail": "LibreOffice `soffice` is required but unavailable.",
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value=None),
            mock.patch("docmason.commands.sys.platform", "darwin"),
        ):
            report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)
        office_check = next(
            check for check in report.payload["checks"] if check["name"] == "office-renderer"
        )
        self.assertEqual(office_check["status"], ACTION_REQUIRED)
        self.assertIn("libreoffice.org/download/download/", office_check["action"])

    def test_status_stage_progression_and_pending_actions(self) -> None:
        workspace = self.make_workspace()
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")

        foundation = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(foundation.payload["stage"], "foundation-only")
        self.assertEqual(
            foundation.payload["pending_actions"],
            ["prepare", "sync"],
        )

        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "prepared_at": "2026-03-15T00:00:00Z",
                "package_manager": "pip",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
            },
        )
        bootstrapped = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(bootstrapped.payload["stage"], "workspace-bootstrapped")
        self.assertEqual(
            bootstrapped.payload["bootstrap_state"]["reason"], "legacy-compatible-ready"
        )
        self.assertTrue(bootstrapped.payload["bootstrap_state"]["cached_ready"])

        adapter_report = sync_adapters(workspace)
        self.assertEqual(adapter_report.exit_code, 0)
        adapter_ready = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(adapter_ready.payload["stage"], "adapter-ready")

        kb_file = workspace.knowledge_base_current_dir / "artifact.md"
        kb_file.parent.mkdir(parents=True, exist_ok=True)
        kb_file.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-15T01:00:00Z",
                "last_sync_at": "2026-03-15T01:00:00Z",
            },
        )
        kb_present = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(kb_present.payload["stage"], "knowledge-base-present")

        os.utime(source_file, None)
        kb_stale = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(kb_stale.payload["stage"], "knowledge-base-stale")

    def test_sync_reports_material_confirmation_payload(self) -> None:
        workspace = self.make_workspace()
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "schema_version": 2,
                "status": "ready",
                "environment_ready": True,
                "prepared_at": "2026-03-15T00:00:00Z",
                "workspace_root": str(workspace.root.resolve()),
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
                "editable_install_detail": "Editable install resolves to the workspace source tree.",
                "office_renderer_ready": True,
                "pdf_renderer_ready": True,
                "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
            },
        )
        for index in range(12):
            (workspace.source_dir / f"sample-{index:02d}.pdf").write_text(
                "pdf placeholder\n",
                encoding="utf-8",
            )

        report = sync_workspace(workspace)

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["sync_status"], "awaiting-confirmation")
        self.assertEqual(
            report.payload["control_plane"]["confirmation_kind"],
            "material-sync",
        )
        self.assertEqual(
            report.payload["control_plane"]["next_command"],
            "docmason sync --yes",
        )

    def test_status_and_doctor_surface_pending_control_plane_confirmation(self) -> None:
        workspace = self.make_workspace()
        ensure_shared_job(
            workspace,
            job_key=f"prepare:{workspace.root}:high-intrusion:cap",
            job_family="prepare",
            criticality="answer-critical",
            scope={"workspace_root": str(workspace.root)},
            input_signature="cap",
            owner={"kind": "command", "id": "prepare-command"},
            requires_confirmation=True,
            confirmation_kind="high-intrusion-prepare",
            confirmation_prompt="当前问题需要补齐本地依赖才能继续，是否现在开始准备环境？",
            confirmation_reason="office-rendering",
        )

        status_report = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(status_report.payload["stage"], "control-plane-pending-confirmation")
        self.assertIn("prepare --yes", status_report.payload["pending_actions"])

        doctor_report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)
        checks = {check["name"]: check for check in doctor_report.payload["checks"]}
        self.assertEqual(checks["control-plane"]["status"], ACTION_REQUIRED)

    def test_shared_job_acquisition_distinguishes_owner_and_waiter(self) -> None:
        workspace = self.make_workspace()
        owner_run_id = "run-owner"
        waiter_run_id = "run-waiter"

        first = ensure_shared_job(
            workspace,
            job_key="sync:test-signature",
            job_family="sync",
            criticality="answer-critical",
            scope={"target": "current"},
            input_signature="test-signature",
            owner={"kind": "command", "id": "sync-command:1"},
            run_id=owner_run_id,
        )
        second = ensure_shared_job(
            workspace,
            job_key="sync:test-signature",
            job_family="sync",
            criticality="answer-critical",
            scope={"target": "current"},
            input_signature="test-signature",
            owner={"kind": "command", "id": "sync-command:2"},
            run_id=waiter_run_id,
        )

        self.assertEqual(first["caller_role"], "owner")
        self.assertEqual(second["caller_role"], "waiter")
        self.assertEqual(first["manifest"]["job_id"], second["manifest"]["job_id"])
        manifest = load_shared_job(workspace, str(first["manifest"]["job_id"]))
        self.assertCountEqual(manifest["attached_run_ids"], [owner_run_id, waiter_run_id])

    def test_sync_adapters_generates_deterministic_claude_files(self) -> None:
        workspace = self.make_workspace()
        (workspace.canonical_skills_dir / "workspace-doctor").mkdir(parents=True)
        (workspace.canonical_skills_dir / "workspace-doctor" / "SKILL.md").write_text(
            "# Workspace Doctor\n",
            encoding="utf-8",
        )
        self.seed_workflow_metadata(
            workspace.canonical_skills_dir / "workspace-doctor",
            workflow_id="workspace-doctor",
            category="foundation",
            mutability="read-only",
            parallelism="read-only-safe",
            background_commands=["docmason doctor --json"],
        )

        report = sync_adapters(workspace)
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["target"], "claude")
        project_memory_text = workspace.claude_project_memory_path.read_text(encoding="utf-8")
        workflow_routing_text = workspace.claude_workflow_routing_path.read_text(encoding="utf-8")
        self.assertFalse((workspace.root / "CLAUDE.md").exists())
        self.assertIn("@workflow-routing.md", project_memory_text)
        self.assertIn("@../../skills/canonical/workspace-bootstrap/SKILL.md", project_memory_text)
        self.assertIn("@../../skills/canonical/workspace-doctor/SKILL.md", project_memory_text)
        self.assertIn("## Foundation Workflows", workflow_routing_text)
        self.assertIn("### `workspace-bootstrap`", workflow_routing_text)
        self.assertIn("### `workspace-doctor`", workflow_routing_text)
        self.assertIn("adapters/claude/project-memory.md", report.payload["generated_files"])
        self.assertIn("adapters/claude/workflow-routing.md", report.payload["generated_files"])
        self.assertIn(".claude/skills", report.payload["generated_files"])
        self.assertNotIn("CLAUDE.md", report.payload["generated_files"])
        self.assertTrue((workspace.claude_skill_shim_dir / "workspace-bootstrap").is_symlink())

        before = workspace.claude_project_memory_path.stat().st_mtime
        updated_sidecar = workspace.canonical_skills_dir / "workspace-bootstrap" / "workflow.json"
        os.utime(updated_sidecar, (before + 10, before + 10))
        stale_status = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(
            stale_status.payload["adapters"]["claude"]["path"],
            "adapters/claude/project-memory.md",
        )
        self.assertFalse(stale_status.payload["adapters"]["claude"]["skill_shims_required"])
        self.assertTrue(stale_status.payload["adapters"]["claude"]["stale"])

    def test_operator_workflow_metadata_does_not_mark_claude_adapter_stale(self) -> None:
        workspace = self.make_workspace()
        operator_dir = workspace.operator_skills_dir / "operator-eval"
        operator_dir.mkdir(parents=True)
        (operator_dir / "SKILL.md").write_text("# Operator Eval\n", encoding="utf-8")
        self.seed_workflow_metadata(
            operator_dir,
            workflow_id="operator-eval",
            category="review",
            mutability="workspace-write",
            parallelism="none",
            background_commands=["docmason workflow operator-eval --json"],
        )

        report = sync_adapters(workspace)
        self.assertEqual(report.exit_code, 0)

        before = workspace.claude_project_memory_path.stat().st_mtime
        updated_sidecar = operator_dir / "workflow.json"
        os.utime(updated_sidecar, (before + 10, before + 10))
        stale_status = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertFalse(stale_status.payload["adapters"]["claude"]["stale"])

    def test_missing_claude_skill_shims_does_not_block_adapter_ready(self) -> None:
        workspace = self.make_workspace()
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "prepared_at": "2026-03-15T00:00:00Z",
                "package_manager": "pip",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
            },
        )
        report = sync_adapters(workspace)
        self.assertEqual(report.exit_code, 0)

        shutil.rmtree(workspace.claude_skill_shim_dir)

        status_report = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(status_report.payload["stage"], "adapter-ready")
        self.assertTrue(status_report.payload["adapters"]["claude"]["present"])
        self.assertFalse(status_report.payload["adapters"]["claude"]["stale"])
        self.assertFalse(status_report.payload["adapters"]["claude"]["skill_shims_present"])

        doctor_report = doctor_workspace(workspace, editable_install_probe=self.ready_probe)
        checks = {check["name"]: check for check in doctor_report.payload["checks"]}
        self.assertEqual(checks["claude-adapter"]["status"], READY)
        self.assertEqual(checks["claude-native-skill-shims"]["status"], DEGRADED)
        self.assertIn("sync-adapters", checks["claude-native-skill-shims"]["action"])

    def test_sync_adapters_rejects_invalid_workflow_metadata(self) -> None:
        workspace = self.make_workspace()
        (workspace.canonical_skills_dir / "workspace-bootstrap" / "workflow.json").write_text(
            json.dumps({"schema_version": 1, "workflow_id": "workspace-bootstrap"}) + "\n",
            encoding="utf-8",
        )
        report = sync_adapters(workspace)
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertIn("missing required fields", report.payload["detail"])

    def test_sync_adapters_rejects_unsupported_targets(self) -> None:
        workspace = self.make_workspace()
        report = sync_adapters(workspace, target="copilot")
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)


if __name__ == "__main__":
    unittest.main()

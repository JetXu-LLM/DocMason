"""Workspace command surface tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import stat
import sys
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
from docmason.coordination import LeaseConflictError
from docmason.project import (
    WorkspacePaths,
    cached_bootstrap_readiness,
    source_inventory_signature,
    write_json,
)
from docmason.toolchain import inspect_toolchain


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
            del cwd
            command_list = list(command)
            if command_list[:3] == [sys.executable, "-m", "venv"]:
                workspace.toolchain_bootstrap_python.parent.mkdir(parents=True, exist_ok=True)
                workspace.toolchain_bootstrap_python.write_text(
                    "#!/bin/sh\n"
                    f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                    encoding="utf-8",
                )
                workspace.toolchain_bootstrap_python.chmod(0o755)
            if "python" in command_list and "install" in command_list:
                self.seed_repo_local_managed_python(workspace)
            if "venv" in command:
                managed_python = self.seed_repo_local_managed_python(workspace)
                self.seed_repo_local_venv(workspace, managed_python=managed_python)
            if command_list[:2] == [str(workspace.venv_python), "-c"]:
                if "pdf_renderer_snapshot" in str(command_list[-1]):
                    return CommandExecution(
                        exit_code=0,
                        stdout=json.dumps(
                            {
                                "ready": True,
                                "detail": "PDF rendering and extraction dependencies are available.",
                                "missing": [],
                            }
                        ),
                    )
                return CommandExecution(exit_code=0)
            if command_list[:4] == [
                str(workspace.venv_python),
                "-m",
                "docmason",
                "status",
            ]:
                return CommandExecution(exit_code=0, stdout='{"status": "ready"}')
            if command_list[:2] == [str(workspace.venv_docmason), "--help"]:
                return CommandExecution(exit_code=0, stdout="DocMason CLI")
            if command_list[:4] == [
                str(workspace.toolchain_bootstrap_python),
                "-m",
                "pip",
                "install",
            ]:
                workspace.toolchain_bootstrap_uv.parent.mkdir(parents=True, exist_ok=True)
                workspace.toolchain_bootstrap_uv.write_text(
                    "#!/bin/sh\n"
                    f"exec {shlex.quote(sys.executable)} -m uv \"$@\"\n",
                    encoding="utf-8",
                )
                workspace.toolchain_bootstrap_uv.chmod(0o755)
            return CommandExecution(exit_code=0)

        return runner

    def seed_repo_local_managed_python(
        self,
        workspace: WorkspacePaths,
        *,
        version: str = "3.13.5",
    ) -> Path:
        minor_version = ".".join(version.split(".")[:2])
        install_root = workspace.toolchain_python_installs_dir / f"cpython-{version}"
        python_path = install_root / "bin" / f"python{minor_version}"
        python_path.parent.mkdir(parents=True, exist_ok=True)
        python_path.write_text(
            "#!/bin/sh\n"
            f"export PYTHONPATH={shlex.quote(str(workspace.root / 'src'))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        python_path.chmod(python_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        workspace.toolchain_python_current_dir.parent.mkdir(parents=True, exist_ok=True)
        if workspace.toolchain_python_current_dir.exists() or workspace.toolchain_python_current_dir.is_symlink():
            if workspace.toolchain_python_current_dir.is_dir() and not workspace.toolchain_python_current_dir.is_symlink():
                shutil.rmtree(workspace.toolchain_python_current_dir)
            else:
                workspace.toolchain_python_current_dir.unlink()
        os.symlink(
            os.path.relpath(install_root, workspace.toolchain_python_current_dir.parent),
            workspace.toolchain_python_current_dir,
        )
        return python_path

    def seed_repo_local_venv(
        self,
        workspace: WorkspacePaths,
        *,
        managed_python: Path | None = None,
        version: str = "3.13.5",
    ) -> None:
        managed = managed_python or self.seed_repo_local_managed_python(workspace, version=version)
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        if workspace.venv_python.exists() or workspace.venv_python.is_symlink():
            workspace.venv_python.unlink()
        os.symlink(os.path.relpath(managed, workspace.venv_python.parent), workspace.venv_python)
        workspace.venv_docmason.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_docmason.write_text(
            "#!/bin/sh\n"
            "printf 'DocMason CLI\\n'\n",
            encoding="utf-8",
        )
        workspace.venv_docmason.chmod(0o755)
        workspace.venv_pyvenv_cfg.write_text(
            f"home = {managed.parent}\nversion = {version}\n",
            encoding="utf-8",
        )

    def seed_external_python(
        self,
        workspace: WorkspacePaths,
        *,
        name: str = "python3",
    ) -> str:
        external_python = workspace.root / ".external-python" / "bin" / name
        external_python.parent.mkdir(parents=True, exist_ok=True)
        external_python.write_text(
            "#!/bin/sh\n"
            f"export PYTHONPATH={shlex.quote(str(workspace.root / 'src'))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        external_python.chmod(
            external_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
        return str(external_python)

    def seed_external_venv(self, workspace: WorkspacePaths, *, external_python: str) -> None:
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        if workspace.venv_python.exists() or workspace.venv_python.is_symlink():
            workspace.venv_python.unlink()
        os.symlink(external_python, workspace.venv_python)
        workspace.venv_docmason.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_docmason.write_text(
            "#!/bin/sh\n"
            "printf 'DocMason CLI\\n'\n",
            encoding="utf-8",
        )
        workspace.venv_docmason.chmod(0o755)
        workspace.venv_pyvenv_cfg.write_text(
            f"home = {Path(external_python).parent}\nversion = 3.11.0\n",
            encoding="utf-8",
        )

    def seed_self_contained_bootstrap_state(
        self,
        workspace: WorkspacePaths,
        *,
        package_manager: str = "uv",
        office_renderer_ready: bool = True,
        pdf_renderer_ready: bool = True,
    ) -> None:
        managed_python = self.seed_repo_local_managed_python(workspace)
        self.seed_repo_local_venv(workspace, managed_python=managed_python)
        write_json(
            workspace.bootstrap_state_path,
            {
                "schema_version": 3,
                "status": "ready",
                "environment_ready": True,
                "checked_at": "2026-03-25T00:00:00Z",
                "prepared_at": "2026-03-25T00:00:00Z",
                "workspace_root": str(workspace.root.resolve()),
                "package_manager": package_manager,
                "python_executable": str(managed_python),
                "venv_python": ".venv/bin/python",
                "editable_install": True,
                "editable_install_detail": "Editable install resolves to the workspace source tree.",
                "python_baseline": "3.13",
                "toolchain_root": ".docmason/toolchain",
                "toolchain_mode": "repo-local-managed",
                "managed_python_executable": str(managed_python),
                "managed_python_version": "3.13.5",
                "managed_python_origin": "repo-local-managed",
                "venv_base_executable": str(managed_python),
                "venv_health": "ready",
                "entrypoint_health": "ready",
                "uv_bootstrap_mode": "shared-uv",
                "uv_cache_dir": ".docmason/toolchain/cache/uv",
                "pip_cache_dir": ".docmason/toolchain/cache/pip",
                "isolation_grade": "self-contained",
                "shared_host_dependency": False,
                "shared_host_dependencies": [],
                "repair_recommended": False,
                "repair_reason": None,
                "last_repair_at": "2026-03-25T00:00:00Z",
                "pdf_renderer_ready": pdf_renderer_ready,
                "office_renderer_ready": office_renderer_ready,
                "office_renderer_required": False,
                "requires_pdf_renderer": False,
                "requires_office_renderer": False,
                "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
            },
        )

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

    def test_prepare_requests_repo_local_uv_bootstrap_when_uv_is_missing(self) -> None:
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
        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["environment"]["package_manager"], "uv")
        self.assertIn("repo-local bootstrap helper", report.payload["next_steps"][0])

    def test_prepare_uses_uv_when_available(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, cwd)

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
        self.assertTrue(
            any(
                command[:3] == ["/usr/local/bin/uv", "python", "install"]
                for command in seen_commands
            )
        )
        self.assertTrue(workspace.agent_work_dir.exists())
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], 3)
        self.assertTrue(state["environment_ready"])
        self.assertEqual(state["package_manager"], "uv")
        self.assertEqual(state["workspace_root"], str(workspace.root.resolve()))
        self.assertEqual(state["toolchain_mode"], "repo-local-managed")
        self.assertEqual(state["isolation_grade"], "self-contained")

    def test_prepare_reuses_repo_local_bootstrap_uv_before_reinstalling(self) -> None:
        workspace = self.make_workspace()
        workspace.toolchain_bootstrap_uv.parent.mkdir(parents=True, exist_ok=True)
        workspace.toolchain_bootstrap_uv.write_text(
            "#!/bin/sh\nexit 0\n",
            encoding="utf-8",
        )
        workspace.toolchain_bootstrap_uv.chmod(0o755)
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, cwd)

        with mock.patch("docmason.commands.find_uv_binary", return_value=str(workspace.toolchain_bootstrap_uv)):
            report = prepare_workspace(
                workspace,
                command_runner=runner,
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertTrue(
            any(
                command[:3] == [str(workspace.toolchain_bootstrap_uv), "python", "install"]
                for command in seen_commands
            )
        )
        self.assertFalse(
            any(
                command[:4]
                == [str(workspace.toolchain_bootstrap_python), "-m", "pip", "install"]
                for command in seen_commands
            )
        )
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["uv_bootstrap_mode"], "bootstrap-venv-reused")

    def test_prepare_reports_managed_python_version_instead_of_bootstrap_version(self) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch("docmason.commands.sys.version_info", (3, 11, 9)),
        ):
            report = prepare_workspace(
                workspace,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["environment"]["python_version"], "3.13.5")
        self.assertTrue(
            str(report.payload["environment"]["python_executable"]).endswith("/python3.13")
        )

    def test_prepare_uses_active_entrypoint_probe_instead_of_status_json(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, cwd)

        with mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"):
            report = prepare_workspace(
                workspace,
                command_runner=runner,
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertFalse(
            any(
                command[:4]
                == [str(workspace.venv_python), "-m", "docmason", "status"]
                for command in seen_commands
            )
        )
        self.assertEqual(
            report.payload["environment"]["toolchain"]["isolation_grade"],
            "self-contained",
        )

    def test_inspect_toolchain_detects_baseline_version_drift_without_bootstrap_state(self) -> None:
        workspace = self.make_workspace()
        managed_python = self.seed_repo_local_managed_python(workspace, version="3.14.2")
        self.seed_repo_local_venv(
            workspace,
            managed_python=managed_python,
            version="3.14.2",
        )

        toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["managed_python_version"], "3.14.2")
        self.assertEqual(toolchain["toolchain_mode"], "repo-local-managed")
        self.assertEqual(toolchain["repair_reason"], "baseline-version-drift")
        self.assertEqual(toolchain["isolation_grade"], "mixed")

    def test_inspect_toolchain_prefers_install_root_version_over_stale_cached_version(self) -> None:
        workspace = self.make_workspace()
        managed_python = self.seed_repo_local_managed_python(workspace, version="3.14.2")
        self.seed_repo_local_venv(
            workspace,
            managed_python=managed_python,
            version="3.14.2",
        )
        cached_state = {
            "managed_python_executable": str(managed_python),
            "managed_python_version": "3.13.5",
        }

        toolchain = inspect_toolchain(
            workspace,
            bootstrap_state=cached_state,
            editable_install=True,
        )

        self.assertEqual(toolchain["managed_python_version"], "3.14.2")
        self.assertEqual(toolchain["repair_reason"], "baseline-version-drift")

    def test_inspect_toolchain_marks_missing_entrypoint_as_module_import_failed(self) -> None:
        workspace = self.make_workspace()
        self.seed_repo_local_venv(workspace)
        workspace.venv_docmason.unlink()

        toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["entrypoint_health"], "module-import-failed")
        self.assertEqual(toolchain["repair_reason"], "entrypoint-broken")

    def test_inspect_toolchain_marks_invalid_shebang_as_broken(self) -> None:
        workspace = self.make_workspace()
        self.seed_repo_local_venv(workspace)
        workspace.venv_docmason.write_text("print('no shebang')\n", encoding="utf-8")
        workspace.venv_docmason.chmod(0o755)

        toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["entrypoint_health"], "broken-shebang")
        self.assertEqual(toolchain["repair_reason"], "entrypoint-broken")

    def test_inspect_toolchain_marks_silent_entrypoint_as_startup_silent(self) -> None:
        workspace = self.make_workspace()
        self.seed_repo_local_venv(workspace)
        workspace.venv_docmason.write_text(
            f"#!{sys.executable}\n",
            encoding="utf-8",
        )
        workspace.venv_docmason.chmod(0o755)

        toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["entrypoint_health"], "startup-silent")
        self.assertEqual(toolchain["repair_reason"], "entrypoint-broken")

    def test_inspect_toolchain_marks_import_failure_as_module_import_failed(self) -> None:
        workspace = self.make_workspace()
        managed_python = self.seed_repo_local_managed_python(workspace)
        self.seed_repo_local_venv(workspace, managed_python=managed_python)
        managed_python.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -S \"$@\"\n",
            encoding="utf-8",
        )
        managed_python.chmod(
            managed_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

        toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["entrypoint_health"], "module-import-failed")
        self.assertEqual(toolchain["repair_reason"], "entrypoint-broken")

    def test_inspect_toolchain_marks_timeout_entrypoint_as_startup_silent(self) -> None:
        workspace = self.make_workspace()
        self.seed_repo_local_venv(workspace)
        workspace.venv_docmason.write_text(
            f"#!{sys.executable}\n"
            "import time\n"
            "time.sleep(1.0)\n",
            encoding="utf-8",
        )
        workspace.venv_docmason.chmod(0o755)

        with mock.patch("docmason.toolchain.ENTRYPOINT_PROBE_TIMEOUT_SECONDS", 0.5):
            toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["entrypoint_health"], "startup-silent")
        self.assertEqual(toolchain["repair_reason"], "entrypoint-broken")

    def test_inspect_toolchain_distinguishes_shared_host_bootstrap_from_legacy_external(self) -> None:
        workspace = self.make_workspace()

        shared_host_toolchain = inspect_toolchain(workspace, editable_install=False)
        self.assertEqual(shared_host_toolchain["toolchain_mode"], "shared-host-bootstrap")
        self.assertTrue(shared_host_toolchain["shared_host_dependency"])

        external_python = self.seed_external_python(workspace)
        self.seed_repo_local_managed_python(workspace)
        self.seed_external_venv(workspace, external_python=external_python)

        external_toolchain = inspect_toolchain(workspace, editable_install=True)
        self.assertEqual(external_toolchain["toolchain_mode"], "legacy-external")
        self.assertEqual(external_toolchain["repair_reason"], "external-venv-provenance")

    def test_prepare_records_pdf_renderer_readiness_from_repo_local_venv_probe(self) -> None:
        workspace = self.make_workspace()

        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch(
                "docmason.commands.pdf_renderer_snapshot",
                return_value={
                    "ready": False,
                    "detail": "Current bootstrap interpreter lacks PDF dependencies.",
                    "missing": ["pymupdf"],
                },
            ),
        ):
            report = prepare_workspace(
                workspace,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 0)
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["pdf_renderer_ready"])

    def test_cached_bootstrap_readiness_detects_workspace_root_drift(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        state["workspace_root"] = "/tmp/old-docmason-root"
        write_json(workspace.bootstrap_state_path, state)

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
        self.assertFalse(ordinary["ready"])
        self.assertFalse(sync_ready["ready"])
        self.assertEqual(sync_ready["reason"], "legacy-bootstrap-state-sync-capability-unknown")

    def test_prepare_prefers_homebrew_for_uv_on_macos(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            del cwd
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, workspace.root)

        with (
            mock.patch("docmason.commands.find_uv_binary", return_value=None),
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
        self.assertTrue(
            any(
                command[:4]
                == [str(workspace.toolchain_bootstrap_python), "-m", "pip", "install"]
                for command in seen_commands
            )
        )

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
        self.assertIn("Prepare the workspace now?", report.payload["control_plane"]["confirmation_prompt"])
        self.assertEqual(
            report.payload["next_steps"][0],
            "Run `docmason prepare --yes` to approve and continue.",
        )

    def test_prepare_auto_installs_libreoffice_with_brew_when_assume_yes(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, cwd)

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
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, cwd)

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

    def test_doctor_reports_repo_local_uv_repair_guidance(self) -> None:
        workspace = self.make_workspace()
        with mock.patch("docmason.commands.find_uv_binary", return_value=None):
            report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)
        uv_check = next(check for check in report.payload["checks"] if check["name"] == "uv")
        self.assertIn("repo-local bootstrap helper", uv_check["detail"])
        self.assertNotIn("fall back to venv + pip", uv_check["detail"])

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

    def test_status_survives_reconciliation_lease_conflict_with_warning(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)

        with mock.patch(
            "docmason.commands._maybe_reconcile_active_thread",
            side_effect=LeaseConflictError(
                "Could not acquire workspace lease for `conversation:thread-status` within 10.0s."
            ),
        ):
            report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        self.assertIn("coordination", report.payload)
        self.assertEqual(report.payload["coordination"]["state"], "warning")
        self.assertTrue(report.lines)

    def test_doctor_survives_reconciliation_lease_conflict_with_warning(self) -> None:
        workspace = self.make_workspace()

        with mock.patch(
            "docmason.commands._maybe_reconcile_active_thread",
            side_effect=LeaseConflictError(
                "Could not acquire workspace lease for `conversation:thread-doctor` within 10.0s."
            ),
        ):
            report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)

        self.assertIn("coordination", report.payload)
        self.assertEqual(report.payload["coordination"]["state"], "warning")
        self.assertTrue(report.lines)

    def test_prepare_returns_structured_coordination_block_instead_of_traceback(self) -> None:
        workspace = self.make_workspace()

        with mock.patch(
            "docmason.commands._maybe_reconcile_active_thread",
            side_effect=LeaseConflictError(
                "Could not acquire workspace lease for `conversation:thread-prepare` within 10.0s."
            ),
        ):
            report = prepare_workspace(
                workspace,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["prepare_status"], "coordination-blocked")
        self.assertEqual(report.payload["coordination"]["state"], "blocked")

    def test_sync_returns_structured_coordination_block_instead_of_traceback(self) -> None:
        workspace = self.make_workspace()

        with mock.patch(
            "docmason.commands._maybe_reconcile_active_thread",
            side_effect=LeaseConflictError(
                "Could not acquire workspace lease for `conversation:thread-sync` within 10.0s."
            ),
        ):
            report = sync_workspace(workspace)

        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["sync_status"], "coordination-blocked")
        self.assertEqual(report.payload["coordination"]["state"], "blocked")

    def test_status_stage_progression_and_pending_actions(self) -> None:
        workspace = self.make_workspace()
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")

        foundation = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(foundation.exit_code, 1)
        self.assertEqual(foundation.payload["stage"], "foundation-only")
        self.assertEqual(
            foundation.payload["pending_actions"],
            ["prepare", "sync"],
        )

        self.seed_self_contained_bootstrap_state(workspace, package_manager="uv")
        bootstrapped = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(bootstrapped.exit_code, 0)
        self.assertEqual(bootstrapped.payload["stage"], "workspace-bootstrapped")
        self.assertEqual(bootstrapped.payload["bootstrap_state"]["reason"], "cached-ready")
        self.assertTrue(bootstrapped.payload["bootstrap_state"]["cached_ready"])

        adapter_report = sync_adapters(workspace)
        self.assertEqual(adapter_report.exit_code, 0)
        adapter_ready = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(adapter_ready.exit_code, 0)
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
        self.assertEqual(kb_present.exit_code, 0)
        self.assertEqual(kb_present.payload["stage"], "knowledge-base-present")

        os.utime(source_file, None)
        kb_stale = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(kb_stale.exit_code, 2)
        self.assertEqual(kb_stale.payload["stage"], "knowledge-base-stale")

    def test_status_reports_knowledge_base_invalid_as_action_required(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        staging_artifact = workspace.knowledge_base_staging_dir / "artifact.md"
        staging_artifact.parent.mkdir(parents=True, exist_ok=True)
        staging_artifact.write_text("staging knowledge\n", encoding="utf-8")
        write_json(
            workspace.staging_validation_report_path,
            {"status": "blocking-errors"},
        )

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["stage"], "knowledge-base-invalid")

    def test_sync_reports_material_confirmation_payload(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
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
            confirmation_prompt=(
                "This question requires additional local dependencies before it can continue "
                "safely. Prepare the workspace now?"
            ),
            confirmation_reason="office-rendering",
        )

        status_report = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(status_report.exit_code, 1)
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
        self.seed_self_contained_bootstrap_state(workspace)
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

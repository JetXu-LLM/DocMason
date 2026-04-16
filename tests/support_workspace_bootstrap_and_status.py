"""Workspace command surface tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
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
    _resolve_official_libreoffice_macos_download,
    doctor_workspace,
    prepare_workspace,
    status_workspace,
    sync_adapters,
    sync_workspace,
)
from docmason.control_plane import (
    ensure_shared_job,
    load_shared_job,
    sync_input_signature,
    workspace_state_snapshot,
)
from docmason.conversation import current_host_execution_context
from docmason.coordination import LeaseConflictError, lease_dir, workspace_lease
from docmason.libreoffice_runtime import LIBREOFFICE_PROBE_CONTRACT
from docmason.project import (
    BOOTSTRAP_STATE_SCHEMA_VERSION,
    WorkspacePaths,
    cached_bootstrap_readiness,
    read_json,
    source_inventory_signature,
    write_json,
)
from docmason.toolchain import ProbeExecution, inspect_toolchain
from docmason.workspace_probe import validate_soffice_binary as validate_probe_soffice_binary

ROOT = Path(__file__).resolve().parents[1]


class WorkspaceBootstrapAndStatusTests(unittest.TestCase):
    """Cover the CLI, workspace bootstrap, and adapter behavior."""

    @contextlib.contextmanager
    def neutral_host_execution_context(self):
        """Run one test with a host context that does not trigger Codex-specific gating."""
        with mock.patch(
            "docmason.conversation.current_host_execution_context",
            return_value={
                "host_provider": "unknown-agent",
                "sandbox_policy": None,
                "approval_mode": None,
                "permission_mode": None,
                "full_machine_access": False,
                "workspace_write_network_access": None,
                "sandbox_writable_roots": [],
                "context_source": "test-override",
            },
        ):
            yield

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
        (root / "src" / "docmason" / "libreoffice_runtime.py").write_text(
            (ROOT / "src" / "docmason" / "libreoffice_runtime.py").read_text(
                encoding="utf-8"
            ),
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

    def rewrite_workspace_libreoffice_runtime(
        self,
        workspace: WorkspacePaths,
        *,
        app_bundle_path: Path,
    ) -> None:
        runtime_path = workspace.root / "src" / "docmason" / "libreoffice_runtime.py"
        binary_path = app_bundle_path / "Contents" / "MacOS" / "soffice"
        text = runtime_path.read_text(encoding="utf-8")
        text = text.replace(
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            str(binary_path),
        )
        text = text.replace("/Applications/LibreOffice.app", str(app_bundle_path))
        runtime_path.write_text(text, encoding="utf-8")

    def seed_codex_thread_context(
        self,
        home: Path,
        *,
        thread_id: str,
        sandbox_policy: dict[str, object],
        approval_mode: str = "never",
    ) -> Path:
        codex_root = home / ".codex"
        sessions_root = codex_root / "sessions" / "2026" / "04" / "08"
        sessions_root.mkdir(parents=True, exist_ok=True)
        rollout_path = sessions_root / f"rollout-{thread_id}.jsonl"
        rollout_path.write_text(
            json.dumps(
                {
                    "type": "turn_context",
                    "payload": {
                        "approval_policy": approval_mode,
                        "sandbox_policy": sandbox_policy,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        database_path = codex_root / "state_5.sqlite"
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY, rollout_path TEXT, sandbox_policy TEXT, "
                "approval_mode TEXT)"
            )
            connection.execute(
                (
                    "INSERT INTO threads "
                    "(id, rollout_path, sandbox_policy, approval_mode) "
                    "VALUES (?, ?, ?, ?)"
                ),
                (
                    thread_id,
                    str(rollout_path),
                    json.dumps(sandbox_policy),
                    approval_mode,
                ),
            )
            connection.commit()
        return rollout_path

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
                                "detail": (
                                    "PDF rendering and extraction dependencies are available."
                                ),
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
            "export PYTHONPATH="
            f"{shlex.quote(str(workspace.root / 'src'))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        python_path.chmod(python_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        workspace.toolchain_python_current_dir.parent.mkdir(parents=True, exist_ok=True)
        if (
            workspace.toolchain_python_current_dir.exists()
            or workspace.toolchain_python_current_dir.is_symlink()
        ):
            if (
                workspace.toolchain_python_current_dir.is_dir()
                and not workspace.toolchain_python_current_dir.is_symlink()
            ):
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
            "export PYTHONPATH="
            f"{shlex.quote(str(workspace.root / 'src'))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
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
                "schema_version": BOOTSTRAP_STATE_SCHEMA_VERSION,
                "status": "ready",
                "environment_ready": True,
                "workspace_runtime_ready": True,
                "machine_baseline_ready": True,
                "machine_baseline_status": "ready",
                "checked_at": "2026-03-25T00:00:00Z",
                "prepared_at": "2026-03-25T00:00:00Z",
                "workspace_root": str(workspace.root.resolve()),
                "package_manager": package_manager,
                "bootstrap_source": "repo-local-managed",
                "python_executable": str(managed_python),
                "venv_python": ".venv/bin/python",
                "editable_install": True,
                "editable_install_detail": (
                    "Editable install resolves to the workspace source tree."
                ),
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
                "host_access_required": False,
                "host_access_guidance": None,
                "machine_baseline_detail": "Native Codex machine baseline is ready.",
                "homebrew_ready": True,
                "homebrew_binary": "/opt/homebrew/bin/brew",
                "pdf_renderer_ready": pdf_renderer_ready,
                "office_renderer_ready": office_renderer_ready,
                "office_renderer_required": False,
                "requires_pdf_renderer": False,
                "requires_office_renderer": False,
                "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                "libreoffice_blocked_by_host_access": False,
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
            "update-core": ["update-core", "--json", "--bundle", "bundle.zip"],
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
            "update-core": CommandReport(0, {"status": READY, "update_core_status": "updated"}, []),
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
            "update-core": "docmason.cli.update_core_workspace",
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
            "update-core": ["update-core", "--json"],
        }
        for command, target in patch_targets.items():
            with self.subTest(command=command), mock.patch(target, return_value=reports[command]):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    exit_code = main(arguments[command])
                self.assertEqual(exit_code, reports[command].exit_code)
                self.assertTrue(buffer.getvalue().strip())

    def test_cli_import_does_not_eagerly_pull_hidden_ask_dependencies(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                (
                    "import sys; "
                    f"sys.path.insert(0, {str(ROOT / 'src')!r}); "
                    "import docmason.cli; "
                    "print('ok')"
                ),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "ok")

    def test_prepare_requests_repo_local_uv_bootstrap_when_uv_is_missing(self) -> None:
        workspace = self.make_workspace()
        with self.neutral_host_execution_context():
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

    def test_current_host_execution_context_normalizes_codex_json_sandbox_policy(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "DOCMASON_AGENT_SURFACE": "codex",
                "DOCMASON_CODEX_SANDBOX_POLICY": '{"type":"danger-full-access"}',
                "DOCMASON_CODEX_APPROVAL_MODE": "never",
            },
            clear=False,
        ):
            context = current_host_execution_context()

        self.assertEqual(context["sandbox_policy"], "danger-full-access")
        self.assertEqual(context["permission_mode"], "full-access")
        self.assertTrue(context["full_machine_access"])

    def test_current_host_execution_context_reads_rollout_turn_context_network_snapshot(
        self,
    ) -> None:
        workspace = self.make_workspace()
        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            codex_root = home / ".codex"
            sessions_root = codex_root / "sessions" / "2026" / "04" / "02"
            sessions_root.mkdir(parents=True, exist_ok=True)
            rollout_path = sessions_root / "rollout-thread-network.jsonl"
            rollout_path.write_text(
                json.dumps(
                    {
                        "type": "turn_context",
                        "payload": {
                            "approval_policy": "on-request",
                            "sandbox_policy": {
                                "type": "workspace-write",
                                "network_access": False,
                                "writable_roots": [str(workspace.root)],
                            },
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            database_path = codex_root / "state_5.sqlite"
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE threads ("
                    "id TEXT PRIMARY KEY, rollout_path TEXT, sandbox_policy TEXT, "
                    "approval_mode TEXT)"
                )
                connection.execute(
                    (
                        "INSERT INTO threads "
                        "(id, rollout_path, sandbox_policy, approval_mode) "
                        "VALUES (?, ?, ?, ?)"
                    ),
                    (
                        "thread-network",
                        str(rollout_path),
                        json.dumps({"type": "workspace-write"}),
                        "on-request",
                    ),
                )
                connection.commit()

            with mock.patch.dict(
                os.environ,
                {
                    "HOME": str(home),
                    "CODEX_THREAD_ID": "thread-network",
                },
                clear=False,
            ):
                context = current_host_execution_context()

        self.assertEqual(context["context_source"], "codex-turn-context")
        self.assertEqual(context["sandbox_policy"], "workspace-write")
        self.assertEqual(context["permission_mode"], "default-permissions")
        self.assertFalse(context["workspace_write_network_access"])
        self.assertEqual(context["sandbox_writable_roots"], [str(workspace.root)])

    def test_bootstrap_launcher_uses_healthy_versioned_host_context_python(self) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        (workspace.root / "scripts" / "read-host-execution-context.py").write_text(
            (ROOT / "scripts" / "read-host-execution-context.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        (workspace.root / "runtime").mkdir(parents=True, exist_ok=True)
        marker_path = workspace.root / "runtime" / "launcher-ran.txt"
        host_context_marker = workspace.root / "runtime" / "host-context-python.txt"
        runtime_python = workspace.root / ".manual-bootstrap-python" / "bin" / "python3"
        runtime_python.parent.mkdir(parents=True, exist_ok=True)
        runtime_python.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        runtime_python.chmod(0o755)
        (workspace.root / "src" / "docmason" / "__main__.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "Path(os.environ['DOCMASON_BOOTSTRAP_MARKER']).write_text('ran\\n', encoding='utf-8')\n"
            "print(json.dumps({'status': 'ready'}))\n",
            encoding="utf-8",
        )

        fake_bin_dir = workspace.root / ".fake-bin-host-context-versioned"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        (fake_bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        (fake_bin_dir / "uname").chmod(0o755)
        (fake_bin_dir / "python3").write_text(
            "#!/bin/sh\n"
            "sleep 10\n",
            encoding="utf-8",
        )
        (fake_bin_dir / "python3").chmod(0o755)
        (fake_bin_dir / "python3.13").write_text(
            "#!/bin/sh\n"
            "if [ \"${1##*/}\" = \"read-host-execution-context.py\" ]; then\n"
            f"  printf 'python3.13\\n' > {shlex.quote(str(host_context_marker))}\n"
            "fi\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin_dir / "python3.13").chmod(0o755)

        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            self.seed_codex_thread_context(
                home,
                thread_id="thread-full-access",
                sandbox_policy={"type": "danger-full-access"},
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_THREAD_ID": "thread-full-access",
                "DOCMASON_BOOTSTRAP_PYTHON": str(runtime_python),
                "DOCMASON_BOOTSTRAP_MARKER": str(marker_path),
                "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
            }
            completed = subprocess.run(
                [str(script_path), "--yes", "--json"],
                cwd=workspace.root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(marker_path.exists())
        self.assertEqual(host_context_marker.read_text(encoding="utf-8").strip(), "python3.13")
        self.assertEqual(json.loads(completed.stdout)["status"], READY)

    def test_bootstrap_launcher_preserves_conservative_fallback_without_healthy_host_context_python(
        self,
    ) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        (workspace.root / "scripts" / "read-host-execution-context.py").write_text(
            (ROOT / "scripts" / "read-host-execution-context.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        fake_bin_dir = workspace.root / ".fake-bin-host-context-missing"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        (fake_bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        (fake_bin_dir / "uname").chmod(0o755)
        for helper_name in (
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3.9",
            "python3",
            "python",
        ):
            (fake_bin_dir / helper_name).write_text(
                "#!/bin/sh\n"
                "sleep 10\n",
                encoding="utf-8",
            )
            (fake_bin_dir / helper_name).chmod(0o755)

        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            self.seed_codex_thread_context(
                home,
                thread_id="thread-fallback",
                sandbox_policy={"type": "danger-full-access"},
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_THREAD_ID": "thread-fallback",
                "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
            }
            completed = subprocess.run(
                [str(script_path), "--yes", "--json"],
                cwd=workspace.root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], ACTION_REQUIRED)
        self.assertEqual(
            payload["detail"],
            (
                "DocMason cannot safely confirm that this Codex turn allows the network "
                "downloads required for repo-local runtime bootstrap."
            ),
        )
        self.assertEqual(
            payload["host_execution"]["context_source"],
            "env-codex-thread-id-fallback",
        )
        self.assertIsNone(payload["host_execution"]["sandbox_policy"])
        self.assertIsNone(payload["host_execution"]["permission_mode"])
        self.assertFalse(payload["host_execution"]["full_machine_access"])

    def test_bootstrap_launcher_skips_unhealthy_explicit_host_context_python_override(
        self,
    ) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        (workspace.root / "scripts" / "read-host-execution-context.py").write_text(
            (ROOT / "scripts" / "read-host-execution-context.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        (workspace.root / "runtime").mkdir(parents=True, exist_ok=True)
        marker_path = workspace.root / "runtime" / "launcher-ran.txt"
        host_context_marker = workspace.root / "runtime" / "host-context-python.txt"
        runtime_python = workspace.root / ".manual-bootstrap-python" / "bin" / "python3"
        runtime_python.parent.mkdir(parents=True, exist_ok=True)
        runtime_python.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        runtime_python.chmod(0o755)
        bad_helper_python = workspace.root / ".bad-host-context-python" / "python3"
        bad_helper_python.parent.mkdir(parents=True, exist_ok=True)
        bad_helper_python.write_text(
            "#!/bin/sh\n"
            "sleep 10\n",
            encoding="utf-8",
        )
        bad_helper_python.chmod(0o755)
        (workspace.root / "src" / "docmason" / "__main__.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "Path(os.environ['DOCMASON_BOOTSTRAP_MARKER']).write_text('ran\\n', encoding='utf-8')\n"
            "print(json.dumps({'status': 'ready'}))\n",
            encoding="utf-8",
        )

        fake_bin_dir = workspace.root / ".fake-bin-host-context-explicit"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        (fake_bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        (fake_bin_dir / "uname").chmod(0o755)
        (fake_bin_dir / "python3.13").write_text(
            "#!/bin/sh\n"
            "if [ \"${1##*/}\" = \"read-host-execution-context.py\" ]; then\n"
            f"  printf 'python3.13\\n' > {shlex.quote(str(host_context_marker))}\n"
            "fi\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        (fake_bin_dir / "python3.13").chmod(0o755)

        with tempfile.TemporaryDirectory() as home_name:
            home = Path(home_name)
            self.seed_codex_thread_context(
                home,
                thread_id="thread-explicit-override",
                sandbox_policy={"type": "danger-full-access"},
            )
            env = {
                **os.environ,
                "HOME": str(home),
                "CODEX_THREAD_ID": "thread-explicit-override",
                "DOCMASON_HOST_CONTEXT_PYTHON": str(bad_helper_python),
                "DOCMASON_BOOTSTRAP_PYTHON": str(runtime_python),
                "DOCMASON_BOOTSTRAP_MARKER": str(marker_path),
                "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
            }
            completed = subprocess.run(
                [str(script_path), "--yes", "--json"],
                cwd=workspace.root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(marker_path.exists())
        self.assertEqual(host_context_marker.read_text(encoding="utf-8").strip(), "python3.13")
        self.assertEqual(json.loads(completed.stdout)["status"], READY)

    def test_prepare_uses_uv_when_available(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, cwd)

        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch.dict(
                os.environ,
                {"DOCMASON_AGENT_SURFACE": "unknown-agent"},
                clear=False,
            ),
        ):
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
        self.assertEqual(state["schema_version"], BOOTSTRAP_STATE_SCHEMA_VERSION)
        self.assertTrue(state["environment_ready"])
        self.assertTrue(state["workspace_runtime_ready"])
        self.assertTrue(state["machine_baseline_ready"])
        self.assertEqual(state["machine_baseline_status"], "not-applicable")
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

        with self.neutral_host_execution_context():
            with mock.patch(
                "docmason.commands.find_uv_binary",
                return_value=str(workspace.toolchain_bootstrap_uv),
            ):
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
        with self.neutral_host_execution_context():
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

        with self.neutral_host_execution_context():
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

        def probe_runner(
            command: list[str] | tuple[str, ...],
            cwd: Path,
            timeout_seconds: float,
        ) -> ProbeExecution:
            del cwd, timeout_seconds
            command_list = list(command)
            if command_list[:2] == [str(workspace.venv_python), "-c"]:
                return ProbeExecution(exit_code=0)
            if command_list[:2] == [str(workspace.venv_docmason), "--help"]:
                return ProbeExecution(exit_code=-1, timed_out=True)
            return ProbeExecution(exit_code=0)

        with mock.patch("docmason.toolchain._default_probe_runner", side_effect=probe_runner):
            toolchain = inspect_toolchain(workspace, editable_install=True)

        self.assertEqual(toolchain["entrypoint_health"], "startup-silent")
        self.assertEqual(toolchain["repair_reason"], "entrypoint-broken")

    def test_bootstrap_launcher_ignores_bad_manual_python_and_uses_controlled_bootstrap_asset(
        self,
    ) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        (workspace.root / "runtime").mkdir(parents=True, exist_ok=True)
        (workspace.root / "docs" / "setup").mkdir(parents=True, exist_ok=True)
        (workspace.root / "docs" / "setup" / "manual-workspace-recovery.md").write_text(
            "# Manual recovery\n",
            encoding="utf-8",
        )
        (workspace.root / "src" / "docmason" / "__main__.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "marker = Path(os.environ['DOCMASON_BOOTSTRAP_MARKER'])\n"
            "marker.write_text(sys.executable + '\\n', encoding='utf-8')\n"
            "print(json.dumps({'status': 'ready'}))\n",
            encoding="utf-8",
        )
        fake_uv_installer = workspace.root / "uv-installer.sh"
        fake_uv_installer.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "cat > \"$UV_UNMANAGED_INSTALL/uv\" <<'EOF'\n"
            "#!/bin/sh\n"
            "set -eu\n"
            "target=''\n"
            "for arg in \"$@\"; do\n"
            "  target=\"$arg\"\n"
            "done\n"
            "mkdir -p \"$target/bin\"\n"
            "cat > \"$target/bin/python\" <<'PYEOF'\n"
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
            "PYEOF\n"
            "chmod +x \"$target/bin/python\"\n"
            "exit 0\n"
            "EOF\n"
            "chmod +x \"$UV_UNMANAGED_INSTALL/uv\"\n",
            encoding="utf-8",
        )
        fake_uv_installer.chmod(0o755)

        fake_bin_dir = workspace.root / ".fake-bin"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_curl = fake_bin_dir / "curl"
        fake_curl.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "output=''\n"
            "url=''\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in\n"
            "    -o)\n"
            "      output=\"$2\"\n"
            "      shift 2\n"
            "      ;;\n"
            "    -*)\n"
            "      shift\n"
            "      ;;\n"
            "    *)\n"
            "      url=\"$1\"\n"
            "      shift\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
            "[ \"$url\" = \"https://astral.sh/uv/install.sh\" ]\n"
            f"cp {shlex.quote(str(fake_uv_installer))} \"$output\"\n",
            encoding="utf-8",
        )
        fake_curl.chmod(0o755)

        bad_stub = workspace.root / ".bad-python" / "python3"
        bad_stub.parent.mkdir(parents=True, exist_ok=True)
        bad_stub.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "os.execvp('python3', ['python3', *os.sys.argv[1:]])\n",
            encoding="utf-8",
        )
        bad_stub.chmod(0o755)

        marker_path = workspace.root / "runtime" / "bootstrap-python.txt"
        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "full-access",
            "DOCMASON_SANDBOX_POLICY": "danger-full-access",
            "DOCMASON_WORKSPACE_WRITE_NETWORK_ACCESS": "true",
            "DOCMASON_BOOTSTRAP_PYTHON": str(bad_stub),
            "DOCMASON_BOOTSTRAP_MARKER": str(marker_path),
            "DOCMASON_BOOTSTRAP_UV_INSTALLER_URL": "https://astral.sh/uv/install.sh",
            "DOCMASON_SHARED_BOOTSTRAP_CACHE": str(
                workspace.root / ".shared-bootstrap-cache"
            ),
            "PATH": str(fake_bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(marker_path.read_text(encoding="utf-8").strip())
        self.assertIn("controlled bootstrap asset path", completed.stderr)

    def test_bootstrap_launcher_returns_host_access_upgrade_before_shared_cache_probe(self) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)

        fake_bin_dir = workspace.root / ".fake-bin"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_uname = fake_bin_dir / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        fake_uname.chmod(0o755)

        shared_cache = workspace.root / ".shared-bootstrap-cache"
        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "default-permissions",
            "DOCMASON_CODEX_NETWORK_ACCESS": "false",
            "DOCMASON_CODEX_WRITABLE_ROOTS": json.dumps([str(workspace.root)]),
            "DOCMASON_SHARED_BOOTSTRAP_CACHE": str(shared_cache),
            "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], ACTION_REQUIRED)
        self.assertEqual(payload["control_plane"]["confirmation_kind"], "host-access-upgrade")
        self.assertTrue(payload["host_access_required"])
        self.assertEqual(payload["machine_baseline_status"], "ready")
        self.assertIn("Default permissions", payload["host_access_guidance"])
        self.assertIn("Full access", payload["next_steps"][0])
        self.assertFalse(shared_cache.exists())
        self.assertNotIn("manual-workspace-recovery", completed.stderr)

    def test_bootstrap_launcher_does_not_pause_for_missing_homebrew_when_office_is_not_required(
        self,
    ) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        (workspace.root / "runtime").mkdir(parents=True, exist_ok=True)
        marker_path = workspace.root / "runtime" / "launcher-ran.txt"
        workspace.toolchain_bootstrap_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.toolchain_bootstrap_python.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        workspace.toolchain_bootstrap_python.chmod(0o755)
        (workspace.root / "src" / "docmason" / "__main__.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "Path(os.environ['DOCMASON_BOOTSTRAP_MARKER']).write_text('ran\\n', encoding='utf-8')\n"
            "print(json.dumps({'status': 'ready'}))\n",
            encoding="utf-8",
        )

        fake_bin_dir = workspace.root / ".fake-bin-no-brew"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_uname = fake_bin_dir / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        fake_uname.chmod(0o755)

        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "default-permissions",
            "DOCMASON_CODEX_NETWORK_ACCESS": "true",
            "DOCMASON_CODEX_WRITABLE_ROOTS": json.dumps([str(workspace.root)]),
            "DOCMASON_BOOTSTRAP_MARKER": str(marker_path),
            "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(marker_path.exists())

    def test_bootstrap_launcher_pauses_when_office_renderer_is_required_and_missing(self) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        fake_soffice_path = (
            workspace.root / "missing-LibreOffice.app" / "Contents" / "MacOS" / "soffice"
        )
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh")
            .read_text(encoding="utf-8")
            .replace(
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                str(fake_soffice_path),
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        self.rewrite_workspace_libreoffice_runtime(
            workspace,
            app_bundle_path=fake_soffice_path.parent.parent.parent,
        )
        (workspace.root / "runtime").mkdir(parents=True, exist_ok=True)
        marker_path = workspace.root / "runtime" / "launcher-should-not-run.txt"
        workspace.toolchain_bootstrap_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.toolchain_bootstrap_python.write_text(
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n",
            encoding="utf-8",
        )
        workspace.toolchain_bootstrap_python.chmod(0o755)
        (workspace.source_dir / "deck.pptx").write_text("pptx placeholder\n", encoding="utf-8")
        (workspace.root / "src" / "docmason" / "__main__.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "from pathlib import Path\n"
            "\n"
            "Path(os.environ['DOCMASON_BOOTSTRAP_MARKER']).write_text('ran\\n', encoding='utf-8')\n"
            "print(json.dumps({'status': 'ready'}))\n",
            encoding="utf-8",
        )

        fake_bin_dir = workspace.root / ".fake-bin-office-gap"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        fake_uname = fake_bin_dir / "uname"
        fake_uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        fake_uname.chmod(0o755)

        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "default-permissions",
            "DOCMASON_CODEX_NETWORK_ACCESS": "true",
            "DOCMASON_CODEX_WRITABLE_ROOTS": json.dumps([str(workspace.root)]),
            "DOCMASON_BOOTSTRAP_MARKER": str(marker_path),
            "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], ACTION_REQUIRED)
        self.assertEqual(payload["control_plane"]["confirmation_kind"], "host-access-upgrade")
        self.assertEqual(payload["machine_baseline_status"], "host-access-upgrade-required")
        self.assertFalse(marker_path.exists())

    def test_bootstrap_launcher_reports_host_access_blocked_soffice_candidate(self) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        fake_soffice_path = (
            workspace.root / "missing-LibreOffice.app" / "Contents" / "MacOS" / "soffice"
        )
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh")
            .read_text(encoding="utf-8")
            .replace(
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                str(fake_soffice_path),
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        self.rewrite_workspace_libreoffice_runtime(
            workspace,
            app_bundle_path=fake_soffice_path.parent.parent.parent,
        )
        (workspace.source_dir / "deck.docx").write_text("docx placeholder\n", encoding="utf-8")

        fake_bin_dir = workspace.root / ".fake-bin-invalid-soffice"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        (fake_bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        (fake_bin_dir / "uname").chmod(0o755)
        (fake_bin_dir / "soffice").write_text(
            "#!/bin/sh\n"
            "printf 'Preview 1.0\\n'\n",
            encoding="utf-8",
        )
        (fake_bin_dir / "soffice").chmod(0o755)

        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "default-permissions",
            "DOCMASON_CODEX_NETWORK_ACCESS": "true",
            "DOCMASON_CODEX_WRITABLE_ROOTS": json.dumps([str(workspace.root)]),
            "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], ACTION_REQUIRED)
        self.assertEqual(payload["machine_baseline_status"], "host-access-upgrade-required")
        self.assertTrue(payload["libreoffice_blocked_by_host_access"])
        self.assertFalse(payload["libreoffice_detected_but_unusable"])
        self.assertEqual(
            payload["libreoffice_candidate_binary"],
            str(fake_bin_dir / "soffice"),
        )
        self.assertIn("Full access", payload["detail"])
        self.assertIn("Office rendering", payload["detail"])

    def test_bootstrap_launcher_reports_validation_unavailable_without_unusable_flag(
        self,
    ) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        fake_soffice_path = (
            workspace.root / "missing-LibreOffice.app" / "Contents" / "MacOS" / "soffice"
        )
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh")
            .read_text(encoding="utf-8")
            .replace(
                "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                str(fake_soffice_path),
            ),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        self.rewrite_workspace_libreoffice_runtime(
            workspace,
            app_bundle_path=fake_soffice_path.parent.parent.parent,
        )
        (workspace.source_dir / "deck.docx").write_text("docx placeholder\n", encoding="utf-8")

        fake_bin_dir = workspace.root / ".fake-bin-validation-unavailable"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        (fake_bin_dir / "uname").write_text("#!/bin/sh\nprintf 'Darwin\\n'\n", encoding="utf-8")
        (fake_bin_dir / "uname").chmod(0o755)
        (fake_bin_dir / "soffice").write_text(
            "#!/bin/sh\n"
            "printf 'Preview 1.0\\n'\n",
            encoding="utf-8",
        )
        (fake_bin_dir / "soffice").chmod(0o755)
        for python_name in (
            "python3.13",
            "python3.12",
            "python3.11",
            "python3.10",
            "python3.9",
            "python3",
            "python",
        ):
            (fake_bin_dir / python_name).write_text(
                "#!/bin/sh\n"
                "printf 'Python 3.8.0\\n' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            (fake_bin_dir / python_name).chmod(0o755)

        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "default-permissions",
            "DOCMASON_CODEX_NETWORK_ACCESS": "true",
            "DOCMASON_CODEX_WRITABLE_ROOTS": json.dumps([str(workspace.root)]),
            "PATH": str(fake_bin_dir) + os.pathsep + "/usr/bin:/bin",
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 1)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], ACTION_REQUIRED)
        self.assertEqual(payload["machine_baseline_status"], "host-access-upgrade-required")
        self.assertFalse(payload["libreoffice_detected_but_unusable"])
        self.assertEqual(
            payload["libreoffice_candidate_binary"],
            str(fake_bin_dir / "soffice"),
        )
        self.assertIn("cannot yet validate LibreOffice", payload["detail"])
        self.assertIn("no supported helper Python or bootstrap runtime", payload["detail"])
        self.assertNotIn("not currently usable", payload["detail"])
        self.assertNotIn("needs machine-level repair", payload["detail"])

    def test_bootstrap_launcher_uses_repo_local_cache_in_controlled_codex_mode(self) -> None:
        workspace = self.make_workspace()
        script_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            (ROOT / "scripts" / "bootstrap-workspace.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        script_path.chmod(0o755)
        (workspace.root / "runtime").mkdir(parents=True, exist_ok=True)
        (workspace.root / "src" / "docmason" / "__main__.py").write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "marker = Path(os.environ['DOCMASON_BOOTSTRAP_MARKER'])\n"
            "marker.write_text(sys.executable + '\\n', encoding='utf-8')\n"
            "print(json.dumps({'status': 'ready'}))\n",
            encoding="utf-8",
        )
        fake_uv_installer = workspace.root / "uv-installer.sh"
        fake_uv_installer.write_text(
            "#!/bin/sh\n"
            "set -eu\n"
            "cat > \"$UV_UNMANAGED_INSTALL/uv\" <<'EOF'\n"
            "#!/bin/sh\n"
            "set -eu\n"
            "target=''\n"
            "for arg in \"$@\"; do\n"
            "  target=\"$arg\"\n"
            "done\n"
            "mkdir -p \"$target/bin\"\n"
            "cat > \"$target/bin/python\" <<'PYEOF'\n"
            "#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
            "PYEOF\n"
            "chmod +x \"$target/bin/python\"\n"
            "exit 0\n"
            "EOF\n"
            "chmod +x \"$UV_UNMANAGED_INSTALL/uv\"\n",
            encoding="utf-8",
        )
        fake_uv_installer.chmod(0o755)

        fake_bin_dir = workspace.root / ".fake-bin-controlled"
        fake_bin_dir.mkdir(parents=True, exist_ok=True)
        for name, body in {
            "uname": "#!/bin/sh\nprintf 'Darwin\\n'\n",
            "brew": "#!/bin/sh\nexit 0\n",
            "soffice": "#!/bin/sh\nexit 0\n",
            "curl": (
                "#!/bin/sh\n"
                "set -eu\n"
                "output=''\n"
                "url=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  case \"$1\" in\n"
                "    -o)\n"
                "      output=\"$2\"\n"
                "      shift 2\n"
                "      ;;\n"
                "    -*)\n"
                "      shift\n"
                "      ;;\n"
                "    *)\n"
                "      url=\"$1\"\n"
                "      shift\n"
                "      ;;\n"
                "  esac\n"
                "done\n"
                "[ \"$url\" = \"https://astral.sh/uv/install.sh\" ]\n"
                f"cp {shlex.quote(str(fake_uv_installer))} \"$output\"\n"
            ),
        }.items():
            script = fake_bin_dir / name
            script.write_text(body, encoding="utf-8")
            script.chmod(0o755)

        marker_path = workspace.root / "runtime" / "bootstrap-python.txt"
        shared_cache = workspace.root / ".shared-bootstrap-cache"
        repo_local_cache = workspace.root / ".docmason" / "toolchain" / "bootstrap" / "cache"
        env = {
            **os.environ,
            "DOCMASON_AGENT_SURFACE": "codex",
            "DOCMASON_PERMISSION_MODE": "default-permissions",
            "DOCMASON_CODEX_NETWORK_ACCESS": "true",
            "DOCMASON_CODEX_WRITABLE_ROOTS": json.dumps([str(workspace.root)]),
            "DOCMASON_BOOTSTRAP_MARKER": str(marker_path),
            "DOCMASON_BOOTSTRAP_UV_INSTALLER_URL": "https://astral.sh/uv/install.sh",
            "DOCMASON_SHARED_BOOTSTRAP_CACHE": str(shared_cache),
            "PATH": str(fake_bin_dir) + os.pathsep + os.environ.get("PATH", ""),
        }
        completed = subprocess.run(
            [str(script_path), "--yes", "--json"],
            cwd=workspace.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        self.assertTrue(marker_path.exists())
        self.assertTrue(repo_local_cache.exists())
        self.assertFalse(shared_cache.exists())

    def test_inspect_toolchain_distinguishes_shared_host_bootstrap_from_legacy_external(
        self,
    ) -> None:
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

        brew_call_count = {"value": 0}

        def staged_brew_binary() -> str | None:
            brew_call_count["value"] += 1
            return None if brew_call_count["value"] < 5 else "/opt/homebrew/bin/brew"

        with self.neutral_host_execution_context():
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
        (workspace.source_dir / "example.docx").write_text("docx placeholder\n", encoding="utf-8")
        self.seed_self_contained_bootstrap_state(workspace)
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 5
        state.pop("office_probe_contract", None)
        write_json(workspace.bootstrap_state_path, state)

        ordinary = cached_bootstrap_readiness(workspace)
        sync_ready = cached_bootstrap_readiness(workspace, require_sync_capability=True)
        self.assertTrue(ordinary["ready"])
        self.assertFalse(sync_ready["ready"])
        self.assertEqual(sync_ready["reason"], "office-renderer-probe-contract-upgrade-required")

    def test_cached_bootstrap_readiness_accepts_schema4_marker_for_ordinary_and_sync(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 4
        write_json(workspace.bootstrap_state_path, state)

        ordinary = cached_bootstrap_readiness(workspace)
        sync_ready = cached_bootstrap_readiness(workspace, require_sync_capability=True)

        self.assertTrue(ordinary["ready"])
        self.assertEqual(ordinary["reason"], "cached-ready")
        self.assertTrue(sync_ready["ready"])
        self.assertEqual(sync_ready["reason"], "cached-ready")

    def test_status_and_doctor_accept_healthy_schema4_marker(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 4
        write_json(workspace.bootstrap_state_path, state)

        status_report = status_workspace(workspace, editable_install_probe=self.ready_probe)
        self.assertEqual(status_report.exit_code, 0)
        self.assertTrue(status_report.payload["environment_ready"])
        self.assertTrue(status_report.payload["bootstrap_state"]["cached_ready"])
        self.assertNotIn("prepare", status_report.payload["pending_actions"])

        doctor_report = doctor_workspace(workspace, editable_install_probe=self.ready_probe)
        checks = {check["name"]: check for check in doctor_report.payload["checks"]}
        self.assertEqual(checks["bootstrap-state"]["status"], READY)

    def test_workspace_state_snapshot_accepts_healthy_schema4_marker(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        state = json.loads(workspace.bootstrap_state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 4
        write_json(workspace.bootstrap_state_path, state)

        snapshot = workspace_state_snapshot(workspace)

        self.assertTrue(snapshot["environment"]["ready"])
        self.assertTrue(snapshot["environment"]["sync_capable"])
        self.assertEqual(snapshot["environment"]["bootstrap_reason"], "cached-ready")
        self.assertNotIn("prepare", snapshot["next_legal_actions"])
        self.assertNotIn("prepare --yes", snapshot["next_legal_actions"])

    def test_prepare_prefers_homebrew_for_uv_on_macos(self) -> None:
        workspace = self.make_workspace()
        seen_commands: list[list[str]] = []

        def runner(command: list[str] | tuple[str, ...], cwd: Path) -> CommandExecution:
            del cwd
            command_list = list(command)
            seen_commands.append(command_list)
            return self.fake_prepare_runner(workspace)(command_list, workspace.root)

        with self.neutral_host_execution_context():
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
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "default-permissions",
                },
                clear=False,
            ),
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
            "host-access-upgrade",
        )
        self.assertIn(
            "Codex `Default permissions`",
            report.payload["control_plane"]["confirmation_prompt"],
        )
        self.assertEqual(
            report.payload["next_steps"][0],
            "Switch Codex to `Full access`, then continue the same task.",
        )
        self.assertTrue(report.payload["host_access_required"])
        self.assertEqual(report.payload["machine_baseline_status"], "host-access-upgrade-required")

    def test_prepare_reports_host_access_blocked_libreoffice_as_host_access_upgrade(self) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                return_value={
                    "required": True,
                    "ready": False,
                    "binary": None,
                    "candidate_binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    "validation_detail": (
                        "DocMason is currently running in Codex `Default permissions` on "
                        "macOS, so it needs `Full access` before it can continue Office "
                        "rendering through LibreOffice."
                    ),
                    "detected_but_unusable": False,
                    "blocked_by_host_access": True,
                    "host_access_required": True,
                    "host_access_guidance": (
                        "Switch Codex to `Full access`, then continue the same task."
                    ),
                    "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
                    "detail": (
                        "LibreOffice `soffice` is required to render PowerPoint, Word, and "
                        "Excel sources. DocMason is currently running in Codex `Default "
                        "permissions` on macOS, so it needs `Full access` before it can "
                        "continue Office rendering through LibreOffice."
                    ),
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value="/opt/homebrew/bin/brew"),
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "default-permissions",
                },
                clear=False,
            ),
        ):
            report = prepare_workspace(
                workspace,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(report.payload["machine_baseline_status"], "host-access-upgrade-required")
        machine_baseline = report.payload["environment"]["machine_baseline"]
        self.assertTrue(machine_baseline["libreoffice_blocked_by_host_access"])
        self.assertFalse(machine_baseline["libreoffice_detected_but_unusable"])
        self.assertEqual(
            machine_baseline["libreoffice_candidate_binary"],
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        )
        self.assertIn("Full access", machine_baseline["detail"])

    def test_prepare_retries_transient_startup_silent_before_failing(self) -> None:
        workspace = self.make_workspace()
        with self.neutral_host_execution_context():
            with (
                mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
                mock.patch(
                    "docmason.commands.inspect_entrypoint",
                    side_effect=[
                        {
                            "entrypoint_health": "startup-silent",
                            "detail": "The launcher timed out during startup.",
                        },
                        {
                            "entrypoint_health": "ready",
                            "detail": None,
                        },
                    ],
                ) as inspect_mock,
                mock.patch("docmason.commands.time.sleep") as sleep_mock,
            ):
                report = prepare_workspace(
                    workspace,
                    command_runner=self.fake_prepare_runner(workspace),
                    editable_install_probe=self.ready_probe,
                    interactive=False,
                )

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["status"], READY)
        self.assertEqual(inspect_mock.call_count, 2)
        sleep_mock.assert_called_once()
        self.assertIn(
            "Retried the repo-local DocMason entrypoint startup probe after a transient "
            "`startup-silent` result.",
            report.payload["actions_performed"],
        )

    def test_prepare_still_fails_when_startup_silent_persists_after_retry(self) -> None:
        workspace = self.make_workspace()
        with self.neutral_host_execution_context():
            with (
                mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
                mock.patch(
                    "docmason.commands.inspect_entrypoint",
                    side_effect=[
                        {
                            "entrypoint_health": "startup-silent",
                            "detail": "The launcher timed out during startup.",
                        },
                        {
                            "entrypoint_health": "startup-silent",
                            "detail": "The launcher timed out during startup.",
                        },
                    ],
                ) as inspect_mock,
                mock.patch("docmason.commands.time.sleep") as sleep_mock,
            ):
                report = prepare_workspace(
                    workspace,
                    command_runner=self.fake_prepare_runner(workspace),
                    editable_install_probe=self.ready_probe,
                    interactive=False,
                )

        self.assertEqual(report.exit_code, 1)
        self.assertEqual(report.payload["status"], ACTION_REQUIRED)
        self.assertEqual(
            report.payload["environment"]["entrypoint_health"],
            "startup-silent",
        )
        self.assertEqual(inspect_mock.call_count, 2)
        sleep_mock.assert_called_once()

    def test_prepare_stays_degraded_for_non_native_office_gap(self) -> None:
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
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "claude-code",
                    "DOCMASON_APPROVAL_MODE": "default",
                },
                clear=False,
            ),
        ):
            report = prepare_workspace(
                workspace,
                assume_yes=True,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
            )

        self.assertEqual(report.exit_code, 2)
        self.assertEqual(report.payload["status"], DEGRADED)
        self.assertEqual(report.payload["machine_baseline_status"], "not-applicable")
        self.assertIn(
            "LibreOffice `soffice` is required but unavailable.",
            "\n".join(report.lines),
        )
        self.assertIn("LibreOffice", " ".join(report.payload["next_steps"]))

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
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "full-access",
                },
                clear=False,
            ),
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
        self.assertTrue(report.payload["workspace_runtime_ready"])
        self.assertTrue(report.payload["machine_baseline_ready"])
        self.assertEqual(report.payload["machine_baseline_status"], "ready")
        self.assertFalse(report.payload["host_access_required"])
        self.assertIn(
            ["/opt/homebrew/bin/brew", "install", "--cask", "libreoffice-still"],
            seen_commands,
        )

    def test_prepare_reinstalls_detected_but_unusable_libreoffice_from_official_macos_path(
        self,
    ) -> None:
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
                        "binary": None,
                        "candidate_binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                        "validation_detail": (
                            "The detected LibreOffice command failed the conversion smoke test: "
                            "terminated by signal 6."
                        ),
                        "detected_but_unusable": True,
                        "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
                        "detail": (
                            "LibreOffice `soffice` is required to render PowerPoint, Word, and "
                            "Excel sources, but the detected candidate "
                            "`/Applications/LibreOffice.app/Contents/MacOS/soffice` is not "
                            "currently usable."
                        ),
                    },
                    {
                        "required": True,
                        "ready": True,
                        "binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                        "candidate_binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                        "validation_detail": "Validated LibreOffice renderer capability.",
                        "detected_but_unusable": False,
                        "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
                        "detail": "LibreOffice rendering is available.",
                    },
                ],
            ),
            mock.patch(
                "docmason.commands.find_brew_binary",
                return_value="/opt/homebrew/bin/brew",
            ),
            mock.patch(
                "docmason.commands._install_libreoffice_from_official_macos_package",
                return_value=(
                    True,
                    "Installed LibreOffice from the official macOS package at "
                    "/Applications/LibreOffice.app.",
                ),
            ) as official_install,
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "full-access",
                    "DOCMASON_HOST_FULL_MACHINE_ACCESS": "true",
                },
                clear=False,
            ),
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
        self.assertEqual(report.payload["machine_baseline_status"], "ready")
        official_install.assert_called_once()
        self.assertFalse(
            any(
                command == ["/opt/homebrew/bin/brew", "install", "--cask", "libreoffice-still"]
                for command in seen_commands
            )
        )

    def test_prepare_is_ready_without_homebrew_when_office_renderer_is_already_available(
        self,
    ) -> None:
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
                return_value={
                    "required": True,
                    "ready": True,
                    "binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    "detail": "LibreOffice rendering is available.",
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value=None),
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "full-access",
                },
                clear=False,
            ),
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
        self.assertTrue(report.payload["machine_baseline_ready"])
        self.assertEqual(report.payload["machine_baseline_status"], "ready")
        self.assertFalse(
            any(command and command[0].endswith("brew") for command in seen_commands)
        )

    def test_prepare_is_ready_without_libreoffice_when_office_renderer_is_not_required(
        self,
    ) -> None:
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
                return_value={
                    "required": False,
                    "ready": False,
                    "binary": None,
                    "detail": (
                        "LibreOffice is optional until PowerPoint, Word, or Excel sources are "
                        "present."
                    ),
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value=None),
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "full-access",
                },
                clear=False,
            ),
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
        self.assertTrue(report.payload["machine_baseline_ready"])
        self.assertEqual(report.payload["machine_baseline_status"], "ready")
        self.assertFalse(any(command and command[0].endswith("brew") for command in seen_commands))

    def test_prepare_generates_repo_local_skill_shims(self) -> None:
        workspace = self.make_workspace()

        with self.neutral_host_execution_context():
            with mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"):
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

        with self.neutral_host_execution_context():
            with mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"):
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

        with self.neutral_host_execution_context():
            with mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"):
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

    def test_prepare_auto_installs_libreoffice_from_official_macos_path_without_homebrew(
        self,
    ) -> None:
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
                return_value=None,
            ),
            mock.patch(
                "docmason.commands._install_libreoffice_from_official_macos_package",
                return_value=(
                    True,
                    "Installed LibreOffice from the official macOS package at "
                    "/Applications/LibreOffice.app.",
                ),
            ) as official_install,
            mock.patch(
                "docmason.commands.preferred_libreoffice_install_command",
                return_value=(None, None),
            ),
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "full-access",
                },
                clear=False,
            ),
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
        self.assertEqual(report.payload["machine_baseline_status"], "ready")
        official_install.assert_called_once()
        self.assertFalse(any(command and command[0].endswith("brew") for command in seen_commands))

    def test_resolve_official_libreoffice_macos_download_parses_current_arch_contract(self) -> None:
        selector_html = (
            '<option value="/download/download-libreoffice/?type=mac-aarch64'
            '&version=26.2.2&lang=en-US">macOS (Apple Silicon)</option>'
        )
        arch_page_html = (
            '<a class="dl_download_link" href="https://www.libreoffice.org/donate/dl/'
            'mac-aarch64/26.2.2/en-US/LibreOffice_26.2.2_MacOS_aarch64.dmg">'
            '<span class="dl_yellow_download_button"><strong>DOWNLOAD</strong></span></a>'
        )
        redirect_html = (
            '<meta http-equiv="Refresh" content="0; '
            "url=https://download.documentfoundation.org/libreoffice/stable/26.2.2/mac/"
            'aarch64/LibreOffice_26.2.2_MacOS_aarch64.dmg"/>'
        )

        with mock.patch(
            "docmason.commands._read_text_url",
            side_effect=[selector_html, arch_page_html, redirect_html],
        ):
            resolved = _resolve_official_libreoffice_macos_download(machine="arm64")

        self.assertEqual(resolved["download_type"], "mac-aarch64")
        self.assertEqual(resolved["version"], "26.2.2")
        self.assertEqual(
            resolved["dmg_url"],
            (
                "https://download.documentfoundation.org/libreoffice/stable/26.2.2/mac/"
                "aarch64/LibreOffice_26.2.2_MacOS_aarch64.dmg"
            ),
        )

    def test_prepare_json_mode_keeps_payload_on_stdout_and_progress_on_stderr(self) -> None:
        workspace = self.make_workspace()

        def cli_prepare(*, assume_yes: bool, progress_stream=None) -> CommandReport:
            return prepare_workspace(
                workspace,
                assume_yes=assume_yes,
                command_runner=self.fake_prepare_runner(workspace),
                editable_install_probe=self.ready_probe,
                interactive=False,
                progress_stream=progress_stream,
            )

        with self.neutral_host_execution_context():
            with (
                mock.patch("docmason.cli.prepare_workspace", side_effect=cli_prepare),
                mock.patch("docmason.commands.find_uv_binary", return_value="/usr/local/bin/uv"),
            ):
                stdout_buffer = io.StringIO()
                stderr_buffer = io.StringIO()
                with (
                    contextlib.redirect_stdout(stdout_buffer),
                    contextlib.redirect_stderr(stderr_buffer),
                ):
                    exit_code = main(["prepare", "--json", "--yes"])

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout_buffer.getvalue())
        self.assertEqual(payload["status"], READY)
        stderr_text = stderr_buffer.getvalue()
        self.assertIn(
            "Prepare progress: provisioning repo-local managed Python 3.13...",
            stderr_text,
        )
        self.assertIn("Prepare progress: rebuilding the repo-local `.venv`...", stderr_text)
        self.assertIn(
            "Prepare progress: installing DocMason into the repo-local `.venv`...",
            stderr_text,
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
        self.assertEqual(statuses["storage-lifecycle"], READY)
        self.assertGreater(report.payload["knowledge_base"]["storage_lifecycle"]["family_count"], 0)

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

    def test_doctor_reports_host_access_blocked_libreoffice_under_default_permissions(
        self,
    ) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                return_value={
                    "required": True,
                    "ready": False,
                    "binary": None,
                    "candidate_binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    "validation_detail": (
                        "DocMason is currently running in Codex `Default permissions` on "
                        "macOS, so it needs `Full access` before it can continue Office "
                        "rendering through LibreOffice."
                    ),
                    "detected_but_unusable": False,
                    "blocked_by_host_access": True,
                    "host_access_required": True,
                    "host_access_guidance": (
                        "Switch Codex to `Full access`, then continue the same task."
                    ),
                    "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
                    "detail": (
                        "LibreOffice `soffice` is required to render PowerPoint, Word, and "
                        "Excel sources. DocMason is currently running in Codex `Default "
                        "permissions` on macOS, so it needs `Full access` before it can "
                        "continue Office rendering through LibreOffice."
                    ),
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value="/opt/homebrew/bin/brew"),
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "default-permissions",
                },
                clear=False,
            ),
        ):
            report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)

        machine_check = next(
            check for check in report.payload["checks"] if check["name"] == "machine-baseline"
        )
        self.assertEqual(machine_check["status"], ACTION_REQUIRED)
        self.assertIn("Full access", machine_check["detail"])
        self.assertIn("Full access", machine_check["action"])

    def test_status_reports_host_access_blocked_libreoffice_under_default_permissions(
        self,
    ) -> None:
        workspace = self.make_workspace()
        with (
            mock.patch(
                "docmason.commands.office_renderer_snapshot",
                return_value={
                    "required": True,
                    "ready": False,
                    "binary": None,
                    "candidate_binary": "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                    "validation_detail": (
                        "DocMason is currently running in Codex `Default permissions` on "
                        "macOS, so it needs `Full access` before it can continue Office "
                        "rendering through LibreOffice."
                    ),
                    "detected_but_unusable": False,
                    "blocked_by_host_access": True,
                    "host_access_required": True,
                    "host_access_guidance": (
                        "Switch Codex to `Full access`, then continue the same task."
                    ),
                    "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
                    "detail": (
                        "LibreOffice `soffice` is required to render PowerPoint, Word, and "
                        "Excel sources. DocMason is currently running in Codex `Default "
                        "permissions` on macOS, so it needs `Full access` before it can "
                        "continue Office rendering through LibreOffice."
                    ),
                },
            ),
            mock.patch("docmason.commands.find_brew_binary", return_value="/opt/homebrew/bin/brew"),
            mock.patch("docmason.commands.sys.platform", "darwin"),
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "default-permissions",
                },
                clear=False,
            ),
        ):
            report = status_workspace(workspace, editable_install_probe=self.missing_probe)

        self.assertEqual(
            report.payload["environment"]["machine_baseline_status"],
            "host-access-upgrade-required",
        )
        self.assertTrue(
            report.payload["environment"]["machine_baseline"]["libreoffice_blocked_by_host_access"]
        )
        self.assertFalse(
            report.payload["environment"]["machine_baseline"]["libreoffice_detected_but_unusable"]
        )
        self.assertIn(
            "Full access",
            report.payload["environment"]["machine_baseline"]["detail"],
        )

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

    def test_sync_blocks_shared_job_when_sync_raises_unexpected_error(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)

        with mock.patch(
            "docmason.commands._run_phase4_sync",
            side_effect=RuntimeError("sync exploded"),
        ):
            with self.assertRaisesRegex(RuntimeError, "sync exploded"):
                sync_workspace(workspace)

        job_dirs = sorted(path for path in workspace.shared_jobs_dir.iterdir() if path.is_dir())
        self.assertEqual(len(job_dirs), 1)
        manifest = load_shared_job(workspace, job_dirs[0].name)
        self.assertEqual(manifest["status"], "blocked")
        result_payload = read_json(job_dirs[0] / "result.json")
        self.assertIn("Unexpected sync failure", result_payload["result"]["detail"])

    def test_workspace_lease_does_not_steal_fresh_empty_directory(self) -> None:
        workspace = self.make_workspace()
        target = lease_dir(workspace, "conversation:fresh-empty")
        target.mkdir(parents=True, exist_ok=False)

        with self.assertRaises(LeaseConflictError):
            with workspace_lease(
                workspace,
                "conversation:fresh-empty",
                timeout_seconds=0.05,
                poll_interval_seconds=0.01,
                stale_after_seconds=600.0,
            ):
                self.fail("workspace_lease should not acquire a fresh empty directory")

        self.assertTrue(target.exists())

    def test_workspace_probe_soffice_timeout_returns_structured_detail(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DOCMASON_AGENT_SURFACE": "codex",
                    "DOCMASON_PERMISSION_MODE": "full-access",
                },
                clear=False,
            ),
            mock.patch(
                "docmason.libreoffice_runtime.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=[sys.executable, "--version"],
                    timeout=15.0,
                ),
            ),
        ):
            validation = validate_probe_soffice_binary(sys.executable)

        self.assertFalse(validation["ready"])
        self.assertIn("timed out", validation["detail"])

    def test_status_stage_progression_and_pending_actions(self) -> None:
        workspace = self.make_workspace()
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")

        with self.neutral_host_execution_context():
            foundation = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertEqual(foundation.exit_code, 1)
        self.assertEqual(foundation.payload["stage"], "foundation-only")
        self.assertEqual(
            foundation.payload["pending_actions"],
            ["prepare", "sync"],
        )

        self.seed_self_contained_bootstrap_state(workspace, package_manager="uv")
        with self.neutral_host_execution_context():
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

    def test_status_ignores_office_temporary_lock_files_in_source_counts(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        (workspace.source_dir / "deck.pptx").write_text("real deck\n", encoding="utf-8")
        (workspace.source_dir / "~$deck.pptx").write_text("office lock\n", encoding="utf-8")

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        self.assertEqual(report.payload["source_documents"]["counts"]["pptx"], 1)
        self.assertEqual(report.payload["source_documents"]["tiers"]["office_pdf"]["total"], 1)

    def test_status_surfaces_legacy_publish_storage_note_before_migration(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")
        current_artifact = workspace.knowledge_base_current_dir / "artifact.md"
        current_artifact.parent.mkdir(parents=True, exist_ok=True)
        current_artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-15T01:00:00Z",
                "last_sync_at": "2026-03-15T01:00:00Z",
            },
        )
        write_json(
            workspace.knowledge_base_dir / "current-pointer.json",
            {"snapshot_id": "snapshot-current"},
        )
        for snapshot_id, published_at in (
            ("snapshot-current", "2026-03-29T01:00:00Z"),
            ("snapshot-recent", "2026-03-28T01:00:00Z"),
            ("snapshot-recent-b", "2026-03-27T01:00:00Z"),
        ):
            snapshot_dir = workspace.knowledge_version_dir(snapshot_id)
            snapshot_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                snapshot_dir / "publish_manifest.json",
                {
                    "snapshot_id": snapshot_id,
                    "published_at": published_at,
                    "validation_status": "valid",
                    "published_source_signature": f"sig-{snapshot_id}",
                },
            )
            write_json(
                snapshot_dir / "validation_report.json",
                {
                    "status": "valid",
                    "source_signature": f"sig-{snapshot_id}",
                },
            )

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.payload["knowledge_base"]["publish_model"], "single-current")
        self.assertTrue(report.payload["knowledge_base"]["legacy_archive_detected"])
        self.assertEqual(report.payload["knowledge_base"]["legacy_archive_version_count"], 3)
        self.assertEqual(report.payload["knowledge_base"]["publish_ledger_count"], 0)
        storage_lifecycle = report.payload["knowledge_base"]["storage_lifecycle"]
        self.assertGreater(storage_lifecycle["family_count"], 0)
        self.assertEqual(storage_lifecycle["published_root_count"], 0)
        self.assertEqual(storage_lifecycle["publish_ledger_count"], 0)
        self.assertIn(
            "Storage lifecycle: ",
            "\n".join(report.lines),
        )
        self.assertIn("Legacy publish storage: detected", "\n".join(report.lines))

    def test_status_surfaces_rebuild_and_lane_b_follow_up_summaries(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")
        current_artifact = workspace.knowledge_base_current_dir / "artifact.md"
        current_artifact.parent.mkdir(parents=True, exist_ok=True)
        current_artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-15T01:00:00Z",
                "last_sync_at": "2026-03-15T01:00:00Z",
                "rebuild_telemetry": {
                    "rebuild_cause": "artifact-contract-backfill",
                    "dirty_source_count": 0,
                    "contract_backfill_source_count": 1,
                    "interaction_promotion_only": False,
                    "scoped_contract_repair_used": True,
                },
                "lane_b_follow_up_summary": {
                    "state": "running",
                    "selected_source_count": 1,
                    "selected_unit_count": 3,
                    "covered_unit_count": 1,
                    "blocked_unit_count": 0,
                    "remaining_unit_count": 2,
                },
            },
        )

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        self.assertEqual(
            report.payload["knowledge_base"]["last_sync_rebuild_telemetry"]["rebuild_cause"],
            "artifact-contract-backfill",
        )
        self.assertEqual(report.payload["knowledge_base"]["lane_b_follow_up"]["state"], "running")
        self.assertIn(
            "Last sync rebuild: cause=artifact-contract-backfill",
            "\n".join(report.lines),
        )
        self.assertIn("Lane B follow-up: state=running", "\n".join(report.lines))

    def test_status_reconciles_lane_b_summary_with_settled_shared_job(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")
        current_artifact = workspace.knowledge_base_current_dir / "artifact.md"
        current_artifact.parent.mkdir(parents=True, exist_ok=True)
        current_artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-15T01:00:00Z",
                "last_sync_at": "2026-03-15T01:00:00Z",
                "lane_b_follow_up_summary": {
                    "state": "running",
                    "job_id": "job-lane-b",
                    "selected_source_count": 2,
                    "selected_unit_count": 6,
                    "covered_unit_count": 0,
                    "blocked_unit_count": 0,
                    "remaining_unit_count": 6,
                },
            },
        )
        job = ensure_shared_job(
            workspace,
            job_key="lane-b:staging:test",
            job_family="lane-b",
            criticality="background",
            scope={"target": "staging"},
            input_signature="lane-b:staging:test",
            owner={"kind": "command", "id": "lane-b:test", "pid": os.getpid()},
        )
        from docmason.control_plane import block_shared_job

        block_shared_job(
            workspace,
            str(job["manifest"]["job_id"]),
            result={
                "selected_unit_count": 6,
                "covered_unit_count": 2,
                "blocked_unit_count": 4,
                "remaining_unit_count": 0,
            },
        )
        state = read_json(workspace.sync_state_path)
        state["lane_b_follow_up_summary"]["job_id"] = str(job["manifest"]["job_id"])
        write_json(workspace.sync_state_path, state)

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        lane_b = report.payload["knowledge_base"]["lane_b_follow_up"]
        self.assertEqual(lane_b["state"], "blocked")
        self.assertEqual(lane_b["covered_unit_count"], 2)
        self.assertEqual(lane_b["blocked_unit_count"], 4)
        self.assertEqual(lane_b["remaining_unit_count"], 0)
        self.assertIn("Lane B follow-up: state=blocked", "\n".join(report.lines))

    def test_status_blocks_lane_b_summary_for_inactive_command_owner(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")
        current_artifact = workspace.knowledge_base_current_dir / "artifact.md"
        current_artifact.parent.mkdir(parents=True, exist_ok=True)
        current_artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-15T01:00:00Z",
                "last_sync_at": "2026-03-15T01:00:00Z",
                "lane_b_follow_up_summary": {
                    "state": "running",
                    "selected_source_count": 1,
                    "selected_unit_count": 3,
                    "covered_unit_count": 1,
                    "blocked_unit_count": 0,
                    "remaining_unit_count": 2,
                },
            },
        )
        job = ensure_shared_job(
            workspace,
            job_key="lane-b:inactive-command-owner",
            job_family="lane-b",
            criticality="background",
            scope={"target": "staging"},
            input_signature="lane-b:inactive-command-owner",
            owner={"kind": "command", "id": "lane-b:test", "pid": 999999},
        )
        state = read_json(workspace.sync_state_path)
        state["lane_b_follow_up_summary"]["job_id"] = str(job["manifest"]["job_id"])
        write_json(workspace.sync_state_path, state)

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        lane_b = report.payload["knowledge_base"]["lane_b_follow_up"]
        self.assertEqual(lane_b["state"], "blocked")
        self.assertIn("Lane B follow-up: state=blocked", "\n".join(report.lines))
        self.assertEqual(
            load_shared_job(workspace, str(job["manifest"]["job_id"]))["status"],
            "blocked",
        )

    def test_status_blocks_lane_b_summary_for_inactive_owner_run(self) -> None:
        workspace = self.make_workspace()
        self.seed_self_contained_bootstrap_state(workspace)
        source_file = workspace.source_dir / "example.pdf"
        source_file.write_text("pdf placeholder\n", encoding="utf-8")
        current_artifact = workspace.knowledge_base_current_dir / "artifact.md"
        current_artifact.parent.mkdir(parents=True, exist_ok=True)
        current_artifact.write_text("compiled knowledge\n", encoding="utf-8")
        write_json(
            workspace.sync_state_path,
            {
                "published_source_signature": source_inventory_signature(workspace),
                "last_publish_at": "2026-03-15T01:00:00Z",
                "last_sync_at": "2026-03-15T01:00:00Z",
                "lane_b_follow_up_summary": {
                    "state": "running",
                    "selected_source_count": 1,
                    "selected_unit_count": 3,
                    "covered_unit_count": 1,
                    "blocked_unit_count": 0,
                    "remaining_unit_count": 2,
                },
            },
        )
        run_dir = workspace.runs_dir / "run-finished"
        run_dir.mkdir(parents=True, exist_ok=True)
        write_json(
            run_dir / "state.json",
            {
                "run_id": "run-finished",
                "status": "completed",
            },
        )
        job = ensure_shared_job(
            workspace,
            job_key="lane-b:inactive-owner-run",
            job_family="lane-b",
            criticality="background",
            scope={"target": "staging"},
            input_signature="lane-b:inactive-owner-run",
            owner={"kind": "run", "id": "run-finished"},
        )
        state = read_json(workspace.sync_state_path)
        state["lane_b_follow_up_summary"]["job_id"] = str(job["manifest"]["job_id"])
        write_json(workspace.sync_state_path, state)

        report = status_workspace(workspace, editable_install_probe=self.ready_probe)

        lane_b = report.payload["knowledge_base"]["lane_b_follow_up"]
        self.assertEqual(lane_b["state"], "blocked")
        self.assertIn("Lane B follow-up: state=blocked", "\n".join(report.lines))
        self.assertEqual(
            load_shared_job(workspace, str(job["manifest"]["job_id"]))["status"],
            "running",
        )

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

    def test_status_and_workspace_state_surface_host_access_upgrade_without_prepare_yes(
        self,
    ) -> None:
        workspace = self.make_workspace()
        ensure_shared_job(
            workspace,
            job_key=f"prepare:{workspace.root}:host-access-upgrade:cap",
            job_family="prepare",
            criticality="answer-critical",
            scope={"workspace_root": str(workspace.root)},
            input_signature="cap",
            owner={"kind": "command", "id": "prepare-command"},
            requires_confirmation=True,
            confirmation_kind="host-access-upgrade",
            confirmation_prompt=(
                "DocMason is currently running in Codex `Default permissions`."
            ),
            confirmation_reason="machine-baseline",
        )

        status_report = status_workspace(workspace, editable_install_probe=self.missing_probe)
        self.assertIn("switch-host-to-full-access", status_report.payload["pending_actions"])
        self.assertNotIn("prepare --yes", status_report.payload["pending_actions"])

        snapshot = workspace_state_snapshot(workspace)
        self.assertIn("switch-host-to-full-access", snapshot["next_legal_actions"])
        self.assertNotIn("prepare --yes", snapshot["next_legal_actions"])

        doctor_report = doctor_workspace(workspace, editable_install_probe=self.missing_probe)
        checks = {check["name"]: check for check in doctor_report.payload["checks"]}
        self.assertEqual(
            checks["control-plane"]["action"],
            "Switch Codex to `Full access`, then continue the same task.",
        )

    def test_status_and_workspace_state_surface_raw_host_access_gap(self) -> None:
        workspace = self.make_workspace()
        doctor_report = doctor_workspace(
            workspace,
            editable_install_probe=self.missing_probe,
        )
        environment = dict(doctor_report.payload["environment"])
        environment["ready"] = False
        environment["host_access_required"] = True
        environment["host_access_guidance"] = (
            "Switch Codex to `Full access`, then continue the same task."
        )
        environment["host_access_reasons"] = [
            "Repo-local runtime bootstrap needs network downloads."
        ]
        environment["machine_baseline_status"] = "host-access-upgrade-required"
        environment["workspace_write_network_access"] = False

        with mock.patch("docmason.commands.environment_snapshot", return_value=environment):
            status_report = status_workspace(workspace, editable_install_probe=self.missing_probe)
            self.assertIn("switch-host-to-full-access", status_report.payload["pending_actions"])
            self.assertNotIn("prepare", status_report.payload["pending_actions"])

            snapshot = workspace_state_snapshot(workspace)
            self.assertIn("switch-host-to-full-access", snapshot["next_legal_actions"])
            self.assertNotIn("prepare", snapshot["next_legal_actions"])

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

    def test_first_publish_sync_signature_is_stable_across_preview_only_source_ids(self) -> None:
        active_sources_a = [
            {
                "source_id": "preview-source-a",
                "current_path": "original_doc/a.pdf",
                "source_fingerprint": "fingerprint-a",
                "identity_basis": "path",
                "change_classification": "added",
            }
        ]
        active_sources_b = [
            {
                "source_id": "preview-source-b",
                "current_path": "original_doc/a.pdf",
                "source_fingerprint": "fingerprint-a",
                "identity_basis": "path",
                "change_classification": "added",
            }
        ]
        change_set_a = {
            "changes": [
                {
                    "source_id": "preview-source-a",
                    "change_classification": "added",
                    "current_path": "original_doc/a.pdf",
                    "previous_path": "",
                    "source_fingerprint": "fingerprint-a",
                    "matched_source_ids": [],
                }
            ]
        }
        change_set_b = {
            "changes": [
                {
                    "source_id": "preview-source-b",
                    "change_classification": "added",
                    "current_path": "original_doc/a.pdf",
                    "previous_path": "",
                    "source_fingerprint": "fingerprint-a",
                    "matched_source_ids": [],
                }
            ]
        }

        signature_a = sync_input_signature(
            active_sources=active_sources_a,
            change_set=change_set_a,
            pending_interaction_signature_value="pending:none",
        )
        signature_b = sync_input_signature(
            active_sources=active_sources_b,
            change_set=change_set_b,
            pending_interaction_signature_value="pending:none",
        )

        self.assertEqual(signature_a, signature_b)

    def test_stable_first_publish_sync_signature_reuses_one_shared_sync_job(self) -> None:
        workspace = self.make_workspace()
        signature = sync_input_signature(
            active_sources=[
                {
                    "source_id": "preview-source-any",
                    "current_path": "original_doc/a.pdf",
                    "source_fingerprint": "fingerprint-a",
                    "identity_basis": "path",
                    "change_classification": "added",
                }
            ],
            change_set={
                "changes": [
                    {
                        "source_id": "preview-source-any",
                        "change_classification": "added",
                        "current_path": "original_doc/a.pdf",
                        "previous_path": "",
                        "source_fingerprint": "fingerprint-a",
                        "matched_source_ids": [],
                    }
                ]
            },
            pending_interaction_signature_value="pending:none",
        )

        owner = ensure_shared_job(
            workspace,
            job_key=f"sync:{signature}",
            job_family="sync",
            criticality="answer-critical",
            scope={"target": "current"},
            input_signature=signature,
            owner={"kind": "run", "id": "run-owner"},
            run_id="run-owner",
        )
        waiter = ensure_shared_job(
            workspace,
            job_key=f"sync:{signature}",
            job_family="sync",
            criticality="answer-critical",
            scope={"target": "current"},
            input_signature=signature,
            owner={"kind": "run", "id": "run-waiter"},
            run_id="run-waiter",
        )

        self.assertEqual(owner["caller_role"], "owner")
        self.assertEqual(waiter["caller_role"], "waiter")
        self.assertEqual(owner["manifest"]["job_id"], waiter["manifest"]["job_id"])

    def test_sync_adapters_generates_deterministic_claude_files(self) -> None:
        workspace = self.make_workspace()
        (workspace.canonical_skills_dir / "ask").mkdir(parents=True)
        (workspace.canonical_skills_dir / "ask" / "SKILL.md").write_text(
            "# Ask\n",
            encoding="utf-8",
        )
        self.seed_workflow_metadata(
            workspace.canonical_skills_dir / "ask",
            workflow_id="ask",
            category="answer",
            mutability="read-only",
            parallelism="read-only-safe",
            background_commands=["docmason _ask"],
        )
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
        self.assertIn("@../../skills/canonical/ask/SKILL.md", project_memory_text)
        self.assertIn("@../../skills/canonical/workspace-bootstrap/SKILL.md", project_memory_text)
        self.assertIn("@../../skills/canonical/workspace-doctor/SKILL.md", project_memory_text)
        self.assertIn("## Foundation Workflows", workflow_routing_text)
        self.assertIn("### `workspace-bootstrap`", workflow_routing_text)
        self.assertIn("### `workspace-doctor`", workflow_routing_text)
        self.assertIn("### `ask`", workflow_routing_text)
        self.assertIn("`docmason _ask`", workflow_routing_text)
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

"""Shared ready-workspace fixtures for toolchain-aware tests."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import sys
from pathlib import Path

from docmason.libreoffice_runtime import LIBREOFFICE_PROBE_CONTRACT
from docmason.project import BOOTSTRAP_STATE_SCHEMA_VERSION, WorkspacePaths, write_json


def seed_repo_local_managed_python(
    workspace: WorkspacePaths,
    *,
    version: str = "3.13.5",
) -> Path:
    """Create a fake repo-local managed Python install for toolchain-aware tests."""
    install_root = workspace.toolchain_python_installs_dir / f"cpython-{version}"
    minor_version = ".".join(version.split(".")[:2])
    python_path = install_root / "bin" / f"python{minor_version}"
    python_path.parent.mkdir(parents=True, exist_ok=True)
    python_path.write_text(
        (
            "#!/bin/sh\n"
            "export PYTHONPATH="
            f"{shlex.quote(str(workspace.root / 'src'))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
        ),
        encoding="utf-8",
    )
    python_path.chmod(
        python_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
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
    workspace: WorkspacePaths,
    *,
    managed_python: Path | None = None,
    version: str = "3.13.5",
) -> Path:
    """Create a fake repo-local `.venv` anchored to the managed Python install."""
    managed = managed_python or seed_repo_local_managed_python(workspace, version=version)
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
    return managed


def seed_external_python(
    workspace: WorkspacePaths,
    *,
    name: str = "python3",
) -> str:
    """Create a fake external Python helper outside the repo-local toolchain boundary."""
    external_python = workspace.root / ".external-python" / "bin" / name
    external_python.parent.mkdir(parents=True, exist_ok=True)
    external_python.write_text(
        (
            "#!/bin/sh\n"
            "export PYTHONPATH="
            f"{shlex.quote(str(workspace.root / 'src'))}${{PYTHONPATH:+:$PYTHONPATH}}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
        ),
        encoding="utf-8",
    )
    external_python.chmod(
        external_python.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )
    return str(external_python)


def seed_self_contained_bootstrap_state(
    workspace: WorkspacePaths,
    *,
    prepared_at: str = "2026-03-25T00:00:00Z",
    checked_at: str | None = None,
    package_manager: str = "uv",
    office_renderer_ready: bool = True,
    pdf_renderer_ready: bool = True,
    uv_bootstrap_mode: str = "shared-uv",
) -> None:
    """Record a schema-v3 self-contained bootstrap marker for one test workspace."""
    timestamp = checked_at or prepared_at
    managed_python = seed_repo_local_venv(workspace)
    resolved_managed_python = str(managed_python.resolve())
    write_json(
        workspace.bootstrap_state_path,
        {
            "schema_version": BOOTSTRAP_STATE_SCHEMA_VERSION,
            "status": "ready",
            "environment_ready": True,
            "workspace_runtime_ready": True,
            "machine_baseline_ready": True,
            "machine_baseline_status": "ready",
            "checked_at": timestamp,
            "prepared_at": prepared_at,
            "workspace_root": str(workspace.root.resolve()),
            "package_manager": package_manager,
            "bootstrap_source": "repo-local-managed",
            "python_executable": resolved_managed_python,
            "venv_python": ".venv/bin/python",
            "editable_install": True,
            "editable_install_detail": "Editable install resolves to the workspace source tree.",
            "python_baseline": "3.13",
            "toolchain_root": ".docmason/toolchain",
            "toolchain_mode": "repo-local-managed",
            "managed_python_executable": resolved_managed_python,
            "managed_python_version": "3.13.5",
            "managed_python_origin": "repo-local-managed",
            "venv_base_executable": resolved_managed_python,
            "venv_health": "ready",
            "entrypoint_health": "ready",
            "uv_bootstrap_mode": uv_bootstrap_mode,
            "uv_cache_dir": ".docmason/toolchain/cache/uv",
            "pip_cache_dir": ".docmason/toolchain/cache/pip",
            "isolation_grade": "self-contained",
            "shared_host_dependency": False,
            "shared_host_dependencies": [],
            "repair_recommended": False,
            "repair_reason": None,
            "last_repair_at": timestamp,
            "host_access_required": False,
            "host_access_guidance": None,
            "machine_baseline_detail": "Native Codex machine baseline is ready.",
            "office_probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "libreoffice_candidate_binary": None,
            "libreoffice_validation_detail": (
                "Validated LibreOffice renderer capability."
                if office_renderer_ready
                else "No LibreOffice command candidate was detected."
            ),
            "libreoffice_detected_but_unusable": False,
            "libreoffice_blocked_by_host_access": False,
            "homebrew_ready": True,
            "homebrew_binary": "/opt/homebrew/bin/brew",
            "pdf_renderer_ready": pdf_renderer_ready,
            "office_renderer_ready": office_renderer_ready,
            "office_renderer_required": False,
            "requires_pdf_renderer": False,
            "requires_office_renderer": False,
            "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
        },
    )


def seed_mixed_external_venv_bootstrap_state(
    workspace: WorkspacePaths,
    *,
    external_python: str | None = None,
    prepared_at: str = "2026-03-25T00:00:00Z",
) -> None:
    """Record a schema-v3 mixed state where `.venv` is externally anchored."""
    external_python = external_python or seed_external_python(workspace)
    managed_python = seed_repo_local_managed_python(workspace)
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
    write_json(
        workspace.bootstrap_state_path,
        {
            "schema_version": BOOTSTRAP_STATE_SCHEMA_VERSION,
            "status": "action-required",
            "environment_ready": False,
            "workspace_runtime_ready": False,
            "machine_baseline_ready": True,
            "machine_baseline_status": "ready",
            "checked_at": prepared_at,
            "prepared_at": prepared_at,
            "workspace_root": str(workspace.root.resolve()),
            "package_manager": "uv",
            "bootstrap_source": "repo-local-managed",
            "python_executable": str(managed_python.resolve()),
            "venv_python": ".venv/bin/python",
            "editable_install": True,
            "editable_install_detail": "Editable install resolves to the workspace source tree.",
            "python_baseline": "3.13",
            "toolchain_root": ".docmason/toolchain",
            "toolchain_mode": "repo-local-managed",
            "managed_python_executable": str(managed_python.resolve()),
            "managed_python_version": "3.13.5",
            "managed_python_origin": "repo-local-managed",
            "venv_base_executable": external_python,
            "venv_health": "external-provenance",
            "entrypoint_health": "broken-shebang",
            "uv_bootstrap_mode": "shared-uv",
            "uv_cache_dir": ".docmason/toolchain/cache/uv",
            "pip_cache_dir": ".docmason/toolchain/cache/pip",
            "isolation_grade": "mixed",
            "shared_host_dependency": True,
            "shared_host_dependencies": [external_python],
            "repair_recommended": True,
            "repair_reason": "external-venv-provenance",
            "last_repair_at": prepared_at,
            "host_access_required": False,
            "host_access_guidance": None,
            "machine_baseline_detail": "Native Codex machine baseline is ready.",
            "office_probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "libreoffice_candidate_binary": None,
            "libreoffice_validation_detail": "Validated LibreOffice renderer capability.",
            "libreoffice_detected_but_unusable": False,
            "libreoffice_blocked_by_host_access": False,
            "homebrew_ready": True,
            "homebrew_binary": "/opt/homebrew/bin/brew",
            "pdf_renderer_ready": True,
            "office_renderer_ready": True,
            "office_renderer_required": False,
            "requires_pdf_renderer": False,
            "requires_office_renderer": False,
            "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
        },
    )


def seed_degraded_broken_venv_bootstrap_state(
    workspace: WorkspacePaths,
    *,
    prepared_at: str = "2026-03-25T00:00:00Z",
) -> None:
    """Record a schema-v3 degraded state where `.venv` points to a broken interpreter path."""
    managed_python = seed_repo_local_managed_python(workspace)
    workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
    if workspace.venv_python.exists() or workspace.venv_python.is_symlink():
        workspace.venv_python.unlink()
    os.symlink("missing-python3.13", workspace.venv_python)
    workspace.venv_docmason.parent.mkdir(parents=True, exist_ok=True)
    workspace.venv_docmason.write_text(
        f"#!{workspace.venv_python}\nprint('docmason')\n",
        encoding="utf-8",
    )
    workspace.venv_docmason.chmod(0o755)
    write_json(
        workspace.bootstrap_state_path,
        {
            "schema_version": BOOTSTRAP_STATE_SCHEMA_VERSION,
            "status": "action-required",
            "environment_ready": False,
            "workspace_runtime_ready": False,
            "machine_baseline_ready": True,
            "machine_baseline_status": "ready",
            "checked_at": prepared_at,
            "prepared_at": prepared_at,
            "workspace_root": str(workspace.root.resolve()),
            "package_manager": "uv",
            "bootstrap_source": "repo-local-managed",
            "python_executable": str(managed_python.resolve()),
            "venv_python": ".venv/bin/python",
            "editable_install": True,
            "editable_install_detail": "Editable install resolves to the workspace source tree.",
            "python_baseline": "3.13",
            "toolchain_root": ".docmason/toolchain",
            "toolchain_mode": "repo-local-managed",
            "managed_python_executable": str(managed_python.resolve()),
            "managed_python_version": "3.13.5",
            "managed_python_origin": "repo-local-managed",
            "venv_base_executable": str(workspace.venv_python),
            "venv_health": "broken-symlink",
            "entrypoint_health": "broken-shebang",
            "uv_bootstrap_mode": "shared-uv",
            "uv_cache_dir": ".docmason/toolchain/cache/uv",
            "pip_cache_dir": ".docmason/toolchain/cache/pip",
            "isolation_grade": "degraded",
            "shared_host_dependency": False,
            "shared_host_dependencies": [],
            "repair_recommended": True,
            "repair_reason": "broken-venv-symlink",
            "last_repair_at": prepared_at,
            "host_access_required": False,
            "host_access_guidance": None,
            "machine_baseline_detail": "Native Codex machine baseline is ready.",
            "office_probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "libreoffice_candidate_binary": None,
            "libreoffice_validation_detail": "Validated LibreOffice renderer capability.",
            "libreoffice_detected_but_unusable": False,
            "libreoffice_blocked_by_host_access": False,
            "homebrew_ready": True,
            "homebrew_binary": "/opt/homebrew/bin/brew",
            "pdf_renderer_ready": True,
            "office_renderer_ready": True,
            "office_renderer_required": False,
            "requires_pdf_renderer": False,
            "requires_office_renderer": False,
            "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
        },
    )

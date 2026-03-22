"""Helpers for building temp workspaces from the tracked public sample corpus."""

from __future__ import annotations

import shutil
from pathlib import Path

from docmason.commands import sync_workspace
from docmason.project import WorkspacePaths, write_json

ROOT = Path(__file__).resolve().parents[1]


def make_minimal_workspace(root: Path) -> WorkspacePaths:
    """Create a minimal runnable DocMason workspace scaffold in `root`."""
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
    workspace = WorkspacePaths(root=root)
    workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
    workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    write_json(
        workspace.bootstrap_state_path,
        {
            "prepared_at": "2026-03-22T00:00:00Z",
            "package_manager": "uv",
            "python_executable": "/usr/bin/python3",
            "venv_python": ".venv/bin/python",
            "editable_install": True,
        },
    )
    return workspace


def materialize_public_markdown_subset(workspace: WorkspacePaths) -> None:
    """Copy the tracked public markdown corpus into a temp workspace."""
    sample_root = ROOT / "sample_corpus" / "ico-gcs"
    for domain in ("ico", "gcs"):
        source_dir = sample_root / domain
        target_dir = workspace.source_dir / domain
        target_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(source_dir.glob("*.md")):
            shutil.copy2(path, target_dir / path.name)


def build_public_markdown_workspace(root: Path) -> tuple[WorkspacePaths, dict[str, object]]:
    """Create a temp workspace, materialize public markdown fixtures, and run sync."""
    workspace = make_minimal_workspace(root)
    materialize_public_markdown_subset(workspace)
    report = sync_workspace(workspace).payload
    return workspace, report

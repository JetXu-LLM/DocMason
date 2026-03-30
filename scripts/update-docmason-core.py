#!/usr/bin/env python3
"""Compatibility wrapper for the stable `docmason update-core` command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper for `docmason update-core`.",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory to update. Defaults to the current directory.",
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="Local generated clean bundle zip to apply instead of downloading the latest core.",
    )
    return parser.parse_args()


def main() -> int:
    from docmason.commands import emit_report, update_core_workspace
    from docmason.project import WorkspacePaths

    args = parse_args()
    workspace = WorkspacePaths(root=args.workspace.resolve())
    bundle = args.bundle.resolve() if args.bundle else None
    report = update_core_workspace(workspace, bundle=bundle)
    return emit_report(report, as_json=False)


if __name__ == "__main__":
    raise SystemExit(main())

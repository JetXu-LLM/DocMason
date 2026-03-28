"""Internal background worker entrypoint for runtime projection refresh."""

from __future__ import annotations

import sys
from pathlib import Path

from .project import WorkspacePaths
from .projections import run_projection_refresh_worker


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        return 2
    workspace = WorkspacePaths(root=Path(args[0]).resolve())
    run_projection_refresh_worker(workspace, trigger="background-worker")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI wrapper
    raise SystemExit(main())

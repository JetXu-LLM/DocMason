"""Projection rebuild helpers for review-facing runtime artifacts."""

from __future__ import annotations

import os
from typing import Any

from .coordination import workspace_lease
from .control_plane import projection_inputs_digest
from .project import WorkspacePaths, read_json, write_json


def refresh_conversation_projections(paths: WorkspacePaths) -> None:
    """Refresh projection-only conversation views from live conversation state."""
    paths.conversation_projections_dir.mkdir(parents=True, exist_ok=True)
    live_files = {path.name for path in paths.conversations_dir.glob("*.json") if path.is_file()}
    for path in sorted(paths.conversations_dir.glob("*.json")):
        payload = read_json(path)
        if not payload:
            continue
        write_json(paths.conversation_projections_dir / path.name, payload)
    for path in sorted(paths.conversation_projections_dir.glob("*.json")):
        if path.name not in live_files:
            os.remove(path)


def refresh_runtime_projections(paths: WorkspacePaths) -> dict[str, Any]:
    """Rebuild runtime projections under one shared projection-refresh lease."""
    from .review import build_answer_history_index, build_benchmark_candidates, build_review_summary

    with workspace_lease(paths, "projection-refresh"):
        refresh_conversation_projections(paths)
        summary = build_review_summary(paths)
        write_json(paths.review_summary_path, summary)
        write_json(paths.benchmark_candidates_path, build_benchmark_candidates(paths, summary=summary))
        write_json(paths.answer_history_index_path, build_answer_history_index(paths))
        write_json(
            paths.projection_state_path,
            {
                "schema_version": 1,
                "updated_at": summary.get("generated_at"),
                "projection_inputs_digest": projection_inputs_digest(paths),
            },
        )
        return summary

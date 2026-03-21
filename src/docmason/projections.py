"""Projection rebuild helpers for review-facing runtime artifacts."""

from __future__ import annotations

from typing import Any

from .coordination import workspace_lease
from .project import WorkspacePaths, write_json


def refresh_runtime_projections(paths: WorkspacePaths) -> dict[str, Any]:
    """Rebuild runtime projections under one shared projection-refresh lease."""
    from .review import build_answer_history_index, build_benchmark_candidates, build_review_summary

    with workspace_lease(paths, "projection-refresh"):
        summary = build_review_summary(paths)
        write_json(paths.review_summary_path, summary)
        write_json(paths.benchmark_candidates_path, build_benchmark_candidates(paths, summary=summary))
        write_json(paths.answer_history_index_path, build_answer_history_index(paths))
        return summary

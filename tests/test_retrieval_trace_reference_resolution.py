"""Retrieval, trace, and reference-resolution tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docmason.commands import retrieve_knowledge, trace_knowledge
from tests.support_reference_resolution import ReferenceResolutionTests
from tests.support_public_corpus import build_public_markdown_workspace
from tests.support_retrieval_trace_core import RetrievalTraceCoreTests


class PublicCorpusRetrievalTraceTests(unittest.TestCase):
    """Exercise retrieval and trace against the tracked ICO + GCS public markdown corpus."""

    def test_public_corpus_retrieval_prefers_oasis_for_campaign_planning_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            workspace, sync_payload = build_public_markdown_workspace(Path(tempdir_name))
            self.assertEqual(sync_payload["sync_status"], "valid")

            result = retrieve_knowledge(
                query="OASIS campaign planning audience implementation scoring",
                top=3,
                paths=workspace,
            )

            self.assertEqual(result.exit_code, 0)
            top_result = result.payload["results"][0]
            self.assertEqual(top_result["current_path"], "original_doc/gcs/oasis-campaign-planning.md")
            self.assertIn("Guide to Campaign Planning: OASIS", top_result["title"])

    def test_public_corpus_trace_returns_ico_governance_source_details(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            workspace, sync_payload = build_public_markdown_workspace(Path(tempdir_name))
            self.assertEqual(sync_payload["sync_status"], "valid")

            retrieve = retrieve_knowledge(
                query="AI fairness meaningful human review data protection",
                top=3,
                paths=workspace,
            )
            self.assertEqual(retrieve.exit_code, 0)
            source_id = retrieve.payload["results"][0]["source_id"]

            trace = trace_knowledge(source_id=source_id, paths=workspace)
            self.assertEqual(trace.exit_code, 0)
            self.assertIn("source", trace.payload)
            self.assertTrue(
                trace.payload["source"]["current_path"].startswith("original_doc/ico/")
            )


__all__ = [
    "RetrievalTraceCoreTests",
    "ReferenceResolutionTests",
    "PublicCorpusRetrievalTraceTests",
]

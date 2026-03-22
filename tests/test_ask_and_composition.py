"""Ask routing, grounded composition, and answer-surface tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docmason.ask import prepare_ask_turn
from tests.support_ask_core import AskRoutingAndCompositionTests
from tests.support_ask_hardening import AskHardeningTests
from tests.support_public_corpus import build_public_markdown_workspace


class PublicCorpusAskRoutingTests(unittest.TestCase):
    """Exercise ask routing against the tracked public markdown sample corpus."""

    def semantic_analysis(self, *, question_class: str) -> dict[str, object]:
        return {
            "question_class": question_class,
            "question_domain": "workspace-corpus",
            "route_reason": f"Public corpus test classified the question as {question_class}.",
            "needs_latest_workspace_state": False,
        }

    def test_public_corpus_ask_routes_campaign_question_to_grounded_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            workspace, sync_payload = build_public_markdown_workspace(Path(tempdir_name))
            self.assertEqual(sync_payload["sync_status"], "valid")

            prepared = prepare_ask_turn(
                workspace,
                question="What does OASIS say about audience insight and scoring?",
                semantic_analysis=self.semantic_analysis(question_class="answer"),
            )

            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(prepared["inner_workflow_id"], "grounded-answer")
            self.assertFalse(prepared["knowledge_base_missing"])
            self.assertEqual(prepared["question_domain"], "workspace-corpus")

    def test_public_corpus_ask_routes_research_request_to_grounded_composition(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            workspace, sync_payload = build_public_markdown_workspace(Path(tempdir_name))
            self.assertEqual(sync_payload["sync_status"], "valid")

            prepared = prepare_ask_turn(
                workspace,
                question=(
                    "Compare the GCS evaluation material with the ICO governance material for "
                    "an audience-segmentation campaign."
                ),
                semantic_analysis=self.semantic_analysis(question_class="composition"),
            )

            self.assertEqual(prepared["status"], "prepared")
            self.assertEqual(prepared["inner_workflow_id"], "grounded-composition")
            self.assertEqual(prepared["question_domain"], "workspace-corpus")


__all__ = [
    "AskRoutingAndCompositionTests",
    "AskHardeningTests",
    "PublicCorpusAskRoutingTests",
]

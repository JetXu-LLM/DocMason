"""Repository-level foundation checks for the DocMason public scaffold."""

from __future__ import annotations

import os
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryFoundationTests(unittest.TestCase):
    """Verify the committed repository foundation and public metadata."""

    def test_expected_foundation_files_exist(self) -> None:
        expected = [
            ROOT / "README.md",
            ROOT / "AGENTS.md",
            ROOT / "LICENSE",
            ROOT / ".gitignore",
            ROOT / "pyproject.toml",
            ROOT / "docmason.yaml",
            ROOT / "CONTRIBUTING.md",
            ROOT / "SECURITY.md",
            ROOT / "docs" / "README.md",
            ROOT / "docs" / "workflows" / "execution-orchestration.md",
            ROOT / "docs" / "workflows" / "operator-eval.md",
            ROOT / "scripts" / "bootstrap-workspace.sh",
            ROOT / "src" / "docmason" / "__init__.py",
            ROOT / "src" / "docmason" / "__main__.py",
            ROOT / "src" / "docmason" / "ask.py",
            ROOT / "src" / "docmason" / "conversation.py",
            ROOT / "src" / "docmason" / "evaluation.py",
            ROOT / "src" / "docmason" / "interaction.py",
            ROOT / "src" / "docmason" / "operator_eval.py",
            ROOT / "src" / "docmason" / "transcript.py",
            ROOT / "skills" / "canonical" / "ask" / "SKILL.md",
            ROOT / "skills" / "canonical" / "ask" / "workflow.json",
            ROOT / "skills" / "canonical" / "workspace-bootstrap" / "SKILL.md",
            ROOT / "skills" / "canonical" / "workspace-bootstrap" / "workflow.json",
            ROOT / "skills" / "canonical" / "workspace-doctor" / "workflow.json",
            ROOT / "skills" / "canonical" / "workspace-status" / "workflow.json",
            ROOT / "skills" / "canonical" / "adapter-sync" / "workflow.json",
            ROOT / "skills" / "canonical" / "knowledge-base-sync" / "SKILL.md",
            ROOT / "skills" / "canonical" / "knowledge-base-sync" / "workflow.json",
            ROOT / "skills" / "canonical" / "knowledge-construction" / "SKILL.md",
            ROOT / "skills" / "canonical" / "knowledge-construction" / "workflow.json",
            ROOT / "skills" / "canonical" / "retrieval-workflow" / "SKILL.md",
            ROOT / "skills" / "canonical" / "retrieval-workflow" / "workflow.json",
            ROOT / "skills" / "canonical" / "provenance-trace" / "SKILL.md",
            ROOT / "skills" / "canonical" / "provenance-trace" / "workflow.json",
            ROOT / "skills" / "canonical" / "validation-repair" / "SKILL.md",
            ROOT / "skills" / "canonical" / "validation-repair" / "workflow.json",
            ROOT / "skills" / "canonical" / "grounded-answer" / "SKILL.md",
            ROOT / "skills" / "canonical" / "grounded-answer" / "workflow.json",
            ROOT / "skills" / "canonical" / "grounded-composition" / "SKILL.md",
            ROOT / "skills" / "canonical" / "grounded-composition" / "workflow.json",
            ROOT / "skills" / "canonical" / "runtime-log-review" / "SKILL.md",
            ROOT / "skills" / "canonical" / "runtime-log-review" / "workflow.json",
            ROOT / "skills" / "operator" / "operator-eval" / "SKILL.md",
            ROOT / "skills" / "operator" / "operator-eval" / "workflow.json",
            ROOT / "skills" / "operator" / "operator-eval" / "examples" / "README.md",
            ROOT / "skills" / "operator" / "operator-eval" / "examples" / "template_request.json",
            ROOT / "skills" / "operator" / "operator-eval" / "examples" / "template_suite.json",
            ROOT / "skills" / "operator" / "operator-eval" / "examples" / "template_rubric.json",
            ROOT / "skills" / "operator" / "operator-eval" / "examples" / "template_baseline.json",
            ROOT / "skills" / "operator" / "operator-eval" / "examples" / "template_candidate.json",
            ROOT / "tests" / "test_phase_five_runtime.py",
            ROOT / "tests" / "test_phase_six_runtime.py",
            ROOT / "tests" / "test_phase_six_follow_on_runtime.py",
            ROOT / "tests" / "test_phase_six_hardening_runtime.py",
            ROOT / "tests" / "test_phase_six_b1_operator_eval_runtime.py",
        ]
        for path in expected:
            self.assertTrue(path.exists(), f"Expected file to exist: {path}")
        self.assertTrue(
            os.access(ROOT / "scripts" / "bootstrap-workspace.sh", os.X_OK),
            "Expected bootstrap-workspace.sh to be executable.",
        )

    def test_pyproject_core_metadata(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]

        self.assertEqual(project["name"], "docmason")
        self.assertEqual(project["readme"], "README.md")
        self.assertEqual(project["requires-python"], ">=3.11")
        self.assertIn("Apache Software License", " ".join(project["classifiers"]))
        self.assertEqual(project["scripts"]["docmason"], "docmason.cli:main")

    def test_gitignore_protects_private_directories(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in [
            "/original_doc/",
            "/knowledge_base/",
            "/runtime/",
            "/planning/",
            "/skills/private/",
            "/IMPLEMENTATION_PLAN.md",
        ]:
            self.assertIn(entry, text)

    def test_config_declares_native_reference_workflow(self) -> None:
        text = (ROOT / "docmason.yaml").read_text(encoding="utf-8")
        required_snippets = [
            "native_agent: codex",
            "platform: macos",
            "source_dir: original_doc",
            "knowledge_base_dir: knowledge_base",
            "strategy: heuristic-only",
            "publish_model: immutable-snapshot-plus-atomic-current-switch",
            (
                "current_completed_phase: "
                "phase-2-workspace-coordination-atomic-publish-and-projection-discipline"
            ),
            "next_phase: phase-3-spreadsheet-and-multimodal-evidence-compiler-deepening",
            "first_class_text:",
            "lightweight_text:",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)

    def test_readme_is_honest_about_phase_status(self) -> None:
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Historical implemented phases:", text)
        self.assertIn("Current architecture refactor program:", text)
        self.assertIn("Phase 1, Repository Foundation and Public Face", text)
        self.assertIn("Phase 2, Agent Operating Surface and Workspace Bootstrap", text)
        self.assertIn("Phase 3, Knowledge-Base Construction and Validation", text)
        self.assertIn("Phase 4, Incremental Maintenance, Retrieval, and Trace", text)
        phase_four_b = "Phase 4b, Workflow Productization and Execution Orchestration"
        self.assertIn(phase_four_b, text)
        phase_five = "Phase 5, Benchmarking, Evaluation, and Feedback Foundation"
        self.assertIn(phase_five, text)
        phase_six = "Phase 6, Natural Intent Routing and Conversation-Native Logging"
        self.assertIn(phase_six, text)
        self.assertIn(
            (
                "Phase 6 follow-on, Native Chat Reconciliation and Interaction-Derived "
                "Knowledge Overlay"
            ),
            text,
        )
        self.assertIn(
            (
                "Phase 6b1, Pre-Learning Boundary, Answer Contract, and "
                "Regression Closure"
            ),
            text,
        )
        self.assertIn("Phase 6b2, User-Native Source Reference Resolution", text)
        self.assertIn("Phase 6b3, Markdown and Plain-Text First-Class Knowledge Sources", text)
        self.assertIn("Phase 0, Rename To DocMason: implemented", text)
        self.assertIn(
            "Phase 1, Run Control, Turn Ownership, and Commit Barrier: implemented",
            text,
        )
        self.assertIn(
            "Phase 2, Workspace Coordination, Atomic Publish, and Projection Discipline: implemented",
            text,
        )
        self.assertIn(
            "Phase 3, Spreadsheet and Multimodal Evidence Compiler Deepening: planned",
            text,
        )
        self.assertIn(
            "Phase 4, Governed Interaction Memory and Operator Control Plane: planned",
            text,
        )


if __name__ == "__main__":
    unittest.main()

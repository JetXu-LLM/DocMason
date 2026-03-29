"""Repository foundation, contract, and public-surface checks."""

from __future__ import annotations

import os
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BANNED_TEST_PHRASES = (
    "architecture strategy deck",
    "delivery timeline plan",
    "project sponsor",
    "latency constraint",
    "platform team",
    "architecture review deck",
)


class FoundationAndContractTests(unittest.TestCase):
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
            ROOT / "docs" / "product" / "distribution-and-benchmarks.md",
            ROOT / "docs" / "setup" / "manual-workspace-recovery.md",
            ROOT / "docs" / "workflows" / "execution-orchestration.md",
            ROOT / "docs" / "workflows" / "operator-eval.md",
            ROOT / "scripts" / "bootstrap-workspace.sh",
            ROOT / "scripts" / "use-sample-corpus.py",
            ROOT / "scripts" / "build-distributions.py",
            ROOT / "scripts" / "update-docmason-core.py",
            ROOT / "scripts" / "check-repo-safety.py",
            ROOT / "scripts" / "install-git-hooks.sh",
            ROOT / ".githooks" / "README.md",
            ROOT / ".githooks" / "pre-commit",
            ROOT / ".githooks" / "pre-push",
            ROOT / ".github" / "copilot-instructions.md",
            ROOT / ".github" / "workflows" / "repository-checks.yml",
            ROOT / ".github" / "workflows" / "release-distributions.yml",
            ROOT / "sample_corpus" / "README.md",
            ROOT / "sample_corpus" / "ico-gcs" / "README.md",
            ROOT / "sample_corpus" / "ico-gcs" / "manifest.json",
            ROOT / "skills" / "optional" / "public-sample-workspace" / "SKILL.md",
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
            ROOT / "tests" / "test_foundation_and_contracts.py",
            ROOT / "tests" / "test_workspace_bootstrap_and_status.py",
            ROOT / "tests" / "test_distribution_and_privacy.py",
            ROOT / "tests" / "test_public_corpus_sync_and_materialization.py",
            ROOT / "tests" / "test_source_build_office_pdf.py",
            ROOT / "tests" / "test_source_build_text_email.py",
            ROOT / "tests" / "test_retrieval_trace_reference_resolution.py",
            ROOT / "tests" / "test_ask_and_composition.py",
            ROOT / "tests" / "test_interaction_ingest_and_review.py",
            ROOT / "tests" / "test_claude_code_adapter_and_hooks.py",
            ROOT / "tests" / "test_operator_eval_runtime.py",
        ]
        for path in expected:
            self.assertTrue(path.exists(), f"Expected file to exist: {path}")
        for executable in [
            ROOT / "scripts" / "bootstrap-workspace.sh",
            ROOT / "scripts" / "use-sample-corpus.py",
            ROOT / "scripts" / "build-distributions.py",
            ROOT / "scripts" / "update-docmason-core.py",
            ROOT / "scripts" / "check-repo-safety.py",
            ROOT / "scripts" / "install-git-hooks.sh",
            ROOT / ".githooks" / "pre-commit",
            ROOT / ".githooks" / "pre-push",
        ]:
            self.assertTrue(
                os.access(executable, os.X_OK),
                f"Expected {executable} to be executable.",
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
            "/original_doc/*",
            "/knowledge_base/*",
            "/runtime/*",
            "/adapters/*",
            "/.agents/",
            "/planning/",
            "/evals/",
            "/scripts/private/",
            "/skills/private/",
            "/IMPLEMENTATION_PLAN.md",
        ]:
            self.assertIn(entry, text)

    def test_committed_git_hooks_are_opt_in_and_layered(self) -> None:
        hooks_readme = (ROOT / ".githooks" / "README.md").read_text(encoding="utf-8")
        pre_commit = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
        pre_push = (ROOT / ".githooks" / "pre-push").read_text(encoding="utf-8")
        install_script = (ROOT / "scripts" / "install-git-hooks.sh").read_text(encoding="utf-8")

        self.assertIn("opt-in", hooks_readme)
        self.assertIn("./scripts/install-git-hooks.sh", hooks_readme)
        self.assertIn("--staged-only", pre_commit)
        self.assertNotIn("--staged-only", pre_push)
        self.assertIn("config core.hooksPath .githooks", install_script)

    def test_config_declares_native_reference_workflow(self) -> None:
        text = (ROOT / "docmason.yaml").read_text(encoding="utf-8")
        required_snippets = [
            "native_agent: codex",
            "platform: macos",
            "source_dir: original_doc",
            "sample_corpus_dir: sample_corpus",
            "knowledge_base_dir: knowledge_base",
            "strategy: heuristic-only",
            "publish_model: immutable-snapshot-plus-atomic-current-switch",
            (
                "current_completed_phase: "
                "phase-3-spreadsheet-and-multimodal-evidence-compiler-deepening"
            ),
            "next_phase: phase-4-governed-interaction-memory-and-operator-control-plane",
            "first_class_text:",
            "lightweight_text:",
        ]
        for snippet in required_snippets:
            self.assertIn(snippet, text)

    def test_agents_contract_stays_generic(self) -> None:
        text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn("sample_corpus/", text)
        self.assertNotIn("evals/", text)
        self.assertIn("original_doc/", text)
        self.assertIn("knowledge_base/", text)

    def test_front_door_contract_surfaces_are_current_and_explicit(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        ask_skill = (ROOT / "skills" / "canonical" / "ask" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        answer_skill = (
            ROOT / "skills" / "canonical" / "grounded-answer" / "SKILL.md"
        ).read_text(encoding="utf-8")
        composition_skill = (
            ROOT / "skills" / "canonical" / "grounded-composition" / "SKILL.md"
        ).read_text(encoding="utf-8")
        trace_skill = (
            ROOT / "skills" / "canonical" / "provenance-trace" / "SKILL.md"
        ).read_text(encoding="utf-8")
        retrieval_skill = (
            ROOT / "skills" / "canonical" / "retrieval-workflow" / "SKILL.md"
        ).read_text(encoding="utf-8")
        review_skill = (
            ROOT / "skills" / "canonical" / "runtime-log-review" / "SKILL.md"
        ).read_text(encoding="utf-8")
        orchestration_doc = (
            ROOT / "docs" / "workflows" / "execution-orchestration.md"
        ).read_text(encoding="utf-8")

        self.assertIn("`ask` remains the only ordinary natural-language front door", agents)
        self.assertIn("`rg --files`", agents)
        self.assertIn("do not guess how this repository should map onto your platform", agents)
        self.assertIn("what you are, or which assistant is operating here", agents)
        self.assertIn("canonical self-reference contract", agents)
        self.assertIn("Final user-facing replies should normally match the user's language", agents)
        self.assertIn("Do not commit or expose private corpus inputs", agents)
        self.assertIn("inspect the matching canonical skill instead of guessing from memory", agents)
        self.assertIn(
            "ordinary natural-language questions still enter through canonical `ask`",
            agents,
        )
        self.assertNotIn("## Procedure", agents)
        self.assertNotIn("## Completion Signal", agents)
        self.assertLess(
            len(agents.splitlines()),
            len(ask_skill.splitlines())
            + len(answer_skill.splitlines())
            + len(composition_skill.splitlines()),
        )
        self.assertIn("Reading this skill is not legal ask execution.", ask_skill)
        self.assertIn("Native-thread reconciliation is not legal ask execution.", ask_skill)
        self.assertIn("The routed inner workflow owns the deeper evidence loop.", ask_skill)
        self.assertIn("keep one concise `route_reason`", ask_skill)
        self.assertIn("route to `knowledge-base-sync`", ask_skill)
        self.assertIn("native ledger", ask_skill)
        self.assertNotIn(
            "upgrade that live turn into canonical ask ownership when reconciliation created it first",
            ask_skill,
        )
        self.assertNotIn("recommended_hybrid_targets", ask_skill)
        self.assertNotIn("Lane C owner", ask_skill)
        self.assertIn("not a free-standing ordinary front door", answer_skill)
        self.assertIn("canonical ask runtime ownership", answer_skill)
        self.assertIn("never a free-standing ordinary front door", composition_skill)
        self.assertIn("legal operator provenance surface", trace_skill)
        self.assertIn("legal operator evidence surface", retrieval_skill)
        self.assertIn("require canonical ask ownership", review_skill)
        self.assertIn("do not replace canonical `ask`", orchestration_doc)

    def test_contributing_points_to_optional_sample_skill(self) -> None:
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("public-sample-workspace", contributing)
        self.assertNotIn("evals/", contributing)

    def test_copilot_instructions_stay_minimal_and_agents_first(self) -> None:
        text = (ROOT / ".github" / "copilot-instructions.md").read_text(encoding="utf-8")
        self.assertIn("Start by reading `AGENTS.md`.", text)
        self.assertIn("minimal GitHub Copilot adaptation layer", text)
        self.assertIn("view_image", text)
        self.assertIn("main and sub agents", text)
        self.assertIn("Do not assume Claude-specific helpers", text)

    def test_gitignore_does_not_ignore_committed_copilot_instructions(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertNotIn("/.github/copilot-instructions.md", text)

    def test_banned_private_style_vocabulary_is_absent_from_tests(self) -> None:
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == "test_foundation_and_contracts.py":
                continue
            text = path.read_text(encoding="utf-8").lower()
            for phrase in BANNED_TEST_PHRASES:
                self.assertNotIn(phrase, text, f"Found banned phrase `{phrase}` in {path}")


if __name__ == "__main__":
    unittest.main()

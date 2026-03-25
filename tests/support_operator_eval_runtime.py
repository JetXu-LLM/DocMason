"""Tests for the operator evaluation loop and four-state answer contract."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import complete_ask_turn, prepare_ask_turn
from docmason.commands import run_workflow, sync_workspace
from docmason.evaluation import load_evaluation_baseline, load_evaluation_suite, load_rubric_definition
from docmason.operator_eval import load_operator_request
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import trace_answer_file
from docmason.workflows import load_workflow_metadata

ROOT = Path(__file__).resolve().parents[1]


class OperatorEvalRuntimeTests(unittest.TestCase):
    """Cover the hidden operator-eval surface and runtime/eval contracts."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        shutil.copytree(ROOT / "skills" / "canonical", root / "skills" / "canonical")
        shutil.copytree(ROOT / "skills" / "operator", root / "skills" / "operator")
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "runtime").mkdir()
        (root / "planning").mkdir()
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
        return WorkspacePaths(root=root)

    def mark_environment_ready(self, workspace: WorkspacePaths) -> None:
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "schema_version": 2,
                "status": "ready",
                "prepared_at": "2026-03-18T00:00:00Z",
                "environment_ready": True,
                "workspace_root": str(workspace.root.resolve()),
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
                "editable_install_detail": "Editable install resolves to the workspace source tree.",
                "office_renderer_ready": True,
                "pdf_renderer_ready": True,
                "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
            },
        )

    def create_pdf(self, path: Path, *, page_count: int = 1) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for index in range(page_count):
            writer.add_blank_page(width=144 + index, height=144 + index)
        with path.open("wb") as handle:
            writer.write(handle)

    def build_seeded_knowledge(
        self,
        source_dir: Path,
        *,
        title: str,
        summary: str,
        key_point: str,
        claim: str,
    ) -> None:
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        write_json(
            source_dir / "knowledge.json",
            {
                "source_id": source_manifest["source_id"],
                "source_fingerprint": source_manifest["source_fingerprint"],
                "title": title,
                "source_language": "en",
                "summary_en": summary,
                "summary_source": summary,
                "document_type": source_manifest["document_type"],
                "key_points": [
                    {
                        "text_en": key_point,
                        "text_source": key_point,
                        "citations": [{"unit_id": first_unit_id, "support": "key point"}],
                    }
                ],
                "entities": [{"name": title, "type": "test artifact"}],
                "claims": [
                    {
                        "statement_en": claim,
                        "statement_source": claim,
                        "citations": [{"unit_id": first_unit_id, "support": "claim"}],
                    }
                ],
                "known_gaps": [],
                "ambiguities": [],
                "confidence": {
                    "level": "high",
                    "notes_en": "Operator evaluation test fixture.",
                    "notes_source": "Operator evaluation test fixture.",
                },
                "citations": [{"unit_id": first_unit_id, "support": "summary support"}],
                "related_sources": [],
            },
        )
        (source_dir / "summary.md").write_text(
            "\n".join(
                [
                    f"# {title}",
                    "",
                    f"Source ID: {source_manifest['source_id']}",
                    "",
                    "## English Summary",
                    summary,
                    "",
                    "## Source-Language Summary",
                    summary,
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def publish_seeded_corpus(self, workspace: WorkspacePaths) -> list[str]:
        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = [item["source_id"] for item in pending.payload["pending_sources"]]
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[0],
            title="Campaign Planning Brief",
            summary="A strategy deck about architecture and operating model.",
            key_point="The strategy defines an architecture operating model.",
            claim="The architecture deck connects strategy to implementation.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / source_ids[1],
            title="Campaign Evaluation Plan",
            summary="A delivery timeline and companion planning document.",
            key_point="The timeline explains rollout milestones.",
            claim="The timeline complements the architecture strategy.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def write_external_manifest(self, workspace: WorkspacePaths) -> Path:
        manifest_path = workspace.agent_work_dir / "operator-eval" / "external-support.json"
        write_json(
            manifest_path,
            {
                "support_basis": "external-source-verified",
                "sources": [
                    {
                        "url": "https://example.com/doc",
                        "title": "Example official doc",
                        "source_type": "official-doc",
                        "support_snippet": "HTTPS is supported.",
                    }
                ],
                "key_assertions": ["Example external support assertion."],
            },
        )
        return manifest_path

    def write_broad_eval_assets(self, workspace: WorkspacePaths) -> None:
        benchmark_dir = workspace.eval_broad_benchmark_dir
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        retrieval_manifest = read_json(workspace.retrieval_manifest_path("current"))
        external_manifest_path = self.write_external_manifest(workspace)
        write_json(
            benchmark_dir / "rubric.json",
            {
                "schema_version": 1,
                "rubric_id": "operator-eval-test-rubric",
                "title": "Operator evaluation test rubric",
                "trial_count": 3,
                "judge_instructions": ["Score only active dimensions from the supplied evidence."],
                "acceptance_thresholds": {
                    "deterministic_pass_rate": 1.0,
                    "answer_mean_score": 1.5,
                    "aggregate_rubric_regression_limit": 0.2,
                },
                "dimensions": {
                    name: {
                        "description": name,
                        "score_0": "0",
                        "score_1": "1",
                        "score_2": "2",
                    }
                    for name in (
                        "factual_alignment",
                        "coverage",
                        "source_discipline",
                        "uncertainty_discipline",
                        "visual_evidence_handling",
                    )
                },
            },
        )
        write_json(
            benchmark_dir / "suite.json",
            {
                "schema_version": 1,
                "suite_id": "operator-eval-broad-test-suite",
                "title": "Operator evaluation broad test suite",
                "description": "A focused broad suite for operator-eval tests.",
                "target": "current",
                "corpus_signature": retrieval_manifest["source_signature"],
                "retrieval_strategy_id": "phase4b-lexical-plus-graph-v1",
                "answer_workflow_id": "phase4b-grounded-answer-v1",
                "cases": [
                    {
                        "case_id": "answer-grounded",
                        "family": "grounded-answer",
                        "execution_mode": "trace-answer",
                        "query_or_prompt": "The architecture strategy connects the operating model to implementation.",
                        "expected_primary_sources": [],
                        "required_sources_or_units": [],
                        "minimum_support_overlap": 0,
                        "forbidden_sources_or_units": [],
                        "expected_status": "ready",
                        "expected_answer_state": "grounded",
                        "expected_support_basis": "kb-grounded",
                        "expected_render_inspection_required": True,
                        "reference_facts": ["The answer should stay within published support."],
                        "active_rubric_dimensions": [],
                        "feedback_tags": ["coverage_gap"],
                        "critical": False,
                        "top": 3,
                        "graph_hops": 1,
                        "include_renders": True,
                        "execution_support_basis": "kb-grounded",
                        "execution_inner_workflow_id": "grounded-answer"
                    },
                    {
                        "case_id": "answer-explicit-abstain",
                        "family": "abstention",
                        "execution_mode": "trace-answer",
                        "query_or_prompt": "I cannot answer from the available evidence.",
                        "expected_primary_sources": [],
                        "required_sources_or_units": [],
                        "minimum_support_overlap": 0,
                        "forbidden_sources_or_units": [],
                        "expected_status": "ready",
                        "expected_answer_state": "abstained",
                        "expected_support_basis": "kb-grounded",
                        "expected_render_inspection_required": False,
                        "reference_facts": ["The abstention case must record explicit abstention."],
                        "active_rubric_dimensions": [],
                        "feedback_tags": ["should_abstain"],
                        "critical": True,
                        "top": 3,
                        "graph_hops": 1,
                        "include_renders": True,
                        "declared_answer_state": "abstained",
                        "execution_support_basis": "kb-grounded",
                        "execution_inner_workflow_id": "grounded-answer"
                    },
                    {
                        "case_id": "external-grounded",
                        "family": "external-source-verified-answer",
                        "execution_mode": "trace-answer",
                        "query_or_prompt": "HTTPS is supported by the externally verified product.",
                        "expected_primary_sources": [],
                        "required_sources_or_units": [],
                        "minimum_support_overlap": 0,
                        "forbidden_sources_or_units": [],
                        "expected_status": "ready",
                        "expected_answer_state": "grounded",
                        "expected_support_basis": "external-source-verified",
                        "expected_render_inspection_required": False,
                        "reference_facts": ["External support basis should allow a grounded result."],
                        "active_rubric_dimensions": [],
                        "feedback_tags": ["coverage_gap"],
                        "critical": False,
                        "top": 3,
                        "graph_hops": 1,
                        "include_renders": True,
                        "declared_answer_state": "grounded",
                        "execution_support_basis": "external-source-verified",
                        "execution_support_manifest_path": str(
                            external_manifest_path.relative_to(workspace.root)
                        ),
                        "execution_inner_workflow_id": "grounded-answer"
                    }
                ],
            },
        )
        write_json(
            benchmark_dir / "judge-trials.json",
            {
                "schema_version": 1,
                "suite_id": "operator-eval-broad-test-suite",
                "judge_profile": {
                    "mode": "agent-judge",
                    "agent_name": "codex",
                    "model_name": "gpt-5",
                    "workflow_id": "operator-eval-test-judge",
                    "trial_count": 3,
                },
                "trials_by_case": {},
            },
        )

    def write_operator_request(
        self,
        workspace: WorkspacePaths,
        *,
        action: str,
        suite: str,
        target_ids: list[str],
        run_label: str | None = None,
    ) -> None:
        write_json(
            workspace.eval_request_path,
            {
                "schema_version": 1,
                "action": action,
                "suite": suite,
                "target_ids": target_ids,
                "run_label": run_label,
                "operator_notes": "Operator test request.",
            },
        )

    def semantic_analysis(self) -> dict[str, object]:
        return {
            "question_class": "answer",
            "question_domain": "workspace-corpus",
            "route_reason": "Operator-eval candidate fixture.",
            "needs_latest_workspace_state": False,
        }

    def create_candidate_turn(self, workspace: WorkspacePaths) -> str:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-operator-candidate"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the architecture strategy connect to?",
                semantic_analysis=self.semantic_analysis(),
            )
        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The architecture strategy connects the operating model to implementation.\n\n"
            "It also proves DocMason already ships watch mode.",
            encoding="utf-8",
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-composition",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_state=trace["answer_state"],
            render_inspection_required=trace["render_inspection_required"],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Mixed evidence answer.",
            question_domain=turn["question_domain"],
            status="answered",
        )
        return f"candidate-{turn['conversation_id']}-{turn['turn_id']}"

    def create_mixed_support_candidate_turn(self, workspace: WorkspacePaths) -> tuple[str, str]:
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-operator-mixed"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What does the architecture strategy connect to, and what does it prove?",
                semantic_analysis=self.semantic_analysis(),
            )
        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The architecture strategy connects the operating model to implementation.\n\n"
            "It also proves DocMason already ships watch mode.",
            encoding="utf-8",
        )
        manifest_path = (
            workspace.agent_work_dir
            / turn["conversation_id"]
            / turn["turn_id"]
            / "external-support-manifest.json"
        )
        manifest_relative = str(manifest_path.relative_to(workspace.root))
        write_json(
            manifest_path,
            {
                "support_basis": "mixed",
                "answer_file_path": turn["answer_file_path"],
                "sources": [
                    {
                        "url": "https://example.com/operator-review",
                        "title": "Operator review note",
                        "source_type": "official-doc",
                        "support_snippet": "Example mixed-support reference.",
                    }
                ],
                "key_assertions": ["Example mixed-support assertion."],
            },
        )
        trace = trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context={
                **turn["log_context"],
                "support_basis": "mixed",
                "support_manifest_path": manifest_relative,
            },
        )
        complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-composition",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_state=trace["answer_state"],
            render_inspection_required=trace["render_inspection_required"],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="Mixed support answer.",
            question_domain=turn["question_domain"],
            support_basis="mixed",
            support_manifest_path=manifest_relative,
            status="answered",
        )
        return f"candidate-{turn['conversation_id']}-{turn['turn_id']}", manifest_relative

    def test_operator_workflow_registry_is_hidden_from_canonical_loading(self) -> None:
        workspace = self.make_workspace()
        canonical_workflows = {item.workflow_id for item in load_workflow_metadata(workspace)}
        all_workflows = {
            item.workflow_id for item in load_workflow_metadata(workspace, include_operator=True)
        }
        self.assertNotIn("operator-eval", canonical_workflows)
        self.assertIn("operator-eval", all_workflows)

    def test_operator_examples_validate(self) -> None:
        workspace = self.make_workspace()
        examples = ROOT / "skills" / "operator" / "operator-eval" / "examples"
        request = load_operator_request(examples / "template_request.json")
        self.assertEqual(request["action"], "run-suite")
        rubric = load_rubric_definition(examples / "template_rubric.json")
        suite = load_evaluation_suite(examples / "template_suite.json", rubric=rubric)
        self.assertEqual(suite["cases"][0]["expected_answer_state"], "abstained")
        baseline = load_evaluation_baseline(examples / "template_baseline.json")
        self.assertEqual(baseline["cases"][0]["answer_state"], "abstained")
        candidate = json.loads((examples / "template_candidate.json").read_text(encoding="utf-8"))
        self.assertEqual(candidate["proposed_case"]["declared_answer_state"], "abstained")

    def test_operator_eval_run_suite_and_freeze_baseline(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        self.write_broad_eval_assets(workspace)

        self.write_operator_request(
            workspace,
            action="run-suite",
            suite="broad",
            target_ids=[],
            run_label="Operator evaluation broad suite",
        )
        run_report = run_workflow("operator-eval", paths=workspace)
        self.assertEqual(run_report.payload["status"], "ready")
        self.assertEqual(run_report.payload["suite"], "broad")
        run_id = run_report.payload["run_id"]

        run_json = workspace.root / run_report.payload["artifacts"]["run_json"]
        stored = read_json(run_json)
        external_case = next(case for case in stored["cases"] if case["case_id"] == "external-grounded")
        abstain_case = next(case for case in stored["cases"] if case["case_id"] == "answer-explicit-abstain")
        self.assertEqual(external_case["execution"]["result"]["support_basis"], "external-source-verified")
        self.assertEqual(external_case["execution"]["result"]["answer_state"], "grounded")
        self.assertEqual(abstain_case["execution"]["result"]["answer_state"], "abstained")

        self.write_operator_request(
            workspace,
            action="freeze-baseline",
            suite="broad",
            target_ids=[run_id],
            run_label="Freeze broad baseline",
        )
        freeze_report = run_workflow("operator-eval", paths=workspace)
        self.assertEqual(freeze_report.payload["status"], "ready")
        baseline = load_evaluation_baseline(workspace.eval_baseline_path("broad"))
        self.assertEqual(baseline["run_id"], run_id)

    def test_operator_eval_promotes_canonical_turn_candidates_and_reviews_regressions(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        self.write_broad_eval_assets(workspace)

        self.write_operator_request(
            workspace,
            action="run-suite",
            suite="broad",
            target_ids=[],
            run_label="Broad run before promotion",
        )
        broad_run = run_workflow("operator-eval", paths=workspace)
        self.assertEqual(broad_run.payload["status"], "ready")

        candidate_id = self.create_candidate_turn(workspace)
        self.write_operator_request(
            workspace,
            action="promote-candidate",
            suite="regression",
            target_ids=[candidate_id],
            run_label="Promote candidate",
        )
        promote_report = run_workflow("operator-eval", paths=workspace)
        self.assertEqual(promote_report.payload["status"], "ready")
        self.assertEqual(promote_report.payload["promoted_candidate_ids"], [candidate_id])
        regression_suite = read_json(workspace.eval_suite_path("regression"))
        self.assertTrue(regression_suite["cases"])
        self.assertEqual(
            regression_suite["cases"][0]["execution_inner_workflow_id"],
            "grounded-composition",
        )

        self.write_operator_request(
            workspace,
            action="run-suite",
            suite="regression",
            target_ids=[],
            run_label="Regression run",
        )
        regression_run = run_workflow("operator-eval", paths=workspace)
        self.assertIn(regression_run.payload["status"], {"ready", "degraded", "action-required"})
        self.assertEqual(regression_run.payload["suite"], "regression")

        self.write_operator_request(
            workspace,
            action="review-regressions",
            suite="regression",
            target_ids=[],
            run_label="Regression review",
        )
        review_report = run_workflow("operator-eval", paths=workspace)
        self.assertEqual(review_report.payload["status"], "ready")
        review_json = workspace.root / review_report.payload["artifacts"]["review_json"]
        review_markdown = workspace.root / review_report.payload["artifacts"]["review_markdown"]
        self.assertTrue(review_json.exists())
        self.assertTrue(review_markdown.exists())
        review_payload = read_json(review_json)
        self.assertGreaterEqual(review_payload["candidate_summary"]["candidate_count"], 1)

    def test_runtime_candidate_generation_aggregates_one_canonical_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        candidate_id = self.create_candidate_turn(workspace)
        candidates_payload = read_json(workspace.benchmark_candidates_path)
        matching = [
            item
            for item in candidates_payload.get("candidates", [])
            if isinstance(item, dict) and item.get("candidate_id") == candidate_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(len(matching[0]["session_ids"]), 1)
        self.assertEqual(len(matching[0]["trace_ids"]), 1)

    def test_promoted_candidate_preserves_support_manifest_path(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        self.publish_seeded_corpus(workspace)
        self.write_broad_eval_assets(workspace)

        candidate_id, manifest_relative = self.create_mixed_support_candidate_turn(workspace)
        candidates_payload = read_json(workspace.benchmark_candidates_path)
        matching = [
            item
            for item in candidates_payload.get("candidates", [])
            if isinstance(item, dict) and item.get("candidate_id") == candidate_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["support_manifest_path"], manifest_relative)

        self.write_operator_request(
            workspace,
            action="promote-candidate",
            suite="regression",
            target_ids=[candidate_id],
            run_label="Promote mixed-support candidate",
        )
        promote_report = run_workflow("operator-eval", paths=workspace)
        self.assertEqual(promote_report.payload["status"], "ready")
        regression_suite = read_json(workspace.eval_suite_path("regression"))
        case = next(
            item
            for item in regression_suite["cases"]
            if item["case_id"] == candidate_id.removeprefix("candidate-")
        )
        self.assertEqual(case["execution_support_manifest_path"], manifest_relative)


if __name__ == "__main__":
    unittest.main()

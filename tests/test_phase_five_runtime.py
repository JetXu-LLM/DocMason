"""Tests for the DocMason Phase 5 private evaluation foundation."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from docmason.cli import build_parser
from docmason.evaluation import (
    EvaluationConfigurationError,
    aggregate_case_rubric,
    compare_against_baseline,
    freeze_baseline_from_run,
    load_evaluation_suite,
    load_judge_trials,
    load_rubric_definition,
    run_evaluation_suite,
    write_feedback_record,
)
from docmason.project import WorkspacePaths, read_json, write_json


class PhaseFiveRuntimeTests(unittest.TestCase):
    """Cover private evaluation, baselines, and feedback behavior."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        (root / "skills" / "canonical" / "workspace-bootstrap").mkdir(parents=True)
        (root / "skills" / "canonical" / "grounded-answer").mkdir(parents=True)
        (root / "skills" / "canonical" / "provenance-trace").mkdir(parents=True)
        (root / "skills" / "canonical" / "retrieval-workflow").mkdir(parents=True)
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "planning").mkdir()
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
        (root / "src" / "docmason" / "retrieval.py").write_text(
            "# retrieval fingerprint fixture\n",
            encoding="utf-8",
        )
        (root / "src" / "docmason" / "evaluation.py").write_text(
            "# evaluation fingerprint fixture\n",
            encoding="utf-8",
        )
        (root / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
        for skill_name in (
            "workspace-bootstrap",
            "grounded-answer",
            "provenance-trace",
            "retrieval-workflow",
        ):
            skill_dir = root / "skills" / "canonical" / skill_name
            (skill_dir / "SKILL.md").write_text(f"# {skill_name}\n", encoding="utf-8")
            (skill_dir / "workflow.json").write_text(
                '{"schema_version": 1, "workflow_id": "' + skill_name + '"}\n',
                encoding="utf-8",
            )
        return WorkspacePaths(root=root)

    def mark_environment_ready(self, workspace: WorkspacePaths) -> None:
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "prepared_at": "2026-03-16T00:00:00Z",
                "package_manager": "uv",
                "python_executable": "/usr/bin/python3",
                "venv_python": ".venv/bin/python",
                "editable_install": True,
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
        related_sources: list[dict[str, object]] | None = None,
    ) -> None:
        source_manifest = read_json(source_dir / "source_manifest.json")
        evidence_manifest = read_json(source_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        knowledge = {
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
                "notes_en": "Phase 5 test fixture.",
                "notes_source": "Phase 5 test fixture.",
            },
            "citations": [{"unit_id": first_unit_id, "support": "summary support"}],
            "related_sources": related_sources or [],
        }
        write_json(source_dir / "knowledge.json", knowledge)
        summary_md = "\n".join(
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
        )
        (source_dir / "summary.md").write_text(summary_md, encoding="utf-8")

    def publish_seeded_corpus(self, workspace: WorkspacePaths) -> list[str]:
        from docmason.commands import sync_workspace

        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = [item["source_id"] for item in pending.payload["pending_sources"]]
        self.assertEqual(len(source_ids), 2)

        source_a = workspace.knowledge_base_staging_dir / "sources" / source_ids[0]
        source_b = workspace.knowledge_base_staging_dir / "sources" / source_ids[1]
        self.build_seeded_knowledge(
            source_a,
            title="Architecture Strategy Deck",
            summary="A strategy deck about architecture and operating model.",
            key_point="The strategy defines an architecture operating model.",
            claim="The architecture deck connects strategy to implementation.",
            related_sources=[
                {
                    "source_id": source_ids[1],
                    "relation_type": "schedule-companion",
                    "strength": "high",
                    "status": "supported",
                    "citation_unit_ids": ["page-001"],
                }
            ],
        )
        self.build_seeded_knowledge(
            source_b,
            title="Delivery Timeline Plan",
            summary="A delivery timeline and companion planning document.",
            key_point="The timeline explains rollout milestones.",
            claim="The timeline complements the architecture strategy.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def write_phase_five_specs(
        self,
        workspace: WorkspacePaths,
        source_ids: list[str],
    ) -> tuple[Path, Path, Path]:
        benchmark_dir = workspace.eval_broad_benchmark_dir
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        rubric = {
            "schema_version": 1,
            "rubric_id": "phase-5-test-rubric",
            "title": "Phase 5 test rubric",
            "trial_count": 3,
            "judge_instructions": ["Score only active dimensions from the case support boundary."],
            "acceptance_thresholds": {
                "deterministic_pass_rate": 1.0,
                "answer_mean_score": 1.5,
                "aggregate_rubric_regression_limit": 0.2,
            },
            "dimensions": {
                "factual_alignment": {
                    "description": "Correctness",
                    "score_0": "Wrong",
                    "score_1": "Mostly right",
                    "score_2": "Correct",
                },
                "coverage": {
                    "description": "Coverage",
                    "score_0": "Misses key facts",
                    "score_1": "Partial",
                    "score_2": "Complete",
                },
                "source_discipline": {
                    "description": "Grounding",
                    "score_0": "Ungrounded",
                    "score_1": "Mixed",
                    "score_2": "Grounded",
                },
                "uncertainty_discipline": {
                    "description": "Uncertainty",
                    "score_0": "Overclaims",
                    "score_1": "Some qualification",
                    "score_2": "Well qualified",
                },
                "visual_evidence_handling": {
                    "description": "Visual handling",
                    "score_0": "Misses render need",
                    "score_1": "Partial",
                    "score_2": "Correct",
                },
            },
        }
        corpus_signature = read_json(workspace.retrieval_manifest_path("current"))[
            "source_signature"
        ]
        suite = {
            "schema_version": 1,
            "suite_id": "phase-5-test-suite",
            "title": "Phase 5 test suite",
            "description": "A focused temporary suite for Phase 5 tests.",
            "target": "current",
            "corpus_signature": corpus_signature,
            "retrieval_strategy_id": "phase4b-lexical-plus-graph-v1",
            "answer_workflow_id": "phase4b-grounded-answer-v1",
            "cases": [
                {
                    "case_id": "retrieve-architecture",
                    "family": "retrieval",
                    "execution_mode": "retrieve",
                    "query_or_prompt": "architecture strategy",
                    "expected_primary_sources": [source_ids[0]],
                    "required_sources_or_units": [source_ids[0]],
                    "forbidden_sources_or_units": [],
                    "minimum_support_overlap": 1,
                    "expected_status": "ready",
                    "expected_answer_state": None,
                    "expected_render_inspection_required": None,
                    "reference_facts": ["Architecture source should rank first."],
                    "active_rubric_dimensions": [],
                    "feedback_tags": ["retrieval_miss"],
                },
                {
                    "case_id": "answer-grounded",
                    "family": "answer",
                    "execution_mode": "trace-answer",
                    "query_or_prompt": (
                        "The architecture strategy connects the operating model to implementation."
                    ),
                    "expected_primary_sources": [source_ids[0]],
                    "required_sources_or_units": [source_ids[0]],
                    "forbidden_sources_or_units": [],
                    "minimum_support_overlap": 1,
                    "expected_status": "ready",
                    "expected_answer_state": "grounded",
                    "expected_render_inspection_required": True,
                    "reference_facts": [
                        "The answer should ground to the seeded architecture source."
                    ],
                    "active_rubric_dimensions": [
                        "factual_alignment",
                        "coverage",
                        "source_discipline",
                    ],
                    "feedback_tags": ["unsupported_synthesis"],
                },
                {
                    "case_id": "answer-false-grounding",
                    "family": "answer-negative",
                    "execution_mode": "trace-answer",
                    "query_or_prompt": (
                        "The architecture strategy deck says DocMason already ships "
                        "watch mode and requires a database service."
                    ),
                    "expected_primary_sources": [],
                    "required_sources_or_units": [],
                    "forbidden_sources_or_units": [],
                    "minimum_support_overlap": 0,
                    "expected_status": "degraded",
                    "expected_answer_state": "unresolved",
                    "expected_render_inspection_required": True,
                    "reference_facts": ["The false claim must not trace as grounded."],
                    "active_rubric_dimensions": [],
                    "feedback_tags": ["unsupported_synthesis", "should_abstain"],
                    "critical": True,
                },
                {
                    "case_id": "answer-render-required",
                    "family": "answer-render",
                    "execution_mode": "trace-answer",
                    "query_or_prompt": "\n\n".join(
                        [
                            "The architecture strategy connects the operating model "
                            "to implementation.",
                            "Zyzzyva quasar nebulae orthonormal frabjous snark.",
                        ]
                    ),
                    "expected_primary_sources": [source_ids[0]],
                    "required_sources_or_units": [source_ids[0]],
                    "forbidden_sources_or_units": [],
                    "minimum_support_overlap": 1,
                    "expected_status": "degraded",
                    "expected_answer_state": "partially-grounded",
                    "expected_render_inspection_required": True,
                    "reference_facts": ["The mixed answer should preserve render escalation."],
                    "active_rubric_dimensions": [],
                    "feedback_tags": ["render_required"],
                    "critical": True,
                },
            ],
        }
        judge_trials = {
            "schema_version": 1,
            "suite_id": "phase-5-test-suite",
            "judge_profile": {
                "mode": "agent-judge",
                "agent_name": "codex",
                "model_name": "gpt-5",
                "workflow_id": "phase-5-test-judge",
                "trial_count": 3,
            },
            "trials_by_case": {
                "answer-grounded": [
                    {
                        "trial_id": "trial-1",
                        "dimension_scores": {
                            "factual_alignment": 2,
                            "coverage": 2,
                            "source_discipline": 2,
                        },
                        "notes": "The answer is short but fully grounded in the seeded source.",
                        "feedback_tags": [],
                    },
                    {
                        "trial_id": "trial-2",
                        "dimension_scores": {
                            "factual_alignment": 2,
                            "coverage": 2,
                            "source_discipline": 2,
                        },
                        "notes": "No material overclaim appears in the answer.",
                        "feedback_tags": [],
                    },
                    {
                        "trial_id": "trial-3",
                        "dimension_scores": {
                            "factual_alignment": 2,
                            "coverage": 2,
                            "source_discipline": 2,
                        },
                        "notes": "The seeded answer remains well aligned with the evidence bundle.",
                        "feedback_tags": [],
                    },
                ]
            },
        }
        rubric_path = benchmark_dir / "rubric.json"
        suite_path = benchmark_dir / "suite.json"
        judge_path = benchmark_dir / "judge-trials.json"
        write_json(rubric_path, rubric)
        write_json(suite_path, suite)
        write_json(judge_path, judge_trials)
        return suite_path, rubric_path, judge_path

    def test_suite_validation_rejects_unknown_feedback_tag(self) -> None:
        workspace = self.make_workspace()
        benchmark_dir = workspace.eval_broad_benchmark_dir
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        rubric_path = benchmark_dir / "rubric.json"
        suite_path = benchmark_dir / "suite.json"
        write_json(
            rubric_path,
            {
                "schema_version": 1,
                "rubric_id": "r1",
                "title": "Rubric",
                "trial_count": 3,
                "judge_instructions": ["score"],
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
            suite_path,
            {
                "schema_version": 1,
                "suite_id": "s1",
                "title": "Suite",
                "description": "desc",
                "target": "current",
                "corpus_signature": "abc",
                "retrieval_strategy_id": "phase4b-lexical-plus-graph-v1",
                "answer_workflow_id": "phase4b-grounded-answer-v1",
                "cases": [
                    {
                        "case_id": "bad-case",
                        "family": "answer",
                        "execution_mode": "trace-answer",
                        "query_or_prompt": "answer",
                        "expected_primary_sources": [],
                        "required_sources_or_units": [],
                        "forbidden_sources_or_units": [],
                        "expected_status": "ready",
                        "expected_answer_state": "grounded",
                        "expected_render_inspection_required": False,
                        "reference_facts": ["fact"],
                        "active_rubric_dimensions": ["factual_alignment"],
                        "feedback_tags": ["not-valid"],
                    }
                ],
            },
        )
        rubric = load_rubric_definition(rubric_path)
        with self.assertRaises(EvaluationConfigurationError):
            load_evaluation_suite(suite_path, rubric=rubric)

    def test_aggregate_case_rubric_uses_median_and_flags_large_spread(self) -> None:
        case = {
            "case_id": "answer-case",
            "active_rubric_dimensions": ["coverage", "factual_alignment"],
        }
        rubric = {"trial_count": 3}
        judge_trials = {
            "answer-case": [
                {
                    "dimension_scores": {"coverage": 0, "factual_alignment": 2},
                    "notes": "t1",
                    "feedback_tags": [],
                },
                {
                    "dimension_scores": {"coverage": 2, "factual_alignment": 2},
                    "notes": "t2",
                    "feedback_tags": [],
                },
                {
                    "dimension_scores": {"coverage": 2, "factual_alignment": 1},
                    "notes": "t3",
                    "feedback_tags": [],
                },
            ]
        }
        result = aggregate_case_rubric(case, rubric=rubric, judge_trials=judge_trials)
        assert result is not None
        self.assertEqual(result["dimension_scores"]["coverage"], 2)
        self.assertEqual(result["dimension_scores"]["factual_alignment"], 2)
        self.assertTrue(result["review_recommended"])

    def test_run_suite_writes_artifacts_and_captures_version_context(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_phase_five_specs(workspace, source_ids)

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            run_label="Phase 5 test run",
        )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        self.assertEqual(
            run_payload["version_context"]["retrieval_strategy_id"],
            "phase4b-lexical-plus-graph-v1",
        )
        self.assertEqual(
            run_payload["version_context"]["answer_workflow_id"],
            "phase4b-grounded-answer-v1",
        )
        self.assertEqual(
            run_payload["version_context"]["judge_profile"]["agent_name"],
            "codex",
        )
        run_json = workspace.root / run_payload["artifacts"]["run_json"]
        scorecard = workspace.root / run_payload["artifacts"]["scorecard_markdown"]
        self.assertTrue(run_json.exists())
        self.assertTrue(scorecard.exists())
        scorecard_text = scorecard.read_text(encoding="utf-8")
        self.assertIn("## Failures", scorecard_text)
        self.assertIn("Artifacts", scorecard_text)
        stored = read_json(run_json)
        self.assertEqual(stored["summary"]["overall_status"], "passed")
        answer_case = next(case for case in stored["cases"] if case["case_id"] == "answer-grounded")
        retrieval_case = next(
            case for case in stored["cases"] if case["case_id"] == "retrieve-architecture"
        )
        self.assertEqual(
            answer_case["execution"]["result"]["answer_workflow_id"],
            "phase4b-grounded-answer-v1",
        )
        self.assertEqual(answer_case["execution"]["result"]["answer_state"], "grounded")
        retrieval_session = read_json(
            workspace.query_sessions_dir / f"{retrieval_case['execution']['session_id']}.json"
        )
        self.assertEqual(retrieval_session["log_origin"], "evaluation-suite")

    def test_freeze_baseline_and_compare_detects_regression(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_phase_five_specs(workspace, source_ids)

        first_run = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
        )
        baseline_path = workspace.eval_baseline_path("broad")
        baseline = freeze_baseline_from_run(first_run, baseline_path=baseline_path)
        self.assertEqual(baseline["suite_id"], "phase-5-test-suite")

        stable_run = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            baseline_path=baseline_path,
        )
        self.assertEqual(stable_run["baseline_comparison"]["status"], "passed")
        self.assertEqual(stable_run["summary"]["overall_status"], "passed")

        regressed_run = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            baseline_path=baseline_path,
            answer_overrides={
                "answer-grounded": (
                    "The architecture strategy deck says DocMason already ships watch mode "
                    "and requires a database service."
                )
            },
        )
        self.assertEqual(regressed_run["summary"]["overall_status"], "failed")

    def test_write_feedback_record_persists_validated_payload(self) -> None:
        workspace = self.make_workspace()
        record = write_feedback_record(
            workspace,
            {
                "case_id": "answer-grounded",
                "run_id": "run-1",
                "session_id": "session-1",
                "trace_id": "trace-1",
                "feedback_tags": ["coverage_gap", "unsupported_synthesis"],
                "corrected_text": "A corrected answer.",
                "notes": "Operator correction.",
            },
        )
        self.assertTrue((workspace.root / record["path"]).exists())
        stored = read_json(workspace.root / record["path"])
        self.assertEqual(stored["case_id"], "answer-grounded")
        self.assertEqual(stored["feedback_tags"], ["coverage_gap", "unsupported_synthesis"])

    def test_noncritical_baseline_regression_marks_run_degraded(self) -> None:
        rubric = {
            "acceptance_thresholds": {"aggregate_rubric_regression_limit": 0.2},
        }
        run_payload = {
            "suite_id": "suite-1",
            "version_context": {"corpus_signature": "sig-1"},
            "summary": {"answer_mean_score": 1.9},
            "cases": [
                {
                    "case_id": "case-1",
                    "critical": False,
                    "deterministic_passed": True,
                    "rubric": {"mean_score": 1.7},
                }
            ],
        }
        baseline_payload = {
            "suite_id": "suite-1",
            "version_context": {"corpus_signature": "sig-1"},
            "summary": {"answer_mean_score": 1.95},
            "cases": [
                {
                    "case_id": "case-1",
                    "deterministic_passed": True,
                    "rubric_mean_score": 1.9,
                }
            ],
        }
        comparison = compare_against_baseline(run_payload, baseline_payload, rubric=rubric)
        self.assertEqual(comparison["status"], "degraded")

    def test_run_suite_resolves_private_paths_relative_to_workspace_root(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        _suite_path, _rubric_path, _judge_path = self.write_phase_five_specs(workspace, source_ids)

        original_cwd = Path.cwd()
        os.chdir(workspace.source_dir)
        self.addCleanup(os.chdir, original_cwd)

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=Path("runtime/eval/benchmarks/broad/suite.json"),
            rubric_path=Path("runtime/eval/benchmarks/broad/rubric.json"),
            judge_trials_path=Path("runtime/eval/benchmarks/broad/judge-trials.json"),
        )
        self.assertEqual(run_payload["summary"]["overall_status"], "passed")

    def test_private_evaluation_does_not_expand_public_cli(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["eval"])

    def test_load_judge_trials_rejects_missing_scored_case(self) -> None:
        workspace = self.make_workspace()
        benchmark_dir = workspace.eval_broad_benchmark_dir
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        rubric_path = benchmark_dir / "rubric.json"
        suite_path = benchmark_dir / "suite.json"
        judge_path = benchmark_dir / "judge-trials.json"
        write_json(
            rubric_path,
            {
                "schema_version": 1,
                "rubric_id": "r1",
                "title": "Rubric",
                "trial_count": 3,
                "judge_instructions": ["score"],
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
            suite_path,
            {
                "schema_version": 1,
                "suite_id": "s1",
                "title": "Suite",
                "description": "desc",
                "target": "current",
                "corpus_signature": "abc",
                "retrieval_strategy_id": "phase4b-lexical-plus-graph-v1",
                "answer_workflow_id": "phase4b-grounded-answer-v1",
                "cases": [
                    {
                        "case_id": "scored-case",
                        "family": "answer",
                        "execution_mode": "trace-answer",
                        "query_or_prompt": "answer",
                        "expected_primary_sources": [],
                        "required_sources_or_units": [],
                        "forbidden_sources_or_units": [],
                        "expected_status": "ready",
                        "expected_answer_state": "grounded",
                        "expected_render_inspection_required": False,
                        "reference_facts": ["fact"],
                        "active_rubric_dimensions": ["factual_alignment"],
                        "feedback_tags": ["coverage_gap"],
                    }
                ],
            },
        )
        write_json(
            judge_path,
            {
                "schema_version": 1,
                "suite_id": "s1",
                "judge_profile": {
                    "mode": "agent-judge",
                    "agent_name": "codex",
                    "model_name": "gpt-5",
                    "workflow_id": "judge",
                    "trial_count": 3,
                },
                "trials_by_case": {},
            },
        )
        rubric = load_rubric_definition(rubric_path)
        suite = load_evaluation_suite(suite_path, rubric=rubric)
        with self.assertRaises(EvaluationConfigurationError):
            load_judge_trials(judge_path, suite=suite, rubric=rubric)


if __name__ == "__main__":
    unittest.main()

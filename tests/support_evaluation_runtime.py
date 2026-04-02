# ruff: noqa: E501
"""Tests for the DocMason private evaluation runtime foundation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import docmason.commands as commands_module
import docmason.retrieval as retrieval_module
from docmason.ask import complete_ask_turn, prepare_ask_turn, settle_lane_c_shared_refresh
from docmason.cli import build_parser
from docmason.commands import CommandReport, sync_workspace
from docmason.control_plane import (
    complete_shared_job,
    ensure_shared_job,
    shared_job_control_plane_payload,
)
from docmason.conversation import load_turn_record, update_conversation_turn
from docmason.evaluation import (
    EvaluationConfigurationError,
    _case_deterministic_checks,
    aggregate_case_rubric,
    compare_against_baseline,
    freeze_baseline_from_run,
    load_evaluation_suite,
    load_judge_trials,
    load_rubric_definition,
    run_evaluation_suite,
    write_feedback_record,
)
from docmason.project import WorkspacePaths, read_json, source_inventory_signature, write_json
from docmason.review import refresh_log_review_summary
from docmason.run_control import load_run_state
from tests.support_ready_workspace import seed_self_contained_bootstrap_state


class EvaluationRuntimeTests(unittest.TestCase):
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
        seed_self_contained_bootstrap_state(
            workspace,
            prepared_at="2026-03-16T00:00:00Z",
        )

    def semantic_analysis(
        self,
        *,
        question_class: str,
        question_domain: str,
        route_reason: str | None = None,
        needs_latest_workspace_state: bool = False,
        evidence_requirements: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "question_class": question_class,
            "question_domain": question_domain,
            "route_reason": route_reason
            or f"Evaluation test classified the question as {question_class}/{question_domain}.",
            "needs_latest_workspace_state": needs_latest_workspace_state,
        }
        if evidence_requirements is not None:
            payload["evidence_requirements"] = evidence_requirements
        return payload

    def create_pdf(self, path: Path, *, page_count: int = 1) -> None:
        from pypdf import PdfWriter

        writer = PdfWriter()
        for index in range(page_count):
            writer.add_blank_page(width=144 + index, height=144 + index)
        with path.open("wb") as handle:
            writer.write(handle)

    def create_pdf_with_full_page_image(self, path: Path) -> None:
        pymupdf_module: Any
        try:
            import pymupdf

            pymupdf_module = pymupdf
        except ImportError:  # pragma: no cover - compatibility import
            import fitz  # type: ignore[import-untyped]

            pymupdf_module = fitz
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tempdir_name:
            image_path = Path(tempdir_name) / "page.png"
            image = Image.new("RGB", (1200, 1600), color=(245, 245, 245))
            draw = ImageDraw.Draw(image)
            draw.rectangle((80, 120, 1120, 1480), outline=(32, 64, 128), width=14)
            draw.rectangle((170, 300, 1030, 540), fill=(210, 225, 245))
            draw.rectangle((170, 660, 1030, 1180), fill=(225, 235, 250))
            draw.rectangle((170, 1230, 760, 1380), fill=(235, 240, 250))
            image.save(image_path)

            document = pymupdf_module.open()
            page = document.new_page(width=595, height=842)
            page.insert_image(page.rect, filename=str(image_path))
            document.save(path)
            document.close()

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
                "notes_en": "Evaluation test fixture.",
                "notes_source": "Evaluation test fixture.",
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
        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        source_ids = [item["source_id"] for item in pending.payload["pending_sources"]]
        self.assertEqual(len(source_ids), 2)

        source_a = workspace.knowledge_base_staging_dir / "sources" / source_ids[0]
        source_b = workspace.knowledge_base_staging_dir / "sources" / source_ids[1]
        self.build_seeded_knowledge(
            source_a,
            title="Project Planning Brief",
            summary="A planning brief about a project outline and work plan.",
            key_point="The outline defines a practical work plan.",
            claim="The project outline connects planning to implementation.",
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
            title="Project Timeline Notes",
            summary="A timeline note and companion planning document.",
            key_point="The timeline explains key milestones.",
            claim="The timeline complements the project outline.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def publish_seeded_scanned_corpus(self, workspace: WorkspacePaths) -> dict[str, str]:
        pending = sync_workspace(workspace, autonomous=False)
        self.assertEqual(pending.payload["sync_status"], "pending-synthesis")
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        scan_source_id = by_path["original_doc/scan.pdf"]
        control_source_id = by_path["original_doc/control.pdf"]
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / scan_source_id,
            title="Scanned Workflow Page",
            summary="A scanned workflow page with a workflow-style layout and limited extracted text.",
            key_point="The rendered page shows a workflow-style process layout.",
            claim=(
                "The page appears to show a workflow-style process layout and needs multimodal "
                "follow-up for reliable semantic detail."
            ),
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / control_source_id,
            title="Control Page",
            summary="A control page with no scan-specific signals.",
            key_point="The control page is intentionally generic.",
            claim="The control page should rank lower for scanned-page queries.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return {"scan": scan_source_id, "control": control_source_id}

    def write_phase_five_specs(
        self,
        workspace: WorkspacePaths,
        source_ids: list[str],
    ) -> tuple[Path, Path, Path]:
        benchmark_dir = workspace.eval_broad_benchmark_dir
        benchmark_dir.mkdir(parents=True, exist_ok=True)
        rubric = {
            "schema_version": 1,
            "rubric_id": "evaluation-test-rubric",
            "title": "Evaluation test rubric",
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
            "suite_id": "evaluation-test-suite",
            "title": "Evaluation test suite",
            "description": "A focused temporary suite for evaluation-runtime tests.",
            "target": "current",
            "corpus_signature": corpus_signature,
            "retrieval_strategy_id": "phase4b-lexical-plus-graph-v1",
            "answer_workflow_id": "phase4b-grounded-answer-v1",
            "cases": [
                {
                    "case_id": "retrieve-architecture",
                    "family": "retrieval",
                    "execution_mode": "retrieve",
                    "query_or_prompt": "project outline",
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
                        "The project outline connects the work plan to implementation."
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
                        "The project planning brief says DocMason already ships "
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
                            "The project outline connects the work plan "
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
            "suite_id": "evaluation-test-suite",
            "judge_profile": {
                "mode": "agent-judge",
                "agent_name": "codex",
                "model_name": "gpt-5",
                "workflow_id": "evaluation-test-judge",
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

    def write_ask_turn_specs(
        self,
        workspace: WorkspacePaths,
        *,
        source_ids: list[str],
        cases: list[dict[str, object]],
        title: str = "Ask-turn evaluation suite",
        description: str = "A focused suite for canonical ask replay tests.",
    ) -> tuple[Path, Path, Path]:
        suite_path, rubric_path, judge_path = self.write_phase_five_specs(workspace, source_ids)
        corpus_signature = read_json(workspace.retrieval_manifest_path("current"))["source_signature"]
        write_json(
            suite_path,
            {
                "schema_version": 1,
                "suite_id": "ask-turn-test-suite",
                "title": title,
                "description": description,
                "target": "current",
                "corpus_signature": corpus_signature,
                "retrieval_strategy_id": "phase4b-lexical-plus-graph-v1",
                "answer_workflow_id": "phase4b-grounded-answer-v1",
                "cases": cases,
            },
        )
        write_json(
            judge_path,
            {
                "schema_version": 1,
                "suite_id": "ask-turn-test-suite",
                "judge_profile": {
                    "mode": "agent-judge",
                    "agent_name": "codex",
                    "model_name": "gpt-5",
                    "workflow_id": "evaluation-test-judge",
                    "trial_count": 3,
                },
                "trials_by_case": {},
            },
        )
        return suite_path, rubric_path, judge_path

    def ask_turn_case(
        self,
        *,
        case_id: str,
        family: str,
        question: str,
        semantic_analysis: dict[str, object],
        expected_status: str,
        expected_answer_state: str | None,
        expected_support_basis: str | None,
        reference_facts: list[str],
        continuations: list[dict[str, object]] | None = None,
        answer_plan: dict[str, object] | None = None,
        hybrid_refresh: dict[str, object] | None = None,
        expectations: dict[str, object] | None = None,
        expected_primary_sources: list[str] | None = None,
        required_sources_or_units: list[str] | None = None,
        minimum_support_overlap: int = 0,
        feedback_tags: list[str] | None = None,
        trace_via_public_command: bool = False,
    ) -> dict[str, object]:
        return {
            "case_id": case_id,
            "family": family,
            "execution_mode": "ask-turn",
            "query_or_prompt": question,
            "expected_primary_sources": expected_primary_sources or [],
            "required_sources_or_units": required_sources_or_units or [],
            "minimum_support_overlap": minimum_support_overlap,
            "forbidden_sources_or_units": [],
            "expected_status": expected_status,
            "expected_answer_state": expected_answer_state,
            "expected_support_basis": expected_support_basis,
            "expected_render_inspection_required": None,
            "reference_facts": reference_facts,
            "active_rubric_dimensions": [],
            "feedback_tags": feedback_tags or ["coverage_gap"],
            "ask_replay": {
                "replay_source": {"kind": "manual-suite"},
                "semantic_analysis": semantic_analysis,
                "continuations": continuations or [],
                **(
                    {"trace_via_public_command": True}
                    if trace_via_public_command
                    else {}
                ),
                **({"answer_plan": answer_plan} if answer_plan is not None else {}),
                **({"hybrid_refresh": hybrid_refresh} if hybrid_refresh is not None else {}),
                **({"expectations": expectations} if expectations is not None else {}),
            },
        }

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

    def test_load_evaluation_suite_accepts_manual_ask_turn_case(self) -> None:
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
                        "case_id": "ask-turn-case",
                        "family": "ask-turn",
                        "execution_mode": "ask-turn",
                        "query_or_prompt": "What does the strategy connect to?",
                        "expected_primary_sources": [],
                        "required_sources_or_units": [],
                        "minimum_support_overlap": 0,
                        "forbidden_sources_or_units": [],
                        "expected_status": "ready",
                        "expected_answer_state": "grounded",
                        "expected_support_basis": "kb-grounded",
                        "expected_render_inspection_required": None,
                        "reference_facts": ["The strategy answer should be replayable."],
                        "active_rubric_dimensions": [],
                        "feedback_tags": ["coverage_gap"],
                        "ask_replay": {
                            "replay_source": {"kind": "manual-suite"},
                            "semantic_analysis": self.semantic_analysis(
                                question_class="answer",
                                question_domain="workspace-corpus",
                            ),
                            "host_thread_ref": "example-thread",
                            "continuations": [{"message": "What does the strategy connect to?"}],
                            "answer_plan": {
                                "answer_text": "The outline connects the work plan to implementation.",
                                "trace_top": 2,
                                "completion_overrides": {"status": "answered"},
                            },
                            "expectations": {
                                "reused_turn": True,
                                "query_session_count": 1,
                                "trace_count": 1,
                            },
                        },
                    }
                ],
            },
        )
        rubric = load_rubric_definition(rubric_path)
        suite = load_evaluation_suite(suite_path, rubric=rubric)
        case = suite["cases"][0]
        self.assertEqual(case["execution_mode"], "ask-turn")
        self.assertEqual(case["ask_replay"]["replay_source"]["kind"], "manual-suite")
        self.assertEqual(case["ask_replay"]["host_thread_ref"], "example-thread")
        self.assertEqual(
            case["ask_replay"]["continuations"][0]["message"],
            "What does the strategy connect to?",
        )
        self.assertEqual(case["ask_replay"]["answer_plan"]["trace_top"], 2)

    def test_load_evaluation_suite_rejects_invalid_ask_turn_contracts(self) -> None:
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
        invalid_cases = {
            "missing-ask-replay": {
                "case_id": "bad-case",
                "family": "ask-turn",
                "execution_mode": "ask-turn",
                "query_or_prompt": "What does the strategy connect to?",
                "expected_primary_sources": [],
                "required_sources_or_units": [],
                "minimum_support_overlap": 0,
                "forbidden_sources_or_units": [],
                "expected_status": "ready",
                "expected_answer_state": "grounded",
                "expected_support_basis": "kb-grounded",
                "expected_render_inspection_required": None,
                "reference_facts": ["fact"],
                "active_rubric_dimensions": [],
                "feedback_tags": ["coverage_gap"],
            },
            "invalid-replay-source": {
                "case_id": "bad-case",
                "family": "ask-turn",
                "execution_mode": "ask-turn",
                "query_or_prompt": "What does the strategy connect to?",
                "expected_primary_sources": [],
                "required_sources_or_units": [],
                "minimum_support_overlap": 0,
                "forbidden_sources_or_units": [],
                "expected_status": "ready",
                "expected_answer_state": "grounded",
                "expected_support_basis": "kb-grounded",
                "expected_render_inspection_required": None,
                "reference_facts": ["fact"],
                "active_rubric_dimensions": [],
                "feedback_tags": ["coverage_gap"],
                "ask_replay": {
                    "replay_source": {"kind": "candidate-driven"},
                    "semantic_analysis": self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                },
            },
            "invalid-hybrid-refresh": {
                "case_id": "bad-case",
                "family": "ask-turn",
                "execution_mode": "ask-turn",
                "query_or_prompt": "What does the strategy connect to?",
                "expected_primary_sources": [],
                "required_sources_or_units": [],
                "minimum_support_overlap": 0,
                "forbidden_sources_or_units": [],
                "expected_status": "ready",
                "expected_answer_state": "grounded",
                "expected_support_basis": "kb-grounded",
                "expected_render_inspection_required": None,
                "reference_facts": ["fact"],
                "active_rubric_dimensions": [],
                "feedback_tags": ["coverage_gap"],
                "ask_replay": {
                    "replay_source": {"kind": "manual-suite"},
                    "semantic_analysis": self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    "hybrid_refresh": {"completion_status": "unsupported"},
                },
            },
            "invalid-expectations": {
                "case_id": "bad-case",
                "family": "ask-turn",
                "execution_mode": "ask-turn",
                "query_or_prompt": "What does the strategy connect to?",
                "expected_primary_sources": [],
                "required_sources_or_units": [],
                "minimum_support_overlap": 0,
                "forbidden_sources_or_units": [],
                "expected_status": "ready",
                "expected_answer_state": "grounded",
                "expected_support_basis": "kb-grounded",
                "expected_render_inspection_required": None,
                "reference_facts": ["fact"],
                "active_rubric_dimensions": [],
                "feedback_tags": ["coverage_gap"],
                "ask_replay": {
                    "replay_source": {"kind": "manual-suite"},
                    "semantic_analysis": self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    "expectations": {"unknown_check": True},
                },
            },
        }
        rubric = load_rubric_definition(rubric_path)
        for name, invalid_case in invalid_cases.items():
            with self.subTest(name=name):
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
                        "cases": [invalid_case],
                    },
                )
                with self.assertRaises(EvaluationConfigurationError):
                    load_evaluation_suite(suite_path, rubric=rubric)

    def test_run_suite_replays_ready_and_reused_ask_turn_cases(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-ready-grounded-answer",
                    family="ask-ready",
                    question="What does the project planning brief say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=["The ask replay should ground to the planning brief source."],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "reused_turn": False,
                        "query_session_count": 1,
                        "trace_count": 1,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "ask-prepared",
                            "trace-completed",
                            "admissibility-passed",
                            "projection-enqueued",
                        ],
                    },
                ),
                self.ask_turn_case(
                    case_id="ask-reuse-same-question",
                    family="ask-reuse",
                    question="What does the project planning brief say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=["The replay continuation should reuse the same open ask turn."],
                    continuations=[
                        {
                            "message": (
                                "What does the project planning brief say about the project "
                                "work plan?"
                            )
                        }
                    ],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "reused_turn": True,
                        "query_session_count": 1,
                        "trace_count": 1,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "ask-prepared",
                            "trace-completed",
                            "admissibility-passed",
                            "projection-enqueued",
                        ],
                    },
                ),
            ],
        )

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            run_label="Ask ready and reuse",
        )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        cases = {case["case_id"]: case for case in run_payload["cases"]}
        ready_case = cases["ask-ready-grounded-answer"]
        reuse_case = cases["ask-reuse-same-question"]
        self.assertFalse(ready_case["execution"]["reused_turn"])
        self.assertTrue(reuse_case["execution"]["reused_turn"])
        self.assertEqual(
            ready_case["execution"]["result"]["front_door_state"],
            "canonical-ask",
        )
        self.assertIn("canonical_conversation", ready_case["artifact_paths"])
        self.assertIn("answer_file", ready_case["artifact_paths"])
        self.assertIn("query_session_01", ready_case["artifact_paths"])
        self.assertIn("retrieval_trace_01", ready_case["artifact_paths"])
        self.assertIn("review_summary", ready_case["artifact_paths"])
        self.assertIn("benchmark_candidates", ready_case["artifact_paths"])
        self.assertIn("answer_history_index", ready_case["artifact_paths"])
        self.assertIn("projection_state", ready_case["artifact_paths"])

    def test_run_suite_can_trace_ask_turn_via_public_trace_command(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-public-trace-path",
                    family="ask-ready",
                    question="What does the project planning brief say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=["The replay should be able to close via the public trace command path."],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "query_session_count": 1,
                        "trace_count": 1,
                    },
                    trace_via_public_command=True,
                )
            ],
        )
        real_trace_knowledge: Any = commands_module.trace_knowledge
        trace_calls = 0

        def patched_trace_knowledge(*args: object, **kwargs: object) -> CommandReport:
            nonlocal trace_calls
            trace_calls += 1
            return real_trace_knowledge(*args, **kwargs)

        with mock.patch(
            "docmason.commands.trace_knowledge",
            side_effect=patched_trace_knowledge,
        ):
            run_payload = run_evaluation_suite(
                workspace,
                suite_path=suite_path,
                rubric_path=rubric_path,
                judge_trials_path=judge_path,
                run_label="Ask public trace path",
            )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        self.assertEqual(trace_calls, 1)
        case = run_payload["cases"][0]
        query_session = read_json(
            workspace.query_sessions_dir / f"{case['execution']['session_id']}.json"
        )
        retrieval_trace = read_json(
            workspace.retrieval_traces_dir / f"{case['execution']['trace_id']}.json"
        )
        self.assertEqual(query_session["log_origin"], "evaluation-suite")
        self.assertEqual(retrieval_trace["log_origin"], "evaluation-suite")

    def test_run_suite_marks_ask_turn_replay_as_synthetic_runtime(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-ready-synthetic-origin",
                    family="ask-ready",
                    question="What does the project planning brief say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=["The replay should remain synthetic even when it commits."],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                )
            ],
        )

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            run_label="Ask synthetic isolation",
        )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        conversation = read_json(
            workspace.conversations_dir
            / f"{case['execution']['result']['conversation_id']}.json"
        )
        turn = conversation["turns"][0]
        self.assertEqual(turn["log_origin"], "evaluation-suite")
        run_state = read_json(
            workspace.runs_dir / f"{case['execution']['result']['run_id']}" / "state.json"
        )
        self.assertEqual(run_state["log_origin"], "evaluation-suite")
        query_session = read_json(
            workspace.query_sessions_dir / f"{case['execution']['session_id']}.json"
        )
        retrieval_trace = read_json(
            workspace.retrieval_traces_dir / f"{case['execution']['trace_id']}.json"
        )
        self.assertEqual(query_session["log_origin"], "evaluation-suite")
        self.assertEqual(retrieval_trace["log_origin"], "evaluation-suite")
        summary = refresh_log_review_summary(workspace)
        self.assertEqual(summary["query_sessions"]["real_total"], 0)
        self.assertEqual(summary["query_sessions"]["synthetic_total"], 1)
        self.assertEqual(summary["retrieval_traces"]["real_total"], 0)
        self.assertEqual(summary["retrieval_traces"]["synthetic_total"], 1)
        self.assertEqual(summary["committed_turns"]["total"], 0)
        answer_history = read_json(workspace.answer_history_index_path)
        self.assertEqual(answer_history["record_count"], 0)

    def test_run_suite_requires_ordered_run_events_for_ask_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-run-event-order",
                    family="ask-ready",
                    question="What does the project planning brief say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    reference_facts=["The deterministic net should reject out-of-order events."],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "required_run_events": [
                            "ask-prepared",
                            "preanswer-governance-started",
                        ]
                    },
                )
            ],
        )

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            run_label="Ask run event ordering",
        )

        self.assertEqual(run_payload["summary"]["overall_status"], "failed")
        required_events_check = next(
            check
            for check in run_payload["cases"][0]["deterministic_checks"]
            if check["name"] == "required_run_events"
        )
        self.assertFalse(required_events_check["passed"])
        self.assertEqual(required_events_check["actual"], ["ask-prepared"])

    def test_ask_turn_structural_checks_fail_when_waiting_job_never_settles(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-wait-closure",
                    family="ask-ready",
                    question="What does the project planning brief say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    reference_facts=["Dirty runtime truth should fail structural closure checks."],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                )
            ],
        )

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            run_label="Ask structural closure",
        )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        suite = load_evaluation_suite(
            suite_path,
            rubric=load_rubric_definition(rubric_path),
        )
        case_definition = suite["cases"][0]
        execution = run_payload["cases"][0]["execution"]
        run_id = execution["run_id"]
        journal_path = workspace.runs_dir / run_id / "journal.jsonl"
        journal_entries = [
            json.loads(line)
            for line in journal_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        commit_index = next(
            index
            for index, payload in enumerate(journal_entries)
            if payload.get("event_type") == "turn-committed"
        )
        commit_entry = journal_entries.pop(commit_index)
        self.assertEqual(commit_entry["event_type"], "turn-committed")
        journal_entries.append(
            {
                "recorded_at": "2026-03-28T00:00:00Z",
                "run_id": run_id,
                "stage": "control-plane",
                "event_type": "shared-job-waiting",
                "payload": {
                    "job_id": "job-missing-settlement",
                    "state": "waiting-shared-job",
                },
            }
        )
        journal_entries.append(commit_entry)
        journal_path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in journal_entries) + "\n",
            encoding="utf-8",
        )

        checks = _case_deterministic_checks(workspace, case_definition, execution)
        closure_check = next(
            check for check in checks if check["name"] == "shared_job_wait_closure"
        )
        self.assertFalse(closure_check["passed"])
        self.assertEqual(closure_check["actual"], ["job-missing-settlement"])

    def test_complete_ask_turn_rejects_orphan_lane_c_job_from_run_state(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")

        pending = sync_workspace(workspace, autonomous=False)
        pending_sources = [
            item for item in pending.payload["pending_sources"] if isinstance(item, dict)
        ]
        by_path = {str(item["current_path"]): str(item["source_id"]) for item in pending_sources}
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/scan.pdf"],
            title="Scanned Workflow Page",
            summary="A scanned workflow page with limited extracted text.",
            key_point="The published baseline preserves the rendered page but not enough semantic detail.",
            claim="This page requires multimodal follow-up before confident semantic use.",
        )
        self.build_seeded_knowledge(
            workspace.knowledge_base_staging_dir / "sources" / by_path["original_doc/control.pdf"],
            title="Control Page",
            summary="A control page with no scan-specific signals.",
            key_point="The control page is intentionally generic.",
            claim="The control page should rank lower for scanned-page queries.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-lane-c-orphan"}, clear=False):
            turn = prepare_ask_turn(
                workspace,
                question="What is shown on the scanned workflow page image?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    evidence_requirements={
                        "preferred_channels": ["render", "structure"],
                        "inspection_scope": "unit",
                        "prefer_published_artifacts": True,
                    },
                ),
            )

        answer_path = workspace.root / turn["answer_file_path"]
        answer_path.write_text(
            "The scanned workflow page appears to show a process diagram, but the current "
            "published artifacts are insufficient for a reliable semantic answer.",
            encoding="utf-8",
        )
        trace = retrieval_module.trace_answer_file(
            workspace,
            answer_file=answer_path,
            top=2,
            log_context=turn["log_context"],
        )
        transitioned = complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt=(
                "The scanned workflow page needs a governed multimodal refresh before a final answer."
            ),
            status="answered",
        )
        job_id = str(transitioned["hybrid_refresh_job_ids"][0])

        trace_path = workspace.retrieval_traces_dir / f"{trace['trace_id']}.json"
        trace_payload = read_json(trace_path)
        trace_payload.update(
            {
                "status": "ready",
                "answer_state": "grounded",
                "support_basis": "kb-grounded",
                "published_artifacts_sufficient": True,
                "source_escalation_required": False,
                "source_escalation_reason": None,
                "recommended_hybrid_targets": [],
            }
        )
        write_json(trace_path, trace_payload)
        session_path = workspace.query_sessions_dir / f"{trace['session_id']}.json"
        session_payload = read_json(session_path)
        session_payload.update(
            {
                "status": "ready",
                "published_artifacts_sufficient": True,
                "source_escalation_required": False,
                "source_escalation_reason": None,
            }
        )
        write_json(session_path, session_payload)
        update_conversation_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            updates={
                "attached_shared_job_ids": [],
                "hybrid_refresh_job_ids": [],
                "hybrid_refresh_triggered": False,
                "hybrid_refresh_sources": [],
                "hybrid_refresh_snapshot_id": None,
                "hybrid_refresh_completion_status": None,
                "hybrid_refresh_summary": None,
                "published_artifacts_sufficient": True,
                "source_escalation_required": False,
                "source_escalation_reason": None,
                "status": "prepared",
                "turn_state": "prepared",
            },
        )

        with self.assertRaises(ValueError) as blocked_commit:
            complete_ask_turn(
                workspace,
                conversation_id=turn["conversation_id"],
                turn_id=turn["turn_id"],
                inner_workflow_id="grounded-answer",
                session_ids=[trace["session_id"]],
                trace_ids=[trace["trace_id"]],
                answer_file_path=turn["answer_file_path"],
                response_excerpt="The scanned workflow page shows a governed multimodal diagram.",
                status="answered",
            )
        self.assertIn(job_id, str(blocked_commit.exception))
        run_state = load_run_state(workspace, turn["run_id"])
        self.assertIn(job_id, run_state["attached_shared_job_ids"])

        settle_lane_c_shared_refresh(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            job_id=job_id,
            completion_status="covered",
            summary={"detail": "The multimodal refresh covered the missing source scope."},
        )
        update_conversation_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            updates={"attached_shared_job_ids": []},
        )
        session_payload["recorded_at"] = "2100-01-01T00:00:00Z"
        write_json(session_path, session_payload)
        trace_payload["recorded_at"] = "2100-01-01T00:00:01Z"
        write_json(trace_path, trace_payload)

        complete_ask_turn(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
            inner_workflow_id="grounded-answer",
            session_ids=[trace["session_id"]],
            trace_ids=[trace["trace_id"]],
            answer_file_path=turn["answer_file_path"],
            response_excerpt="The scanned workflow page shows a governed multimodal diagram.",
            status="answered",
        )
        committed_turn = load_turn_record(
            workspace,
            conversation_id=turn["conversation_id"],
            turn_id=turn["turn_id"],
        )
        self.assertIn(job_id, committed_turn["attached_shared_job_ids"])

    def test_run_suite_replays_confirmation_decline_ask_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        self.create_pdf(workspace.source_dir / "fresh.pdf")

        def fake_sync(_paths: WorkspacePaths, assume_yes: bool = False, **kwargs: object) -> CommandReport:
            if assume_yes:
                raise AssertionError("Decline replay should not approve the sync job.")
            run_id = kwargs.get("run_id")
            owner = kwargs.get("owner")
            job = ensure_shared_job(
                workspace,
                job_key="sync:evaluation:decline",
                job_family="sync",
                criticality="answer-critical",
                scope={"workspace_root": str(workspace.root)},
                input_signature="sync:evaluation:decline",
                owner=owner if isinstance(owner, dict) else {"kind": "run", "id": "eval"},
                run_id=str(run_id) if isinstance(run_id, str) else None,
                requires_confirmation=True,
                confirmation_kind="material-sync",
                confirmation_prompt=(
                    "A large unpublished workspace change set was detected. Build or refresh the "
                    "knowledge base now before continuing this question?"
                ),
                confirmation_reason="changed_total=1 >= 1",
            )
            return CommandReport(
                1,
                {
                    "status": "action-required",
                    "sync_status": "awaiting-confirmation",
                    "detail": "Material sync confirmation is required.",
                    "published": False,
                    "control_plane": shared_job_control_plane_payload(
                        job["manifest"],
                        next_command="docmason sync --yes",
                        state="awaiting-confirmation",
                    ),
                },
                [],
            )

        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-confirmation-decline",
                    family="ask-confirmation",
                    question="What do the latest documents say after the newest local updates?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                        needs_latest_workspace_state=True,
                    ),
                    expected_status="ready",
                    expected_answer_state="abstained",
                    expected_support_basis="governed-boundary",
                    reference_facts=["Declining confirmation should settle the same canonical turn."],
                    continuations=[{"message": "no"}],
                    expectations={
                        "final_turn_status": "completed",
                        "reused_turn": True,
                        "auto_sync_triggered": True,
                        "query_session_count": 0,
                        "trace_count": 0,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "shared-job-declined",
                            "shared-job-settled",
                            "projection-enqueued",
                        ],
                    },
                )
            ],
        )

        with mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync):
            run_payload = run_evaluation_suite(
                workspace,
                suite_path=suite_path,
                rubric_path=rubric_path,
                judge_trials_path=judge_path,
                run_label="Ask confirmation decline",
            )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        self.assertEqual(case["execution"]["result"]["turn_status"], "completed")
        self.assertEqual(case["execution"]["result"]["support_basis"], "governed-boundary")
        self.assertIn("answer_file", case["artifact_paths"])

    def test_run_suite_replays_confirmation_approve_ask_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        self.create_pdf(workspace.source_dir / "fresh.pdf")
        shared_job_id: str | None = None

        def fake_sync(_paths: WorkspacePaths, assume_yes: bool = False, **kwargs: object) -> CommandReport:
            nonlocal shared_job_id
            run_id = kwargs.get("run_id")
            owner = kwargs.get("owner")
            if not assume_yes:
                job = ensure_shared_job(
                    workspace,
                    job_key="sync:evaluation:approve",
                    job_family="sync",
                    criticality="answer-critical",
                    scope={"workspace_root": str(workspace.root)},
                    input_signature="sync:evaluation:approve",
                    owner=owner if isinstance(owner, dict) else {"kind": "run", "id": "eval"},
                    run_id=str(run_id) if isinstance(run_id, str) else None,
                    requires_confirmation=True,
                    confirmation_kind="material-sync",
                    confirmation_prompt=(
                        "A large unpublished workspace change set was detected. Build or refresh the "
                        "knowledge base now before continuing this question?"
                    ),
                    confirmation_reason="changed_total=1 >= 1",
                )
                shared_job_id = str(job["manifest"]["job_id"])
                return CommandReport(
                    1,
                    {
                        "status": "action-required",
                        "sync_status": "awaiting-confirmation",
                        "detail": "Material sync confirmation is required.",
                        "published": False,
                        "control_plane": shared_job_control_plane_payload(
                            job["manifest"],
                            next_command="docmason sync --yes",
                            state="awaiting-confirmation",
                        ),
                    },
                    [],
                )
            if not isinstance(shared_job_id, str) or not shared_job_id:
                raise AssertionError("The confirmation job was not created before approval.")
            complete_shared_job(
                workspace,
                shared_job_id,
                result={"status": "completed", "detail": "Operator-eval test sync completed."},
            )
            write_json(
                workspace.current_publish_manifest_path,
                {
                    "snapshot_id": "snapshot-confirm-approve",
                    "published_at": "2026-03-28T00:10:00Z",
                },
            )
            write_json(
                workspace.sync_state_path,
                {
                    "published_source_signature": source_inventory_signature(workspace),
                    "last_publish_at": "2026-03-28T00:10:00Z",
                    "last_sync_at": "2026-03-28T00:10:00Z",
                },
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "sync_status": "valid",
                    "detail": "Published.",
                    "published": True,
                    "change_set": {"stats": {}},
                    "auto_repairs": {"repair_count": 0},
                    "auto_authoring": {"authored_count": 0},
                    "autonomous_steps": [],
                },
                [],
            )

        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-confirmation-approve",
                    family="ask-confirmation",
                    question="What do the latest documents say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                        needs_latest_workspace_state=True,
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=["Approving confirmation should resume the same canonical turn."],
                    continuations=[{"message": "yes"}],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "reused_turn": True,
                        "query_session_count": 1,
                        "trace_count": 1,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "shared-job-approved",
                            "trace-completed",
                            "admissibility-passed",
                            "projection-enqueued",
                        ],
                    },
                )
            ],
        )

        with mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync):
            run_payload = run_evaluation_suite(
                workspace,
                suite_path=suite_path,
                rubric_path=rubric_path,
                judge_trials_path=judge_path,
                run_label="Ask confirmation approve",
            )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        self.assertTrue(case["execution"]["reused_turn"])
        self.assertEqual(case["execution"]["result"]["answer_state"], "grounded")

    def test_run_suite_replays_auto_prepare_and_auto_sync_ask_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        self.create_pdf(workspace.source_dir / "fresh.pdf")
        workspace.bootstrap_state_path.unlink()

        def fake_prepare(*args: object, **kwargs: object) -> CommandReport:
            del args, kwargs
            seed_self_contained_bootstrap_state(
                workspace,
                prepared_at="2026-03-28T00:00:00Z",
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "actions_performed": ["Refreshed bootstrap marker."],
                    "actions_skipped": [],
                    "next_steps": [],
                    "environment": {
                        "package_manager": "uv",
                        "manual_recovery_doc": "docs/setup/manual-workspace-recovery.md",
                    },
                },
                [],
            )

        def fake_sync(*args: object, **kwargs: object) -> CommandReport:
            del args, kwargs
            write_json(
                workspace.current_publish_manifest_path,
                {
                    "snapshot_id": "snapshot-auto-prepare-eval",
                    "published_at": "2026-03-28T00:05:00Z",
                },
            )
            write_json(
                workspace.sync_state_path,
                {
                    "published_source_signature": source_inventory_signature(workspace),
                    "last_publish_at": "2026-03-28T00:05:00Z",
                    "last_sync_at": "2026-03-28T00:05:00Z",
                },
            )
            return CommandReport(
                0,
                {
                    "status": "ready",
                    "sync_status": "valid",
                    "detail": "Published.",
                    "published": True,
                    "change_set": {"stats": {}},
                    "auto_repairs": {"repair_count": 0},
                    "auto_authoring": {"authored_count": 0},
                    "autonomous_steps": [],
                },
                [],
            )

        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-auto-prepare-auto-sync",
                    family="ask-auto-repair",
                    question="What do the latest documents say about the project work plan?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                        needs_latest_workspace_state=True,
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=["The replay should auto-prepare and auto-sync before answering."],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "auto_prepare_triggered": True,
                        "auto_sync_triggered": True,
                        "query_session_count": 1,
                        "trace_count": 1,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "ask-prepared",
                            "trace-completed",
                            "admissibility-passed",
                            "projection-enqueued",
                        ],
                    },
                )
            ],
        )

        with (
            mock.patch(
                "docmason.ask.bootstrap_workspace_with_launcher",
                side_effect=fake_prepare,
            ),
            mock.patch("docmason.ask.prepare_workspace", side_effect=fake_prepare),
            mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync),
        ):
            run_payload = run_evaluation_suite(
                workspace,
                suite_path=suite_path,
                rubric_path=rubric_path,
                judge_trials_path=judge_path,
                run_label="Ask auto prepare sync",
            )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        self.assertTrue(case["execution"]["result"]["auto_prepare_triggered"])
        self.assertTrue(case["execution"]["result"]["auto_sync_triggered"])

    def test_run_suite_replays_same_run_governance_reuse_without_duplicate_sync(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        self.create_pdf(workspace.source_dir / "fresh.pdf")

        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=source_ids,
            cases=[
                self.ask_turn_case(
                    case_id="ask-governance-reuse",
                    family="ask-governance",
                    question="What do the latest workspace documents say about the project outline?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                        needs_latest_workspace_state=True,
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids[0]],
                    required_sources_or_units=[source_ids[0]],
                    minimum_support_overlap=1,
                    reference_facts=[
                        "The replay should reuse the same governed preanswer result instead of rerunning sync."
                    ],
                    continuations=[
                        {
                            "message": "What do the latest workspace documents say about the project outline?",
                            "semantic_analysis": self.semantic_analysis(
                                question_class="answer",
                                question_domain="workspace-corpus",
                                needs_latest_workspace_state=True,
                            ),
                        }
                    ],
                    answer_plan={
                        "answer_text": (
                            "The project planning brief says the outline defines a practical "
                            "work plan and connects planning to implementation."
                        ),
                        "trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "reused_turn": True,
                        "auto_sync_triggered": True,
                        "query_session_count": 1,
                        "trace_count": 1,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "ask-prepared",
                            "preanswer-governance-reused",
                            "trace-completed",
                            "admissibility-passed",
                            "projection-enqueued",
                        ],
                    },
                )
            ],
        )

        def fake_sync(*args: object, **kwargs: object) -> CommandReport:
            return sync_workspace(
                workspace,
                assume_yes=True,
                owner=kwargs.get("owner"),
                run_id=kwargs.get("run_id"),
            )

        with mock.patch("docmason.ask.run_sync_command", side_effect=fake_sync):
            run_payload = run_evaluation_suite(
                workspace,
                suite_path=suite_path,
                rubric_path=rubric_path,
                judge_trials_path=judge_path,
                run_label="Ask governance reuse",
            )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        failed_checks = {
            check["name"]: check
            for check in case["deterministic_checks"]
            if not check["passed"]
        }
        self.assertNotIn("same_run_preanswer_governance_reentry", failed_checks)
        self.assertNotIn("same_run_ask_prepared_reentry", failed_checks)
        self.assertNotIn("same_run_answer_critical_sync_job_reentry", failed_checks)

    def test_run_suite_replays_lane_c_blocked_ask_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")
        source_ids = self.publish_seeded_scanned_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=list(source_ids.values()),
            cases=[
                self.ask_turn_case(
                    case_id="ask-lane-c-blocked",
                    family="ask-lane-c",
                    question="What is shown on the scanned workflow page image?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                        evidence_requirements={
                            "preferred_channels": ["render", "structure"],
                            "inspection_scope": "unit",
                            "prefer_published_artifacts": True,
                        },
                    ),
                    expected_status="ready",
                    expected_answer_state="abstained",
                    expected_support_basis="governed-boundary",
                    expected_primary_sources=[source_ids["scan"]],
                    required_sources_or_units=[source_ids["scan"]],
                    minimum_support_overlap=1,
                    reference_facts=["A blocked Lane C refresh should settle the same turn honestly."],
                    answer_plan={
                        "answer_text": (
                            "The scanned workflow page appears to show a process diagram, but the "
                            "current published artifacts are insufficient for a reliable semantic answer."
                        ),
                        "trace_top": 2,
                    },
                    hybrid_refresh={
                        "completion_status": "blocked",
                        "summary": {
                            "detail": "The required multimodal source refresh could not continue safely."
                        },
                    },
                    expectations={
                        "final_turn_status": "completed",
                        "hybrid_refresh_triggered": True,
                        "hybrid_refresh_completion_status": "blocked",
                        "query_session_count": 1,
                        "trace_count": 1,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "shared-job-waiting",
                            "shared-job-settled",
                            "projection-enqueued",
                        ],
                    },
                )
            ],
        )

        run_payload = run_evaluation_suite(
            workspace,
            suite_path=suite_path,
            rubric_path=rubric_path,
            judge_trials_path=judge_path,
            run_label="Ask lane c blocked",
        )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        self.assertTrue(case["execution"]["result"]["hybrid_refresh_triggered"])
        self.assertEqual(
            case["execution"]["result"]["hybrid_refresh_completion_status"],
            "blocked",
        )
        self.assertIn("hybrid_refresh_work", case["artifact_paths"])
        self.assertIn("shared_job_01_manifest", case["artifact_paths"])

    def test_run_suite_replays_lane_c_covered_ask_turn(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf_with_full_page_image(workspace.source_dir / "scan.pdf")
        self.create_pdf(workspace.source_dir / "control.pdf")
        source_ids = self.publish_seeded_scanned_corpus(workspace)
        suite_path, rubric_path, judge_path = self.write_ask_turn_specs(
            workspace,
            source_ids=list(source_ids.values()),
            cases=[
                self.ask_turn_case(
                    case_id="ask-lane-c-covered",
                    family="ask-lane-c",
                    question="What is shown on the scanned workflow page image?",
                    semantic_analysis=self.semantic_analysis(
                        question_class="answer",
                        question_domain="workspace-corpus",
                        evidence_requirements={
                            "preferred_channels": ["render", "structure"],
                            "inspection_scope": "unit",
                            "prefer_published_artifacts": True,
                        },
                    ),
                    expected_status="ready",
                    expected_answer_state="grounded",
                    expected_support_basis="kb-grounded",
                    expected_primary_sources=[source_ids["scan"]],
                    required_sources_or_units=[source_ids["scan"]],
                    minimum_support_overlap=1,
                    reference_facts=["A covered Lane C refresh should rerun trace and then commit."],
                    answer_plan={
                        "answer_text": (
                            "The scanned workflow page appears to show a process diagram, but the "
                            "current published artifacts are insufficient for a reliable semantic answer."
                        ),
                        "trace_top": 2,
                    },
                    hybrid_refresh={
                        "completion_status": "covered",
                        "summary": {
                            "covered_source_count": 1,
                            "detail": "Governed multimodal refresh coverage was recorded for the source.",
                        },
                        "post_refresh_answer_text": (
                            "The scanned workflow page shows a workflow-style process layout."
                        ),
                        "post_refresh_trace_top": 2,
                    },
                    expectations={
                        "final_turn_status": "answered",
                        "hybrid_refresh_triggered": True,
                        "hybrid_refresh_completion_status": "covered",
                        "query_session_count": 2,
                        "trace_count": 2,
                        "required_run_events": [
                            "preanswer-governance-started",
                            "shared-job-waiting",
                            "shared-job-settled",
                            "trace-completed",
                            "admissibility-passed",
                            "projection-enqueued",
                        ],
                    },
                )
            ],
        )
        real_trace_answer_file: Any = retrieval_module.trace_answer_file
        trace_call_count = 0

        def patched_trace_answer_file(*args: object, **kwargs: object) -> dict[str, object]:
            nonlocal trace_call_count
            trace_call_count += 1
            payload: dict[str, object] = real_trace_answer_file(*args, **kwargs)
            if trace_call_count != 2:
                return payload
            trace_path = workspace.retrieval_traces_dir / f"{payload['trace_id']}.json"
            trace_payload = read_json(trace_path)
            trace_payload.update(
                {
                    "status": "ready",
                    "answer_state": "grounded",
                    "support_basis": "kb-grounded",
                    "published_artifacts_sufficient": True,
                    "source_escalation_required": False,
                    "source_escalation_reason": None,
                    "recommended_hybrid_targets": [],
                }
            )
            write_json(trace_path, trace_payload)
            session_path = workspace.query_sessions_dir / f"{payload['session_id']}.json"
            session_payload = read_json(session_path)
            session_payload["status"] = "ready"
            write_json(session_path, session_payload)
            return {
                **payload,
                "status": "ready",
                "answer_state": "grounded",
                "support_basis": "kb-grounded",
                "published_artifacts_sufficient": True,
                "source_escalation_required": False,
                "source_escalation_reason": None,
                "recommended_hybrid_targets": [],
            }

        with mock.patch(
            "docmason.retrieval.trace_answer_file",
            side_effect=patched_trace_answer_file,
        ):
            run_payload = run_evaluation_suite(
                workspace,
                suite_path=suite_path,
                rubric_path=rubric_path,
                judge_trials_path=judge_path,
                run_label="Ask lane c covered",
            )

        self.assertEqual(run_payload["summary"]["overall_status"], "passed")
        case = run_payload["cases"][0]
        self.assertEqual(
            case["execution"]["result"]["hybrid_refresh_completion_status"],
            "covered",
        )
        self.assertEqual(case["execution"]["result"]["answer_state"], "grounded")
        self.assertIn("hybrid_refresh_work", case["artifact_paths"])
        self.assertIn("retrieval_trace_02", case["artifact_paths"])

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
            run_label="Evaluation test run",
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
        self.assertEqual(baseline["suite_id"], "evaluation-test-suite")

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
                    "The project planning brief says DocMason already ships watch mode "
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

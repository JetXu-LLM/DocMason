"""Decision-complete acceptance coverage for the high-ROI work program."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.ask import _changed_source_relevance
from docmason.conversation import (
    base_turn_record,
    claim_turn_closure_continuation,
    conversation_path,
    load_turn_record,
)
from docmason.front_controller import (
    validate_support_manifest_binding,
    write_external_support_manifest,
)
from docmason.hooks import (
    evaluate_hook_decision,
    normalize_hook_context,
    serialize_hook_decision,
)
from docmason.host_integration import progress_canonical_ask
from docmason.hybrid import focus_render_contract_complete
from docmason.interaction import (
    _persist_transcript_cursor,
    _transcript_records_with_cursor,
)
from docmason.knowledge import (
    SOURCE_ARTIFACT_CONTRACT_VERSION,
    SOURCE_VALIDATOR_VERSION,
    _source_validation_cache_key,
    _source_validation_input_digest,
    _validation_configuration_digest,
    build_staging_artifacts,
    classify_rebuild_telemetry,
    settle_staging_transaction,
    update_source_index,
    validate_target,
)
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.versioning import copy_snapshot_tree, publish_staging_snapshot
from docmason.work_experience import (
    build_turn_work_state,
    evidence_packet_support_sources,
    normalize_decision_gate,
    normalize_decision_resolution,
    normalize_new_decision_gate,
    resolve_continuation_anchor,
)


class HighRoiWorkExperienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.paths = WorkspacePaths(root=Path(self.tempdir.name))

    def test_cbdt_reply_email_attachment_and_constraint_revision(self) -> None:
        attachment = self.paths.root / "meeting-minutes.docx"
        attachment.write_bytes(b"official current meeting minutes")
        first = build_turn_work_state(
            self.paths,
            question=(
                "I will reply to the meeting invitation. Keep Notification and Standard "
                "Campaign separate."
            ),
            semantic_analysis={
                "evidence_items": [{"locator": str(attachment)}],
                "work_brief": {
                    "deliverable": {"kind": "email", "medium": "reply-email"},
                    "scope": {
                        "must_preserve_distinctions": [
                            "Notification",
                            "Standard Campaign",
                        ]
                    },
                },
            },
            previous_turn=None,
            turn_id="turn-1",
        )
        self.assertEqual(first["work_brief"]["deliverable"]["kind"], "email")
        self.assertEqual(first["work_brief"]["deliverable"]["medium"], "reply-email")
        item = first["evidence_packet"]["items"][0]
        self.assertEqual(item["authority"], "user-provided-current")
        self.assertEqual(item["availability"], "available")
        self.assertTrue(item["sha256"])

        prior = {"turn_id": "turn-1", **first, "session_ids": ["session-1"]}
        revision = build_turn_work_state(
            self.paths,
            question="Use 'will coordinate' instead of 'will manage'.",
            semantic_analysis={
                "revision_of": "turn-1",
                "continuation_type": "constraint-update",
                "revision_scope": {"wording": ["ownership verb"]},
                "affected_outputs": ["reply-email"],
            },
            previous_turn=prior,
            turn_id="turn-2",
        )
        self.assertEqual(revision["continuation_type"], "constraint-update")
        self.assertTrue(revision["reused_previous_evidence"])
        self.assertFalse(revision["new_retrieval_executed"])
        self.assertEqual(revision["scope_freshness"], "no-relevant-delta")
        self.assertEqual(revision["evidence_packet"]["items"][0]["sha256"], item["sha256"])
        manifest_sources = evidence_packet_support_sources(revision["evidence_packet"])
        self.assertEqual(manifest_sources[0]["authority"], "user-provided-current")

    def test_personalization_architecture_gate_and_accepted_scope_protection(self) -> None:
        gate = {
            "class": "judgment-authority-gap",
            "question": "Who owns the reusable personalization primitive?",
            "why_user_is_required": "Two valid ownership models change the architecture.",
            "evidence_boundary": "Evidence supports both models but cannot assign authority.",
            "options": [
                {
                    "id": "pc",
                    "label": "PC owns",
                    "impact": "PC carries the domain contract.",
                    "recommended": True,
                },
                {
                    "id": "pai",
                    "label": "PAI owns",
                    "impact": "PAI owns both domain and execution contracts.",
                    "recommended": False,
                },
            ],
            "affected_outputs": ["architecture"],
            "invalidates": ["ownership-model"],
        }
        first = build_turn_work_state(
            self.paths,
            question="Build only the architecture part of the Personalization Centric DA deck.",
            semantic_analysis={
                "work_brief": {
                    "scope": {"in": ["architecture"]},
                    "method": {
                        "framework": "Personalization Framework",
                        "storyline": ["thesis", "ownership", "reusable primitive"],
                    },
                    "artifact_risk": {
                        "batch_cost": "high",
                        "grammar_proven": False,
                        "pilot_recommended": True,
                    },
                },
                "decision_frontier": gate,
                "accepted_scopes": [
                    {
                        "scope_id": "slide-1",
                        "event": "accept",
                        "accepted_by_user": True,
                        "artifact_digest": "digest-1",
                        "accepted_by_turn": "turn-1",
                        "dependencies": ["framework-thesis"],
                    }
                ],
            },
            previous_turn=None,
            turn_id="turn-1",
        )
        self.assertEqual(first["work_brief"]["scope"]["in"], ["architecture"])
        self.assertEqual(first["decision_frontier"]["status"], "open")
        self.assertEqual(first["accepted_scopes"][0]["status"], "accepted")

        prior = {"turn_id": "turn-1", **first}
        unrelated = build_turn_work_state(
            self.paths,
            question="Revise the ownership page only.",
            semantic_analysis={
                "revision_of": "turn-1",
                "continuation_type": "constraint-update",
                "affected_outputs": ["slide-2"],
                "invalidates": ["ownership-model"],
            },
            previous_turn=prior,
            turn_id="turn-2",
        )
        self.assertEqual(unrelated["accepted_scopes"][0]["status"], "accepted")
        self.assertEqual(unrelated["accepted_scopes"][0]["artifact_digest"], "digest-1")

    def test_decision_resolution_is_linked_new_turn_and_never_defaults(self) -> None:
        gate = normalize_decision_gate(
            {
                "class": "high-cost-expression-choice",
                "question": "Which page grammar should scale to the remaining deck?",
                "why_user_is_required": "Both grammars are viable and imply costly rework.",
                "evidence_boundary": "Both grammars satisfy the content evidence.",
                "options": [
                    {
                        "id": "a",
                        "label": "Concept map",
                        "impact": "Validates the architecture thesis before batch work.",
                        "recommended": True,
                    },
                    {
                        "id": "b",
                        "label": "Component map",
                        "impact": "Prioritizes topology over conceptual reuse.",
                        "recommended": False,
                    },
                ],
                "affected_outputs": ["deck"],
            }
        )
        assert gate is not None
        previous = {
            "turn_id": "turn-1",
            "decision_frontier": gate,
            "decision_ledger": [],
            "work_brief": {"artifact_risk": {"batch_cost": "high"}},
            "evidence_packet": {"packet_id": "packet-1", "items": []},
        }
        resolved = build_turn_work_state(
            self.paths,
            question="Choose the concept map.",
            semantic_analysis={
                "resolves_gate_id": gate["gate_id"],
                "decision_resolution": {"option_id": "a"},
            },
            previous_turn=previous,
            turn_id="turn-2",
        )
        self.assertEqual(resolved["revision_of"], "turn-1")
        self.assertEqual(resolved["continuation_type"], "decision-resolution")
        self.assertEqual(resolved["decision_frontier"]["resolved_by_turn"], "turn-2")
        self.assertEqual(resolved["decision_ledger"][0]["resolved_by_turn"], "turn-2")
        self.assertEqual(
            resolved["decision_ledger"][0]["resolution"],
            {"option_id": "a"},
        )
        self.assertNotIn("timeout", gate)
        self.assertIsNone(
            normalize_decision_gate(
                {
                    "class": "evidence-gap",
                    "question": "Where is the file?",
                    "why_user_is_required": "It is missing.",
                }
            )
        )

    def test_decision_resolution_rejects_unknown_option_and_arbitrary_mapping(self) -> None:
        gate = normalize_decision_gate(
            {
                "class": "judgment-authority-gap",
                "question": "Who owns the contract?",
                "why_user_is_required": "Only the accountable owner can decide.",
                "evidence_boundary": "Both ownership models remain viable.",
                "options": [
                    {
                        "id": "domain",
                        "label": "Domain",
                        "impact": "The domain owns the contract.",
                        "recommended": True,
                    },
                    {
                        "id": "platform",
                        "label": "Platform",
                        "impact": "The platform owns the contract.",
                        "recommended": False,
                    },
                ],
                "affected_outputs": ["architecture"],
            }
        )
        assert gate is not None
        self.assertIsNone(normalize_decision_resolution(gate, {"option_id": "typo"}))
        self.assertIsNone(normalize_decision_resolution(gate, {"comment": "Domain"}))
        self.assertIsNone(
            normalize_decision_resolution(
                gate,
                {"option_id": "domain", "free_form": "Use the platform."},
            )
        )
        self.assertEqual(
            normalize_decision_resolution(gate, "Use a shared ownership model."),
            {"free_form": "Use a shared ownership model."},
        )
        prior = {
            "turn_id": "turn-1",
            "decision_frontier": gate,
            "decision_ledger": [],
        }
        blocked = build_turn_work_state(
            self.paths,
            question="Choose the typo option.",
            semantic_analysis={
                "resolves_gate_id": gate["gate_id"],
                "decision_resolution": {"option_id": "typo"},
            },
            previous_turn=prior,
            turn_id="turn-2",
        )
        self.assertEqual(blocked["work_state_issue_code"], "invalid-decision-resolution")
        self.assertEqual(blocked["decision_frontier"]["status"], "open")
        self.assertIsNone(blocked["decision_frontier"]["resolved_by_turn"])
        self.assertEqual(blocked["decision_ledger"], [])

    def test_decision_gate_rejects_duplicate_option_ids(self) -> None:
        self.assertIsNone(
            normalize_decision_gate(
                {
                    "class": "judgment-authority-gap",
                    "question": "Who owns the contract?",
                    "why_user_is_required": "Only the owner can decide.",
                    "evidence_boundary": "Evidence supports both options.",
                    "options": [
                        {
                            "id": "same",
                            "label": "Domain",
                            "impact": "Domain ownership.",
                            "recommended": True,
                        },
                        {
                            "id": "same",
                            "label": "Platform",
                            "impact": "Platform ownership.",
                            "recommended": False,
                        },
                    ],
                    "affected_outputs": ["architecture"],
                }
            )
        )

    def test_new_decision_gate_identity_is_runtime_owned(self) -> None:
        gate = normalize_new_decision_gate(
            {
                "gate_id": "caller-controlled",
                "status": "resolved",
                "opened_at": "2000-01-01T00:00:00Z",
                "resolved_by_turn": "unrelated-turn",
                "class": "judgment-authority-gap",
                "question": "Who owns the contract?",
                "why_user_is_required": "Only the accountable owner can decide.",
                "evidence_boundary": "Both models remain viable.",
                "options": [
                    {
                        "id": "domain",
                        "label": "Domain",
                        "impact": "Domain ownership.",
                        "recommended": True,
                    },
                    {
                        "id": "platform",
                        "label": "Platform",
                        "impact": "Platform ownership.",
                        "recommended": False,
                    },
                ],
                "affected_outputs": ["architecture"],
            }
        )
        assert gate is not None
        self.assertNotEqual(gate["gate_id"], "caller-controlled")
        self.assertEqual(gate["status"], "open")
        self.assertIsNone(gate["resolved_by_turn"])

    def test_progress_persists_material_gate_and_legacy_turns_backfill(self) -> None:
        turn = base_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
            user_question="Draft the architecture deck.",
        )
        for field_name in (
            "work_brief",
            "decision_frontier",
            "accepted_scopes",
            "evidence_packet",
            "closure_continuation",
        ):
            turn.pop(field_name, None)
        turn.update(
            {
                "turn_state": "prepared",
                "status": "prepared",
                "inner_workflow_id": "grounded-composition",
            }
        )
        write_json(
            conversation_path(self.paths, "conversation-1"),
            {
                "conversation_id": "conversation-1",
                "turns": [turn],
            },
        )
        legacy_loaded = load_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
        )
        self.assertEqual(legacy_loaded["work_brief"], {})
        self.assertEqual(legacy_loaded["accepted_scopes"], [])

        progressed = progress_canonical_ask(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
            request={
                "decision_frontier": {
                    "class": "judgment-authority-gap",
                    "question": "Which operating model owns the primitive?",
                    "why_user_is_required": "The organization must assign accountability.",
                    "evidence_boundary": "Repository evidence does not assign authority.",
                    "options": [
                        {
                            "id": "domain",
                            "label": "Domain owner",
                            "impact": "The domain team owns the contract.",
                            "recommended": True,
                        },
                        {
                            "id": "platform",
                            "label": "Platform owner",
                            "impact": "The platform team owns the contract.",
                            "recommended": False,
                        },
                    ],
                    "affected_outputs": ["ownership-model"],
                }
            },
        )
        self.assertEqual(progressed["status"], "awaiting-user-decision")
        persisted = load_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
        )
        self.assertEqual(persisted["turn_state"], "awaiting-user-decision")
        self.assertEqual(persisted["decision_frontier"]["status"], "open")
        self.assertNotIn("timeout", persisted["decision_frontier"])
        repeated = progress_canonical_ask(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
            request={
                "decision_frontier": {
                    "class": "judgment-authority-gap",
                    "question": "A replacement question must not overwrite the open gate.",
                    "why_user_is_required": "This should be ignored while a gate is open.",
                    "evidence_boundary": "The existing decision remains pending.",
                    "options": [
                        {
                            "id": "x",
                            "label": "X",
                            "impact": "Replace the gate.",
                            "recommended": True,
                        },
                        {
                            "id": "y",
                            "label": "Y",
                            "impact": "Replace the gate another way.",
                            "recommended": False,
                        },
                    ],
                    "affected_outputs": ["ownership-model"],
                }
            },
        )
        self.assertEqual(repeated["status"], "awaiting-user-decision")
        self.assertEqual(
            repeated["decision_frontier"]["gate_id"],
            persisted["decision_frontier"]["gate_id"],
        )
        self.assertEqual(
            repeated["decision_frontier"]["question"],
            "Which operating model owns the primitive?",
        )

    def test_progress_cannot_open_gate_on_completed_turn(self) -> None:
        turn = base_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
            user_question="Draft the architecture deck.",
        )
        turn.update(
            {
                "turn_state": "committed",
                "status": "completed",
                "committed_run_id": "run-1",
            }
        )
        write_json(
            conversation_path(self.paths, "conversation-1"),
            {"conversation_id": "conversation-1", "turns": [turn]},
        )
        blocked = progress_canonical_ask(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
            request={
                "decision_frontier": {
                    "class": "judgment-authority-gap",
                    "question": "Who owns the contract?",
                    "why_user_is_required": "Only the accountable owner can decide.",
                    "evidence_boundary": "Both models remain viable.",
                    "options": [
                        {
                            "id": "domain",
                            "label": "Domain",
                            "impact": "Domain ownership.",
                            "recommended": True,
                        },
                        {
                            "id": "platform",
                            "label": "Platform",
                            "impact": "Platform ownership.",
                            "recommended": False,
                        },
                    ],
                    "affected_outputs": ["architecture"],
                }
            },
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(
            blocked["primary_issue_code"],
            "decision-gate-turn-not-progressable",
        )
        persisted = load_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
        )
        self.assertIsNone(persisted["decision_frontier"])

    def test_unknown_freshness_does_not_trigger_global_sync(self) -> None:
        should_sync, reason, freshness = _changed_source_relevance(
            question="Summarize the operating model.",
            change_set={
                "changes": [
                    {
                        "source_id": "unrelated",
                        "current_path": "original_doc/unrelated.pdf",
                        "change_classification": "modified",
                    }
                ]
            },
            source_scope_policy={"scope_mode": "global"},
            reference_resolution=None,
            needs_latest_workspace_state=False,
        )
        self.assertFalse(should_sync)
        self.assertIn("unresolved", reason)
        self.assertEqual(freshness, "unresolved-target-freshness")

    def test_hook_host_parity_and_exactly_one_stop_continuation(self) -> None:
        answer = self.paths.root / "runtime" / "answers" / "c" / "t.md"
        answer.parent.mkdir(parents=True, exist_ok=True)
        answer.write_text("Clean answer", encoding="utf-8")
        turn = {
            "turn_id": "turn-1",
            "user_question": "Finish the current governed answer.",
            "turn_state": "prepared",
            "status": "prepared",
            "answer_file_path": str(answer.relative_to(self.paths.root)),
            "session_ids": ["session-1"],
            "attached_shared_job_ids": [],
            "accepted_scopes": [],
        }
        codex = normalize_hook_context(
            host="codex",
            event_name="Stop",
            payload={"session_id": "host-1", "stop_hook_active": False},
        )
        claude = normalize_hook_context(
            host="claude-code",
            event_name="Stop",
            payload={"session_id": "host-1", "stop_hook_active": False},
        )
        self.assertEqual(
            {key: value for key, value in codex.items() if key != "host"},
            {key: value for key, value in claude.items() if key != "host"},
        )
        for host in ("chatgpt-work", "claude-code"):
            mirror = self.paths.interaction_ingest_dir / host / "host-1.jsonl"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            mirror.write_text(
                '{"record_type":"prompt-submit","prompt":"Finish the current governed answer."}\n',
                encoding="utf-8",
            )
        with (
            mock.patch("docmason.hooks._linked_turn", return_value=("conversation-1", turn)),
            mock.patch(
                "docmason.hooks.claim_turn_closure_continuation",
                side_effect=[True, False],
            ),
        ):
            first = evaluate_hook_decision(self.paths, codex)
            second = evaluate_hook_decision(self.paths, claude)
        self.assertEqual(first["action"], "continue-once")
        self.assertEqual(second["action"], "allow")
        self.assertEqual(
            serialize_hook_decision(codex, first)["decision"],  # type: ignore[index]
            "block",
        )
        active = {**codex, "stop_hook_active": True}
        with mock.patch("docmason.hooks._linked_turn", return_value=("conversation-1", turn)):
            self.assertEqual(evaluate_hook_decision(self.paths, active)["action"], "allow")

    def test_prompt_hook_injects_exact_revision_and_decision_anchors(self) -> None:
        turn = {
            "turn_id": "turn-7",
            "decision_frontier": {
                "gate_id": "gate-3",
                "status": "open",
                "question": "Which ownership model should be used?",
                "options": [
                    {"id": "domain", "label": "Domain ownership"},
                    {"id": "platform", "label": "Platform ownership"},
                ],
            },
            "accepted_scopes": [
                {"scope_id": "slide-1", "status": "accepted"},
            ],
        }
        context = normalize_hook_context(
            host="codex",
            event_name="UserPromptSubmit",
            payload={"session_id": "host-1", "prompt": "Use domain ownership."},
        )
        with mock.patch(
            "docmason.hooks._linked_turn",
            return_value=("conversation-1", turn),
        ):
            decision = evaluate_hook_decision(self.paths, context)
        additional_context = str(decision["additional_context"])
        self.assertIn("`turn-7`", additional_context)
        self.assertIn("`revision_of`", additional_context)
        self.assertIn("`gate-3`", additional_context)
        self.assertIn("domain=Domain ownership", additional_context)
        self.assertIn("platform=Platform ownership", additional_context)
        self.assertIn("slide-1", additional_context)
        self.assertIn("otherwise leave it open", additional_context)

    def test_prompt_hook_is_silent_without_linked_continuation_state(self) -> None:
        context = normalize_hook_context(
            host="codex",
            event_name="UserPromptSubmit",
            payload={"session_id": "host-1", "prompt": "Start unrelated work."},
        )
        with mock.patch("docmason.hooks._linked_turn", return_value=None):
            decision = evaluate_hook_decision(self.paths, context)
        self.assertEqual(decision["action"], "allow")
        self.assertEqual(decision["reason"], "no-continuation-state")
        self.assertEqual(decision["additional_context"], "")
        self.assertIsNone(serialize_hook_decision(context, decision))

    def test_transcript_cursor_append_truncate_and_corruption_recovery(self) -> None:
        transcript_path = self.paths.root / "host.jsonl"
        transcript_path.write_text('{"a":1}\n', encoding="utf-8")
        records, state = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-1",
            transcript_path=transcript_path,
        )
        self.assertEqual(state["mode"], "full-fallback")
        _persist_transcript_cursor(
            provider="codex",
            host_thread_ref="thread-1",
            transcript={"turns": [{"native_turn_id": "turn-1"}]},
            cursor_state=state,
        )
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write('{"a":2}\n')
        appended, append_state = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-1",
            transcript_path=transcript_path,
        )
        self.assertEqual(append_state["mode"], "incremental-append")
        self.assertEqual(append_state["records_read"], 1)
        self.assertEqual(len(appended), 2)

        transcript_path.write_text('{"a":3}\n', encoding="utf-8")
        _records, truncated = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-1",
            transcript_path=transcript_path,
        )
        self.assertEqual(truncated["mode"], "full-fallback")
        self.assertIn(truncated["fallback_reason"], {"truncation", "tail-checksum-mismatch"})

        write_json(
            self.paths.transcript_cursor_path("codex", "thread-1"),
            {"schema_version": "corrupt"},
        )
        _records, corrupt = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-1",
            transcript_path=transcript_path,
        )
        self.assertEqual(corrupt["fallback_reason"], "schema-change")

        self.paths.transcript_cursor_path("codex", "thread-1").write_text(
            "{not-json",
            encoding="utf-8",
        )
        raw_records, raw_corrupt = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-1",
            transcript_path=transcript_path,
        )
        self.assertEqual(raw_corrupt["mode"], "full-fallback")
        self.assertEqual(raw_corrupt["fallback_reason"], "corrupt-cursor")
        self.assertEqual(raw_records, [{"a": 3}])

    def test_validation_cache_key_invalidates_on_configuration_change(self) -> None:
        manifest = {"source_fingerprint": "fingerprint"}
        first_digest = _validation_configuration_digest(self.paths)
        first_key = _source_validation_cache_key(
            source_manifest=manifest,
            configuration_digest=first_digest,
        )
        (self.paths.root / "docmason.yaml").write_text("mode: changed\n", encoding="utf-8")
        second_digest = _validation_configuration_digest(self.paths)
        second_key = _source_validation_cache_key(
            source_manifest=manifest,
            configuration_digest=second_digest,
        )
        self.assertNotEqual(first_key, second_key)

    def test_validation_cache_key_invalidates_on_staged_artifact_change(self) -> None:
        source_dir = self.paths.root / "source"
        source_dir.mkdir()
        artifact = source_dir / "knowledge.json"
        artifact.write_text('{"summary": "first"}\n', encoding="utf-8")
        manifest = {"source_fingerprint": "stable-original"}
        config = _validation_configuration_digest(self.paths)
        first = _source_validation_cache_key(
            source_manifest=manifest,
            configuration_digest=config,
            validation_input_digest=_source_validation_input_digest(source_dir),
        )
        artifact.write_text('{"summary": "second"}\n', encoding="utf-8")
        second = _source_validation_cache_key(
            source_manifest=manifest,
            configuration_digest=config,
            validation_input_digest=_source_validation_input_digest(source_dir),
        )
        self.assertNotEqual(first, second)

    def test_legal_staging_validation_skips_full_contract_rescan(self) -> None:
        self.paths.knowledge_base_staging_dir.mkdir(parents=True)
        signature = "stable-signature"
        write_json(
            self.paths.staging_validation_report_path,
            {
                "status": "valid",
                "source_signature": signature,
                "validation_cache": {
                    "artifact_contract_version": SOURCE_ARTIFACT_CONTRACT_VERSION,
                    "validator_version": SOURCE_VALIDATOR_VERSION,
                    "configuration_digest": _validation_configuration_digest(self.paths),
                },
            },
        )
        write_json(
            self.paths.validation_cache_path,
            {"schema_version": 1, "entries": {}},
        )
        with mock.patch(
            "docmason.knowledge.staging_incomplete_source_ids",
            side_effect=AssertionError("full-contract-rescan"),
        ):
            telemetry = classify_rebuild_telemetry(
                self.paths,
                current_signature=signature,
                state={
                    "staging_source_signature": signature,
                    "published_source_signature": signature,
                    "lane_b_follow_up_summary": {},
                },
                active_sources=[],
                change_set={"stats": {}},
                ambiguous_match=False,
                interaction_snapshot={"pending_promotion_count": 0},
            )
        self.assertTrue(telemetry["artifact_contract_cache_hit"])
        self.assertEqual(telemetry["rebuild_cause"], "published-current-repair")

    def test_validation_cache_hits_after_validator_enrichment_writes(self) -> None:
        self.paths.source_dir.mkdir(parents=True)
        self.paths.runtime_dir.mkdir(parents=True)
        source = self.paths.source_dir / "cache.md"
        source.write_text("# Cache fixture\n\nStable evidence.\n", encoding="utf-8")
        _index, active_sources, _ambiguous, _changes = update_source_index(self.paths)
        build_staging_artifacts(self.paths, active_sources, None)

        first = validate_target(self.paths, "staging", use_cache=True)
        second = validate_target(self.paths, "staging", use_cache=True)

        self.assertEqual(first["validation_cache"]["misses"], 1)
        self.assertEqual(second["validation_cache"]["hits"], 1)
        self.assertEqual(second["validation_cache"]["misses"], 0)
        self.assertEqual(second["validation_cache"]["entry_count"], 1)

    def test_publication_copy_fallback_validates_inventory(self) -> None:
        source = self.paths.root / "staging"
        target = self.paths.root / "published"
        source.mkdir()
        for filename in (
            "catalog.json",
            "coverage_manifest.json",
            "graph_edges.json",
            "pending_work.json",
            "hybrid_work.json",
            "publish_manifest.json",
            "validation_report.json",
        ):
            (source / filename).write_text("{}\n", encoding="utf-8")
        with (
            mock.patch("docmason.versioning.platform.system", return_value="Linux"),
            mock.patch(
                "docmason.versioning.subprocess.run",
                return_value=mock.Mock(returncode=1),
            ),
        ):
            telemetry = copy_snapshot_tree(source, target)
        self.assertEqual(telemetry["method"], "copytree")
        self.assertEqual(
            {path.name for path in target.iterdir()},
            {path.name for path in source.iterdir()},
        )

    def test_darwin_publication_prefers_native_atomic_clone(self) -> None:
        source = self.paths.root / "darwin-staging"
        target = self.paths.root / "darwin-published"
        source.mkdir()
        for filename in (
            "catalog.json",
            "coverage_manifest.json",
            "graph_edges.json",
            "pending_work.json",
            "hybrid_work.json",
            "publish_manifest.json",
            "validation_report.json",
        ):
            (source / filename).write_text("{}\n", encoding="utf-8")

        def clone_tree(source_path: Path, target_path: Path) -> bool:
            shutil.copytree(source_path, target_path)
            return True

        with (
            mock.patch("docmason.versioning.platform.system", return_value="Darwin"),
            mock.patch(
                "docmason.versioning._darwin_clone_tree",
                side_effect=clone_tree,
            ),
            mock.patch("docmason.versioning.subprocess.run") as fallback,
        ):
            telemetry = copy_snapshot_tree(source, target)
        self.assertEqual(telemetry["method"], "apfs-clone")
        self.assertEqual(telemetry["clone_strategy"], "clonefile")
        fallback.assert_not_called()

    def test_publication_clone_rejects_missing_nested_file(self) -> None:
        source = self.paths.root / "nested-staging"
        target = self.paths.root / "nested-published"
        nested = source / "sources" / "source-1"
        nested.mkdir(parents=True)
        for filename in (
            "catalog.json",
            "coverage_manifest.json",
            "graph_edges.json",
            "pending_work.json",
            "hybrid_work.json",
            "publish_manifest.json",
            "validation_report.json",
        ):
            (source / filename).write_text("{}\n", encoding="utf-8")
        (nested / "knowledge.md").write_text("required nested artifact\n", encoding="utf-8")

        def incomplete_clone(source_path: Path, target_path: Path) -> bool:
            shutil.copytree(source_path, target_path)
            (target_path / "sources" / "source-1" / "knowledge.md").unlink()
            return True

        with (
            mock.patch("docmason.versioning.platform.system", return_value="Darwin"),
            mock.patch(
                "docmason.versioning._darwin_clone_tree",
                side_effect=incomplete_clone,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "file-inventory validation"):
                copy_snapshot_tree(source, target)
        self.assertFalse(target.exists())

    def test_hidden_pptx_units_do_not_require_focus_render_assets(self) -> None:
        source_dir = self.paths.root / "hidden-slide-source"
        source_dir.mkdir()
        write_json(
            source_dir / "evidence_manifest.json",
            {
                "document_type": "pptx",
                "units": [{"unit_id": "slide-001", "hidden": True}],
            },
        )
        write_json(
            source_dir / "artifact_index.json",
            {
                "artifacts": [
                    {
                        "artifact_id": "slide-001:picture-001",
                        "unit_id": "slide-001",
                        "artifact_type": "picture",
                    }
                ]
            },
        )
        self.assertTrue(focus_render_contract_complete(source_dir))

    def test_dirty_source_install_rolls_back_source_and_root_artifacts(self) -> None:
        self.paths.source_dir.mkdir(parents=True)
        self.paths.knowledge_base_dir.mkdir(parents=True)
        self.paths.runtime_dir.mkdir(parents=True)
        source_path = self.paths.source_dir / "brief.md"
        source_path.write_text("# Stable brief\n\nOriginal truth.\n", encoding="utf-8")
        _index, active_sources, _ambiguous, _changes = update_source_index(self.paths)
        build_staging_artifacts(self.paths, active_sources, None)

        source_id = str(active_sources[0]["source_id"])
        staged_manifest_path = (
            self.paths.knowledge_base_staging_dir / "sources" / source_id / "source_manifest.json"
        )
        prior_manifest = read_json(staged_manifest_path)
        prior_catalog = self.paths.staging_catalog_path.read_bytes()

        source_path.write_text("# Changed brief\n\nNew uncommitted truth.\n", encoding="utf-8")
        _index, dirty_sources, _ambiguous, _changes = update_source_index(self.paths)

        def fail_after_root_mutation(*_args: object, **_kwargs: object) -> object:
            self.paths.staging_catalog_path.write_text("partial", encoding="utf-8")
            raise RuntimeError("root-artifact-write-failed")

        with mock.patch(
            "docmason.knowledge.write_staging_root_artifacts",
            side_effect=fail_after_root_mutation,
        ):
            with self.assertRaisesRegex(RuntimeError, "root-artifact-write-failed"):
                build_staging_artifacts(self.paths, dirty_sources, None)

        self.assertEqual(read_json(staged_manifest_path), prior_manifest)
        self.assertEqual(self.paths.staging_catalog_path.read_bytes(), prior_catalog)
        self.assertFalse((self.paths.knowledge_base_dir / ".staging-build").exists())
        self.assertEqual(
            list(self.paths.knowledge_base_dir.glob(".staging-backup-*")),
            [],
        )

    def test_one_dirty_source_leaves_unchanged_staging_tree_in_place(self) -> None:
        self.paths.source_dir.mkdir(parents=True)
        self.paths.runtime_dir.mkdir(parents=True)
        first_source = self.paths.source_dir / "first.md"
        second_source = self.paths.source_dir / "second.md"
        first_source.write_text("# First\n\nInitial.\n", encoding="utf-8")
        second_source.write_text("# Second\n\nStable.\n", encoding="utf-8")
        _index, active_sources, _ambiguous, _changes = update_source_index(self.paths)
        build_staging_artifacts(self.paths, active_sources, None)
        second_entry = next(
            source for source in active_sources if source["current_path"].endswith("second.md")
        )
        stable_dir = (
            self.paths.knowledge_base_staging_dir / "sources" / str(second_entry["source_id"])
        )
        stable_inode = stable_dir.stat().st_ino

        first_source.write_text("# First\n\nChanged.\n", encoding="utf-8")
        _index, changed_sources, _ambiguous, _changes = update_source_index(self.paths)
        _catalog, _summaries, _ambiguous, stats = build_staging_artifacts(
            self.paths,
            changed_sources,
            None,
        )

        self.assertEqual(stats["rebuilt_sources"], 1)
        self.assertEqual(stats["reused_sources"], 1)
        self.assertEqual(stats["files_copied"], 0)
        self.assertEqual(stable_dir.stat().st_ino, stable_inode)

    def test_support_manifest_binds_exact_answer_bytes(self) -> None:
        answer = self.paths.root / "runtime" / "answers" / "c" / "t.md"
        answer.parent.mkdir(parents=True)
        answer_text = "Clean business answer with a meaningful trailing newline.\n"
        answer.write_text(answer_text, encoding="utf-8")
        manifest_path = write_external_support_manifest(
            self.paths,
            conversation_id="c",
            turn_id="t",
            answer_file_path=str(answer.relative_to(self.paths.root)),
            support_basis="external-source-verified",
            sources=[
                {
                    "locator": "host:attachment-1",
                    "sha256": "source-digest",
                    "authority": "official-current",
                    "freshness": "current-for-turn",
                }
            ],
        )
        manifest = read_json(self.paths.root / manifest_path)
        self.assertEqual(
            manifest["answer_digest"],
            {
                "algorithm": "sha256",
                "hex": hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
                "byte_count": len(answer_text.encode("utf-8")),
            },
        )
        answer.write_text("Tampered answer\n", encoding="utf-8")
        issues = validate_support_manifest_binding(
            self.paths,
            manifest,
            conversation_id="c",
            turn_id="t",
            answer_file_path=str(answer.relative_to(self.paths.root)),
            support_basis="external-source-verified",
        )
        self.assertIn("support-manifest-digest-mismatch", [code for code, _ in issues])

    def test_unrelated_message_cannot_inherit_revision_or_evidence(self) -> None:
        prior = {
            "turn_id": "turn-1",
            "work_brief": {"scope": {"in": ["old-work"]}},
            "evidence_packet": {
                "packet_id": "packet-1",
                "items": [{"locator": "host:old", "availability": "available"}],
            },
            "session_ids": ["old-session"],
        }
        state = build_turn_work_state(
            self.paths,
            question="Start a separate task.",
            semantic_analysis={"work_brief": {"scope": {"in": ["new-work"]}}},
            previous_turn=prior,
            turn_id="turn-2",
        )
        self.assertIsNone(state["revision_of"])
        self.assertIsNone(state["continuation_type"])
        self.assertFalse(state["reused_previous_evidence"])
        self.assertEqual(state["work_brief"]["scope"]["in"], ["new-work"])
        self.assertEqual(state["evidence_packet"]["items"], [])

    def test_gate_resolution_requires_exact_open_gate(self) -> None:
        gate = normalize_decision_gate(
            {
                "class": "judgment-authority-gap",
                "question": "Who owns the contract?",
                "why_user_is_required": "Only the accountable owner can decide.",
                "evidence_boundary": "Evidence supports both ownership models.",
                "options": [
                    {
                        "id": "a",
                        "label": "Domain",
                        "impact": "Domain owns the contract.",
                        "recommended": True,
                    },
                    {
                        "id": "b",
                        "label": "Platform",
                        "impact": "Platform owns the contract.",
                        "recommended": False,
                    },
                ],
                "affected_outputs": ["architecture"],
            }
        )
        assert gate is not None
        anchor, issue = resolve_continuation_anchor(
            [{"turn_id": "turn-1", "decision_frontier": gate}],
            {
                "resolves_gate_id": "wrong-gate",
                "decision_resolution": {"option_id": "a"},
            },
        )
        self.assertIsNone(anchor)
        self.assertEqual(issue["code"], "unknown-decision-gate")  # type: ignore[index]

    def test_accepted_scope_requires_explicit_user_acceptance(self) -> None:
        state = build_turn_work_state(
            self.paths,
            question="Accept slide one.",
            semantic_analysis={
                "accepted_scopes": [
                    {
                        "scope_id": "slide-1",
                        "event": "accept",
                        "artifact_digest": "digest",
                    }
                ]
            },
            previous_turn=None,
            turn_id="turn-1",
        )
        self.assertEqual(
            state["work_state_issue_code"],
            "accepted-scope-missing-user-acceptance",
        )

    def test_accepted_scope_reopen_requires_explicit_user_authority(self) -> None:
        previous = {
            "turn_id": "turn-1",
            "accepted_scopes": [
                {
                    "scope_id": "slide-1",
                    "status": "accepted",
                    "accepted_by_user": True,
                    "artifact_digest": "digest",
                }
            ],
        }
        blocked = build_turn_work_state(
            self.paths,
            question="Revise slide one.",
            semantic_analysis={
                "revision_of": "turn-1",
                "continuation_type": "constraint-update",
                "accepted_scopes": [
                    {"scope_id": "slide-1", "event": "reopen", "reason": "Revise it."}
                ],
            },
            previous_turn=previous,
            turn_id="turn-2",
        )
        self.assertEqual(
            blocked["work_state_issue_code"],
            "accepted-scope-reopen-missing-user-authority",
        )
        self.assertEqual(blocked["accepted_scopes"][0]["status"], "accepted")

        reopened = build_turn_work_state(
            self.paths,
            question="I explicitly want to reopen slide one.",
            semantic_analysis={
                "revision_of": "turn-1",
                "continuation_type": "constraint-update",
                "accepted_scopes": [
                    {
                        "scope_id": "slide-1",
                        "event": "reopen",
                        "reopened_by_user": True,
                        "reason": "The user explicitly reopened this slide.",
                    }
                ],
            },
            previous_turn=previous,
            turn_id="turn-3",
        )
        self.assertIsNone(reopened["work_state_issue_code"])
        self.assertEqual(reopened["accepted_scopes"][0]["status"], "reopened")
        self.assertTrue(reopened["accepted_scopes"][0]["reopened_by_user"])

    def test_accepted_scope_updates_are_atomic(self) -> None:
        state = build_turn_work_state(
            self.paths,
            question="Accept two slides.",
            semantic_analysis={
                "accepted_scopes": [
                    {
                        "scope_id": "slide-1",
                        "event": "accept",
                        "accepted_by_user": True,
                        "artifact_digest": "digest-1",
                    },
                    {
                        "scope_id": "slide-2",
                        "event": "accept",
                        "artifact_digest": "digest-2",
                    },
                ]
            },
            previous_turn=None,
            turn_id="turn-1",
        )
        self.assertEqual(
            state["work_state_issue_code"],
            "accepted-scope-missing-user-acceptance",
        )
        self.assertEqual(state["accepted_scopes"], [])

        malformed = build_turn_work_state(
            self.paths,
            question="Accept slide one.",
            semantic_analysis={"accepted_scopes": {"scope_id": "slide-1"}},
            previous_turn=None,
            turn_id="turn-2",
        )
        self.assertEqual(
            malformed["work_state_issue_code"],
            "accepted-scope-update-invalid",
        )

    def test_compound_failure_does_not_shadow_open_decision_gate(self) -> None:
        gate = normalize_decision_gate(
            {
                "class": "judgment-authority-gap",
                "question": "Who owns the contract?",
                "why_user_is_required": "Only the accountable owner can decide.",
                "evidence_boundary": "Both models remain viable.",
                "options": [
                    {
                        "id": "domain",
                        "label": "Domain",
                        "impact": "Domain ownership.",
                        "recommended": True,
                    },
                    {
                        "id": "platform",
                        "label": "Platform",
                        "impact": "Platform ownership.",
                        "recommended": False,
                    },
                ],
                "affected_outputs": ["architecture"],
            }
        )
        assert gate is not None
        owner = {
            "turn_id": "turn-1",
            "decision_frontier": gate,
            "decision_ledger": [],
        }
        blocked = build_turn_work_state(
            self.paths,
            question="Choose domain ownership and accept slide one.",
            semantic_analysis={
                "resolves_gate_id": gate["gate_id"],
                "decision_resolution": {"option_id": "domain"},
                "accepted_scopes": [
                    {
                        "scope_id": "slide-1",
                        "event": "accept",
                        "artifact_digest": "digest-1",
                    }
                ],
            },
            previous_turn=owner,
            turn_id="turn-2",
        )
        self.assertEqual(
            blocked["work_state_issue_code"],
            "accepted-scope-missing-user-acceptance",
        )
        self.assertIsNone(blocked["decision_frontier"])
        self.assertEqual(blocked["decision_ledger"], [])

        anchor, issue = resolve_continuation_anchor(
            [owner, {"turn_id": "turn-2", **blocked}],
            {
                "resolves_gate_id": gate["gate_id"],
                "decision_resolution": {"option_id": "domain"},
            },
        )
        self.assertIsNone(issue)
        self.assertEqual(anchor["turn_id"], "turn-1")  # type: ignore[index]

    def test_stop_hook_never_closes_an_unrelated_prior_prompt(self) -> None:
        answer = self.paths.root / "runtime" / "answers" / "c" / "t.md"
        answer.parent.mkdir(parents=True)
        answer.write_text("Answer", encoding="utf-8")
        mirror = self.paths.interaction_ingest_dir / "chatgpt-work" / "host-1.jsonl"
        mirror.parent.mkdir(parents=True)
        mirror.write_text(
            '{"record_type":"prompt-submit","prompt":"Run operator diagnostics."}\n',
            encoding="utf-8",
        )
        turn = {
            "turn_id": "turn-1",
            "user_question": "Answer the old document question.",
            "turn_state": "prepared",
            "status": "prepared",
            "answer_file_path": str(answer.relative_to(self.paths.root)),
            "session_ids": ["session-1"],
            "attached_shared_job_ids": [],
            "accepted_scopes": [],
        }
        context = normalize_hook_context(
            host="codex",
            event_name="Stop",
            payload={"session_id": "host-1", "stop_hook_active": False},
        )
        with (
            mock.patch("docmason.hooks._linked_turn", return_value=("c", turn)),
            mock.patch("docmason.hooks.claim_turn_closure_continuation") as claim,
        ):
            decision = evaluate_hook_decision(self.paths, context)
        self.assertEqual(decision["action"], "allow")
        claim.assert_not_called()

    def test_stale_closure_claim_can_recover_once(self) -> None:
        turn = base_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
            user_question="Question",
        )
        turn["closure_continuation"] = {
            "status": "requested",
            "requested_at": "2000-01-01T00:00:00Z",
            "retry_count": 0,
        }
        write_json(
            conversation_path(self.paths, "conversation-1"),
            {"conversation_id": "conversation-1", "turns": [turn]},
        )
        self.assertTrue(
            claim_turn_closure_continuation(
                self.paths,
                conversation_id="conversation-1",
                turn_id="turn-1",
                host="chatgpt-work",
            )
        )
        updated = load_turn_record(
            self.paths,
            conversation_id="conversation-1",
            turn_id="turn-1",
        )
        self.assertEqual(updated["closure_continuation"]["retry_count"], 1)
        self.assertFalse(
            claim_turn_closure_continuation(
                self.paths,
                conversation_id="conversation-1",
                turn_id="turn-1",
                host="chatgpt-work",
            )
        )
        updated["closure_continuation"]["requested_at"] = "2000-01-01T00:00:00Z"
        write_json(
            conversation_path(self.paths, "conversation-1"),
            {"conversation_id": "conversation-1", "turns": [updated]},
        )
        self.assertFalse(
            claim_turn_closure_continuation(
                self.paths,
                conversation_id="conversation-1",
                turn_id="turn-1",
                host="chatgpt-work",
            )
        )

    def test_retained_staging_candidate_can_roll_back_after_late_failure(self) -> None:
        self.paths.source_dir.mkdir(parents=True)
        self.paths.runtime_dir.mkdir(parents=True)
        source = self.paths.source_dir / "brief.md"
        source.write_text("# Original\n", encoding="utf-8")
        _index, active, _ambiguous, _changes = update_source_index(self.paths)
        build_staging_artifacts(self.paths, active, None)
        source_id = str(active[0]["source_id"])
        prior_manifest = read_json(
            self.paths.knowledge_base_staging_dir / "sources" / source_id / "source_manifest.json"
        )
        prior_catalog = self.paths.staging_catalog_path.read_bytes()

        source.write_text("# Candidate\n", encoding="utf-8")
        _index, dirty, _ambiguous, _changes = update_source_index(self.paths)
        _catalog, _summaries, _ambiguous, stats = build_staging_artifacts(
            self.paths,
            dirty,
            None,
            retain_transaction=True,
            source_signature="candidate-signature",
        )
        transaction = self.paths.root / str(stats["staging_transaction_path"])
        self.assertTrue(transaction.exists())
        (self.paths.staging_catalog_path).write_text("late invalid state", encoding="utf-8")
        outcome = settle_staging_transaction(self.paths, transaction, commit=False)
        self.assertEqual(outcome["outcome"], "rolled-back")
        self.assertEqual(
            read_json(
                self.paths.knowledge_base_staging_dir
                / "sources"
                / source_id
                / "source_manifest.json"
            ),
            prior_manifest,
        )
        self.assertEqual(self.paths.staging_catalog_path.read_bytes(), prior_catalog)

    def test_validation_digest_detects_nested_artifact_mutation(self) -> None:
        source_dir = self.paths.root / "source-with-render"
        render = source_dir / "renders" / "page.png"
        render.parent.mkdir(parents=True)
        render.write_bytes(b"first")
        first = _source_validation_input_digest(source_dir)
        render.write_bytes(b"second")
        second = _source_validation_input_digest(source_dir)
        self.assertNotEqual(first, second)

    def test_large_incomplete_jsonl_tail_is_not_reparsed(self) -> None:
        transcript_path = self.paths.root / "large-tail.jsonl"
        transcript_path.write_text('{"a":1}\n', encoding="utf-8")
        _records, first_state = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-large",
            transcript_path=transcript_path,
        )
        _persist_transcript_cursor(
            provider="codex",
            host_thread_ref="thread-large",
            transcript={"turns": []},
            cursor_state=first_state,
        )
        with transcript_path.open("ab") as handle:
            handle.write(b"x" * 70000)
        _records, observed = _transcript_records_with_cursor(
            self.paths,
            provider="codex",
            host_thread_ref="thread-large",
            transcript_path=transcript_path,
        )
        self.assertEqual(observed["mode"], "no-change")
        with mock.patch(
            "docmason.interaction._complete_jsonl_offset",
            side_effect=AssertionError("incomplete tail rescanned"),
        ):
            _records, repeated = _transcript_records_with_cursor(
                self.paths,
                provider="codex",
                host_thread_ref="thread-large",
                transcript_path=transcript_path,
            )
        self.assertEqual(repeated["mode"], "no-change")

    def test_publication_commit_failure_restores_previous_legal_snapshot(self) -> None:
        staging = self.paths.knowledge_base_staging_dir
        staging.mkdir(parents=True)
        validation = {"status": "valid", "source_signature": "new-signature"}
        for filename in (
            "catalog.json",
            "coverage_manifest.json",
            "graph_edges.json",
            "pending_work.json",
            "hybrid_work.json",
            "publish_manifest.json",
        ):
            write_json(staging / filename, {})
        write_json(staging / "validation_report.json", validation)

        old_root = self.paths.knowledge_base_published_dir / "old-snapshot"
        old_root.mkdir(parents=True)
        (old_root / "marker.txt").write_text("old legal truth", encoding="utf-8")
        self.paths.knowledge_base_current_dir.symlink_to(
            old_root.relative_to(self.paths.knowledge_base_dir)
        )
        prior_pointer = {"snapshot_id": "old-snapshot"}
        write_json(self.paths.current_publish_pointer_path, prior_pointer)

        with mock.patch(
            "docmason.versioning.append_publish_ledger_record",
            side_effect=OSError("ledger-write-failed"),
        ):
            with self.assertRaisesRegex(OSError, "ledger-write-failed"):
                publish_staging_snapshot(
                    self.paths,
                    validation_report=validation,
                    published_at="2026-07-13T00:00:00Z",
                    rebuild_cause="source-delta",
                    publish_driver="source-delta",
                )

        self.assertEqual(
            self.paths.knowledge_base_current_dir.resolve(),
            old_root.resolve(),
        )
        self.assertEqual(read_json(self.paths.current_publish_pointer_path), prior_pointer)
        self.assertEqual(
            [path.name for path in self.paths.knowledge_base_published_dir.iterdir()],
            ["old-snapshot"],
        )

    def test_cleanup_annotation_failure_does_not_revert_committed_publication(self) -> None:
        staging = self.paths.knowledge_base_staging_dir
        staging.mkdir(parents=True)
        validation = {"status": "valid", "source_signature": "new-signature"}
        for filename in (
            "catalog.json",
            "coverage_manifest.json",
            "graph_edges.json",
            "pending_work.json",
            "hybrid_work.json",
            "publish_manifest.json",
        ):
            write_json(staging / filename, {})
        write_json(staging / "validation_report.json", validation)

        real_write_json = write_json
        publish_manifest_writes = 0

        def fail_only_cleanup_annotation(path: Path, payload: object) -> None:
            nonlocal publish_manifest_writes
            if path.name == "publish_manifest.json":
                publish_manifest_writes += 1
                if publish_manifest_writes == 2:
                    raise OSError("cleanup-annotation-write-failed")
            real_write_json(path, payload)

        with mock.patch(
            "docmason.versioning.write_json",
            side_effect=fail_only_cleanup_annotation,
        ):
            published = publish_staging_snapshot(
                self.paths,
                validation_report=validation,
                published_at="2026-07-13T00:00:00Z",
                rebuild_cause="source-delta",
                publish_driver="source-delta",
            )

        self.assertTrue(self.paths.knowledge_base_current_dir.is_symlink())
        self.assertEqual(
            read_json(self.paths.current_publish_pointer_path)["snapshot_id"],
            published["snapshot_id"],
        )
        self.assertEqual(
            published["publish_cleanup"]["manifest_update"],
            "degraded",
        )


if __name__ == "__main__":
    unittest.main()

"""Interaction overlay and native chat reconciliation tests."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from docmason.ask import prepare_ask_turn
from docmason.commands import sync_workspace
from docmason.coordination import workspace_lease
from docmason.interaction import (
    _persist_interaction_entry,
    build_promoted_interaction_memories,
    decode_data_url,
    interaction_ingest_snapshot,
    reconcile_codex_thread,
)
from docmason.project import WorkspacePaths, read_json, write_json
from docmason.retrieval import retrieve_corpus, trace_source
from docmason.review import refresh_log_review_summary
from docmason.transcript import load_codex_transcript, validate_normalized_transcript


class InteractionIngestAndReviewTests(unittest.TestCase):
    """Cover native chat reconciliation and interaction-derived overlay behavior."""

    def semantic_analysis(
        self,
        *,
        question_class: str,
        question_domain: str,
        route_reason: str | None = None,
        needs_latest_workspace_state: bool = False,
        memory_mode: str | None = None,
        relevant_memory_kinds: list[str] | None = None,
        evidence_requirements: dict[str, object] | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "question_class": question_class,
            "question_domain": question_domain,
            "route_reason": route_reason
            or f"Test analysis classified the question as {question_class}/{question_domain}.",
            "needs_latest_workspace_state": needs_latest_workspace_state,
        }
        if memory_mode is not None or relevant_memory_kinds is not None:
            payload["memory_query_profile"] = {
                "mode": memory_mode or "minimal",
                "relevant_memory_kinds": relevant_memory_kinds or [],
            }
        if evidence_requirements is not None:
            payload["evidence_requirements"] = evidence_requirements
        return payload

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        (root / "skills" / "canonical" / "workspace-bootstrap").mkdir(parents=True)
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
        (root / "skills" / "canonical" / "workspace-bootstrap" / "SKILL.md").write_text(
            "# Workspace Bootstrap\n",
            encoding="utf-8",
        )
        return WorkspacePaths(root=root)

    def mark_environment_ready(self, workspace: WorkspacePaths) -> None:
        workspace.venv_python.parent.mkdir(parents=True, exist_ok=True)
        workspace.venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        write_json(
            workspace.bootstrap_state_path,
            {
                "schema_version": 2,
                "status": "ready",
                "prepared_at": "2026-03-17T00:00:00Z",
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
                "notes_en": "Interaction-ingest test fixture.",
                "notes_source": "Interaction-ingest test fixture.",
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
            title="Campaign Planning Brief",
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
            title="Campaign Evaluation Plan",
            summary="A delivery timeline and companion planning document.",
            key_point="The timeline explains rollout milestones.",
            claim="The timeline complements the architecture strategy.",
        )
        published = sync_workspace(workspace)
        self.assertEqual(published.payload["sync_status"], "valid")
        return source_ids

    def seed_interaction_memory(self, workspace: WorkspacePaths) -> str:
        interaction_manifest = read_json(workspace.interaction_manifest_path("staging"))
        memory = interaction_manifest["memories"][0]
        memory_id = memory["memory_id"]
        memory_dir = workspace.interaction_memories_dir("staging") / memory_id
        source_manifest = read_json(memory_dir / "source_manifest.json")
        evidence_manifest = read_json(memory_dir / "evidence_manifest.json")
        first_unit_id = evidence_manifest["units"][0]["unit_id"]
        knowledge = {
            "source_id": source_manifest["source_id"],
            "source_fingerprint": source_manifest["source_fingerprint"],
            "title": "Interaction Memory for sponsor constraint updates",
            "source_language": "mixed-or-non-en",
            "summary_en": (
                "The interaction memory captures a response-time requirement, concept-style guidance, "
                "and screenshot-backed expectations from a real business follow-up."
            ),
            "summary_source": (
                "The interaction memory captures a response-time requirement, concept-style guidance, "
                "and screenshot-backed expectations from a real business follow-up."
            ),
            "document_type": "interaction",
            "key_points": [
                {
                    "text_en": (
                        "A later user turn added a response-time requirement and a concept-style "
                        "requirement."
                    ),
                    "text_source": (
                        "A later user turn added a response-time requirement and a concept-style "
                        "requirement."
                    ),
                    "citations": [{"unit_id": first_unit_id, "support": "Follow-up user turn"}],
                }
            ],
            "entities": [{"name": source_manifest["conversation_ids"][0], "type": "conversation"}],
            "claims": [
                {
                    "statement_en": (
                        "The promoted memory should remain distinct from source-authored documents."
                    ),
                    "statement_source": (
                        "The promoted memory should remain distinct from source-authored documents."
                    ),
                    "citations": [
                        {"unit_id": first_unit_id, "support": "Interaction memory contract"}
                    ],
                }
            ],
            "known_gaps": [],
            "ambiguities": [],
            "confidence": {
                "level": "medium",
                "notes_en": "Authored during staged interaction-memory synthesis.",
                "notes_source": "Authored during staged interaction-memory synthesis.",
            },
            "citations": [{"unit_id": first_unit_id, "support": "Interaction memory anchor"}],
            "related_sources": [
                {
                    "source_id": related_source_id,
                    "relation_type": "constraint-for",
                    "strength": "medium",
                    "status": "supported",
                    "citation_unit_ids": [first_unit_id],
                }
                for related_source_id in memory["related_source_ids"][:2]
            ],
        }
        write_json(memory_dir / "knowledge.json", knowledge)
        (memory_dir / "summary.md").write_text(
            "\n".join(
                [
                    "# Interaction Memory for sponsor constraint updates",
                    "",
                    f"Source ID: {memory_id}",
                    "",
                    "## English Summary",
                    knowledge["summary_en"],
                    "",
                    "## Source-Language Summary",
                    knowledge["summary_source"],
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return memory_id

    def fake_png_data_url(self) -> str:
        # 1x1 transparent PNG
        raw = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aQ1EAAAAASUVORK5CYII="
        )
        return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")

    def write_fake_codex_storage(
        self,
        workspace: WorkspacePaths,
        *,
        thread_id: str,
        source_ids: list[str],
    ) -> tuple[Path, Path]:
        home = workspace.root / "fake-home"
        state_db = home / ".codex" / "state_5.sqlite"
        sessions_root = home / ".codex" / "sessions" / "2026" / "03" / "17"
        sessions_root.mkdir(parents=True, exist_ok=True)
        state_db.parent.mkdir(parents=True, exist_ok=True)
        rollout_path = sessions_root / f"rollout-2026-03-17T00-00-00-{thread_id}.jsonl"

        with closing(sqlite3.connect(state_db)) as connection:
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, "
                "updated_at INTEGER, source TEXT, model_provider TEXT, cwd TEXT, "
                "title TEXT, sandbox_policy TEXT, approval_mode TEXT, tokens_used INTEGER, "
                "has_user_event INTEGER, archived INTEGER, archived_at INTEGER, "
                "git_sha TEXT, git_branch TEXT, git_origin_url TEXT, cli_version TEXT, "
                "first_user_message TEXT, agent_nickname TEXT, agent_role TEXT, "
                "memory_mode TEXT)"
            )
            connection.execute(
                (
                    "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, "
                    "model_provider, cwd, title, sandbox_policy, approval_mode, tokens_used, "
                    "has_user_event, archived, archived_at, git_sha, git_branch, git_origin_url, "
                    "cli_version, first_user_message, agent_nickname, agent_role, memory_mode) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    thread_id,
                    str(rollout_path),
                    1,
                    2,
                    "vscode",
                    "codex",
                    str(workspace.root),
                    "Real business thread",
                    "danger-full-access",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    "0.0.0-test",
                    "How should the architecture deck change?",
                    None,
                    None,
                    None,
                ),
            )
            connection.commit()

        records = [
            {
                "timestamp": "2026-03-17T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "native-turn-1"},
            },
            {
                "timestamp": "2026-03-17T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "How should I draft the campaign planning brief for the programme lead?",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({"cmd": "docmason doctor --json"}),
                },
            },
            {
                "timestamp": "2026-03-17T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "call_id": "call-2",
                    "arguments": json.dumps(
                        {
                            "cmd": (
                                "sed -n '1,40p' "
                                f"knowledge_base/current/sources/{source_ids[0]}/summary.md"
                            )
                        }
                    ),
                },
            },
            {
                "timestamp": "2026-03-17T00:00:04Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "Start from the operating model, then connect it to "
                                "implementation evidence."
                            ),
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:05Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "native-turn-1"},
            },
            {
                "timestamp": "2026-03-17T00:00:06Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "native-turn-2"},
            },
            {
                "timestamp": "2026-03-17T00:00:07Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "How should I frame the next architecture review response? "
                                "The programme lead added a response-time requirement, and this screenshot "
                                "shows the concept style we must follow."
                            ),
                        },
                        {"type": "input_image", "image_url": self.fake_png_data_url()},
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:08Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                "The follow-up should emphasize the response-time requirement "
                                "and visual concept expectations."
                            ),
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:09Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "native-turn-2"},
            },
        ]
        rollout_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        return state_db, home / ".codex" / "sessions"

    def write_chatter_codex_storage(
        self,
        workspace: WorkspacePaths,
        *,
        thread_id: str,
    ) -> tuple[Path, Path]:
        home = workspace.root / "fake-home-chatter"
        state_db = home / ".codex" / "state_5.sqlite"
        sessions_root = home / ".codex" / "sessions" / "2026" / "03" / "17"
        sessions_root.mkdir(parents=True, exist_ok=True)
        state_db.parent.mkdir(parents=True, exist_ok=True)
        rollout_path = sessions_root / f"rollout-2026-03-17T00-00-00-{thread_id}.jsonl"

        with closing(sqlite3.connect(state_db)) as connection:
            connection.execute(
                "CREATE TABLE threads ("
                "id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER, "
                "updated_at INTEGER, source TEXT, model_provider TEXT, cwd TEXT, "
                "title TEXT, sandbox_policy TEXT, approval_mode TEXT, tokens_used INTEGER, "
                "has_user_event INTEGER, archived INTEGER, archived_at INTEGER, "
                "git_sha TEXT, git_branch TEXT, git_origin_url TEXT, cli_version TEXT, "
                "first_user_message TEXT, agent_nickname TEXT, agent_role TEXT, "
                "memory_mode TEXT)"
            )
            connection.execute(
                (
                    "INSERT INTO threads (id, rollout_path, created_at, updated_at, source, "
                    "model_provider, cwd, title, sandbox_policy, approval_mode, tokens_used, "
                    "has_user_event, archived, archived_at, git_sha, git_branch, git_origin_url, "
                    "cli_version, first_user_message, agent_nickname, agent_role, memory_mode) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                ),
                (
                    thread_id,
                    str(rollout_path),
                    1,
                    2,
                    "vscode",
                    "codex",
                    str(workspace.root),
                    "Chatter thread",
                    "danger-full-access",
                    "never",
                    0,
                    1,
                    0,
                    None,
                    None,
                    None,
                    None,
                    "0.0.0-test",
                    "Does Aliyun SMS support HTTPS API?",
                    None,
                    None,
                    None,
                ),
            )
            connection.commit()

        records = [
            {
                "timestamp": "2026-03-17T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "native-turn-1"},
            },
            {
                "timestamp": "2026-03-17T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Does Aliyun SMS support HTTPS API?",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "I will verify the official documentation first.",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:03Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Yes. Aliyun SMS supports HTTPS API access.",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-03-17T00:00:04Z",
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "native-turn-1"},
            },
        ]
        rollout_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8",
        )
        return state_db, home / ".codex" / "sessions"

    def patch_codex_storage(self, state_db: Path, sessions_root: Path):
        return mock.patch.multiple(
            "docmason.transcript",
            codex_state_db_path=mock.Mock(return_value=state_db),
            codex_sessions_root=mock.Mock(return_value=sessions_root),
        )

    def patch_interaction_storage(self, state_db: Path, sessions_root: Path):
        return mock.patch.multiple(
            "docmason.interaction",
            codex_state_db_path=mock.Mock(return_value=state_db),
            codex_sessions_root=mock.Mock(return_value=sessions_root),
        )

    def seed_prepared_turns(self, workspace: WorkspacePaths, *, thread_id: str) -> None:
        with mock.patch.dict(
            os.environ,
            {"DOCMASON_CONVERSATION_ID": thread_id},
            clear=False,
        ):
            prepare_ask_turn(
                workspace,
                question="How should I draft the campaign planning brief for the programme lead?",
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                ),
            )
            prepare_ask_turn(
                workspace,
                question=(
                    "How should I frame the next architecture review response? "
                    "The programme lead added a response-time requirement, and this screenshot "
                    "shows the concept style we must follow."
                ),
                semantic_analysis=self.semantic_analysis(
                    question_class="composition",
                    question_domain="composition",
                    memory_mode="strong",
                    relevant_memory_kinds=[
                        "constraint",
                        "clarification",
                        "preference",
                        "working-note",
                    ],
                ),
            )

    def test_data_url_decode_and_transcript_validation(self) -> None:
        mime_type, raw = decode_data_url(self.fake_png_data_url())
        self.assertEqual(mime_type, "image/png")
        self.assertTrue(raw)
        payload = {
            "provider": "codex",
            "native_thread_id": "thread-1",
            "turns": [{"user_text": "hello", "attachments": []}],
        }
        validate_normalized_transcript(payload)

    def test_reconcile_native_thread_creates_pending_overlay_and_conversation_turns(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        thread_id = "thread-reconcile"
        state_db, sessions_root = self.write_fake_codex_storage(
            workspace,
            thread_id=thread_id,
            source_ids=source_ids,
        )
        self.seed_prepared_turns(workspace, thread_id=thread_id)

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(
                state_db,
                sessions_root,
            ),
        ):
            transcript = load_codex_transcript(thread_id)
            self.assertEqual(len(transcript["turns"]), 2)
            reconciled = reconcile_codex_thread(workspace, thread_id=thread_id)

        self.assertEqual(reconciled["status"], "reconciled")
        conversation = read_json(workspace.conversations_dir / f"{thread_id}.json")
        self.assertEqual(len(conversation["turns"]), 2)
        self.assertEqual(conversation["turns"][0]["native_turn_id"], "native-turn-1")
        self.assertEqual(conversation["turns"][0]["inner_workflow_id"], "grounded-composition")
        self.assertEqual(conversation["turns"][0]["question_class"], "composition")
        self.assertEqual(conversation["turns"][1]["continuation_type"], "constraint-update")
        self.assertEqual(conversation["turns"][1]["inner_workflow_id"], "grounded-composition")
        self.assertEqual(conversation["turns"][1]["question_class"], "composition")
        self.assertTrue(conversation["turns"][1]["bundle_paths"])
        self.assertTrue(conversation["turns"][1]["attachments"])

        snapshot = interaction_ingest_snapshot(workspace)
        self.assertEqual(snapshot["pending_capture_count"], 2)
        overlay_source_records = read_json(workspace.interaction_overlay_source_records_path)[
            "records"
        ]
        self.assertEqual(len(overlay_source_records), 2)
        self.assertEqual(overlay_source_records[1]["memory_kind"], "constraint")
        self.assertEqual(overlay_source_records[1]["answer_use_policy"], "direct-support")
        self.assertIn("structure", overlay_source_records[1]["available_channels"])
        self.assertIn("render", overlay_source_records[1]["available_channels"])

        overlay_retrieval = retrieve_corpus(
            workspace,
            query="response-time requirement screenshot concept style",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=True,
        )
        self.assertTrue(overlay_retrieval["results"])
        self.assertEqual(overlay_retrieval["results"][0]["source_family"], "interaction-pending")
        self.assertTrue(overlay_retrieval["results"][0]["pending_promotion"])

    def test_reconcile_uses_final_assistant_message_for_canonical_answer(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        thread_id = "thread-chatter"
        state_db, sessions_root = self.write_chatter_codex_storage(
            workspace,
            thread_id=thread_id,
        )

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(state_db, sessions_root),
        ):
            transcript = load_codex_transcript(thread_id)
            self.assertEqual(
                transcript["turns"][0]["assistant_final_text"],
                "Yes. Aliyun SMS supports HTTPS API access.",
            )
            reconcile_codex_thread(workspace, thread_id=thread_id)

        conversation = read_json(workspace.conversations_dir / f"{thread_id}.json")
        turn = conversation["turns"][0]
        answer_path = workspace.root / turn["answer_file_path"]
        self.assertEqual(
            answer_path.read_text(encoding="utf-8").strip(),
            "Yes. Aliyun SMS supports HTTPS API access.",
        )
        self.assertEqual(
            turn["response_excerpt"],
            "Yes. Aliyun SMS supports HTTPS API access.",
        )
        entries = sorted(workspace.interaction_entries_dir.glob("*.json"))
        self.assertEqual(len(entries), 1)
        entry = read_json(entries[0])
        self.assertEqual(entry["question_class"], "answer")
        self.assertEqual(entry["question_domain"], "general-stable")
        self.assertEqual(entry["support_strategy"], "model-first")
        self.assertEqual(entry["analysis_origin"], "repair-backstop")
        self.assertIn("semantic_analysis", entry)
        overlay_source_records = read_json(workspace.interaction_overlay_source_records_path)[
            "records"
        ]
        self.assertEqual(overlay_source_records[0]["question_class"], "answer")
        self.assertEqual(overlay_source_records[0]["support_strategy"], "model-first")

    def test_reconcile_repairs_legacy_routing_metadata(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        thread_id = "thread-repair"
        state_db, sessions_root = self.write_fake_codex_storage(
            workspace,
            thread_id=thread_id,
            source_ids=source_ids,
        )
        self.seed_prepared_turns(workspace, thread_id=thread_id)

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(state_db, sessions_root),
        ):
            reconcile_codex_thread(workspace, thread_id=thread_id)

        legacy_conversation = read_json(workspace.conversations_dir / f"{thread_id}.json")
        legacy_conversation["turns"][1]["inner_workflow_id"] = "grounded-answer"
        legacy_conversation["turns"][1]["question_class"] = None
        legacy_conversation["turns"][1]["evidence_mode"] = None
        legacy_conversation["turns"][1]["research_depth"] = None
        legacy_conversation["turns"][1]["bundle_paths"] = []
        write_json(workspace.conversations_dir / f"{thread_id}.json", legacy_conversation)

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(
                state_db,
                sessions_root,
            ),
        ):
            repaired = reconcile_codex_thread(workspace, thread_id=thread_id)

        self.assertEqual(repaired["status"], "reconciled")
        repaired_conversation = read_json(workspace.conversations_dir / f"{thread_id}.json")
        self.assertEqual(
            repaired_conversation["turns"][1]["inner_workflow_id"],
            "grounded-composition",
        )
        self.assertEqual(repaired_conversation["turns"][1]["question_class"], "composition")
        self.assertEqual(repaired_conversation["turns"][1]["evidence_mode"], "kb-first-escalation")
        self.assertEqual(repaired_conversation["turns"][1]["research_depth"], "deep")
        self.assertTrue(repaired_conversation["turns"][1]["bundle_paths"])

    def test_prepare_ask_turn_recommends_sync_when_pending_interaction_is_relevant(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        thread_id = "thread-ask-overlay"
        state_db, sessions_root = self.write_fake_codex_storage(
            workspace,
            thread_id=thread_id,
            source_ids=source_ids,
        )

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(
                state_db,
                sessions_root,
            ),
            mock.patch.dict(os.environ, {"CODEX_THREAD_ID": thread_id}, clear=False),
        ):
            turn = prepare_ask_turn(
                workspace,
                question="How should I handle the response-time requirement from the screenshot?",
                semantic_analysis=self.semantic_analysis(
                    question_class="answer",
                    question_domain="workspace-corpus",
                    memory_mode="strong",
                    relevant_memory_kinds=[
                        "constraint",
                        "clarification",
                        "preference",
                        "working-note",
                    ],
                ),
            )

        self.assertTrue(turn["auto_sync_triggered"])
        self.assertEqual(turn["auto_sync_summary"]["status"], "valid")
        self.assertEqual(turn["auto_sync_reason"], "Relevant pending interaction-derived knowledge still awaits sync-time promotion.")

    def test_sync_promotes_pending_interactions_into_current_kb_and_trace(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        thread_id = "thread-promote"
        state_db, sessions_root = self.write_fake_codex_storage(
            workspace,
            thread_id=thread_id,
            source_ids=source_ids,
        )

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(
                state_db,
                sessions_root,
            ),
        ):
            reconcile_codex_thread(workspace, thread_id=thread_id)

        first_sync = sync_workspace(workspace, autonomous=False)
        self.assertEqual(first_sync.payload["sync_status"], "pending-synthesis")
        pending_kinds = {item.get("kind") for item in first_sync.payload["pending_sources"]}
        self.assertIn("interaction-memory", pending_kinds)
        self.seed_interaction_memory(workspace)
        result = sync_workspace(workspace)
        self.assertEqual(result.payload["sync_status"], "valid")
        self.assertEqual(result.payload["interaction_ingest"]["promoted_memory_count"], 1)
        current_manifest = read_json(workspace.interaction_manifest_path("current"))
        self.assertEqual(current_manifest["memory_count"], 1)
        memory_id = current_manifest["memories"][0]["memory_id"]
        self.assertTrue(
            (
                workspace.interaction_memories_dir("current")
                / memory_id
                / "derived_affordances.json"
            ).exists()
        )

        retrieval = retrieve_corpus(
            workspace,
            query="response-time requirement screenshot concept style",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=True,
        )
        self.assertTrue(retrieval["results"])
        self.assertEqual(retrieval["results"][0]["source_family"], "interaction-memory")
        self.assertFalse(retrieval["results"][0]["pending_promotion"])
        self.assertEqual(retrieval["results"][0]["memory_kind"], "constraint")

        traced = trace_source(workspace, source_id=memory_id, unit_id=None)
        self.assertEqual(traced["source"]["source_family"], "interaction-memory")

        snapshot = interaction_ingest_snapshot(workspace)
        self.assertEqual(snapshot["pending_promotion_count"], 0)

        corpus_first = retrieve_corpus(
            workspace,
            query="architecture strategy operating model",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertTrue(corpus_first["results"])
        self.assertEqual(corpus_first["results"][0]["source_family"], "corpus")

    def test_legacy_interaction_memories_get_semantic_defaults_backfilled(self) -> None:
        workspace = self.make_workspace()
        self.mark_environment_ready(workspace)
        self.create_pdf(workspace.source_dir / "a.pdf")
        self.create_pdf(workspace.source_dir / "b.pdf")
        source_ids = self.publish_seeded_corpus(workspace)
        thread_id = "thread-legacy-memory"
        state_db, sessions_root = self.write_fake_codex_storage(
            workspace,
            thread_id=thread_id,
            source_ids=source_ids,
        )

        with (
            self.patch_codex_storage(state_db, sessions_root),
            self.patch_interaction_storage(
                state_db,
                sessions_root,
            ),
        ):
            reconcile_codex_thread(workspace, thread_id=thread_id)

        first_sync = sync_workspace(workspace, autonomous=False)
        self.assertEqual(first_sync.payload["sync_status"], "pending-synthesis")
        memory_id = self.seed_interaction_memory(workspace)
        result = sync_workspace(workspace)
        self.assertEqual(result.payload["sync_status"], "valid")

        current_dir = workspace.interaction_memories_dir("current") / memory_id
        interaction_context = read_json(current_dir / "interaction_context.json")
        interaction_context.pop("semantics", None)
        write_json(current_dir / "interaction_context.json", interaction_context)

        source_manifest = read_json(current_dir / "source_manifest.json")
        for field_name in (
            "memory_kind",
            "durability",
            "uncertainty",
            "answer_use_policy",
            "retrieval_rank_prior",
        ):
            source_manifest.pop(field_name, None)
        write_json(current_dir / "source_manifest.json", source_manifest)

        knowledge = read_json(current_dir / "knowledge.json")
        for field_name in (
            "memory_kind",
            "durability",
            "uncertainty",
            "answer_use_policy",
            "retrieval_rank_prior",
        ):
            knowledge.pop(field_name, None)
        write_json(current_dir / "knowledge.json", knowledge)

        retrieval_source_records = read_json(workspace.retrieval_source_records_path("current"))
        for record in retrieval_source_records.get("records", []):
            if isinstance(record, dict) and record.get("source_id") == memory_id:
                for field_name in (
                    "memory_kind",
                    "durability",
                    "uncertainty",
                    "answer_use_policy",
                    "retrieval_rank_prior",
                ):
                    record.pop(field_name, None)
        write_json(workspace.retrieval_source_records_path("current"), retrieval_source_records)

        retrieval_unit_records = read_json(workspace.retrieval_unit_records_path("current"))
        for record in retrieval_unit_records.get("records", []):
            if isinstance(record, dict) and record.get("source_id") == memory_id:
                for field_name in (
                    "memory_kind",
                    "durability",
                    "uncertainty",
                    "answer_use_policy",
                    "retrieval_rank_prior",
                ):
                    record.pop(field_name, None)
        write_json(workspace.retrieval_unit_records_path("current"), retrieval_unit_records)

        trace_source_provenance = read_json(workspace.trace_source_provenance_path("current"))
        if isinstance(trace_source_provenance.get(memory_id), dict):
            for field_name in (
                "memory_kind",
                "durability",
                "uncertainty",
                "answer_use_policy",
                "retrieval_rank_prior",
            ):
                trace_source_provenance[memory_id].pop(field_name, None)
        write_json(workspace.trace_source_provenance_path("current"), trace_source_provenance)

        loaded_contexts = retrieve_corpus(
            workspace,
            query="response-time requirement concept style",
            top=3,
            graph_hops=1,
            document_types=None,
            source_ids=None,
            include_renders=False,
        )
        self.assertTrue(loaded_contexts["results"])
        self.assertEqual(loaded_contexts["results"][0]["memory_kind"], "constraint")
        self.assertEqual(loaded_contexts["results"][0]["answer_use_policy"], "direct-support")
        traced = trace_source(workspace, source_id=memory_id, unit_id=None)
        self.assertEqual(traced["source"]["memory_kind"], "constraint")

        rebuilt_manifest = build_promoted_interaction_memories(workspace, target="staging")
        self.assertEqual(rebuilt_manifest["memory_count"], 1)
        rebuilt_dir = workspace.interaction_memories_dir("staging") / memory_id
        rebuilt_context = read_json(rebuilt_dir / "interaction_context.json")
        rebuilt_source_manifest = read_json(rebuilt_dir / "source_manifest.json")
        rebuilt_work_item = read_json(rebuilt_dir / "work_item.json")
        rebuilt_knowledge = read_json(rebuilt_dir / "knowledge.json")
        self.assertEqual(rebuilt_context["semantics"]["memory_kind"], "constraint")
        self.assertEqual(rebuilt_source_manifest["memory_kind"], "constraint")
        self.assertEqual(rebuilt_work_item["semantic_hints"]["memory_kind"], "constraint")
        self.assertEqual(rebuilt_knowledge["memory_kind"], "constraint")

    def test_review_summary_demotes_evaluation_suite_traffic(self) -> None:
        workspace = self.make_workspace()
        workspace.query_sessions_dir.mkdir(parents=True, exist_ok=True)
        workspace.retrieval_traces_dir.mkdir(parents=True, exist_ok=True)

        write_json(
            workspace.query_sessions_dir / "real.json",
            {
                "recorded_at": "2026-03-17T00:00:00Z",
                "command": "retrieve",
                "status": "no-results",
                "query": "real failure",
                "session_id": "real-session",
                "log_origin": "interactive-ask",
            },
        )
        write_json(
            workspace.query_sessions_dir / "synthetic.json",
            {
                "recorded_at": "2026-03-17T00:00:01Z",
                "command": "trace",
                "status": "degraded",
                "query": "synthetic failure",
                "session_id": "synthetic-session",
                "log_origin": "evaluation-suite",
                "final_answer": "synthetic",
            },
        )
        summary = refresh_log_review_summary(workspace)
        self.assertEqual(summary["query_sessions"]["real_total"], 1)
        self.assertEqual(summary["query_sessions"]["synthetic_total"], 1)
        self.assertEqual(summary["query_sessions"]["recent"][0]["session_id"], "real-session")
        self.assertEqual(
            summary["query_sessions"]["synthetic_recent"][0]["session_id"],
            "synthetic-session",
        )

    def test_reconcile_codex_thread_waits_for_conversation_lease(self) -> None:
        workspace = self.make_workspace()
        result: dict[str, object] = {}
        finished = threading.Event()
        transcript = {
            "cwd": str(workspace.root),
            "rollout_path": "runtime/rollouts/test",
            "turns": [
                {
                    "native_turn_id": "native-turn-1",
                    "opened_at": "2026-03-17T00:00:00Z",
                    "completed_at": "2026-03-17T00:01:00Z",
                    "user_text": "Summarize the proposal.",
                    "assistant_final_text": "Summary ready.",
                    "attachments": [],
                    "function_calls": [],
                }
            ],
        }
        profile = {
            "question_class": "direct",
            "question_domain": "workspace-corpus",
            "inner_workflow_id": "grounded-answer",
            "support_strategy": "kb-first",
            "analysis_origin": "test",
            "evidence_requirements": {
                "inspection_scope": "kb",
                "preferred_channels": ["text"],
            },
            "semantic_analysis": self.semantic_analysis(
                question_class="direct",
                question_domain="workspace-corpus",
            ),
            "evidence_mode": "kb-native",
            "research_depth": "standard",
            "bundle_paths": [],
        }

        def run_reconcile() -> None:
            result["payload"] = reconcile_codex_thread(workspace, thread_id="thread-123")
            finished.set()

        with workspace_lease(workspace, "conversation:thread-123", timeout_seconds=1.0):
            with (
                mock.patch(
                    "docmason.interaction.refresh_generated_connector_manifests",
                    return_value=None,
                ),
                mock.patch("docmason.interaction.load_codex_transcript", return_value=transcript),
                mock.patch(
                    "docmason.interaction.question_execution_profile",
                    return_value=profile,
                ),
                mock.patch(
                    "docmason.interaction.build_tool_use_audit",
                    return_value={
                        "consulted_source_ids": [],
                        "docmason_commands": [],
                        "direct_knowledge_base_access": False,
                        "direct_original_doc_access": False,
                        "render_inspection_used": False,
                    },
                ),
                mock.patch(
                    "docmason.interaction.ensure_run_for_turn",
                    return_value={"run_id": "run-123"},
                ),
                mock.patch(
                    "docmason.interaction.refresh_runtime_projections",
                    return_value={},
                ),
            ):
                thread = threading.Thread(target=run_reconcile)
                thread.start()
                time.sleep(0.2)
                self.assertFalse(
                    finished.is_set(),
                    "Reconciliation should wait while the conversation lease is active.",
                )
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        payload = result["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["status"], "reconciled")

    def test_reconcile_does_not_reopen_already_promoted_interaction_entries(self) -> None:
        workspace = self.make_workspace()
        entry = _persist_interaction_entry(
            workspace,
            conversation_id="thread-123",
            turn_id="turn-001",
            native_turn_id="native-turn-1",
            recorded_at="2026-03-17T00:00:00Z",
            user_text="Summarize the proposal.",
            assistant_excerpt="Summary ready.",
            attachment_refs=[],
            continuation_type=None,
            related_source_ids=[],
            tool_use_audit={},
            question_class="direct",
            question_domain="workspace-corpus",
            support_strategy="kb-first",
            analysis_origin="test",
            semantic_analysis=self.semantic_analysis(
                question_class="direct",
                question_domain="workspace-corpus",
            ),
        )
        entry_path = (
            workspace.interaction_entries_dir / f"{entry['interaction_id']}.json"
        )
        persisted = read_json(entry_path)
        persisted["pending_promotion"] = False
        persisted["status"] = "promoted"
        persisted["promoted_memory_id"] = "interaction-memory-123"
        persisted["promoted_at"] = "2026-03-17T00:10:00Z"
        write_json(entry_path, persisted)

        refreshed = _persist_interaction_entry(
            workspace,
            conversation_id="thread-123",
            turn_id="turn-001",
            native_turn_id="native-turn-1",
            recorded_at="2026-03-17T00:00:00Z",
            user_text="Summarize the proposal.",
            assistant_excerpt="Summary ready.",
            attachment_refs=[],
            continuation_type=None,
            related_source_ids=[],
            tool_use_audit={},
            question_class="direct",
            question_domain="workspace-corpus",
            support_strategy="kb-first",
            analysis_origin="test",
            semantic_analysis=self.semantic_analysis(
                question_class="direct",
                question_domain="workspace-corpus",
            ),
        )

        self.assertFalse(refreshed["pending_promotion"])
        self.assertEqual(refreshed["status"], "promoted")
        self.assertEqual(refreshed["promoted_memory_id"], "interaction-memory-123")
        self.assertEqual(refreshed["promoted_at"], "2026-03-17T00:10:00Z")


if __name__ == "__main__":
    unittest.main()

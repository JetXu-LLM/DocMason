"""Claude Code compatibility, transcript, and hook tests.

Covers:
- Hook event handler (hooks.py)
- Claude Code transcript reader (transcript.py)
- Claude Code thread reconciliation (interaction.py)
- Provider-agnostic dispatch (interaction.py)
- Doctor checklist for hook configuration (commands.py)
- CLI _hook subcommand (cli.py)
- Committed .claude/ bootstrapper files
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from docmason.cli import build_parser
from docmason.cli import main as docmason_main
from docmason.commands import READY, doctor_workspace
from docmason.conversation import detect_agent_surface
from docmason.hooks import (
    SUPPORTED_EVENTS,
    _mirror_path,
    handle_hook_event,
    run_hook_cli,
)
from docmason.project import WorkspacePaths, read_json
from docmason.transcript import (
    SUPPORTED_PROVIDERS,
    iter_jsonl,
    load_claude_code_transcript,
    locate_claude_code_session,
    validate_normalized_transcript,
)
from tests.support_ready_workspace import seed_self_contained_bootstrap_state

ROOT = Path(__file__).resolve().parents[1]


class HookEventHandlerTests(unittest.TestCase):
    """Test the Claude Code hook event handler in hooks.py."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace_root = Path(self.tempdir.name)

    def _seed_skill_workspace(self) -> WorkspacePaths:
        workspace = WorkspacePaths(root=self.workspace_root)
        shutil.copytree(ROOT / "skills" / "canonical", workspace.root / "skills" / "canonical")
        (workspace.root / "skills" / "operator").mkdir(parents=True, exist_ok=True)
        return workspace

    def test_session_start_writes_mirror_record(self) -> None:
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "test-session-001",
            "cwd": str(self.workspace_root),
            "transcript_path": "/tmp/transcript.jsonl",
            "model": "claude-sonnet-4.6",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        self.assertTrue(mirror_file.exists(), "Mirror file should be created")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "session-start")
        self.assertEqual(records[0]["session_id"], "test-session-001")
        self.assertEqual(records[0]["model"], "claude-sonnet-4.6")
        self.assertEqual(records[0]["transcript_path"], "/tmp/transcript.jsonl")

    def test_hook_records_use_atomic_jsonl_append_helper(self) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test-session-atomic",
            "prompt": "What changed?",
            "permission_mode": "default",
        }
        with (
            mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root),
            mock.patch("docmason.hooks.append_jsonl") as append_jsonl,
        ):
            handle_hook_event("prompt-submit", json.dumps(payload))

        append_jsonl.assert_called_once()

    def test_session_end_writes_mirror_record(self) -> None:
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "test-session-001",
            "reason": "user_exit",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "session-end")
        self.assertEqual(records[0]["reason"], "user_exit")

    def test_session_end_preserves_optional_diagnostics(self) -> None:
        payload = {
            "hook_event_name": "SessionEnd",
            "session_id": "test-session-001",
            "reason": "other",
            "host_error_text": "Context budget exceeded",
            "hook_activity_state": "teardown-running",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(records[0]["host_error_text"], "Context budget exceeded")
        self.assertEqual(records[0]["hook_activity_state"], "teardown-running")

    def test_prompt_submit_writes_mirror_record(self) -> None:
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "test-session-001",
            "prompt": "What is DocMason?",
            "permission_mode": "default",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("prompt-submit", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "prompt-submit")
        self.assertEqual(records[0]["prompt"], "What is DocMason?")

    def test_post_tool_use_writes_mirror_record(self) -> None:
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": "test-session-001",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"output": "file.txt"},
            "tool_use_id": "tool-123",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("post-tool-use", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "tool-use")
        self.assertEqual(records[0]["tool_name"], "Bash")

    def test_stop_writes_mirror_record(self) -> None:
        payload = {
            "hook_event_name": "Stop",
            "session_id": "test-session-001",
            "last_assistant_message": "Here is the answer.",
            "stop_hook_active": True,
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("stop", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_type"], "stop")
        self.assertEqual(records[0]["last_assistant_message"], "Here is the answer.")

    def test_stop_preserves_optional_diagnostics(self) -> None:
        payload = {
            "hook_event_name": "Stop",
            "session_id": "test-session-001",
            "last_assistant_message": "Execution failed cleanly.",
            "stop_reason": "sdk-error",
            "host_error_text": "Cannot read properties of undefined.",
            "hook_activity_state": "stop-hook",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("stop", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-001")
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(records[0]["stop_reason"], "sdk-error")
        self.assertEqual(
            records[0]["host_error_text"],
            "Cannot read properties of undefined.",
        )
        self.assertEqual(records[0]["hook_activity_state"], "stop-hook")

    def test_missing_session_id_silently_skipped(self) -> None:
        payload = {
            "hook_event_name": "SessionStart",
            # no session_id
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(payload))

        mirror_root = self.workspace_root / "runtime" / "interaction-ingest" / "claude-code"
        self.assertFalse(
            mirror_root.exists(), "No mirror file should be created without session_id"
        )

    def test_invalid_json_silently_handled(self) -> None:
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", "this is not json")
        # No exception should be raised

    def test_empty_stdin_silently_handled(self) -> None:
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", "")
        # No exception should be raised

    def test_cli_fallback_when_hook_event_name_missing(self) -> None:
        """When payload lacks hook_event_name, falls back to CLI event-name mapping."""
        payload = {
            "session_id": "test-session-002",
            "prompt": "Hello",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("prompt-submit", json.dumps(payload))

        mirror_file = _mirror_path(self.workspace_root, "test-session-002")
        self.assertTrue(mirror_file.exists())
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(records[0]["record_type"], "prompt-submit")

    def test_multiple_events_append_to_same_file(self) -> None:
        sid = "multi-event-session"
        start = {
            "hook_event_name": "SessionStart",
            "session_id": sid,
            "cwd": str(self.workspace_root),
        }
        prompt = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": sid,
            "prompt": "Hello",
        }
        stop = {
            "hook_event_name": "Stop",
            "session_id": sid,
            "last_assistant_message": "Done",
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(start))
            handle_hook_event("prompt-submit", json.dumps(prompt))
            handle_hook_event("stop", json.dumps(stop))

        mirror_file = _mirror_path(self.workspace_root, sid)
        records = [json.loads(line) for line in mirror_file.read_text().splitlines()]
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["record_type"], "session-start")
        self.assertEqual(records[1]["record_type"], "prompt-submit")
        self.assertEqual(records[2]["record_type"], "stop")

    def test_session_start_refreshes_skill_shims_for_self_contained_workspace(self) -> None:
        workspace = self._seed_skill_workspace()
        seed_self_contained_bootstrap_state(workspace)

        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "shim-refresh-session",
            "cwd": str(self.workspace_root),
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(payload))

        claude_shim = workspace.claude_skill_shim_dir / "workspace-bootstrap"
        codex_shim = workspace.repo_skill_shim_dir / "workspace-bootstrap"
        self.assertTrue(claude_shim.is_symlink())
        self.assertTrue(codex_shim.is_symlink())
        expected = (workspace.canonical_skills_dir / "workspace-bootstrap").resolve()
        self.assertEqual(claude_shim.resolve(), expected)
        self.assertEqual(codex_shim.resolve(), expected)

    def test_session_start_skips_skill_shims_when_workspace_not_self_contained(self) -> None:
        workspace = self._seed_skill_workspace()
        workspace.bootstrap_state_path.parent.mkdir(parents=True, exist_ok=True)
        workspace.bootstrap_state_path.write_text(
            json.dumps(
                {
                    "schema_version": 4,
                    "status": "action-required",
                    "environment_ready": False,
                    "workspace_runtime_ready": False,
                    "machine_baseline_ready": True,
                    "machine_baseline_status": "not-applicable",
                    "bootstrap_source": "shared-python",
                    "workspace_root": str(workspace.root.resolve()),
                    "isolation_grade": "degraded",
                    "host_access_required": False,
                }
            ),
            encoding="utf-8",
        )

        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "shim-skip-session",
            "cwd": str(self.workspace_root),
        }
        with mock.patch("docmason.hooks._resolve_workspace_root", return_value=self.workspace_root):
            handle_hook_event("session", json.dumps(payload))

        self.assertFalse(workspace.claude_skill_shim_dir.exists())
        self.assertFalse(workspace.repo_skill_shim_dir.exists())


class SupportedEventsTests(unittest.TestCase):
    """Test SUPPORTED_EVENTS constant."""

    def test_supported_events_complete(self) -> None:
        expected = {"session", "prompt-submit", "post-tool-use", "stop"}
        self.assertEqual(SUPPORTED_EVENTS, expected)


class RunHookCliTests(unittest.TestCase):
    """Test the run_hook_cli entry point."""

    def test_unsupported_event_returns_zero(self) -> None:
        self.assertEqual(run_hook_cli("unknown-event"), 0)

    def test_returns_zero_for_supported_event(self) -> None:
        with mock.patch("docmason.hooks.handle_hook_event"):
            with mock.patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                result = run_hook_cli("session")
        self.assertEqual(result, 0)


class ClaudeCodeTranscriptReaderTests(unittest.TestCase):
    """Test the Claude Code transcript reader in transcript.py."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace_root = Path(self.tempdir.name)

    def _write_mirror_session(self, session_id: str, records: list[dict]) -> Path:
        mirror_dir = self.workspace_root / "runtime" / "interaction-ingest" / "claude-code"
        mirror_dir.mkdir(parents=True, exist_ok=True)
        path = mirror_dir / f"{session_id}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        return path

    def test_iter_jsonl_skips_malformed_lines(self) -> None:
        path = self.workspace_root / "broken.jsonl"
        path.write_text(
            '{"record_type":"good-1"}\nnot json\n["skip-me"]\n{"record_type":"good-2"}\n',
            encoding="utf-8",
        )

        records = iter_jsonl(path)

        self.assertEqual(
            records,
            [
                {"record_type": "good-1"},
                {"record_type": "good-2"},
            ],
        )

    def _write_native_transcript(self, filename: str, records: list[dict]) -> Path:
        path = self.workspace_root / filename
        with path.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record) + "\n")
        return path

    def test_locate_existing_session(self) -> None:
        self._write_mirror_session(
            "abc-123", [{"record_type": "session-start", "session_id": "abc-123"}]
        )
        result = locate_claude_code_session("abc-123", self.workspace_root)
        self.assertIsNotNone(result)

    def test_locate_missing_session(self) -> None:
        result = locate_claude_code_session("nonexistent", self.workspace_root)
        self.assertIsNone(result)

    def test_load_minimal_transcript(self) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-001",
                "cwd": str(self.workspace_root),
                "transcript_path": "",
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-001",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "What is this project?",
            },
            {
                "record_type": "stop",
                "session_id": "sess-001",
                "recorded_at": "2026-03-20T10:01:00Z",
                "last_assistant_message": "This is DocMason.",
            },
        ]
        self._write_mirror_session("sess-001", records)
        transcript = load_claude_code_transcript("sess-001", self.workspace_root)

        self.assertEqual(transcript["provider"], "claude-code")
        self.assertEqual(transcript["native_thread_id"], "sess-001")
        self.assertEqual(len(transcript["turns"]), 1)
        turn = transcript["turns"][0]
        self.assertEqual(turn["user_text"], "What is this project?")
        self.assertEqual(turn["assistant_final_text"], "This is DocMason.")
        self.assertEqual(turn["native_turn_id"], "turn-001")
        self.assertEqual(transcript["fidelity"]["capability_scope"], "captured-transcript")
        self.assertFalse(transcript["fidelity"]["attachments_captured"])
        self.assertEqual(transcript["fidelity"]["capture_method"], "hook-mirror")

    def test_load_multi_turn_transcript(self) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-002",
                "cwd": "",
                "transcript_path": "",
                "model": "",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-002",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "First question",
            },
            {
                "record_type": "stop",
                "session_id": "sess-002",
                "recorded_at": "2026-03-20T10:01:00Z",
                "last_assistant_message": "First answer",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-002",
                "recorded_at": "2026-03-20T10:02:00Z",
                "prompt": "Second question",
            },
            {
                "record_type": "stop",
                "session_id": "sess-002",
                "recorded_at": "2026-03-20T10:03:00Z",
                "last_assistant_message": "Second answer",
            },
        ]
        self._write_mirror_session("sess-002", records)
        transcript = load_claude_code_transcript("sess-002", self.workspace_root)

        self.assertEqual(len(transcript["turns"]), 2)
        self.assertEqual(transcript["turns"][0]["user_text"], "First question")
        self.assertEqual(transcript["turns"][1]["user_text"], "Second question")
        self.assertEqual(transcript["turns"][0]["native_turn_id"], "turn-001")
        self.assertEqual(transcript["turns"][1]["native_turn_id"], "turn-002")

    def test_missing_mirror_file_raises_file_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            load_claude_code_transcript("missing-session", self.workspace_root)

    def test_tool_use_records_captured(self) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-003",
                "cwd": "",
                "transcript_path": "",
                "model": "",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-003",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Run ls",
            },
            {
                "record_type": "tool-use",
                "session_id": "sess-003",
                "recorded_at": "2026-03-20T10:00:30Z",
                "tool_name": "Bash",
                "tool_use_id": "t-1",
                "tool_input": {"command": "ls"},
                "tool_response": "file.txt",
            },
            {
                "record_type": "stop",
                "session_id": "sess-003",
                "recorded_at": "2026-03-20T10:01:00Z",
                "last_assistant_message": "Done",
            },
        ]
        self._write_mirror_session("sess-003", records)
        transcript = load_claude_code_transcript("sess-003", self.workspace_root)

        turn = transcript["turns"][0]
        self.assertEqual(len(turn["function_calls"]), 1)
        self.assertEqual(turn["function_calls"][0]["tool_name"], "Bash")

    def test_native_transcript_enrichment_merges_assistant_and_tool_context(self) -> None:
        native_path = self._write_native_transcript(
            "native-claude.jsonl",
            [
                {
                    "type": "user",
                    "timestamp": "2026-03-20T10:00:00Z",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Run the ask workflow"},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-03-20T10:00:05Z",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Opening canonical ask turn."},
                            {
                                "type": "tool_use",
                                "name": "Skill",
                                "id": "skill-001",
                                "input": {"skill": "ask"},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-03-20T10:00:06Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "skill-001",
                                "content": "Launching skill: ask",
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-03-20T10:00:10Z",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [
                            {"type": "text", "text": "Final grounded answer."},
                        ]
                    },
                },
            ],
        )
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-004",
                "cwd": str(self.workspace_root),
                "transcript_path": str(native_path),
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-004",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Run the ask workflow",
            },
            {
                "record_type": "tool-use",
                "session_id": "sess-004",
                "recorded_at": "2026-03-20T10:00:07Z",
                "tool_name": "Bash",
                "tool_use_id": "bash-001",
                "tool_input": {"command": "docmason status --json"},
                "tool_response": "ready",
            },
            {
                "record_type": "stop",
                "session_id": "sess-004",
                "recorded_at": "2026-03-20T10:00:12Z",
                "last_assistant_message": "Final grounded answer.",
            },
        ]
        self._write_mirror_session("sess-004", records)

        transcript = load_claude_code_transcript("sess-004", self.workspace_root)

        turn = transcript["turns"][0]
        self.assertEqual(transcript["fidelity"]["capture_method"], "hook-mirror-plus-native")
        self.assertTrue(transcript["fidelity"]["has_mid_turn_messages"])
        self.assertEqual(turn["assistant_message_count"], 2)
        self.assertIn("Opening canonical ask turn.", turn["assistant_text"])
        tool_names = {call["tool_name"] for call in turn["function_calls"]}
        self.assertIn("Bash", tool_names)
        self.assertIn("Skill", tool_names)
        self.assertTrue(
            any(
                output.get("call_id") == "skill-001"
                for output in turn["function_call_outputs"]
            )
        )

    def test_native_transcript_meta_skill_text_and_nested_image_keep_one_turn(self) -> None:
        image_payload = base64.b64encode(b"fake-png-bytes").decode("ascii")
        native_path = self._write_native_transcript(
            "native-claude-realistic.jsonl",
            [
                {
                    "type": "user",
                    "timestamp": "2026-03-20T10:00:00Z",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Please inspect the rendered page."},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-03-20T10:00:02Z",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Opening canonical ask turn."},
                            {
                                "type": "tool_use",
                                "name": "Skill",
                                "id": "skill-001",
                                "input": {"skill": "ask"},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-03-20T10:00:03Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "skill-001",
                                "content": "Launching skill: ask",
                            }
                        ]
                    },
                    "toolUseResult": {"success": True, "commandName": "ask"},
                },
                {
                    "type": "user",
                    "timestamp": "2026-03-20T10:00:03Z",
                    "isMeta": True,
                    "sourceToolUseID": "skill-001",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "Base directory for this skill: /tmp/skills/ask",
                            }
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-03-20T10:00:05Z",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Reading the rendered evidence now."},
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "id": "read-001",
                                "input": {"file_path": "renders/page-001.png"},
                            },
                        ]
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2026-03-20T10:00:06Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "read-001",
                                "content": [
                                    {"type": "text", "text": "Loaded page render."},
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/png",
                                            "data": image_payload,
                                        },
                                    },
                                ],
                            }
                        ]
                    },
                    "toolUseResult": {"success": True, "commandName": "Read"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-03-20T10:00:10Z",
                    "message": {
                        "stop_reason": "end_turn",
                        "content": [
                            {"type": "text", "text": "Final grounded answer."},
                        ],
                    },
                },
            ],
        )
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-004b",
                "cwd": str(self.workspace_root),
                "transcript_path": str(native_path),
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-004b",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Please inspect the rendered page.",
            },
            {
                "record_type": "tool-use",
                "session_id": "sess-004b",
                "recorded_at": "2026-03-20T10:00:07Z",
                "tool_name": "Bash",
                "tool_use_id": "bash-001",
                "tool_input": {"command": "docmason status --json"},
                "tool_response": "ready",
            },
            {
                "record_type": "stop",
                "session_id": "sess-004b",
                "recorded_at": "2026-03-20T10:00:12Z",
                "last_assistant_message": "Final grounded answer.",
            },
        ]
        self._write_mirror_session("sess-004b", records)

        transcript = load_claude_code_transcript("sess-004b", self.workspace_root)

        self.assertEqual(len(transcript["turns"]), 1)
        self.assertTrue(transcript["fidelity"]["has_mid_turn_messages"])
        self.assertTrue(transcript["fidelity"]["attachments_captured"])
        turn = transcript["turns"][0]
        self.assertEqual(turn["assistant_message_count"], 3)
        self.assertIn("Reading the rendered evidence now.", turn["assistant_text"])
        self.assertEqual(len(turn["attachments"]), 1)
        self.assertTrue(turn["attachments"][0]["image_url"].startswith("data:image/png;base64,"))
        tool_names = {call["tool_name"] for call in turn["function_calls"]}
        self.assertIn("Read", tool_names)
        self.assertIn("Skill", tool_names)
        self.assertTrue(
            any(
                output.get("call_id") == "read-001"
                for output in turn["function_call_outputs"]
            )
        )

    def test_session_end_without_stop_marks_incomplete_operator_evidence(self) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-005",
                "cwd": str(self.workspace_root),
                "transcript_path": "",
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-005",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Please continue the ask workflow.",
            },
            {
                "record_type": "session-end",
                "session_id": "sess-005",
                "recorded_at": "2026-03-20T10:00:40Z",
                "reason": "other",
                "host_error_text": "Context budget exceeded before completion.",
            },
        ]
        self._write_mirror_session("sess-005", records)

        transcript = load_claude_code_transcript("sess-005", self.workspace_root)

        turn = transcript["turns"][0]
        self.assertEqual(turn["closure"]["status"], "incomplete")
        self.assertEqual(
            turn["operator_evidence"]["classification"],
            "host-runtime-overload",
        )

    def test_stop_text_about_sdk_docs_does_not_trigger_host_failure(self) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-006",
                "cwd": str(self.workspace_root),
                "transcript_path": "",
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-006",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Explain the Claude workflow.",
            },
            {
                "record_type": "stop",
                "session_id": "sess-006",
                "recorded_at": "2026-03-20T10:00:40Z",
                "last_assistant_message": (
                    "The SDK documentation recommends sequential image reads."
                ),
                "stop_reason": "end_turn",
            },
        ]
        self._write_mirror_session("sess-006", records)

        transcript = load_claude_code_transcript("sess-006", self.workspace_root)

        turn = transcript["turns"][0]
        self.assertIsNone(turn["operator_evidence"]["classification"])

    def test_stop_text_about_context_budget_does_not_trigger_overload_without_host_signal(
        self,
    ) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-007",
                "cwd": str(self.workspace_root),
                "transcript_path": "",
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-007",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Explain the workflow guardrails.",
            },
            {
                "record_type": "stop",
                "session_id": "sess-007",
                "recorded_at": "2026-03-20T10:00:40Z",
                "last_assistant_message": (
                    "In general, keep the context budget small for future prompts."
                ),
                "stop_reason": "end_turn",
            },
        ]
        self._write_mirror_session("sess-007", records)

        transcript = load_claude_code_transcript("sess-007", self.workspace_root)

        turn = transcript["turns"][0]
        self.assertIsNone(turn["operator_evidence"]["classification"])

    def test_explicit_assistant_error_phrase_can_still_trigger_host_failure_fallback(self) -> None:
        records = [
            {
                "record_type": "session-start",
                "session_id": "sess-008",
                "cwd": str(self.workspace_root),
                "transcript_path": "",
                "model": "claude-sonnet-4.6",
            },
            {
                "record_type": "prompt-submit",
                "session_id": "sess-008",
                "recorded_at": "2026-03-20T10:00:00Z",
                "prompt": "Explain the earlier failure.",
            },
            {
                "record_type": "stop",
                "session_id": "sess-008",
                "recorded_at": "2026-03-20T10:00:40Z",
                "last_assistant_message": "Error during execution while loading the image payload.",
                "stop_reason": "end_turn",
            },
        ]
        self._write_mirror_session("sess-008", records)

        transcript = load_claude_code_transcript("sess-008", self.workspace_root)

        turn = transcript["turns"][0]
        self.assertEqual(
            turn["operator_evidence"]["classification"],
            "host-runtime-failure",
        )


class TranscriptValidationTests(unittest.TestCase):
    """Test normalized transcript validation accepts both providers."""

    def test_supported_providers_includes_claude_code(self) -> None:
        self.assertIn("claude-code", SUPPORTED_PROVIDERS)
        self.assertIn("codex", SUPPORTED_PROVIDERS)

    def test_validate_claude_code_transcript(self) -> None:
        transcript = {
            "provider": "claude-code",
            "native_thread_id": "test-session",
            "turns": [
                {"user_text": "hello", "attachments": []},
            ],
        }
        validate_normalized_transcript(transcript)  # Should not raise

    def test_validate_rejects_unknown_provider(self) -> None:
        transcript = {
            "provider": "unknown-provider",
            "native_thread_id": "test",
            "turns": [],
        }
        with self.assertRaises(ValueError):
            validate_normalized_transcript(transcript)


class DetectAgentSurfaceTests(unittest.TestCase):
    """Test the detect_agent_surface refinement in conversation.py."""

    def test_codex_thread_id_returns_codex(self) -> None:
        with mock.patch.dict("os.environ", {"CODEX_THREAD_ID": "thread-1"}, clear=False):
            with mock.patch.dict(
                "os.environ",
                {
                    k: ""
                    for k in ("DOCMASON_AGENT_SURFACE", "CLAUDE_PROJECT_DIR", "CLAUDE_SESSION_ID")
                },
                clear=False,
            ):
                surface = detect_agent_surface()
        self.assertEqual(surface, "codex")

    def test_claude_project_dir_returns_claude_code(self) -> None:
        env = {
            "CLAUDE_PROJECT_DIR": "/some/path",
            "CODEX_THREAD_ID": "",
            "DOCMASON_AGENT_SURFACE": "",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            surface = detect_agent_surface()
        self.assertEqual(surface, "claude-code")

    def test_explicit_override_takes_priority(self) -> None:
        env = {
            "DOCMASON_AGENT_SURFACE": "custom-agent",
            "CLAUDE_PROJECT_DIR": "/some/path",
            "CODEX_THREAD_ID": "thread-1",
        }
        with mock.patch.dict("os.environ", env, clear=False):
            surface = detect_agent_surface()
        self.assertEqual(surface, "custom-agent")


class MaybeReconcileActiveThreadTests(unittest.TestCase):
    """Test the provider-agnostic reconciliation dispatch."""

    def test_dispatch_to_claude_code_when_claude_session(self) -> None:
        from docmason.interaction import maybe_reconcile_active_thread

        env = {
            "CLAUDE_SESSION_ID": "test-session",
            "CLAUDE_PROJECT_DIR": "/some/path",
            "CODEX_THREAD_ID": "",
            "DOCMASON_AGENT_SURFACE": "",
        }
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        workspace_root = Path(tempdir.name)
        (workspace_root / "runtime").mkdir()
        workspace = WorkspacePaths(root=workspace_root)

        with mock.patch.dict("os.environ", env, clear=False):
            result = maybe_reconcile_active_thread(workspace)
        # reconcile_claude_code_thread returns a status dict (not None) when
        # the mirror file is missing — the catch in maybe_reconcile_active_claude_code_thread
        # only catches FileNotFoundError/KeyError/ValueError exceptions.
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "not-available")


class CLIHookSubcommandTests(unittest.TestCase):
    """Test the hidden _hook CLI subcommand."""

    def test_main_dispatches_hook_subcommand(self) -> None:
        with mock.patch("docmason.hooks.run_hook_cli", return_value=0) as hook_cli:
            result = docmason_main(["_hook", "session"])
        hook_cli.assert_called_once_with("session")
        self.assertEqual(result, 0)

    def test_main_dispatches_all_hook_event_names(self) -> None:
        for event in SUPPORTED_EVENTS:
            with mock.patch("docmason.hooks.run_hook_cli", return_value=0) as hook_cli:
                docmason_main(["_hook", event])
            hook_cli.assert_called_once_with(event)

    def test_top_level_help_hides_hidden_hook_and_ask_commands(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        self.assertNotIn("_hook", help_text)
        self.assertNotIn("_ask", help_text)


class CommittedBootstrapperFilesTests(unittest.TestCase):
    """Test the committed .claude/ directory structure."""

    def test_committed_claude_md_exists(self) -> None:
        path = ROOT / ".claude" / "CLAUDE.md"
        self.assertTrue(path.exists(), ".claude/CLAUDE.md should be committed")

    def test_committed_claude_md_imports_agents(self) -> None:
        path = ROOT / ".claude" / "CLAUDE.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("@../AGENTS.md", content)

    def test_committed_claude_md_imports_generated_project_memory(self) -> None:
        path = ROOT / ".claude" / "CLAUDE.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("@../adapters/claude/project-memory.md", content)

    def test_committed_claude_md_keeps_hidden_ask_out_of_entry_text(self) -> None:
        path = ROOT / ".claude" / "CLAUDE.md"
        content = path.read_text(encoding="utf-8")
        self.assertIn("generated helpers", content)
        self.assertIn("committed hooks", content)
        self.assertNotIn("`docmason _ask`", content)
        self.assertNotIn("docmason.ask.prepare_ask_turn()", content)
        self.assertNotIn("docmason.ask.complete_ask_turn()", content)

    def test_committed_settings_json_exists(self) -> None:
        path = ROOT / ".claude" / "settings.json"
        self.assertTrue(path.exists(), ".claude/settings.json should be committed")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("hooks", data)

    def test_hook_scripts_exist_and_executable(self) -> None:
        hooks_dir = ROOT / ".claude" / "hooks"
        self.assertTrue(hooks_dir.exists())
        scripts = list(hooks_dir.glob("on-*.sh"))
        self.assertGreaterEqual(len(scripts), 3, "At least 3 hook scripts expected")
        for script in scripts:
            self.assertTrue(
                os.access(script, os.X_OK),
                f"Hook script {script.name} should be executable",
            )

    def test_committed_claude_md_avoids_manual_skills_symlink_steps(self) -> None:
        path = ROOT / ".claude" / "CLAUDE.md"
        content = path.read_text(encoding="utf-8")
        self.assertNotIn("ln -s", content)
        self.assertIn(".claude/skills", content)

    def test_gitignore_excludes_repo_local_skill_shims(self) -> None:
        gitignore = ROOT / ".gitignore"
        content = gitignore.read_text(encoding="utf-8")
        self.assertIn("/.claude/skills", content)
        self.assertIn("/.agents/", content)
        self.assertNotIn("/CLAUDE.md", content)

    def test_repo_does_not_commit_root_claude_md(self) -> None:
        self.assertFalse((ROOT / "CLAUDE.md").exists())


class ProjectPathsTests(unittest.TestCase):
    """Test the Claude Code mirror path properties in project.py."""

    def test_claude_code_mirror_root(self) -> None:
        root = Path("/fake/workspace")
        paths = WorkspacePaths(root=root)
        self.assertEqual(
            paths.claude_code_mirror_root,
            root / "runtime" / "interaction-ingest" / "claude-code",
        )

    def test_claude_code_mirror_path(self) -> None:
        root = Path("/fake/workspace")
        paths = WorkspacePaths(root=root)
        self.assertEqual(
            paths.claude_code_mirror_path("abc-123"),
            root / "runtime" / "interaction-ingest" / "claude-code" / "abc-123.jsonl",
        )

    def test_claude_code_connector_manifest_path(self) -> None:
        root = Path("/fake/workspace")
        paths = WorkspacePaths(root=root)
        self.assertTrue(
            str(paths.claude_code_connector_manifest_path).endswith("claude-code.json"),
        )

    def test_repo_skill_shim_paths(self) -> None:
        root = Path("/fake/workspace")
        paths = WorkspacePaths(root=root)
        self.assertEqual(paths.repo_skill_shim_dir, root / ".agents" / "skills")
        self.assertEqual(paths.claude_skill_shim_dir, root / ".claude" / "skills")


class DoctorHookCheckTests(unittest.TestCase):
    """Test the doctor command includes Claude Code hook diagnostics."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)

        (root / "src" / "docmason").mkdir(parents=True)
        shutil.copytree(ROOT / "skills" / "canonical", root / "skills" / "canonical")
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "runtime").mkdir()
        (root / "adapters").mkdir()
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

    def test_doctor_reports_hooks_when_settings_present(self) -> None:
        workspace = self.make_workspace()
        hooks_dir = workspace.root / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True)
        settings = workspace.root / ".claude" / "settings.json"
        settings.write_text('{"hooks":{}}', encoding="utf-8")

        # Create executable hook script
        script = hooks_dir / "on-session.sh"
        script.write_text("#!/bin/bash\n", encoding="utf-8")
        script.chmod(0o755)

        def fake_editable_install(paths: WorkspacePaths) -> tuple[bool, str]:
            return True, "editable install available"

        with mock.patch.dict(
            "os.environ", {"CODEX_THREAD_ID": "", "CLAUDE_SESSION_ID": ""}, clear=False
        ):
            report = doctor_workspace(workspace, editable_install_probe=fake_editable_install)

        checks = report.payload.get("checks", [])
        hook_checks = [c for c in checks if c["name"] == "claude-code-hooks"]
        self.assertEqual(len(hook_checks), 1, "Should have a claude-code-hooks check")
        self.assertEqual(hook_checks[0]["status"], READY)

    def test_doctor_reports_hooks_absent_gracefully(self) -> None:
        workspace = self.make_workspace()
        # No .claude/settings.json — hooks not configured

        def fake_editable_install(paths: WorkspacePaths) -> tuple[bool, str]:
            return True, "editable install available"

        with mock.patch.dict(
            "os.environ", {"CODEX_THREAD_ID": "", "CLAUDE_SESSION_ID": ""}, clear=False
        ):
            report = doctor_workspace(workspace, editable_install_probe=fake_editable_install)

        checks = report.payload.get("checks", [])
        hook_checks = [c for c in checks if c["name"] == "claude-code-hooks"]
        self.assertEqual(len(hook_checks), 1)
        self.assertEqual(hook_checks[0]["status"], READY)  # Not a blocker when absent


class ConnectorManifestTests(unittest.TestCase):
    """Test that connector manifests include Claude Code."""

    def make_workspace(self) -> WorkspacePaths:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "runtime").mkdir()
        (root / "original_doc").mkdir()
        (root / "knowledge_base").mkdir()
        (root / "src" / "docmason").mkdir(parents=True)
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

    def test_refresh_creates_claude_code_connector(self) -> None:
        from docmason.interaction import refresh_generated_connector_manifests

        workspace = self.make_workspace()
        refresh_generated_connector_manifests(workspace)
        manifest_path = workspace.claude_code_connector_manifest_path
        self.assertTrue(manifest_path.exists(), "Claude Code connector manifest should be created")
        data = read_json(manifest_path)
        self.assertEqual(data["provider"], "claude-code")
        self.assertEqual(data["connector_kind"], "hook-mirror")
        self.assertEqual(data["capability_scope"], "connector-capture")
        self.assertTrue(data["captures_attachments"])
        self.assertTrue(data["captures_multimodal_content"])


class ToolUseAuditClaudeCodeTests(unittest.TestCase):
    """Test that build_tool_use_audit handles Claude Code Bash tool format."""

    def test_command_text_extracts_bash_tool_commands(self) -> None:
        from docmason.interaction import _command_text

        # Claude Code Bash tool uses tool_input.command (stored as arguments)
        call = {"tool_name": "Bash", "arguments": {"command": "docmason retrieve 'test'"}}
        self.assertIn("docmason retrieve", _command_text(call))

    def test_command_text_extracts_bash_tool_input(self) -> None:
        from docmason.interaction import _command_text

        # When stored under tool_input instead of arguments (hook-mirror format)
        call = {"tool_name": "Bash", "tool_input": {"command": "ls knowledge_base/"}}
        self.assertIn("knowledge_base/", _command_text(call))

    def test_command_text_handles_codex_exec_command(self) -> None:
        from docmason.interaction import _command_text

        call = {"tool_name": "exec_command", "arguments": {"cmd": "docmason status"}}
        self.assertIn("docmason status", _command_text(call))


if __name__ == "__main__":
    unittest.main()

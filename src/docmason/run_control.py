"""Run-control helpers for governed DocMason ask turns."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .contracts import validate_commit_contract
from .project import WorkspacePaths, append_jsonl, read_json, write_json

RUN_WORKFLOW_VERSION = "phase-1-run-control"


def run_dir(paths: WorkspacePaths, run_id: str) -> Path:
    """Return the runtime directory for one governed run."""
    return paths.runs_dir / run_id


def run_state_path(paths: WorkspacePaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / "state.json"


def run_journal_path(paths: WorkspacePaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / "journal.jsonl"


def run_commit_path(paths: WorkspacePaths, run_id: str) -> Path:
    return run_dir(paths, run_id) / "commit.json"


def load_run_state(paths: WorkspacePaths, run_id: str) -> dict[str, Any]:
    """Load one run state when it exists."""
    return read_json(run_state_path(paths, run_id))


def capability_profile(paths: WorkspacePaths) -> dict[str, Any]:
    """Return the compact capability profile for the current run environment."""
    from .conversation import detect_agent_surface

    surface = detect_agent_surface()
    return {
        "agent_surface": surface,
        "local_file_access": True,
        "shell_access": True,
        "image_inspection": surface in {"codex", "claude-code"},
        "workspace_python_ready": paths.venv_python.exists(),
    }


def version_context(paths: WorkspacePaths) -> dict[str, Any]:
    """Capture the minimum version context needed to interpret a committed turn."""
    from .conversation import current_corpus_signature, utc_now

    publish_manifest = read_json(paths.current_publish_manifest_path)
    return {
        "captured_at": utc_now(),
        "corpus_signature": current_corpus_signature(paths),
        "published_at": publish_manifest.get("published_at"),
        "published_snapshot_id": publish_manifest.get("snapshot_id"),
        "answer_workflow_version": RUN_WORKFLOW_VERSION,
    }


def record_run_event(
    paths: WorkspacePaths,
    *,
    run_id: str,
    stage: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one run-journal event and update the materialized run state."""
    from .conversation import utc_now

    event = {
        "recorded_at": utc_now(),
        "run_id": run_id,
        "stage": stage,
        "event_type": event_type,
        "payload": payload or {},
    }
    state = load_run_state(paths, run_id)
    if not state:
        raise FileNotFoundError(run_state_path(paths, run_id))
    state["updated_at"] = event["recorded_at"]
    state["last_stage"] = stage
    state["last_event_type"] = event_type
    state["event_count"] = int(state.get("event_count", 0)) + 1
    write_json(run_state_path(paths, run_id), state)
    append_jsonl(run_journal_path(paths, run_id), event)
    return event


def ensure_run_for_turn(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    user_question: str,
    entry_workflow_id: str = "ask",
) -> dict[str, Any]:
    """Create or reuse the governed run for one canonical turn."""
    from .conversation import load_turn_record, update_conversation_turn, utc_now

    turn: dict[str, Any] = {}
    try:
        turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    except KeyError:
        turn = {}
    existing_run_id = turn.get("active_run_id") or turn.get("committed_run_id")
    if isinstance(existing_run_id, str) and existing_run_id and load_run_state(paths, existing_run_id):
        return load_run_state(paths, existing_run_id)

    run_id = str(uuid.uuid4())
    payload = {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "entry_workflow_id": entry_workflow_id,
        "user_question": user_question,
        "status": "active",
        "opened_at": utc_now(),
        "updated_at": utc_now(),
        "last_stage": "prepare",
        "last_event_type": "run-opened",
        "event_count": 0,
        "capability_profile": capability_profile(paths),
        "version_context": version_context(paths),
    }
    run_dir(paths, run_id).mkdir(parents=True, exist_ok=True)
    write_json(run_state_path(paths, run_id), payload)
    append_jsonl(
        run_journal_path(paths, run_id),
        {
            "recorded_at": payload["opened_at"],
            "run_id": run_id,
            "stage": "prepare",
            "event_type": "run-opened",
            "payload": {
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "entry_workflow_id": entry_workflow_id,
            },
        },
    )
    if turn:
        update_conversation_turn(
            paths,
            conversation_id=conversation_id,
            turn_id=turn_id,
            updates={
                "active_run_id": run_id,
                "turn_state": "prepared",
                "version_context": payload["version_context"],
                "capability_profile": payload["capability_profile"],
            },
        )
    return load_run_state(paths, run_id)


def commit_run(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    status: str,
    answer_state: str | None,
    support_basis: str | None,
    support_manifest_path: str | None,
    answer_file_path: str | None,
    response_excerpt: str | None,
    turn_updates: dict[str, Any],
) -> dict[str, Any]:
    """Commit one governed turn outcome through the shared commit barrier."""
    from .conversation import load_turn_record, update_conversation_turn, utc_now

    turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    run_payload = ensure_run_for_turn(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_question=str(turn.get("user_question") or ""),
        entry_workflow_id=str(turn.get("entry_workflow_id") or "ask"),
    )
    run_id = str(run_payload["run_id"])
    committed_run_id = turn.get("committed_run_id")
    if isinstance(committed_run_id, str) and committed_run_id and committed_run_id != run_id:
        raise ValueError(
            f"Turn `{turn_id}` is already committed by run `{committed_run_id}`."
        )

    effective_version_context = turn.get("version_context")
    if not isinstance(effective_version_context, dict):
        effective_version_context = version_context(paths)
    validate_commit_contract(
        answer_state=answer_state,
        support_basis=support_basis,
        support_manifest_path=support_manifest_path,
        version_context=effective_version_context,
    )

    effective_answer_file = answer_file_path or turn.get("answer_file_path")
    if not isinstance(effective_answer_file, str) or not effective_answer_file:
        raise ValueError("Committed turns require an answer_file_path.")
    answer_path = Path(effective_answer_file)
    if not answer_path.is_absolute():
        answer_path = paths.root / answer_path
    answer_text = answer_path.read_text(encoding="utf-8").strip() if answer_path.exists() else ""
    if answer_state != "abstained" and not answer_text:
        raise ValueError("Committed turns require a non-empty final answer file.")

    derived_excerpt = response_excerpt
    if not isinstance(derived_excerpt, str) or not derived_excerpt:
        derived_excerpt = answer_text[:500] if answer_text else None

    commit_payload = {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "committed_at": utc_now(),
        "status": status,
        "answer_state": answer_state,
        "support_basis": support_basis,
        "support_manifest_path": support_manifest_path,
        "answer_file_path": effective_answer_file,
        "response_excerpt": derived_excerpt,
        "version_context": effective_version_context,
    }
    write_json(run_commit_path(paths, run_id), commit_payload)
    run_payload.update(
        {
            "status": "committed",
            "updated_at": commit_payload["committed_at"],
            "last_stage": "commit",
            "last_event_type": "turn-committed",
            "version_context": effective_version_context,
        }
    )
    write_json(run_state_path(paths, run_id), run_payload)
    append_jsonl(
        run_journal_path(paths, run_id),
        {
            "recorded_at": commit_payload["committed_at"],
            "run_id": run_id,
            "stage": "commit",
            "event_type": "turn-committed",
            "payload": {
                "answer_state": answer_state,
                "support_basis": support_basis,
                "answer_file_path": effective_answer_file,
            },
        },
    )
    updated = update_conversation_turn(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        updates={
            **turn_updates,
            "active_run_id": run_id,
            "committed_run_id": run_id,
            "turn_state": "committed",
            "version_context": effective_version_context,
            "capability_profile": run_payload.get("capability_profile"),
            "answer_file_path": effective_answer_file,
            "response_excerpt": derived_excerpt,
            "status": status,
        },
    )
    return updated

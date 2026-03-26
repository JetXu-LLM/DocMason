"""Run-control helpers for governed DocMason ask turns."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .contracts import validate_commit_contract
from .project import WorkspacePaths, append_jsonl, read_json, write_json

RUN_WORKFLOW_VERSION = "phase-1-run-control"
RUN_ORIGIN_NATIVE_RECONCILIATION = "native-reconciliation"
RUN_ORIGIN_ASK_FRONT_DOOR = "ask-front-door"
_RUN_ORIGIN_PRIORITY = {
    None: 0,
    RUN_ORIGIN_NATIVE_RECONCILIATION: 1,
    RUN_ORIGIN_ASK_FRONT_DOOR: 2,
}


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
    corpus_signature = current_corpus_signature(paths)
    return {
        "captured_at": utc_now(),
        "corpus_signature": corpus_signature,
        "published_source_signature": corpus_signature,
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


def record_run_event_if_present(
    paths: WorkspacePaths,
    *,
    run_id: str | None,
    stage: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Append a run-journal event when the run still exists."""
    if not isinstance(run_id, str) or not run_id:
        return None
    if not load_run_state(paths, run_id):
        return None
    return record_run_event(
        paths,
        run_id=run_id,
        stage=stage,
        event_type=event_type,
        payload=payload,
    )


def record_run_event_for_runs(
    paths: WorkspacePaths,
    *,
    run_ids: list[str] | None,
    stage: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Append the same event to each existing run in one run-id list."""
    if not isinstance(run_ids, list):
        return
    for run_id in run_ids:
        record_run_event_if_present(
            paths,
            run_id=run_id if isinstance(run_id, str) else None,
            stage=stage,
            event_type=event_type,
            payload=payload,
        )


def update_run_state(paths: WorkspacePaths, *, run_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Merge updates into one run state."""
    state = load_run_state(paths, run_id)
    if not state:
        raise FileNotFoundError(run_state_path(paths, run_id))
    state.update(updates)
    write_json(run_state_path(paths, run_id), state)
    return state


def normalize_run_origin(value: Any) -> str | None:
    """Normalize one run-origin value into the supported contract."""
    if isinstance(value, str) and value in {
        RUN_ORIGIN_NATIVE_RECONCILIATION,
        RUN_ORIGIN_ASK_FRONT_DOOR,
    }:
        return value
    return None


def stronger_run_origin(current: Any, candidate: Any) -> str | None:
    """Return the stronger run-origin value without demotion."""
    current_origin = normalize_run_origin(current)
    candidate_origin = normalize_run_origin(candidate)
    if _RUN_ORIGIN_PRIORITY[candidate_origin] > _RUN_ORIGIN_PRIORITY[current_origin]:
        return candidate_origin
    return current_origin


def attach_shared_job_to_run(
    paths: WorkspacePaths,
    *,
    run_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Attach one shared job to a run's tracked dependencies."""
    state = load_run_state(paths, run_id)
    if not state:
        raise FileNotFoundError(run_state_path(paths, run_id))
    attached = state.get("attached_shared_job_ids", [])
    if not isinstance(attached, list):
        attached = []
    added = False
    if job_id not in attached:
        attached.append(job_id)
        added = True
    state["attached_shared_job_ids"] = attached
    write_json(run_state_path(paths, run_id), state)
    if added:
        record_run_event(
            paths,
            run_id=run_id,
            stage="control-plane",
            event_type="shared-job-attached",
            payload={"job_id": job_id},
        )
    return state


def refresh_turn_run_version_truth(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    run_id: str | None,
) -> dict[str, Any]:
    """Refresh the active run and turn version truth from current published state."""
    from .conversation import update_conversation_turn
    from .control_plane import workspace_state_ref

    refreshed_context = version_context(paths)
    if isinstance(run_id, str) and run_id and load_run_state(paths, run_id):
        update_run_state(
            paths,
            run_id=run_id,
            updates={
                "version_context": refreshed_context,
                "workspace_state_ref": workspace_state_ref(paths),
            },
        )
    update_conversation_turn(
        paths,
        conversation_id=conversation_id,
        turn_id=turn_id,
        updates={"version_context": refreshed_context},
    )
    return refreshed_context


def ensure_run_for_turn(
    paths: WorkspacePaths,
    *,
    conversation_id: str,
    turn_id: str,
    user_question: str,
    entry_workflow_id: str = "ask",
    run_origin: str | None = None,
) -> dict[str, Any]:
    """Create or reuse the governed run for one canonical turn."""
    from .conversation import load_turn_record, update_conversation_turn, utc_now
    from .control_plane import workspace_state_ref

    turn: dict[str, Any] = {}
    try:
        turn = load_turn_record(paths, conversation_id=conversation_id, turn_id=turn_id)
    except KeyError:
        turn = {}
    existing_run_id = turn.get("active_run_id") or turn.get("committed_run_id")
    if isinstance(existing_run_id, str) and existing_run_id and load_run_state(paths, existing_run_id):
        existing_state = load_run_state(paths, existing_run_id)
        requested_origin = normalize_run_origin(run_origin)
        effective_origin = stronger_run_origin(existing_state.get("run_origin"), requested_origin)
        if effective_origin != normalize_run_origin(existing_state.get("run_origin")):
            existing_state = update_run_state(
                paths,
                run_id=existing_run_id,
                updates={"run_origin": effective_origin},
            )
        return existing_state

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
        "workspace_state_ref": workspace_state_ref(paths),
        "attached_shared_job_ids": [],
        "admissibility_gate_result": None,
        "published_snapshot_id_used": None,
        "published_source_signature_used": None,
        "run_origin": normalize_run_origin(run_origin),
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
    admissibility_gate_result: dict[str, Any] | None = None,
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

    effective_version_context = run_payload.get("version_context")
    if not isinstance(effective_version_context, dict):
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
        "admissibility_gate_result": admissibility_gate_result,
    }
    write_json(run_commit_path(paths, run_id), commit_payload)
    run_payload.update(
        {
            "status": "committed",
            "updated_at": commit_payload["committed_at"],
            "last_stage": "commit",
            "last_event_type": "turn-committed",
            "version_context": effective_version_context,
            "admissibility_gate_result": admissibility_gate_result,
            "published_snapshot_id_used": effective_version_context.get("published_snapshot_id"),
            "published_source_signature_used": (
                effective_version_context.get("published_source_signature")
                or effective_version_context.get("corpus_signature")
            ),
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
            "turn_state": turn_updates.get("turn_state", "committed"),
            "version_context": effective_version_context,
            "capability_profile": run_payload.get("capability_profile"),
            "answer_file_path": effective_answer_file,
            "response_excerpt": derived_excerpt,
            "status": status,
        },
    )
    return updated

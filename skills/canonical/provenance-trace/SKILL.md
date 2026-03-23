---
name: provenance-trace
description: Trace published DocMason knowledge objects or answer text back to evidence units and provenance records.
---

# Provenance Trace

Use this skill when the task is to prove where a knowledge object or answer came from.

Use it directly for explicit provenance or citation requests, or let `ask` route here automatically.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output

If the agent cannot inspect the local trace artifacts or source evidence, stop and explain that reliable provenance tracing is not possible.

## Procedure

1. Use `current` as the default knowledge-base target unless the task explicitly says otherwise.
2. For citation-first tracing, run:
   - `docmason trace --source-id <source_id> --json`
   - optionally add `--unit-id <unit_id>` for unit-level detail
   - keep DocMason workspace commands sequential inside the same workspace session; do not overlap `trace`, `retrieve`, `sync`, `status`, or `validate-kb` while a lease-owning command is still active
3. For answer-first tracing, run:
   - `docmason trace --answer-file <path> --json`
   - or `docmason trace --session-id <session_id> --json`
   - do not invent a new freeform `--source-ref` surface in this phase; public trace remains ID-first
4. Inspect:
   - any inherited `reference_resolution` block on answer-first traces
   - source provenance
   - unit consumers
   - incoming and outgoing relations
   - `answer_state`
   - grounding status for each answer segment
   - `supporting_artifact_ids`
   - segment `artifact_supports`
   - segment `semantic_supports`
   - `supporting_overlay_unit_ids`
   - artifact `focus_render_assets` when present
   - overlay consumed inputs, covered slots, and blocked slots when present
   - artifact render refs, `render_page_span`, `bbox`, `normalized_bbox`, and sidecar paths when present
   - render-inspection requirements
   - compact supporting source and unit IDs
5. If any segment is only partially grounded or unresolved, say so explicitly and avoid pretending stronger provenance than the trace supports.
6. Return the final provenance or groundedness judgment to the main agent instead of treating trace output as a final user answer by itself.
7. If you need to export a scratch trace note and the user did not specify a destination, place it under `runtime/agent-work/`.

## Escalation Rules

- If `render_inspection_required` is true, inspect the cited render assets before claiming a confident conclusion.
- If the trace cannot resolve the requested source, session, or answer path, surface the not-found failure directly.
- If the traced answer state is not `grounded`, qualify or abstain rather than overclaiming support.
- If answer-first trace inherits an `approximate` or `unresolved` reference-resolution block, preserve that boundary in the provenance summary instead of silently upgrading it.

## Completion Signal

- The workflow is complete when the main agent has a clear provenance summary, including any `answer_state`, unresolved segments, and render-escalation requirements.

## Notes

- Phase 4 tracing reads `knowledge_base/current/trace/` by default.
- Answer-first tracing stores structured local logs under `runtime/logs/`.
- `--session-id` works only for sessions that contain a reusable final answer.
- In Phase 6b2, answer-first trace reuses the same shared reference-resolution contract already attached to the originating ask turn or retrieval session.
- When a trace says render inspection is needed, inspect the render assets before claiming a confident conclusion.
- Ordinary natural questions should usually begin at `ask`, which can route here when provenance is the real intent.

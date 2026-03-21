---
name: workspace-status
description: Inspect the current DocMason workspace stage and pending actions from the filesystem and runtime state.
---

# Workspace Status

Use this skill when the task is to understand what stage the workspace is in and what should happen next.

## Required Capabilities

- local file access
- shell or command execution
- ability to reason about staged workflow state

If the agent cannot inspect the filesystem, stop and explain that the current workspace state cannot be determined safely.

## Procedure

1. Run `docmason status --json`.
2. Use the reported `stage` and `pending_actions` as the authoritative workspace status summary.
3. Do not claim that a knowledge base exists unless `status` reports it as present.
4. If the task is ambiguous, interpret `pending_actions` in context rather than as a rigid universal order.
   - missing or stale adapter guidance matters only when the current flow depends on generated adapter files
   - missing or stale knowledge-base state matters directly for ordinary asking
5. Return the final status interpretation to the main agent without mutating workspace state.

## Escalation Rules

- `status` is descriptive, not reparative. Do not silently switch into `prepare`, `sync`, or `sync-adapters` from inside this workflow.
- When the workspace is stale or invalid, preserve that distinction instead of flattening it into a generic “not ready” message.

## Completion Signal

- The workflow is complete when the main agent has the authoritative workspace stage, environment readiness state, and next obvious actions.

## Notes

- `status` is derived from the filesystem and runtime state. There is no daemon or database.
- Pending actions may include `prepare`, `sync-adapters`, `sync`, and `validate-kb`.
- A staged knowledge base with pending synthesis or blocking validation is reported as `knowledge-base-invalid`.
- `sync-adapters` is an operator-maintenance action, not a universal prerequisite before every `ask` turn.

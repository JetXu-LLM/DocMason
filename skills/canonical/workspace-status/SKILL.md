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
2. Use the reported `stage`, `bootstrap_state`, `control_plane`, and `pending_actions` as the authoritative workspace status summary.
   - use the `environment` block to distinguish `self-contained`, `mixed`, and `degraded`
   - treat only `self-contained` as ordinary ask-time ready
3. Do not claim that a knowledge base exists unless `status` reports it as present.
4. If the task is ambiguous, interpret `pending_actions` in context rather than as a rigid universal order.
   - missing or stale adapter guidance matters only when the current flow depends on generated adapter files
   - missing or stale knowledge-base state matters directly for ordinary asking
5. Return the final status interpretation to the main agent without mutating workspace state.

## Escalation Rules

- `status` is descriptive, not reparative. Do not silently switch into `prepare`, `sync`, or `sync-adapters` from inside this workflow.
- When the workspace is stale or invalid, preserve that distinction instead of flattening it into a generic “not ready” message.
- When the stage is `control-plane-pending-confirmation`, preserve that state explicitly and surface the exact pending approval action such as `prepare --yes` or `sync --yes`.

## Completion Signal

- The workflow is complete when the main agent has the authoritative workspace stage, environment readiness state, and next obvious actions.

## Notes

- `status` is derived from the filesystem and runtime state. There is no daemon or database.
- `bootstrap_state` is the explicit cached-ready summary for ordinary ask-time reuse.
- `environment.toolchain_mode`, `environment.isolation_grade`, and `environment.entrypoint_health`
  are the stable readiness fields for the prepared runtime.
- Pending actions may include `prepare`, `sync-adapters`, `sync`, and `validate-kb`.
- Control-plane pending confirmations are authoritative status truth, not optional side notes.
- A staged knowledge base with pending synthesis or blocking validation is reported as `knowledge-base-invalid`.
- `sync-adapters` is an operator-maintenance action, not a universal prerequisite before every `ask` turn.

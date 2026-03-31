---
name: knowledge-base-sync
description: Stage, incrementally refresh, validate, and publish the DocMason knowledge base from the local source corpus.
---

# Knowledge-Base Sync

Use this skill when the task is to build or refresh the DocMason knowledge base.

This is a top-level operator workflow.
Ordinary users should not need to name `knowledge-construction` or `validation-repair` themselves.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output

If the agent cannot run local commands or inspect the resulting artifacts, stop and explain that the workspace cannot be synced reliably.

## Procedure

1. Start with `docmason status --json` when you need the current stage, pending actions, or control-plane state.
2. Use `docmason sync --json` as the default build or refresh entry point.
   - keep workspace commands sequential while the sync lease is active
   - do not overlap `status`, `sync`, `retrieve`, `trace`, or `validate-kb` against the same workspace
3. Respect the control-plane result before reasoning about later workflow steps.
   - if `sync_status=awaiting-confirmation`, surface the confirmation prompt and continue with `docmason sync --yes --json`
   - if `sync_status=waiting-shared-job`, treat the existing shared sync job as the legal owner and wait or retry rather than starting a second path
   - if `sync_status=action-required`, surface the blocker directly
     - when the blocker is missing sync capability, route the operator to `prepare`
4. Treat successful `sync` as the deterministic truth-building path.
   - detect source changes
   - rebuild or reuse staged evidence
   - apply safe staged repairs
   - validate
   - publish to `knowledge_base/current`
5. If `sync_status` is `valid` or `warnings`, inspect `hybrid_enrichment`.
   - `hybrid_enrichment` describes whether deterministic sync left a remaining multimodal semantic gap
   - if `mode` is `not-needed` or `covered`, stop and return the publication result
   - if the sync payload also includes `lane_b_follow_up.work_path`, open that governed work packet first and treat it as the authoritative next step for this sync state
     - `lane_b_follow_up` is the bounded sync-time follow-up packet for that current staging state
   - when that governed packet is present, do not consume the broad queue blindly; only fall back to `hybrid_work_path` when no bounded packet was handed off
     - `hybrid_work_path` is the broader staged queue and is only the fallback when no bounded packet was handed off
   - if `mode` is `candidate-prepared` or `partially-covered` and no governed packet path was handed off, treat `hybrid_work_path` as the authoritative hard-artifact queue
   - do not improvise a second sync path or rewrite deterministic sidecars
   - write only additive `semantic_overlay/` sidecars for units you can support honestly, then rerun `docmason sync --json`
6. Treat `pending-synthesis` as a compatibility or deliberate manual-mode state, not the normal operator destination.
   - route to `knowledge-construction` only when that legacy or manual path is actually intended
   - rerun `docmason sync --json` after staged authoring completes
7. If validation still blocks publication after the deterministic sync path finishes, route to `validation-repair`, then rerun `docmason sync --json`.
8. Return the final publication judgment to the main agent. Do not delegate final publication sign-off.

## Escalation Rules

- Do not invent a second approval surface. The public approval command is `docmason sync --yes`.
- If Office rendering is required but unavailable, stop and return the concrete install step.
- If staged or hybrid follow-up work requires per-source editing, that bounded work may be parallelized, but the final rerun and final judgment remain on the main path.
- Do not silently trigger this workflow from an ordinary answer path without surfacing the governed state transition.

## Completion Signal

- The workflow is complete when `docmason sync --json` returns a final publication outcome for the current workspace state and any remaining hybrid state is either honestly covered, explicitly blocked, or explicitly surfaced through the governed follow-up packet and control-plane state.

## Notes

- Bare `docmason sync` remains the deterministic published-truth builder.
- Shared-job reuse is part of the sync contract. Matching asks and operator sync commands should converge on one shared sync job, not parallel owners.
- PDF rendering uses Python dependencies. PPTX, DOCX, and XLSX rendering requires LibreOffice `soffice`.
- Interaction-derived memories may also be promoted through the same autonomous sync loop when the staged interaction path is relevant and supported.

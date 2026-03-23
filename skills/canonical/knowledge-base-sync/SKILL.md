---
name: knowledge-base-sync
description: Stage, incrementally refresh, validate, and publish the DocMason knowledge base from the local source corpus.
---

# Knowledge-Base Sync

Use this skill when the task is to build or refresh the DocMason knowledge base.

This is a top-level build and refresh workflow.
Ordinary users should not need to name `knowledge-construction` or `validation-repair` themselves.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output

If the agent cannot run local commands or read the staged filesystem outputs, stop and explain that the workspace cannot be synced reliably.

## Procedure

1. Run `docmason status --json` when you need the current stage and pending actions.
2. Run `docmason sync --json` as the default build or refresh step.
   - keep DocMason workspace commands sequential inside one live workspace session
   - do not overlap `status`, `sync`, `retrieve`, `trace`, or `validate-kb` against the same workspace while a lease-owning command is still running
3. Treat `docmason sync --json` as an autonomous closed-loop workflow by default:
   - detect source changes
   - rebuild or reuse staged evidence
   - apply silent staged repairs
   - write conservative in-repo semantic outputs when staged knowledge is missing
   - report any additive high-value `semantic_overlay/` opportunities
   - validate
   - publish to `knowledge_base/current`
4. If `sync_status` is `valid` or `warnings`, inspect `hybrid_enrichment` in the payload.
   - when `mode` is `not-needed` or `covered`, stop and return the publication result
   - when `mode` is `candidate-prepared` or `partially-covered`, treat `hybrid_work_path` as the machine-readable hard-artifact queue rather than improvising your own candidate list
   - inspect `workflow_auto_supported` and `capability_gap_reason` explicitly so you do not pretend the bare CLI already completed the multimodal lane
   - in normal mode, consume only one budgeted Lane B batch per workflow pass:
     - at most `12` units total
     - at most `4` sources
     - at most `3` units per source
     - follow the source and unit order already sorted inside `hybrid_work.json`
   - only enter deep whole-corpus mode when the user explicitly asks to deeply complete the whole KB, not when they merely ask a normal question
   - when the current host agent can inspect renders, read the relevant staged artifacts in this order when available:
     - `hybrid_work.json`
     - `artifact_index.json`
     - `pdf_document.json`
     - `visual_layout/*.json`
     - `spreadsheet_workbook.json`
     - `spreadsheet_sheet/*.json`
     - then the cited renders
   - work only on the queued hard artifacts or unit renders named by `hybrid_work.json`
     - do not switch back to whole-document multimodal ingestion
     - for image-only or scanned PDF pages, prefer the published `page-image` artifact plus the queued render span
     - prefer `target_focus_render_assets` first, then full `target_render_assets`
     - if the baseline focus render is still not legible enough, use the repo's targeted hi-res focus-render helper for that specific artifact instead of rerendering the whole document
   - when writing overlays, keep the new governance contract intact:
     - `origin=sync-hybrid`
     - `source_fingerprint`
     - `unit_evidence_fingerprint`
     - `covered_slots`
     - `blocked_slots`
     - `consumed_inputs.focus_render_assets`
   - after that, write additive `semantic_overlay/<unit-id>.json` sidecars only for the units you can support honestly, then rerun `docmason sync --json`
   - if a queued source is already fully `covered`, skip it
   - if a queued source has only explicit blocked slots left, surface that honest boundary instead of pretending the lane is unfinished for mysterious reasons
   - do not rewrite deterministic sidecars such as `artifact_index.json`, `visual_layout/*.json`, `spreadsheet_*`, or `pdf_document.json`
5. If `sync_status` is `action-required`, surface the blocker directly instead of pretending the build is recoverable.
6. Treat `pending-synthesis` as a compatibility or manual-mode status rather than the normal operator path.
   - only route to `knowledge-construction` when a legacy or deliberately non-autonomous path is being exercised
   - rerun `docmason sync --json` after that staged authoring completes
7. If publication is still blocked by validation after the autonomous sync path finishes, route to `validation-repair`, then rerun `docmason sync --json`.
8. Always return the final publication status judgment to the main agent. Do not delegate publication sign-off.

## Escalation Rules

- If `sync` returns `action-required`, surface the blocker directly instead of pretending the build is recoverable.
- If Office rendering is required but unavailable, stop and return the concrete install step.
- If staged pending work or validation failures require editing source-specific files or staged interaction-memory files, those edits may be parallelized per bounded item, but the final rerun and final judgment return to the main agent.
- Do not silently trigger this workflow from an ordinary answer path without surfacing the state transition to the user.

## Completion Signal

- The workflow is complete when both conditions are true:
  - `docmason sync --json` returns a final publication status for the current workspace state, normally `valid` or `warnings`, or else an actionable blocker
  - `hybrid_enrichment.mode` is not `candidate-prepared`, unless the current environment lacks the multimodal capability and the workflow is explicitly surfacing that honest boundary, or the remaining state is an explicit blocked-slot boundary rather than unfinished hidden work

## Notes

- Use this workflow when the user explicitly asks to build or refresh the corpus, or when `ask` has no safe answer path without a usable published knowledge base.
- DocMason uses `knowledge_base/staging/` and `knowledge_base/current/`.
- Phase 4 sync reuses unchanged staged or published source directories when possible and rebuilds only changed sources.
- The current sync path also reuses prior semantic outputs when rebuilt evidence stays semantically stable, and it silently prunes deleted-source `related_sources` before validation.
- `hybrid_enrichment` is additive and honest. It reports semantic-overlay candidate coverage without turning deterministic publication into a hard dependency on a provider-specific model call.
- Bare `docmason sync` remains the deterministic truth builder. The workflow-level hybrid closure path belongs to this canonical skill, not to the public CLI itself.
- PDF rendering uses Python dependencies. PPTX, DOCX, and XLSX rendering requires LibreOffice `soffice`.
- On macOS without Homebrew, the recommended LibreOffice path is the official installer from `https://www.libreoffice.org/download/download/`; DocMason detects the standard `/Applications/.../soffice` path automatically.
- `sync` can publish with warnings when partial extraction failures were recorded honestly and surviving evidence is preserved.
- Interaction-derived memory candidates may also appear under `knowledge_base/staging/interaction/`; under normal supported conditions they are auto-authored and published through the same autonomous sync loop as staged source-derived knowledge objects.

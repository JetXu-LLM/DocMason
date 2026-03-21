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
3. Treat `docmason sync --json` as an autonomous closed-loop workflow by default:
   - detect source changes
   - rebuild or reuse staged evidence
   - apply silent staged repairs
   - write conservative in-repo semantic outputs when staged knowledge is missing
   - validate
   - publish to `knowledge_base/current`
4. If `sync_status` is `valid` or `warnings`, stop and return the publication result to the main agent.
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

- The workflow is complete when `docmason sync --json` returns a final status for the current workspace state, normally `valid` or `warnings`, or else an actionable blocker that the main agent can surface honestly.

## Notes

- Use this workflow when the user explicitly asks to build or refresh the corpus, or when `ask` has no safe answer path without a usable published knowledge base.
- DocMason uses `knowledge_base/staging/` and `knowledge_base/current/`.
- Phase 4 sync reuses unchanged staged or published source directories when possible and rebuilds only changed sources.
- The current sync path also reuses prior semantic outputs when rebuilt evidence stays semantically stable, and it silently prunes deleted-source `related_sources` before validation.
- PDF rendering uses Python dependencies. PPTX, DOCX, and XLSX rendering requires LibreOffice `soffice`.
- On macOS without Homebrew, the recommended LibreOffice path is the official installer from `https://www.libreoffice.org/download/download/`; DocMason detects the standard `/Applications/.../soffice` path automatically.
- `sync` can publish with warnings when partial extraction failures were recorded honestly and surviving evidence is preserved.
- Interaction-derived memory candidates may also appear under `knowledge_base/staging/interaction/`; under normal supported conditions they are auto-authored and published through the same autonomous sync loop as staged source-derived knowledge objects.

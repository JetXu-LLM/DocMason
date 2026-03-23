---
name: validation-repair
description: Validate the staged or published DocMason knowledge base and repair blocking issues until the quality gate passes.
---

# Validation Repair

Use this skill when the knowledge base needs validation, repair, or publication follow-through.

This is an internal follow-on workflow behind `knowledge-base-sync`.
Ordinary users should not need to invoke it by name.

## Required Capabilities

- local file access
- shell or command execution
- ability to read machine-readable validation reports

If the agent cannot run `docmason validate-kb` or inspect the reported errors, stop and explain that reliable repair is not possible.

## Procedure

1. Use `staging` as the default validation target unless the task explicitly says otherwise.
2. Run `docmason validate-kb --json`.
   - keep DocMason workspace commands sequential inside one live workspace session
   - do not overlap `validate-kb` with `sync`, `status`, `retrieve`, or `trace` against the same workspace while leases may still be active
3. Inspect the `blocking_errors`, `warnings`, and per-source reports in `knowledge_base/<target>/validation_report.json`.
4. Fix the staged source or interaction-memory files that caused the failures:
   - missing or stale `knowledge.json`
   - missing or malformed `summary.md`
   - unresolved citations
   - unresolved `artifact_id` citations
   - invalid related-source links
   - missing or malformed `artifact_index.json`
   - missing or malformed `pdf_document.json`
   - missing or malformed `semantic_overlay/*.json`
   - invalid `render_page_span`, artifact refs, or sidecar asset references
   - placeholder or incomplete bilingual content
5. Rerun `docmason validate-kb --json` until the result is `valid` or `warnings`.
6. When staged validation is no longer blocking, return control to the main agent so it can rerun `docmason sync --json` for final publication.

## Escalation Rules

- If validation reveals a capability or evidence gap that cannot be repaired honestly, stop and surface that blocker instead of weakening the quality gate.
- If a fix would require changing derived retrieval or trace artifacts directly, do not hand-edit them. Regenerate them through the supported workflows.
- If the failure is in deterministic sidecars such as `artifact_index`, `visual_layout`, `spreadsheet_*`, or `pdf_document`, repair the upstream staged source inputs or rerun the supported compiler path instead of hand-editing the derived files.
- Final publication remains a main-agent step.

## Completion Signal

- The workflow is complete when validation reaches `valid` or `warnings`, or when a concrete unrecoverable blocker has been surfaced to the main agent.

## Notes

- Blocking errors prevent publication.
- Warnings are acceptable only when the evidence manifest records partial extraction failure clearly and the surviving evidence still exists.
- Do not hand-edit derived graph, retrieval, or trace files unless you are explicitly regenerating them through the supported DocMason commands.

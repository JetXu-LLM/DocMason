---
name: knowledge-construction
description: Write bilingual Phase 3 knowledge objects for staged DocMason sources from rendered evidence and extracted structure.
---

# Knowledge Construction

Use this skill when `docmason sync` has prepared staged evidence and the agent must write `knowledge.json`, `summary.md`, or additive `semantic_overlay/<unit-id>.json` sidecars.

This is an internal follow-on workflow behind `knowledge-base-sync`.
Ordinary users should not need to invoke it by name.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect rendered images and extracted artifacts

If the agent cannot inspect rendered images, stop and explain that the environment is not capable enough for multimodal knowledge construction.

## Procedure

1. Read `knowledge_base/staging/pending_work.json`.
2. If a governed Lane B work packet is present under `runtime/control_plane/shared_jobs/<job_id>/lane_b_work.json`, treat that packet as the bounded authoritative source-selection scope for this pass.
3. If `knowledge_base/staging/hybrid_work.json` exists and contains the staged source you are handling, treat that file as the authoritative hard-artifact overlay queue inside that bounded scope.
4. Work only on the staged items listed there. Treat each pending source or interaction memory as an independent bounded write scope.
5. For each pending item, open:
   - `work_item.json`
   - `source_manifest.json`
   - `evidence_manifest.json`
   - `knowledge_base/staging/hybrid_work.json` when the sync payload reported `candidate-prepared`
   - `artifact_index.json` when present
   - `pdf_document.json` when present
   - `spreadsheet_workbook.json` when present
   - `spreadsheet_sheet/*.json` when present
   - `visual_layout/*.json` when present
   - `derived_affordances.json` when it already exists
   - extracted text and structure files
   - rendered assets referenced by the evidence manifest
   - when present, the staged interaction-specific context file such as `interaction_context.json`
6. Build the source semantics from the richest published evidence available instead of defaulting to flattened text:
   - for spreadsheets, prefer workbook, sheet, table, chart, metric, dimension, time-axis, hidden-sheet, and formula summaries over raw cell dumps
   - for PDF and PPTX, prefer section paths, captions, continuation links, procedure spans, region roles, charts, tables, pictures, connectors, groups, and major regions over page-level text alone
   - when the real support is artifact-level, include `artifact_id` in the citation instead of citing only the parent `unit_id`
7. Write `knowledge.json` with the required bilingual fields and only cite real evidence-unit IDs from the matching source.
8. Write `summary.md` with:
   - `# <title>`
   - a line that mentions the source ID
   - `## English Summary`
   - `## Source-Language Summary`
9. When the staged source includes high-value hybrid candidates and the environment can inspect renders, write additive `semantic_overlay/<unit-id>.json` sidecars only for the queued units in `hybrid_work.json`.
   - prefer overlay work where deterministic structure is already rich but cross-region or multimodal semantics are still missing
   - keep the hard-artifact boundary intact:
     - use the queued `target_artifact_ids`
     - use the queued `target_focus_render_assets` first
     - use the queued `target_render_assets` and `target_render_page_span`
     - for image-only or scanned PDF pages, treat the published `page-image` artifact as the first-class target instead of pretending the text layer is enough
     - if the baseline focus render is still not legible enough, use the targeted hi-res focus-render helper for that artifact instead of rerendering the whole source
   - overlays must remain additive
   - do not rewrite deterministic sidecars such as `artifact_index.json`, `visual_layout/*.json`, `spreadsheet_*`, or `pdf_document.json`
   - bind overlay claims to consumed inputs, artifact ids when available, explicit uncertainty notes, and the current freshness contract:
     - `origin`
     - `source_fingerprint`
     - `unit_evidence_fingerprint`
     - `covered_slots`
     - `blocked_slots`
10. Avoid placeholders, speculative citations, and unsupported related-source links.
11. Treat `derived_affordances.json` as a published sidecar rather than scratch output.
   - the baseline affordance sidecar is generated deterministically by the repo
   - if you enrich it, keep descriptors compact, evidence-backed, grouped by channel, and explicitly derived rather than source-authored fact
12. When evidence is weak, say so explicitly in `known_gaps`, `ambiguities`, confidence notes, or overlay uncertainty notes instead of inventing certainty.
13. After all assigned staged sources are complete, return control to the main agent so it can rerun `docmason sync --json` or `docmason validate-kb --json`.

## Escalation Rules

- If a staged source or interaction memory requires render inspection and the environment cannot inspect renders, stop that item and report the blocker directly.
- If a cross-source relation is uncertain, omit it rather than guessing.
- If a chart, table, diagram, or region claim cannot be supported by the published artifacts, write the uncertainty explicitly rather than laundering it through a source-level summary.
- If `hybrid_work.json` says a source is `candidate-prepared`, do not declare that hybrid lane complete merely because deterministic publication already succeeded.
- Do not publish, validate, or sign off the final sync result from inside this workflow. That judgment belongs to the main agent.

## Completion Signal

- The workflow is complete when every assigned staged source has updated `knowledge.json` and `summary.md`, or when a concrete capability blocker has been surfaced to the main agent.

## Notes

- `summary_source` may equal `summary_en` when the source itself is English.
- `related_sources` should stay light in Phase 3. Only add a relation when the source evidence clearly supports it.
- `semantic_overlay` is for cross-region or multimodal semantics that add value beyond the deterministic substrate. It is not a second primary truth surface.
- If extraction is weak, record that weakness explicitly in `known_gaps` or `ambiguities`. Do not invent certainty.
- Interaction memories should remain explicit about lower trust tier, interaction-derived provenance, and the distinction from authored source documents.

---
name: knowledge-construction
description: Write bilingual Phase 3 knowledge objects for staged DocMason sources from rendered evidence and extracted structure.
---

# Knowledge Construction

Use this skill when `docmason sync` has prepared staged evidence and the agent must write `knowledge.json` plus `summary.md`.

This is an internal follow-on workflow behind `knowledge-base-sync`.
Ordinary users should not need to invoke it by name.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect rendered images and extracted artifacts

If the agent cannot inspect rendered images, stop and explain that the environment is not capable enough for multimodal knowledge construction.

## Procedure

1. Read `knowledge_base/staging/pending_work.json`.
2. Work only on the staged items listed there. Treat each pending source or interaction memory as an independent bounded write scope.
3. For each pending item, open:
   - `work_item.json`
   - `source_manifest.json`
   - `evidence_manifest.json`
   - `derived_affordances.json` when it already exists
   - extracted text and structure files
   - rendered assets referenced by the evidence manifest
   - when present, the staged interaction-specific context file such as `interaction_context.json`
4. Write `knowledge.json` with the required bilingual fields and only cite real evidence-unit IDs from the matching source.
5. Write `summary.md` with:
   - `# <title>`
   - a line that mentions the source ID
   - `## English Summary`
   - `## Source-Language Summary`
6. Avoid placeholders, speculative citations, and unsupported related-source links.
7. Treat `derived_affordances.json` as a published sidecar rather than scratch output.
   - the baseline affordance sidecar is generated deterministically by the repo
   - if you enrich it, keep descriptors compact, evidence-backed, grouped by channel, and explicitly derived rather than source-authored fact
8. When evidence is weak, say so explicitly in `known_gaps`, `ambiguities`, or confidence notes instead of inventing certainty.
9. After all assigned staged sources are complete, return control to the main agent so it can rerun `docmason sync --json` or `docmason validate-kb --json`.

## Escalation Rules

- If a staged source or interaction memory requires render inspection and the environment cannot inspect renders, stop that item and report the blocker directly.
- If a cross-source relation is uncertain, omit it rather than guessing.
- Do not publish, validate, or sign off the final sync result from inside this workflow. That judgment belongs to the main agent.

## Completion Signal

- The workflow is complete when every assigned staged source has updated `knowledge.json` and `summary.md`, or when a concrete capability blocker has been surfaced to the main agent.

## Notes

- `summary_source` may equal `summary_en` when the source itself is English.
- `related_sources` should stay light in Phase 3. Only add a relation when the source evidence clearly supports it.
- If extraction is weak, record that weakness explicitly in `known_gaps` or `ambiguities`. Do not invent certainty.
- Interaction memories should remain explicit about lower trust tier, interaction-derived provenance, and the distinction from authored source documents.

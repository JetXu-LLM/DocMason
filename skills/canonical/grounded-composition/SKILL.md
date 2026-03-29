---
name: grounded-composition
description: Produce evidence-backed research, planning, drafting, or composition output from the published DocMason knowledge base while preserving provenance and answer-file discipline.
---

# Grounded Composition

Use this workflow when the user is not only asking for a direct answer, but is asking for evidence-backed drafting, planning, synthesis, or composition work such as:

- slide or deck planning
- executive summary drafting
- outline design
- wording proposals
- research bundles for a later deliverable

This is an inner specialist workflow behind `ask`.
Ordinary users should not need to name it explicitly before asking.

## Front-Door Precondition

- `grounded-composition` is never a free-standing ordinary front door.
- Start only from canonical ask turn metadata and canonical ask runtime ownership.
- Compatible hosts should enter through the repo-provided hidden canonical ask integration path rather than stitching ordinary ask lifecycle helpers manually.
- If the current turn is missing explicit canonical ask ownership, stop and route back to `ask`.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output
- ability to inspect rendered images when visual style, layout, or diagram detail matters

If the environment cannot inspect the required evidence, stop and explain the blocker instead of improvising weak output.

## Procedure

1. Start from the canonical `ask` turn metadata and answer-file path.
   - when `semantic_analysis.evidence_requirements` is present, treat it as the first-pass plan for which published evidence channels to inspect
2. Treat the task as KB-first escalation:
   - run retrieval and trace first
   - inspect `reference_resolution` when the user names a document or locator in user-native terms
   - inspect published text, render, structure, notes, or media artifacts first
   - treat those published artifacts as the primary working surface: draft from retrieved units and artifact sidecars first, inspect cited `focus_render_assets` when visual or tabular semantics matter, and reopen source files only after the published KB has been shown insufficient for the requested deliverable
   - for spreadsheet, chart, table, diagram, PDF-layout, or slide-structure work, read the artifact-aware payload rather than only the unit summary:
     - `matched_artifacts`
     - `focus_render_assets`
     - `recommended_hybrid_targets`
     - `artifact_index.json`
     - `visual_layout/*.json`
     - `spreadsheet_workbook.json`
     - `spreadsheet_sheet/*.json`
     - `pdf_document.json`
     - `semantic_overlay/*.json` when present
  - if the composition task still depends on unresolved hard-artifact semantics, the canonical path must enter the governed ask-time multimodal refresh before any source fallback
     - use `recommended_hybrid_targets` as the only legal narrowing entrypoint
     - write the current-turn `hybrid_refresh_work.json`
     - reuse a matching shared refresh result when the turn is a waiter
     - complete the selected source's current hybrid candidates, then rerun retrieve and trace before drafting the final synthesis
     - if the governed refresh settles `blocked`, stop with `abstained + governed-boundary` instead of improvising around the gap
   - inspect direct source files or rerender only when the published-artifact plan says the knowledge base is insufficient for style, visual structure, or low-level detail
   - bring in external verification or stable model knowledge only when the composition task genuinely needs it, and keep the support basis explicit
3. Start complex work with a visible method or plan summary before diving into the deeper evidence loop.
   - when the workflow enters repository-owned drafting work, record the phase honestly through the hidden run-phase helpers:
     - first drafting pass -> `draft`
     - answer text changed before a follow-on trace -> `rewrite`
     - a later trace over the updated draft -> `retrace`
     - shared confirmation or shared-job waiting -> `retry_wait`
4. Keep the work evidence-backed rather than speculative.
5. For compare or synthesis tasks, keep an explicit support ledger while drafting:
   - which source or unit supports each major claim
   - which artifact supports each visual, tabular, or layout-sensitive claim
   - whether the current support set still lacks balance across compared documents
6. Do not route simple direct factual questions into composition just because the wording is polite or open-ended.
7. Write the main user-facing result to the canonical answer file under `runtime/answers/`.
   - keep that canonical answer file for the final result only, not process chatter
8. When structured drafting or research artifacts help, place them under `runtime/agent-work/<conversation_id>/<turn_id>/`.
   - keep a bundle manifest
   - keep at least one research-notes artifact
   - add draft artifacts when needed
9. Run final provenance tracing over the answer file when the result makes source-grounded claims.
   - do not keep retracing the same unchanged answer text; if the answer-file digest did not change and no new trace or session is needed, stop or reuse the existing final trace instead of silently looping
10. Return the main result plus any relevant bundle paths, support boundary, overall support basis, and next steps to the main agent.

## Escalation Rules

- Do not bypass retrieval and trace just because the task feels like writing rather than answering.
- Do not flatten source-derived evidence and user-memory context together without surfacing source family and trust distinctions.
- If style or visual constraints come from screenshots, preserve that boundary and inspect the stored attachments or renders before finalizing.
- If the result still depends on unresolved design tradeoffs or weak evidence, qualify the output instead of presenting it as settled fact.
- If source-reference resolution is only approximate or unresolved, keep that notice explicit in the composition boundary rather than pretending the cited source was matched exactly.
- If visual or tabular claims are really artifact-level, do not cite only a loose source summary in your internal evidence notes. Carry the artifact grounding through the draft.
- Do not let composition become a catch-all for simple factual lookup. The user should still get the narrowest honest workflow and evidence basis.
- Do not create a growing list of special composition subtypes for odd questions. Prefer the shared evidence-channel model and published affordance layer instead.

## Completion Signal

- The workflow is complete when the main result is written to the canonical answer path, any optional composition bundle artifacts are linked, and the support boundary remains explicit.

## Notes

- This is an inner agent-facing workflow behind `ask`. It is not a public `docmason compose` command.
- Reconciliation-only or operator-direct evidence work does not satisfy this workflow's front-door precondition.
- `grounded-composition` is for evidence-backed white-collar drafting and research, not freeform unsupported creative writing.

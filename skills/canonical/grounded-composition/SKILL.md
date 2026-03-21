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
   - inspect direct source files or rerender only when the published-artifact plan says the knowledge base is insufficient for style, visual structure, or low-level detail
   - bring in external verification or stable model knowledge only when the composition task genuinely needs it, and keep the support basis explicit
3. Start complex work with a visible method or plan summary before diving into the deeper evidence loop.
4. Keep the work evidence-backed rather than speculative.
5. Do not route simple direct factual questions into composition just because the wording is polite or open-ended.
6. Write the main user-facing result to the canonical answer file under `runtime/answers/`.
   - keep that canonical answer file for the final result only, not process chatter
7. When structured drafting or research artifacts help, place them under `runtime/agent-work/<conversation_id>/<turn_id>/`.
   - keep a bundle manifest
   - keep at least one research-notes artifact
   - add draft artifacts when needed
8. Run final provenance tracing over the answer file when the result makes source-grounded claims.
9. Return the main result plus any relevant bundle paths, support boundary, overall support basis, and next steps to the main agent.

## Escalation Rules

- Do not bypass retrieval and trace just because the task feels like writing rather than answering.
- Do not flatten source-derived evidence and user-memory context together without surfacing source family and trust distinctions.
- If style or visual constraints come from screenshots, preserve that boundary and inspect the stored attachments or renders before finalizing.
- If the result still depends on unresolved design tradeoffs or weak evidence, qualify the output instead of presenting it as settled fact.
- If source-reference resolution is only approximate or unresolved, keep that notice explicit in the composition boundary rather than pretending the cited source was matched exactly.
- Do not let composition become a catch-all for simple factual lookup. The user should still get the narrowest honest workflow and evidence basis.
- Do not create a growing list of special composition subtypes for odd questions. Prefer the shared evidence-channel model and published affordance layer instead.

## Completion Signal

- The workflow is complete when the main result is written to the canonical answer path, any optional composition bundle artifacts are linked, and the support boundary remains explicit.

## Notes

- This is an inner agent-facing workflow behind `ask`. It is not a public `docmason compose` command.
- `grounded-composition` is for evidence-backed white-collar drafting and research, not freeform unsupported creative writing.

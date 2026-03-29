---
name: grounded-answer
description: Answer a user question through DocMason's canonical grounded workflow using retrieval, provenance tracing, render escalation, and a final answer-state check.
---

# Grounded Answer

Use this skill when the task is to answer a question from the published DocMason knowledge base rather than only retrieve evidence.

This is the inner specialist answer workflow behind the user-facing `ask` entry surface.
Ordinary users should not need to name this workflow explicitly before asking business questions.

## Front-Door Precondition

- `grounded-answer` is not a free-standing ordinary front door.
- Start only from canonical ask turn metadata and canonical ask runtime ownership.
- Compatible hosts should enter through the repo-provided hidden canonical ask integration path rather than stitching `prepare_ask_turn()` / `complete_ask_turn()` manually.
- If the current turn is missing explicit canonical ask ownership, stop and route back to `ask`.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output
- ability to inspect rendered images when the cited evidence requires visual confirmation

If the agent cannot inspect required rendered evidence, stop and explain that the environment is not capable enough for grounded answering.

## Procedure

1. Start from canonical ask turn metadata instead of assuming every direct answer is purely KB-grounded.
   - treat `answer_state` as the top-level four-state answer contract
   - choose an explicit `support_basis` for the overall answer:
     - `kb-grounded`
     - `external-source-verified`
     - `model-knowledge`
     - `mixed`
   - when the front controller provides `evidence_requirements`, treat them as the canonical odd-question inspection contract
2. Normalize the user question and decompose it only when that improves grounded retrieval.
3. Run `docmason retrieve "<query>" --json` for the initial question or sub-question when KB evidence is part of the answer path.
   - prefer published affordance sidecars and already-published evidence channels over ad hoc source inspection
   - inspect `reference_resolution` first when the user has named a document or locator in user-native terms
   - keep DocMason workspace commands sequential inside the same live answer path; do not overlap `retrieve`, `trace`, `sync`, `status`, or `validate-kb` while a lease-owning step is still running
4. Inspect the strongest evidence bundles, matched units, graph expansions, render references, and published-evidence sufficiency judgment.
   - when `reference_resolution.status` is `exact`, preserve that narrowing and do not let neighboring documents silently dilute it
   - when `reference_resolution.status` is `approximate` but `unit_match_status` is `exact`, preserve the approximate notice but still treat the resolved source narrowing as intentional
   - when `reference_resolution.status` is `approximate` or `unresolved`, keep the inline notice and answer wording honest about that boundary
   - when the question is artifact-sensitive, inspect artifact-aware retrieval payloads explicitly:
     - `matched_artifacts`
     - `matched_artifact_ids`
     - `focus_render_assets`
     - `recommended_hybrid_targets`
     - artifact `section_path`, `caption_text`, `continuation_group_ids`, `procedure_hints`, and `semantic_labels`
     - score details such as `structure_context_bonus`, `semantic_overlay_bonus`, and `compare_coverage_bonus`
   - treat those published payloads as an ordered evidence path: first decide whether retrieved text, structure, notes, or media already settle the claim, then inspect cited `focus_render_assets`, render refs, or page spans when visual confirmation would materially change the answer, and only then escalate
  - when published sufficiency fails because of hard-artifact semantic gaps, the grounded-answer path must enter the governed ask-time multimodal refresh before any raw source inspection
     - use `recommended_hybrid_targets` as the only legal query-aware narrowing entrypoint
     - if the turn becomes a waiter on that governed refresh, keep the same turn paused and reuse the shared result
     - once the governed refresh picks a source, complete that source's current hybrid candidates, reretrieve, and retrace before treating the ask as ready to answer
     - if the governed refresh settles `blocked`, close the turn as `abstained + governed-boundary`
5. Run provenance tracing for the strongest support when you need corroboration, contradiction checks, or answer-state clarification:
   - `docmason trace --source-id <source_id> --json`
   - `docmason trace --answer-file <path> --json`
   - `docmason trace --session-id <session_id> --json`
6. Inspect renders when:
   - the strongest support uses low-confidence extracted text
   - a cited unit has little or no text but does have rendered evidence
   - layout, tables, diagrams, screenshots, or visual style are part of the answer boundary
   - the odd-question plan explicitly prefers `render` or `media`
   - artifact supports expose `render_page_span`, `bbox`, or `normalized_bbox` that materially narrow what must be checked
   - artifact or segment supports expose `focus_render_assets`, which should be preferred over full-page renders when present
   - do not treat every multimodal source as a reason to reopen the raw file; render inspection is for questions whose answer boundary actually depends on visual semantics or a published render-only gap
7. Draft the canonical answer file under `runtime/answers/` when conversation context exists.
   - if you need auxiliary drafts or exported scratch artifacts and the user did not specify a path, place them under `runtime/agent-work/`
8. When the answer is externally verified, persist the lightweight external support manifest before or alongside the final trace so the combined support contract stays machine-readable.
9. Run `docmason trace --answer-file <path> --json` as the final grounding check.
   - when the answer depends on artifact-level support, inspect:
     - `supporting_artifact_ids`
     - segment `artifact_supports`
     - segment `semantic_supports`
     - any overlay `covered_slots`, `blocked_slots`, and consumed render inputs when present
     - render refs, page spans, and region boxes when present
10. Emit one of these final answer states:
   - `grounded`
   - `partially-grounded`
   - `unresolved`
   - `abstained`
11. Return the final answer, final `answer_state`, overall `support_basis`, support boundary, and next steps to the main agent.

## Escalation Rules

- If the final answer trace returns `partially-grounded` or `unresolved`, do not relabel the result as `grounded`.
- If the answer is externally verified or intentionally based on stable model knowledge, do not relabel it as a degraded product failure solely because the final answer is not KB-grounded.
- If the final answer trace still requires render inspection, inspect the cited renders before issuing a confident answer.
- If the evidence is contradictory, ambiguous, or insufficient, qualify the answer or abstain.
- If source-reference resolution only succeeded approximately, do not phrase the answer as though the cited document or locator was matched exactly.
- If the answer is comparative, do not finalize it from a support set dominated by one source when the trace or retrieval payload still shows weak comparative coverage.
- Do not treat retrieval alone as a complete grounded-answer workflow.
- If published KB artifacts already satisfy the required evidence channels, do not rerender source files or rummage through `original_doc/` as a first reflex.

## Completion Signal

- The workflow is complete when the main agent has a final answer text, a final answer state, and an explicit support or uncertainty boundary grounded in retrieval and trace results.

## Notes

- This is an inner agent-facing workflow behind `ask`. It is not a new public `docmason answer` command.
- New runtime artifacts use the same four-state `answer_state` contract:
  - `grounded`
  - `partially-grounded`
  - `unresolved`
  - `abstained`
- `support_basis` remains an explicit orthogonal field. Interpret `answer_state` relative to that declared support basis.
- When a narrower KB-specific grounding view is needed, use trace grounding summaries, segment grounding statuses, and `kb_answer_state` rather than overloading the top-level answer contract.

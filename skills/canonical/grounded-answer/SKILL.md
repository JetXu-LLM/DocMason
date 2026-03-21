---
name: grounded-answer
description: Answer a user question through DocMason's canonical grounded workflow using retrieval, provenance tracing, render escalation, and a final answer-state check.
---

# Grounded Answer

Use this skill when the task is to answer a question from the published DocMason knowledge base rather than only retrieve evidence.

This is the inner specialist answer workflow behind the user-facing `ask` entry surface.
Ordinary users should not need to name this workflow explicitly before asking business questions.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output
- ability to inspect rendered images when the cited evidence requires visual confirmation

If the agent cannot inspect required rendered evidence, stop and explain that the environment is not capable enough for grounded answering.

## Procedure

1. Start from the `ask` front-controller metadata instead of assuming every direct answer is purely KB-grounded.
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
4. Inspect the strongest evidence bundles, matched units, graph expansions, render references, and published-evidence sufficiency judgment.
   - when `reference_resolution.status` is `exact`, preserve that narrowing and do not let neighboring documents silently dilute it
   - when `reference_resolution.status` is `approximate` or `unresolved`, keep the inline notice and answer wording honest about that boundary
5. Run provenance tracing for the strongest support when you need corroboration, contradiction checks, or answer-state clarification:
   - `docmason trace --source-id <source_id> --json`
   - `docmason trace --answer-file <path> --json`
   - `docmason trace --session-id <session_id> --json`
6. Inspect renders when:
   - the strongest support uses low-confidence extracted text
   - a cited unit has little or no text but does have rendered evidence
   - layout, tables, diagrams, screenshots, or visual style are part of the answer boundary
   - the odd-question plan explicitly prefers `render` or `media`
7. Draft the canonical answer file under `runtime/answers/` when conversation context exists.
   - if you need auxiliary drafts or exported scratch artifacts and the user did not specify a path, place them under `runtime/agent-work/`
8. When the answer is externally verified, persist the lightweight external support manifest before or alongside the final trace so the combined support contract stays machine-readable.
9. Run `docmason trace --answer-file <path> --json` as the final grounding check.
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

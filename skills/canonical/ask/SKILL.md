---
name: ask
description: Accept an ordinary user question inside a DocMason workspace, route it to the right inner workflow, and preserve conversation-native logs automatically.
---

# Ask

Use this workflow as the default entry surface for ordinary user questions inside a valid DocMason workspace.

The user should be able to ask naturally.
They should not need to name internal workflow IDs or repository mechanics first.

If the request is clearly an explicit setup, status, sync, adapter-maintenance, or operator-review request, switch to the matching top-level workflow instead of forcing it through `ask`.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output
- ability to inspect rendered images when the answer boundary requires it

If the environment cannot satisfy those capabilities, stop and explain the blocker instead of improvising.

## Procedure

1. Treat one ordinary user message as one canonical `ask` turn.
   - keep one live user question mapped to one canonical turn
   - reuse the live turn when the same question is continuing
   - return in the user's language unless they ask for another language
2. Use the repository helpers in `docmason.ask`, `docmason.front_controller`, and `docmason.conversation` to:
   - reconcile any active native thread
   - open or reuse the canonical turn
   - obtain the canonical answer-file path or composition bundle path
   - pass an agent-authored `semantic_analysis`
   - preserve flat semantic fields such as `question_class`, `question_domain`, `support_strategy`, and `analysis_origin`
   - resolve user-native source references when the user names a document, path, page, slide, sheet, heading, or similar locator
3. Choose the narrowest honest evidence basis before choosing workflow detail.
   - `workspace-corpus` -> KB-first
   - `composition` -> KB-first with explicit evidence planning
   - `external-factual` -> web-first
   - `general-stable` -> model knowledge when the boundary is explicit
4. Check workspace state only when the answer really depends on workspace truth.
   - use `runtime/bootstrap_state.json` as the cached readiness marker
   - allow safe silent bootstrap or repair when the workspace-dependent path can continue safely
   - if fresh workspace state is genuinely needed, let the ask helper govern prepare or sync rather than improvising it in the workflow
   - if prepare or sync becomes a confirmation-required shared job, pause the same turn in `awaiting-confirmation`
     - accept short same-session `yes` or `no` replies as approve or decline
     - `yes` continues the same task
     - `no` commits the same turn as `abstained + governed-boundary`
   - while a turn is in `waiting-shared-job` or `awaiting-confirmation`, do not bypass the governed path by answering from `original_doc/`, `knowledge_base/staging/`, or `.staging-build`
5. Route to the narrowest inner workflow that matches the ask.
   - direct supported answer -> `grounded-answer`
   - evidence-backed drafting, planning, or research -> `grounded-composition`
   - evidence-only request -> `retrieval-workflow`
   - provenance or citation request -> `provenance-trace`
   - runtime review request -> `runtime-log-review`
6. Execute the answer path with published-artifact discipline.
   - keep workspace commands sequential inside the live turn
   - use published KB artifacts first when they already expose the needed evidence channels
   - keep approximate or unresolved reference notices explicit
   - for artifact-sensitive questions, inspect artifact-aware retrieval fields before drafting
   - if published artifacts are still insufficient because of hard-artifact semantic gaps, the canonical ask path must enter one governed narrowed hybrid refresh before any raw source fallback
     - use `recommended_hybrid_targets` as the only legal narrowing entrypoint
     - if the current run becomes the Lane C owner, keep the same turn paused in `waiting-shared-job` until the shared job settles
     - if the current run is a waiter, reuse the same shared Lane C result rather than opening a second path
     - if Lane C settles `blocked`, commit the same turn as `abstained + governed-boundary`
7. Complete the turn through the repository helpers.
   - write only the final answer under `runtime/answers/<conversation_id>/<turn_id>.md`
   - keep scratch work under `runtime/agent-work/` when needed
   - trace the final answer through the shipped trace path
   - let the commit barrier run only after the admissibility gate passes
   - preserve `answer_state`, `support_basis`, optional `support_manifest_path`, and linked session or trace IDs
8. Return the result cleanly.
   - direct answer when supported
   - explicit non-answer boundary when not
   - one concise freshness or waiting note only when it materially helps the user

## Escalation Rules

- Do not require the user to name a skill or repository command before this workflow can run.
- Do not turn natural-language routing into large keyword tables or a growing odd-question taxonomy.
- Do not create a replacement turn when the same-turn confirmation or waiting path is available.
- Do not relabel approximate references as exact.
- Do not treat `external-source-verified`, `model-knowledge`, or `governed-boundary` outcomes as product failures just because the KB path was not fully grounded.
- If published KB artifacts already satisfy the evidence need, do not reopen `original_doc/` or rerender source files by reflex.

## Completion Signal

- The workflow is complete when the user question has been routed through one canonical turn, the resulting logs are linked correctly, and the final answer or explicit boundary has been returned cleanly.

## Notes

- `ask` is the user-facing top-level workflow surface.
- `grounded-answer` and `grounded-composition` remain inner specialist workflows.
- If the ask path performs a narrowed hybrid refresh, keep it mostly silent unless the wait is no longer brief or the turn must surface a real boundary.

---
name: ask
description: Accept an ordinary user question inside a DocMason workspace, route it to the right inner workflow, and preserve conversation-native logs automatically.
---

# Ask

Use this workflow as the default entry surface for ordinary user questions inside a valid DocMason workspace.

The user should be able to ask naturally.
They should not need to name internal workflow IDs or repository mechanics first.

If the request is clearly an explicit setup, status, sync, adapter-maintenance, or operator-review request, switch to the matching top-level workflow instead of forcing it through `ask`.

## Front-Door Law

- Reading this skill is not legal ask execution.
- Native-thread reconciliation is not legal ask execution.
- Only canonical ask runtime ownership counts as ordinary front-door execution.
- Direct evidence commands such as public `retrieve` or `trace` remain legal operator tools, but they do not complete the ordinary ask contract by themselves.

`ask` owns front-door legality, same-turn governance, workspace gating, and routing.
The routed inner workflow owns the deeper evidence loop.

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
   - when the same live turn and the same active run are re-entered, reuse the existing governed preanswer result instead of restarting preanswer governance
   - return in the user's language unless they ask for another language
2. Use the repository helpers in `docmason.ask`, `docmason.front_controller`, and `docmason.conversation` to:
   - reconcile any active native thread
   - keep native reconciliation in the native ledger and interaction-ingest path by default
   - open or reuse the canonical turn
   - for adapter-owned or compatible host execution, use the repo-provided hidden canonical ask integration path rather than calling lifecycle helpers directly
   - keep canonical ask truth separate from native-ledger audit truth unless an explicit bridge or promotion is required
   - obtain the canonical answer-file path or composition bundle path
   - pass an agent-authored `semantic_analysis`
   - preserve flat semantic fields such as `question_class`, `question_domain`, `support_strategy`, and `analysis_origin`
   - keep one concise `route_reason`
   - set `needs_latest_workspace_state` when fresh local workspace truth is actually required
   - include compact `evidence_requirements` when odd or artifact-sensitive questions need channel guidance
   - resolve user-native source references when the user names a document, path, page, slide, sheet, heading, or similar locator
3. Choose the narrowest honest evidence basis before choosing workflow detail.
   - `workspace-corpus` -> KB-first
   - `composition` -> KB-first with explicit evidence planning
   - `external-factual` -> web-first
   - `general-stable` -> model knowledge when the boundary is explicit
4. Check workspace state only when the answer really depends on workspace truth.
   - use `runtime/bootstrap_state.json` as the cached readiness marker
   - treat workspace-dependent ask as legal only when the prepared environment is `self-contained`
   - if the environment is `mixed` or `degraded`, let the ask helper repair or surface the governed boundary instead of answering from a partially trusted runtime
   - allow safe silent bootstrap or repair when the workspace-dependent path can continue safely
   - if the environment is ready but no published knowledge base exists yet, route to `knowledge-base-sync` instead of bluffing a workspace-grounded answer
   - if the published knowledge base is stale but still usable, answer from the published corpus with one concise freshness notice
   - if fresh workspace state is genuinely needed, let the ask helper govern prepare or sync rather than improvising it in the workflow
   - do not turn simple exact-source asks into answer-critical sync just because pending interaction promotion backlog exists
   - when the user has already narrowed to one exact source or unit, treat pending interaction backlog as an advisory notice unless the current turn truly depends on interaction-derived evidence
   - when workspace freshness depends on live local files, use repo-side live corpus discovery for `original_doc/` rather than git-tracked repo search
   - if prepare or sync becomes a confirmation-required shared job, pause the same turn in `awaiting-confirmation`
     - accept short same-session `yes` or `no` replies as approve or decline
     - `yes` continues the same task
     - `no` commits the same turn as `abstained + governed-boundary`
   - if the same-session turn is already `prepared`, `awaiting-confirmation`, or `waiting-shared-job`, prefer reusing that governed state over rerunning shared-state mutation
   - while a turn is in `waiting-shared-job` or `awaiting-confirmation`, do not bypass the governed path by answering from `original_doc/`, `knowledge_base/staging/`, or `.staging-build`
5. Route to the narrowest inner workflow that matches the ask.
   - direct supported answer -> `grounded-answer`
   - evidence-backed drafting, planning, or research -> `grounded-composition`
   - evidence-only request -> `retrieval-workflow`
   - provenance or citation request -> `provenance-trace`
   - runtime review request -> `runtime-log-review`
6. Route into the chosen inner workflow and let it own the evidence loop.
   - keep workspace commands sequential inside the live turn
   - do not treat `retrieve`, `trace`, or direct helper calls as a substitute for canonical ordinary ask execution
   - use published KB artifacts first when they already expose the needed evidence channels
   - treat the published KB as the primary evidence surface: inspect retrieved text, structure, notes, media, and artifact metadata first, then inspect cited `focus_render_assets` or render spans when the question is genuinely visual or layout-sensitive, and only then consider governed refresh or source fallback
   - keep approximate or unresolved reference notices explicit
   - let the routed inner workflow own retrieval, trace, render inspection, and answer or composition drafting
   - if published artifacts are still insufficient because of hard-artifact semantic gaps, let the canonical routed path enter one governed narrowed hybrid refresh instead of improvising raw source fallback
   - if that governed path becomes a shared wait or blocked boundary, keep the same turn paused or committed through the existing ask control-plane states rather than opening a side path
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
- A reconciled native turn is not yet a legal canonical ask turn until runtime ownership is opened explicitly.
- Native reconciliation does not write canonical conversation truth by default; it lands in native ledger and interaction-ingest first.
- Canonical ask may later link to native-ledger evidence through explicit promotion or bridge metadata when the governed path requires it.
- `grounded-answer` and `grounded-composition` remain inner specialist workflows.
- Tracked repo search, live corpus discovery, knowledge-base artifact discovery, and runtime
  artifact discovery are different surfaces; do not substitute one for another silently.
- If the ask path enters a governed narrowed hybrid refresh, keep the transition concise unless the turn must surface a real wait or boundary.

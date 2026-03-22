---
name: ask
description: Accept an ordinary user question inside a DocMason workspace, route it to the right inner workflow, and preserve conversation-native logs automatically.
---

# Ask

Use this workflow as the default entry surface for ordinary user questions inside a valid DocMason workspace.

The user should be able to ask naturally.
They should not need to name `grounded-answer`, `grounded-composition`, `retrieval-workflow`, `provenance-trace`, `runtime-log-review`, or intermediate repository mechanics.

If the request is clearly an explicit setup, status, sync, adapter-maintenance, or operator-review request rather than an ordinary question, route to the matching top-level workflow instead of forcing it through `ask`.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output
- ability to inspect rendered images when the answer boundary requires it

If the environment cannot satisfy those capabilities, stop and explain the blocker instead of improvising.

## Procedure

1. Treat an ordinary freeform user question as an `ask` turn by default.
2. Let the user speak in any language.
   - Answer in the user's language by default unless they ask for another output language.
   - When retrieval or trace quality would improve, reformulate the working query in the user language, the likely source-document language, or both.
3. Use the repository helpers in `docmason.ask`, `docmason.front_controller`, and `docmason.conversation` to:
   - treat `runtime/bootstrap_state.json` as the cached environment-ready marker for
     workspace-dependent asks
   - if that marker says the current root is ready and `.venv` still exists, continue without a
     deep bootstrap check
   - if it is missing, stale after a repo move, or clearly non-ready, let the ask path trigger a
     silent bootstrap or repair attempt before surfacing manual setup work
   - reconcile any active native thread when available
   - open or continue exactly one canonical turn for the user question
   - obtain the canonical answer-file path
   - obtain the composition bundle path when the task is composition-style rather than direct QA
   - resolve user-native source references implicitly when the question names a document, path, page, slide, sheet, heading, or similar locator
   - pass an agent-authored structured `semantic_analysis` object rather than relying on repo-side keyword classification
   - classify both `question_class` and `question_domain`
   - obtain `support_strategy`, routing guidance, and workflow linkage metadata
   - the semantic analysis should normally include:
     - `question_class`
     - `question_domain`
     - optional `support_strategy`
     - optional `needs_latest_workspace_state`
     - optional `memory_query_profile`
     - optional `evidence_requirements`
     - one concise `route_reason`
   - when the question is odd, visual, structural, media-oriented, or otherwise not a narrow factual lookup, the semantic analysis should usually include compact `evidence_requirements`:
     - `preferred_channels`
     - `inspection_scope`
     - `prefer_published_artifacts`
4. When native chat history is available, reconcile the active thread back into DocMason conversation state before answering.
   - Preserve prior real user turns, screenshots, and tool-use audits instead of treating the current message as an isolated one-shot prompt.
5. Choose the evidence basis before choosing the amount of repo mechanics to expose:
   - `workspace-corpus` -> `KB-first`
   - `external-factual` -> `web-first`
   - `general-stable` -> model knowledge first, then web only when needed for honesty
   - `composition` -> `KB-first escalation` plus explicit multi-channel evidence when needed
   - for odd workspace questions, first choose the required published evidence channels such as `text`, `render`, `structure`, `notes`, or `media` before escalating to source files
6. Check workspace state only when the chosen evidence basis actually depends on the workspace:
   - if a workspace- or corpus-dependent answer has no published knowledge base, route internally to `workspace-bootstrap` or `knowledge-base-sync` and explain that boundary in user-facing terms
   - if the knowledge base is stale and still usable, keep the answer path honest and attach one concise freshness notice
   - if the question is `workspace-corpus`, the environment is ready, and fresh local state is genuinely needed, let the routed ask helper run its concise auto-sync path before answering
   - if the user explicitly asks about newly added or latest local documents, prefer that ask-path auto-sync before answering rather than guessing from stale current state
   - if pending interaction-derived knowledge is relevant and still awaits sync-time promotion, let the ask-path auto-sync promote it when that pending knowledge matters to the current answer path
   - suppress workspace freshness and sync notices for ordinary `external-factual` and `general-stable` questions unless the workspace is genuinely part of the evidence chain
7. Route to the best inner workflow:
   - ordinary business question -> `grounded-answer`
   - evidence-backed drafting, planning, or research output -> `grounded-composition`
   - evidence-only request -> `retrieval-workflow`
   - provenance or citation request -> `provenance-trace`
   - runtime failure or activity review request -> `runtime-log-review`
8. Shape the user-facing response according to complexity:
   - for simple factual questions, start with one brief method sentence, then give the direct answer
   - for complex research or composition tasks, first explain the plan or method, then perform the deeper evidence loop
9. When answering:
   - decompose the question only when needed
   - run repeated retrieval and trace steps silently as needed
   - let `ask` and `retrieve` keep the front-end simple by parsing user-native source references implicitly rather than asking the user for internal source IDs
   - if the shared resolver marks a reference as `approximate` or `unresolved`, keep the inline notice explicit and do not quietly relabel it as exact
   - allow pending interaction-derived overlay knowledge to participate only when relevant to the current domain and support strategy
   - inspect published renders, structure sidecars, notes, or media first when the chosen evidence channels point there
   - inspect source files or rerender only when the published-artifact plan says the KB is insufficient
   - write only the final answer under `runtime/answers/<conversation_id>/<turn_id>.md`
   - keep progress chatter, shell correction, and self-talk out of canonical answer files and interaction excerpts
   - if you need auxiliary drafts or exported scratch artifacts and the user did not specify a path, place them under `runtime/agent-work/`
   - for composition-style work, keep a bundle manifest and research notes under `runtime/agent-work/<conversation_id>/<turn_id>/`
   - when the final answer is externally verified, write a lightweight external support manifest under `runtime/agent-work/<conversation_id>/<turn_id>/`
   - run the final `docmason trace --answer-file <path> --json`
10. Ensure retrieval and trace logs inherit the active `conversation_id`, `turn_id`, `entry_workflow_id`, `inner_workflow_id`, and the flat semantic contract fields such as `question_class`, `question_domain`, `support_strategy`, and `analysis_origin`.
   - The repository commands can inherit that linkage automatically through:
     - `DOCMASON_CONVERSATION_ID`
     - `DOCMASON_TURN_ID`
     - `DOCMASON_ENTRY_WORKFLOW_ID`
     - `DOCMASON_INNER_WORKFLOW_ID`
11. Complete the turn by updating the conversation record with:
   - routed inner workflow
   - linked `session_id` values
   - linked `trace_id` values
   - answer-file path
   - `question_class`
   - `question_domain`
   - `support_strategy`
   - `analysis_origin`
   - `reference_resolution`
   - `reference_resolution_summary`
   - final `answer_state`
   - final `support_basis`
   - optional `support_manifest_path`
   - render-inspection requirement
   - whether sync was suggested or requested
12. Return the operator-facing result with:
   - final answer or explicit non-answer boundary
   - routed workflow
   - `answer_state` when applicable
   - `support_basis` when applicable
   - whether render inspection is required
   - concise freshness guidance when relevant

## Escalation Rules

- Do not require the user to name a skill or repository command before this workflow can run.
- Do not push a workspace-dependent first ask into manual setup immediately when a safe silent
  bootstrap or repair attempt can finish the work.
- Do not require the user to pivot into internal source IDs when a normal file-plus-locator reference is enough for the shared resolver to work.
- Do not push natural-language semantic routing down into large deterministic keyword lists. The main agent should make the semantic judgment and pass it into the repo helpers explicitly.
- Do not build a growing odd-question taxonomy. Route those questions by evidence needs and published evidence channels instead.
- Do not run mutating sync during an ordinary answer path unless the routed ask helper has already determined that the current `workspace-corpus` question needs fresh local state; when it does, keep the transition concise and auditable.
- If the current published corpus is stale but still usable, answer from the published corpus first and append one concise freshness notice.
- If the user explicitly needs latest local document state, prefer the ask helper's auto-sync path before answering rather than guessing.
- If the user message is clearly a direct setup, status, sync, adapter, or runtime-review request, switch to the matching top-level workflow instead of overloading `ask`.
- Do not force the user into English-only interaction. Match the user-facing answer language unless they explicitly ask otherwise.
- If the final answer trace is not `grounded`, preserve that KB boundary honestly instead of relabeling it.
- If the shared source-reference resolver returns `approximate` or `unresolved`, keep that notice explicit in the answer path and let the persisted runtime artifacts remain auditable.
- Do not treat `external-source-verified` or `model-knowledge` answers as product failures just because KB `answer_state` is not `grounded`.
- Keep one user question mapped to one canonical turn. Re-entering helper layers inside the same live question should reuse the open turn rather than creating a second canonical answer.
- If published KB artifacts already satisfy the required evidence channels, do not go back to `original_doc/` or rerender source files as a first move.

## Completion Signal

- The workflow is complete when the user question has been routed, the resulting logs are linked to a parent conversation turn, and the final answer or explicit boundary has been returned cleanly.

## Notes

- `ask` is the user-facing top-level workflow surface.
- `grounded-answer` remains the inner specialist workflow for direct supported answers.
- `grounded-composition` is the inner specialist workflow for evidence-backed drafting, planning, and research outputs.
- `@ask` may be used as an optional shortcut in platforms that support it, but the primary path is natural freeform asking inside the workspace.

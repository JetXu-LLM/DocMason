---
name: ask
description: Accept an ordinary user question inside a DocMason workspace, route it to the right inner workflow, and preserve conversation-native logs automatically.
---

# Ask

`ask` is the canonical skill at `skills/canonical/ask/SKILL.md`.
It is the user-facing top-level workflow for ordinary natural-language requests.
Use this skill as the default top-level workflow for a new ordinary user request in this workspace unless the request is clearly explicit operator work.

The user should be able to ask naturally.
They should not need to name internal workflow IDs or repository mechanics first.

If the request is clearly an explicit setup, status, sync, adapter-maintenance, or operator-review request, switch to the matching top-level workflow instead of forcing it through `ask`.

## Front-Door Law

- Reading this skill is not legal ask execution.
- Native-thread reconciliation is not legal ask execution.
- `ask` itself remains the only ordinary natural-language front door.
- A request counts as ordinary front-door execution only after it has been opened as a canonical ask turn under this workflow.
- `Plan Mode` or a similar host planning/collaboration mode should not discourage the host from opening canonical `ask` for an ordinary user request; if the current mode still blocks `ask`, the host should use a concise in-band alert or approval question and continue the same user task in the most natural available way rather than silently defaulting to operator-direct or side-path work.
- This skill contains the canonical ordinary-ask opening and completion contract for compatible hosts. Generated adapter guidance may restate that contract, but it does not replace this workflow surface.
- Compatible hosts must use the repo-provided host integration surface rather than stitching internal ask helpers or side paths together themselves.
- Compatible hosts must not treat ad hoc probing of internal ask surfaces, ad hoc Python snippets, or direct lifecycle-helper calls as legal ask entry.
- Direct evidence commands such as public `retrieve` or `trace` remain legal operator tools, but they do not complete the ordinary ask contract by themselves.

`ask` owns front-door legality, same-turn governance, workspace gating, and routing.
The routed inner workflow owns the deeper evidence loop.

## Turn Terms

- `native turn` means the host's own chat turn before DocMason opens canonical ask handling.
- `canonical ask turn` means the governed DocMason turn for the current request.
- `runtime ownership` means the current request has been opened into that canonical ask turn and is now governed by DocMason.
- `native ledger` means host-side audit capture that is not canonical ask truth by itself.
- `interaction-ingest` means the runtime holding area for reconciled host activity before any governed promotion.
- A practical sign that canonical ask really opened is that the request leaves linked runtime artifacts rather than only a host-visible reply, typically under `runtime/answers/`, `runtime/runs/`, and `runtime/logs/`.

## Canonical Ask Contract

- This section is the authoritative ordinary-ask execution contract for compatible hosts.
- `workflow.json` remains routing metadata. It does not define the executable host call contract.
- A request counts as an opened canonical ask turn only when both are true:
  - the supported ask entry surface has returned stable `conversation_id`, `turn_id`, `run_id`, `answer_file_path`, and `log_context`
  - the underlying turn has been upgraded to `front_door_state = canonical-ask`
- For compatible-host execution, the supported ask entry surface is the hidden host wrapper:

```bash
cat <<'JSON' | ./.venv/bin/python -m docmason _ask
{ ...payload... }
JSON
```

- Hidden wrapper actions:
  - `open`
  - `progress`
  - `finalize`
- `open` request envelope:

```json
{
  "action": "open",
  "question": "<user question>",
  "host_provider": "<provider>",
  "host_thread_ref": "<stable host thread ref>",
  "host_identity_source": "<host identity source>",
  "semantic_analysis": { "...": "..." }
}
```

- After `open`, preserve the returned `conversation_id`, `turn_id`, `run_id`, `answer_file_path`, and `log_context` for the rest of the same canonical ask turn.
- Hidden wrapper status meanings:
  - `execute` means canonical ask is open and the host should continue through the chosen inner workflow.
  - `awaiting-confirmation` means pause the same turn and wait for the user's confirmation reply.
  - `waiting-shared-job` means pause the same turn and wait for governed shared-job settlement.
  - `completed` means the turn is committed and a final business answer may be returned.
  - `boundary` means the turn is committed as a governed boundary and that boundary reply may be returned.
  - `blocked` means no final business answer may be returned yet.
- In compatible-host execution, `open` or same-turn reuse already performs governed preanswer work:
  - question classification and support-strategy selection
  - workspace gating and knowledge-base freshness checks
  - initial inner-workflow routing plus any confirmation or waiting-state settlement
- After `open`, treat the returned `status`, `inner_workflow_id`, `support_strategy`, `reference_resolution`, `source_scope_policy`, and notices as the source of truth for the next step. Do not re-derive them from side-path probing before honoring that result.
- Retrieve / trace binding rule:
  - when using public `docmason retrieve` or `docmason trace` inside the same canonical ask turn, export each returned `log_context` field as `DOCMASON_<FIELD>` and then call the public command normally
  - do not switch to direct Python helpers such as `prepare_ask_turn()`, `complete_ask_turn()`, or `trace_answer_file(...)` as a substitute for the supported path
- Example retrieve / trace binding:

```bash
export DOCMASON_CONVERSATION_ID="<conversation_id>"
export DOCMASON_TURN_ID="<turn_id>"
export DOCMASON_RUN_ID="<run_id>"
export DOCMASON_ENTRY_WORKFLOW_ID="ask"
export DOCMASON_INNER_WORKFLOW_ID="<inner_workflow_id>"
export DOCMASON_FRONT_DOOR_STATE="canonical-ask"

./.venv/bin/python -m docmason retrieve "<query>" --json
./.venv/bin/python -m docmason trace --answer-file "<answer_file_path>" --json
```

- `progress` request envelope:

```json
{
  "action": "progress",
  "conversation_id": "<conversation_id>",
  "turn_id": "<turn_id>",
  "completion_status": "covered | blocked",
  "hybrid_refresh_summary": { "...": "..." }
}
```

- `finalize` request envelope:

```json
{
  "action": "finalize",
  "conversation_id": "<conversation_id>",
  "turn_id": "<turn_id>",
  "answer_file_path": "<answer_file_path>",
  "response_excerpt": "<short excerpt>"
}
```

- Completion rule:
  - only `completed` or `boundary` permits a final business reply to the user
  - `execute`, `awaiting-confirmation`, `waiting-shared-job`, and `blocked` do not

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
2. Use the repository helpers in `docmason.ask`, `docmason.front_controller`, and `docmason.conversation` for two responsibilities:
   - open or reuse the canonical ask turn:
     - reconcile any active native thread
     - keep native reconciliation in the native ledger and interaction-ingest path by default
     - open or reuse the canonical turn
     - for adapter-owned or compatible host execution, use the supported ask entry surface defined in `Canonical Ask Contract` rather than direct internal helper calls or ad hoc snippets
     - keep canonical ask truth separate from native-ledger audit truth unless an explicit bridge or promotion is required
   - preserve the required turn state and metadata:
     - obtain the canonical answer-file path or composition bundle path
     - pass an agent-authored `semantic_analysis`
     - preserve flat semantic fields such as `question_class`, `question_domain`, `support_strategy`, and `analysis_origin`
     - keep one concise `route_reason`
     - set `needs_latest_workspace_state` when fresh local workspace truth is actually required
     - include compact `evidence_requirements` when odd or artifact-sensitive questions need channel guidance
     - resolve user-native source references when the user names a document, path, page, slide, sheet, heading, or similar locator
3. During `open` or same-turn reuse, let the governed ask path choose the smallest evidence basis that can support the answer correctly and truthfully before deeper workflow execution.
   - `workspace-corpus` -> KB-first
   - `composition` -> KB-first with explicit evidence planning
   - `external-factual` -> web-first
   - `general-stable` -> model knowledge when the boundary is explicit
4. During that same governed preanswer step, check workspace state only when the answer really depends on workspace truth.
   - use `runtime/bootstrap_state.json` as the cached readiness marker
   - treat workspace-dependent ask as legal only when the prepared environment is `self-contained`
     - `self-contained` means the repo-local steady-state runtime is trusted for ordinary ask work
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
5. When governed preanswer returns `status = execute`, let the canonical ask turn continue through the narrowest ask-routed inner workflow that matches the ask.
   - if the same turn is still paused, blocked, or has been routed to `workspace-bootstrap` or `knowledge-base-sync`, honor that governed state first instead of forcing one of the ask-routed evidence workflows
   - for answer-eligible ask-time execution, the workflow must choose the most appropriate one of the following 5 inner workflows and continue through that path
   - direct supported answer -> `grounded-answer`
   - evidence-backed drafting, planning, or research -> `grounded-composition`
   - evidence-only request -> `retrieval-workflow`
   - provenance or citation request -> `provenance-trace`
   - runtime review request -> `runtime-log-review`
6. Route into the chosen ask-routed inner workflow and let it own the evidence loop.
   - keep workspace commands sequential inside the live turn
   - keep the same canonical turn ownership through the inner workflow instead of reopening the question from side paths
   - do not treat `retrieve`, `trace`, direct helper calls, ad hoc internal-surface probing, or raw-source inspection as a substitute for canonical ordinary ask execution
   - use published KB artifacts first when they already expose the needed evidence channels
   - treat published-KB answering as the ordinary ask path; governed Lane B follow-up remains sync/publication-owned and may surface as freshness or degradation notice, but it is not a separate host-driven ask step
   - treat the published KB as the primary evidence surface: inspect retrieved text, structure, notes, media, and artifact metadata first, then inspect cited `focus_render_assets` or render spans when the question is genuinely visual or layout-sensitive, and only then consider governed refresh or source fallback
   - keep approximate or unresolved reference notices explicit
   - let the routed inner workflow own retrieval, trace, render inspection, and answer or composition drafting
   - if published artifacts are still insufficient because of hard-artifact semantic gaps, let the canonical routed path enter one governed narrowed hybrid refresh instead of improvising raw source fallback
     - this ask-owned narrowed hybrid refresh is the Lane C path and is the only ordinary ask follow-up settled through hidden `progress`
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

- `ask` is the canonical skill at `skills/canonical/ask/SKILL.md` and the user-facing top-level workflow surface.
- A reconciled native turn is still only host-side context until the matching canonical ask turn has been opened.
- Native reconciliation does not write canonical conversation truth by default; it lands in native ledger and interaction-ingest first.
- Canonical ask may later link to native-ledger evidence through explicit promotion or bridge metadata when the governed path requires it.
- `grounded-answer` and `grounded-composition` remain inner specialist workflows.
- Tracked repo search, live corpus discovery, knowledge-base artifact discovery, and runtime
  artifact discovery are different surfaces; do not substitute one for another silently.
- If the ask path enters a governed narrowed hybrid refresh, keep the transition concise unless the turn must surface a real wait or boundary.

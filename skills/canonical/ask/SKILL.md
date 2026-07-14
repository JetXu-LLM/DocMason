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
- `ask` itself remains the only ordinary natural-language front door, and a request counts as ordinary front-door execution only after it has been opened as a canonical ask turn under this workflow.
- `Plan Mode` or a similar host planning/collaboration mode should not discourage the host from opening canonical `ask` for an ordinary user request; if the current mode still blocks `ask`, the host should use a concise in-band alert or approval question and continue the same user task in the most natural available way rather than silently defaulting to operator-direct or side-path work.
- See `Canonical Ask Contract` below for the exact ordinary-ask opening and completion rules. Generated adapter guidance may restate that contract, but it does not replace this workflow surface, and compatible hosts must use that supported entry surface rather than helper stitching, ad hoc probing, or direct lifecycle calls.
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
  "semantic_analysis": {
    "question_class": "answer",
    "question_domain": "workspace-corpus",
    "route_reason": "<one concise reason>"
  }
}
```

- Minimal host hinting rule:
  - `semantic_analysis` is best-effort. For an ordinary native Codex ask, `question_class`, `question_domain`, and one concise `route_reason` are usually enough.
  - classify by deliverable and evidence basis, not by perceived difficulty:
    - `question_class` chooses workflow shape. It is not a difficulty score: use `answer` for a direct answer, explanation, or source-backed summary; use `composition` for a new evidence-backed work product or synthesis; use `retrieval`, `provenance`, or `runtime-review` only for explicit evidence, citation, or runtime-review requests.
    - `question_domain` chooses evidence basis. It is not a mirror of `question_class`: prefer `workspace-corpus`, `external-factual`, or `general-stable` when one of those is the real basis, and reserve `composition` for composition-shaped evidence planning rather than setting it merely because `question_class = composition`.
    - a compare, draft, or plan request over workspace materials is therefore usually `question_class = composition` with `question_domain = workspace-corpus`.
  - `open` normalizes missing supported routing fields, derives defaults such as `support_strategy`, and may refine reference resolution or workspace notices from repository truth.
- Native Codex fast path:
  - for an ordinary request on the native Codex path, once repo-local `.venv` is available, call hidden `open` directly with best-effort `semantic_analysis`
  - prefer the real native `CODEX_THREAD_ID` identity from the execution environment; do not hand-fill placeholder host thread references such as `codex-desktop-thread` or `codex-native-thread`
  - do not read other workflow skills, `workspace-status`, `workspace-bootstrap`, source search, implementation source, or tests first just to decide whether canonical ask may open, whether a named source exists, or which `semantic_analysis` fields are accepted
  - use those surfaces only after `open` returns a governed blocker, waiting state, or explicit operator route
- After `open`, preserve the returned `conversation_id`, `turn_id`, `run_id`, `answer_file_path`, `log_context`, and `support_contract` for the rest of the same canonical ask turn.
- Hidden wrapper status meanings:
  - `execute` means canonical ask is open and the host should continue through the chosen inner workflow.
  - `awaiting-confirmation` means pause the same turn and wait for the user's confirmation reply.
  - `awaiting-user-decision` means pause the current work for a material decision that only
    the user can authorize. It never implies a timeout, default, or skip.
  - `waiting-shared-job` means pause the same turn and wait for governed shared-job settlement.
  - `completed` means the turn is committed and a final business answer may be returned.
  - `boundary` means the turn is committed as a governed boundary and that boundary reply may be returned.
  - `blocked` means no final business answer may be returned yet.
- Hidden wrapper `next_step` is a derived convenience field and should stay aligned with that status law:
  - `execute -> continue-inner-workflow`
  - `awaiting-confirmation -> wait-for-user-confirmation`
  - `awaiting-user-decision -> wait-for-user-decision`
  - `waiting-shared-job -> wait-for-shared-job`
  - `completed -> return-final-answer`
  - `boundary -> return-boundary-answer`
  - `blocked -> do-not-return-final-answer`
- Hidden wrapper `result_explanation` is a derived convenience field, not a new truth surface.
  - When `result_explanation.show_to_user = true` for a blocker or evidence boundary, translate its `summary`, `why`, and `next_step` into one concise user-facing explanation in the user's language.
  - Do not append it to a successful copy-ready artifact; successful completion returns only the exact business answer plus the separate `user_status_line`.
  - When `show_to_user = false`, do not add extra result-explanation prose.
  - Do not write `result_explanation` text into the canonical answer markdown.
- Hidden wrapper `admissibility_repair` is present only for same-turn repairable finalize failures.
  - Treat it as repair metadata for the next rewrite/retrace attempt, not as permission to bypass trace or admissibility.
- In compatible-host execution, `open` or same-turn reuse already performs governed preanswer work:
  - question classification and support-strategy selection
  - workspace gating and knowledge-base freshness checks
  - initial inner-workflow routing plus any confirmation or waiting-state settlement
- After `open`, treat the returned `status`, `question_class`, `question_domain`, `analysis_origin`, `route_reason`, `inner_workflow_id`, `support_strategy`, `reference_resolution`, `source_scope_policy`, `support_contract`, and notices as the source of truth for the next step. Do not re-derive them from side-path probing before honoring that result.
- Retrieve / trace binding rule:
  - when using public `docmason retrieve` or `docmason trace` inside the same canonical ask turn, export each returned `log_context` field as `DOCMASON_<FIELD>` and then call the public command normally
  - in chat-host execution, prefer `--json --compact` for interactive inspection; if full nested retrieve or trace detail is genuinely needed, redirect full `--json` to a local file and inspect it selectively instead of loading the raw payload straight into the live chat context
  - treat compact payloads as the stable host-facing inspection contract; do not rebuild alternate schemas with ad hoc `jq` assumptions such as `.matches`
  - compact retrieve inspection should normally start from:
    - `session_id`
    - `results`
    - `reference_resolution`
    - `source_scope_policy`
  - compact trace inspection should normally start from:
    - `trace_id`
    - `session_id`
    - `answer_state`
    - `reference_resolution`
    - `source_scope_policy`
    - `issue_codes`
  - do not switch to direct Python helpers such as `prepare_ask_turn()`, `complete_ask_turn()`, or `trace_answer_file(...)` as a substitute for the supported path
- Example retrieve / trace binding:

```bash
export DOCMASON_CONVERSATION_ID="<conversation_id>"
export DOCMASON_TURN_ID="<turn_id>"
export DOCMASON_RUN_ID="<run_id>"
export DOCMASON_ENTRY_WORKFLOW_ID="ask"
export DOCMASON_INNER_WORKFLOW_ID="<inner_workflow_id>"
export DOCMASON_FRONT_DOOR_STATE="canonical-ask"

./.venv/bin/python -m docmason retrieve "<query>" --json --compact
./.venv/bin/python -m docmason trace --answer-file "<answer_file_path>" --json --compact
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

- A material professional judgment may instead persist a decision gate:

```json
{
  "action": "progress",
  "conversation_id": "<conversation_id>",
  "turn_id": "<turn_id>",
  "decision_frontier": {
    "class": "judgment-authority-gap | high-cost-expression-choice",
    "question": "<material decision question>",
    "why_user_is_required": "<why evidence and reasoning cannot decide it>",
    "evidence_boundary": "<what evidence establishes and cannot decide>",
    "options": [
      {
        "id": "<stable option id>",
        "label": "<short label>",
        "impact": "<real downstream consequence>",
        "recommended": true
      },
      {
        "id": "<stable option id>",
        "label": "<short label>",
        "impact": "<real downstream consequence>",
        "recommended": false
      }
    ],
    "affected_outputs": ["<specific model or artifact scope>"],
    "invalidates": []
  }
}
```

- A legal gate has two or three real options, explains their downstream impact, marks exactly
  one current recommendation, and names the affected output scope. Do not persist a vague or
  non-material question.
- After `progress` returns `awaiting-user-decision`, present the same gate through the host's
  native Plan or structured-input UI, without a timeout or default, and wait. If no such UI is
  available, ask the one material question concisely in-band.
- The user's answer opens a new linked turn with `continuation_type=decision-resolution`, the
  exact `gate_id` as `resolves_gate_id`, and either one legal `option_id` or a non-empty
  `free_form` decision. Never settle the old turn in place or reinterpret an unrelated message
  as consent.

- `completion_status` is optional when the caller is only re-entering a `waiting-shared-job` turn to let the hidden wrapper reconcile deterministic repo-owned shared-job truth.
- supply `completion_status` only when the host is actively settling a still-unsettled governed multimodal refresh.
- when the current-turn `hybrid_refresh_work.json` lists render or focus-render assets and the host can inspect images, inspect the relevant assets lightly and include `render_inspection_used` plus `inspected_render_assets` in `hybrid_refresh_summary`.
- when a turn is paused in `waiting-shared-job`, re-enter through hidden `open` reuse, hidden `progress`, or hidden `finalize`; do not grep `runtime/control_plane/` or shared-job files manually.

- `finalize` request envelope:

```json
{
  "action": "finalize",
  "conversation_id": "<conversation_id>",
  "turn_id": "<turn_id>",
  "answer_text": "<exact final business answer>",
  "answer_file_path": "<answer_file_path>",
  "response_excerpt": "<short excerpt>",
  "session_ids": ["<selected_session_id>"],
  "trace_ids": ["<selected_trace_id>"],
  "workflow_outcome": {
    "support_basis": "kb-grounded | mixed | external-source-verified | model-knowledge | governed-boundary",
    "session_ids": ["<selected_session_id>"],
    "trace_ids": ["<selected_trace_id>"],
    "bundle_paths": ["<composition bundle path>"]
  }
}
```

- `answer_text` is the preferred terminal input. The wrapper writes it atomically to the
  canonical answer path, binds the exact digest, reuses only a digest-matching trace, and runs
  one exact trace when necessary. The successful response keeps `answer_text` clean and returns
  one separate `user_status_line`; internal IDs and support state remain in `governance_detail`.
  When the exact final answer is already present at `answer_file_path`, callers may omit
  `answer_text` and use the file-based handshake below.

- `session_ids` and `trace_ids` are optional advanced-caller fields. Omit them when the turn has exactly one ask-owned retrieve session and one final trace candidate. Supply them only when the caller has already selected the canonical pair among multiple ask-owned candidates.
- `workflow_outcome` is the preferred finalize-time handoff for workflow-owned facts. Supply it when the inner workflow already knows the correct `support_basis`, selected `session_ids` / `trace_ids`, support-manifest linkage, bundle linkage, or bounded degradation metadata. Older callers may keep using the compatible top-level finalize fields.
- Legal closure handshake for one canonical ask turn:

```bash
./.venv/bin/python -m docmason trace --answer-file "<answer_file_path>" --json --compact

cat <<'JSON' | ./.venv/bin/python -m docmason _ask
{
  "action": "finalize",
  "conversation_id": "<conversation_id>",
  "turn_id": "<turn_id>",
  "answer_file_path": "<answer_file_path>",
  "response_excerpt": "<short excerpt>"
}
JSON
```

- Completion rule:
  - only `completed` or `boundary` permits a final business reply to the user
  - `execute`, `awaiting-confirmation`, `awaiting-user-decision`,
    `waiting-shared-job`, and `blocked` do not

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output
- ability to inspect rendered images when the answer boundary requires it

If the environment cannot satisfy those capabilities, stop and explain the blocker instead of improvising.

## Procedure

1. Treat one ordinary user message as one canonical `ask` turn, and run steps 2 through 4 as the governed `open` or same-turn reuse phase before any inner workflow execution.
   - keep one live user question mapped to one canonical turn
   - reuse the live turn when the same question is continuing
   - when the same live turn and the same active run are re-entered, reuse the existing governed preanswer result instead of restarting preanswer governance
   - return in the user's language unless they ask for another language
2. Open or reuse the canonical ask turn through the supported path defined in `Canonical Ask Contract`.
   - reconcile any active native thread, and keep that reconciliation in the native ledger and interaction-ingest path until canonical ask ownership is open
   - before calling `open`, form a thin agent-authored `work_brief` from the user's actual goal
     and medium; include only fields that matter for this turn: deliverable and use, audience,
     in/out scope, success criteria, distinctions that must survive, named evidence and
     exemplars, confirmed method/storyline/expression grammar, and high-cost artifact risk
   - simple work may use an empty or very small brief; never ask the user to fill a form and
     never replace professional framing with keyword inference in deterministic runtime code
   - pass the brief inside best-effort `semantic_analysis`; keep one concise `route_reason`, and
     set `needs_latest_workspace_state` only when fresh local workspace truth is
     actually required, and include compact `evidence_requirements` only when the question
     needs channel guidance
   - when the user explicitly revises a prior result, pass its exact `revision_of` turn id and
     the narrow `continuation_type`, `revision_scope`, and `affected_outputs`; adjacency in the
     same chat is not sufficient authority to inherit evidence or accepted scopes
   - when the user answers a persisted gate, pass the exact `resolves_gate_id` plus either a
     legal `option_id` from that gate or an explicit non-empty `free_form` decision; do not
     reinterpret arbitrary later text as gate approval
   - let `open` normalize supported routing fields, resolve user-native source references when the user names a document, path, page, slide, sheet, heading, or similar locator, and return the governed turn binding
3. During `open` or same-turn reuse, let the governed ask path choose the smallest evidence basis that can support the answer correctly and truthfully before deeper workflow execution.
   - `workspace-corpus` -> KB-first
   - `composition` -> KB-first with explicit evidence planning
   - `external-factual` -> web-first
   - `general-stable` -> model knowledge when the boundary is explicit
4. During that same governed preanswer step inside `open`, check workspace state only when the answer really depends on workspace truth.
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
   - keep the same canonical turn ownership through the inner workflow; `retrieve`, `trace`, raw-source inspection, or internal helper probing may support that live turn, but they do not replace canonical ask ownership or its completion rules
   - use published KB artifacts first when they already expose the needed evidence channels
   - treat published-KB answering as the ordinary ask path; governed Lane B follow-up remains sync/publication-owned and may surface as freshness or degradation notice, but it is not a separate host-driven ask step
   - treat the published KB as the primary evidence surface: inspect retrieved text, structure, notes, media, and artifact metadata first, then inspect cited `focus_render_assets` or render spans when the question is genuinely visual or layout-sensitive, and only then consider governed refresh or source fallback
   - start execution with a short support ledger derived from `support_contract`:
     - which source boundary must survive
     - which comparison sources must both survive
     - which published evidence channels are required
     - whether a single contract-repair chance exists for this turn
   - keep approximate or unresolved reference notices explicit
   - normalize the additive thin `work_brief`, revision lineage, accepted scopes, and turn
     evidence packet before deciding whether evidence work is necessary
   - treat user-named files and host attachments as current turn evidence by default, but keep
     their role, authority, freshness, hash and supersession semantics explicit; do not promote
     them into the durable KB merely because they were used once
   - distinguish `evidence-gap`, `reasoning-uncertainty`, and
     `judgment-authority-gap`: solve the first two autonomously; persist a decision gate only
     for the third or for a genuinely expensive expression choice
   - for `constraint-update` and evidence-neutral `decision-resolution`, inherit legal evidence,
     skip sync and retrieval, preserve unrelated accepted scopes, retrace only the exact revised
     answer or artifact, and finalize
   - record an accepted scope only when the current user explicitly accepted it and the host sets
     `accepted_by_user: true`; reopen it only after explicit user authority with
     `reopened_by_user: true`. Dependency drift marks a scope `at-risk` but never silently
     reopens or replaces it
   - for `evidence-refresh` or `mixed`, retrieve only the evidence delta and invalidate only
     explicitly affected or dependency-linked outputs
   - unresolved relevance alone does not authorize global sync; use task-scoped freshness and
     expose a boundary when critical target freshness cannot be established
   - let the routed inner workflow own retrieval, trace, render inspection, and answer or composition drafting
   - if published artifacts are still insufficient because of hard-artifact semantic gaps, let the canonical routed path enter one governed narrowed hybrid refresh instead of improvising raw source fallback
     - this ask-owned narrowed hybrid refresh is the Lane C path and is the only ordinary ask follow-up settled through hidden `progress`
     - after a `covered` settlement, rerun retrieve and trace exactly on the post-refresh evidence; if the result is commit-admissible but still only partially supported, finalize honestly as `partially-grounded` instead of starting a second refresh
   - if that governed path becomes a shared wait or blocked boundary, keep the same turn paused or committed through the existing ask control-plane states rather than opening a side path
7. Complete the turn through the supported completion path.
   - write only the final answer under `runtime/answers/<conversation_id>/<turn_id>.md`
   - keep scratch work under `runtime/agent-work/` when needed
   - follow the finalize handshake in `Canonical Ask Contract`: run the final trace on the exact answer-file version, then call hidden `finalize`, passing selected artifact IDs only when the turn is ambiguous
   - if the first finalize attempt returns `status = execute` with a repairable `support_fulfillment`, keep the same turn open, do one contract-aware rewrite and retrace, then finalize once more
   - do not open a second or unbounded repair loop; the second finalize attempt must close honestly
   - let the commit barrier run only after the admissibility gate passes
   - preserve `answer_state`, `support_basis`, optional `support_manifest_path`, and linked session or trace IDs
8. Return the result cleanly.
   - direct answer when supported
   - explicit non-answer boundary when not
   - one concise freshness or waiting note only when it materially helps the user
   - return the business `answer_text` unchanged, followed outside the artifact by exactly one
     concise completion line; use `user_status_code` as the stable meaning and render the line
     naturally in the user's language (`completed-and-verified` or
     `completed-with-evidence-boundary`)
   - show `governance_detail` only for blockers, evidence boundaries, diagnostics, or explicit
     user inspection; never append trace IDs, manifest paths, repair codes, or release-entry
     notices to a successful copy-ready artifact

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

- A reconciled native turn is still only host-side context until the matching canonical ask turn has been opened.
- `grounded-answer` and `grounded-composition` remain inner specialist workflows.
- Host Hooks are optional accelerators. Never assume their context injection or one-shot Stop
  continuation ran; without them, follow the same canonical contract directly.
- Tracked repo search, live corpus discovery, knowledge-base artifact discovery, and runtime
  artifact discovery are different surfaces; do not substitute one for another silently.

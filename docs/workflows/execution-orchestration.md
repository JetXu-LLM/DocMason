# Execution-Orchestration Notes

DocMason uses three layers for workflow execution:

1. the stable public CLI
2. the canonical vendor-neutral workflows under `skills/canonical/`
3. the execution-orchestration layer described by `AGENTS.md`, canonical `workflow.json` sidecars, and generated adapter routing docs

## Public Surface

The public command surface now includes:

- `docmason prepare`
- `docmason doctor`
- `docmason status`
- `docmason sync`
- `docmason retrieve`
- `docmason trace`
- `docmason validate-kb`
- `docmason sync-adapters`
- `docmason workflow`

Phase 4b intentionally does not add public `docmason answer` or `docmason review-logs` commands.
Phase 5 also intentionally keeps benchmarking and evaluation private-first instead of adding a public `docmason eval` command before the operator-review layer exists.
The Phase 6 hardening patch adds `docmason workflow` as an advanced public execution surface without weakening `ask` as the only natural-language question entry.

## Canonical Workflow Surface

The canonical workflow surface includes thirteen workflows, but they are not all peer user-entry points.

### Default Natural-Language Entry

- `ask`

`ask` is the user-facing top-level workflow.
It is the default route for ordinary freeform business questions inside a valid workspace.

### Explicit Top-Level Operator Workflows

- `workspace-bootstrap`
- `workspace-doctor`
- `workspace-status`
- `knowledge-base-sync`
- `runtime-log-review`
- `adapter-sync`

These are direct routes for explicit setup, status, sync, review, and adapter-maintenance intents.

### Inner Specialist Workflows

- `grounded-answer`
- `grounded-composition`
- `retrieval-workflow`
- `provenance-trace`

These are specialist inner workflows that the main agent should invoke when a top-level workflow needs supported answering, evidence retrieval, or provenance analysis.

### Supporting Construction And Repair Workflows

- `knowledge-construction`
- `validation-repair`

These are follow-on workflows used by sync and validation loops rather than ordinary user entry points.
Under the current autonomous sync path, they are mainly compatibility or recovery workflows rather
than the normal publication path.

Each workflow directory includes:

- `SKILL.md` for the procedural workflow contract
- `workflow.json` for lightweight execution metadata such as mutability, parallelism, background-command hints, handoff signals, and user-entry routing hints when relevant

## Core Routing Policy

- The main agent owns critical-path reasoning, shared-state mutation, publication, final answers, and final operator-facing conclusions.
- Deterministic shell steps should run as main-agent or background command steps once parameters are known.
- Delegation is allowed only for read-only analysis or bounded disjoint per-source work.
- Do not delegate sync publication, validation sign-off, adapter regeneration sign-off, or final answer integration.
- The project prefers better end-to-end quality and honest state disclosure over maximum parallelism.

## User-Intent Policy

- Ordinary business questions should route to `ask`.
- The primary semantic routing decision for `ask` should come from agent-supplied structured analysis rather than growing repo-side keyword classifiers.
- For odd or non-typical workspace questions, the agent should prefer choosing required published evidence channels over inventing a new special-question taxonomy.
- Explicit setup, readiness, status, sync, adapter, or runtime-review requests should route to their matching top-level workflows directly.
- `docmason workflow` is an explicit advanced execution surface, not a replacement for natural-language `ask`.
- `ask` should choose the narrowest honest evidence basis rather than forcing every question through KB routing.
- `ask` and public `retrieve` may parse user-native file-plus-locator references implicitly from freeform queries, but public `trace` remains ID-first in Phase 6b2.
- When the published KB already exposes the needed `text`, `render`, `structure`, `notes`, or `media` artifacts, odd-question handling should stay KB-native rather than jumping straight back to `original_doc/`.
- If a workspace-dependent question encounters a missing published knowledge base, it should route to workspace bootstrap or knowledge-base sync rather than bluffing an answer.
- If the published corpus is stale but still usable, `ask` should answer from the published corpus with a concise freshness notice only when the answer path depends on that corpus.
- If a `workspace-corpus` question genuinely needs fresh local state and the environment is ready, the routed ask path may auto-sync before answering and should keep that transition concise and auditable.

## Why The Metadata Exists

The `workflow.json` sidecars are execution metadata, not a second authored workflow truth source.

They exist to help adapters and future workflow routers answer questions like:

- is this workflow read-only or mutating?
- is per-source parallelism safe?
- which deterministic commands are the right background candidates?
- what artifacts or completion signal should the main agent expect next?
- which workflow should be treated as the user-facing natural entry surface?

The canonical workflow semantics still live in:

- `AGENTS.md`
- canonical `SKILL.md` files
- the stable public CLI behavior

## Generated Adapter Routing

`docmason sync-adapters` generates:

- `CLAUDE.md`
- `adapters/claude/project-memory.md`
- `adapters/claude/workflow-routing.md`

`workflow-routing.md` groups workflows by tier and category so a fresh capable agent can infer the intended product surface without relying on a hidden chat transcript.

---
name: retrieval-workflow
description: Retrieve ranked evidence bundles from the published DocMason knowledge base.
---

# Retrieval Workflow

Use this skill when the task is to retrieve the strongest published evidence bundles for a question or topic.

This is an evidence-focused workflow.
Use it directly for explicit evidence requests, or let `ask` route here automatically.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output

If the agent cannot run local commands or inspect the published file-only knowledge base, stop and explain that reliable retrieval is not possible.

## Procedure

1. Confirm that the published knowledge base exists with `docmason status --json` when needed.
2. Run `docmason retrieve "<query>" --json`.
   - keep user-native source references inside the freeform query rather than inventing internal source IDs when the user already knows a file name, path, page, slide, sheet, or heading
3. Inspect:
   - `reference_resolution`
   - ranked source bundles
   - matched units
   - graph expansions
   - render references when relevant
   - any published-evidence plan fields such as preferred channels, matched channels, and whether published artifacts already look sufficient
4. Narrow or widen the query by:
   - `--document-type`
   - `--source-id`
   - `--top`
   - `--graph-hops`
   - when `reference_resolution.status` is `exact`, expect the source filter and any exact unit targeting to have already narrowed the candidate set decisively
   - when `reference_resolution.status` is `approximate` or `unresolved`, preserve the notice boundary rather than pretending the narrowing was exact
5. If the strongest results are weak or empty, say so explicitly instead of pretending the query succeeded.
6. Open the cited source, unit, and render assets before claiming confidence on difficult evidence judgments.
7. When the task is moving toward a final answer or deliverable draft, return retrieval bundles to the main agent for provenance tracing, `grounded-answer`, or `grounded-composition`.
8. If you need to export a scratch evidence note and the user did not specify a destination, place it under `runtime/agent-work/`.

## Escalation Rules

- If retrieval returns no results, surface that boundary directly and consider narrower or alternate queries only when they remain faithful to the user intent.
- If render references or low-confidence extraction suggest visual confirmation is required, escalate to render inspection or provenance trace before final synthesis.
- If the retrieval result already shows that the preferred published evidence channels are sufficient, do not jump back to `original_doc/` as a first move.
- Retrieval alone is not the grounded-answer contract. Do not present retrieval output as a fully supported final answer.
- Public `retrieve` now does implicit source-reference parsing, but public `trace` still remains ID-first in this phase.

## Completion Signal

- The workflow is complete when the main agent has either a ranked grounded evidence bundle or an explicit no-results boundary with concrete next steps.

## Notes

- Retrieval runs over `knowledge_base/current/` by default.
- Phase 4 retrieval is deterministic lexical plus metadata plus graph expansion. It does not use embeddings yet.
- Retrieval logs are stored locally under `runtime/logs/`.
- `--json` output always includes a structured `reference_resolution` block, and normal CLI output echoes the resolution status plus any best-effort notice.
- If the strongest evidence depends on renders or low-confidence text extraction, inspect the render assets before finalizing the answer.
- Ordinary natural questions should usually begin at `ask`, not by requiring the user to name this workflow ID.

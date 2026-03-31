---
name: runtime-log-review
description: Review DocMason runtime query-session and retrieval-trace logs through the summary surface rather than raw JSON browsing alone.
---

# Runtime Log Review

Use this skill when the task is to review recent runtime activity, identify failures, or extract candidate cases from DocMason logs.

This is an explicit operator-facing workflow.
The user may ask for it directly, or `ask` may route here automatically when the real intent is runtime review rather than question answering.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output

If the agent cannot inspect local runtime logs, stop and explain that log review is not possible.

## Procedure

1. For an explicit operator refresh, prefer `docmason workflow runtime-log-review --json` so the derived summary and the request-level audit record are regenerated together.
2. Start with `runtime/logs/review/summary.json` and `runtime/logs/review/benchmark-candidates.json` when they exist.
   - treat live conversation state under `runtime/state/` as the owner and `runtime/logs/conversations/` as projection-only
     - `projection-only` means a derived mirror, not the primary truth surface
   - require canonical ask ownership before classifying a case as `interactive-ask`; workflow names, conversation linkage, or reconciliation leftovers alone are not enough
     - here `canonical ask ownership` means the case is backed by a governed ask turn and linked runtime artifacts, not only host transcript residue
3. Use the summary modes that best match the request:
   - recent activity
   - no-result retrieval sessions
   - artifact-rich queries that still degraded or returned the wrong source family
   - degraded answer-first traces
   - trace cases where artifact supports existed but the final answer still remained partially grounded or unresolved
   - repeated failure patterns
   - frequently consulted sources or units
   - candidate benchmark or operator-review cases
   - real interaction activity versus synthetic evaluation traffic
   - active waiting shared jobs
   - active confirmation-required shared jobs
   - orphaned query sessions or retrieval traces that are not backed by committed truth
4. When the summary shows a case worth deeper inspection, open the referenced query-session or retrieval-trace JSON directly.
5. If the operator needs the underlying evidence, route to retrieval, provenance tracing, or grounded-answer rather than guessing from log metadata alone.
6. Keep the workflow descriptive and review-oriented. Do not mutate prompts, skills, overlays, or benchmarks from inside this workflow.
7. If you need to export a scratch review summary and the user did not specify a destination, place it under `runtime/agent-work/`.
8. Treat `runtime/logs/review/requests/<request_id>.json` as the canonical audit surface for the explicit review request that refreshed or read the review-side outputs.
9. Return the operator-facing review summary and recommended next steps to the main agent.

## Escalation Rules

- If `runtime/logs/review/summary.json` does not exist yet, refresh the workflow once. If the regenerated summary still shows no recent activity, explain that the workspace has no recent ask, retrieval, or trace evidence to review yet.
- If the summary shows degraded answer traces or unresolved answer states, preserve that uncertainty instead of flattening it into a generic warning.
- If the request requires evidence validation rather than log review, switch to retrieval or provenance tracing.

## Completion Signal

- The workflow is complete when the main agent has a concise review summary with the relevant case IDs, repeated patterns, and follow-up recommendations.

## Notes

- This is an explicit operator-facing workflow, not a public `docmason review-logs` command.
- Each explicit review invocation should leave one replayable request artifact under `runtime/logs/review/requests/`.
- The review summary is derived from runtime logs under `runtime/logs/`.
- The summary should distinguish committed truth from orphaned leftovers instead of reconstructing legality from mixed artifacts.
- `orphaned leftovers` means reviewable runtime artifacts that are not backed by the committed governing truth for a completed case.
- `runtime/logs/review/benchmark-candidates.json` is a read-only derived artifact that suggests future benchmark cases from conversation turns, retrieval sessions, and trace outcomes.
- Real operator and user interactions should stay at the top of the main recent-activity views; evaluation-suite traffic is intentionally demoted into separate synthetic buckets.

---
name: operator-eval
description: Run the hidden operator-only evaluation loop over local runtime/eval artifacts without exposing that surface in normal first-contact workflows.
---

# Operator Eval

Use this workflow only for advanced operator-quality tasks such as replayable evaluation runs, regression review, candidate promotion, and baseline freezing.

This workflow is intentionally open-source but non-first-contact.
Do not surface it in ordinary user entry flows, and do not treat it as part of the default `ask` experience.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect structured JSON output

## Procedure

1. Read the current operator request from `runtime/eval/requests/current.json`.
2. Validate the request against the tracked examples in `skills/operator/operator-eval/examples/`.
3. Execute exactly one requested action through the `operator-eval` runtime surface:
   - `run-suite`
   - `review-regressions`
   - `promote-candidate`
   - `freeze-baseline`
4. Keep live eval truth under `runtime/eval/`, not under tracked repository paths.
5. Keep review of ordinary runtime activity under `runtime/logs/`.
6. Treat tracked examples as schema guidance only. Never commit confidential runtime/eval payloads.
7. Return the operator-facing result, written artifacts, and next corrective step to the main agent.

## Notes

- This workflow is intentionally absent from first-contact guidance.
- Frozen baselines and promotion decisions remain human-governed even though this workflow is productized.
- `skills/operator/operator-eval/examples/` exists so fresh open-source users can understand the local-only artifact contracts without access to private corpus data.

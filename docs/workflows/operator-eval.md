# Operator Eval

`operator-eval` is the hidden advanced workflow for the local Phase 6b1 operator-quality loop.

It is intentionally open-source but non-first-contact.
Ordinary user guidance should continue to center on `ask`, setup, sync, retrieval, trace, and runtime review.

## Entry Surface

Run the workflow through:

```bash
./.venv/bin/python -m docmason workflow operator-eval --json
```

The workflow reads one request file from:

- `runtime/eval/requests/current.json`

## Supported Actions

- `run-suite`
- `review-regressions`
- `promote-candidate`
- `freeze-baseline`

## Live Artifact Root

All live operator-eval artifacts stay under:

- `runtime/eval/`

Important subtrees:

- `runtime/eval/benchmarks/broad/`
- `runtime/eval/benchmarks/regression/`
- `runtime/eval/drafts/candidates/`
- `runtime/eval/runs/`
- `runtime/eval/reviews/`
- `runtime/eval/feedback/`

## Tracked Examples

Tracked schema examples live under:

- `skills/operator/operator-eval/examples/`

Those files are illustrative only and must never be mistaken for live local benchmark truth.

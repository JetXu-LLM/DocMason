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

Suite definitions may now include manual `execution_mode="ask-turn"` cases in addition to the
existing retrieval and trace replay modes. This remains a hidden local operator surface only.
Ask-turn replay remains auditably synthetic:

- the replayed turn and linked runtime artifacts carry `log_origin="evaluation-suite"`
- review-facing real buckets ignore those synthetic records
- `required_run_events` is checked as an ordered run-journal subsequence rather than an unordered set
- shared-job wait or settle closure is validated from persisted runtime truth, not only case-authored expectations

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

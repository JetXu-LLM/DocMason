# Hidden Local Maintenance Workflow

This page exists because the repository still ships a hidden `operator-eval` workflow surface.
It is not a public product feature, not a recommended contributor starting point, and not part of DocMason's current release direction.

## Current Status

- local-only
- hidden from normal user guidance
- not part of any public benchmark or competition program
- safe to ignore unless you are maintaining legacy local review or replay tooling

## Entry Surface

Run it only from an already prepared source repository:

```bash
./.venv/bin/python -m docmason workflow operator-eval --json
```

## What It Is For

Use this workflow only when a maintainer explicitly needs to inspect or replay local maintenance artifacts under `runtime/eval/`.
It is not required for ordinary ask, sync, trace, runtime review, or bundle use.

## Artifact Boundary

If this surface is used at all, its live artifacts stay under:

- `runtime/eval/`
- `runtime/eval/benchmarks/`
- `runtime/eval/runs/`
- `runtime/eval/reviews/`
- `runtime/eval/feedback/`

Tracked examples under `skills/operator/operator-eval/examples/` are schema examples only.
They are documentation aids, not live runtime truth.

## Public Boundary

- public bundles do not depend on this workflow
- ordinary users should ignore it
- contributors should not treat it as the preferred quality loop for the project
- DocMason is not currently pursuing a public evaluation or benchmark track

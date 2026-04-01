# Documentation Index

`README.md` is the fastest public entry point for DocMason.
This `docs/` tree is the deeper public reference for users, evaluators, contributors, and installed AI agents working from the tracked repository.

## What belongs here

Use `docs/` for current public behavior and public operating boundaries:

- product shape and supported use cases
- bundle and workspace boundaries
- workflow and command reference
- setup, recovery, and privacy guidance
- architecture notes that help a public reader understand the product

Keep private design law, implementation programs, and historical notes in the repository's private design stack.
Public docs should never require private design-note context to make sense.

## Recommended Reading Paths

### First Evaluation

1. Read `README.md`.
2. Read [Product Overview](product/README.md).
3. Read [Distribution And Public Bundles](product/distribution-and-benchmarks.md).

### Running A Private Workspace

1. Read `README.md`.
2. Read [Workflow Overview](workflows/README.md).
3. Use [Manual Workspace Bootstrap And Recovery](setup/manual-workspace-recovery.md) only when the normal automation path cannot finish honestly.

### Contributing

1. Read [../CONTRIBUTING.md](../CONTRIBUTING.md).
2. Read [Execution-Orchestration Reference](workflows/execution-orchestration.md).
3. Read [Architecture Overview](architecture/README.md) and [Policy Index](policies/README.md) as needed.

## Current Document Map

- [Architecture Overview](architecture/README.md)
- [Policy Index](policies/README.md)
- [Product Overview](product/README.md)
- [Distribution And Public Bundles](product/distribution-and-benchmarks.md)
- [Workflow Overview](workflows/README.md)
- [Execution-Orchestration Reference](workflows/execution-orchestration.md)
- [Manual Workspace Bootstrap And Recovery](setup/manual-workspace-recovery.md)

## Writing Standard

Public docs in this tree should stay:

- current-state, not phase-history-first
- public and reader-first
- behavior and boundary focused
- honest about unsupported or deferred paths
- consistent with `README.md`, `CONTRIBUTING.md`, and the canonical workflow surfaces

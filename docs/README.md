# Documentation Index

`README.md` is the primary user-facing document.
It should sell the project honestly, explain the value quickly, and show the default onboarding path.

`AGENTS.md` is the minimal first-contact contract for agents entering the repository.
Detailed workflow procedures belong in `skills/canonical/` and the deeper docs, not in `AGENTS.md` itself.

This `docs/` tree is the deeper reference layer for contributors, advanced operators, and adapter authors.

Current documentation areas:

- [Architecture](architecture/README.md)
- [Product](product/README.md)
- [Distribution Strategy](product/distribution-and-benchmarks.md)
- [Setup](setup/manual-workspace-recovery.md)
- [Workflows](workflows/README.md)
- [Execution Orchestration](workflows/execution-orchestration.md)
- [Policies](policies/README.md)

The current documentation set explains:

- the public nine-command CLI surface, including the advanced `workflow` entry
- `ask` as the default natural-language entry surface inside a valid workspace
- the smaller set of explicit top-level operator workflows
- the inner specialist workflows that support answering, composition, retrieval, and trace
- the explicit execution-orchestration policy
- the interaction-derived knowledge boundary
- the one-repo distribution model and public sample-corpus boundary

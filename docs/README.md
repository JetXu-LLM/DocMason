# Documentation Index

`README.md` is the primary user-facing document.
It should sell the project honestly, explain the value quickly, and show the default onboarding path.

`AGENTS.md` is the minimal top-level routing contract for agents entering the repository.
Detailed workflow procedures belong in `skills/canonical/` and selected deeper reference pages, not in `AGENTS.md` itself.

This `docs/` tree is the public deeper documentation layer for DocMason.
It is written for users and evaluators first, then for contributors and installed AI agents that need a stable public reference.
It should explain the current product, how to operate it, and where its public boundaries are.

`docs/` is not the private design stack.
Private design law, Dao/Fa/Qi/Shu reasoning, detailed technical specifications, implementation programs, and supersession records belong under `planning/`, not here.

The main document families in `docs/` are:

- product and mental-model explainers
- task-oriented how-to and operating guides
- stable public reference for CLI, workflows, architecture, and repository boundaries
- policy, privacy, distribution, and support-boundary documentation

The writing standard for `docs/` is:

- human-first but agent-legible
- current-state and de-phased by default
- honest about public capability and limits
- focused on public behavior rather than private design theory

Current documentation areas:

- [Architecture](architecture/README.md)
- [Product](product/README.md)
- [Distribution Strategy](product/distribution-and-benchmarks.md)
- [Setup](setup/manual-workspace-recovery.md)
- [Workflows](workflows/README.md)
- [Execution Orchestration](workflows/execution-orchestration.md)
- [Policies](policies/README.md)

The current documentation set explains:

- what DocMason is and how to think about its public product shape
- how to bootstrap, recover, and operate a workspace through the public entry surfaces
- the public CLI surface and the public workflow boundary around `ask`
- the repository's public architecture, policy, privacy, and distribution boundaries

Some existing pages still contain more historical or internal detail than this standard intends.
Those pages should be tightened over time toward current-state public reference rather than treated as the public home for private design reasoning.

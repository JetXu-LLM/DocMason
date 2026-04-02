# Workflow Overview

DocMason keeps one ordinary natural-language front door and a small set of explicit operator routes.

## Default Front Door

- `ask` is the only ordinary natural-language entry surface
- ordinary business questions should start there
- public `retrieve`, `trace`, and `docmason workflow` are not substitutes for canonical `ask`

## Stable Public CLI

The current public CLI includes:

- `docmason doctor`
- `docmason prepare`
- `docmason status`
- `docmason sync`
- `docmason retrieve`
- `docmason trace`
- `docmason validate-kb`
- `docmason sync-adapters`
- `docmason update-core`
- `docmason workflow`

All public commands support `--json`.

## Explicit Operator Routes

Use explicit operator workflows or commands when the task is clearly about:

- preparing or repairing the workspace
- checking readiness or status
- syncing the knowledge base
- reviewing recent runtime failures or degraded behavior
- refreshing generated adapter guidance

## How Ordinary Work Usually Flows

1. The user asks naturally through a supported host agent.
2. DocMason opens or reuses canonical `ask`.
3. The ask path chooses the narrowest honest evidence basis.
4. If the workspace is not ready, the ordinary native bootstrap path first routes through the governed launcher `./scripts/bootstrap-workspace.sh --yes --json`.
5. On native Codex, `Default permissions` and `Full access` are explicit different states. A per-command `Yes` popup is not the same thing as switching the thread to `Full access`.
6. If the workspace is not ready or the published KB is missing or stale, the system routes to the needed governed preparation or sync work.
7. Manual workspace recovery is the last fallback, not the normal ordinary-path next step.
8. The final answer is committed only after answer-critical work has settled.

## Evidence Rules For Workflow Selection

- workspace or corpus questions default to published-KB support
- external latest-state questions use web support when needed
- stable low-risk general knowledge may use model knowledge with honest boundaries
- visual or structure-sensitive questions should inspect published text, render, structure, notes, or media artifacts before falling back to source inspection

## Hidden Or Local-Only Surfaces

Some workflows exist for maintainers, compatibility, or local repair.
They are not part of the normal product story and should not appear as ordinary first-contact steps.
The hidden `operator-eval` workflow falls into that category and is documented only as a local maintenance note.

For Claude Code and other compatibility hosts, keep higher-access fallback wording brief and host-generic.

## Next References

- [Execution-Orchestration Reference](execution-orchestration.md)
- [Manual Workspace Bootstrap And Recovery](../setup/manual-workspace-recovery.md)
- [Architecture Overview](../architecture/README.md)

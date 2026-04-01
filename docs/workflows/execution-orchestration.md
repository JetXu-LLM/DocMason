# Execution-Orchestration Reference

This page is the public advanced reference for how DocMason organizes its commands, workflows, and adapter-facing contracts.

## Public Surfaces

DocMason exposes three public execution surfaces:

1. supported host-agent entry through canonical `ask`
2. stable public CLI for deterministic operations
3. canonical workflow metadata and generated adapter routing for explicit advanced use

## Stable Public CLI

The public CLI includes:

- `docmason prepare`
- `docmason doctor`
- `docmason status`
- `docmason sync`
- `docmason retrieve`
- `docmason trace`
- `docmason validate-kb`
- `docmason sync-adapters`
- `docmason update-core`
- `docmason workflow`

`docmason workflow` is an advanced execution surface for explicit workflow-level invocation.
Direct operator tools do not replace canonical `ask` for ordinary questions.

## Workflow Roles

### Ordinary Questions

- `ask`

`ask` is the only natural-language front door for ordinary business questions inside a valid workspace.
Compatible hosts should open canonical `ask` rather than stitching together side paths.

### Explicit Operator Workflows

- `workspace-bootstrap`
- `workspace-doctor`
- `workspace-status`
- `knowledge-base-sync`
- `runtime-log-review`
- `adapter-sync`

Use these only when the user is explicitly asking for setup, status, sync, review, or adapter maintenance work.

### Inner Specialist Workflows

- `grounded-answer`
- `grounded-composition`
- `retrieval-workflow`
- `provenance-trace`
- `knowledge-construction`
- `validation-repair`

These workflows exist so the main agent can do governed work without turning every user into a workflow operator.
Ordinary users should not need to name them first.

## Public Command Versus Workflow Boundary

- `retrieve` and `trace` are legal operator evidence tools
- they do not replace canonical `ask`
- `docmason workflow` is useful only when the operator already knows the exact workflow to run
- generated adapters may restate routing hints, but they do not replace canonical surfaces

## Canonical Sources Of Truth

The public execution contract is layered deliberately:

- `AGENTS.md` for first-contact routing
- canonical `SKILL.md` files for workflow contracts
- `workflow.json` sidecars for lightweight execution metadata
- generated adapters as translations of canonical truth

`workflow.json` exists to help adapters and future routers with metadata such as read-only versus mutating behavior, safe parallelism hints, or expected artifacts.
It is not a second authored workflow contract.

## Local-Only And Hidden Surfaces

DocMason may still contain hidden or maintenance-only workflows.
They are not public first-contact routes and should never be advertised as the normal path for ordinary users.
The current `operator-eval` surface is in this bucket.

## Non-Goals

- no alternate ordinary natural-language front door
- no public `docmason answer`
- no public `docmason eval`
- no expectation that reading private design notes or implementation code is required before using the product

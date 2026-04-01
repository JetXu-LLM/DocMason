# Architecture Overview

DocMason is a local, file-first, repo-native system.
The repository is the operating surface; generated bundles are distribution variants of that same surface, not a separate product.

## Core Architectural Commitments

- file-only persistence rather than a required database service
- published knowledge under `knowledge_base/current/`
- `knowledge_base/staging/` as a working area, not reader-facing truth
- ordinary natural-language work entering through canonical `ask`
- deterministic preparation, validation, and publication around agent reasoning
- strict separation between tracked public fixtures and live private workspace content
- generated adapters translating canonical repository contracts rather than defining parallel truth

## Public Layers

### Workspace Surfaces

- `original_doc/` is the live private source boundary.
- `knowledge_base/current/` is the current published read surface.
- `knowledge_base/staging/` is a working area during sync and publication.
- `runtime/` holds local execution, review, and audit state.
- `sample_corpus/` holds tracked public demo fixtures.

### Execution Surfaces

- ordinary questions go through a supported host agent and canonical `ask`
- the public CLI handles deterministic setup, sync, and evidence operations
- `docmason workflow` is the advanced explicit workflow surface
- canonical workflow contracts live under `skills/canonical/`

### Evidence Surfaces

- published text, render, structure, notes, and media artifacts
- retrieval and trace outputs derived from the published corpus
- source references and locators that keep answer support inspectable

### Distribution Surfaces

- the canonical source repository is the contributor surface
- the clean bundle is the private real-use distribution surface
- the demo bundle is the public product-evaluation surface

## Local-Only And Derived Surfaces

Some files exist only to support local execution or maintainer review:

- `runtime/logs/` and `runtime/answers/`
- request-level review artifacts under `runtime/logs/review/`
- generated adapters under `adapters/`
- hidden maintenance or compatibility surfaces that are not part of the default product story

These surfaces may be useful, but they are not a second public contract.
Derived runtime projections remain local read surfaces rather than canonical authored truth.

## What This Page Does Not Do

This page does not document internal design stages, private design law, or implementation programs.
Those belong in the repository's private design stack.

For operator entry points and workflow boundaries, read [Workflow Overview](../workflows/README.md) and [Execution-Orchestration Reference](../workflows/execution-orchestration.md).

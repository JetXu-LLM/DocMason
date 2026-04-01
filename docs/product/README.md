# Product Overview

DocMason is a repo-native AI application for serious work over private documents.
It compiles a local, provenance-aware knowledge base so a strong host agent can answer questions against published evidence instead of a flattened text dump.

## What The Product Is Today

DocMason currently ships as:

- a local workspace for Office files, PDFs, email, markdown, and other supported text inputs
- a published knowledge base under `knowledge_base/current/`
- a governed natural-language front door through canonical `ask`
- deterministic retrieval and provenance tracing over the published corpus
- generated clean and demo bundles for simpler onboarding

## Current Entry Surfaces

- the source repository is the contributor and maintainer surface
- the clean bundle is the safest start for private real use
- the demo bundle is the fastest public evaluation path
- Codex on macOS is the reference host path; Claude Code and GitHub Copilot remain compatibility paths

## What Users Should Expect

- local file ownership and clear corpus boundaries
- published-KB answers when the question depends on workspace content
- explicit boundaries when setup, sync, approval, or refresh work is still required
- public docs that explain shipped behavior rather than private design-history context

## Current Product Boundaries

- no web UI
- Windows is not the primary supported platform
- no public benchmark or competition workflow
- no default cloud ingestion of corpus data
- no public command that bypasses canonical `ask` for ordinary questions
- no promise that hidden maintainer workflows are part of the end-user product

## Product Shape

DocMason keeps a small public command surface and pushes most ordinary work through natural language plus governed workflows.
The repository remains the canonical source of truth; bundles are distribution variants, not a second product.

## Next References

- [Distribution And Public Bundles](distribution-and-benchmarks.md)
- [Workflow Overview](../workflows/README.md)
- [Architecture Overview](../architecture/README.md)
- [Policy Index](../policies/README.md)

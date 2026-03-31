# Contributing to DocMason

Thank you for contributing.

DocMason aims to be a top-tier public repository for serious document knowledge work.
Contributions should improve shipped behavior, public clarity, and local operating quality without weakening privacy, provenance, or workflow discipline.

## Before You Start

- read [README.md](README.md) for the public product story
- read [docs/README.md](docs/README.md) for the public documentation map
- keep changes aligned with the native reference workflow: Codex on macOS first, then careful compatibility adaptation for other hosts and environments

## Non-Negotiable Boundaries

### Public Language And Repository Shape

- write code, comments, documentation, prompts, and public-facing repository text in English
- prefer Python unless another language is clearly justified
- keep persistent knowledge artifacts file-based
- do not introduce a required database service

### Private Data Boundaries

- never commit confidential source documents
- never commit compiled private knowledge bases
- never commit local planning artifacts intended to stay private
- never paste confidential corpus material into public issues or pull requests
- keep live `original_doc/`, `knowledge_base/`, `runtime/`, and `adapters/` out of tracked changes

### Public Docs Standard

- public docs must be current-state, public-context readable, and free of private planning assumptions
- do not write `/docs` as design history, phase notes, or internal constitutional theory
- if behavior changes, update docs, tests, and canonical surfaces together

### Workflow And Product Boundaries

- `ask` remains the only ordinary natural-language front door
- do not add public commands or public workflow paths without a clear product reason
- generated adapters translate canonical surfaces; they should not become hand-maintained parallel truth

## Sample Corpus And Bundles

- tracked public demo fixtures live under `sample_corpus/`, not under live `original_doc/`
- if you want the canonical public demo corpus in a real workspace, ask the agent to use `public-sample-workspace`, or run `python3 scripts/use-sample-corpus.py --preset ico-gcs`
- do not replace `sample_corpus/` with private corpus material
- if a change affects release bundles, update scripts, docs, and disclosure text together
- clean and demo bundles are end-user distribution channels, not alternate source repositories

## Dependencies And Platform Changes

- prefer dependencies installable through `uv` or `pip`
- avoid high-friction system dependencies when practical
- if a change requires heavy OS-level tooling or intrusive machine setup, document why and get project-owner alignment first
- Windows and non-native host support are compatibility work, not the native design center

## Validation Expectations

- run targeted tests for the behavior you changed
- run `python3 scripts/check-repo-safety.py` if workspace boundaries might be affected
- inspect public docs and command text when user-facing wording changes
- if you touch bundle or update behavior, verify the related docs and scripts stay aligned

## Pull Request Checklist

- explain what changed and why
- call out user-visible effects
- call out privacy, provenance, or boundary implications
- mention any bundle, sample corpus, or adapter impact
- mention any deferred follow-up instead of silently leaving docs stale

## Local Setup For Contributors

- clone the canonical repository
- install the repo hooks with `./scripts/install-git-hooks.sh`
- materialize the public sample corpus only when you actually need it in a live workspace
- use the source repository for issues, branches, tags, and pull requests

## Large Changes

If you want to introduce a major new capability or change the product boundary, start with a concise planning note and explicit owner alignment before writing a large patch.

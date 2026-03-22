# Distribution Strategy

DocMason now uses one canonical source repository plus two generated release channels:

- `clean`: empty live workspace for private real use
- `demo-ico-gcs`: preloaded public sample corpus for product evaluation

Why this shape exists:

- pure end users need a download-first, no-git-history private workspace
- evaluators need a fast demo corpus
- contributors need the full source repository, tests, and public fixtures

That means the repository distinguishes three boundaries clearly:

- `original_doc/`: writable live corpus boundary for normal workspace use
- `sample_corpus/`: tracked public fixture boundary

The canonical repository keeps `original_doc/`, `knowledge_base/`, `runtime/`, and `adapters/`
empty and gitignored. Public sample content lives under `sample_corpus/` and is copied into
`original_doc/` only when a contributor or generated demo bundle explicitly materializes it.

## Release Channels

### Clean

The clean bundle is for private real use.

- no `.git`
- no `tests/`
- no `sample_corpus/`
- empty `original_doc/`
- empty `knowledge_base/`
- empty `runtime/`
- empty `adapters/`

### Demo

The demo bundle is for product evaluation.

- no `.git`
- no `tests/`
- sample corpus materialized into `original_doc/ico/` and `original_doc/gcs/`
- empty `knowledge_base/`
- empty `runtime/`
- empty `adapters/`

### Canonical Source Repo

The canonical repository remains the contributor and maintainer surface.

- includes `tests/`
- includes `sample_corpus/`
- includes release-build and safety tooling
- does not track live workspace content beneath `original_doc/`

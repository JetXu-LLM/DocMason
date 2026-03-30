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

## Release-Entry Update Checks

Generated release bundles now carry a bounded release-entry contract:

- only `clean` and `demo-ico-gcs` bundles participate
- only canonical `ask` completion can auto-trigger the network call
- automatic checks run at most once every 20 hours
- the source repository and fresh clone paths stay automatic-network disabled
- `docmason update-core` is the explicit in-place core update path for generated bundles

Each generated bundle includes:

- `distribution-manifest.json` with a `release_entry` block
- `runtime/state/release-client.json` as the single local release-entry control file

The network payload is intentionally narrow:

- `distribution_channel`
- `source_version`
- `installation_hash`
- `trigger`

It never includes corpus content, paths, file names, query text, answer text, source locators,
environment variables, secrets, machine fingerprints, or IP-derived identifiers.

`DO_NOT_TRACK=1` disables the automatic update check completely.
Users may also set `automatic_check_enabled` to `false` in `runtime/state/release-client.json`.
An explicit `docmason update-core` command still contacts the release-entry service, because it is
a direct user-requested update action rather than a background automatic check.

When a newer bundle release is known, the host-visible final ask reply may include one short update
reminder.
The canonical answer file under `runtime/answers/` remains unchanged.
When the user explicitly updates, DocMason downloads the latest generated clean core, verifies the
published checksum, preserves local workspace state such as `original_doc/`, `knowledge_base/`,
`runtime/`, `adapters/`, `.docmason/`, and `.agents/`, and replaces the remaining top-level core
surface in place.

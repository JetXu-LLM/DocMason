# Distribution And Public Bundles

DocMason ships one canonical source repository plus two generated bundle channels.

## Why Multiple Channels Exist

- contributors need the full repository, tests, scripts, and tracked public fixtures
- private end users need a workspace that starts clean and local
- public evaluators need a no-private-data demo path

## Not A Benchmark Track

DocMason currently ships public bundles and public sample fixtures.
It does not maintain a public benchmark package, mirrored benchmark datasets, or a competition submission workflow.

## Channels

### Canonical Source Repository

Use the source repository when you need full repository context.

- includes code, tests, scripts, `sample_corpus/`, planning, and contributor surfaces
- does not track live private corpus, published KB, or runtime state
- use this path for issues, pull requests, and maintenance

### Clean Bundle

Use the clean bundle for private real use.

- no `.git`
- no `tests/`
- empty `original_doc/`, `knowledge_base/`, `runtime/`, and `adapters/`
- includes the distribution manifest and bounded update contract

### Demo Bundle

Use the demo bundle for public product evaluation.

- no `.git`
- no `tests/`
- public sample corpus materialized into `original_doc/`
- empty `knowledge_base/`, `runtime/`, and `adapters/`

## Sample Corpus Boundary

Tracked public demo files live under `sample_corpus/` in the source repository.
They are copied into `original_doc/` only when a contributor explicitly materializes the sample corpus or when the demo bundle is generated.
`sample_corpus/` is not a substitute for the live private corpus boundary.

## What Bundles Do Not Ship

- private documents
- compiled private knowledge bases
- local runtime history
- generated adapters from a specific user's workspace
- contributor test suites or maintainer-only tooling that is irrelevant to bundle use

## Update Behavior

Generated bundles may participate in the bounded release-entry contract documented in [Release Entry And Networking](../policies/release-entry-and-networking.md).
That contract exists only to support update checks and explicit `docmason update-core`; it is not a general telemetry surface.

## Choosing The Right Path

- use the source repository if you are contributing or need full repository context
- use the clean bundle if you want the fastest private start
- use the demo bundle if you want to see the product over public fixtures before loading your own files

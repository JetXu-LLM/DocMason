# Product Notes

DocMason is intended to become a top-tier white-collar document copilot for serious private knowledge work.

The product direction is shaped by a few core beliefs:

- business documents are often visually meaningful, not just textual
- strong AI agents should reason over prepared evidence instead of being limited to brittle text dumps
- deterministic code should prepare evidence, enforce quality, and expose state transitions honestly
- the compiled knowledge base should optimize first for AI-agent use, with human-readable outputs as a secondary concern

The product surface is intentionally layered:

- the public CLI stays small and stable, but it may expand when doing so clearly improves usability and auditability
- `ask` is the one obvious natural-language entry surface for ordinary business questions
- a smaller set of explicit top-level operator workflows handles setup, sync, status, adapter refresh, and runtime review
- inner specialist workflows handle grounded answering, grounded composition, retrieval, provenance tracing, staged authoring, and repair

The public distribution shape is now also intentionally layered:

- the canonical source repository is the contributor surface
- the clean release bundle is the safest private-workspace start
- the demo release bundle is the fastest public product-evaluation start
- tracked public sample fixtures live under `sample_corpus/`, not under live `original_doc/`

See [Distribution Strategy](distribution-and-benchmarks.md) for the deeper rationale.

Phase 4 and Phase 4b established the core operating model:

- deterministic evidence preparation
- staged agent-authored knowledge objects
- validation-gated publication
- incremental maintenance
- retrieval and provenance trace
- explicit execution-orchestration policy
- productized answer and log-review workflows built on the stable CLI rather than new public commands

Phase 5 added the private-first evaluation foundation:

- replayable local benchmark suites over the current published corpus
- scorecards, baselines, and regression comparison for grounded workflow behavior
- structured feedback storage aligned to a frozen taxonomy
- explicit version capture for corpus, retrieval strategy, and grounded-answer workflow surfaces
- a deliberate decision to keep evaluation out of the public CLI while still keeping the operator-quality loop local and open-source

Phase 6 made everyday use feel more natural:

- `ask` became the default front door for ordinary questions
- natural freeform asking became the primary UX, with `@ask` only as an optional adapter-local shortcut
- runtime logs became conversation-native and replayable per turn
- review artifacts gained benchmark-candidate extraction instead of remaining raw log archives

The Phase 6 follow-on extension added the missing real-session bridge:

- native Codex chat history can be reconciled back into DocMason conversation records
- raw user text, screenshots, and attachments can be captured into a private interaction-ingest layer
- pending interaction-derived knowledge is retrievable immediately through a runtime overlay
- sync stages merged interaction memory candidates and publishes them only after the same staged knowledge-authoring quality contract is satisfied
- review summaries demote synthetic evaluation traffic so real operator sessions stay visible

The Phase 6 hardening patch closes the next product gaps:

- `ask` now has a stronger repo-side front-controller substrate rather than relying only on skill discipline
- semantic routing for `ask` now prefers agent-supplied structured analysis rather than large repo-embedded keyword classifiers
- composition-style research and drafting are explicitly separated from direct grounded answering through `grounded-composition`
- `docmason workflow` becomes an advanced public execution surface for explicit workflow-level operator and agent use
- broad interaction memory gains richer semantics and query-time routing instead of naïve flattening
- external-factual asking now has an explicit support contract so externally verified answers do not get mistaken for KB-grounding failures

The public face of the project should remain modern, credible, and easy to understand without overselling unimplemented functionality.

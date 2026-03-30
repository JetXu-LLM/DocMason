# Architecture Notes

DocMason is built around a layered architecture:

1. workspace bootstrap and orchestration
2. deterministic preprocessing and evidence preparation
3. agent-led knowledge construction
4. knowledge compilation and graph maintenance
5. retrieval and trace
6. grounded answer workflow and response contract
7. benchmark, evaluation, and feedback governance
8. operator review surface and manual learning lab
9. adaptive operational learning over private overlays
10. future local web companion

Key architectural commitments:

- file-only persistent knowledge
- multimodal evidence preservation
- Codex on macOS as the current native reference workflow for repository implementation
- strong provenance
- strong validation gates
- elegant adaptation for other agents and environments

Phase 6 now implements the stable primitive, workflow-productization, private-first evaluation, and natural-intent routing layers through:

- `AGENTS.md` as the minimal top-level routing contract for agents
- canonical public workflows under `skills/canonical/`
- per-workflow execution metadata under `skills/canonical/*/workflow.json`
- the user-facing `ask` workflow as the default natural entry surface
- the `docmason` CLI for `prepare`, `doctor`, `status`, `sync`, `retrieve`, `trace`, `validate-kb`, and `sync-adapters`
- local Claude adapter generation from canonical sources
- generated Claude workflow-routing guidance derived from canonical workflow metadata
- staged knowledge-base artifacts under `knowledge_base/staging/`
- immutable published snapshots under `knowledge_base/versions/<snapshot_id>/`
- the compatibility publish pointer under `knowledge_base/current/` plus `knowledge_base/current-pointer.json`
- runtime source identity, dependency, and query-log state under `runtime/`
- runtime conversation-turn state under `runtime/logs/conversations/`
- runtime interaction-ingest state, attachments, overlays, and reconciliation manifests under `runtime/interaction-ingest/`
- validation reports that gate publication
- published retrieval and trace artifacts under the activated published root
- a shared `docmason.source_references` layer that enriches manifests, retrieval records, trace provenance, and turn-linked runtime logs with normalized source aliases and unit locators
- published promoted interaction memories under `knowledge_base/current/interaction/`
- deterministic answer-state classification for answer-first trace runs
- review-friendly runtime summary artifacts under `runtime/logs/review/`
- private evaluation runs, scorecards, feedback records, and operator review packs under `runtime/eval/`
- internal evaluation helpers for replayable suites, baseline comparison, and version capture
- benchmark-candidate extraction derived from runtime logs and conversation turns
- compact derived `reference_resolution_summary` labels in review-facing artifacts rather than full duplicated resolver blocks

What remains later-phase work:

- a richer operator review surface beyond summary artifacts and canonical review workflows
- any decision to expose a public evaluation command after the operator surface matures
- adaptive private overlays
- later automation such as `watch`

Detailed architecture docs will expand further as later roadmap phases are implemented.

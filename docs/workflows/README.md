# Workflow Notes

The current native reference path for workflow documentation is Codex on macOS.

## Stable Public CLI

The public command surface now includes a deterministic substrate plus one advanced workflow entry:

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

All ten commands support `--json`.
The deterministic substrate remains compact, while `docmason workflow` is the advanced execution entry for explicit workflow-level operator and agent use.

## Workflow Layers

The canonical workflow surface includes thirteen workflows, but they are intentionally tiered.

### Default Natural-Language Entry

- `ask`

Inside a valid workspace, ordinary freeform business questions should be treated as `ask` by default.

### Explicit Top-Level Operator Workflows

- `workspace-bootstrap`
- `workspace-doctor`
- `workspace-status`
- `knowledge-base-sync`
- `runtime-log-review`
- `adapter-sync`

These are valid direct routes when the user explicitly asks to prepare the workspace, inspect readiness or status, build or refresh the corpus, review failures, or regenerate adapter guidance.

### Inner Specialist Workflows

- `grounded-answer`
- `grounded-composition`
- `retrieval-workflow`
- `provenance-trace`

These are inner workflows that the agent should invoke when supported answering, retrieval, or provenance analysis is needed.
Ordinary users should not need to name them before asking questions.

### Supporting Construction And Repair Workflows

- `knowledge-construction`
- `validation-repair`

These are follow-on workflows used by sync when staged authoring or repair work is required.
Under the current autonomous sync design, they are compatibility or recovery workflows rather than
the normal operator end state.
After the latest Phase 3 follow-on work, the deterministic sync lane may also leave staged
high-value `semantic_overlay/` opportunities for a capable multimodal host agent. The machine-
readable queue for that work is `knowledge_base/staging/hybrid_work.json`.
Those overlays remain additive sidecars and do not replace the deterministic `artifact_index`,
`visual_layout`, `spreadsheet_*`, or `pdf_document` contracts.
When that queue remains non-empty, treat deterministic publication as successful but hybrid closure
as still pending. The canonical workflow should surface that boundary explicitly rather than calling
the multimodal lane completed.
The current hybrid queue now distinguishes:

- `candidate-prepared` for queued work with no real overlay coverage yet
- `partially-covered` for sources or units where some required slots are covered or explicitly blocked
- `covered` only when the current required slots are satisfied without hidden remaining work

## Default Operating Paths

### First Run

1. Put private source materials into `original_doc/`.
2. For `Path A` or a small first-use corpus, ask naturally through `ask`.
3. Let the ask path reuse the cached ready marker when it is valid, then attempt silent prepare, and delegate to `./scripts/bootstrap-workspace.sh --yes --json` when in-process prepare cannot honestly finish the workspace.
4. If you prefer an explicit operator setup path or are following `Path B`, run `./scripts/bootstrap-workspace.sh --yes`.
5. Run `./.venv/bin/python -m docmason sync` for medium-to-large corpora or whenever governed sync is still required.
6. Ask naturally through `ask`.

`docmason sync-adapters` is not a required step before every first question.
Use it when the chosen agent ecosystem needs generated adapter files or when those files are missing or stale.
If the normal launcher or `prepare` path cannot finish in the current shell or platform, continue
with [`docs/setup/manual-workspace-recovery.md`](../setup/manual-workspace-recovery.md).

### Ordinary Ongoing Usage

1. Treat a natural business question as `ask`.
2. Let the agent classify the question through structured semantic analysis before calling repo helpers. Do not depend on large repo-side keyword tables for the primary routing decision.
3. Route internally to grounded answer, retrieval, provenance trace, or runtime review as needed.
4. Use the narrowest honest evidence basis:
   - workspace or corpus question -> KB-first
   - external factual or latest-state question -> web-first
   - stable low-risk general knowledge -> model knowledge first with honest boundaries
5. If a workspace-dependent question has no published knowledge base, route to workspace bootstrap or knowledge-base sync instead of bluffing.
6. For workspace-dependent asks, reuse `runtime/bootstrap_state.json` as the cached ready marker instead of rerunning deep setup checks every time.
7. If that marker is missing, stale after a repo move, or clearly non-ready, let the ask path attempt silent bootstrap or repair before surfacing manual setup work.
8. If the published knowledge base is stale but still usable, answer with one concise freshness notice only when the answer path actually depends on the workspace corpus.
9. If the question is `workspace-corpus`, the environment is ready, and fresh local state is genuinely needed, let the ask path run its concise auto-sync before answering.
10. If published retrieval says the KB is insufficient because of hard-artifact semantic gaps, prefer a narrowed hybrid refresh over raw source fallback when the environment can inspect renders.
11. For narrowed hybrid refresh, use `recommended_hybrid_targets` plus the current-turn `hybrid_refresh_work.json` packet rather than inventing an ad hoc multimodal scope.

## Odd Question Handling

DocMason should not treat odd questions as a separate zoo of workflow IDs or keyword buckets.

When a user asks about things like:

- visual style
- layout rhythm
- information density
- document tone
- screenshot or media usage
- stakeholder posture or presentation stance

the preferred question is:

- which published evidence channels are actually needed?

The current generic published evidence channels are:

- `text`
- `render`
- `structure`
- `notes`
- `media`

The expected operating rule is:

1. let `ask` or an inner workflow express compact `evidence_requirements`
2. inspect published KB artifacts first
3. escalate to source rerender or direct source inspection only when the published artifacts are genuinely insufficient

This keeps the product surface compact while still supporting non-typical white-collar questions.

For artifact-aware work such as spreadsheets, charts, tables, diagrams, PDF layout, slide
structure, hidden sheets, or compare tasks, agents should keep the user-facing artifact language
in the working query and inspect the published artifact-aware payload directly:

- `matched_artifacts`
- `matched_artifact_ids`
- `focus_render_assets`
- `recommended_hybrid_targets`
- artifact `section_path`, `caption_text`, `continuation_group_ids`, `procedure_hints`
- artifact-aware score details such as `structure_context_bonus`, `semantic_overlay_bonus`, and
  `compare_coverage_bonus`

Soft document aliases should not collapse these queries to one source when the real task is
artifact-level retrieval or cross-source comparison.

## Environment Preparation Notes

- `./scripts/bootstrap-workspace.sh --yes` is the preferred zero-to-working launcher from a raw checkout.
- The launcher should only choose a healthy bootstrap Python and delegate to `docmason prepare --yes`.
- The launcher now rejects broken recursive stubs and startup-silent bootstrap candidates instead of hanging on them.
- Prepared steady-state work should run from repo-local managed Python `3.13` under `.docmason/toolchain/python/`.
- If `uv` is missing, `prepare` should provision it inside `.docmason/toolchain/bootstrap/venv` rather than keeping an externally anchored steady-state runtime.
- The project runtime itself remains isolated inside repo-local `.venv`.
- Once `.venv` exists, prefer `./.venv/bin/python -m docmason ...` or the CLI inside `.venv` for ordinary workspace operations.
- For Office rendering, install LibreOffice before syncing PowerPoint, Word, or Excel sources, including legacy `.ppt`, `.doc`, and `.xls` files.
- For PDF corpora, keep the repo-local PDF stack installed: `PyMuPDF`, `pypdfium2`, `pypdf`, and `pillow`.
- Markdown, plain text, `.eml`, and the lightweight-compatible text-like inputs do not require LibreOffice.
- On macOS with Homebrew, prefer letting the bootstrap launcher or `docmason prepare --yes` auto-install LibreOffice when required, or use `brew install --cask libreoffice-still`.
- On macOS without Homebrew, download the official installer from `https://www.libreoffice.org/download/download/`, open the `.dmg`, and drag LibreOffice into `/Applications`.
- On Linux, install LibreOffice with your distribution's preferred package manager flow or the official download page, then ensure `soffice` is on `PATH`.

## Runtime And Review Notes

- `ask` is the user-facing top-level workflow for natural business questions.
- `grounded-answer` remains the inner specialist workflow for supported answers. It is not a public `docmason answer` command.
- `runtime-log-review` is an explicit operator workflow for recent activity and failure review. It is not a public `docmason review-logs` command.
- `runtime-log-review` refreshes derived review-side outputs and writes one replayable request-level audit artifact under `runtime/logs/review/requests/<request_id>.json` for each explicit invocation.
- `docmason workflow <workflow_id>` is the advanced public workflow surface for explicit workflow execution. It does not replace `ask` as the only natural-language question entry path.
- Retrieval and trace logs are written locally under `runtime/logs/`.
- `runtime/logs/review/summary.json` provides a review-friendly summary over recent sessions and degraded cases.
- `runtime/logs/review/benchmark-candidates.json` suggests future benchmark cases derived from real runtime interactions.
- `runtime/logs/review/summary.json` and `runtime/logs/review/benchmark-candidates.json` are derived outputs; the per-request audit artifact under `runtime/logs/review/requests/` is the canonical review request record.
- Pending interaction-derived knowledge remains distinct from authored source documents and may participate in retrieval before sync-time promotion.

## Deferred Beyond Phase 6 Follow-On

- watch mode
- sync adapters for additional agent ecosystems
- any public `docmason eval` exposure

Those workflows remain planned, but they are not implemented in the current repository state.

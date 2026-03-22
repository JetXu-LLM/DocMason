# Contributing to DocMason

Thank you for contributing.

DocMason is being built as a high-quality open-source repository for multimodal document knowledge work. Contributions should improve the project without weakening its quality bar, privacy posture, or operator experience.

## Before You Start

- Read [README.md](README.md) for the current project status and positioning.
- Do not assume future roadmap items are already implemented.
- Keep changes aligned with the native reference workflow: Codex on macOS first, then elegant adaptation for other agents and environments.

## Hard Requirements

- Write code, comments, documentation, prompts, and public-facing repository text in English.
- Prefer Python unless another language is clearly justified.
- Keep persistent knowledge artifacts file-based.
- Do not introduce a required database service.
- Do not lower the multimodal quality bar just to support weaker agent environments.

## Private Data Policy

- Never commit confidential source documents.
- Never commit compiled private knowledge bases.
- Never commit local planning artifacts intended to stay private.
- Never paste confidential corpus material into public issues or pull requests.
- Keep live `original_doc/`, `knowledge_base/`, `runtime/`, and `adapters/` out of tracked changes.

## Public Sample Corpus Policy

- Tracked public demo fixtures live under `sample_corpus/`, not under live `original_doc/`.
- If you want the canonical public demo corpus in your local workspace, ask the agent to use `public-sample-workspace`, or run `python3 scripts/use-sample-corpus.py --preset ico-gcs`.
- Do not replace `sample_corpus/` with your private corpus.
- If you expand or refresh the public sample corpus, keep the source URLs, license notes, and local-path manifest honest.

## Dependency Policy

- Prefer dependencies installable with `uv` or `pip`.
- Avoid high-friction system dependencies when practical.
- If a change requires heavy OS-level tooling, large office suites, or other intrusive dependencies, document why and get project-owner confirmation first.

## Quality Expectations

- Keep public docs clear and polished.
- Add or update tests when behavior changes.
- Prefer explicit failure and strong validation over weak fallback behavior.
- Keep operator flows simple and obvious.
- Run `python3 scripts/check-repo-safety.py` before opening a PR if you touched workspace boundaries.

## Pull Request Guidance

- Explain what changed and why.
- Call out any tradeoffs introduced by the change.
- Mention any user-facing documentation that also needs updating.
- Mention any heavy dependency or platform implications.
- Call out whether the change affects `sample_corpus/` or release-bundle behavior.

## Local Setup For Contributors

- Clone the canonical repo.
- Install the repo hooks with `./scripts/install-git-hooks.sh`.
- If you want the tracked public demo corpus in your local workspace, ask the agent to use `public-sample-workspace`, or run `python3 scripts/use-sample-corpus.py --preset ico-gcs`.
- Use the canonical repo for issues, branches, tags, and PRs. The clean/demo release bundles are end-user distribution channels, not alternate sources of truth.

## Scope Awareness

The repository is roadmap-driven. If you want to implement a later-phase capability early, first make sure it does not conflict with the current architecture plan and documented direction.

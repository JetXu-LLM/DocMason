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

## Dependency Policy

- Prefer dependencies installable with `uv` or `pip`.
- Avoid high-friction system dependencies when practical.
- If a change requires heavy OS-level tooling, large office suites, or other intrusive dependencies, document why and get project-owner confirmation first.

## Quality Expectations

- Keep public docs clear and polished.
- Add or update tests when behavior changes.
- Prefer explicit failure and strong validation over weak fallback behavior.
- Keep operator flows simple and obvious.

## Pull Request Guidance

- Explain what changed and why.
- Call out any tradeoffs introduced by the change.
- Mention any user-facing documentation that also needs updating.
- Mention any heavy dependency or platform implications.

## Scope Awareness

The repository is roadmap-driven. If you want to implement a later-phase capability early, first make sure it does not conflict with the current architecture plan and documented direction.

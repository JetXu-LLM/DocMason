# DocMason

This is a DocMason workspace. Read the full agent contract below.

@../AGENTS.md

## Claude Code Notes

This file is the committed Claude Code entry surface for this repo.
`AGENTS.md` is the baseline contract.

Bootstrap, repair, or `docmason sync-adapters` may generate repo-local Claude helpers such as:

- `adapters/claude/project-memory.md` for richer workflow routing
- `.claude/skills/` for repo-local Claude skill discovery

Treat those as generated local helpers, not authored instructions.
Do not hand-edit them, create global skill links, or maintain parallel skill copies.
Do not treat low-level Python helpers such as `docmason.ask.prepare_ask_turn()` or
`docmason.ask.complete_ask_turn()` as preferred host entrypoints.
They are internal lifecycle primitives behind the canonical `ask` workflow contract.

The import below is optional enrichment.
If the file exists, Claude gets the richer routing layer.
If it does not exist yet, continue from `AGENTS.md` and the normal bootstrap, status, and sync flows.

@../adapters/claude/project-memory.md

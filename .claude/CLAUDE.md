# DocMason

This is a DocMason workspace. Read the full agent contract below.

@../AGENTS.md

## Claude Code Notes

This file is the committed Claude Code entry surface for this repo.
`AGENTS.md` is the baseline contract.
Nothing below overrides `AGENTS.md`; it only gives Claude-specific entry guidance for the same contract.

For an ordinary user request on Claude Code:

- after `AGENTS.md`, the normal next read is `skills/canonical/ask/SKILL.md`
- then open canonical `ask` through the `Canonical Ask Contract` in that skill and follow the returned route
- do not start with `workspace-status`, `workspace-bootstrap`, source search, CLI help, or ad hoc implementation reading unless the request is clearly explicit operator work or canonical `ask` returns that route

For explicit operator work:

- follow the matching canonical workflow or stable `docmason` CLI from `AGENTS.md`
- do not force setup, status, sync, or review work through the ordinary ask front door

Bootstrap, repair, or `docmason sync-adapters` may generate repo-local Claude helpers such as:

- `adapters/claude/project-memory.md` for richer workflow routing
- `.claude/skills/` for repo-local Claude skill discovery

Treat those as generated local helpers, not authored instructions.
Do not hand-edit them, create global skill links, or maintain parallel skill copies.
The committed `.claude/settings.json` plus `.claude/hooks/` are the repo's Claude host plumbing.
They support native capture, audit, and repo-local shim refresh; they are not a replacement for bootstrap or for canonical ordinary ask execution.
When `.venv` is absent, hook behavior remains best-effort and may only surface bootstrap guidance rather than full workspace capability.
Do not treat low-level Python helpers in `docmason.ask` as preferred host entrypoints.
They are internal lifecycle primitives behind the canonical `ask` workflow contract.
Generated helpers and adapter routing are derived convenience surfaces for Claude Code.
These generated helpers should help Claude follow the authored canonical contract, not replace it.
If generated Claude helpers exist, use them to avoid reverse engineering the repo.
If they do not exist yet, continue directly from `AGENTS.md` and `skills/canonical/ask/SKILL.md`.
Use committed hooks as Claude-side capture and shim plumbing, not as the ordinary ask front door.
Do not reverse engineer `ask.py` or substitute `retrieve` / `trace` for canonical ask completion.
Do not return a final business answer unless the canonical turn has already reached legal completion or governed boundary closure.

The import below is optional enrichment.
If the file exists, Claude gets the richer routing layer.
If it does not exist yet, continue from `AGENTS.md` and the canonical `ask` contract rather than improvising a host entry path.

@../adapters/claude/project-memory.md

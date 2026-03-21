---
name: adapter-sync
description: Generate or refresh the local Claude adapter surface for DocMason from canonical committed sources.
---

# Adapter Sync

Use this skill when the task is to generate or refresh the currently supported adapter artifacts.

This is an explicit adapter-maintenance workflow.
It is not a universal prerequisite before every ordinary `ask` turn.
For non-native compatibility targets such as Claude Code, this is the first adaptation step once workspace bootstrap has determined that generated adapter guidance is needed.

## Required Capabilities

- local file access
- shell or command execution
- ability to regenerate derived files instead of editing them manually

If the agent cannot write local generated files, stop and explain that adapter synchronization cannot proceed.

## Procedure

1. Treat `AGENTS.md`, canonical `SKILL.md` files, and canonical `workflow.json` sidecars as the authored source of truth.
2. Use `claude` as the default target unless the user explicitly asks for another target.
3. Run `docmason status --json` when you need to confirm whether the generated adapter is missing or stale.
4. Run `docmason sync-adapters --json`.
5. If adapter sync fails because canonical sources or workflow metadata are missing or invalid, stop and return that failure to the main agent instead of improvising a manual adapter.
6. If adapter sync succeeds, run `docmason status --json` when you need to confirm the adapter is now fresh.
7. Return the final adapter status judgment to the main agent. Do not delegate adapter regeneration sign-off.

## Escalation Rules

- If the requested target is not implemented, stop and report the current deferral clearly.
- If the generated adapter is stale after canonical source changes, rerun `sync-adapters` instead of hand-editing generated files.

## Completion Signal

- The workflow is complete when `docmason sync-adapters --json` succeeds or returns a clear unsupported-target or metadata-validation failure that the main agent can surface directly.

## Notes

- DocMason currently supports the Claude target only.
- Generated adapters are local derived artifacts and should not be hand-maintained.
- The generated root `CLAUDE.md` imports canonical committed sources through Claude-supported `@path` imports.
- Use this workflow when the chosen agent ecosystem actually depends on generated adapter files, not as a reflex before every workflow.

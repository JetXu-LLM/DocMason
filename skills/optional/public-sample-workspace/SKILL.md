---
name: public-sample-workspace
description: Materialize the tracked ICO + GCS public sample corpus into live original_doc/ for contributor testing inside the canonical source repo.
---

# Public Sample Workspace

Use this skill only when the user explicitly wants a local demo workspace built from the tracked
public sample corpus in the canonical source repository.

This is contributor-oriented setup help.
It should not replace the clean or demo release-bundle onboarding paths.

## Activation

This optional skill is intended to be exposed only when the canonical source repo contains the
tracked `ICO + GCS` sample preset.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect whether `original_doc/` already contains user files

If the current environment cannot inspect or modify the local workspace, stop and explain the
blocker.

## Procedure

1. Confirm that the current repository contains the tracked public sample preset.
2. Inspect `original_doc/` for visible user files before copying anything.
3. If `original_doc/` is non-empty and the user did not explicitly ask to replace it:
   - stop
   - explain that live corpus files already exist
   - ask for explicit confirmation before overwriting them
4. If `original_doc/` is empty, or the user explicitly asked to replace it, run:
   - `python3 scripts/use-sample-corpus.py --preset ico-gcs`
5. Add `--force` only when the user explicitly asked to replace existing live corpus files.
6. Add `--prepare` only when the user explicitly asked to bootstrap the workspace immediately after materialization.
7. Add `--sync` only when the user explicitly asked to build or refresh the knowledge base immediately after materialization.
8. After success, summarize that the tracked public sample corpus was copied into live `original_doc/` and note whether bootstrap or sync also ran.

## Escalation Rules

- Do not overwrite a non-empty `original_doc/` silently.
- Do not run bootstrap or sync as a side effect unless the user asked for it.
- If the tracked preset is missing, stop and report that this checkout does not expose the public sample workspace helper.

## Completion Signal

- The workflow is complete when the public sample corpus has been copied into live `original_doc/`, or when a clear overwrite/missing-preset blocker has been surfaced.

## Notes

- The tracked sample corpus is a contributor and demo fixture, not the normal writable user corpus boundary.
- The direct non-agent equivalent is `python3 scripts/use-sample-corpus.py --preset ico-gcs`.

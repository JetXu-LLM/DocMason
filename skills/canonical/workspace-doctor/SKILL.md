---
name: workspace-doctor
description: Diagnose DocMason workspace readiness and report actionable remediation without mutating the repository.
---

# Workspace Doctor

Use this skill when the task is to inspect the current workspace and explain what is missing or degraded.

## Required Capabilities

- local file access
- shell or command execution
- ability to summarize structured diagnostics

If the agent cannot inspect local files or run commands, stop and explain that the workspace cannot be diagnosed reliably.

## Procedure

1. Run `docmason doctor --json`.
2. Explain blockers first, then degraded conditions, then optional follow-up.
3. If the environment is not ready and `.venv` is absent, direct the next action to `./scripts/bootstrap-workspace.sh --yes`.
4. Otherwise, if the environment is not ready, direct the next action to `docmason prepare --yes`.
5. If `office-renderer` is blocked:
   - on macOS with Homebrew, recommend `brew install --cask libreoffice`
   - on macOS without Homebrew, recommend the official installer from `https://www.libreoffice.org/download/download/`
   - on Linux, recommend the distro package manager or official packages, then re-run `doctor`
6. Return the diagnosis to the main agent without mutating workspace state.

## Escalation Rules

- `doctor` is read-only. Do not silently switch into setup or repair work from inside this workflow.
- If multiple blockers exist, preserve their ordering and do not hide the highest-severity one behind a degraded follow-up item.

## Completion Signal

- The workflow is complete when the main agent has a clear blocker and degraded-condition summary plus the next obvious action.

## Notes

- `doctor` is read-only.
- Treat unsupported platforms, unsupported Python versions, and missing editable-install availability as blockers.
- Treat missing `uv`, stale adapters, and an empty source corpus as degraded conditions rather than hard blockers.
- Treat missing LibreOffice as a blocker only when the current corpus contains PPTX, DOCX, or XLSX files.
- A stale or missing generated adapter matters only when the current flow depends on that adapter surface.

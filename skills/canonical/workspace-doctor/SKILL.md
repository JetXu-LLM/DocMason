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
   - treat the launcher as the governed cold-start preflight and bounded liveness gate for bootstrap Python selection, not as an unbounded trust of whatever `python3` happens to be first on `PATH`
   - on native Codex, the ordinary path should not branch into manual cache probing or manual recovery before that governed launcher settles
4. Otherwise, if the environment is not ready, direct the next action to `docmason prepare --yes`.
   - treat `mixed` and `degraded` toolchain states as repair-needed, not ordinary ask-time ready
5. If `doctor` reports a control-plane blocker or pending confirmation, surface that blocker before lower-severity degraded conditions.
   - for pending native Codex host-access upgrade, direct the next action to switching the thread to `Full access`
   - for pending high-intrusion prepare, direct the next action to `docmason prepare --yes`
   - for pending material sync, direct the next action to `docmason sync --yes`
6. If `office-renderer` is blocked:
   - on macOS with Homebrew already present, recommend `brew install --cask libreoffice-still` or `docmason prepare --yes`
   - on macOS without Homebrew, recommend `docmason prepare --yes` or the official installer from `https://www.libreoffice.org/download/download/`
   - on Linux, recommend the distro package manager or official packages, then re-run `doctor`
7. If the normal launcher or `prepare` path still cannot complete honestly after the governed automatic path has already had enough access, point the deeper fallback to `docs/setup/manual-workspace-recovery.md`.
8. Return the diagnosis to the main agent without mutating workspace state.

## Escalation Rules

- `doctor` is read-only. Do not silently switch into setup or repair work from inside this workflow.
- If multiple blockers exist, preserve their ordering and do not hide the highest-severity one behind a degraded follow-up item.
- Do not downgrade a live control-plane confirmation blocker into a generic degraded suggestion.

## Completion Signal

- The workflow is complete when the main agent has a clear blocker and degraded-condition summary plus the next obvious action.

## Notes

- `doctor` is read-only.
- Treat unsupported platforms, unsupported Python versions, and missing editable-install availability as blockers.
- Treat only a `self-contained` prepared toolchain as ready for ordinary workspace asks.
- Treat missing `uv`, stale adapters, and an empty source corpus as degraded conditions rather than hard blockers.
- On native Codex/macOS, treat missing LibreOffice as a blocker only when the workspace currently depends on Office rendering. Missing Homebrew alone is informational.
- On native Codex/macOS, treat `Default permissions` versus `Full access` as an explicit first-class boundary; a per-command `Yes` popup is not equivalent to switching the thread to `Full access`.
- Outside that native machine-baseline policy, treat missing LibreOffice as a blocker only when the current corpus contains PPTX, DOCX, or XLSX files.
- For Claude Code or another compatibility host, keep any higher-access fallback wording brief and host-generic instead of copying the Codex-specific wording.
- A stale or missing generated adapter matters only when the current flow depends on that adapter surface.

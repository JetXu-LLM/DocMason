---
name: workspace-bootstrap
description: Prepare a DocMason repository for local operation by bootstrapping the environment, creating required directories, and recording runtime state.
---

# Workspace Bootstrap

Use this skill when the task is to make a DocMason workspace ready for use, or when `ask` has discovered that the workspace is not yet ready for a safe answer path.

This is also the correct first explicit setup workflow when the current agent is not running on the native Codex path and needs to determine whether adapter-specific guidance should be refreshed.

## Required Capabilities

- local file access
- shell or command execution
- ability to inspect command output

If the agent cannot perform these capabilities, stop and explain that the environment is not capable enough for the workflow.

## Procedure

1. Inspect `runtime/bootstrap_state.json` when it exists.
   - if it already says the current workspace root is ready and `.venv` still exists, do not
     rerun deep bootstrap work by default
   - if it belongs to another workspace root, treat that as a moved-repo repair case
   - treat `self-contained` as the only ordinary ask-time ready environment grade
     - in practice this means the repo-local steady-state runtime is trusted for ordinary work
   - treat `mixed` and `degraded` as repair-needed states
2. If `.venv` is absent or `docmason` is not yet runnable from the repo-local environment, start with:
   - `./scripts/bootstrap-workspace.sh --yes`
   - add `--json` when machine-readable output helps
   - the launcher should prefer the controlled UV bootstrap asset path for ordinary native cold starts instead of broad local-machine Python scavenging
   - when that path is needed, it should fetch the bootstrap asset into a shared user cache and use it only to create the repo-local bootstrap helper venv
   - the launcher may honor an explicit `DOCMASON_BOOTSTRAP_PYTHON` override for operator repair, but that is not the ordinary first-run path
3. Once the launcher succeeds, prefer the repo-local environment for subsequent commands:
   - `./.venv/bin/python -m docmason doctor --json`
   - `./.venv/bin/python -m docmason prepare --json --yes`
   - or the `docmason` executable installed inside `.venv`
4. Run `docmason doctor --json` when you need a readiness snapshot after launcher completion or on an already prepared workspace.
5. Run `docmason prepare --json --yes` when the launcher was not used, or when bootstrap needs an explicit rerun to repair or complete the repo-local environment.
6. If `prepare` reports a degraded result, follow the reported next steps and rerun only the necessary deterministic command.
7. If the launcher or `prepare` cannot finish because the current shell, platform, or path shape falls outside the normal automation path, continue with `docs/setup/manual-workspace-recovery.md`.
8. If the corpus already contains PPTX, DOCX, or XLSX files and LibreOffice is missing:
   - on macOS with Homebrew, run `brew install --cask libreoffice-still`
   - on macOS without Homebrew, install LibreOffice from `https://www.libreoffice.org/download/download/`
   - on Linux, install LibreOffice with the distro package manager or the official packages, then ensure `soffice` is on `PATH`
   - on native Codex/macOS cold starts, treat Homebrew and LibreOffice as standard machine baseline work rather than as a late surprise dependency
9. Run `docmason status --json` when you need to confirm the resulting workspace stage.
10. Recommend `docmason sync --json` when source files are present and the user needs a usable knowledge base next.
11. If the current agent ecosystem is a compatibility target such as Claude Code rather than the native Codex path, decide here whether generated adapter guidance is needed.
12. Recommend `docmason sync-adapters --json` only when the current agent ecosystem depends on generated adapter files or those files are missing or stale.
13. Once `.venv` exists, prefer the repo-local interpreter for subsequent repository commands instead of switching back to an arbitrary system Python.
14. Return the final readiness judgment to the main agent. Do not delegate environment sign-off.

## Escalation Rules

- If the platform or Python version is unsupported, stop and surface that blocker directly.
- If `prepare` can only proceed through a higher-intrusion install step, explain it explicitly rather than hiding it inside automation.
- If system-level installation requires additional permissions, request them when the current platform supports that flow; otherwise give the user the exact command or GUI step to run.
- On native Codex/macOS, if the thread is still in `Default permissions` and machine-level setup is required, stop once with an explicit `Full access` upgrade instruction. Do not keep asking lower-level machine-inspection questions.
- Deterministic shell setup steps may run as background or main-agent commands, but the final environment judgment returns to the main agent.

## Completion Signal

- The workflow is complete when `prepare` and follow-up readiness checks leave the workspace ready, or when an actionable environment blocker has been surfaced to the main agent.

## Notes

- `prepare` bootstraps repo-local state only.
- `./scripts/bootstrap-workspace.sh --yes` is the preferred zero-to-working launcher from a raw checkout because it can prepare `.venv` before the package is importable from the `src/` layout.
- The launcher now probes bootstrap-Python liveness in bounded time and prefers repo-local candidates before shared ones.
- `runtime/bootstrap_state.json` is the cached ready marker that ordinary ask-time work should reuse.
- The steady-state runtime is repo-local managed Python `3.13` under `.docmason/toolchain/python/`.
- On the native Codex path, bootstrap should refresh repo-local skill shims under `.agents/skills/` rather than writing into `~/.codex/skills`.
- `prepare` may use shared/system Python only as a bootstrap or repair helper; ordinary steady-state commands should not depend on it.
- When `uv` is missing, `prepare` should provision the repo-local bootstrap helper venv under `.docmason/toolchain/bootstrap/venv` and install `uv` there.
- On the native macOS path, ordinary cold starts should prefer the controlled UV bootstrap asset path first, then auto-attempt supported machine-baseline installs such as Homebrew and LibreOffice when host access allows it.
- After preparation, prefer `./.venv/bin/python -m docmason ...` or the CLI installed inside `.venv` for ordinary workspace operations.
- For Office rendering, DocMason detects the standard macOS `soffice` path inside `/Applications/LibreOffice.app/Contents/MacOS/soffice`, so shell-profile changes are usually unnecessary.
- Not every Codex-first first-answer path requires `sync-adapters` before work can proceed.
- Avoid shell-profile mutation unless it is clearly required and explicitly explained to the user.
- Do not silently upgrade the host from Codex `Default permissions` to `Full access`; explain that boundary explicitly.

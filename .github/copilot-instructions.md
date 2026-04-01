# DocMason GitHub Copilot Notes

Start by reading `AGENTS.md`.
`AGENTS.md` is the baseline repository contract.
This file is only the minimal GitHub Copilot adaptation layer for the current workspace.

For an ordinary user request in GitHub Copilot:

- after `AGENTS.md`, the normal next read is `skills/canonical/ask/SKILL.md`
- then open canonical `ask` through the `Canonical Ask Contract` in that skill and follow the returned route
- do not start with `workspace-status`, `workspace-bootstrap`, source search, CLI help, or ad hoc implementation reading unless the request is clearly explicit operator work or canonical `ask` returns that route
- do not treat direct `retrieve` or `trace` output as completion of the ask contract

For explicit operator work:

- follow the matching canonical workflow or stable `docmason` CLI from `AGENTS.md`
- do not force setup, status, sync, or review work through the ordinary ask front door

## Copilot-Specific Delta

- Do not assume Codex-native behavior is available just because repository guidance mentions Codex as the native reference path.
- Use the current GitHub Copilot tool surface to achieve the same repository outcome when the exact Codex interaction model is different.
- If a Codex-oriented note assumes implicit image visibility, use explicit image inspection tools such as `view_image`, or use published multimodal artifacts such as `visual_layout` and render-linked evidence instead of assuming that file paths are already visually available.
- Treat differences in transcript fidelity, attachment capture, or image visibility as current adapter-surface differences unless the repository explicitly states a product limitation.
- If a Codex-oriented workflow mentions main and sub agents, keep the main agent responsible for critical-path reasoning, mutations, publication, and final sign-off. Use a subagent only when the current environment explicitly supports it and the delegated work is read-only or clearly disjoint.
- Prefer equivalent file reads, searches, validation surfaces, terminal commands, and published artifact inspection over inventing Copilot-only behavior.
- Do not assume Claude-specific helpers, generated skill shims, or interaction-history enrichments are loaded unless the current GitHub Copilot environment explicitly exposes them.
- Keep the adaptation minimal. Do not create a parallel GitHub Copilot product contract when the existing repository contract already applies.

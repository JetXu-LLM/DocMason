# Git Hooks

This directory contains lightweight contributor safety hooks for the canonical source repository.

They are intentionally:

- committed and reviewable
- opt-in after clone
- fast
- local-only
- focused on protecting DocMason's private workspace boundary

These hooks are not installed automatically when someone clones the repository.
Contributors enable them explicitly with:

```bash
./scripts/install-git-hooks.sh
```

Current behavior:

- `pre-commit` checks staged changes and blocks newly staged paths under live private workspace directories such as `original_doc/`, `knowledge_base/`, `runtime/`, and `adapters/`
- `pre-push` re-checks the current tracked tree as a final guardrail before publishing commits

These hooks do not run networked steps, long test suites, or destructive commands.

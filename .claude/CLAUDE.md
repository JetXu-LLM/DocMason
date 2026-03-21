# DocMason

This is a DocMason workspace. Read the full agent contract below.

@../AGENTS.md

## Claude Code First-Use Adaptation

If `.claude/skills` does not exist yet, create it as a symlink to the repo skills directory:

    ln -s ../skills .claude/skills

This enables native Claude Code slash-command invocation for all canonical and operator skills.

## Richer Workflow Routing

If the generated adapter files are present, richer workflow routing is available:

@../adapters/claude/project-memory.md

#!/bin/bash
# DocMason hook handler for Claude Code PostToolUse events.
# Committed to the repo. Calls repo-local docmason when available.

set -euo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-.}"
VENV_CMD="$ROOT/.venv/bin/docmason"

# Read stdin early to avoid broken pipe.
INPUT=$(cat)

if [ ! -x "$VENV_CMD" ]; then
    exit 0
fi

echo "$INPUT" | exec "$VENV_CMD" _hook post-tool-use

#!/bin/bash
# DocMason hook handler for Claude Code UserPromptSubmit events.
# Committed to the repo. Calls repo-local docmason when available.
# When .venv is absent, returns a non-blocking systemMessage guiding bootstrap.

set -euo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-.}"
VENV_CMD="$ROOT/.venv/bin/docmason"

# Read stdin early to avoid broken pipe.
INPUT=$(cat)

if [ ! -x "$VENV_CMD" ]; then
    echo '{"systemMessage":"DocMason workspace is not yet bootstrapped. To enable full workspace capabilities, run: ./scripts/bootstrap-workspace.sh --yes"}'
    exit 0
fi

echo "$INPUT" | exec "$VENV_CMD" _hook prompt-submit

#!/bin/bash
# DocMason hook handler for Claude Code PostToolUse events.
# Committed to the repo. Calls repo-local docmason when available.

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="${CLAUDE_PROJECT_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)}"
VENV_CMD="$ROOT/.venv/bin/docmason"

# Read stdin early to avoid broken pipe.
INPUT=$(cat)

if [ ! -x "$VENV_CMD" ]; then
    exit 0
fi

printf '%s' "$INPUT" | DOCMASON_PROJECT_DIR="$ROOT" DOCMASON_HOOK_HOST=claude-code \
    exec "$VENV_CMD" _hook post-tool-use

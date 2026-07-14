#!/bin/bash
# Thin ChatGPT Work/Codex UserPromptSubmit adapter for the shared Hook core.

set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="${DOCMASON_PROJECT_DIR:-$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)}"
VENV_CMD="$ROOT/.venv/bin/docmason"
INPUT=$(cat)

if [ ! -x "$VENV_CMD" ]; then
    exit 0
fi

printf '%s' "$INPUT" | DOCMASON_PROJECT_DIR="$ROOT" DOCMASON_HOOK_HOST=codex \
    exec "$VENV_CMD" _hook prompt-submit

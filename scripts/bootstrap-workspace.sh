#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ASSUME_YES=0
JSON_FLAG=""

for arg in "$@"; do
  case "$arg" in
    --yes)
      ASSUME_YES=1
      ;;
    --json)
      JSON_FLAG="--json"
      ;;
  esac
done

log() {
  if [[ -n "$JSON_FLAG" ]]; then
    printf '%s\n' "$*" >&2
  else
    printf '%s\n' "$*"
  fi
}

fail() {
  log "$*"
  exit 1
}

python_is_supported() {
  local candidate="$1"
  "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    >/dev/null 2>&1
}

resolve_candidate() {
  local candidate="$1"
  if command -v "$candidate" >/dev/null 2>&1; then
    command -v "$candidate"
  else
    printf '%s\n' "$candidate"
  fi
}

find_supported_python() {
  local candidates=()
  local candidate resolved

  if [[ -n "${DOCMASON_BOOTSTRAP_PYTHON:-}" ]]; then
    candidates+=("${DOCMASON_BOOTSTRAP_PYTHON}")
  fi
  candidates+=(
    python3.14
    python3.13
    python3.12
    python3.11
    python3
    python
    /opt/homebrew/bin/python3
    /opt/homebrew/bin/python3.14
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    /usr/local/bin/python3
    /usr/local/bin/python3.14
    /usr/local/bin/python3.13
    /usr/local/bin/python3.12
    /usr/local/bin/python3.11
  )

  for candidate in "${candidates[@]}"; do
    resolved="$(resolve_candidate "$candidate")"
    if [[ ! -x "$resolved" ]]; then
      continue
    fi
    if python_is_supported "$resolved"; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  return 1
}

find_brew() {
  if command -v brew >/dev/null 2>&1; then
    command -v brew
  fi
}

find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
  fi
}

update_user_bin_path() {
  local python_bin="$1"
  local user_base
  user_base="$("$python_bin" -m site --user-base 2>/dev/null || true)"
  if [[ -n "$user_base" ]]; then
    export PATH="$user_base/bin:$PATH"
  fi
}

install_supported_python() {
  local brew_bin="$1"
  log "Installing supported Python with Homebrew..."
  "$brew_bin" install python
}

install_uv() {
  local python_bin="$1"
  local brew_bin="$2"
  if [[ -n "$brew_bin" ]]; then
    log "Installing uv with Homebrew..."
    "$brew_bin" install uv
    return 0
  fi
  log "Installing uv with user-scoped pip..."
  "$python_bin" -m pip install --user uv
}

BREW_BIN="$(find_brew || true)"
PYTHON_BIN="$(find_supported_python || true)"

if [[ -z "$PYTHON_BIN" && -n "$BREW_BIN" ]]; then
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    install_supported_python "$BREW_BIN"
    hash -r
    PYTHON_BIN="$(find_supported_python || true)"
  else
    fail "No supported Python 3.11+ interpreter was found. Rerun with --yes to allow automated installation through Homebrew."
  fi
fi

if [[ -z "$PYTHON_BIN" ]]; then
  fail "Could not find a supported Python 3.11+ interpreter. On macOS, install Homebrew and rerun this launcher, or provide one via DOCMASON_BOOTSTRAP_PYTHON."
fi

update_user_bin_path "$PYTHON_BIN"

UV_BIN="$(find_uv || true)"
if [[ -z "$UV_BIN" ]]; then
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    install_uv "$PYTHON_BIN" "$BREW_BIN"
    hash -r
    update_user_bin_path "$PYTHON_BIN"
    UV_BIN="$(find_uv || true)"
  else
    fail "uv is missing. Rerun with --yes to allow the automated install attempt."
  fi
fi

if [[ -z "$UV_BIN" ]]; then
  fail "Could not find uv after the automated install attempt."
fi

cd "$ROOT"
log "Creating or repairing .venv..."
"$UV_BIN" venv --allow-existing --python "$PYTHON_BIN" "$ROOT/.venv"

log "Installing DocMason into .venv..."
"$UV_BIN" pip install --python "$ROOT/.venv/bin/python" -e ".[dev]"

exec "$ROOT/.venv/bin/python" -m docmason prepare --yes ${JSON_FLAG:+$JSON_FLAG}

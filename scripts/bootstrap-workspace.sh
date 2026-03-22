#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANUAL_RECOVERY_DOC="docs/setup/manual-workspace-recovery.md"
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
  log "Manual fallback: see $MANUAL_RECOVERY_DOC"
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

homebrew_prefix() {
  if [[ "$(uname -m)" == "arm64" ]]; then
    printf '%s\n' "/opt/homebrew"
  else
    printf '%s\n' "/usr/local"
  fi
}

refresh_brew_path() {
  local prefix
  prefix="$(homebrew_prefix)"
  if [[ -x "$prefix/bin/brew" ]]; then
    export PATH="$prefix/bin:$PATH"
  fi
}

homebrew_auto_install_feasible() {
  local prefix parent
  [[ "$(uname -s)" == "Darwin" ]] || return 1
  [[ -x /bin/bash ]] || return 1
  [[ -x /usr/bin/curl ]] || return 1
  [[ -x /usr/bin/xcode-select ]] || return 1
  /usr/bin/xcode-select -p >/dev/null 2>&1 || return 1
  prefix="$(homebrew_prefix)"
  if [[ -e "$prefix" ]]; then
    [[ -w "$prefix" ]] || return 1
  else
    parent="$(dirname "$prefix")"
    [[ -w "$parent" ]] || return 1
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

ensure_pip() {
  local python_bin="$1"
  if "$python_bin" -m pip --version >/dev/null 2>&1; then
    return 0
  fi
  log "Restoring pip with ensurepip..."
  "$python_bin" -m ensurepip --upgrade >/dev/null 2>&1 || return 1
  "$python_bin" -m pip --version >/dev/null 2>&1
}

install_homebrew() {
  log "Installing Homebrew with the official unattended installer..."
  NONINTERACTIVE=1 /bin/bash -c "$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
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
  ensure_pip "$python_bin" || return 1
  log "Installing uv with user-scoped pip..."
  "$python_bin" -m pip install --user uv
}

create_with_uv() {
  local uv_bin="$1"
  local python_bin="$2"
  log "Creating or repairing .venv with uv..."
  "$uv_bin" venv --allow-existing --python "$python_bin" "$ROOT/.venv"
}

install_with_uv() {
  local uv_bin="$1"
  log "Installing DocMason into .venv with uv..."
  "$uv_bin" pip install --python "$ROOT/.venv/bin/python" -e ".[dev]"
}

create_with_venv() {
  local python_bin="$1"
  log "Creating or repairing .venv with venv..."
  "$python_bin" -m venv "$ROOT/.venv"
}

install_with_pip() {
  log "Installing DocMason into .venv with pip..."
  "$ROOT/.venv/bin/python" -m pip install --upgrade pip
  "$ROOT/.venv/bin/python" -m pip install -e ".[dev]"
}

BREW_BIN="$(find_brew || true)"
refresh_brew_path
BREW_BIN="$(find_brew || true)"
PYTHON_BIN="$(find_supported_python || true)"

if [[ -z "$PYTHON_BIN" && -z "$BREW_BIN" && "$ASSUME_YES" -eq 1 ]]; then
  if homebrew_auto_install_feasible; then
    install_homebrew || fail "Could not install Homebrew through the official unattended path."
    hash -r
    refresh_brew_path
    BREW_BIN="$(find_brew || true)"
  fi
fi

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
    if install_uv "$PYTHON_BIN" "$BREW_BIN"; then
      hash -r
      update_user_bin_path "$PYTHON_BIN"
      UV_BIN="$(find_uv || true)"
    else
      log "The automated uv install attempt failed; the launcher will fall back to repo-local venv + pip."
    fi
  else
    fail "uv is missing. Rerun with --yes to allow the automated install attempt."
  fi
fi

cd "$ROOT"
USE_UV=1
if [[ -z "$UV_BIN" ]]; then
  USE_UV=0
  log "uv is unavailable after the automated install attempt; falling back to repo-local venv + pip."
fi

if [[ "$USE_UV" -eq 1 ]]; then
  if ! create_with_uv "$UV_BIN" "$PYTHON_BIN"; then
    log "uv failed while creating .venv; falling back to repo-local venv + pip."
    USE_UV=0
  elif ! install_with_uv "$UV_BIN"; then
    log "uv failed while installing DocMason; falling back to repo-local venv + pip."
    USE_UV=0
  fi
fi

if [[ "$USE_UV" -eq 0 ]]; then
  create_with_venv "$PYTHON_BIN" || fail "Could not create the repo-local virtual environment."
  install_with_pip || fail "Could not install DocMason into the repo-local virtual environment."
fi

exec "$ROOT/.venv/bin/python" -m docmason prepare --yes ${JSON_FLAG:+$JSON_FLAG}

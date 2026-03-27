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
  local first_line=""
  local probe_pid=""
  local ticks=0

  if [[ ! -x "$candidate" ]]; then
    return 1
  fi

  if command -v file >/dev/null 2>&1; then
    if file "$candidate" 2>/dev/null | grep -qi 'Python script text executable'; then
      first_line="$(head -n 1 "$candidate" 2>/dev/null || true)"
      if [[ "$first_line" == '#!/usr/bin/env python3' || "$first_line" == '#!/usr/bin/env python' ]]; then
        return 1
      fi
    fi
  fi

  "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    >/dev/null 2>&1 &
  probe_pid="$!"
  while kill -0 "$probe_pid" >/dev/null 2>&1; do
    if (( ticks >= 30 )); then
      pkill -P "$probe_pid" >/dev/null 2>&1 || true
      kill "$probe_pid" >/dev/null 2>&1 || true
      sleep 0.1
      pkill -P "$probe_pid" >/dev/null 2>&1 || true
      kill -9 "$probe_pid" >/dev/null 2>&1 || true
      wait "$probe_pid" >/dev/null 2>&1 || true
      return 1
    fi
    sleep 0.1
    ticks=$((ticks + 1))
  done
  wait "$probe_pid" >/dev/null 2>&1
}

resolve_candidate() {
  local candidate="$1"
  if command -v "$candidate" >/dev/null 2>&1; then
    command -v "$candidate"
  else
    printf '%s\n' "$candidate"
  fi
}

find_repo_local_bootstrap_python() {
  local candidates=(
    "$ROOT/.docmason/toolchain/python/current/bin/python3.13"
    "$ROOT/.docmason/toolchain/bootstrap/venv/bin/python"
  )
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]] && python_is_supported "$candidate"; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

find_supported_shared_python() {
  local candidates=()
  local candidate resolved

  if [[ -n "${DOCMASON_BOOTSTRAP_PYTHON:-}" ]]; then
    candidates+=("${DOCMASON_BOOTSTRAP_PYTHON}")
  fi
  candidates+=(
    python3.13
    python3.12
    python3.11
    python3
    python
    /opt/homebrew/bin/python3.13
    /opt/homebrew/bin/python3.12
    /opt/homebrew/bin/python3.11
    /opt/homebrew/bin/python3
    /usr/local/bin/python3.13
    /usr/local/bin/python3.12
    /usr/local/bin/python3.11
    /usr/local/bin/python3
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

install_homebrew() {
  log "Installing Homebrew with the official unattended installer..."
  NONINTERACTIVE=1 /bin/bash -c "$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
}

install_supported_python() {
  local brew_bin="$1"
  log "Installing a shared bootstrap Python with Homebrew..."
  "$brew_bin" install python
}

launch_prepare() {
  local python_bin="$1"
  shift
  cd "$ROOT"
  export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  exec "$python_bin" -m docmason prepare --yes "$@"
}

BOOTSTRAP_PYTHON="$(find_repo_local_bootstrap_python || true)"
if [[ -n "$BOOTSTRAP_PYTHON" ]]; then
  if [[ "$BOOTSTRAP_PYTHON" == "$ROOT/.docmason/toolchain/python/current/bin/python3.13" ]]; then
    log "Using the repo-local managed Python repair path."
  else
    log "Using the repo-local bootstrap helper venv."
  fi
  launch_prepare "$BOOTSTRAP_PYTHON" ${JSON_FLAG:+$JSON_FLAG}
fi

BREW_BIN="$(find_brew || true)"
refresh_brew_path
BREW_BIN="$(find_brew || true)"
BOOTSTRAP_PYTHON="$(find_supported_shared_python || true)"

if [[ -z "$BOOTSTRAP_PYTHON" && -z "$BREW_BIN" && "$ASSUME_YES" -eq 1 ]]; then
  if homebrew_auto_install_feasible; then
    install_homebrew || fail "Could not install Homebrew through the official unattended path."
    hash -r
    refresh_brew_path
    BREW_BIN="$(find_brew || true)"
  fi
fi

if [[ -z "$BOOTSTRAP_PYTHON" && -n "$BREW_BIN" ]]; then
  if [[ "$ASSUME_YES" -eq 1 ]]; then
    install_supported_python "$BREW_BIN" || fail "Could not install a supported bootstrap Python with Homebrew."
    hash -r
    BOOTSTRAP_PYTHON="$(find_supported_shared_python || true)"
  else
    fail "No supported Python 3.11+ bootstrap interpreter was found. Rerun with --yes to allow automated installation through Homebrew."
  fi
fi

if [[ -z "$BOOTSTRAP_PYTHON" ]]; then
  fail "Could not find a supported Python 3.11+ bootstrap interpreter. Install one or provide it via DOCMASON_BOOTSTRAP_PYTHON."
fi

log "Using shared bootstrap Python to provision the repo-local managed Python 3.13 workspace."
launch_prepare "$BOOTSTRAP_PYTHON" ${JSON_FLAG:+$JSON_FLAG}

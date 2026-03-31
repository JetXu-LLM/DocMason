#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANUAL_RECOVERY_DOC="docs/setup/manual-workspace-recovery.md"
ASSUME_YES=0
JSON_FLAG=""
BOOTSTRAP_UV_INSTALLER_URL="${DOCMASON_BOOTSTRAP_UV_INSTALLER_URL:-https://astral.sh/uv/install.sh}"
BOOTSTRAP_PYTHON_REQUEST="${DOCMASON_BOOTSTRAP_PYTHON_VERSION:-3.13}"

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

  # A path-bound Python script wrapper is not a stable bootstrap interpreter boundary.
  first_line="$(head -n 1 "$candidate" 2>/dev/null || true)"
  if [[ "$first_line" == '#!/usr/bin/env python3' || "$first_line" == '#!/usr/bin/env python' ]]; then
    return 1
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

launch_prepare() {
  local python_bin="$1"
  local bootstrap_source="$2"
  shift 2
  cd "$ROOT"
  export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
  export DOCMASON_BOOTSTRAP_SOURCE="$bootstrap_source"
  exec "$python_bin" -m docmason prepare --yes "$@"
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

shared_bootstrap_cache_root() {
  if [[ -n "${DOCMASON_SHARED_BOOTSTRAP_CACHE:-}" ]]; then
    printf '%s\n' "${DOCMASON_SHARED_BOOTSTRAP_CACHE}"
    return 0
  fi
  if [[ "$(uname -s)" == "Darwin" && -n "${HOME:-}" ]]; then
    printf '%s\n' "$HOME/Library/Caches/DocMason/bootstrap"
    return 0
  fi
  if [[ -n "${XDG_CACHE_HOME:-}" ]]; then
    printf '%s\n' "$XDG_CACHE_HOME/docmason/bootstrap"
    return 0
  fi
  if [[ -n "${HOME:-}" ]]; then
    printf '%s\n' "$HOME/.cache/docmason/bootstrap"
    return 0
  fi
  printf '%s\n' "$ROOT/.docmason/toolchain/bootstrap/cache"
}

ensure_bootstrap_cache_root() {
  local cache_root="$1"
  mkdir -p "$cache_root"
}

install_controlled_bootstrap_uv() {
  local cache_root="$1"
  local installer_path="$cache_root/uv-installer.sh"
  local unmanaged_dir="$cache_root/uv-unmanaged"
  local uv_bin="$unmanaged_dir/uv"

  if [[ -x "$uv_bin" ]]; then
    printf '%s\n' "$uv_bin"
    return 0
  fi

  command -v curl >/dev/null 2>&1 || fail "The controlled bootstrap asset requires `curl`."
  command -v sh >/dev/null 2>&1 || fail "The controlled bootstrap asset requires `/bin/sh`."

  log "Downloading the controlled UV bootstrap asset..."
  curl -LsSf "$BOOTSTRAP_UV_INSTALLER_URL" -o "$installer_path" \
    || fail "Could not download the controlled UV bootstrap asset."

  log "Installing the controlled UV bootstrap asset..."
  mkdir -p "$unmanaged_dir"
  UV_UNMANAGED_INSTALL="$unmanaged_dir" UV_NO_MODIFY_PATH=1 sh "$installer_path" \
    || fail "Could not install the controlled UV bootstrap asset."

  if [[ ! -x "$uv_bin" && -x "$unmanaged_dir/bin/uv" ]]; then
    uv_bin="$unmanaged_dir/bin/uv"
  fi
  [[ -x "$uv_bin" ]] || fail "The controlled UV bootstrap asset did not produce a runnable `uv` binary."
  printf '%s\n' "$uv_bin"
}

create_repo_local_bootstrap_venv() {
  local uv_bin="$1"
  local bootstrap_python="$ROOT/.docmason/toolchain/bootstrap/venv/bin/python"

  if [[ -x "$bootstrap_python" ]] && python_is_supported "$bootstrap_python"; then
    printf '%s\n' "$bootstrap_python"
    return 0
  fi

  mkdir -p "$ROOT/.docmason/toolchain/bootstrap"
  mkdir -p "$ROOT/.docmason/toolchain/cache/uv"
  log "Creating the repo-local bootstrap helper venv through the controlled UV bootstrap asset..."
  UV_CACHE_DIR="$ROOT/.docmason/toolchain/cache/uv" \
    "$uv_bin" venv --python "$BOOTSTRAP_PYTHON_REQUEST" "$ROOT/.docmason/toolchain/bootstrap/venv" \
    || fail "Could not create the repo-local bootstrap helper venv from the controlled bootstrap asset."

  [[ -x "$bootstrap_python" ]] || fail "The repo-local bootstrap helper venv was created without a runnable Python."
  printf '%s\n' "$bootstrap_python"
}

if [[ -n "${DOCMASON_BOOTSTRAP_PYTHON:-}" ]] && python_is_supported "${DOCMASON_BOOTSTRAP_PYTHON}"; then
  log "Using the explicit manual bootstrap Python override."
  launch_prepare "${DOCMASON_BOOTSTRAP_PYTHON}" "manual-bootstrap-python" ${JSON_FLAG:+$JSON_FLAG}
fi

BOOTSTRAP_PYTHON="$(find_repo_local_bootstrap_python || true)"
if [[ -n "$BOOTSTRAP_PYTHON" ]]; then
  if [[ "$BOOTSTRAP_PYTHON" == "$ROOT/.docmason/toolchain/python/current/bin/python3.13" ]]; then
    log "Using the repo-local managed Python repair path."
    launch_prepare "$BOOTSTRAP_PYTHON" "repo-local-managed" ${JSON_FLAG:+$JSON_FLAG}
  fi
  log "Using the repo-local bootstrap helper venv."
  launch_prepare "$BOOTSTRAP_PYTHON" "repo-local-bootstrap-venv" ${JSON_FLAG:+$JSON_FLAG}
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  fail "No supported repo-local bootstrap runtime is available yet. Rerun with --yes to allow the controlled bootstrap asset path."
fi

CACHE_ROOT="$(shared_bootstrap_cache_root)"
ensure_bootstrap_cache_root "$CACHE_ROOT"
BOOTSTRAP_UV="$(install_controlled_bootstrap_uv "$CACHE_ROOT")"
BOOTSTRAP_PYTHON="$(create_repo_local_bootstrap_venv "$BOOTSTRAP_UV")"
log "Using the controlled bootstrap asset path to provision the repo-local runtime."
launch_prepare "$BOOTSTRAP_PYTHON" "controlled-bootstrap-asset" ${JSON_FLAG:+$JSON_FLAG}

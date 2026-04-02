#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANUAL_RECOVERY_DOC="docs/setup/manual-workspace-recovery.md"
HOST_CONTEXT_HELPER="$ROOT/scripts/read-host-execution-context.py"
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

json_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '%s' "$value"
}

json_string_or_null() {
  local value="${1-}"
  if [[ -z "$value" ]]; then
    printf 'null'
  else
    printf '"%s"' "$(json_escape "$value")"
  fi
}

json_bool() {
  if [[ "${1-}" == "true" ]]; then
    printf 'true'
  else
    printf 'false'
  fi
}

json_array_from_args() {
  local first=1
  local item=""
  printf '['
  for item in "$@"; do
    if (( first == 0 )); then
      printf ', '
    fi
    first=0
    printf '"%s"' "$(json_escape "$item")"
  done
  printf ']'
}

json_array_literal_or_empty() {
  local value="${1-}"
  if [[ -n "$value" && "$value" == \[*\] ]]; then
    printf '%s' "$value"
  else
    printf '[]'
  fi
}

fail_last_resort() {
  log "$*"
  log "Last-resort manual fallback: see $MANUAL_RECOVERY_DOC"
  exit 1
}

emit_simple_action_required() {
  local detail="$1"
  local next_step="${2-}"
  if [[ -n "$JSON_FLAG" ]]; then
    printf '{\n'
    printf '  "status": "action-required",\n'
    printf '  "detail": "%s",\n' "$(json_escape "$detail")"
    printf '  "next_steps": '
    if [[ -n "$next_step" ]]; then
      json_array_from_args "$next_step"
    else
      printf '[]'
    fi
    printf '\n}\n'
  else
    log "$detail"
    if [[ -n "$next_step" ]]; then
      log "Next step: $next_step"
    fi
  fi
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

repo_local_bootstrap_cache_root() {
  printf '%s\n' "$ROOT/.docmason/toolchain/bootstrap/cache"
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
  repo_local_bootstrap_cache_root
}

ensure_bootstrap_cache_root() {
  local cache_root="$1"
  mkdir -p "$cache_root"
}

validate_bootstrap_uv_installer_url() {
  local url="$1"
  if [[ "$url" =~ ^https://astral\.sh/uv(/[^/?#]+)?/install\.sh$ ]]; then
    return 0
  fi
  fail_last_resort \
    "The controlled UV bootstrap asset URL must remain an official Astral HTTPS installer URL."
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
  if [[ -x "$unmanaged_dir/bin/uv" ]]; then
    printf '%s\n' "$unmanaged_dir/bin/uv"
    return 0
  fi

  command -v curl >/dev/null 2>&1 \
    || fail_last_resort "The controlled bootstrap asset requires `curl`."
  command -v sh >/dev/null 2>&1 \
    || fail_last_resort "The controlled bootstrap asset requires `/bin/sh`."
  validate_bootstrap_uv_installer_url "$BOOTSTRAP_UV_INSTALLER_URL"

  log "Downloading the controlled UV bootstrap asset..."
  curl -LsSf "$BOOTSTRAP_UV_INSTALLER_URL" -o "$installer_path" \
    || fail_last_resort "Could not download the controlled UV bootstrap asset."

  log "Installing the controlled UV bootstrap asset..."
  mkdir -p "$unmanaged_dir"
  UV_UNMANAGED_INSTALL="$unmanaged_dir" UV_NO_MODIFY_PATH=1 sh "$installer_path" \
    || fail_last_resort "Could not install the controlled UV bootstrap asset."

  if [[ ! -x "$uv_bin" && -x "$unmanaged_dir/bin/uv" ]]; then
    uv_bin="$unmanaged_dir/bin/uv"
  fi
  [[ -x "$uv_bin" ]] \
    || fail_last_resort \
      "The controlled UV bootstrap asset did not produce a runnable `uv` binary."
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
    || fail_last_resort \
      "Could not create the repo-local bootstrap helper venv from the controlled bootstrap asset."

  [[ -x "$bootstrap_python" ]] \
    || fail_last_resort \
      "The repo-local bootstrap helper venv was created without a runnable Python."
  printf '%s\n' "$bootstrap_python"
}

load_host_execution_context() {
  local helper_python=""
  HOST_PROVIDER="${DOCMASON_AGENT_SURFACE:-unknown-agent}"
  SANDBOX_POLICY="${DOCMASON_SANDBOX_POLICY:-${DOCMASON_CODEX_SANDBOX_POLICY:-}}"
  APPROVAL_MODE="${DOCMASON_APPROVAL_MODE:-${DOCMASON_CODEX_APPROVAL_MODE:-}}"
  PERMISSION_MODE="${DOCMASON_PERMISSION_MODE:-}"
  FULL_MACHINE_ACCESS="false"
  WORKSPACE_WRITE_NETWORK_ACCESS="${DOCMASON_WORKSPACE_WRITE_NETWORK_ACCESS:-${DOCMASON_CODEX_NETWORK_ACCESS:-}}"
  SANDBOX_WRITABLE_ROOTS="${DOCMASON_SANDBOX_WRITABLE_ROOTS:-${DOCMASON_CODEX_WRITABLE_ROOTS:-[]}}"
  CONTEXT_SOURCE="unknown"

  if [[ -f "$HOST_CONTEXT_HELPER" ]]; then
    if [[ -n "${DOCMASON_HOST_CONTEXT_PYTHON:-}" ]]; then
      helper_python="${DOCMASON_HOST_CONTEXT_PYTHON}"
    elif command -v python3 >/dev/null 2>&1; then
      helper_python="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
      helper_python="$(command -v python)"
    fi
    if [[ -n "$helper_python" ]]; then
      while IFS= read -r shell_line; do
        eval "export $shell_line"
      done < <("$helper_python" "$HOST_CONTEXT_HELPER" --format shell 2>/dev/null || true)
      HOST_PROVIDER="${DOCMASON_HOST_HOST_PROVIDER:-$HOST_PROVIDER}"
      SANDBOX_POLICY="${DOCMASON_HOST_SANDBOX_POLICY:-$SANDBOX_POLICY}"
      APPROVAL_MODE="${DOCMASON_HOST_APPROVAL_MODE:-$APPROVAL_MODE}"
      PERMISSION_MODE="${DOCMASON_HOST_PERMISSION_MODE:-$PERMISSION_MODE}"
      FULL_MACHINE_ACCESS="${DOCMASON_HOST_FULL_MACHINE_ACCESS:-$FULL_MACHINE_ACCESS}"
      WORKSPACE_WRITE_NETWORK_ACCESS="${DOCMASON_HOST_WORKSPACE_WRITE_NETWORK_ACCESS:-$WORKSPACE_WRITE_NETWORK_ACCESS}"
      SANDBOX_WRITABLE_ROOTS="${DOCMASON_HOST_SANDBOX_WRITABLE_ROOTS:-$SANDBOX_WRITABLE_ROOTS}"
      CONTEXT_SOURCE="${DOCMASON_HOST_CONTEXT_SOURCE:-$CONTEXT_SOURCE}"
    fi
  fi

  # Fallback agent detection when the Python helper was unavailable.
  if [[ "$HOST_PROVIDER" == "unknown-agent" && -n "${CODEX_THREAD_ID:-}" ]]; then
    HOST_PROVIDER="codex"
    CONTEXT_SOURCE="env-codex-thread-id-fallback"
  fi
}

workspace_runtime_ready() {
  [[ -x "$ROOT/.venv/bin/python" && -f "$ROOT/runtime/bootstrap_state.json" ]]
}

probe_machine_baseline() {
  local platform_name=""
  local soffice_binary=""
  local missing=()

  MACHINE_BASELINE_APPLICABLE=0
  MACHINE_BASELINE_READY=1
  MACHINE_BASELINE_STATUS="not-applicable"
  MACHINE_BASELINE_DETAIL="Native macOS machine-baseline policy is not active for this host surface."
  BREW_BINARY=""
  LIBREOFFICE_BINARY=""
  MACHINE_BASELINE_MISSING_COMPONENTS=()

  platform_name="$(uname -s)"
  if [[ "$platform_name" != "Darwin" || "$HOST_PROVIDER" != "codex" ]]; then
    return 0
  fi

  MACHINE_BASELINE_APPLICABLE=1
  if command -v brew >/dev/null 2>&1; then
    BREW_BINARY="$(command -v brew)"
  fi
  if command -v soffice >/dev/null 2>&1; then
    soffice_binary="$(command -v soffice)"
  elif [[ -x "/Applications/LibreOffice.app/Contents/MacOS/soffice" ]]; then
    soffice_binary="/Applications/LibreOffice.app/Contents/MacOS/soffice"
  fi
  LIBREOFFICE_BINARY="$soffice_binary"

  if [[ -z "$BREW_BINARY" ]]; then
    missing+=("Homebrew")
  fi
  if [[ -z "$LIBREOFFICE_BINARY" ]]; then
    missing+=("LibreOffice")
  fi

  if (( ${#missing[@]} == 0 )); then
    MACHINE_BASELINE_READY=1
    MACHINE_BASELINE_STATUS="ready"
    MACHINE_BASELINE_DETAIL="Native Codex machine baseline is ready."
    return 0
  fi

  MACHINE_BASELINE_READY=0
  MACHINE_BASELINE_MISSING_COMPONENTS=("${missing[@]}")
  if [[ "$FULL_MACHINE_ACCESS" == "true" ]]; then
    MACHINE_BASELINE_STATUS="install-required"
    MACHINE_BASELINE_DETAIL="Native Codex machine baseline is missing ${missing[*]}."
  else
    MACHINE_BASELINE_STATUS="host-access-upgrade-required"
    if [[ "$PERMISSION_MODE" == "default-permissions" ]]; then
      MACHINE_BASELINE_DETAIL="Native Codex machine baseline is missing ${missing[*]}, and the current thread is still in \`Default permissions\`."
    else
      MACHINE_BASELINE_DETAIL="Native Codex machine baseline is missing ${missing[*]}, and the current turn does not expose \`Full access\` yet."
    fi
  fi
}

emit_host_access_upgrade() {
  local detail="$1"
  local next_step="$2"
  shift 2
  local reasons=("$@")
  local guidance=""

  if [[ "$HOST_PROVIDER" == "codex" ]]; then
    guidance="DocMason is currently running in Codex \`Default permissions\`. This bootstrap step needs capabilities that the current thread does not expose there, such as repo-local runtime downloads or machine-level setup. Clicking \`Yes\` on a single command prompt only approves that command; it does not switch the thread out of \`Default permissions\`. Switch this thread to \`Full access\`, then continue the same task."
  else
    guidance="DocMason needs broader host permissions or network access before this bootstrap step can continue. Enable the higher-access host mode, then continue the same task."
  fi

  if [[ -n "$JSON_FLAG" ]]; then
    printf '{\n'
    printf '  "status": "action-required",\n'
    printf '  "detail": "%s",\n' "$(json_escape "$detail")"
    printf '  "workspace_runtime_ready": '
    if workspace_runtime_ready; then
      printf 'true,\n'
    else
      printf 'false,\n'
    fi
    printf '  "machine_baseline_ready": '
    if [[ "$MACHINE_BASELINE_READY" == "1" ]]; then
      printf 'true,\n'
    else
      printf 'false,\n'
    fi
    printf '  "machine_baseline_status": "%s",\n' "$(json_escape "$MACHINE_BASELINE_STATUS")"
    printf '  "bootstrap_source": '
    json_string_or_null "$BOOTSTRAP_SOURCE"
    printf ',\n'
    printf '  "host_access_required": true,\n'
    printf '  "host_access_guidance": "%s",\n' "$(json_escape "$guidance")"
    printf '  "host_access_reasons": '
    json_array_from_args "${reasons[@]}"
    printf ',\n'
    printf '  "host_execution": {\n'
    printf '    "host_provider": %s,\n' "$(json_string_or_null "$HOST_PROVIDER")"
    printf '    "sandbox_policy": %s,\n' "$(json_string_or_null "$SANDBOX_POLICY")"
    printf '    "approval_mode": %s,\n' "$(json_string_or_null "$APPROVAL_MODE")"
    printf '    "permission_mode": %s,\n' "$(json_string_or_null "$PERMISSION_MODE")"
    printf '    "full_machine_access": %s,\n' "$(json_bool "$FULL_MACHINE_ACCESS")"
    printf '    "workspace_write_network_access": '
    if [[ "$WORKSPACE_WRITE_NETWORK_ACCESS" == "true" ]]; then
      printf 'true,\n'
    elif [[ "$WORKSPACE_WRITE_NETWORK_ACCESS" == "false" ]]; then
      printf 'false,\n'
    else
      printf 'null,\n'
    fi
    printf '    "sandbox_writable_roots": %s,\n' "$(json_array_literal_or_empty "$SANDBOX_WRITABLE_ROOTS")"
    printf '    "context_source": %s\n' "$(json_string_or_null "$CONTEXT_SOURCE")"
    printf '  },\n'
    printf '  "workspace_write_network_access": '
    if [[ "$WORKSPACE_WRITE_NETWORK_ACCESS" == "true" ]]; then
      printf 'true,\n'
    elif [[ "$WORKSPACE_WRITE_NETWORK_ACCESS" == "false" ]]; then
      printf 'false,\n'
    else
      printf 'null,\n'
    fi
    printf '  "sandbox_writable_roots": %s,\n' "$(json_array_literal_or_empty "$SANDBOX_WRITABLE_ROOTS")"
    printf '  "control_plane": {\n'
    printf '    "state": "awaiting-confirmation",\n'
    printf '    "confirmation_kind": "host-access-upgrade",\n'
    printf '    "confirmation_prompt": "%s",\n' "$(json_escape "$guidance")"
    printf '    "confirmation_reason": "%s",\n' "$(json_escape "$detail")"
    printf '    "next_command": "%s"\n' "$(json_escape "$next_step")"
    printf '  },\n'
    printf '  "next_steps": '
    json_array_from_args "$next_step"
    printf '\n}\n'
  else
    log "$detail"
    local reason=""
    for reason in "${reasons[@]}"; do
      log "- $reason"
    done
    log "$guidance"
    log "Next step: $next_step"
  fi
  exit 1
}

load_host_execution_context
WORKSPACE_RUNTIME_READY=0
if workspace_runtime_ready; then
  WORKSPACE_RUNTIME_READY=1
fi

REPO_LOCAL_BOOTSTRAP_PYTHON="$(find_repo_local_bootstrap_python || true)"
BOOTSTRAP_SOURCE="controlled-bootstrap-asset"
if [[ -n "$REPO_LOCAL_BOOTSTRAP_PYTHON" ]]; then
  if [[ "$REPO_LOCAL_BOOTSTRAP_PYTHON" == "$ROOT/.docmason/toolchain/python/current/bin/python3.13" ]]; then
    BOOTSTRAP_SOURCE="repo-local-managed"
  else
    BOOTSTRAP_SOURCE="repo-local-bootstrap-venv"
  fi
fi

probe_machine_baseline

HOST_ACCESS_REASONS=()
if [[ "$MACHINE_BASELINE_APPLICABLE" == "1" && "$MACHINE_BASELINE_READY" != "1" && "$FULL_MACHINE_ACCESS" != "true" ]]; then
  HOST_ACCESS_REASONS+=(
    "Native Codex machine baseline is missing ${MACHINE_BASELINE_MISSING_COMPONENTS[*]} and needs machine-level installation."
  )
fi
if [[ "$WORKSPACE_RUNTIME_READY" != "1" ]]; then
  if [[ "$WORKSPACE_WRITE_NETWORK_ACCESS" == "false" ]]; then
    HOST_ACCESS_REASONS+=(
      "Repo-local runtime bootstrap needs network downloads, but the current host execution context reports network access is disabled."
    )
  elif [[ "$HOST_PROVIDER" == "codex" && "$FULL_MACHINE_ACCESS" != "true" && -z "$WORKSPACE_WRITE_NETWORK_ACCESS" ]]; then
    HOST_ACCESS_REASONS+=(
      "DocMason cannot safely confirm that this Codex turn allows the network downloads required for repo-local runtime bootstrap."
    )
  fi
fi

if (( ${#HOST_ACCESS_REASONS[@]} > 0 )); then
  if [[ "$HOST_PROVIDER" == "codex" ]]; then
    emit_host_access_upgrade \
      "${HOST_ACCESS_REASONS[0]}" \
      "Switch Codex to \`Full access\`, then continue the same task." \
      "${HOST_ACCESS_REASONS[@]}"
  fi
  emit_host_access_upgrade \
    "${HOST_ACCESS_REASONS[0]}" \
    "Enable the higher-access host mode, then continue the same task." \
    "${HOST_ACCESS_REASONS[@]}"
fi

if [[ -n "${DOCMASON_BOOTSTRAP_PYTHON:-}" ]] && python_is_supported "${DOCMASON_BOOTSTRAP_PYTHON}"; then
  log "Using the explicit manual bootstrap Python override."
  launch_prepare "${DOCMASON_BOOTSTRAP_PYTHON}" "manual-bootstrap-python" ${JSON_FLAG:+$JSON_FLAG}
fi

if [[ -n "$REPO_LOCAL_BOOTSTRAP_PYTHON" ]]; then
  if [[ "$BOOTSTRAP_SOURCE" == "repo-local-managed" ]]; then
    log "Using the repo-local managed Python repair path."
  else
    log "Using the repo-local bootstrap helper venv."
  fi
  launch_prepare "$REPO_LOCAL_BOOTSTRAP_PYTHON" "$BOOTSTRAP_SOURCE" ${JSON_FLAG:+$JSON_FLAG}
fi

if [[ "$ASSUME_YES" -ne 1 ]]; then
  emit_simple_action_required \
    "No supported repo-local bootstrap runtime is available yet." \
    "Rerun ./scripts/bootstrap-workspace.sh --yes to allow the governed bootstrap path."
fi

BOOTSTRAP_CACHE_ROOT="$(shared_bootstrap_cache_root)"
if [[ "$HOST_PROVIDER" == "codex" && "$FULL_MACHINE_ACCESS" != "true" ]]; then
  BOOTSTRAP_CACHE_ROOT="$(repo_local_bootstrap_cache_root)"
fi
ensure_bootstrap_cache_root "$BOOTSTRAP_CACHE_ROOT"
BOOTSTRAP_UV="$(install_controlled_bootstrap_uv "$BOOTSTRAP_CACHE_ROOT")"
BOOTSTRAP_PYTHON="$(create_repo_local_bootstrap_venv "$BOOTSTRAP_UV")"
log "Using the controlled bootstrap asset path to provision the repo-local runtime."
launch_prepare "$BOOTSTRAP_PYTHON" "controlled-bootstrap-asset" ${JSON_FLAG:+$JSON_FLAG}

"""Bounded release-entry update-check and DAU helpers for bundle workspaces."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from .project import WorkspacePaths, read_json, write_json

RELEASE_CLIENT_SCHEMA_VERSION = 1
RELEASE_ENTRY_REQUEST_SCHEMA_VERSION = 1
RELEASE_ENTRY_RESPONSE_SCHEMA_VERSION = 1
RELEASE_ENTRY_AUTO_TRIGGER = "ask-auto"
RELEASE_ENTRY_MANUAL_UPDATE_TRIGGER = "update-core"
RELEASE_ENTRY_DISABLED_SOURCE_REPO = "source-repo"
RELEASE_ENTRY_DISABLED_DNT = "dnt"
RELEASE_ENTRY_DISABLED_LOCAL_CONFIG = "local-config"
RELEASE_ENTRY_DISABLED_BUNDLE_UNCONFIGURED = "bundle-unconfigured"
RELEASE_ENTRY_SUPPORTED_CHANNELS = frozenset({"clean", "demo-ico-gcs"})
DEFAULT_RELEASE_ENTRY_SCOPE = "canonical-ask"
DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS = 20
DEFAULT_RELEASE_ENTRY_TIMEOUT_SECONDS = 2.0
RELEASE_ENTRY_USER_AGENT = "DocMasonReleaseEntry/1.0 (+https://github.com/JetXu-LLM/DocMason)"


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _current_time(*, now: datetime | None = None) -> datetime:
    return (now or datetime.now(tz=UTC)).astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _safe_int(value: Any, *, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_read_json(path: Any) -> tuple[dict[str, Any], str | None]:
    try:
        return read_json(path), None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, str(exc)


def do_not_track_enabled() -> bool:
    """Return whether the Console-style DNT override is active."""
    return os.environ.get("DO_NOT_TRACK") == "1"


def default_release_client_state(*, automatic_check_enabled: bool) -> dict[str, Any]:
    """Return the persisted local release-entry state template."""
    return {
        "schema_version": RELEASE_CLIENT_SCHEMA_VERSION,
        "automatic_check_enabled": automatic_check_enabled,
        "installation_hash": None,
        "created_at": None,
        "last_check_attempted_at": None,
        "next_eligible_at": None,
        "last_known_latest_version": None,
        "last_notified_version": None,
        "last_check_status": None,
    }


def _normalized_release_client_state(
    raw_state: dict[str, Any],
    *,
    automatic_check_enabled_default: bool,
) -> dict[str, Any]:
    state = default_release_client_state(
        automatic_check_enabled=automatic_check_enabled_default,
    )
    if not raw_state:
        return state
    state["schema_version"] = _safe_int(
        raw_state.get("schema_version"),
        fallback=RELEASE_CLIENT_SCHEMA_VERSION,
    )
    if isinstance(raw_state.get("automatic_check_enabled"), bool):
        state["automatic_check_enabled"] = raw_state["automatic_check_enabled"]
    for field_name in (
        "installation_hash",
        "created_at",
        "last_check_attempted_at",
        "next_eligible_at",
        "last_known_latest_version",
        "last_notified_version",
        "last_check_status",
    ):
        state[field_name] = _nonempty_string(raw_state.get(field_name))
    return state


def release_distribution_manifest(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the bundle distribution manifest when present."""
    manifest, _error = _safe_read_json(paths.distribution_manifest_path)
    return manifest


def release_entry_manifest(paths: WorkspacePaths) -> dict[str, Any]:
    """Load the nested release-entry manifest block."""
    manifest = release_distribution_manifest(paths)
    block = manifest.get("release_entry")
    if isinstance(block, dict):
        return dict(block)
    return {}


def _release_entry_bundle_config(paths: WorkspacePaths) -> dict[str, Any]:
    manifest = release_distribution_manifest(paths)
    release_entry = release_entry_manifest(paths)
    cooldown_hours = _safe_int(
        release_entry.get("automatic_check_cooldown_hours"),
        fallback=DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS,
    )
    automatic_check_scope = (
        _nonempty_string(release_entry.get("automatic_check_scope"))
        or DEFAULT_RELEASE_ENTRY_SCOPE
    )
    enabled_by_default = (
        bool(release_entry.get("automatic_check_enabled_by_default"))
        if isinstance(release_entry.get("automatic_check_enabled_by_default"), bool)
        else False
    )
    distribution_channel = _nonempty_string(manifest.get("distribution_channel"))
    current_version = _nonempty_string(manifest.get("source_version"))
    update_service_url = _nonempty_string(release_entry.get("update_service_url"))
    asset_name = _nonempty_string(release_entry.get("asset_name")) or _nonempty_string(
        manifest.get("asset_name")
    )
    bundle_detected = bool(manifest)
    bundle_configured = bool(
        bundle_detected
        and distribution_channel in RELEASE_ENTRY_SUPPORTED_CHANNELS
        and current_version
        and update_service_url
        and automatic_check_scope == DEFAULT_RELEASE_ENTRY_SCOPE
    )
    return {
        "bundle_detected": bundle_detected,
        "bundle_configured": bundle_configured,
        "distribution_channel": distribution_channel,
        "current_version": current_version,
        "update_service_url": update_service_url,
        "asset_name": asset_name,
        "automatic_check_scope": automatic_check_scope,
        "automatic_check_enabled_by_default": enabled_by_default,
        "automatic_check_cooldown_hours": cooldown_hours,
    }


def release_entry_snapshot(
    paths: WorkspacePaths,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Summarize release-entry state for explicit status and doctor surfaces."""
    bundle = _release_entry_bundle_config(paths)
    raw_state, state_error = _safe_read_json(paths.release_client_state_path)
    state = _normalized_release_client_state(
        raw_state,
        automatic_check_enabled_default=bool(bundle["automatic_check_enabled_by_default"]),
    )
    disabled_reason = None
    effective_enabled = False
    if not bundle["bundle_detected"]:
        disabled_reason = RELEASE_ENTRY_DISABLED_SOURCE_REPO
    elif not bundle["bundle_configured"]:
        disabled_reason = RELEASE_ENTRY_DISABLED_BUNDLE_UNCONFIGURED
    elif do_not_track_enabled():
        disabled_reason = RELEASE_ENTRY_DISABLED_DNT
    elif not bool(state["automatic_check_enabled"]):
        disabled_reason = RELEASE_ENTRY_DISABLED_LOCAL_CONFIG
    else:
        effective_enabled = True

    check_now = _current_time(now=now)
    next_eligible = _parse_timestamp(state.get("next_eligible_at"))
    eligible_now = bool(
        effective_enabled and (next_eligible is None or check_now >= next_eligible)
    )
    current_version = _nonempty_string(bundle.get("current_version"))
    last_known_latest_version = _nonempty_string(state.get("last_known_latest_version"))
    update_available = bool(
        current_version
        and last_known_latest_version
        and current_version != last_known_latest_version
    )
    return {
        "schema_version": RELEASE_CLIENT_SCHEMA_VERSION,
        "bundle_detected": bool(bundle["bundle_detected"]),
        "bundle_configured": bool(bundle["bundle_configured"]),
        "distribution_channel": bundle["distribution_channel"],
        "current_version": current_version,
        "asset_name": bundle["asset_name"],
        "automatic_check_scope": bundle["automatic_check_scope"],
        "automatic_check_cooldown_hours": bundle["automatic_check_cooldown_hours"],
        "automatic_check_enabled_by_default": bool(
            bundle["automatic_check_enabled_by_default"]
        ),
        "effective_enabled": effective_enabled,
        "disabled_reason": disabled_reason,
        "dnt_active": do_not_track_enabled(),
        "state_path": str(paths.release_client_state_path.relative_to(paths.root)),
        "state_present": bool(raw_state),
        "state_error": state_error,
        "automatic_check_enabled": bool(state["automatic_check_enabled"]),
        "installation_hash_present": bool(_nonempty_string(state.get("installation_hash"))),
        "last_check_attempted_at": _nonempty_string(state.get("last_check_attempted_at")),
        "next_eligible_at": _nonempty_string(state.get("next_eligible_at")),
        "eligible_now": eligible_now,
        "last_known_latest_version": last_known_latest_version,
        "last_notified_version": _nonempty_string(state.get("last_notified_version")),
        "last_check_status": _nonempty_string(state.get("last_check_status")),
        "update_available": update_available,
        "update_service_url_configured": bool(_nonempty_string(bundle["update_service_url"])),
    }


def _generated_installation_hash() -> str:
    return hashlib.sha256(secrets.token_bytes(32)).hexdigest()


def _persist_release_client_state(paths: WorkspacePaths, state: dict[str, Any]) -> None:
    write_json(paths.release_client_state_path, state)


def _prepared_release_client_state(
    paths: WorkspacePaths,
    *,
    automatic_check_enabled_default: bool,
    now: datetime,
) -> dict[str, Any]:
    raw_state: dict[str, Any] = {}
    if paths.release_client_state_path.exists():
        raw_state, _error = _safe_read_json(paths.release_client_state_path)
    state = _normalized_release_client_state(
        raw_state,
        automatic_check_enabled_default=automatic_check_enabled_default,
    )
    if _nonempty_string(state.get("installation_hash")) is None:
        state["installation_hash"] = _generated_installation_hash()
        state["created_at"] = _isoformat(now)
    elif _nonempty_string(state.get("created_at")) is None:
        state["created_at"] = _isoformat(now)
    return state


def release_entry_bundle_config(paths: WorkspacePaths) -> dict[str, Any]:
    """Return the effective release-entry bundle configuration for this workspace."""
    return _release_entry_bundle_config(paths)


def prepare_release_client_state(
    paths: WorkspacePaths,
    *,
    automatic_check_enabled_default: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load or initialize the persisted local release-entry state."""
    return _prepared_release_client_state(
        paths,
        automatic_check_enabled_default=automatic_check_enabled_default,
        now=_current_time(now=now),
    )


def persist_release_client_state(paths: WorkspacePaths, state: dict[str, Any]) -> None:
    """Persist one normalized release-entry client state payload."""
    _persist_release_client_state(paths, state)


def _release_entry_request_payload(
    *,
    distribution_channel: str,
    current_version: str,
    installation_hash: str,
    trigger: str,
) -> bytes:
    payload = {
        "schema_version": RELEASE_ENTRY_REQUEST_SCHEMA_VERSION,
        "distribution_channel": distribution_channel,
        "source_version": current_version,
        "installation_hash": installation_hash,
        "trigger": trigger,
    }
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def _parsed_release_entry_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    current_release = payload.get("current_release")
    if isinstance(current_release, dict):
        block = current_release
    else:
        block = payload
    latest_version = _nonempty_string(block.get("latest_version"))
    distribution_channel = _nonempty_string(block.get("distribution_channel"))
    if latest_version is None or distribution_channel is None:
        return {}
    return {
        "schema_version": _safe_int(
            payload.get("schema_version"),
            fallback=RELEASE_ENTRY_RESPONSE_SCHEMA_VERSION,
        ),
        "distribution_channel": distribution_channel,
        "latest_version": latest_version,
        "published_at": _nonempty_string(block.get("published_at")),
        "release_url": _nonempty_string(block.get("release_url")),
        "asset_url": _nonempty_string(block.get("asset_url")),
        "asset_name": _nonempty_string(block.get("asset_name")),
    }


def request_release_entry_service(
    service_url: str,
    *,
    distribution_channel: str,
    current_version: str,
    installation_hash: str,
    trigger: str,
    timeout_seconds: float = DEFAULT_RELEASE_ENTRY_TIMEOUT_SECONDS,
    urlopen: Any | None = None,
) -> dict[str, Any]:
    """Call the bounded release-entry service and return parsed release metadata."""
    parsed_url = urllib.parse.urlsplit(service_url)
    if parsed_url.scheme != "https" or not parsed_url.netloc:
        raise ValueError("Release-entry service URL must use HTTPS.")
    request = urllib.request.Request(
        service_url,
        data=_release_entry_request_payload(
            distribution_channel=distribution_channel,
            current_version=current_version,
            installation_hash=installation_hash,
            trigger=trigger,
        ),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": RELEASE_ENTRY_USER_AGENT,
        },
        method="POST",
    )
    effective_urlopen = urlopen or urllib.request.urlopen
    with effective_urlopen(request, timeout=timeout_seconds) as response:
        response_body = response.read().decode("utf-8")
    parsed = _parsed_release_entry_response(json.loads(response_body))
    if not parsed:
        raise ValueError("Release-entry service returned an invalid response.")
    return parsed


def _release_entry_notice(
    *,
    current_version: str,
    latest_version: str,
    release_url: str | None,
) -> str:
    return (
        "DocMason update available: "
        f"{latest_version} (current bundle: {current_version}). "
        "Run `docmason update-core` to apply the latest clean core."
        + (
            f" Manual download remains available from {release_url}."
            if release_url
            else ""
        )
    )


def maybe_run_release_entry_check(
    paths: WorkspacePaths,
    *,
    trigger: str = RELEASE_ENTRY_AUTO_TRIGGER,
    now: datetime | None = None,
    timeout_seconds: float = DEFAULT_RELEASE_ENTRY_TIMEOUT_SECONDS,
    urlopen: Any | None = None,
) -> dict[str, Any]:
    """Run the bounded release-entry network check when the current bundle is eligible."""
    initial_snapshot = release_entry_snapshot(paths, now=now)
    result: dict[str, Any] = {
        "notice": None,
        "release_entry_status": initial_snapshot,
        "attempted": False,
    }
    if (
        trigger != RELEASE_ENTRY_AUTO_TRIGGER
        or not initial_snapshot["effective_enabled"]
        or not initial_snapshot["eligible_now"]
    ):
        return result

    bundle = _release_entry_bundle_config(paths)
    distribution_channel = _nonempty_string(bundle.get("distribution_channel"))
    current_version = _nonempty_string(bundle.get("current_version"))
    service_url = _nonempty_string(bundle.get("update_service_url"))
    if distribution_channel is None or current_version is None or service_url is None:
        return result

    current_time = _current_time(now=now)
    state = _prepared_release_client_state(
        paths,
        automatic_check_enabled_default=bool(bundle["automatic_check_enabled_by_default"]),
        now=current_time,
    )
    state["last_check_attempted_at"] = _isoformat(current_time)
    state["next_eligible_at"] = _isoformat(
        current_time
        + timedelta(
            hours=_safe_int(
                bundle.get("automatic_check_cooldown_hours"),
                fallback=DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS,
            )
        )
    )
    state["last_check_status"] = "attempted"
    _persist_release_client_state(paths, state)
    result["attempted"] = True

    try:
        parsed = request_release_entry_service(
            service_url,
            distribution_channel=distribution_channel,
            current_version=current_version,
            installation_hash=str(state["installation_hash"]),
            trigger=trigger,
            timeout_seconds=timeout_seconds,
            urlopen=urlopen,
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ):
        state["last_check_status"] = "network-error"
        _persist_release_client_state(paths, state)
        result["release_entry_status"] = release_entry_snapshot(paths, now=current_time)
        return result

    if not parsed or parsed["distribution_channel"] != distribution_channel:
        state["last_check_status"] = "invalid-response"
        _persist_release_client_state(paths, state)
        result["release_entry_status"] = release_entry_snapshot(paths, now=current_time)
        return result

    latest_version = str(parsed["latest_version"])
    state["last_known_latest_version"] = latest_version
    update_available = latest_version != current_version
    state["last_check_status"] = "ok-update-available" if update_available else "ok-no-update"
    if update_available and state.get("last_notified_version") != latest_version:
        state["last_notified_version"] = latest_version
        result["notice"] = _release_entry_notice(
            current_version=current_version,
            latest_version=latest_version,
            release_url=_nonempty_string(parsed.get("release_url")),
        )
    _persist_release_client_state(paths, state)
    result["release_entry_status"] = release_entry_snapshot(paths, now=current_time)
    return result

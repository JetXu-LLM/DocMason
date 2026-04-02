"""Explicit in-place core-update support for generated DocMason release bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from .project import WorkspacePaths
from .release_entry import (
    DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS,
    DEFAULT_RELEASE_ENTRY_TIMEOUT_SECONDS,
    RELEASE_ENTRY_MANUAL_UPDATE_TRIGGER,
    persist_release_client_state,
    prepare_release_client_state,
    release_distribution_manifest,
    release_entry_bundle_config,
    request_release_entry_service,
)

PROTECTED_TOP_LEVEL = {
    ".agents",
    ".docmason",
    ".git",
    ".venv",
    "adapters",
    "knowledge_base",
    "original_doc",
    "runtime",
    "venv",
}
CLEAN_ASSET_NAME = "DocMason-clean.zip"
DOWNLOAD_TIMEOUT_SECONDS = max(DEFAULT_RELEASE_ENTRY_TIMEOUT_SECONDS, 20.0)
CHECK_TIMEOUT_SECONDS = max(DEFAULT_RELEASE_ENTRY_TIMEOUT_SECONDS, 10.0)
UPDATE_CORE_STATUS_UPDATED = "updated"
UPDATE_CORE_STATUS_ALREADY_CURRENT = "already-current"
_SOURCE_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UpdateCoreError(RuntimeError):
    """Raised when explicit core update cannot proceed safely."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        next_steps: list[str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.next_steps = next_steps or []
        self.payload = payload or {}


def _current_time(*, now: datetime | None = None) -> datetime:
    return (now or datetime.now(tz=UTC)).astimezone(UTC)


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _relative_to_workspace(paths: WorkspacePaths, path: Path) -> str:
    try:
        return str(path.relative_to(paths.root))
    except ValueError:
        return str(path)


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(path)


def _download_to_path(
    url: str,
    destination: Path,
    *,
    urlopen: Any | None,
    timeout_seconds: float,
) -> None:
    effective_urlopen = urlopen or urllib.request.urlopen
    with (
        effective_urlopen(url, timeout=timeout_seconds) as response,
        destination.open("wb") as handle,
    ):
        shutil.copyfileobj(response, handle)


def _normalized_source_repo(value: Any) -> str | None:
    repo = _nonempty_string(value)
    if repo is None or not _SOURCE_REPO_PATTERN.fullmatch(repo):
        return None
    return repo


def _validated_trusted_github_release_url(
    url: str,
    *,
    source_repo: str,
    kind: str,
) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise UpdateCoreError(
            "invalid-release-entry-response",
            f"The release-entry service returned a non-HTTPS {kind} URL.",
        )
    if parsed.hostname != "github.com":
        raise UpdateCoreError(
            "invalid-release-entry-response",
            f"The release-entry service returned a non-GitHub {kind} URL.",
        )
    expected_prefix = (
        f"/{source_repo}/releases/tag/"
        if kind == "release"
        else f"/{source_repo}/releases/download/"
    )
    if not parsed.path.startswith(expected_prefix):
        raise UpdateCoreError(
            "invalid-release-entry-response",
            "The release-entry service returned an update URL outside the trusted "
            f"GitHub release boundary for `{source_repo}`.",
        )
    return url


def _download_text(
    url: str,
    *,
    urlopen: Any | None,
    timeout_seconds: float,
) -> str:
    effective_urlopen = urlopen or urllib.request.urlopen
    with effective_urlopen(url, timeout=timeout_seconds) as response:
        payload = response.read()
    if not isinstance(payload, bytes):
        raise UpdateCoreError(
            "invalid-download",
            "The downloaded text payload is not valid UTF-8 content.",
        )
    return payload.decode("utf-8")


def _sha256_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _expected_sha256(sha256_text: str, *, asset_name: str) -> str:
    for line in sha256_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        digest = parts[0].lower()
        candidate_name = parts[-1] if len(parts) > 1 else asset_name
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise UpdateCoreError(
                "invalid-checksum",
                "The published checksum file is malformed.",
            )
        if candidate_name != asset_name:
            continue
        return digest
    raise UpdateCoreError(
        "missing-checksum",
        f"The published checksum file does not describe `{asset_name}`.",
    )


def _load_bundle_context(paths: WorkspacePaths) -> dict[str, Any]:
    manifest = release_distribution_manifest(paths)
    bundle = release_entry_bundle_config(paths)
    if not manifest or not bundle["bundle_detected"]:
        raise UpdateCoreError(
            "unsupported-workspace",
            "`docmason update-core` only supports generated clean or demo release bundles.",
            next_steps=[
                "Use a generated `clean` or `demo-ico-gcs` bundle instead of the "
                "canonical source repository.",
            ],
        )
    if not bundle["bundle_configured"]:
        raise UpdateCoreError(
            "bundle-unconfigured",
            "This generated bundle does not have a configured release-entry update service.",
            next_steps=[
                "Download a current official DocMason release bundle before retrying "
                "`docmason update-core`.",
            ],
        )
    distribution_channel = _nonempty_string(bundle.get("distribution_channel"))
    current_version = _nonempty_string(bundle.get("current_version"))
    update_service_url = _nonempty_string(bundle.get("update_service_url"))
    source_repo = _normalized_source_repo(manifest.get("source_repo"))
    if (
        distribution_channel is None
        or current_version is None
        or update_service_url is None
        or source_repo is None
    ):
        raise UpdateCoreError(
            "invalid-bundle-manifest",
            "The generated bundle manifest is incomplete.",
        )
    return {
        "distribution_channel": distribution_channel,
        "current_version": current_version,
        "update_service_url": update_service_url,
        "source_repo": source_repo,
        "automatic_check_enabled_by_default": bool(
            bundle.get("automatic_check_enabled_by_default")
        ),
        "automatic_check_cooldown_hours": int(
            bundle.get("automatic_check_cooldown_hours")
            or DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS
        ),
    }


def _read_bundle_manifest_from_zip(bundle_path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            payload = json.loads(archive.read("distribution-manifest.json").decode("utf-8"))
    except FileNotFoundError as exc:
        raise UpdateCoreError(
            "missing-bundle",
            f"Bundle not found: {bundle_path}",
        ) from exc
    except KeyError as exc:
        raise UpdateCoreError(
            "invalid-bundle",
            "The supplied bundle is missing `distribution-manifest.json`.",
        ) from exc
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise UpdateCoreError(
            "invalid-bundle",
            f"The supplied bundle could not be read safely: {exc}",
        ) from exc
    if not isinstance(payload, dict):
        raise UpdateCoreError(
            "invalid-bundle",
            "The supplied bundle manifest is not a JSON object.",
        )
    return payload


def _clean_bundle_download_urls(
    *,
    current_channel: str,
    source_repo: str,
    release_metadata: dict[str, Any],
) -> tuple[str, str]:
    release_url = _nonempty_string(release_metadata.get("release_url"))
    asset_url = _nonempty_string(release_metadata.get("asset_url"))
    asset_name = _nonempty_string(release_metadata.get("asset_name"))
    if current_channel == "clean":
        if asset_url is None or asset_name != CLEAN_ASSET_NAME:
            raise UpdateCoreError(
                "invalid-release-entry-response",
                "The release-entry service did not return a clean bundle asset.",
            )
        _validated_trusted_github_release_url(
            asset_url,
            source_repo=source_repo,
            kind="asset",
        )
        if release_url is not None:
            _validated_trusted_github_release_url(
                release_url,
                source_repo=source_repo,
                kind="release",
            )
        return asset_url, asset_url + ".sha256"
    if release_url is None or "/releases/tag/" not in release_url:
        raise UpdateCoreError(
            "invalid-release-entry-response",
            "The release-entry service did not return a usable GitHub release URL.",
        )
    _validated_trusted_github_release_url(
        release_url,
        source_repo=source_repo,
        kind="release",
    )
    download_root = release_url.replace("/releases/tag/", "/releases/download/", 1)
    asset_url = f"{download_root}/{CLEAN_ASSET_NAME}"
    return asset_url, asset_url + ".sha256"


def _sync_release_client_state(
    paths: WorkspacePaths,
    *,
    bundle_context: dict[str, Any],
    state: dict[str, Any],
    latest_version: str,
    now: datetime,
    status: str,
) -> None:
    cooldown_hours = int(
        bundle_context.get("automatic_check_cooldown_hours") or DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS
    )
    state["last_check_attempted_at"] = _isoformat(now)
    state["next_eligible_at"] = _isoformat(now + timedelta(hours=cooldown_hours))
    state["last_known_latest_version"] = latest_version
    state["last_notified_version"] = latest_version
    state["last_check_status"] = status
    try:
        persist_release_client_state(paths, state)
    except OSError as exc:
        raise UpdateCoreError(
            "state-sync-failed",
            f"DocMason updated the local core but could not persist release-entry state: {exc}",
            next_steps=[
                "Check write access to `runtime/state/release-client.json` and rerun "
                "`docmason update-core` if you want the local update state refreshed.",
            ],
            payload={
                "core_updated": True,
                "state_path": _relative_to_workspace(paths, paths.release_client_state_path),
            },
        ) from exc


def _download_remote_update_bundle(
    *,
    target_bundle_path: Path,
    bundle_download_url: str,
    checksum_url: str,
    urlopen: Any | None,
) -> str:
    try:
        _download_to_path(
            bundle_download_url,
            target_bundle_path,
            urlopen=urlopen,
            timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS,
        )
        checksum_text = _download_text(
            checksum_url,
            urlopen=urlopen,
            timeout_seconds=DOWNLOAD_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError) as exc:
        raise UpdateCoreError(
            "download-failed",
            f"DocMason could not download the latest clean bundle safely: {exc}",
            next_steps=[
                "Retry `docmason update-core` after the release asset and checksum are "
                "reachable again.",
            ],
        ) from exc
    return checksum_text


def _extract_bundle(bundle_path: Path, destination: Path) -> Path:
    try:
        with zipfile.ZipFile(bundle_path) as archive:
            destination.mkdir(parents=True, exist_ok=True)
            destination_root = destination.resolve()
            for member in archive.infolist():
                mode = member.external_attr >> 16
                if (mode & 0o170000) == 0o120000:
                    raise UpdateCoreError(
                        "invalid-bundle",
                        "The bundle archive contains an unsupported symbolic link entry.",
                    )
                raw_name = member.filename.replace("\\", "/")
                normalized = PurePosixPath(raw_name)
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise UpdateCoreError(
                        "invalid-bundle",
                        f"The bundle archive contains an unsafe member path: {member.filename}",
                    )
                parts = [part for part in normalized.parts if part not in {"", "."}]
                if not parts:
                    continue
                target_path = destination.joinpath(*parts)
                resolved_target = target_path.resolve(strict=False)
                if os.path.commonpath([str(destination_root), str(resolved_target)]) != str(
                    destination_root
                ):
                    raise UpdateCoreError(
                        "invalid-bundle",
                        f"The bundle archive escapes the extraction root: {member.filename}",
                    )
                if member.is_dir():
                    target_path.mkdir(parents=True, exist_ok=True)
                    continue
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, "r") as source, target_path.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
                file_mode = mode & 0o777
                if file_mode:
                    target_path.chmod(file_mode)
    except (OSError, zipfile.BadZipFile) as exc:
        raise UpdateCoreError(
            "invalid-bundle",
            f"The bundle archive could not be extracted safely: {exc}",
        ) from exc
    return destination


def _validated_extracted_clean_bundle(
    bundle_root: Path,
    *,
    expected_version: str | None,
) -> dict[str, Any]:
    manifest_path = bundle_root / "distribution-manifest.json"
    if not manifest_path.exists():
        raise UpdateCoreError(
            "invalid-bundle",
            "The bundle archive is missing `distribution-manifest.json` after extraction.",
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise UpdateCoreError(
            "invalid-bundle",
            f"The extracted bundle manifest could not be parsed: {exc}",
        ) from exc
    if not isinstance(manifest, dict):
        raise UpdateCoreError(
            "invalid-bundle",
            "The extracted bundle manifest is not a JSON object.",
        )
    channel = _nonempty_string(manifest.get("distribution_channel"))
    version = _nonempty_string(manifest.get("source_version"))
    if channel != "clean":
        raise UpdateCoreError(
            "invalid-bundle",
            "Explicit core updates require a generated clean bundle payload.",
        )
    if version is None:
        raise UpdateCoreError(
            "invalid-bundle",
            "The extracted clean bundle does not declare `source_version`.",
        )
    if expected_version is not None and version != expected_version:
        raise UpdateCoreError(
            "bundle-version-mismatch",
            "The downloaded clean bundle version does not match the release-entry response.",
        )
    return manifest


def _replace_workspace_core(bundle_root: Path, workspace_root: Path) -> list[str]:
    rollback_root = bundle_root.parent / "rollback"
    rollback_root.mkdir(parents=True, exist_ok=True)
    moved_existing: list[str] = []
    applied_names: list[str] = []
    try:
        for child in sorted(workspace_root.iterdir(), key=lambda item: item.name):
            if child.name in PROTECTED_TOP_LEVEL:
                continue
            shutil.move(str(child), str(rollback_root / child.name))
            moved_existing.append(child.name)
        for child in sorted(bundle_root.iterdir(), key=lambda item: item.name):
            if child.name in PROTECTED_TOP_LEVEL:
                continue
            shutil.move(str(child), str(workspace_root / child.name))
            applied_names.append(child.name)
    except Exception as exc:
        for name in applied_names:
            _remove_path(workspace_root / name)
        for name in reversed(moved_existing):
            source = rollback_root / name
            if source.exists() or source.is_symlink():
                shutil.move(str(source), str(workspace_root / name))
        raise UpdateCoreError(
            "apply-failed",
            f"DocMason could not replace the local core safely: {exc}",
        ) from exc
    finally:
        shutil.rmtree(rollback_root, ignore_errors=True)
    return applied_names


def perform_update_core(
    paths: WorkspacePaths,
    *,
    bundle_path: Path | None = None,
    now: datetime | None = None,
    urlopen: Any | None = None,
) -> dict[str, Any]:
    """Apply the latest clean DocMason core onto a generated bundle workspace."""
    bundle_context = _load_bundle_context(paths)
    current_time = _current_time(now=now)
    state = prepare_release_client_state(
        paths,
        automatic_check_enabled_default=bundle_context["automatic_check_enabled_by_default"],
        now=current_time,
    )
    current_version = str(bundle_context["current_version"])

    target_bundle_path: Path | None = None
    bundle_source = "local-path" if bundle_path is not None else "release-entry-service"
    latest_version = current_version
    bundle_download_url = None
    checksum_url = None

    if bundle_path is None:
        try:
            release_metadata = request_release_entry_service(
                str(bundle_context["update_service_url"]),
                distribution_channel=str(bundle_context["distribution_channel"]),
                installation_hash=str(state["installation_hash"]),
                trigger=RELEASE_ENTRY_MANUAL_UPDATE_TRIGGER,
                timeout_seconds=CHECK_TIMEOUT_SECONDS,
                urlopen=urlopen,
            )
        except Exception as exc:  # pragma: no cover - exercised via command tests
            raise UpdateCoreError(
                "release-entry-failed",
                f"DocMason could not check the current release metadata: {exc}",
                next_steps=[
                    "Retry `docmason update-core` after the release-entry service is "
                    "reachable again.",
                ],
            ) from exc
        latest_version = str(release_metadata["latest_version"])
        if latest_version == current_version:
            _sync_release_client_state(
                paths,
                bundle_context=bundle_context,
                state=state,
                latest_version=latest_version,
                now=current_time,
                status="manual-ok-no-update",
            )
            return {
                "update_core_status": UPDATE_CORE_STATUS_ALREADY_CURRENT,
                "current_version": current_version,
                "latest_version": latest_version,
                "applied_version": current_version,
                "applied_bundle_channel": str(bundle_context["distribution_channel"]),
                "bundle_source": bundle_source,
                "downloaded_bundle_url": None,
                "downloaded_checksum_url": None,
                "release_entry_trigger": RELEASE_ENTRY_MANUAL_UPDATE_TRIGGER,
                "forced_network_action": True,
                "preserved_paths": sorted(PROTECTED_TOP_LEVEL),
                "state_path": _relative_to_workspace(paths, paths.release_client_state_path),
            }

        bundle_download_url, checksum_url = _clean_bundle_download_urls(
            current_channel=str(bundle_context["distribution_channel"]),
            source_repo=str(bundle_context["source_repo"]),
            release_metadata=release_metadata,
        )
        with tempfile.TemporaryDirectory(prefix="docmason-update-core-") as tempdir_name:
            tempdir = Path(tempdir_name)
            target_bundle_path = tempdir / CLEAN_ASSET_NAME
            checksum_text = _download_remote_update_bundle(
                target_bundle_path=target_bundle_path,
                bundle_download_url=str(bundle_download_url),
                checksum_url=str(checksum_url),
                urlopen=urlopen,
            )
            expected_digest = _expected_sha256(checksum_text, asset_name=CLEAN_ASSET_NAME)
            actual_digest = _sha256_digest(target_bundle_path)
            if actual_digest != expected_digest:
                raise UpdateCoreError(
                    "checksum-mismatch",
                    "The downloaded clean bundle failed checksum verification.",
                )
            extracted_root = _extract_bundle(target_bundle_path, tempdir / "bundle")
            manifest = _validated_extracted_clean_bundle(
                extracted_root,
                expected_version=latest_version,
            )
            applied_names = _replace_workspace_core(extracted_root, paths.root)
    else:
        target_bundle_path = bundle_path.resolve()
        manifest = _read_bundle_manifest_from_zip(target_bundle_path)
        latest_version = _nonempty_string(manifest.get("source_version")) or current_version
        with tempfile.TemporaryDirectory(prefix="docmason-update-core-") as tempdir_name:
            tempdir = Path(tempdir_name)
            extracted_root = _extract_bundle(target_bundle_path, tempdir / "bundle")
            manifest = _validated_extracted_clean_bundle(
                extracted_root,
                expected_version=None,
            )
            applied_names = _replace_workspace_core(extracted_root, paths.root)

    applied_version = _nonempty_string(manifest.get("source_version")) or latest_version
    _sync_release_client_state(
        paths,
        bundle_context=bundle_context,
        state=state,
        latest_version=applied_version,
        now=current_time,
        status="manual-updated" if bundle_path is None else "manual-local-bundle",
    )
    return {
        "update_core_status": UPDATE_CORE_STATUS_UPDATED,
        "current_version": current_version,
        "latest_version": latest_version,
        "applied_version": applied_version,
        "applied_bundle_channel": _nonempty_string(manifest.get("distribution_channel")) or "clean",
        "bundle_source": bundle_source,
        "bundle_path": str(target_bundle_path) if target_bundle_path is not None else None,
        "downloaded_bundle_url": bundle_download_url,
        "downloaded_checksum_url": checksum_url,
        "release_entry_trigger": RELEASE_ENTRY_MANUAL_UPDATE_TRIGGER,
        "forced_network_action": bundle_path is None,
        "preserved_paths": sorted(PROTECTED_TOP_LEVEL),
        "applied_paths": applied_names,
        "state_path": _relative_to_workspace(paths, paths.release_client_state_path),
    }

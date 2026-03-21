"""Shared workspace-coordination helpers for mutable DocMason surfaces."""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .project import WorkspacePaths, read_json, write_json


class LeaseConflictError(RuntimeError):
    """Raised when a shared workspace lease cannot be acquired safely."""


def _resource_key(resource: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "-"
        for character in resource.strip()
    )
    if not cleaned:
        raise ValueError("Lease resource is empty.")
    return cleaned


def lease_dir(paths: WorkspacePaths, resource: str) -> Path:
    """Return the coordination-directory path for one leased resource."""
    return paths.coordination_dir / _resource_key(resource)


def lease_payload(paths: WorkspacePaths, resource: str) -> dict[str, Any]:
    """Load one active lease payload when it exists."""
    return read_json(lease_dir(paths, resource) / "lease.json")


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _stale_lease(path: Path, *, stale_after_seconds: float) -> bool:
    payload = read_json(path / "lease.json")
    created_at = payload.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        return True
    try:
        created = created_at.replace("Z", "+00:00")
        age_seconds = time.time() - Path(path / "lease.json").stat().st_mtime
    except OSError:
        return True
    _ = created
    return age_seconds > stale_after_seconds


@contextmanager
def workspace_lease(
    paths: WorkspacePaths,
    resource: str,
    *,
    owner: str | None = None,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.05,
    stale_after_seconds: float = 600.0,
) -> Iterator[dict[str, Any]]:
    """Acquire a best-effort filesystem lease for one shared workspace resource."""
    payload = {
        "resource": resource,
        "owner": owner or str(uuid.uuid4()),
        "created_at": _utc_now(),
    }
    target = lease_dir(paths, resource)
    paths.coordination_dir.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            target.mkdir(parents=False, exist_ok=False)
        except FileExistsError as error:
            if _stale_lease(target, stale_after_seconds=stale_after_seconds):
                shutil.rmtree(target, ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise LeaseConflictError(
                    f"Could not acquire workspace lease for `{resource}` "
                    f"within {timeout_seconds:.1f}s."
                ) from error
            time.sleep(poll_interval_seconds)
            continue
        write_json(target / "lease.json", payload)
        break
    try:
        yield payload
    finally:
        lease_info = read_json(target / "lease.json")
        if lease_info.get("owner") == payload["owner"]:
            shutil.rmtree(target, ignore_errors=True)

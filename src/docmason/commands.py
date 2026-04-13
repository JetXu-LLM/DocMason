"""Command implementations for the DocMason operator surface."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from .control_plane import (
    approve_shared_job,
    block_shared_job,
    classify_sync_materiality,
    complete_shared_job,
    ensure_shared_job,
    pending_interaction_signature,
    required_prepare_capabilities,
    shared_job_control_plane_payload,
    shared_job_is_settled,
    strong_source_fingerprint_signature,
    sync_input_signature,
    workspace_state_snapshot,
)
from .coordination import LeaseConflictError, workspace_lease
from .libreoffice_runtime import validate_soffice_binary
from .project import (
    BOOTSTRAP_STATE_SCHEMA_VERSION,
    MINIMUM_PYTHON,
    SUPPORTED_INPUTS,
    WorkspacePaths,
    adapter_snapshot,
    append_jsonl,
    bootstrap_state,
    bootstrap_state_summary,
    cached_bootstrap_readiness,
    count_source_documents,
    isoformat_timestamp,
    knowledge_base_snapshot,
    locate_workspace,
    manual_workspace_recovery_doc,
    read_json,
    source_runtime_requirements,
    supported_input_tiers,
    supported_source_documents,
    write_json,
)
from .release_entry import release_entry_snapshot
from .review import record_runtime_review_request, refresh_log_review_summary
from .run_control import record_run_event_for_runs, record_shared_job_settled_once
from .toolchain import (
    PREPARED_WORKSPACE_PYTHON_BASELINE,
    inspect_entrypoint,
    inspect_toolchain,
)
from .update_core import UPDATE_CORE_STATUS_ALREADY_CURRENT, UpdateCoreError, perform_update_core
from .workflows import (
    WorkflowMetadata,
    WorkflowMetadataError,
    load_workflow_metadata,
    render_workflow_routing_markdown,
)
from .workspace_probe import (
    office_renderer_snapshot,
    pdf_renderer_snapshot,
    preview_source_changes,
)

READY = "ready"
DEGRADED = "degraded"
ACTION_REQUIRED = "action-required"
UNSUPPORTED_TARGET = "planned but not implemented yet"
_LEASE_RESOURCE_PATTERN = re.compile(r"for `([^`]+)`")
PREPARE_ENTRYPOINT_RETRY_DELAY_SECONDS = 0.35
PREPARE_ENTRYPOINT_RETRY_TIMEOUT_SECONDS = 8.0
_LIBREOFFICE_DOWNLOAD_PAGE = "https://www.libreoffice.org/download/download/"
_LIBREOFFICE_SELECTOR_PAGE = "https://www.libreoffice.org/download/download-libreoffice/"
_LIBREOFFICE_HTTP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class CommandExecution:
    """Captured result of a subprocess call used by a workspace command."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class CommandReport:
    """Structured command result plus human-readable output lines."""

    exit_code: int
    payload: dict[str, Any]
    lines: list[str]


CommandRunner = Callable[[Sequence[str], Path], CommandExecution]
EditableInstallProbe = Callable[[WorkspacePaths], tuple[bool, str]]


def default_runner(command: Sequence[str], cwd: Path) -> CommandExecution:
    """Run a subprocess command and capture trimmed output."""
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandExecution(
        exit_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def _interaction_ingest_snapshot(paths: WorkspacePaths) -> dict[str, Any]:
    """Load interaction-ingest state lazily so command import stays lightweight."""
    from .interaction import interaction_ingest_snapshot

    return interaction_ingest_snapshot(paths)


def _maybe_reconcile_active_thread(paths: WorkspacePaths) -> None:
    """Run active-thread reconciliation lazily so readiness paths avoid heavy imports."""
    from .interaction import maybe_reconcile_active_thread

    maybe_reconcile_active_thread(paths)


def _coordination_payload(
    *,
    state: str,
    code: str,
    detail: str,
    retryable: bool,
    resource: str | None = None,
) -> dict[str, Any]:
    payload = {
        "state": state,
        "code": code,
        "detail": detail,
        "retryable": retryable,
    }
    if isinstance(resource, str) and resource:
        payload["resource"] = resource
    return payload


def _coordination_from_lease_conflict(
    exc: LeaseConflictError,
    *,
    state: str,
) -> dict[str, Any]:
    detail = str(exc)
    match = _LEASE_RESOURCE_PATTERN.search(detail)
    resource = match.group(1) if match else None
    return _coordination_payload(
        state=state,
        code="reconciliation-lease-conflict",
        detail=detail,
        retryable=True,
        resource=resource,
    )


def _active_front_door_context(paths: WorkspacePaths) -> dict[str, Any]:
    """Load active native-thread front-door context when it can be detected honestly."""
    from .conversation import (
        build_log_context,
        current_host_identity,
        detect_agent_surface,
        latest_conversation_turn,
        load_bound_conversation_record_for_host,
        normalize_front_door_state,
    )
    from .interaction import load_bound_native_ledger

    agent_surface = detect_agent_surface()
    host_identity = current_host_identity(agent_surface=agent_surface)
    host_thread_ref = (
        str(host_identity.get("host_thread_ref"))
        if (
            isinstance(host_identity.get("host_thread_ref"), str)
            and host_identity.get("host_thread_ref")
        )
        else None
    )
    conversation = load_bound_conversation_record_for_host(paths, host_identity=host_identity)
    native_ledger = load_bound_native_ledger(paths, host_identity=host_identity)

    latest_turn = latest_conversation_turn(conversation)
    front_door_state = (
        normalize_front_door_state(latest_turn.get("front_door_state"))
        if isinstance(latest_turn, dict)
        else None
    )
    warning: dict[str, Any] | None = None
    if (
        isinstance(host_thread_ref, str)
        and host_thread_ref
        and front_door_state != "canonical-ask"
    ):
        warning = {
            "code": "noncanonical-operator-direct",
            "detail": (
                "This result is operator evidence only. The active host thread has not yet "
                "entered canonical ask ownership for the current turn."
            ),
            "recommended_action": (
                "Route the ordinary question back through canonical ask before treating this "
                "evidence as ordinary-answer completion."
            ),
        }
        anomaly_flags = host_identity.get("anomaly_flags")
        if isinstance(anomaly_flags, list) and anomaly_flags:
            warning["host_identity_anomalies"] = [
                value for value in anomaly_flags if isinstance(value, str) and value
            ]

    log_context = None
    conversation_id = (
        str(conversation.get("conversation_id"))
        if (
            isinstance(conversation.get("conversation_id"), str)
            and conversation.get("conversation_id")
        )
        else None
    )
    if isinstance(conversation_id, str) and conversation_id and isinstance(latest_turn, dict):
        log_context = build_log_context(
            conversation_id=conversation_id,
            turn_id=str(latest_turn.get("turn_id") or ""),
            run_id=(
                str(latest_turn.get("active_run_id"))
                if isinstance(latest_turn.get("active_run_id"), str)
                and latest_turn.get("active_run_id")
                else None
            ),
            entry_workflow_id=str(latest_turn.get("entry_workflow_id") or "ask"),
            inner_workflow_id=str(latest_turn.get("inner_workflow_id") or "ask"),
            native_turn_id=(
                str(latest_turn.get("native_turn_id"))
                if isinstance(latest_turn.get("native_turn_id"), str)
                else None
            ),
            front_door_state=front_door_state,
            question_class=(
                str(latest_turn.get("question_class"))
                if isinstance(latest_turn.get("question_class"), str)
                else None
            ),
            question_domain=(
                str(latest_turn.get("question_domain"))
                if isinstance(latest_turn.get("question_domain"), str)
                else None
            ),
            support_strategy=(
                str(latest_turn.get("support_strategy"))
                if isinstance(latest_turn.get("support_strategy"), str)
                else None
            ),
            analysis_origin=(
                str(latest_turn.get("analysis_origin"))
                if isinstance(latest_turn.get("analysis_origin"), str)
                else None
            ),
            support_basis=(
                str(latest_turn.get("support_basis"))
                if isinstance(latest_turn.get("support_basis"), str)
                else None
            ),
            support_manifest_path=(
                str(latest_turn.get("support_manifest_path"))
                if isinstance(latest_turn.get("support_manifest_path"), str)
                else None
            ),
        )

    return {
        "agent_surface": agent_surface,
        "conversation_id": conversation_id,
        "turn_id": (
            str(latest_turn.get("turn_id"))
            if isinstance(latest_turn, dict) and isinstance(latest_turn.get("turn_id"), str)
            else None
        ),
        "host_identity": host_identity,
        "native_ledger_id": native_ledger.get("ledger_id")
        if isinstance(native_ledger.get("ledger_id"), str)
        else None,
        "turn_front_door_state": front_door_state,
        "canonical_ask_opened": front_door_state == "canonical-ask",
        "warning": warning,
        "log_context": log_context,
    }


def _reconcile_command_context(
    paths: WorkspacePaths,
    *,
    mutating: bool,
) -> dict[str, Any]:
    """Reconcile active native state when safe and classify coordination outcomes."""
    coordination = None
    reconciliation_state = "ready"
    reconciliation_result = None
    try:
        _maybe_reconcile_active_thread(paths)
    except LeaseConflictError as exc:
        reconciliation_state = "blocked" if mutating else "warning"
        coordination = _coordination_from_lease_conflict(
            exc,
            state=reconciliation_state,
        )
    return {
        "state": reconciliation_state,
        "coordination": coordination,
        "reconciliation_result": reconciliation_result,
        "front_door": _active_front_door_context(paths),
    }


def _apply_coordination_warning(
    *,
    payload: dict[str, Any],
    lines: list[str],
    coordination: dict[str, Any] | None,
) -> None:
    if not isinstance(coordination, dict):
        return
    payload["coordination"] = coordination
    lines.append(f"Coordination warning: {coordination['detail']}")


def _mutating_command_coordination_report(
    *,
    command_name: str,
    status_field: str,
    coordination: dict[str, Any],
    environment: dict[str, Any] | None = None,
) -> CommandReport:
    payload: dict[str, Any] = {
        "status": ACTION_REQUIRED,
        status_field: "coordination-blocked",
        "detail": coordination["detail"],
        "coordination": coordination,
    }
    if isinstance(environment, dict):
        payload["environment"] = environment
    lines = [
        f"{command_name}: {ACTION_REQUIRED}",
        coordination["detail"],
        "Next step: retry after the active native-thread reconciliation lease conflict clears.",
    ]
    return make_report(ACTION_REQUIRED, payload, lines)


def _refresh_generated_connector_manifests(paths: WorkspacePaths) -> None:
    """Refresh generated connector manifests lazily."""
    from .interaction import refresh_generated_connector_manifests

    refresh_generated_connector_manifests(paths)


def _validate_workspace(paths: WorkspacePaths, *, target: str) -> dict[str, Any]:
    """Run knowledge-base validation lazily."""
    from .knowledge import validate_workspace

    return validate_workspace(paths, target=target)


def _run_phase4_sync(
    paths: WorkspacePaths,
    *,
    autonomous: bool,
    owner: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run the Phase 4 sync body lazily."""
    from .knowledge import sync_workspace

    return sync_workspace(paths, autonomous=autonomous, owner=owner, run_id=run_id)


def _retrieve_corpus(
    *,
    paths: WorkspacePaths,
    query: str,
    top: int,
    graph_hops: int,
    document_types: list[str] | None,
    source_ids: list[str] | None,
    include_renders: bool,
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Run retrieval lazily so command import avoids hybrid dependencies."""
    from .retrieval import retrieve_corpus

    return retrieve_corpus(
        paths,
        query=query,
        top=top,
        graph_hops=graph_hops,
        document_types=document_types,
        source_ids=source_ids,
        include_renders=include_renders,
        log_context=log_context,
        log_origin=log_origin,
    )


def _trace_source(
    *,
    paths: WorkspacePaths,
    source_id: str,
    unit_id: str | None,
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Trace one source lazily."""
    from .retrieval import trace_source

    return trace_source(
        paths,
        source_id=source_id,
        unit_id=unit_id,
        log_context=log_context,
        log_origin=log_origin,
    )


def _trace_answer_file(
    *,
    paths: WorkspacePaths,
    answer_file: Path,
    top: int,
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Trace one answer file lazily."""
    from .retrieval import trace_answer_file

    return trace_answer_file(
        paths,
        answer_file=answer_file,
        top=top,
        log_context=log_context,
        log_origin=log_origin,
    )


def _trace_session(
    *,
    paths: WorkspacePaths,
    session_id: str,
    top: int,
    log_context: dict[str, str] | None = None,
    log_origin: str | None = None,
) -> dict[str, Any]:
    """Trace one retrieval session lazily."""
    from .retrieval import trace_session

    return trace_session(
        paths,
        session_id=session_id,
        top=top,
        log_context=log_context,
        log_origin=log_origin,
    )


def _run_operator_eval(paths: WorkspacePaths) -> tuple[dict[str, Any], list[str]]:
    """Run operator eval lazily."""
    from .operator_eval import run_operator_eval

    return run_operator_eval(paths)


def summarize_command_failure(command: Sequence[str], execution: CommandExecution) -> str:
    """Render a compact failure summary for subprocess-driven steps."""
    details = execution.stderr or execution.stdout or "no output"
    return f"{' '.join(command)} failed with exit code {execution.exit_code}: {details}"


def _unique_command_owner(job_family: str) -> dict[str, Any]:
    """Return a unique control-plane owner identity for one command invocation."""
    return {
        "kind": "command",
        "id": f"{job_family}-command:{uuid.uuid4()}",
        "pid": os.getpid(),
    }


def python_supported() -> bool:
    """Return whether the current interpreter satisfies the minimum version."""
    return sys.version_info >= MINIMUM_PYTHON


def platform_supported() -> bool:
    """Return whether the current platform is in the supported set."""
    return sys.platform in {"darwin", "linux"}


def discover_uv_binary(workspace: WorkspacePaths | None = None) -> tuple[str | None, str | None]:
    """Resolve uv from repo-local bootstrap tooling first, then from the active PATH."""
    if workspace is not None and workspace.toolchain_bootstrap_uv.exists():
        return str(workspace.toolchain_bootstrap_uv), "bootstrap-venv-reused"
    path_uv = shutil.which("uv")
    if path_uv is not None:
        return path_uv, "shared-uv"
    return None, None


def find_uv_binary(workspace: WorkspacePaths | None = None) -> str | None:
    """Resolve uv from repo-local bootstrap tooling or the active PATH when available."""
    uv_binary, _source = discover_uv_binary(workspace)
    return uv_binary


def uv_binary_mode(workspace: WorkspacePaths, uv_binary: str | None) -> str | None:
    """Classify which uv surface the current command is using."""
    if not isinstance(uv_binary, str) or not uv_binary:
        return None
    if uv_binary == str(workspace.toolchain_bootstrap_uv):
        return "bootstrap-venv-reused"
    return "shared-uv"


def find_brew_binary() -> str | None:
    """Resolve Homebrew from the active ``PATH`` when available."""
    return shutil.which("brew")


def user_scoped_uv_path() -> Path | None:
    """Return the default user-scoped uv path for installer fallback flows."""
    if site.USER_BASE is None:
        return None
    return Path(site.USER_BASE) / "bin" / "uv"


def preferred_uv_install_command(bootstrap_python: str) -> tuple[list[str], str]:
    """Choose the least-friction uv install command for the current machine."""
    brew_binary = find_brew_binary()
    # On macOS, Homebrew usually provides the cleanest PATH behavior for end users.
    if sys.platform == "darwin" and brew_binary is not None:
        return [brew_binary, "install", "uv"], "`brew install uv`"
    return [bootstrap_python, "-m", "pip", "install", "--user", "uv"], (
        f"`{bootstrap_python} -m pip install --user uv`"
    )


def preferred_libreoffice_install_command() -> tuple[list[str] | None, str | None]:
    """Choose the preferred supported LibreOffice install command when automation is possible."""
    brew_binary = find_brew_binary()
    if sys.platform == "darwin" and brew_binary is not None:
        return [brew_binary, "install", "--cask", "libreoffice-still"], (
            "`brew install --cask libreoffice-still`"
        )
    return None, None


def _emit_prepare_progress(progress_stream: TextIO | None, message: str) -> None:
    """Emit one concise prepare progress banner when the caller requested it."""
    if progress_stream is None:
        return
    progress_stream.write(f"Prepare progress: {message}\n")
    progress_stream.flush()


def _read_text_url(url: str, *, timeout_seconds: float = _LIBREOFFICE_HTTP_TIMEOUT_SECONDS) -> str:
    """Fetch one text response from an HTTPS endpoint using the standard library only."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "DocMason/0.1 bootstrap"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = cast(bytes, response.read())
        return payload.decode("utf-8", errors="replace")


def _download_url_to_path(
    url: str,
    destination: Path,
    *,
    timeout_seconds: float = _LIBREOFFICE_HTTP_TIMEOUT_SECONDS,
) -> None:
    """Download one URL into a local path using the standard library only."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "DocMason/0.1 bootstrap"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)


def _macos_libreoffice_download_type(machine: str | None = None) -> str:
    """Map the current macOS architecture to the official LibreOffice download selector."""
    normalized_machine = (machine or platform.machine()).strip().lower()
    if normalized_machine == "arm64":
        return "mac-aarch64"
    if normalized_machine == "x86_64":
        return "mac-x86_64"
    raise ValueError(
        "Official LibreOffice auto-install is only configured for macOS `arm64` and "
        f"`x86_64`, not `{normalized_machine or 'unknown'}`."
    )


def _resolve_official_libreoffice_macos_download(
    *,
    machine: str | None = None,
) -> dict[str, str]:
    """Resolve the current stable official LibreOffice DMG URL for the active macOS arch."""
    download_type = _macos_libreoffice_download_type(machine)
    selector_html = _read_text_url(_LIBREOFFICE_SELECTOR_PAGE)
    version_match = re.search(
        rf"/download/download-libreoffice/\?type={re.escape(download_type)}&version="
        r"(?P<version>[0-9][^&\"']*)&lang=en-US",
        selector_html,
    )
    if version_match is None:
        raise RuntimeError(
            "Could not determine the current stable LibreOffice version from the official "
            "download selector page."
        )
    version = str(version_match.group("version"))
    arch_page_url = (
        f"{_LIBREOFFICE_SELECTOR_PAGE}?type={download_type}&version={version}&lang=en-US"
    )
    arch_page_html = _read_text_url(arch_page_url)
    redirect_match = re.search(
        r'<a class="dl_download_link" href="(?P<redirect>https://www\.libreoffice\.org/'
        r'donate/dl/[^"]+\.dmg)"',
        arch_page_html,
    )
    if redirect_match is None:
        raise RuntimeError(
            "Could not determine the official LibreOffice redirect page for the current "
            "macOS architecture."
        )
    redirect_page_url = str(redirect_match.group("redirect"))
    redirect_html = _read_text_url(redirect_page_url)
    dmg_match = re.search(
        r'content="0;\s*url=(?P<dmg_url>https://download\.documentfoundation\.org/[^"]+\.dmg)"',
        redirect_html,
        flags=re.IGNORECASE,
    )
    if dmg_match is None:
        raise RuntimeError(
            "Could not determine the final LibreOffice DMG URL from the official redirect page."
        )
    dmg_url = str(dmg_match.group("dmg_url"))
    file_name = Path(urllib.parse.urlparse(dmg_url).path).name
    if not file_name.endswith(".dmg"):
        raise RuntimeError("The official LibreOffice download target did not resolve to a DMG.")
    return {
        "version": version,
        "download_type": download_type,
        "selector_page_url": _LIBREOFFICE_SELECTOR_PAGE,
        "arch_page_url": arch_page_url,
        "redirect_page_url": redirect_page_url,
        "dmg_url": dmg_url,
        "file_name": file_name,
        "manual_download_page": _LIBREOFFICE_DOWNLOAD_PAGE,
    }


def _detach_macos_disk_image(
    mount_point: Path,
    *,
    command_runner: CommandRunner,
    cwd: Path,
) -> str | None:
    """Detach one mounted DMG, escalating to `-force` only when the normal detach fails."""
    execution = command_runner(
        ["/usr/bin/hdiutil", "detach", str(mount_point)],
        cwd,
    )
    if execution.exit_code == 0:
        return None
    forced = command_runner(
        ["/usr/bin/hdiutil", "detach", "-force", str(mount_point)],
        cwd,
    )
    if forced.exit_code == 0:
        return None
    return forced.stderr or forced.stdout or execution.stderr or execution.stdout or "no output"


def _install_libreoffice_from_official_macos_package(
    workspace: WorkspacePaths,
    *,
    command_runner: CommandRunner,
    machine: str | None = None,
) -> tuple[bool, str]:
    """Install LibreOffice from the official macOS DMG when Homebrew is unavailable."""
    if sys.platform != "darwin":
        return False, "Official LibreOffice auto-install is only supported on macOS."

    try:
        download = _resolve_official_libreoffice_macos_download(machine=machine)
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return (
            False,
            "Official LibreOffice auto-install failed before download resolution. Details: "
            f"{detail}",
        )

    workspace.agent_work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="libreoffice-install-",
        dir=workspace.agent_work_dir,
    ) as tempdir_name:
        tempdir = Path(tempdir_name)
        dmg_path = tempdir / download["file_name"]
        mount_point = tempdir / "mount"
        mount_point.mkdir(parents=True, exist_ok=True)
        attached = False
        try:
            _download_url_to_path(download["dmg_url"], dmg_path)
        except Exception as exc:
            detail = str(exc).strip() or type(exc).__name__
            return (
                False,
                "Official LibreOffice auto-install failed while downloading the DMG. Details: "
                f"{detail}",
            )

        imageinfo = command_runner(
            ["/usr/bin/hdiutil", "imageinfo", str(dmg_path)],
            workspace.root,
        )
        if imageinfo.exit_code != 0:
            return (
                False,
                "Official LibreOffice auto-install failed while validating the downloaded DMG. "
                f"Details: {imageinfo.stderr or imageinfo.stdout or 'no output'}",
            )

        attach = command_runner(
            [
                "/usr/bin/hdiutil",
                "attach",
                "-nobrowse",
                "-readonly",
                "-mountpoint",
                str(mount_point),
                str(dmg_path),
            ],
            workspace.root,
        )
        if attach.exit_code != 0:
            return (
                False,
                "Official LibreOffice auto-install failed while mounting the downloaded DMG. "
                f"Details: {attach.stderr or attach.stdout or 'no output'}",
            )
        attached = True
        detach_error = None
        try:
            app_bundle = next(
                (
                    path
                    for path in mount_point.rglob("LibreOffice.app")
                    if path.name == "LibreOffice.app"
                ),
                None,
            )
            if app_bundle is None:
                return (
                    False,
                    "Official LibreOffice auto-install mounted the DMG, but `LibreOffice.app` "
                    "was not found inside it.",
                )

            copy = command_runner(
                ["/usr/bin/ditto", str(app_bundle), "/Applications/LibreOffice.app"],
                workspace.root,
            )
            if copy.exit_code != 0:
                return (
                    False,
                    "Official LibreOffice auto-install failed while copying LibreOffice.app into "
                    f"/Applications. Details: {copy.stderr or copy.stdout or 'no output'}",
                )

            validation = validate_soffice_binary(
                "/Applications/LibreOffice.app/Contents/MacOS/soffice"
            )
            if not validation["ready"]:
                return (
                    False,
                    "Official LibreOffice auto-install copied LibreOffice.app, but the installed "
                    f"`soffice` validation failed. Details: {validation['detail']}",
                )
        finally:
            if attached:
                detach_error = _detach_macos_disk_image(
                    mount_point,
                    command_runner=command_runner,
                    cwd=workspace.root,
                )
                attached = False

        if detach_error is not None:
            return (
                False,
                "Official LibreOffice auto-install succeeded through validation, but the "
                f"downloaded DMG could not be detached cleanly. Details: {detach_error}",
            )

    version_suffix = (
        f" ({validation['version']})"
        if isinstance(validation.get("version"), str) and validation["version"]
        else ""
    )
    return (
        True,
        "Installed LibreOffice from the official macOS package at "
        f"/Applications/LibreOffice.app{version_suffix}.",
    )


def ensure_python_pip(
    python_executable: str,
    *,
    cwd: Path,
    command_runner: CommandRunner,
) -> tuple[bool, str]:
    """Ensure that a Python interpreter exposes pip, using ensurepip when needed."""
    pip_check = command_runner([python_executable, "-m", "pip", "--version"], cwd)
    if pip_check.exit_code == 0:
        return True, "pip is available."

    ensurepip = command_runner([python_executable, "-m", "ensurepip", "--upgrade"], cwd)
    if ensurepip.exit_code != 0:
        return (
            False,
            summarize_command_failure([python_executable, "-m", "ensurepip"], ensurepip),
        )

    pip_verify = command_runner([python_executable, "-m", "pip", "--version"], cwd)
    if pip_verify.exit_code == 0:
        return True, "Restored pip with ensurepip."
    return (
        False,
        summarize_command_failure([python_executable, "-m", "pip", "--version"], pip_verify),
    )


def remove_generated_path(path: Path) -> None:
    """Remove a generated file, directory, or symlink so it can be recreated cleanly."""
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)


def skill_shim_sources(paths: WorkspacePaths) -> list[Path]:
    """Return the authored skill directories that thin repo-local shims should expose."""
    directories = paths.agent_skill_directories(include_operator=True, include_optional=True)
    names = [directory.name for directory in directories]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        duplicate_list = ", ".join(duplicates)
        raise ValueError(
            f"Cannot generate repo-local skill shims because skill names collide: {duplicate_list}."
        )
    return directories


def sync_skill_shim_root(root: Path, skill_directories: list[Path]) -> list[Path]:
    """Mirror authored skill directories into a flat symlink shim root."""
    if root.is_symlink() or root.is_file():
        remove_generated_path(root)
    root.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []
    expected_names = {directory.name for directory in skill_directories}

    for existing in list(root.iterdir()):
        if existing.name not in expected_names:
            remove_generated_path(existing)

    for source_dir in skill_directories:
        destination = root / source_dir.name
        relative_target = os.path.relpath(source_dir, root)
        if destination.is_symlink():
            current_target = os.readlink(destination)
            if current_target == relative_target:
                generated.append(destination)
                continue
            destination.unlink()
        elif destination.exists():
            remove_generated_path(destination)
        os.symlink(relative_target, destination)
        generated.append(destination)
    return generated


def sync_repo_local_skill_shims(paths: WorkspacePaths) -> list[Path]:
    """Generate the thin repo-local skill shim layers for supported agent surfaces."""
    skill_directories = skill_shim_sources(paths)
    generated = sync_skill_shim_root(paths.repo_skill_shim_dir, skill_directories)
    generated.extend(sync_skill_shim_root(paths.claude_skill_shim_dir, skill_directories))
    return generated


def inspect_editable_install(paths: WorkspacePaths) -> tuple[bool, str]:
    """Confirm that the workspace resolves to the editable source tree inside ``.venv``."""
    if not paths.venv_python.exists():
        return False, f"Missing virtual environment interpreter at {paths.venv_python}."

    try:
        completed = subprocess.run(
            [
                str(paths.venv_python),
                "-c",
                ("import pathlib, docmason; print(pathlib.Path(docmason.__file__).resolve())"),
            ],
            cwd=paths.root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return False, f"Editable install probe failed to execute: {exc.strerror or str(exc)}"
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "import failed"
        return False, f"Editable install probe failed: {detail}"

    module_path = Path(completed.stdout.strip())
    expected_root = (paths.root / "src").resolve()
    if str(module_path).startswith(str(expected_root)):
        return True, f"Editable install resolves to {module_path}."
    return False, f"DocMason resolves outside the workspace source tree: {module_path}."


def deduplicate(items: list[str]) -> list[str]:
    """Preserve the first occurrence of each string in order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _stable_json_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _inspect_prepare_entrypoint(workspace: WorkspacePaths) -> tuple[dict[str, Any], bool]:
    """Probe entrypoint health for prepare, retrying once on transient startup-silent."""
    probe = inspect_entrypoint(workspace)
    if str(probe.get("entrypoint_health") or "") != "startup-silent":
        return probe, False
    time.sleep(PREPARE_ENTRYPOINT_RETRY_DELAY_SECONDS)
    retried_probe = inspect_entrypoint(
        workspace,
        timeout_seconds=PREPARE_ENTRYPOINT_RETRY_TIMEOUT_SECONDS,
    )
    recovered = str(retried_probe.get("entrypoint_health") or "") == "ready"
    return retried_probe, recovered


def make_report(status: str, payload: dict[str, Any], lines: list[str]) -> CommandReport:
    """Translate a logical status into the standard CLI exit-code contract."""
    exit_code = {READY: 0, DEGRADED: 2, ACTION_REQUIRED: 1}[status]
    if "status" in payload:
        payload["status"] = status
    return CommandReport(exit_code=exit_code, payload=payload, lines=lines)


def _record_shared_job_settlement(workspace: WorkspacePaths, shared_job: dict[str, Any]) -> None:
    if not shared_job_is_settled(shared_job):
        return
    record_shared_job_settled_once(
        workspace,
        run_ids=shared_job.get("attached_run_ids"),
        job_id=str(shared_job.get("job_id") or ""),
        status=str(shared_job.get("status") or ""),
    )


def _settle_sync_shared_job(
    workspace: WorkspacePaths,
    shared_job: dict[str, Any],
    *,
    result: dict[str, Any] | None = None,
    unexpected_error: Exception | None = None,
) -> dict[str, Any]:
    if not shared_job:
        return {}
    if shared_job_is_settled(shared_job):
        _record_shared_job_settlement(workspace, shared_job)
        return shared_job
    job_id = str(shared_job.get("job_id") or "")
    if not job_id:
        return shared_job
    if unexpected_error is not None:
        detail = str(unexpected_error).strip() or type(unexpected_error).__name__
        shared_job = block_shared_job(
            workspace,
            job_id,
            result={
                "status": "blocked",
                "detail": (
                    "Unexpected sync failure: "
                    f"{type(unexpected_error).__name__}: {detail}"
                ),
            },
        )
    elif isinstance(result, dict):
        if result["status"] in {"valid", "warnings"} and (
            bool(result.get("published")) or bool(result.get("publish_skipped"))
        ):
            shared_job = complete_shared_job(workspace, job_id, result=result)
        elif result["status"] in {"action-required", "blocking-errors"}:
            shared_job = block_shared_job(
                workspace,
                job_id,
                result={"detail": result.get("detail"), "status": result.get("status")},
            )
        elif not shared_job_is_settled(shared_job):
            shared_job = block_shared_job(
                workspace,
                job_id,
                result={
                    "status": "blocked",
                    "detail": (
                        f"Sync returned non-terminal status: {result.get('status')}"
                    ),
                },
            )
    _record_shared_job_settlement(workspace, shared_job)
    return shared_job


def bootstrap_workspace_with_launcher(
    paths: WorkspacePaths | None = None,
    *,
    command_runner: CommandRunner = default_runner,
) -> CommandReport:
    """Run the canonical zero-to-working bootstrap launcher and normalize its result."""
    workspace = paths or locate_workspace()
    launcher_path = workspace.root / "scripts" / "bootstrap-workspace.sh"
    launcher_command = "./scripts/bootstrap-workspace.sh --yes --json"
    if not launcher_path.exists():
        missing_payload: dict[str, Any] = {
            "status": ACTION_REQUIRED,
            "detail": "The canonical workspace bootstrap launcher is missing.",
            "launcher_command": launcher_command,
            "launcher_delegated": True,
            "next_steps": [manual_workspace_recovery_doc()],
        }
        lines: list[str] = [
            f"Bootstrap launcher status: {ACTION_REQUIRED}",
            str(missing_payload["detail"]),
        ]
        return make_report(ACTION_REQUIRED, missing_payload, lines)

    execution = command_runner(["/bin/bash", str(launcher_path), "--yes", "--json"], workspace.root)
    payload: dict[str, Any] = {}
    stdout_text = execution.stdout.strip()
    if stdout_text:
        try:
            decoded = json.loads(stdout_text)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}

    status = str(payload.get("status") or "")
    if status not in {READY, DEGRADED, ACTION_REQUIRED}:
        status = READY if execution.exit_code == 0 else ACTION_REQUIRED

    detail = str(payload.get("detail") or "").strip()
    if not detail:
        detail = execution.stderr or stdout_text or "The bootstrap launcher returned no detail."

    normalized_payload = {
        **payload,
        "status": status,
        "detail": detail,
        "launcher_command": launcher_command,
        "launcher_delegated": True,
        "launcher_exit_code": execution.exit_code,
    }
    if execution.stderr:
        normalized_payload["launcher_stderr"] = execution.stderr

    next_steps = normalized_payload.get("next_steps")
    if not isinstance(next_steps, list):
        normalized_payload["next_steps"] = []

    lines = [
        f"Bootstrap launcher status: {status}",
        detail,
    ]
    return make_report(status, normalized_payload, lines)


@contextmanager
def _temporary_env(overrides: dict[str, str]) -> Any:
    """Temporarily apply environment overrides for subprocess-driven bootstrap steps."""
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        os.environ.update(overrides)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _ensure_repo_local_toolchain_dirs(workspace: WorkspacePaths) -> list[str]:
    """Create the repo-local toolchain directory layout when it is missing."""
    created: list[str] = []
    for directory in (
        workspace.docmason_dir,
        workspace.toolchain_dir,
        workspace.toolchain_python_dir,
        workspace.toolchain_python_installs_dir,
        workspace.toolchain_cache_dir,
        workspace.toolchain_uv_cache_dir,
        workspace.toolchain_pip_cache_dir,
        workspace.toolchain_bootstrap_dir,
        workspace.toolchain_state_dir,
    ):
        if directory.exists():
            continue
        directory.mkdir(parents=True, exist_ok=True)
        created.append(str(directory.relative_to(workspace.root)))
    return created


def _refresh_symlink(link_path: Path, target_path: Path) -> None:
    """Replace one symlink atomically enough for repo-local toolchain updates."""
    if link_path.is_symlink() or link_path.exists():
        remove_generated_path(link_path)
    link_path.parent.mkdir(parents=True, exist_ok=True)
    relative_target = os.path.relpath(target_path, link_path.parent)
    os.symlink(relative_target, link_path)


def _install_uv_into_bootstrap_venv(
    workspace: WorkspacePaths,
    *,
    bootstrap_python: str,
    command_runner: CommandRunner,
) -> tuple[str | None, list[str], list[str]]:
    """Create the repo-local bootstrap helper venv and install uv into it."""
    actions_performed: list[str] = []
    actions_skipped: list[str] = []
    creation = command_runner(
        [bootstrap_python, "-m", "venv", str(workspace.toolchain_bootstrap_venv_dir)],
        workspace.root,
    )
    if creation.exit_code != 0:
        actions_skipped.append(
            summarize_command_failure([bootstrap_python, "-m", "venv"], creation)
        )
        return None, actions_performed, actions_skipped
    actions_performed.append(
        "Created the repo-local bootstrap helper venv under `.docmason/toolchain/bootstrap/venv`."
    )
    pip_ready, pip_detail = ensure_python_pip(
        str(workspace.toolchain_bootstrap_python),
        cwd=workspace.root,
        command_runner=command_runner,
    )
    if not pip_ready:
        actions_skipped.append(f"Bootstrap helper pip is unavailable. Details: {pip_detail}")
        return None, actions_performed, actions_skipped
    if pip_detail == "Restored pip with ensurepip.":
        actions_performed.append("Restored bootstrap-helper pip with ensurepip.")
    with _temporary_env({"PIP_CACHE_DIR": str(workspace.toolchain_pip_cache_dir)}):
        install = command_runner(
            [str(workspace.toolchain_bootstrap_python), "-m", "pip", "install", "uv"],
            workspace.root,
        )
    if install.exit_code != 0:
        actions_skipped.append(
            summarize_command_failure(
                [str(workspace.toolchain_bootstrap_python), "-m", "pip", "install", "uv"],
                install,
            )
        )
        return None, actions_performed, actions_skipped
    if workspace.toolchain_bootstrap_uv.exists():
        actions_performed.append("Installed uv into the repo-local bootstrap helper venv.")
        return str(workspace.toolchain_bootstrap_uv), actions_performed, actions_skipped
    actions_skipped.append(
        "uv install completed but the bootstrap-helper uv executable is missing."
    )
    return None, actions_performed, actions_skipped


def _provision_managed_python(
    workspace: WorkspacePaths,
    *,
    uv_binary: str,
    command_runner: CommandRunner,
) -> tuple[Path | None, str | None]:
    """Provision the repo-local managed Python baseline and return its executable."""
    with _temporary_env({"UV_CACHE_DIR": str(workspace.toolchain_uv_cache_dir)}):
        install = command_runner(
            [
                uv_binary,
                "python",
                "install",
                PREPARED_WORKSPACE_PYTHON_BASELINE,
                "--install-dir",
                str(workspace.toolchain_python_installs_dir),
            ],
            workspace.root,
        )
    if install.exit_code != 0:
        return None, summarize_command_failure(
            [uv_binary, "python", "install", PREPARED_WORKSPACE_PYTHON_BASELINE],
            install,
        )
    from .toolchain import latest_managed_python_candidate

    executable = latest_managed_python_candidate(workspace)
    if executable is None:
        return (
            None,
            "uv reported success, but no repo-local managed Python 3.13 executable "
            "was found.",
        )
    _refresh_symlink(workspace.toolchain_python_current_dir, executable.parent.parent)
    return (
        workspace.toolchain_python_current_dir
        / "bin"
        / f"python{PREPARED_WORKSPACE_PYTHON_BASELINE}",
        None,
    )


def _rebuild_repo_local_venv(
    workspace: WorkspacePaths,
    *,
    uv_binary: str,
    managed_python: Path,
    command_runner: CommandRunner,
) -> str | None:
    """Rebuild `.venv` against the repo-local managed Python baseline."""
    with _temporary_env({"UV_CACHE_DIR": str(workspace.toolchain_uv_cache_dir)}):
        creation = command_runner(
            [
                uv_binary,
                "venv",
                "--clear",
                "--python",
                str(managed_python),
                str(workspace.venv_dir),
            ],
            workspace.root,
        )
    if creation.exit_code != 0:
        return summarize_command_failure([uv_binary, "venv", "--clear"], creation)
    return None


def _install_workspace_into_repo_local_venv(
    workspace: WorkspacePaths,
    *,
    uv_binary: str,
    command_runner: CommandRunner,
) -> str | None:
    """Install DocMason into the rebuilt repo-local `.venv`."""
    with _temporary_env({"UV_CACHE_DIR": str(workspace.toolchain_uv_cache_dir)}):
        install = command_runner(
            [uv_binary, "pip", "install", "--python", str(workspace.venv_python), "-e", ".[dev]"],
            workspace.root,
        )
    if install.exit_code != 0:
        return summarize_command_failure([uv_binary, "pip", "install"], install)
    return None


def _steady_state_pdf_renderer_snapshot(
    workspace: WorkspacePaths,
    *,
    command_runner: CommandRunner,
) -> dict[str, Any]:
    """Probe PDF renderer readiness through the rebuilt repo-local `.venv`."""
    probe = command_runner(
        [
            str(workspace.venv_python),
            "-c",
            (
                "import json; "
                "from docmason.workspace_probe import pdf_renderer_snapshot; "
                "print(json.dumps(pdf_renderer_snapshot()))"
            ),
        ],
        workspace.root,
    )
    payload_text = probe.stdout or probe.stderr
    if probe.exit_code != 0 or not payload_text:
        return {
            "ready": False,
            "detail": summarize_command_failure(
                [str(workspace.venv_python), "-c", "pdf_renderer_snapshot()"],
                probe,
            ),
            "missing": [],
        }
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return {
            "ready": False,
            "detail": (
                "The repo-local `.venv` PDF renderer probe returned non-JSON output during "
                "prepare."
            ),
            "missing": [],
        }
    if not isinstance(payload, dict):
        return {
            "ready": False,
            "detail": (
                "The repo-local `.venv` PDF renderer probe returned an invalid payload during "
                "prepare."
            ),
            "missing": [],
        }
    return payload


def validation_command_status(validation_status: str) -> str:
    """Map a validation result to the CLI status contract."""
    if validation_status == "valid":
        return READY
    if validation_status in {"warnings", "pending-synthesis"}:
        return DEGRADED
    return ACTION_REQUIRED


def office_renderer_next_step() -> str:
    """Return the preferred next-step guidance for installing or repairing LibreOffice."""
    if sys.platform == "darwin":
        if find_brew_binary():
            return (
                "Run `docmason prepare --yes` to let the workspace install or repair "
                "LibreOffice. If you need to do it manually, use `brew install --cask "
                "libreoffice-still` or the official macOS installer from "
                f"{_LIBREOFFICE_DOWNLOAD_PAGE}, then rerun `docmason doctor`."
            )
        return (
            "Run `docmason prepare --yes` to let the workspace install or repair "
            "LibreOffice through the official macOS installer path, or download it from "
            f"{_LIBREOFFICE_DOWNLOAD_PAGE}. On macOS, drag the app into `/Applications`; "
            "DocMason will detect the standard `soffice` path there. Then rerun "
            "`docmason doctor`."
        )
    return (
        "Install or repair LibreOffice with your Linux distribution's package manager or from "
        f"{_LIBREOFFICE_DOWNLOAD_PAGE}, ensure `soffice` is on PATH, "
        "then rerun `docmason doctor`."
    )


def _resolve_bootstrap_source(workspace: WorkspacePaths) -> str:
    """Classify the current bootstrap entrypoint for prepare-state reporting."""
    explicit = os.environ.get("DOCMASON_BOOTSTRAP_SOURCE", "").strip()
    if explicit:
        return explicit

    executable = Path(sys.executable).resolve()
    if workspace.toolchain_bootstrap_python.exists():
        with suppress(OSError):
            if executable == workspace.toolchain_bootstrap_python.resolve():
                return "repo-local-bootstrap-venv"
    if workspace.toolchain_python_current_dir.exists():
        current_python = workspace.toolchain_python_current_dir / "bin" / "python3.13"
        with suppress(OSError):
            if current_python.exists() and executable == current_python.resolve():
                return "repo-local-managed"

    manual_override = os.environ.get("DOCMASON_BOOTSTRAP_PYTHON", "").strip()
    if manual_override:
        with suppress(OSError):
            if executable == Path(manual_override).expanduser().resolve():
                return "manual-bootstrap-python"
    return "shared-python"


def _native_machine_baseline_install_guidance(
    *,
    rerun_command: str = "docmason prepare --yes",
) -> str:
    if find_brew_binary():
        manual_follow_up = (
            "install LibreOffice yourself with `brew install --cask libreoffice-still` or from "
            f"{_LIBREOFFICE_DOWNLOAD_PAGE}"
        )
    else:
        manual_follow_up = f"install LibreOffice from {_LIBREOFFICE_DOWNLOAD_PAGE}"
    return (
        "Run "
        f"`{rerun_command}` to let DocMason install or repair the native machine baseline. "
        "If that managed path still cannot finish, "
        f"{manual_follow_up} manually, then rerun the same command."
    )


def _codex_full_access_guidance(*, rerun_command: str = "docmason prepare --yes") -> str:
    return (
        "DocMason is currently running in Codex `Default permissions`. This bootstrap step needs "
        "capabilities that the current thread does not expose there, such as repo-local runtime "
        "downloads or machine-level setup. Clicking `Yes` on a single command prompt only "
        "approves that command; it does not switch the thread out of `Default permissions`. "
        "Switch this thread to `Full access`, then continue the same task or rerun "
        f"`{rerun_command}`."
    )


def _generic_host_access_guidance(*, rerun_command: str = "docmason prepare --yes") -> str:
    return (
        "DocMason needs broader host permissions or network access before this bootstrap step "
        "can continue. Allow the higher-access path on the current host, then continue the same "
        f"task or rerun `{rerun_command}`."
    )


def _workspace_path_is_writable(
    host_execution: dict[str, Any],
    *,
    target_path: Path,
) -> bool:
    if bool(host_execution.get("full_machine_access")):
        return True
    writable_roots = host_execution.get("sandbox_writable_roots")
    if not isinstance(writable_roots, list) or not writable_roots:
        return False
    try:
        resolved_target = target_path.resolve()
    except OSError:
        resolved_target = target_path
    for raw_root in writable_roots:
        if not isinstance(raw_root, str) or not raw_root:
            continue
        try:
            resolved_root = Path(raw_root).expanduser().resolve()
        except OSError:
            resolved_root = Path(raw_root).expanduser()
        with suppress(ValueError):
            resolved_target.relative_to(resolved_root)
            return True
    return False


def _host_execution_network_access(host_execution: dict[str, Any]) -> bool | None:
    if bool(host_execution.get("full_machine_access")):
        return True
    value = host_execution.get("workspace_write_network_access")
    return value if isinstance(value, bool) else None


def _prepare_host_access_snapshot(
    workspace: WorkspacePaths,
    *,
    host_execution: dict[str, Any],
    machine_baseline: dict[str, Any],
    workspace_runtime_ready: bool,
    rerun_command: str = "docmason prepare --yes",
) -> dict[str, Any]:
    """Classify whether the current host can continue prepare without extra access."""
    provider = str(host_execution.get("host_provider") or "unknown-agent")
    permission_mode = str(host_execution.get("permission_mode") or "")
    full_machine_access = bool(host_execution.get("full_machine_access"))
    network_access = _host_execution_network_access(host_execution)
    workspace_root_writable = _workspace_path_is_writable(
        host_execution,
        target_path=workspace.root,
    )
    writable_roots = host_execution.get("sandbox_writable_roots")
    writable_roots_known = isinstance(writable_roots, list) and bool(writable_roots)
    reasons: list[str] = []
    host_access_required = False

    machine_reasons = machine_baseline.get("host_access_reasons")
    if isinstance(machine_reasons, list):
        reasons.extend(
            str(item)
            for item in machine_reasons
            if isinstance(item, str) and item.strip()
        )
        host_access_required = host_access_required or bool(
            machine_baseline.get("host_access_required")
        )

    if not workspace_runtime_ready:
        if not workspace_root_writable and not full_machine_access and writable_roots_known:
            reasons.append(
                "The current sandbox cannot write the workspace root required for repo-local "
                "bootstrap."
            )
            host_access_required = True
        if network_access is False:
            reasons.append(
                "Repo-local runtime bootstrap needs network downloads, but the current host "
                "execution context reports network access is disabled."
            )
            host_access_required = True
        elif network_access is None and provider == "codex" and not full_machine_access:
            reasons.append(
                "DocMason cannot safely confirm that this Codex turn allows the network "
                "downloads required for repo-local runtime bootstrap."
            )
            host_access_required = True

    reasons = deduplicate(reasons)
    if host_access_required:
        guidance = (
            _codex_full_access_guidance(rerun_command=rerun_command)
            if provider == "codex"
            else _generic_host_access_guidance(rerun_command=rerun_command)
        )
    else:
        guidance = None

    return {
        "host_execution": host_execution,
        "workspace_write_network_access": network_access,
        "sandbox_writable_roots": list(host_execution.get("sandbox_writable_roots") or []),
        "host_access_required": host_access_required,
        "host_access_guidance": guidance,
        "host_access_reasons": reasons,
        "permission_mode": permission_mode or None,
    }


def _machine_baseline_snapshot(
    workspace: WorkspacePaths,
    *,
    office_snapshot: dict[str, Any] | None = None,
    host_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe native machine-baseline readiness separately from repo-local runtime state."""
    from .conversation import current_host_execution_context

    host_execution = host_execution or current_host_execution_context()
    office = office_snapshot or office_renderer_snapshot(workspace)
    brew_binary = find_brew_binary()
    brew_ready = bool(brew_binary)
    libreoffice_required = bool(office.get("required"))
    libreoffice_ready = bool(office.get("ready"))
    libreoffice_binary = office.get("binary")
    libreoffice_candidate_binary = office.get("candidate_binary") or libreoffice_binary
    libreoffice_validation_detail = office.get("validation_detail")
    libreoffice_probe_contract = office.get("probe_contract")
    libreoffice_detected_but_unusable = bool(office.get("detected_but_unusable"))
    provider = str(host_execution.get("host_provider") or "unknown-agent")
    applicable = sys.platform == "darwin" and provider == "codex"
    if not applicable:
        return {
            "applicable": False,
            "ready": True,
            "status": "not-applicable",
            "detail": "Native macOS machine-baseline policy is not active for this host surface.",
            "brew_ready": brew_ready,
            "brew_binary": brew_binary,
            "libreoffice_required": libreoffice_required,
            "libreoffice_ready": libreoffice_ready,
            "libreoffice_binary": libreoffice_binary,
            "libreoffice_candidate_binary": libreoffice_candidate_binary,
            "libreoffice_validation_detail": libreoffice_validation_detail,
            "libreoffice_probe_contract": libreoffice_probe_contract,
            "libreoffice_detected_but_unusable": libreoffice_detected_but_unusable,
            "host_access_required": False,
            "host_access_guidance": None,
            "host_access_reasons": [],
            "host_execution": host_execution,
        }

    missing_components: list[str] = []
    if libreoffice_required and not libreoffice_ready:
        missing_components.append("LibreOffice")
    if not missing_components:
        detail = "Native Codex machine baseline is ready."
        if libreoffice_required and libreoffice_ready and not brew_ready:
            detail = (
                "Native Codex machine baseline is ready for the current corpus. LibreOffice is "
                "installed, and Homebrew is optional."
            )
        elif not libreoffice_required:
            detail = (
                "Native Codex machine baseline is ready. LibreOffice is optional until Office "
                "sources are present."
            )
        return {
            "applicable": True,
            "ready": True,
            "status": "ready",
            "detail": detail,
            "brew_ready": brew_ready,
            "brew_binary": brew_binary,
            "libreoffice_required": libreoffice_required,
            "libreoffice_ready": libreoffice_ready,
            "libreoffice_binary": libreoffice_binary,
            "libreoffice_candidate_binary": libreoffice_candidate_binary,
            "libreoffice_validation_detail": libreoffice_validation_detail,
            "libreoffice_probe_contract": libreoffice_probe_contract,
            "libreoffice_detected_but_unusable": libreoffice_detected_but_unusable,
            "host_access_required": False,
            "host_access_guidance": None,
            "host_access_reasons": [],
            "host_execution": host_execution,
        }

    missing_detail = ", ".join(missing_components)
    if libreoffice_detected_but_unusable:
        candidate_detail = (
            f" at `{libreoffice_candidate_binary}`"
            if isinstance(libreoffice_candidate_binary, str) and libreoffice_candidate_binary
            else ""
        )
        validation_suffix = (
            f" Validation detail: {libreoffice_validation_detail}"
            if isinstance(libreoffice_validation_detail, str) and libreoffice_validation_detail
            else ""
        )
        baseline_gap_detail = (
            "Native Codex machine baseline detected LibreOffice"
            f"{candidate_detail}, but it is not currently usable for the current Office corpus."
            f"{validation_suffix}"
        )
        host_access_reason = (
            "Native Codex machine baseline detected LibreOffice, but it is not currently "
            "usable for the current Office corpus and needs machine-level repair."
        )
    else:
        baseline_gap_detail = (
            "Native Codex machine baseline is missing "
            f"{missing_detail} for the current Office corpus."
        )
        host_access_reason = (
            "Native Codex machine baseline is missing "
            f"{missing_detail} for the current Office corpus and needs machine-level "
            "installation."
        )
    full_machine_access = bool(host_execution.get("full_machine_access"))
    permission_mode = str(host_execution.get("permission_mode") or "")
    if not full_machine_access:
        guidance = _codex_full_access_guidance()
        status = "host-access-upgrade-required"
        if permission_mode == "default-permissions":
            detail = (
                f"{baseline_gap_detail} The current thread is still in `Default permissions`."
            )
        else:
            detail = f"{baseline_gap_detail} The current turn does not expose `Full access` yet."
        host_access_required = True
        host_access_reasons = [host_access_reason]
    else:
        guidance = _native_machine_baseline_install_guidance()
        status = "install-required"
        detail = baseline_gap_detail
        host_access_required = False
        host_access_reasons = []

    return {
        "applicable": True,
        "ready": False,
        "status": status,
        "detail": detail,
        "brew_ready": brew_ready,
        "brew_binary": brew_binary,
        "libreoffice_required": libreoffice_required,
        "libreoffice_ready": libreoffice_ready,
        "libreoffice_binary": libreoffice_binary,
        "libreoffice_candidate_binary": libreoffice_candidate_binary,
        "libreoffice_validation_detail": libreoffice_validation_detail,
        "libreoffice_probe_contract": libreoffice_probe_contract,
        "libreoffice_detected_but_unusable": libreoffice_detected_but_unusable,
        "host_access_required": host_access_required,
        "host_access_guidance": guidance,
        "host_access_reasons": host_access_reasons,
        "host_execution": host_execution,
    }


def pdf_renderer_next_step() -> str:
    """Return the preferred next-step guidance for the PDF extraction stack."""
    return (
        'Run `.venv/bin/python -m pip install -e ".[dev]"` from the workspace root, or '
        "install `PyMuPDF`, `pypdfium2`, `pypdf`, and `pillow` into the repo-local `.venv`, "
        "then rerun `docmason doctor`."
    )


def manual_workspace_recovery_step() -> str:
    """Return the canonical deeper fallback reference for manual bootstrap or repair."""
    return (
        f"Follow `{manual_workspace_recovery_doc()}` for the manual workspace bootstrap and "
        "repair fallback."
    )


def bootstrap_checked_at() -> str:
    """Return the current UTC timestamp for bootstrap-marker writes."""
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def write_bootstrap_ready_marker(
    workspace: WorkspacePaths,
    *,
    status: str,
    package_manager: str,
    bootstrap_python: str,
    bootstrap_source: str,
    editable_install: bool,
    editable_detail: str,
    office_snapshot: dict[str, Any],
    machine_baseline: dict[str, Any],
    pdf_snapshot: dict[str, Any],
    uv_bootstrap_mode: str | None = None,
    last_repair_at: str | None = None,
) -> None:
    """Persist the lightweight cached ready marker used by ordinary ask flows."""
    requirements = source_runtime_requirements(workspace)
    toolchain = inspect_toolchain(
        workspace,
        editable_install=editable_install,
    )
    managed_python_executable = (
        str(toolchain.get("managed_python_executable"))
        if isinstance(toolchain.get("managed_python_executable"), str)
        and toolchain.get("managed_python_executable")
        else bootstrap_python
    )
    workspace_runtime_ready = bool(
        editable_install and toolchain.get("isolation_grade") == "self-contained"
    )
    machine_baseline_ready = bool(machine_baseline.get("ready", True))
    state = {
        "schema_version": BOOTSTRAP_STATE_SCHEMA_VERSION,
        "status": status,
        "environment_ready": workspace_runtime_ready and machine_baseline_ready,
        "workspace_runtime_ready": workspace_runtime_ready,
        "machine_baseline_ready": machine_baseline_ready,
        "machine_baseline_status": machine_baseline.get("status"),
        "checked_at": bootstrap_checked_at(),
        "prepared_at": (
            isoformat_timestamp(workspace.venv_python.stat().st_mtime)
            if workspace.venv_python.exists()
            else None
        ),
        "workspace_root": str(workspace.root.resolve()),
        "package_manager": package_manager,
        "bootstrap_source": bootstrap_source,
        "python_executable": managed_python_executable,
        "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
        "editable_install": editable_install,
        "editable_install_detail": editable_detail,
        "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
        "toolchain_root": str(workspace.toolchain_dir.relative_to(workspace.root)),
        "toolchain_mode": toolchain.get("toolchain_mode"),
        "managed_python_executable": toolchain.get("managed_python_executable"),
        "managed_python_version": toolchain.get("managed_python_version"),
        "managed_python_origin": toolchain.get("managed_python_origin"),
        "venv_base_executable": toolchain.get("venv_base_executable"),
        "venv_health": toolchain.get("venv_health"),
        "entrypoint_health": toolchain.get("entrypoint_health"),
        "uv_bootstrap_mode": uv_bootstrap_mode,
        "uv_cache_dir": str(workspace.toolchain_uv_cache_dir.relative_to(workspace.root)),
        "pip_cache_dir": str(workspace.toolchain_pip_cache_dir.relative_to(workspace.root)),
        "isolation_grade": toolchain.get("isolation_grade"),
        "shared_host_dependency": toolchain.get("shared_host_dependency"),
        "shared_host_dependencies": toolchain.get("shared_host_dependencies"),
        "repair_recommended": toolchain.get("repair_recommended"),
        "repair_reason": toolchain.get("repair_reason"),
        "last_repair_at": last_repair_at,
        "host_access_required": bool(machine_baseline.get("host_access_required")),
        "host_access_guidance": machine_baseline.get("host_access_guidance"),
        "host_access_reasons": list(machine_baseline.get("host_access_reasons") or []),
        "libreoffice_executable": office_snapshot.get("binary"),
        "office_probe_contract": office_snapshot.get("probe_contract"),
        "libreoffice_candidate_binary": office_snapshot.get("candidate_binary"),
        "libreoffice_validation_detail": office_snapshot.get("validation_detail"),
        "libreoffice_detected_but_unusable": bool(
            office_snapshot.get("detected_but_unusable")
        ),
        "libreoffice_validation_launcher": office_snapshot.get("validation_launcher"),
        "libreoffice_origin": (
            "system-discovery"
            if office_snapshot.get("binary") or office_snapshot.get("candidate_binary")
            else None
        ),
        "homebrew_binary": machine_baseline.get("brew_binary"),
        "homebrew_ready": bool(machine_baseline.get("brew_ready")),
        "pdf_renderer_ready": bool(pdf_snapshot.get("ready", False)),
        "office_renderer_ready": bool(office_snapshot.get("ready", False)),
        "office_renderer_required": bool(office_snapshot.get("required", False)),
        "requires_pdf_renderer": requirements["requires_pdf_renderer"],
        "requires_office_renderer": requirements["requires_office_renderer"],
        "manual_recovery_doc": manual_workspace_recovery_doc(),
    }
    write_json(workspace.bootstrap_state_path, state)


def environment_snapshot(
    paths: WorkspacePaths,
    *,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
) -> dict[str, Any]:
    """Summarize repo-local environment readiness for status and doctor flows."""
    from .conversation import current_host_execution_context

    editable_install, editable_detail = editable_install_probe(paths)
    state = bootstrap_state(paths)
    cached = cached_bootstrap_readiness(paths)
    toolchain = inspect_toolchain(
        paths,
        bootstrap_state=state,
        editable_install=editable_install,
    )
    host_execution = current_host_execution_context()
    workspace_runtime_ready = bool(
        state.get(
            "workspace_runtime_ready",
            editable_install and toolchain.get("isolation_grade") == "self-contained",
        )
    )
    live_machine_baseline = _machine_baseline_snapshot(
        paths,
        host_execution=host_execution,
    )
    host_access = _prepare_host_access_snapshot(
        paths,
        host_execution=host_execution,
        machine_baseline=live_machine_baseline,
        workspace_runtime_ready=workspace_runtime_ready,
    )
    return {
        "ready": bool(cached.get("ready")),
        "workspace_runtime_ready": workspace_runtime_ready,
        "machine_baseline_ready": bool(live_machine_baseline.get("ready")),
        "machine_baseline_status": live_machine_baseline.get("status"),
        "bootstrap_source": state.get("bootstrap_source"),
        "host_access_required": bool(host_access.get("host_access_required")),
        "host_access_guidance": host_access.get("host_access_guidance"),
        "host_access_reasons": list(host_access.get("host_access_reasons") or []),
        "host_execution": host_execution,
        "workspace_write_network_access": host_access.get("workspace_write_network_access"),
        "sandbox_writable_roots": list(host_access.get("sandbox_writable_roots") or []),
        "venv_python": str(paths.venv_python.relative_to(paths.root)),
        "editable_install": editable_install,
        "editable_install_detail": editable_detail,
        "bootstrap_state_present": bool(state),
        "package_manager": state.get("package_manager"),
        "prepared_at": state.get("prepared_at"),
        "python_baseline": toolchain.get("python_baseline"),
        "toolchain_mode": toolchain.get("toolchain_mode"),
        "isolation_grade": toolchain.get("isolation_grade"),
        "managed_python_healthy": toolchain.get("managed_python_healthy"),
        "venv_healthy": toolchain.get("venv_healthy"),
        "entrypoint_health": toolchain.get("entrypoint_health"),
        "shared_host_dependency": toolchain.get("shared_host_dependency"),
        "shared_host_dependencies": toolchain.get("shared_host_dependencies"),
        "repair_required": toolchain.get("repair_required"),
        "repair_recommended": toolchain.get("repair_recommended"),
        "repair_reason": toolchain.get("repair_reason"),
        "repair_intrusion_class": toolchain.get("repair_intrusion_class"),
        "manual_recovery_doc": manual_workspace_recovery_doc(),
        "cached_ready": bool(cached.get("ready")),
        "cached_ready_reason": cached.get("reason"),
        "cached_ready_detail": cached.get("detail"),
        "toolchain": toolchain,
        "bootstrap_state": bootstrap_state_summary(paths),
        "machine_baseline": live_machine_baseline,
    }


def source_document_tier_counts(source_counts: dict[str, int]) -> dict[str, dict[str, Any]]:
    """Group source-document counts by the public support tiers."""
    grouped: dict[str, dict[str, Any]] = {}
    for tier_name, extensions in supported_input_tiers().items():
        grouped[tier_name] = {
            "extensions": list(extensions),
            "counts": {extension: int(source_counts.get(extension, 0)) for extension in extensions},
            "total": sum(int(source_counts.get(extension, 0)) for extension in extensions),
        }
    return grouped


def _release_entry_status_line(snapshot: dict[str, Any]) -> str:
    """Render one concise human-readable release-entry summary line."""
    if not snapshot:
        return "Release entry: unavailable"
    if not snapshot.get("bundle_detected"):
        return "Release entry: disabled (source-repo)"
    state = "enabled" if snapshot.get("effective_enabled") else "disabled"
    parts = [f"Release entry: {state}"]
    disabled_reason = snapshot.get("disabled_reason")
    if isinstance(disabled_reason, str) and disabled_reason:
        parts.append(f"reason={disabled_reason}")
    channel = snapshot.get("distribution_channel")
    if isinstance(channel, str) and channel:
        parts.append(f"channel={channel}")
    current_version = snapshot.get("current_version")
    if isinstance(current_version, str) and current_version:
        parts.append(f"current={current_version}")
    latest_version = snapshot.get("last_known_latest_version")
    if isinstance(latest_version, str) and latest_version:
        parts.append(f"latest={latest_version}")
    next_eligible_at = snapshot.get("next_eligible_at")
    if isinstance(next_eligible_at, str) and next_eligible_at:
        parts.append(f"next-eligible={next_eligible_at}")
    if snapshot.get("update_available"):
        parts.append("update-available=yes")
    return ", ".join(parts)


def workspace_stage(
    paths: WorkspacePaths,
    *,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
) -> tuple[str, bool, dict[str, Any], dict[str, Any], dict[str, Any], list[str]]:
    """Compute the current workspace stage and the next obvious operator actions."""
    environment = environment_snapshot(paths, editable_install_probe=editable_install_probe)
    source_counts = count_source_documents(paths)
    source_total = sum(source_counts.values())
    source_tiers = source_document_tier_counts(source_counts)
    kb = knowledge_base_snapshot(paths)
    adapters = adapter_snapshot(paths)
    interaction = _interaction_ingest_snapshot(paths)
    control_plane = workspace_state_snapshot(paths)
    release_entry = release_entry_snapshot(paths)
    claude = adapters["claude"]
    active_confirmation_jobs = [
        job
        for job in control_plane.get("active_answer_critical_jobs", [])
        if isinstance(job, dict) and job.get("status") == "awaiting-confirmation"
    ]
    host_access_upgrade_pending = any(
        job.get("job_family") == "prepare" and job.get("confirmation_kind") == "host-access-upgrade"
        for job in active_confirmation_jobs
    )

    if active_confirmation_jobs:
        stage = "control-plane-pending-confirmation"
    elif kb["stale"]:
        stage = "knowledge-base-stale"
    elif kb["staging_present"] and kb["validation_status"] in {
        "blocking-errors",
        "pending-synthesis",
    }:
        stage = "knowledge-base-invalid"
    elif kb["present"]:
        stage = "knowledge-base-present"
    elif environment["ready"] and claude["present"] and not claude["stale"]:
        stage = "adapter-ready"
    elif environment["ready"]:
        stage = "workspace-bootstrapped"
    else:
        stage = "foundation-only"

    pending_actions: list[str] = []
    if not environment["ready"] and not host_access_upgrade_pending:
        if environment.get("host_access_required"):
            pending_actions.append("switch-host-to-full-access")
        else:
            pending_actions.append("prepare")
    if active_confirmation_jobs:
        primary_job = active_confirmation_jobs[0]
        if primary_job.get("job_family") == "prepare":
            if primary_job.get("confirmation_kind") == "host-access-upgrade":
                pending_actions.append("switch-host-to-full-access")
            else:
                pending_actions.append("prepare --yes")
        elif primary_job.get("job_family") == "sync":
            pending_actions.append("sync --yes")
    if source_total > 0 and (not kb["present"] or kb["stale"] or stage == "knowledge-base-invalid"):
        pending_actions.append("sync")
    if kb["staging_present"] and kb["validation_status"] in {"blocking-errors", "warnings"}:
        pending_actions.append("validate-kb")
    if interaction["sync_recommended"]:
        pending_actions.append("sync")

    payload = {
        "stage": stage,
        "environment_ready": environment["ready"],
        "environment": environment,
        "bootstrap_state": dict(environment["bootstrap_state"]),
        "source_documents": {
            "path": str(paths.source_dir.relative_to(paths.root)),
            "counts": source_counts,
            "tiers": source_tiers,
            "total": source_total,
        },
        "knowledge_base": kb,
        "interaction_ingest": interaction,
        "control_plane": control_plane,
        "adapters": adapters,
        "release_entry": release_entry,
        "pending_actions": deduplicate(pending_actions),
    }
    return stage, environment["ready"], payload, environment, kb, deduplicate(pending_actions)


def prepare_workspace(
    paths: WorkspacePaths | None = None,
    *,
    assume_yes: bool = False,
    command_runner: CommandRunner = default_runner,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
    prompt: Callable[[str], str] = input,
    interactive: bool | None = None,
    owner: dict[str, Any] | None = None,
    run_id: str | None = None,
    progress_stream: TextIO | None = None,
) -> CommandReport:
    """Bootstrap repo-local state and install DocMason into the workspace environment."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=True)
    if command_context["state"] == "blocked":
        return _mutating_command_coordination_report(
            command_name="Prepare status",
            status_field="prepare_status",
            coordination=command_context["coordination"],
            environment=environment_snapshot(
                workspace,
                editable_install_probe=editable_install_probe,
            ),
        )
    actions_performed: list[str] = []
    actions_skipped: list[str] = []
    next_steps: list[str] = []
    manual_recovery_next_step = manual_workspace_recovery_step()
    initial_toolchain = inspect_toolchain(
        workspace,
        bootstrap_state=bootstrap_state(workspace),
    )
    bootstrap_source = _resolve_bootstrap_source(workspace)

    if not platform_supported():
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {"platform": sys.platform},
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [
                "Use macOS or Linux for the supported DocMason workflow.",
                manual_recovery_next_step,
            ],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            f"Unsupported platform: {sys.platform}",
            "Next step: use macOS or Linux for DocMason.",
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    if not python_supported():
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {"python_version": ".".join(str(part) for part in sys.version_info[:3])},
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [
                "Install Python 3.11 or newer and rerun `docmason prepare`.",
                manual_recovery_next_step,
            ],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            (
                f"Python {sys.version_info.major}.{sys.version_info.minor} "
                "is below the supported minimum."
            ),
            "Next step: install Python 3.11 or newer and rerun the command.",
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    for directory in (
        workspace.source_dir,
        workspace.knowledge_base_dir,
        workspace.runtime_dir,
        workspace.agent_work_dir,
        workspace.adapters_dir,
        workspace.interaction_ingest_dir,
        workspace.interaction_entries_dir,
        workspace.interaction_attachments_dir,
        workspace.interaction_overlay_dir,
        workspace.interaction_connectors_dir,
    ):
        if directory.exists():
            actions_skipped.append(f"{directory.relative_to(workspace.root)} already exists.")
        else:
            directory.mkdir(parents=True, exist_ok=True)
            actions_performed.append(f"Created {directory.relative_to(workspace.root)}.")

    for created in _ensure_repo_local_toolchain_dirs(workspace):
        actions_performed.append(f"Created {created}.")

    bootstrap_python = str(Path(sys.executable).resolve())
    package_manager = "uv"
    status = READY
    effective_owner = owner or _unique_command_owner("prepare")
    uv_binary = find_uv_binary(workspace)
    uv_bootstrap_mode = uv_binary_mode(workspace, uv_binary)
    uv_install_command, uv_install_display = preferred_uv_install_command(bootstrap_python)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    from .conversation import current_host_execution_context

    host_execution = current_host_execution_context()
    office_snapshot = office_renderer_snapshot(workspace)
    machine_baseline = _machine_baseline_snapshot(
        workspace,
        office_snapshot=office_snapshot,
        host_execution=host_execution,
    )
    workspace_runtime_ready = bool(initial_toolchain.get("isolation_grade") == "self-contained")
    prepare_host_access = _prepare_host_access_snapshot(
        workspace,
        host_execution=host_execution,
        machine_baseline=machine_baseline,
        workspace_runtime_ready=workspace_runtime_ready,
    )
    if prepare_host_access["host_access_required"]:
        host_access_signature = _stable_json_digest(
            {
                "reasons": prepare_host_access["host_access_reasons"],
                "provider": host_execution.get("host_provider"),
                "permission_mode": host_execution.get("permission_mode"),
            }
        )
        next_command = (
            "Switch Codex to `Full access`, then continue the same task."
            if str(host_execution.get("host_provider") or "") == "codex"
            else "Enable the higher-access host mode, then continue the same task."
        )
        prepare_job_info = ensure_shared_job(
            workspace,
            job_key=(
                "prepare:"
                f"{workspace.root}:"
                "host-access-upgrade:"
                f"{host_access_signature}"
            ),
            job_family="prepare",
            criticality="answer-critical",
            scope={
                "workspace_root": str(workspace.root),
                "intrusion_class": "host-access-upgrade",
                "required_capabilities": ["host-access-upgrade"],
                "host_access_reasons": list(prepare_host_access["host_access_reasons"]),
            },
            input_signature=host_access_signature,
            owner=effective_owner,
            run_id=run_id,
            requires_confirmation=True,
            confirmation_kind="host-access-upgrade",
            confirmation_prompt=str(prepare_host_access["host_access_guidance"]),
            confirmation_reason="; ".join(prepare_host_access["host_access_reasons"]),
        )
        host_access_shared_job = prepare_job_info["manifest"]
        host_access_confirmation_payload: dict[str, Any] = {
            "status": ACTION_REQUIRED,
            "prepare_status": "awaiting-confirmation",
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "environment": {
                "python_executable": bootstrap_python,
                "python_version": ".".join(str(part) for part in sys.version_info[:3]),
                "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                "package_manager": package_manager,
                "machine_baseline": machine_baseline,
                "host_execution": host_execution,
            },
            "control_plane": shared_job_control_plane_payload(
                host_access_shared_job,
                next_command=next_command,
            ),
            "workspace_runtime_ready": workspace_runtime_ready,
            "machine_baseline_ready": bool(machine_baseline.get("ready")),
            "machine_baseline_status": machine_baseline.get("status"),
            "bootstrap_source": bootstrap_source,
            "host_execution": host_execution,
            "workspace_write_network_access": prepare_host_access.get(
                "workspace_write_network_access"
            ),
            "sandbox_writable_roots": list(
                prepare_host_access.get("sandbox_writable_roots") or []
            ),
            "host_access_required": True,
            "host_access_guidance": prepare_host_access.get("host_access_guidance"),
            "host_access_reasons": list(prepare_host_access["host_access_reasons"]),
            "next_steps": [next_command],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            str(prepare_host_access["host_access_guidance"]),
            f"Next step: {next_command}",
        ]
        return make_report(ACTION_REQUIRED, host_access_confirmation_payload, lines)

    if uv_binary is None:
        should_attempt_uv_install = bool(assume_yes)
        if not should_attempt_uv_install and interactive:
            answer = prompt(
                "uv is not installed. Install it with "
                f"{uv_install_display} before continuing? [y/N]: "
            )
            should_attempt_uv_install = answer.strip().lower() in {"y", "yes"}
        if not should_attempt_uv_install:
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": {
                    "package_manager": "uv",
                    "python_executable": bootstrap_python,
                    "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
                },
                "manual_recovery_doc": manual_workspace_recovery_doc(),
                "next_steps": [
                    (
                        "Run `docmason prepare --yes` to let the workspace create "
                        "a repo-local bootstrap helper and install uv."
                    ),
                    manual_recovery_next_step,
                ],
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                "uv is required to provision the repo-local managed Python 3.13 toolchain.",
                (
                    "Next step: run `docmason prepare --yes` to let the workspace "
                    "create a repo-local bootstrap helper and install uv."
                ),
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        uv_binary, bootstrap_actions, bootstrap_skips = _install_uv_into_bootstrap_venv(
            workspace,
            bootstrap_python=bootstrap_python,
            command_runner=command_runner,
        )
        actions_performed.extend(bootstrap_actions)
        actions_skipped.extend(bootstrap_skips)
        uv_bootstrap_mode = "bootstrap-venv-installed" if uv_binary is not None else None
        if uv_binary is None:
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": {
                    "package_manager": package_manager,
                    "python_executable": bootstrap_python,
                    "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
                },
                "manual_recovery_doc": manual_workspace_recovery_doc(),
                "next_steps": [
                    (
                        f"Install uv with {uv_install_display} or repair the "
                        "bootstrap helper venv, then rerun `docmason prepare`."
                    ),
                    manual_recovery_next_step,
                ],
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                "The workspace could not provision a repo-local uv bootstrap helper.",
                (
                    "Next step: install uv manually or repair the bootstrap helper "
                    "venv, then rerun `docmason prepare`."
                ),
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
    _emit_prepare_progress(progress_stream, "provisioning repo-local managed Python 3.13...")
    managed_python, managed_error = _provision_managed_python(
        workspace,
        uv_binary=uv_binary,
        command_runner=command_runner,
    )
    if managed_error is not None or managed_python is None:
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
                "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
                "toolchain_mode": "missing",
            },
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [
                managed_error
                or "Provision the repo-local managed Python 3.13 toolchain and retry.",
                manual_recovery_next_step,
            ],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            managed_error
            or "The repo-local managed Python 3.13 toolchain could not be provisioned.",
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    actions_performed.append(
        "Provisioned repo-local managed Python 3.13 under `.docmason/toolchain/python`."
    )

    _emit_prepare_progress(progress_stream, "rebuilding the repo-local `.venv`...")
    venv_error = _rebuild_repo_local_venv(
        workspace,
        uv_binary=uv_binary,
        managed_python=managed_python,
        command_runner=command_runner,
    )
    if venv_error is not None:
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {
                "package_manager": package_manager,
                "python_executable": str(managed_python),
                "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
            },
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [venv_error, manual_recovery_next_step],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            venv_error,
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    actions_performed.append("Rebuilt `.venv` against the repo-local managed Python 3.13 baseline.")

    _emit_prepare_progress(progress_stream, "installing DocMason into the repo-local `.venv`...")
    install_error = _install_workspace_into_repo_local_venv(
        workspace,
        uv_binary=uv_binary,
        command_runner=command_runner,
    )
    if install_error is not None:
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {
                "package_manager": package_manager,
                "python_executable": str(managed_python),
                "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
            },
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [install_error, manual_recovery_next_step],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            install_error,
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    actions_performed.append(
        "Installed DocMason in editable mode with dev dependencies into the "
        "repo-local `.venv` via uv."
    )

    entrypoint_probe, recovered_after_retry = _inspect_prepare_entrypoint(workspace)
    if recovered_after_retry:
        actions_performed.append(
            "Retried the repo-local DocMason entrypoint startup probe after a transient "
            "`startup-silent` result."
        )
    entrypoint_health = str(entrypoint_probe.get("entrypoint_health") or "module-import-failed")
    if entrypoint_health != "ready":
        startup_reason = str(
            entrypoint_probe.get("detail")
            or "The repo-local DocMason entrypoint is not healthy."
        )
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {
                "package_manager": package_manager,
                "python_executable": str(managed_python),
                "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                "entrypoint_health": entrypoint_health,
            },
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [startup_reason, manual_recovery_next_step],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            startup_reason,
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    editable_install, editable_detail = editable_install_probe(workspace)
    if not editable_install:
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
                "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                "editable_install": editable_install,
                "editable_install_detail": editable_detail,
            },
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [
                "Repair the editable install inside .venv and rerun `docmason prepare`.",
                manual_recovery_next_step,
            ],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            editable_detail,
            "Next step: repair the editable install inside .venv and rerun the command.",
            manual_recovery_next_step,
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    try:
        generated_skill_shims = sync_repo_local_skill_shims(workspace)
    except ValueError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "actions_performed": actions_performed,
            "actions_skipped": actions_skipped,
            "environment": {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
                "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                "editable_install": editable_install,
                "editable_install_detail": editable_detail,
            },
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "next_steps": [
                str(exc),
                "Resolve the skill shim generation issue and rerun `docmason prepare`.",
            ],
        }
        lines = [
            f"Prepare status: {ACTION_REQUIRED}",
            str(exc),
            "Next step: resolve the skill shim generation issue and rerun the command.",
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    if generated_skill_shims:
        actions_performed.append(
            "Refreshed repo-local skill shims under .agents/skills and .claude/skills."
        )

    prepare_shared_job: dict[str, Any] = {}
    machine_baseline = _machine_baseline_snapshot(
        workspace,
        office_snapshot=office_snapshot,
        host_execution=host_execution,
    )
    prepare_requirements = required_prepare_capabilities(
        workspace,
        editable_install=editable_install,
        editable_detail=editable_detail,
        office_snapshot=office_snapshot,
        machine_baseline=machine_baseline,
    )
    if prepare_requirements["high_intrusion_required"]:
        confirmation_kind = (
            "host-access-upgrade"
            if prepare_requirements.get("host_access_upgrade_required")
            else str(prepare_requirements.get("confirmation_kind") or "high-intrusion-prepare")
        )
        confirmation_prompt = (
            str(machine_baseline.get("host_access_guidance"))
            if confirmation_kind == "host-access-upgrade"
            else (
                "This question requires additional local dependencies before it can continue "
                "safely. Prepare the workspace now?"
            )
        )
        next_command = (
            "Switch Codex to `Full access`, then continue the same task."
            if confirmation_kind == "host-access-upgrade"
            else "docmason prepare --yes"
        )
        requires_confirmation = bool(
            prepare_requirements.get("host_access_upgrade_required")
        ) or not assume_yes
        prepare_job_info = ensure_shared_job(
            workspace,
            job_key=(
                "prepare:"
                f"{workspace.root}:"
                f"{prepare_requirements['intrusion_class']}:"
                f"{prepare_requirements['required_capability_signature']}"
            ),
            job_family="prepare",
            criticality="answer-critical",
            scope={
                "workspace_root": str(workspace.root),
                "intrusion_class": prepare_requirements["intrusion_class"],
                "required_capabilities": prepare_requirements["required_capabilities"],
            },
            input_signature=prepare_requirements["required_capability_signature"],
            owner=effective_owner,
            run_id=run_id,
            requires_confirmation=requires_confirmation,
            confirmation_kind=confirmation_kind,
            confirmation_prompt=confirmation_prompt,
            confirmation_reason="; ".join(prepare_requirements["reasons"]),
        )
        prepare_shared_job = prepare_job_info["manifest"]
        caller_role = str(prepare_job_info.get("caller_role") or "owner")
        if prepare_shared_job.get("status") == "awaiting-confirmation" and (
            not assume_yes or prepare_requirements.get("host_access_upgrade_required")
        ):
            confirmation_payload: dict[str, Any] = {
                "status": ACTION_REQUIRED,
                "prepare_status": "awaiting-confirmation",
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "manual_recovery_doc": manual_workspace_recovery_doc(),
                "environment": {
                    "python_executable": bootstrap_python,
                    "python_version": ".".join(str(part) for part in sys.version_info[:3]),
                    "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                    "package_manager": package_manager,
                    "machine_baseline": machine_baseline,
                    "host_execution": host_execution,
                },
                "control_plane": shared_job_control_plane_payload(
                    prepare_shared_job,
                    next_command=next_command,
                ),
                "workspace_runtime_ready": bool(
                    editable_install
                    and initial_toolchain.get("isolation_grade") == "self-contained"
                ),
                "machine_baseline_ready": bool(machine_baseline.get("ready")),
                "machine_baseline_status": machine_baseline.get("status"),
                "bootstrap_source": bootstrap_source,
                "host_execution": host_execution,
                "workspace_write_network_access": _host_execution_network_access(
                    host_execution
                ),
                "sandbox_writable_roots": list(
                    host_execution.get("sandbox_writable_roots") or []
                ),
                "host_access_required": bool(machine_baseline.get("host_access_required")),
                "host_access_guidance": machine_baseline.get("host_access_guidance"),
                "host_access_reasons": list(machine_baseline.get("host_access_reasons") or []),
                "next_steps": [
                    (
                        "Switch Codex to `Full access`, then continue the same task."
                        if confirmation_kind == "host-access-upgrade"
                        else "Run `docmason prepare --yes` to approve and continue."
                    )
                ],
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                str(
                    prepare_shared_job.get("confirmation_prompt")
                    or prepare_shared_job.get("confirmation_reason")
                ),
                (
                    "Next step: switch Codex to `Full access`, then continue the same task."
                    if confirmation_kind == "host-access-upgrade"
                    else "Next step: run `docmason prepare --yes` to approve and continue."
                ),
            ]
            return make_report(ACTION_REQUIRED, confirmation_payload, lines)
        if (
            prepare_shared_job.get("status") == "awaiting-confirmation"
            and assume_yes
            and not prepare_requirements.get("host_access_upgrade_required")
        ):
            prepare_shared_job = approve_shared_job(
                workspace,
                str(prepare_shared_job["job_id"]),
                owner=effective_owner,
                run_id=run_id,
            )
            record_run_event_for_runs(
                workspace,
                run_ids=prepare_shared_job.get("attached_run_ids"),
                stage="control-plane",
                event_type="shared-job-approved",
                payload={"job_id": prepare_shared_job.get("job_id")},
            )
        elif caller_role == "waiter" and prepare_shared_job.get("status") == "running":
            payload = {
                "status": DEGRADED,
                "prepare_status": "waiting-shared-job",
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "manual_recovery_doc": manual_workspace_recovery_doc(),
                "environment": {
                    "python_executable": bootstrap_python,
                    "python_version": ".".join(str(part) for part in sys.version_info[:3]),
                    "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
                    "package_manager": package_manager,
                },
                "control_plane": shared_job_control_plane_payload(
                    prepare_shared_job,
                    state="waiting-shared-job",
                ),
                "next_steps": [
                    "Wait for the active shared prepare job to settle, then retry if needed."
                ],
            }
            lines = [
                f"Prepare status: {DEGRADED}",
                "A matching shared prepare job is already running.",
            ]
            return make_report(DEGRADED, payload, lines)
    if (
        bool(machine_baseline.get("applicable"))
        and not bool(machine_baseline.get("ready"))
        and assume_yes
        and bool(office_snapshot.get("required"))
        and not bool(office_snapshot.get("ready"))
    ):
        install_command, install_display = preferred_libreoffice_install_command()
        detected_but_unusable = bool(office_snapshot.get("detected_but_unusable"))
        if detected_but_unusable:
            _emit_prepare_progress(
                progress_stream,
                "repairing LibreOffice via the official macOS package...",
            )
            installed, detail = _install_libreoffice_from_official_macos_package(
                workspace,
                command_runner=command_runner,
            )
            if installed:
                actions_performed.append(detail)
                office_snapshot = office_renderer_snapshot(workspace)
                if not bool(office_snapshot.get("ready")):
                    actions_skipped.append(
                        "LibreOffice repair completed, but the renderer is still not usable. "
                        f"Details: {office_snapshot.get('validation_detail') or office_snapshot.get('detail')}"
                    )
            else:
                actions_skipped.append(detail)
                next_steps.append(
                    "Official LibreOffice auto-repair failed. Reinstall LibreOffice from "
                    f"{_LIBREOFFICE_DOWNLOAD_PAGE} and rerun `docmason prepare --yes`."
                )
        elif install_command is not None and install_display is not None:
            _emit_prepare_progress(progress_stream, "installing LibreOffice via Homebrew...")
            execution = command_runner(install_command, workspace.root)
            if execution.exit_code == 0:
                actions_performed.append(f"Installed LibreOffice with {install_display}.")
                office_snapshot = office_renderer_snapshot(workspace)
            else:
                actions_skipped.append(
                    "LibreOffice installation via Homebrew failed during prepare. Details: "
                    f"{execution.stderr or execution.stdout or 'no output'}"
                )
                next_steps.append(
                    "Install LibreOffice from "
                    f"{_LIBREOFFICE_DOWNLOAD_PAGE} or repair the Homebrew install, then rerun "
                    "`docmason prepare --yes`."
                )
        else:
            _emit_prepare_progress(
                progress_stream,
                "installing LibreOffice via the official macOS package...",
            )
            installed, detail = _install_libreoffice_from_official_macos_package(
                workspace,
                command_runner=command_runner,
            )
            if installed:
                actions_performed.append(detail)
                office_snapshot = office_renderer_snapshot(workspace)
            else:
                actions_skipped.append(detail)
                next_steps.append(
                    "Official LibreOffice auto-install failed. Install LibreOffice from "
                    f"{_LIBREOFFICE_DOWNLOAD_PAGE} and rerun `docmason prepare --yes`."
                )
        machine_baseline = _machine_baseline_snapshot(
            workspace,
            office_snapshot=office_snapshot,
            host_execution=host_execution,
        )
    office_renderer_gap = bool(office_snapshot["required"]) and not bool(
        office_snapshot["ready"]
    ) and not bool(machine_baseline.get("applicable"))
    if bool(machine_baseline.get("applicable")) and not bool(machine_baseline.get("ready")):
        status = DEGRADED
        next_steps.append(
            str(machine_baseline.get("host_access_guidance"))
            or _native_machine_baseline_install_guidance()
        )
    elif office_renderer_gap:
        status = DEGRADED
        next_steps.append(office_renderer_next_step())

    pdf_snapshot = _steady_state_pdf_renderer_snapshot(
        workspace,
        command_runner=command_runner,
    )
    write_bootstrap_ready_marker(
        workspace,
        status=status,
        package_manager=package_manager,
        bootstrap_python=bootstrap_python,
        bootstrap_source=bootstrap_source,
        editable_install=editable_install,
        editable_detail=editable_detail,
        office_snapshot=office_snapshot,
        machine_baseline=machine_baseline,
        pdf_snapshot=pdf_snapshot,
        uv_bootstrap_mode=uv_bootstrap_mode,
        last_repair_at=bootstrap_checked_at(),
    )
    final_toolchain = inspect_toolchain(
        workspace,
        bootstrap_state=bootstrap_state(workspace),
        editable_install=editable_install,
    )
    write_json(
        workspace.toolchain_manifest_path,
        {
            "schema_version": 1,
            "updated_at": bootstrap_checked_at(),
            "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
            "toolchain": final_toolchain,
            "package_manager": package_manager,
            "uv_bootstrap_mode": uv_bootstrap_mode,
        },
    )
    if initial_toolchain.get("repair_required"):
        append_jsonl(
            workspace.toolchain_repair_history_path,
            {
                "recorded_at": bootstrap_checked_at(),
                "previous_repair_reason": initial_toolchain.get("repair_reason"),
                "previous_isolation_grade": initial_toolchain.get("isolation_grade"),
                "previous_toolchain_mode": initial_toolchain.get("toolchain_mode"),
                "current_toolchain": final_toolchain,
            },
        )
    _refresh_generated_connector_manifests(workspace)
    actions_performed.append("Recorded bootstrap state in runtime/bootstrap_state.json.")
    if prepare_shared_job:
        if not bool(machine_baseline.get("ready")):
            prepare_shared_job = block_shared_job(
                workspace,
                str(prepare_shared_job["job_id"]),
                result={"detail": machine_baseline["detail"]},
            )
        elif office_renderer_gap:
            prepare_shared_job = block_shared_job(
                workspace,
                str(prepare_shared_job["job_id"]),
                result={"detail": office_snapshot["detail"]},
            )
        else:
            prepare_shared_job = complete_shared_job(
                workspace,
                str(prepare_shared_job["job_id"]),
                result={"status": status, "detail": "Prepare completed."},
            )
        record_shared_job_settled_once(
            workspace,
            run_ids=prepare_shared_job.get("attached_run_ids"),
            job_id=str(prepare_shared_job.get("job_id") or ""),
            status=str(prepare_shared_job.get("status") or ""),
        )

    prepare_payload: dict[str, Any] = {
        "status": status,
        "prepare_status": status,
        "workspace_runtime_ready": bool(
            editable_install and final_toolchain.get("isolation_grade") == "self-contained"
        ),
        "machine_baseline_ready": bool(machine_baseline.get("ready")),
        "machine_baseline_status": machine_baseline.get("status"),
        "bootstrap_source": bootstrap_source,
        "host_execution": host_execution,
        "workspace_write_network_access": _host_execution_network_access(host_execution),
        "sandbox_writable_roots": list(host_execution.get("sandbox_writable_roots") or []),
        "host_access_required": bool(machine_baseline.get("host_access_required")),
        "host_access_guidance": machine_baseline.get("host_access_guidance"),
        "host_access_reasons": list(machine_baseline.get("host_access_reasons") or []),
        "actions_performed": actions_performed,
        "actions_skipped": actions_skipped,
        "manual_recovery_doc": manual_workspace_recovery_doc(),
        "control_plane": (
            shared_job_control_plane_payload(prepare_shared_job)
            if prepare_shared_job
            else {}
        ),
        "environment": {
            "python_executable": str(managed_python),
            "python_version": final_toolchain.get("managed_python_version"),
            "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
            "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
            "package_manager": package_manager,
            "editable_install": editable_install,
            "editable_install_detail": editable_detail,
            "toolchain": final_toolchain,
            "bootstrap_state": str(workspace.bootstrap_state_path.relative_to(workspace.root)),
            "manual_recovery_doc": manual_workspace_recovery_doc(),
            "machine_baseline": machine_baseline,
            "host_execution": host_execution,
        },
        "next_steps": deduplicate(next_steps),
    }
    lines = [
        f"Prepare status: {status}",
        f"Package workflow: {package_manager}",
        f"Python baseline: {PREPARED_WORKSPACE_PYTHON_BASELINE}",
        f"Virtual environment: {workspace.venv_dir.relative_to(workspace.root)}",
        editable_detail,
    ]
    lines.append(f"Bootstrap source: {bootstrap_source}")
    lines.append(
        "Machine baseline: "
        f"{machine_baseline.get('status', 'unknown')}"
    )
    if not bool(machine_baseline.get("ready")):
        lines.append(str(machine_baseline.get("detail")))
    elif office_renderer_gap:
        lines.append(str(office_snapshot.get("detail")))
    if next_steps:
        lines.append(f"Next steps: {', '.join(deduplicate(next_steps))}")
    return make_report(status, prepare_payload, lines)


def doctor_workspace(
    paths: WorkspacePaths | None = None,
    *,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
) -> CommandReport:
    """Inspect workspace readiness without mutating any repository state."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=False)
    environment = environment_snapshot(workspace, editable_install_probe=editable_install_probe)
    checks: list[dict[str, Any]] = []
    next_steps: list[str] = []

    def add_check(name: str, status: str, detail: str, action: str | None = None) -> None:
        checks.append({"name": name, "status": status, "detail": detail, "action": action})
        if action and status != READY:
            next_steps.append(action)

    if platform_supported():
        add_check("platform", READY, f"Platform {sys.platform} is supported.")
    else:
        add_check(
            "platform",
            ACTION_REQUIRED,
            f"Platform {sys.platform} is not supported for DocMason.",
            "Use macOS or Linux for the supported DocMason workflow.",
        )

    version_string = ".".join(str(part) for part in sys.version_info[:3])
    if python_supported():
        add_check("python", READY, f"Python {version_string} satisfies the minimum requirement.")
    else:
        add_check(
            "python",
            ACTION_REQUIRED,
            f"Python {version_string} is below the supported minimum of 3.11.",
            "Install Python 3.11 or newer and rerun `docmason doctor`.",
        )

    if environment["isolation_grade"] == "self-contained":
        add_check(
            "toolchain",
            READY,
            (
                "Prepared-workspace toolchain is self-contained under "
                f"`{environment['toolchain_mode']}`."
            ),
        )
    elif environment["isolation_grade"] == "mixed":
        add_check(
            "toolchain",
            DEGRADED,
            (
                "Prepared-workspace toolchain is mixed and requires repo-local repair before "
                "ordinary ask can continue safely."
            ),
            (
                "Run `docmason prepare --yes` to rebuild `.venv` against "
                "repo-local managed Python 3.13."
            ),
        )
    else:
        add_check(
            "toolchain",
            ACTION_REQUIRED,
            (
                "Prepared-workspace toolchain is degraded and cannot support ordinary ask until "
                "it is repaired."
            ),
            "Run `docmason prepare --yes` to repair the repo-local toolchain.",
        )

    if environment["entrypoint_health"] == "ready":
        add_check("entrypoint", READY, "The repo-local DocMason entrypoint is healthy.")
    else:
        add_check(
            "entrypoint",
            ACTION_REQUIRED,
            f"The repo-local DocMason entrypoint is `{environment['entrypoint_health']}`.",
            "Run `docmason prepare --yes` to repair the repo-local entrypoint chain.",
        )

    bootstrap_snapshot = bootstrap_state_summary(workspace)
    bootstrap_reason = str(bootstrap_snapshot.get("reason") or "")
    bootstrap_detail = str(bootstrap_snapshot.get("detail") or "Bootstrap state is unavailable.")
    if bootstrap_reason == "cached-ready":
        add_check("bootstrap-state", READY, bootstrap_detail)
    elif bootstrap_reason == "workspace-root-drift":
        add_check(
            "bootstrap-state",
            ACTION_REQUIRED,
            bootstrap_detail,
            (
                "Run `docmason prepare --yes` from the current workspace root "
                "to refresh the cached bootstrap marker."
            ),
        )
    elif bootstrap_reason in {
        "missing-venv",
        "environment-not-ready",
        "legacy-bootstrap-state-sync-capability-unknown",
    }:
        add_check(
            "bootstrap-state",
            ACTION_REQUIRED,
            bootstrap_detail,
            "Run `docmason prepare --yes` to refresh the cached bootstrap marker.",
        )
    elif bootstrap_reason in {"legacy-compatible-ready", "legacy-bootstrap-state"}:
        add_check(
            "bootstrap-state",
            DEGRADED,
            bootstrap_detail,
            (
                "Run `docmason prepare --yes` to refresh the cached bootstrap "
                "marker to the current contract."
            ),
        )
    else:
        add_check(
            "bootstrap-state",
            DEGRADED,
            bootstrap_detail,
            "Run `docmason prepare --yes` to record the current bootstrap marker.",
        )

    if environment["host_access_required"]:
        host_access_detail = "; ".join(
            str(item)
            for item in environment.get("host_access_reasons", [])
            if isinstance(item, str) and item.strip()
        ) or str(environment.get("host_access_guidance") or "Higher host access is required.")
        add_check(
            "host-access",
            ACTION_REQUIRED,
            host_access_detail,
            str(environment.get("host_access_guidance") or ""),
        )

    machine_baseline = (
        dict(environment.get("machine_baseline", {}))
        if isinstance(environment.get("machine_baseline"), dict)
        else _machine_baseline_snapshot(workspace)
    )
    if machine_baseline["status"] == "ready":
        add_check("machine-baseline", READY, machine_baseline["detail"])
    elif machine_baseline["status"] == "host-access-upgrade-required":
        add_check(
            "machine-baseline",
            ACTION_REQUIRED,
            machine_baseline["detail"],
            str(machine_baseline.get("host_access_guidance")),
        )
    elif machine_baseline["status"] != "not-applicable":
        add_check(
            "machine-baseline",
            ACTION_REQUIRED,
            machine_baseline["detail"],
            "Run `docmason prepare --yes` to install or repair the native machine baseline.",
        )

    uv_binary = find_uv_binary(workspace)
    _install_command, uv_install_display = preferred_uv_install_command(str(Path(sys.executable)))
    if uv_binary:
        add_check("uv", READY, f"uv is available at {uv_binary}.")
    else:
        add_check(
            "uv",
            DEGRADED,
            (
                "uv is not installed; `prepare` will use the repo-local "
                "bootstrap helper to install or repair uv before continuing."
            ),
            f"Recommended: install uv with {uv_install_display}, or run "
            "`docmason prepare --yes` to let the workspace attempt that install path.",
        )

    if workspace.venv_python.exists():
        add_check(
            "venv",
            READY,
            f"Virtual environment interpreter exists at {workspace.venv_python}.",
        )
    else:
        add_check(
            "venv",
            ACTION_REQUIRED,
            "Virtual environment has not been created yet.",
            "Run `docmason prepare` to create the repo-local environment.",
        )

    editable_install, editable_detail = editable_install_probe(workspace)
    if editable_install:
        add_check("editable-install", READY, editable_detail)
    else:
        add_check(
            "editable-install",
            ACTION_REQUIRED,
            editable_detail,
            "Run `docmason prepare` to install DocMason in editable mode inside `.venv`.",
        )

    missing_directories = [
        str(path.relative_to(workspace.root))
        for path in (
            workspace.source_dir,
            workspace.knowledge_base_dir,
            workspace.runtime_dir,
            workspace.adapters_dir,
        )
        if not path.exists()
    ]
    if missing_directories:
        add_check(
            "directories",
            DEGRADED,
            f"Missing expected workspace directories: {', '.join(missing_directories)}.",
            "Run `docmason prepare` to create the expected local directories.",
        )
    else:
        add_check("directories", READY, "Expected workspace directories exist.")

    source_documents = supported_source_documents(workspace)
    if source_documents:
        add_check(
            "source-corpus",
            READY,
            f"Found {len(source_documents)} supported source documents under original_doc/.",
        )
    else:
        add_check(
            "source-corpus",
            DEGRADED,
            "No supported source documents were found under original_doc/ yet.",
            (
                "Add supported office/PDF files or supported text-like files such as "
                "Markdown, plain text, or `.eml` to original_doc/ before the sync phase."
            ),
        )

    pdf_snapshot = pdf_renderer_snapshot()
    if any(path.suffix.lower() == ".pdf" for path in source_documents):
        if pdf_snapshot["ready"]:
            add_check("pdf-renderer", READY, pdf_snapshot["detail"])
        else:
            add_check(
                "pdf-renderer",
                ACTION_REQUIRED,
                pdf_snapshot["detail"],
                pdf_renderer_next_step(),
            )
    else:
        add_check("pdf-renderer", READY, pdf_snapshot["detail"])

    office_snapshot = office_renderer_snapshot(workspace)
    office_action = None
    if office_snapshot["required"] and not office_snapshot["ready"]:
        office_action = office_renderer_next_step()
        add_check("office-renderer", ACTION_REQUIRED, office_snapshot["detail"], office_action)
    else:
        add_check("office-renderer", READY, office_snapshot["detail"])

    claude = adapter_snapshot(workspace)["claude"]
    if claude["present"] and not claude["stale"]:
        add_check(
            "claude-adapter",
            READY,
            "Claude adapter memory files are present and fresh.",
        )
    elif claude["present"]:
        add_check(
            "claude-adapter",
            READY,
            "Claude adapter memory files are present but stale relative to canonical sources.",
            "If you plan to use Claude, run `docmason sync-adapters` to refresh the adapter.",
        )
    else:
        add_check(
            "claude-adapter",
            READY,
            (
                "Claude adapter memory files have not been generated yet. This is optional "
                "until that ecosystem is used."
            ),
        )

    if claude["skill_shims_present"]:
        add_check(
            "claude-native-skill-shims",
            READY,
            "Claude native skill shims are present for repo-local slash-command discovery.",
        )
    elif claude["present"]:
        add_check(
            "claude-native-skill-shims",
            DEGRADED,
            (
                "Claude native skill shims are not present. The core Claude "
                "adapter remains usable, "
                "but native slash-command discovery is unavailable."
            ),
            (
                "If you want Claude native slash-command discovery, run "
                "`docmason sync-adapters` to refresh the repo-local skill shims."
            ),
        )
    else:
        add_check(
            "claude-native-skill-shims",
            READY,
            (
                "Claude native skill shims have not been generated yet. This is optional "
                "until that ecosystem is used."
            ),
        )

    # Claude Code hook configuration check.
    claude_code_settings = workspace.root / ".claude" / "settings.json"
    claude_code_hooks_dir = workspace.root / ".claude" / "hooks"
    if claude_code_settings.exists():
        hook_scripts = (
            sorted(claude_code_hooks_dir.glob("on-*.sh")) if claude_code_hooks_dir.exists() else []
        )
        if hook_scripts:
            non_executable = [s.name for s in hook_scripts if not os.access(s, os.X_OK)]
            if non_executable:
                add_check(
                    "claude-code-hooks",
                    DEGRADED,
                    f"Hook scripts are present but not executable: {', '.join(non_executable)}.",
                    f"Run `chmod +x {claude_code_hooks_dir / '*.sh'}` to fix permissions.",
                )
            else:
                add_check(
                    "claude-code-hooks",
                    READY,
                    f"Claude Code hooks are configured with {len(hook_scripts)} scripts.",
                )
        else:
            add_check(
                "claude-code-hooks",
                DEGRADED,
                "Claude Code settings.json exists but no hook scripts were found.",
                "Re-check the .claude/hooks/ directory.",
            )
    else:
        add_check(
            "claude-code-hooks",
            READY,
            (
                "Claude Code hook configuration is not present. This is expected "
                "when Claude Code is not the active agent surface."
            ),
        )

    interaction = _interaction_ingest_snapshot(workspace)
    if interaction["load_warnings"]:
        detail = (
            "Interaction-ingest runtime state was only partially readable during this check, "
            "so pending overlay information may be incomplete."
        )
        if interaction["pending_promotion_count"]:
            detail = (
                f"{detail} {interaction['pending_promotion_count']} pending interaction-derived "
                "items still await sync-time promotion."
            )
        add_check(
            "interaction-ingest",
            DEGRADED,
            detail,
            (
                "Retry the command after active interaction-ingest writes finish. If pending "
                "interaction memory should be promoted, run `docmason sync`."
            ),
        )
    elif interaction["pending_promotion_count"]:
        add_check(
            "interaction-ingest",
            DEGRADED,
            (
                "Interaction-ingest has pending overlay entries, and "
                f"{interaction['pending_promotion_count']} still await sync-time promotion."
            ),
            "Run `docmason sync` when you want pending interaction memory promoted.",
        )
    else:
        add_check(
            "interaction-ingest",
            READY,
            "Interaction-ingest runtime state is available and does not currently require sync.",
        )

    control_plane = workspace_state_snapshot(workspace)
    pending_confirmations = [
        job
        for job in control_plane.get("active_answer_critical_jobs", [])
        if isinstance(job, dict) and job.get("status") == "awaiting-confirmation"
    ]
    if pending_confirmations:
        primary_job = pending_confirmations[0]
        next_step = (
            "Switch Codex to `Full access`, then continue the same task."
            if primary_job.get("confirmation_kind") == "host-access-upgrade"
            else (
                "Run `docmason prepare --yes` to approve and continue."
                if primary_job.get("job_family") == "prepare"
                else "Run `docmason sync --yes` to approve and continue."
            )
        )
        add_check(
            "control-plane",
            ACTION_REQUIRED,
            (
                "A confirmation-required shared control-plane job is blocking safe continuation: "
                f"{primary_job.get('job_family')}."
            ),
            next_step,
        )
    else:
        repair_count = len(control_plane.get("repair_actions", []))
        add_check(
            "control-plane",
            READY,
            (
                "No confirmation-required shared control-plane job is currently "
                "blocking the workspace."
                + (
                    f" Recent auto-repairs={repair_count}."
                    if repair_count
                    else ""
                )
            ),
        )

    knowledge_base = knowledge_base_snapshot(workspace)
    storage_lifecycle = knowledge_base.get("storage_lifecycle", {})
    if isinstance(storage_lifecycle, dict) and storage_lifecycle:
        detail = (
            "Storage lifecycle tracks "
            f"{storage_lifecycle.get('family_count', 0)} artifact families, "
            f"{storage_lifecycle.get('published_root_count', 0)} live published root(s), "
            "and "
            f"{storage_lifecycle.get('publish_ledger_count', 0)} publish ledger record(s)."
        )
        if knowledge_base.get("legacy_archive_detected"):
            detail += (
                " Legacy archive storage is still present and will be compacted on the next "
                "mutating sync."
            )
        add_check(
            "storage-lifecycle",
            READY,
            detail,
        )
    else:
        add_check(
            "storage-lifecycle",
            DEGRADED,
            "Storage lifecycle summary is unavailable.",
            "Inspect `knowledge_base/` state and rerun `docmason doctor`.",
        )

    release_entry = release_entry_snapshot(workspace)
    release_entry_detail = _release_entry_status_line(release_entry)
    if release_entry.get("effective_enabled"):
        add_check("release-entry", READY, release_entry_detail)
    elif (
        release_entry.get("bundle_detected")
        and release_entry.get("disabled_reason") == "bundle-unconfigured"
    ):
        add_check("release-entry", DEGRADED, release_entry_detail)
    else:
        add_check("release-entry", READY, release_entry_detail)

    overall = READY
    if any(check["status"] == ACTION_REQUIRED for check in checks):
        overall = ACTION_REQUIRED
    elif any(check["status"] == DEGRADED for check in checks):
        overall = DEGRADED

    if any(check["name"] == "platform" and check["status"] != READY for check in checks):
        next_steps.append(manual_workspace_recovery_step())

    payload = {
        "status": overall,
        "environment": environment,
        "knowledge_base": knowledge_base,
        "release_entry": release_entry,
        "checks": checks,
        "supported_inputs": list(SUPPORTED_INPUTS),
        "supported_input_tiers": supported_input_tiers(),
        "manual_recovery_doc": manual_workspace_recovery_doc(),
        "next_steps": deduplicate(next_steps),
    }
    lines = [f"Doctor status: {overall}"]
    for check in checks:
        lines.append(f"[{check['status']}] {check['name']}: {check['detail']}")
    if next_steps:
        lines.append(f"Next steps: {', '.join(deduplicate(next_steps))}")
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )
    return make_report(overall, payload, lines)


def status_workspace(
    paths: WorkspacePaths | None = None,
    *,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
) -> CommandReport:
    """Report the current workspace stage and pending operator actions."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=False)
    (
        stage,
        environment_ready,
        payload,
        _environment,
        _knowledge_base,
        pending_actions,
    ) = workspace_stage(workspace, editable_install_probe=editable_install_probe)
    lines = [
        f"Stage: {stage}",
        f"Environment ready: {'yes' if environment_ready else 'no'}",
        f"Toolchain mode: {payload['environment']['toolchain_mode']}",
        f"Isolation grade: {payload['environment']['isolation_grade']}",
        f"Entrypoint health: {payload['environment']['entrypoint_health']}",
        (
            "Machine baseline: "
            f"{payload['environment'].get('machine_baseline_status', 'unknown')}"
        ),
        (
            "Host network access: "
            + (
                "yes"
                if payload["environment"].get("workspace_write_network_access") is True
                else (
                    "no"
                    if payload["environment"].get("workspace_write_network_access") is False
                    else "unknown"
                )
            )
        ),
        (
            "Bootstrap state: "
            + (
                "ready"
                if payload["bootstrap_state"]["cached_ready"]
                else str(payload["bootstrap_state"]["reason"] or "not-recorded")
            )
        ),
        (
            "Source documents: "
            + ", ".join(
                (f"{tier_name}={payload['source_documents']['tiers'][tier_name]['total']}")
                for tier_name in (
                    "office_pdf",
                    "first_class_text",
                    "first_class_email",
                    "lightweight_text",
                )
            )
            + f" (total={payload['source_documents']['total']})"
        ),
        (
            "Knowledge base: "
            + ("present" if payload["knowledge_base"]["present"] else "not present")
            + (
                ", stale"
                if payload["knowledge_base"]["present"] and payload["knowledge_base"]["stale"]
                else ""
            )
        ),
        ("Stale reason: " + str(payload["knowledge_base"].get("stale_reason") or "n/a")),
        f"Validation status: {payload['knowledge_base']['validation_status']}",
        (
            "Staging: "
            + ("present" if payload["knowledge_base"]["staging_present"] else "not present")
        ),
        f"Last sync: {payload['knowledge_base']['last_sync_at'] or 'not yet run'}",
        f"Last publish: {payload['knowledge_base']['last_publish_at'] or 'not yet published'}",
        (
            "Interaction ingest: "
            f"pending-capture={payload['interaction_ingest']['pending_capture_count']}, "
            f"pending-promotion={payload['interaction_ingest']['pending_promotion_count']}"
        ),
        (
            "Claude adapter: "
            + ("present" if payload["adapters"]["claude"]["present"] else "not present")
            + (
                ", stale"
                if payload["adapters"]["claude"]["present"]
                and payload["adapters"]["claude"]["stale"]
                else ""
            )
        ),
        (
            "Control plane: "
            + str(
                len(payload.get("control_plane", {}).get("active_answer_critical_jobs", []))
            )
            + " active answer-critical job(s)"
        ),
        _release_entry_status_line(payload.get("release_entry", {})),
    ]
    control_plane_repairs = payload.get("control_plane", {}).get("repair_actions", [])
    if isinstance(control_plane_repairs, list) and control_plane_repairs:
        lines.append(f"Control-plane repairs: {len(control_plane_repairs)}")
    if payload["environment"].get("host_access_required"):
        lines.append("Host access required: yes")
    host_access_reasons = payload["environment"].get("host_access_reasons")
    if isinstance(host_access_reasons, list) and host_access_reasons:
        lines.append(
            "Host access reasons: " + "; ".join(str(item) for item in host_access_reasons)
        )
    if payload["environment"].get("host_access_guidance"):
        lines.append(str(payload["environment"]["host_access_guidance"]))
    rebuild_telemetry = payload["knowledge_base"].get("last_sync_rebuild_telemetry", {})
    if isinstance(rebuild_telemetry, dict) and rebuild_telemetry:
        lines.append(
            "Last sync rebuild: "
            f"cause={rebuild_telemetry.get('rebuild_cause', 'unknown')}, "
            f"dirty_sources={rebuild_telemetry.get('dirty_source_count', 0)}, "
            "contract_backfill_sources="
            f"{rebuild_telemetry.get('contract_backfill_source_count', 0)}, "
            "interaction_promotion_only="
            f"{'yes' if rebuild_telemetry.get('interaction_promotion_only') else 'no'}"
        )
    lane_b_follow_up = payload["knowledge_base"].get("lane_b_follow_up", {})
    if isinstance(lane_b_follow_up, dict) and lane_b_follow_up:
        lines.append(
            "Lane B follow-up: "
            f"state={lane_b_follow_up.get('state', 'unknown')}, "
            f"selected_sources={lane_b_follow_up.get('selected_source_count', 0)}, "
            f"selected_units={lane_b_follow_up.get('selected_unit_count', 0)}, "
            f"covered={lane_b_follow_up.get('covered_unit_count', 0)}, "
            f"blocked={lane_b_follow_up.get('blocked_unit_count', 0)}, "
            f"remaining={lane_b_follow_up.get('remaining_unit_count', 0)}"
        )
    storage_lifecycle = payload["knowledge_base"].get("storage_lifecycle", {})
    if isinstance(storage_lifecycle, dict) and storage_lifecycle:
        lines.append(
            "Storage lifecycle: "
            f"families={storage_lifecycle.get('family_count', 0)}, "
            f"publish_model={storage_lifecycle.get('publish_model', 'single-current')}, "
            f"published_roots={storage_lifecycle.get('published_root_count', 0)}, "
            "publish_ledger_entries="
            f"{storage_lifecycle.get('publish_ledger_count', 0)}"
        )
    if payload["knowledge_base"].get("legacy_archive_detected"):
        lines.append(
            "Legacy publish storage: "
            "detected (versions="
            f"{payload['knowledge_base'].get('legacy_archive_version_count', 0)}); "
            "the next mutating sync will compact it into single-current mode."
        )
    if pending_actions:
        lines.append(f"Pending actions: {', '.join(pending_actions)}")
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )

    exit_code_by_stage = {
        "foundation-only": 1,
        "workspace-bootstrapped": 0,
        "adapter-ready": 0,
        "control-plane-pending-confirmation": 1,
        "knowledge-base-invalid": 1,
        "knowledge-base-present": 0,
        "knowledge-base-stale": 2,
    }
    exit_code = exit_code_by_stage.get(stage, 2)
    return CommandReport(exit_code=exit_code, payload=payload, lines=lines)


def sync_workspace(
    paths: WorkspacePaths | None = None,
    *,
    autonomous: bool = True,
    assume_yes: bool = False,
    owner: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> CommandReport:
    """Stage, validate, and publish the Phase 4 knowledge base."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=True)
    if command_context["state"] == "blocked":
        return _mutating_command_coordination_report(
            command_name="Sync status",
            status_field="sync_status",
            coordination=command_context["coordination"],
            environment=environment_snapshot(workspace),
        )
    environment = environment_snapshot(workspace)
    effective_owner = owner or _unique_command_owner("sync")
    if autonomous:
        sync_readiness = cached_bootstrap_readiness(workspace, require_sync_capability=True)
        if not sync_readiness["ready"]:
            reason = str(sync_readiness.get("reason") or "")
            readiness_next_steps: list[str] = []
            required_capabilities: list[str] = []
            if reason == "office-renderer-required":
                readiness_next_steps.append(office_renderer_next_step())
                required_capabilities.append("office-rendering")
            elif reason == "pdf-renderer-required":
                readiness_next_steps.append(pdf_renderer_next_step())
                required_capabilities.append("pdf-rendering")
            else:
                readiness_next_steps.append(
                    "Run `docmason prepare` before retrying `docmason sync`."
                )
            detail = str(sync_readiness.get("detail") or "The workspace is not ready for sync.")
            payload = {
                "status": ACTION_REQUIRED,
                "sync_status": ACTION_REQUIRED,
                "detail": detail,
                "environment": environment,
                "control_plane": {},
                "change_set": {},
                "pending_sources": [],
                "validation": None,
                "published": False,
                "interaction_ingest": _interaction_ingest_snapshot(workspace),
                "rebuilt": False,
                "build_stats": {},
                "auto_repairs": {"repair_count": 0},
                "auto_authoring": {"attempted": 0, "authored": [], "authored_count": 0},
                "hybrid_enrichment": {},
                "autonomous_steps": [],
                "required_capabilities": required_capabilities,
                "pending_work_path": None,
                "next_workflows": [],
                "next_steps": readiness_next_steps,
                "rebuild_telemetry": {},
                "publish_storage": {},
                "lane_b_follow_up": {},
                "lane_b_follow_up_summary": {},
            }
            lines = [
                f"Sync status: {ACTION_REQUIRED}",
                detail,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        (
            _index_payload,
            active_sources,
            _ambiguous_match,
            preview_change_set,
        ) = preview_source_changes(workspace)
        interaction_signature = pending_interaction_signature(workspace)
        kb_snapshot = knowledge_base_snapshot(workspace)
        sync_signature = sync_input_signature(
            active_sources=active_sources,
            change_set=preview_change_set,
            pending_interaction_signature_value=interaction_signature,
        )
        materiality = classify_sync_materiality(
            change_set=preview_change_set,
            active_source_count=len(active_sources),
            published_present=bool(kb_snapshot.get("present")),
        )
        job_info = ensure_shared_job(
            workspace,
            job_key=f"sync:{sync_signature}",
            job_family="sync",
            criticality="answer-critical",
            scope={
                "target": "current",
                "strong_source_fingerprint_signature": strong_source_fingerprint_signature(
                    active_sources
                ),
                "materiality": materiality["materiality"],
            },
            input_signature=sync_signature,
            owner=effective_owner,
            run_id=run_id,
            requires_confirmation=materiality["materiality"] == "material" and not assume_yes,
            confirmation_kind="material-sync"
            if materiality["materiality"] == "material"
            else None,
            confirmation_prompt=(
                "A large unpublished workspace change set was detected. Build or refresh the "
                "knowledge base now before continuing this question?"
                if materiality["materiality"] == "material"
                else None
            ),
            confirmation_reason=(
                "; ".join(materiality["materiality_reasons"])
                if materiality["materiality_reasons"]
                else None
            ),
        )
        shared_job = job_info["manifest"]
        caller_role = str(job_info.get("caller_role") or "owner")
        if shared_job.get("status") == "awaiting-confirmation" and not assume_yes:
            detail = str(shared_job.get("confirmation_reason") or "Sync approval is required.")
            prompt = str(shared_job.get("confirmation_prompt") or detail)
            payload = {
                "status": ACTION_REQUIRED,
                "sync_status": "awaiting-confirmation",
                "detail": detail,
                "environment": environment,
                "control_plane": shared_job_control_plane_payload(
                    shared_job,
                    next_command="docmason sync --yes",
                ),
                "change_set": preview_change_set,
                "pending_sources": [],
                "validation": None,
                "published": False,
                "interaction_ingest": _interaction_ingest_snapshot(workspace),
                "rebuilt": False,
                "build_stats": {},
                "auto_repairs": {"repair_count": 0},
                "auto_authoring": {"attempted": 0, "authored": [], "authored_count": 0},
                "hybrid_enrichment": {},
                "autonomous_steps": [],
                "required_capabilities": [],
                "pending_work_path": None,
                "next_workflows": [],
                "next_steps": ["Run `docmason sync --yes` to approve and continue."],
                "rebuild_telemetry": {},
                "publish_storage": {},
                "lane_b_follow_up": {},
                "lane_b_follow_up_summary": {},
            }
            lines = [
                f"Sync status: {ACTION_REQUIRED}",
                prompt,
                "Next step: run `docmason sync --yes` to approve and continue.",
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        if shared_job.get("status") == "awaiting-confirmation" and assume_yes:
            shared_job = approve_shared_job(
                workspace,
                str(shared_job["job_id"]),
                owner=effective_owner,
                run_id=run_id,
            )
            record_run_event_for_runs(
                workspace,
                run_ids=shared_job.get("attached_run_ids"),
                stage="control-plane",
                event_type="shared-job-approved",
                payload={"job_id": shared_job.get("job_id")},
            )
        elif caller_role == "waiter" and shared_job.get("status") == "running":
            detail = "A matching shared sync job is already running."
            payload = {
                "status": DEGRADED,
                "sync_status": "waiting-shared-job",
                "detail": detail,
                "environment": environment,
                "control_plane": shared_job_control_plane_payload(
                    shared_job,
                    state="waiting-shared-job",
                ),
                "change_set": preview_change_set,
                "pending_sources": [],
                "validation": None,
                "published": False,
                "interaction_ingest": _interaction_ingest_snapshot(workspace),
                "rebuilt": False,
                "build_stats": {},
                "auto_repairs": {"repair_count": 0},
                "auto_authoring": {"attempted": 0, "authored": [], "authored_count": 0},
                "hybrid_enrichment": {},
                "autonomous_steps": [],
                "required_capabilities": [],
                "pending_work_path": None,
                "next_workflows": [],
                "next_steps": [
                    "Wait for the active shared sync job to settle, then retry if needed."
                ],
                "rebuild_telemetry": {},
                "publish_storage": {},
                "lane_b_follow_up": {},
                "lane_b_follow_up_summary": {},
            }
            lines = [
                f"Sync status: {DEGRADED}",
                detail,
            ]
            return make_report(DEGRADED, payload, lines)
    else:
        shared_job = {}
    try:
        result = _run_phase4_sync(
            workspace,
            autonomous=autonomous,
            owner=effective_owner,
            run_id=run_id,
        )
    except Exception as exc:
        if autonomous and shared_job:
            _settle_sync_shared_job(workspace, shared_job, unexpected_error=exc)
        raise
    if autonomous and shared_job:
        shared_job = _settle_sync_shared_job(workspace, shared_job, result=result)
    status = validation_command_status(result["status"])
    hybrid_mode = None
    if isinstance(result.get("hybrid_enrichment"), dict):
        hybrid_mode = result["hybrid_enrichment"].get("mode")
    if result["status"] in {"valid", "warnings"} and hybrid_mode in {
        "candidate-prepared",
        "partially-covered",
    }:
        status = DEGRADED
    next_workflows: list[str] = []
    follow_up_steps: list[str] = []
    pending_work_path = None
    if result["status"] == "pending-synthesis":
        pending_work_path = "knowledge_base/staging/pending_work.json"
        next_workflows = ["knowledge-construction", "knowledge-base-sync"]
        follow_up_steps.append(
            "Complete staged authoring from "
            "`knowledge_base/staging/pending_work.json`, then rerun "
            "`docmason sync` or `docmason workflow knowledge-base-sync`."
        )
    elif result["status"] == "blocking-errors":
        next_workflows = ["validation-repair", "knowledge-base-sync"]
        follow_up_steps.append(
            "Repair staged validation blockers, then rerun `docmason sync` "
            "or `docmason workflow knowledge-base-sync`."
        )
    elif result["status"] == "action-required":
        follow_up_steps.append(str(result["detail"]))
    payload = {
        "status": status,
        "sync_status": result["status"],
        "detail": result["detail"],
        "environment": environment,
        "control_plane": (
            shared_job_control_plane_payload(shared_job) if autonomous and shared_job else {}
        ),
        "pending_sources": result["pending_sources"],
        "validation": result["validation"],
        "published": result["published"],
        "interaction_ingest": result.get("interaction_ingest", {}),
        "rebuilt": result.get("rebuilt", False),
        "build_stats": result.get("build_stats", {}),
        "change_set": result.get("change_set", {}),
        "auto_repairs": result.get("auto_repairs", {}),
        "auto_authoring": result.get("auto_authoring", {}),
        "hybrid_enrichment": result.get("hybrid_enrichment", {}),
        "autonomous_steps": result.get("autonomous_steps", []),
        "required_capabilities": result.get("required_capabilities", []),
        "phase_costs": result.get("phase_costs", {}),
        "publish_skipped": result.get("publish_skipped", False),
        "publish_skip_reason": result.get("publish_skip_reason"),
        "repair_actions": result.get("repair_actions", []),
        "projection_state": result.get("projection_state", {}),
        "rebuild_telemetry": result.get("rebuild_telemetry", {}),
        "publish_storage": result.get("publish_storage", {}),
        "lane_b_follow_up": result.get("lane_b_follow_up", {}),
        "lane_b_follow_up_summary": result.get("lane_b_follow_up_summary", {}),
        "pending_work_path": pending_work_path,
        "next_workflows": next_workflows,
        "next_steps": follow_up_steps,
    }
    lines = [
        f"Sync status: {status}",
        result["detail"],
        f"Staging rebuilt: {'yes' if result.get('rebuilt', False) else 'no'}",
        f"Published: {'yes' if result['published'] else 'no'}",
    ]
    if result.get("publish_skipped"):
        lines.append("Publish skipped: yes")
    publish_skip_reason = result.get("publish_skip_reason")
    if isinstance(publish_skip_reason, str) and publish_skip_reason:
        lines.append(f"Publish skip reason: {publish_skip_reason}")
    build_stats = result.get("build_stats", {})
    if isinstance(build_stats, dict):
        lines.append(
            "Build stats: "
            f"reused={build_stats.get('reused_sources', 0)}, "
            f"rebuilt={build_stats.get('rebuilt_sources', 0)}"
        )
    rebuild_telemetry = result.get("rebuild_telemetry", {})
    if isinstance(rebuild_telemetry, dict) and rebuild_telemetry:
        lines.append(
            "Rebuild telemetry: "
            f"cause={rebuild_telemetry.get('rebuild_cause', 'unknown')}, "
            f"dirty_sources={rebuild_telemetry.get('dirty_source_count', 0)}, "
            "contract_backfill_sources="
            f"{rebuild_telemetry.get('contract_backfill_source_count', 0)}, "
            "interaction_promotion_only="
            f"{'yes' if rebuild_telemetry.get('interaction_promotion_only') else 'no'}, "
            "scoped_contract_repair="
            f"{'yes' if rebuild_telemetry.get('scoped_contract_repair_used') else 'no'}"
        )
    auto_repairs = result.get("auto_repairs", {})
    if isinstance(auto_repairs, dict):
        lines.append(f"Auto repairs: total={auto_repairs.get('repair_count', 0)}")
    hybrid_enrichment = result.get("hybrid_enrichment", {})
    if isinstance(hybrid_enrichment, dict):
        lines.append(
            "Hybrid enrichment: "
            f"mode={hybrid_enrichment.get('mode', 'unknown')}, "
            f"eligible={hybrid_enrichment.get('eligible_unit_count', 0)}, "
            f"covered={hybrid_enrichment.get('covered_unit_count', 0)}, "
            f"remaining={hybrid_enrichment.get('remaining_unit_count', 0)}, "
            f"blocked={hybrid_enrichment.get('blocked_unit_count', 0)}"
        )
        hybrid_work_path = hybrid_enrichment.get("hybrid_work_path")
        if isinstance(hybrid_work_path, str) and hybrid_work_path:
            lines.append(f"Hybrid work queue: {hybrid_work_path}")
        capability_gap_reason = hybrid_enrichment.get("capability_gap_reason")
        if isinstance(capability_gap_reason, str) and capability_gap_reason:
            lines.append(f"Hybrid gap: {capability_gap_reason}")
    auto_authoring = result.get("auto_authoring", {})
    if isinstance(auto_authoring, dict):
        lines.append(
            "Auto authoring: "
            f"attempted={auto_authoring.get('attempted', 0)}, "
            f"authored={auto_authoring.get('authored_count', 0)}"
        )
    interaction_ingest = result.get("interaction_ingest", {})
    if isinstance(interaction_ingest, dict):
        lines.append(
            "Interaction ingest: "
            f"pending={interaction_ingest.get('pending_promotion_count', 0)}, "
            f"promoted={interaction_ingest.get('promoted_memory_count', 0)}"
        )
    lane_b_follow_up_summary = result.get("lane_b_follow_up_summary", {})
    if isinstance(lane_b_follow_up_summary, dict) and lane_b_follow_up_summary:
        lines.append(
            "Lane B follow-up: "
            f"state={lane_b_follow_up_summary.get('state', 'unknown')}, "
            f"selected_sources={lane_b_follow_up_summary.get('selected_source_count', 0)}, "
            f"selected_units={lane_b_follow_up_summary.get('selected_unit_count', 0)}, "
            f"covered={lane_b_follow_up_summary.get('covered_unit_count', 0)}, "
            f"blocked={lane_b_follow_up_summary.get('blocked_unit_count', 0)}, "
            f"remaining={lane_b_follow_up_summary.get('remaining_unit_count', 0)}"
        )
    change_set = result.get("change_set", {})
    if isinstance(change_set, dict) and isinstance(change_set.get("stats"), dict):
        stats = change_set["stats"]
        lines.append(
            "Changes: "
            f"unchanged={stats.get('unchanged', 0)}, "
            f"added={stats.get('added', 0)}, "
            f"modified={stats.get('modified', 0)}, "
            f"moved-or-renamed={stats.get('moved_or_renamed', 0)}, "
            f"deleted={stats.get('deleted', 0)}, "
            f"ambiguous={stats.get('ambiguous', 0)}"
        )
    phase_costs = result.get("phase_costs", {})
    if isinstance(phase_costs, dict):
        lines.append(
            "Phase costs (s): "
            + ", ".join(
                f"{name}={float(value):.3f}"
                for name, value in phase_costs.items()
                if isinstance(value, (int, float))
            )
        )
    repair_actions = result.get("repair_actions", [])
    if isinstance(repair_actions, list) and repair_actions:
        lines.append(f"Repair actions: {len(repair_actions)}")
    projection_state = result.get("projection_state", {})
    if isinstance(projection_state, dict):
        lines.append(
            "Projection state: "
            f"dirty={'yes' if projection_state.get('dirty') else 'no'}, "
            f"active_job={projection_state.get('active_job_id') or 'none'}"
        )
    if result["pending_sources"]:
        lines.append(
            "Pending synthesis: "
            + ", ".join(source["source_id"] for source in result["pending_sources"])
        )
    if follow_up_steps:
        lines.append("Next steps: " + " ".join(follow_up_steps))
    validation = result["validation"]
    if isinstance(validation, dict):
        lines.append(
            f"Validation: {validation['status']} "
            f"(blocking={len(validation['blocking_errors'])}, "
            f"warnings={len(validation['warnings'])})"
        )
    required_capabilities = result.get("required_capabilities", [])
    if isinstance(required_capabilities, list) and required_capabilities:
        lines.append(
            "Required capabilities: " + ", ".join(str(item) for item in required_capabilities)
        )
    return make_report(status, payload, lines)


def retrieve_knowledge(
    *,
    query: str,
    top: int = 5,
    graph_hops: int = 1,
    document_types: list[str] | None = None,
    source_ids: list[str] | None = None,
    include_renders: bool = False,
    paths: WorkspacePaths | None = None,
) -> CommandReport:
    """Run retrieval over the published knowledge base."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=False)
    front_door = dict(command_context["front_door"])
    front_door_warning = (
        front_door.get("warning") if isinstance(front_door.get("warning"), dict) else None
    )
    retrieve_log_origin = "operator-direct" if front_door_warning else None
    try:
        result = _retrieve_corpus(
            paths=workspace,
            query=query,
            top=max(top, 1),
            graph_hops=max(graph_hops, 0),
            document_types=document_types,
            source_ids=source_ids,
            include_renders=include_renders,
            log_context=(
                front_door.get("log_context")
                if isinstance(front_door.get("log_context"), dict)
                else None
            ),
            log_origin=retrieve_log_origin,
        )
    except FileNotFoundError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "retrieve_status": "artifacts-missing",
            "detail": str(exc),
            "front_door": {
                key: value
                for key, value in front_door.items()
                if key != "log_context"
            },
        }
        lines = [
            f"Retrieve status: {ACTION_REQUIRED}",
            str(exc),
            "Next step: run `docmason sync` to rebuild retrieval artifacts.",
        ]
        if front_door_warning:
            lines.append(f"Front-door warning: {front_door_warning['detail']}")
        _apply_coordination_warning(
            payload=payload,
            lines=lines,
            coordination=command_context["coordination"],
        )
        return make_report(ACTION_REQUIRED, payload, lines)

    status = READY if result["results"] else DEGRADED
    payload = {
        "status": status,
        "retrieve_status": result["status"],
        **result,
        "front_door": {
            key: value
            for key, value in front_door.items()
            if key != "log_context"
        },
    }
    lines = [
        f"Retrieve status: {status}",
        f"Query: {query}",
        f"Session ID: {result['session_id']}",
        f"Results: {len(result['results'])}",
        _reference_resolution_line(
            result.get("reference_resolution")
            if isinstance(result.get("reference_resolution"), dict)
            else None
        ),
    ]
    reference_detail = _reference_resolution_detail_line(
        result.get("reference_resolution")
        if isinstance(result.get("reference_resolution"), dict)
        else None
    )
    if isinstance(reference_detail, str):
        lines.append(reference_detail)
    reference_notice = (
        result.get("reference_resolution", {}).get("notice_text")
        if isinstance(result.get("reference_resolution"), dict)
        else None
    )
    if isinstance(reference_notice, str) and reference_notice:
        lines.append(f"Reference notice: {reference_notice}")
    if published_evidence_line := _published_evidence_line(
        preferred_channels=result.get("preferred_channels"),
        matched_or_used_channels=result.get("matched_published_channels"),
        published_artifacts_sufficient=result.get("published_artifacts_sufficient"),
    ):
        lines.append(published_evidence_line)
    if result.get("source_escalation_required") and result.get("source_escalation_reason"):
        lines.append(f"Source escalation: {result['source_escalation_reason']}")
    if front_door_warning:
        lines.append(f"Front-door warning: {front_door_warning['detail']}")
    for index, item in enumerate(result["results"], start=1):
        lines.append(
            f"{index}. {item.get('title') or item['source_id']} [score={item['score']['total']}]"
        )
    if not result["results"]:
        lines.append("No grounded retrieval results were found for the query.")
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )
    return make_report(status, payload, lines)


def _published_evidence_line(
    *,
    preferred_channels: list[str] | None,
    matched_or_used_channels: list[str] | None,
    published_artifacts_sufficient: bool | None,
) -> str | None:
    preferred = [
        channel for channel in (preferred_channels or []) if isinstance(channel, str) and channel
    ]
    matched_or_used = [
        channel
        for channel in (matched_or_used_channels or [])
        if isinstance(channel, str) and channel
    ]
    if not preferred and not matched_or_used:
        return None
    parts = [
        "Published evidence",
        ("preferred=" + ",".join(preferred) if preferred else "preferred=auto"),
        ("matched=" + ",".join(matched_or_used) if matched_or_used else "matched=none"),
    ]
    if isinstance(published_artifacts_sufficient, bool):
        parts.append("sufficient=" + ("yes" if published_artifacts_sufficient else "no"))
    return "; ".join(parts)


def _reference_resolution_line(reference_resolution: dict[str, Any] | None) -> str:
    if not isinstance(reference_resolution, dict):
        return "Reference resolution: none detected"
    status = str(reference_resolution.get("status") or "none")
    if status == "none":
        return "Reference resolution: none detected"
    return f"Reference resolution: {status}"


def _reference_resolution_detail_line(reference_resolution: dict[str, Any] | None) -> str | None:
    if not isinstance(reference_resolution, dict):
        return None
    status = str(reference_resolution.get("status") or "none")
    if status == "none":
        return None
    parts: list[str] = []
    parsed_document_ref = reference_resolution.get("parsed_document_ref")
    if isinstance(parsed_document_ref, dict):
        raw_text = parsed_document_ref.get("raw_text") or parsed_document_ref.get("text")
        if isinstance(raw_text, str) and raw_text:
            parts.append(f"document=`{raw_text}`")
    parsed_locator_ref = reference_resolution.get("parsed_locator_ref")
    if isinstance(parsed_locator_ref, dict):
        raw_text = parsed_locator_ref.get("raw_text") or parsed_locator_ref.get("matched_alias")
        if isinstance(raw_text, str) and raw_text:
            parts.append(f"locator=`{raw_text}`")
    resolved_source_id = reference_resolution.get("resolved_source_id")
    if isinstance(resolved_source_id, str) and resolved_source_id:
        parts.append(f"source_id={resolved_source_id}")
    resolved_unit_id = reference_resolution.get("resolved_unit_id")
    if isinstance(resolved_unit_id, str) and resolved_unit_id:
        parts.append(f"unit_id={resolved_unit_id}")
    return "Reference detail: " + "; ".join(parts) if parts else None


def trace_knowledge(
    *,
    source_id: str | None = None,
    unit_id: str | None = None,
    answer_file: str | None = None,
    session_id: str | None = None,
    top: int = 3,
    paths: WorkspacePaths | None = None,
) -> CommandReport:
    """Trace provenance from a source or an answer back to evidence."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=False)
    front_door = dict(command_context["front_door"])
    front_door_warning = (
        front_door.get("warning") if isinstance(front_door.get("warning"), dict) else None
    )
    trace_log_origin = "operator-direct" if front_door_warning else None
    try:
        if source_id is not None:
            result = _trace_source(
                paths=workspace,
                source_id=source_id,
                unit_id=unit_id,
                log_context=(
                    front_door.get("log_context")
                    if isinstance(front_door.get("log_context"), dict)
                    else None
                ),
                log_origin=trace_log_origin,
            )
            status = READY
            payload = {
                "status": status,
                **result,
                "front_door": {
                    key: value
                    for key, value in front_door.items()
                    if key != "log_context"
                },
            }
            lines = [
                f"Trace status: {status}",
                f"Source ID: {source_id}",
                f"Title: {result['source'].get('title') or 'unknown'}",
                "Trace mode: citation-first",
            ]
            if "unit" in result:
                lines.append(
                    f"Unit: {result['unit'].get('unit_id')} "
                    f"({result['unit'].get('title') or 'untitled'})"
                )
            if front_door_warning:
                lines.append(f"Front-door warning: {front_door_warning['detail']}")
            _apply_coordination_warning(
                payload=payload,
                lines=lines,
                coordination=command_context["coordination"],
            )
            return make_report(status, payload, lines)

        if answer_file is not None:
            answer_file_path = Path(answer_file)
            if not answer_file_path.is_absolute():
                answer_file_path = workspace.root / answer_file_path
            result = _trace_answer_file(
                paths=workspace,
                answer_file=answer_file_path,
                top=max(top, 1),
                log_context=(
                    front_door.get("log_context")
                    if isinstance(front_door.get("log_context"), dict)
                    else None
                ),
                log_origin=trace_log_origin,
            )
        elif session_id is not None:
            result = _trace_session(
                paths=workspace,
                session_id=session_id,
                top=max(top, 1),
                log_context=(
                    front_door.get("log_context")
                    if isinstance(front_door.get("log_context"), dict)
                    else None
                ),
                log_origin=trace_log_origin,
            )
        else:  # pragma: no cover - protected by argparse
            raise ValueError("One trace entrypoint must be selected.")
    except FileNotFoundError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "trace_status": "artifacts-missing",
            "detail": str(exc),
            "front_door": {
                key: value
                for key, value in front_door.items()
                if key != "log_context"
            },
        }
        lines = [
            f"Trace status: {ACTION_REQUIRED}",
            str(exc),
            "Next step: run `docmason sync` to rebuild trace artifacts or logs.",
        ]
        if front_door_warning:
            lines.append(f"Front-door warning: {front_door_warning['detail']}")
        _apply_coordination_warning(
            payload=payload,
            lines=lines,
            coordination=command_context["coordination"],
        )
        return make_report(ACTION_REQUIRED, payload, lines)
    except KeyError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "trace_status": "not-found",
            "detail": str(exc),
            "front_door": {
                key: value
                for key, value in front_door.items()
                if key != "log_context"
            },
        }
        lines = [
            f"Trace status: {ACTION_REQUIRED}",
            f"Unknown trace target: {exc}",
        ]
        if front_door_warning:
            lines.append(f"Front-door warning: {front_door_warning['detail']}")
        _apply_coordination_warning(
            payload=payload,
            lines=lines,
            coordination=command_context["coordination"],
        )
        return make_report(ACTION_REQUIRED, payload, lines)
    except ValueError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "trace_status": "invalid-input",
            "detail": str(exc),
            "front_door": {
                key: value
                for key, value in front_door.items()
                if key != "log_context"
            },
        }
        lines = [
            f"Trace status: {ACTION_REQUIRED}",
            str(exc),
        ]
        if front_door_warning:
            lines.append(f"Front-door warning: {front_door_warning['detail']}")
        _apply_coordination_warning(
            payload=payload,
            lines=lines,
            coordination=command_context["coordination"],
        )
        return make_report(ACTION_REQUIRED, payload, lines)

    status = READY if result["status"] == "ready" else DEGRADED
    payload = {
        "status": status,
        **result,
        "front_door": {
            key: value
            for key, value in front_door.items()
            if key != "log_context"
        },
    }
    lines = [
        f"Trace status: {status}",
        f"Trace mode: {result['trace_mode']}",
    ]
    if result["trace_mode"] == "answer-first":
        lines.extend(
            [
                f"Session ID: {result['session_id']}",
                f"Trace ID: {result['trace_id']}",
                f"Answer state: {result.get('answer_state', 'unknown')}",
                f"Support basis: {result.get('support_basis') or 'kb-grounding-only'}",
                (
                    "Render inspection required: "
                    + ("yes" if result.get("render_inspection_required") else "no")
                ),
                f"Segments: {result['segment_count']}",
                (
                    "Grounding summary: "
                    f"grounded={result['grounding_summary']['grounded']}, "
                    f"partially-grounded={result['grounding_summary']['partially_grounded']}, "
                    f"unresolved={result['grounding_summary']['unresolved']}"
                ),
                _reference_resolution_line(
                    result.get("reference_resolution")
                    if isinstance(result.get("reference_resolution"), dict)
                    else None
                ),
            ]
        )
        reference_detail = _reference_resolution_detail_line(
            result.get("reference_resolution")
            if isinstance(result.get("reference_resolution"), dict)
            else None
        )
        if isinstance(reference_detail, str):
            lines.append(reference_detail)
        reference_notice = (
            result.get("reference_resolution", {}).get("notice_text")
            if isinstance(result.get("reference_resolution"), dict)
            else None
        )
        if isinstance(reference_notice, str) and reference_notice:
            lines.append(f"Reference notice: {reference_notice}")
        if published_evidence_line := _published_evidence_line(
            preferred_channels=result.get("preferred_channels"),
            matched_or_used_channels=result.get("used_published_channels"),
            published_artifacts_sufficient=result.get("published_artifacts_sufficient"),
        ):
            lines.append(published_evidence_line)
        if result.get("source_escalation_required") and result.get("source_escalation_reason"):
            lines.append(f"Source escalation: {result['source_escalation_reason']}")
    if front_door_warning:
        lines.append(f"Front-door warning: {front_door_warning['detail']}")
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )
    return make_report(status, payload, lines)


def validate_knowledge_base(
    paths: WorkspacePaths | None = None,
    *,
    target: str | None = None,
) -> CommandReport:
    """Validate the staged or published knowledge base."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=False)
    resolved_target = target or (
        "staging" if workspace.knowledge_base_staging_dir.exists() else "current"
    )
    if resolved_target not in {"staging", "current"}:
        failure_payload = {
            "status": ACTION_REQUIRED,
            "target": resolved_target,
            "validation_status": "blocking-errors",
        }
        lines = [
            f"Validate status: {ACTION_REQUIRED}",
            f"Unsupported validation target `{resolved_target}`.",
        ]
        return make_report(ACTION_REQUIRED, failure_payload, lines)

    validation = _validate_workspace(workspace, target=resolved_target)
    status = validation_command_status(validation["status"])
    payload: dict[str, Any] = {
        "status": status,
        "target": resolved_target,
        "validation_status": validation["status"],
        "validation": validation,
    }
    lines = [
        f"Validate status: {status}",
        f"Target: {resolved_target}",
        (
            f"Validation result: {validation['status']} "
            f"(blocking={len(validation['blocking_errors'])}, "
            f"warnings={len(validation['warnings'])})"
        ),
    ]
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )
    return make_report(status, payload, lines)


def build_claude_project_memory(
    paths: WorkspacePaths,
    workflow_metadata: list[WorkflowMetadata],
) -> str:
    """Render the generated Claude project-memory file from canonical skills."""
    skill_imports = [
        f"@../../{workflow.skill_path.relative_to(paths.root)}" for workflow in workflow_metadata
    ]
    lines = [
        "# DocMason Claude Project Memory",
        "",
        "This file is generated from committed canonical sources.",
        "It imports the vendor-neutral canonical skills so the generated adapter stays thin.",
        "",
        "@workflow-routing.md",
        "",
    ]
    lines.extend(skill_imports)
    lines.append("")
    return "\n".join(lines)


def sync_adapters(
    paths: WorkspacePaths | None = None,
    *,
    target: str = "claude",
) -> CommandReport:
    """Generate the supported local adapter artifacts from canonical sources."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=True)
    if command_context["state"] == "blocked":
        return _mutating_command_coordination_report(
            command_name="Adapter sync status",
            status_field="adapter_sync_status",
            coordination=command_context["coordination"],
        )
    if target != "claude":
        payload = {
            "status": ACTION_REQUIRED,
            "target": target,
            "generated_files": [],
            "source_inputs": [],
        }
        lines = [
            f"Adapter sync status: {ACTION_REQUIRED}",
            f"Target `{target}` is {UNSUPPORTED_TARGET}.",
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    source_inputs = workspace.claude_adapter_source_inputs()
    missing_sources = [path for path in source_inputs if not path.exists()]
    if missing_sources:
        payload = {
            "status": ACTION_REQUIRED,
            "target": target,
            "generated_files": [],
            "source_inputs": [
                str(path.relative_to(workspace.root)) for path in source_inputs if path.exists()
            ],
        }
        lines = [
            f"Adapter sync status: {ACTION_REQUIRED}",
            "Missing canonical adapter sources: "
            + ", ".join(str(path.relative_to(workspace.root)) for path in missing_sources),
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    try:
        workflow_metadata = load_workflow_metadata(workspace)
    except WorkflowMetadataError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "target": target,
            "generated_files": [],
            "source_inputs": [str(path.relative_to(workspace.root)) for path in source_inputs],
            "detail": str(exc),
        }
        lines = [
            f"Adapter sync status: {ACTION_REQUIRED}",
            str(exc),
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    workspace.claude_adapter_dir.mkdir(parents=True, exist_ok=True)
    _refresh_generated_connector_manifests(workspace)
    workspace.claude_workflow_routing_path.write_text(
        render_workflow_routing_markdown(workflow_metadata),
        encoding="utf-8",
    )
    workspace.claude_project_memory_path.write_text(
        build_claude_project_memory(workspace, workflow_metadata),
        encoding="utf-8",
    )
    try:
        sync_repo_local_skill_shims(workspace)
    except ValueError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "target": target,
            "generated_files": [],
            "source_inputs": [str(path.relative_to(workspace.root)) for path in source_inputs],
            "detail": str(exc),
        }
        lines = [
            f"Adapter sync status: {ACTION_REQUIRED}",
            str(exc),
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    generated_files = workspace.generated_claude_files()
    payload = {
        "status": READY,
        "target": target,
        "generated_files": [str(path.relative_to(workspace.root)) for path in generated_files],
        "source_inputs": [str(path.relative_to(workspace.root)) for path in source_inputs],
    }
    lines = [
        f"Adapter sync status: {READY}",
        "Generated files: " + ", ".join(payload["generated_files"]),
        "Source inputs: " + ", ".join(payload["source_inputs"]),
    ]
    return make_report(READY, payload, lines)


def update_core_workspace(
    paths: WorkspacePaths | None = None,
    *,
    bundle: Path | None = None,
) -> CommandReport:
    """Apply the latest generated clean core onto the current release bundle workspace."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=True)
    if command_context["state"] == "blocked":
        return _mutating_command_coordination_report(
            command_name="Update-core status",
            status_field="update_core_status",
            coordination=command_context["coordination"],
        )
    try:
        with workspace_lease(workspace, "update-core", timeout_seconds=30.0):
            payload = perform_update_core(workspace, bundle_path=bundle)
    except LeaseConflictError as exc:
        return _mutating_command_coordination_report(
            command_name="Update-core status",
            status_field="update_core_status",
            coordination=_coordination_from_lease_conflict(exc, state="blocked"),
        )
    except UpdateCoreError as exc:
        status = DEGRADED if exc.payload.get("core_updated") else ACTION_REQUIRED
        payload = {
            "status": status,
            "update_core_status": exc.code,
            "detail": exc.detail,
            "next_steps": exc.next_steps,
            **exc.payload,
        }
        lines = [
            f"Update-core status: {status}",
            exc.detail,
        ]
        if exc.next_steps:
            lines.append(f"Next steps: {', '.join(deduplicate(exc.next_steps))}")
        _apply_coordination_warning(
            payload=payload,
            lines=lines,
            coordination=command_context["coordination"],
        )
        return make_report(status, payload, lines)

    payload["status"] = READY
    latest_version = payload.get("latest_version") or payload.get("applied_version")
    if payload.get("update_core_status") == UPDATE_CORE_STATUS_ALREADY_CURRENT:
        lines = [
            f"Update-core status: {READY}",
            f"Current bundle is already up to date at {latest_version}.",
        ]
    else:
        lines = [
            f"Update-core status: {READY}",
            (
                "Applied clean core update: "
                f"{payload.get('current_version')} -> {payload.get('applied_version')}."
            ),
            (
                "Preserved paths: "
                + ", ".join(str(item) for item in payload.get("preserved_paths", []))
            ),
        ]
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )
    return make_report(READY, payload, lines)


def review_runtime_logs(paths: WorkspacePaths | None = None) -> CommandReport:
    """Refresh, summarize, and audit one runtime review request."""
    workspace = paths or locate_workspace()
    command_context = _reconcile_command_context(workspace, mutating=False)
    summary = refresh_log_review_summary(workspace)
    recent_conversations = summary.get("conversations", {}).get("recent", [])
    recent_query_sessions = summary.get("query_sessions", {}).get("recent", [])
    candidates = read_json(workspace.benchmark_candidates_path).get("candidates", [])
    if not recent_conversations and not recent_query_sessions:
        request_artifact = record_runtime_review_request(
            workspace,
            summary=summary,
            final_status=DEGRADED,
        )
        payload = {
            "status": DEGRADED,
            "review_summary": summary,
            "benchmark_candidates": {"candidate_count": len(candidates), "candidates": candidates},
            "review_request_id": request_artifact["request_id"],
            "review_request_path": request_artifact["artifact_path"],
        }
        lines = [
            f"Runtime review status: {DEGRADED}",
            "No recent workflow-linked query, trace, or conversation activity is available yet.",
            (
                "Run retrieval, trace, ask, or workflow-linked repository activity "
                "before expecting a populated review summary."
            ),
        ]
        _apply_coordination_warning(
            payload=payload,
            lines=lines,
            coordination=command_context["coordination"],
        )
        return make_report(DEGRADED, payload, lines)

    request_artifact = record_runtime_review_request(
        workspace,
        summary=summary,
        final_status=READY,
    )
    payload = {
        "status": READY,
        "review_summary": summary,
        "benchmark_candidates": {"candidate_count": len(candidates), "candidates": candidates},
        "review_request_id": request_artifact["request_id"],
        "review_request_path": request_artifact["artifact_path"],
    }
    lines = [
        f"Runtime review status: {READY}",
        f"Recent conversations: {len(recent_conversations)}",
        f"Recent query sessions: {len(recent_query_sessions)}",
        f"Benchmark candidates: {len(candidates)}",
    ]
    _apply_coordination_warning(
        payload=payload,
        lines=lines,
        coordination=command_context["coordination"],
    )
    return make_report(READY, payload, lines)


def _workflow_step(name: str, report: CommandReport) -> dict[str, Any]:
    return {
        "step": name,
        "status": report.payload.get("status"),
        "exit_code": report.exit_code,
        "payload": report.payload,
    }


def _workflow_status(reports: list[CommandReport]) -> str:
    if any(report.payload.get("status") == ACTION_REQUIRED for report in reports):
        return ACTION_REQUIRED
    if any(report.payload.get("status") == DEGRADED for report in reports):
        return DEGRADED
    return READY


def run_workflow(
    workflow_id: str,
    *,
    paths: WorkspacePaths | None = None,
) -> CommandReport:
    """Execute a supported public workflow surface as one advanced command entry."""
    workspace = paths or locate_workspace()
    try:
        metadata_lookup = {
            workflow.workflow_id: workflow
            for workflow in load_workflow_metadata(workspace, include_operator=True)
        }
    except WorkflowMetadataError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "workflow_id": workflow_id,
            "detail": str(exc),
        }
        lines = [
            f"Workflow status: {ACTION_REQUIRED}",
            str(exc),
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    metadata = metadata_lookup.get(workflow_id)
    if metadata is None:
        unknown_workflow_payload: dict[str, Any] = {
            "status": ACTION_REQUIRED,
            "workflow_id": workflow_id,
            "supported_workflows": sorted(metadata_lookup),
        }
        lines = [
            f"Workflow status: {ACTION_REQUIRED}",
            f"Unknown workflow `{workflow_id}`.",
        ]
        return make_report(ACTION_REQUIRED, unknown_workflow_payload, lines)

    if workflow_id in {
        "ask",
        "grounded-answer",
        "grounded-composition",
        "retrieval-workflow",
        "provenance-trace",
        "knowledge-construction",
        "validation-repair",
    }:
        payload = {
            "status": ACTION_REQUIRED,
            "workflow_id": workflow_id,
            "detail": (
                "This workflow is not exposed through `docmason workflow`. "
                "`ask` remains the only natural-language question entry surface, and "
                "inner or agent-authored workflows still require routed agent execution."
            ),
        }
        lines = [
            f"Workflow status: {ACTION_REQUIRED}",
            payload["detail"],
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    steps: list[tuple[str, CommandReport]] = []
    workflow_status = "completed"
    next_workflows: list[str] = []
    next_steps: list[str] = []

    if workflow_id == "workspace-bootstrap":
        steps.append(("prepare", bootstrap_workspace_with_launcher(workspace)))
        steps.append(("status", status_workspace(workspace)))
    elif workflow_id == "workspace-doctor":
        steps.append(("doctor", doctor_workspace(workspace)))
    elif workflow_id == "workspace-status":
        steps.append(("status", status_workspace(workspace)))
    elif workflow_id == "adapter-sync":
        steps.append(("sync-adapters", sync_adapters(workspace)))
        steps.append(("status", status_workspace(workspace)))
    elif workflow_id == "runtime-log-review":
        steps.append(("runtime-log-review", review_runtime_logs(workspace)))
    elif workflow_id == "operator-eval":
        operator_payload, operator_lines = _run_operator_eval(workspace)
        operator_report_payload: dict[str, Any] = {
            **operator_payload,
            "completion_signal": metadata.handoff["completion_signal"],
            "artifacts": operator_payload.get("artifacts", metadata.handoff.get("artifacts", [])),
            "follow_up": metadata.handoff.get("follow_up", []),
            "workflow_status": (
                "completed"
                if operator_payload.get("status") not in {ACTION_REQUIRED, DEGRADED}
                else "needs-attention"
            ),
            "steps": [],
            "final_report": operator_payload,
        }
        return make_report(
            str(operator_payload.get("status") or READY),
            operator_report_payload,
            operator_lines,
        )
    elif workflow_id == "knowledge-base-sync":
        steps.append(("status", status_workspace(workspace)))
        sync_report = sync_workspace(workspace)
        steps.append(("sync", sync_report))
        sync_status = sync_report.payload.get("sync_status")
        if sync_status == "awaiting-confirmation":
            workflow_status = "needs-confirmation"
            next_steps.extend(sync_report.payload.get("next_steps", []))
        elif sync_status == "waiting-shared-job":
            workflow_status = "waiting-shared-job"
            next_steps.extend(sync_report.payload.get("next_steps", []))
        elif sync_status == "pending-synthesis":
            workflow_status = "needs-agent-authoring"
            next_workflows = ["knowledge-construction", "knowledge-base-sync"]
            next_steps.append(
                "Complete staged authoring from "
                "`knowledge_base/staging/pending_work.json`, then rerun "
                "`docmason workflow knowledge-base-sync`."
            )
        elif sync_status == "blocking-errors":
            workflow_status = "needs-validation-repair"
            next_workflows = ["validation-repair", "knowledge-base-sync"]
            next_steps.append(
                "Repair staged validation blockers, then rerun "
                "`docmason workflow knowledge-base-sync`."
            )
        else:
            hybrid_enrichment = sync_report.payload.get("hybrid_enrichment", {})
            lane_b_follow_up = sync_report.payload.get("lane_b_follow_up", {})
            if (
                sync_status in {"valid", "warnings"}
                and isinstance(hybrid_enrichment, dict)
                and hybrid_enrichment.get("mode") in {"candidate-prepared", "partially-covered"}
            ):
                workflow_status = "needs-hybrid-enrichment"
                next_workflows = ["knowledge-construction", "knowledge-base-sync"]
                work_path = (
                    lane_b_follow_up.get("work_path")
                    if isinstance(lane_b_follow_up, dict)
                    else None
                )
                if isinstance(work_path, str) and work_path:
                    next_steps.append(
                        "Deterministic publication succeeded, and a governed staged multimodal "
                        f"follow-up batch is ready at `{work_path}`. Consume that bounded work "
                        "packet with a capable host agent, write additive `semantic_overlay/` "
                        "sidecars, then rerun `docmason workflow knowledge-base-sync`."
                    )
                else:
                    hybrid_work_path = hybrid_enrichment.get("hybrid_work_path")
                    queue_detail = (
                        f" from `{hybrid_work_path}`"
                        if isinstance(hybrid_work_path, str) and hybrid_work_path
                        else ""
                    )
                    next_steps.append(
                        "Deterministic publication succeeded, but hard-artifact multimodal "
                        f"enrichment is still queued{queue_detail}. Consume that queue with a "
                        "capable multimodal host agent, write additive `semantic_overlay/` "
                        "sidecars, then rerun `docmason workflow knowledge-base-sync`."
                    )
    else:
        payload = {
            "status": ACTION_REQUIRED,
            "workflow_id": workflow_id,
            "detail": f"Workflow `{workflow_id}` is {UNSUPPORTED_TARGET}.",
        }
        lines = [
            f"Workflow status: {ACTION_REQUIRED}",
            payload["detail"],
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    reports = [report for _step_name, report in steps]
    status = _workflow_status(reports)
    if workflow_status != "completed" and status == READY:
        status = DEGRADED
    workflow_payload: dict[str, Any] = {
        "status": status,
        "workflow_id": workflow_id,
        "workflow_status": workflow_status,
        "completion_signal": metadata.handoff["completion_signal"],
        "artifacts": metadata.handoff.get("artifacts", []),
        "follow_up": metadata.handoff.get("follow_up", []),
        "next_workflows": next_workflows,
        "next_steps": next_steps,
        "steps": [_workflow_step(step_name, report) for step_name, report in steps],
        "final_report": reports[-1].payload if reports else {},
    }
    lines = [
        f"Workflow status: {status}",
        f"Workflow: {workflow_id}",
        f"Workflow outcome: {workflow_status}",
    ]
    if next_steps:
        lines.extend(next_steps)
    elif reports:
        lines.extend(reports[-1].lines)
    return make_report(status, workflow_payload, lines)


def emit_report(report: CommandReport, *, as_json: bool) -> int:
    """Write a command report to stdout in either JSON or human-readable form."""
    if as_json:
        print(json.dumps(report.payload, indent=2, sort_keys=True))
    else:
        print("\n".join(report.lines))
    return report.exit_code

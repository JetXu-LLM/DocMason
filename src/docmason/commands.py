"""Command implementations for the DocMason operator surface."""

from __future__ import annotations

import json
import os
import platform
import shutil
import site
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .interaction import (
    interaction_ingest_snapshot,
    maybe_reconcile_active_thread,
    refresh_generated_connector_manifests,
)
from .knowledge import (
    office_renderer_snapshot,
    pdf_renderer_snapshot,
    validate_workspace,
)
from .knowledge import (
    sync_workspace as run_phase4_sync,
)
from .operator_eval import run_operator_eval
from .project import (
    BOOTSTRAP_STATE_SCHEMA_VERSION,
    MINIMUM_PYTHON,
    SUPPORTED_INPUTS,
    WorkspacePaths,
    adapter_snapshot,
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
from .retrieval import retrieve_corpus, trace_answer_file, trace_session, trace_source
from .review import refresh_log_review_summary
from .workflows import (
    WorkflowMetadata,
    WorkflowMetadataError,
    load_workflow_metadata,
    render_workflow_routing_markdown,
)

READY = "ready"
DEGRADED = "degraded"
ACTION_REQUIRED = "action-required"
UNSUPPORTED_TARGET = "planned but not implemented yet"


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


def summarize_command_failure(command: Sequence[str], execution: CommandExecution) -> str:
    """Render a compact failure summary for subprocess-driven steps."""
    details = execution.stderr or execution.stdout or "no output"
    return f"{' '.join(command)} failed with exit code {execution.exit_code}: {details}"


def python_supported() -> bool:
    """Return whether the current interpreter satisfies the minimum version."""
    return sys.version_info >= MINIMUM_PYTHON


def platform_supported() -> bool:
    """Return whether the current platform is in the supported set."""
    return sys.platform in {"darwin", "linux"}


def find_uv_binary() -> str | None:
    """Resolve ``uv`` from the active ``PATH`` when available."""
    return shutil.which("uv")


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


def homebrew_auto_install_plan(
    *,
    command_runner: CommandRunner,
    cwd: Path,
) -> dict[str, Any]:
    """Return whether the official unattended Homebrew install path is viable."""
    if sys.platform != "darwin":
        return {"feasible": False, "detail": "Homebrew automation is only supported on macOS."}
    if find_brew_binary() is not None:
        return {"feasible": False, "detail": "Homebrew is already installed."}

    machine = platform.machine().lower()
    if machine not in {"arm64", "x86_64"}:
        return {
            "feasible": False,
            "detail": f"Homebrew automation is not configured for machine type `{machine}`.",
        }

    bash_path = Path("/bin/bash")
    curl_path = Path("/usr/bin/curl")
    xcode_select_path = Path("/usr/bin/xcode-select")
    if not bash_path.exists() or not os.access(bash_path, os.X_OK):
        return {
            "feasible": False,
            "detail": "The official Homebrew installer requires `/bin/bash`.",
        }
    if not curl_path.exists() or not os.access(curl_path, os.X_OK):
        return {
            "feasible": False,
            "detail": "The official Homebrew installer requires `/usr/bin/curl`.",
        }
    if not xcode_select_path.exists() or command_runner(
        [str(xcode_select_path), "-p"],
        cwd,
    ).exit_code != 0:
        return {
            "feasible": False,
            "detail": (
                "The official Homebrew installer requires Xcode Command Line Tools to be "
                "available first."
            ),
        }

    prefix = Path("/opt/homebrew" if machine == "arm64" else "/usr/local")
    writable_probe = prefix if prefix.exists() else prefix.parent
    if not os.access(writable_probe, os.W_OK):
        return {
            "feasible": False,
            "detail": (
                "The default Homebrew prefix is not writable non-interactively on this host, so "
                "silent Homebrew installation is not viable."
            ),
        }

    install_command = [
        "/usr/bin/env",
        "NONINTERACTIVE=1",
        str(bash_path),
        "-c",
        '"$(/usr/bin/curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
    ]
    return {
        "feasible": True,
        "detail": "The official unattended Homebrew install path is available.",
        "expected_brew": str(prefix / "bin" / "brew"),
        "install_command": install_command,
        "install_display": (
            '`NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL '
            'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`'
        ),
    }


def refresh_brew_binary_after_install(plan: dict[str, Any]) -> str | None:
    """Resolve Homebrew again after a successful unattended install attempt."""
    brew_binary = find_brew_binary()
    if brew_binary is not None:
        return brew_binary
    expected_brew = plan.get("expected_brew")
    if isinstance(expected_brew, str) and expected_brew and Path(expected_brew).exists():
        return expected_brew
    return None


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


def make_report(status: str, payload: dict[str, Any], lines: list[str]) -> CommandReport:
    """Translate a logical status into the standard CLI exit-code contract."""
    exit_code = {READY: 0, DEGRADED: 2, ACTION_REQUIRED: 1}[status]
    if "status" in payload:
        payload["status"] = status
    return CommandReport(exit_code=exit_code, payload=payload, lines=lines)


def validation_command_status(validation_status: str) -> str:
    """Map a validation result to the CLI status contract."""
    if validation_status == "valid":
        return READY
    if validation_status in {"warnings", "pending-synthesis"}:
        return DEGRADED
    return ACTION_REQUIRED


def office_renderer_next_step() -> str:
    """Return the preferred next-step guidance for installing LibreOffice."""
    if sys.platform == "darwin":
        if find_brew_binary():
            return (
                "Install LibreOffice with `brew install --cask libreoffice-still`, or download "
                "the official macOS installer from https://www.libreoffice.org/download/download/, "
                "then rerun `docmason doctor`."
            )
        return (
            "Run `docmason prepare --yes` to let the workspace attempt the managed Homebrew plus "
            "LibreOffice install path when the host supports silent automation, or download and "
            "install LibreOffice from https://www.libreoffice.org/download/download/. On macOS, "
            "drag the app into `/Applications`; DocMason will detect the standard `soffice` path "
            "there. Then rerun `docmason doctor`."
        )
    return (
        "Install LibreOffice with your Linux distribution's package manager or from "
        "https://www.libreoffice.org/download/download/, ensure `soffice` is on PATH, "
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
    editable_install: bool,
    editable_detail: str,
    office_snapshot: dict[str, Any],
    pdf_snapshot: dict[str, Any],
) -> None:
    """Persist the lightweight cached ready marker used by ordinary ask flows."""
    requirements = source_runtime_requirements(workspace)
    state = {
        "schema_version": BOOTSTRAP_STATE_SCHEMA_VERSION,
        "status": status,
        "environment_ready": bool(workspace.venv_python.exists() and editable_install),
        "checked_at": bootstrap_checked_at(),
        "prepared_at": (
            isoformat_timestamp(workspace.venv_python.stat().st_mtime)
            if workspace.venv_python.exists()
            else None
        ),
        "workspace_root": str(workspace.root.resolve()),
        "package_manager": package_manager,
        "python_executable": bootstrap_python,
        "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
        "editable_install": editable_install,
        "editable_install_detail": editable_detail,
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
    editable_install, editable_detail = editable_install_probe(paths)
    state = bootstrap_state(paths)
    cached = cached_bootstrap_readiness(paths)
    return {
        "ready": bool(paths.venv_python.exists() and editable_install),
        "venv_python": str(paths.venv_python.relative_to(paths.root)),
        "editable_install": editable_install,
        "editable_install_detail": editable_detail,
        "bootstrap_state_present": bool(state),
        "package_manager": state.get("package_manager"),
        "prepared_at": state.get("prepared_at"),
        "manual_recovery_doc": manual_workspace_recovery_doc(),
        "cached_ready": bool(cached.get("ready")),
        "cached_ready_reason": cached.get("reason"),
        "cached_ready_detail": cached.get("detail"),
        "bootstrap_state": bootstrap_state_summary(paths),
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
    interaction = interaction_ingest_snapshot(paths)
    claude = adapters["claude"]

    if kb["stale"]:
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
    if not environment["ready"]:
        pending_actions.append("prepare")
    if source_total > 0 and (not kb["present"] or kb["stale"] or stage == "knowledge-base-invalid"):
        pending_actions.append("sync")
    if kb["staging_present"] and kb["validation_status"] in {"blocking-errors", "warnings"}:
        pending_actions.append("validate-kb")
    if interaction["sync_recommended"]:
        pending_actions.append("sync")

    payload = {
        "stage": stage,
        "environment_ready": environment["ready"],
        "bootstrap_state": dict(environment["bootstrap_state"]),
        "source_documents": {
            "path": str(paths.source_dir.relative_to(paths.root)),
            "counts": source_counts,
            "tiers": source_tiers,
            "total": source_total,
        },
        "knowledge_base": kb,
        "interaction_ingest": interaction,
        "adapters": adapters,
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
) -> CommandReport:
    """Bootstrap repo-local state and install DocMason into the workspace environment."""
    workspace = paths or locate_workspace()
    actions_performed: list[str] = []
    actions_skipped: list[str] = []
    next_steps: list[str] = []
    manual_recovery_next_step = manual_workspace_recovery_step()

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

    bootstrap_python = str(Path(sys.executable).resolve())
    package_manager = "pip"
    status = READY
    uv_binary = find_uv_binary()
    should_attempt_uv_install = False
    uv_install_command, uv_install_display = preferred_uv_install_command(bootstrap_python)
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()

    if uv_binary is None:
        if assume_yes:
            should_attempt_uv_install = True
        elif interactive:
            answer = prompt(
                "uv is not installed. Install it with "
                f"{uv_install_display} before continuing? [y/N]: "
            )
            should_attempt_uv_install = answer.strip().lower() in {"y", "yes"}
        else:
            actions_skipped.append(
                "Skipped uv installation in non-interactive mode; falling back to venv + pip."
            )
            next_steps.append(f"Recommended: install uv with {uv_install_display}.")

    if uv_binary is None and should_attempt_uv_install:
        pip_ready, pip_detail = ensure_python_pip(
            bootstrap_python,
            cwd=workspace.root,
            command_runner=command_runner,
        )
        if not pip_ready:
            actions_skipped.append(
                "pip is unavailable for the bootstrap interpreter; falling back to venv + pip. "
                f"Details: {pip_detail}"
            )
            next_steps.append(
                "Repair the bootstrap interpreter so `python -m pip` works, or continue with the "
                "repo-local venv + pip fallback."
            )
        else:
            if pip_detail == "Restored pip with ensurepip.":
                actions_performed.append("Restored bootstrap pip with ensurepip.")
            execution = command_runner(uv_install_command, workspace.root)
            if execution.exit_code == 0:
                uv_binary = find_uv_binary()
                uv_candidate = user_scoped_uv_path()
                if uv_binary is not None:
                    actions_performed.append(f"Installed uv with {uv_install_display}.")
                elif uv_candidate is not None and uv_candidate.exists():
                    uv_binary = str(uv_candidate)
                    actions_performed.append(f"Installed uv with {uv_install_display}.")
                else:
                    actions_skipped.append(
                        "Completed a uv install attempt, but the uv executable was not found "
                        "on the current PATH; "
                        "falling back to venv + pip."
                    )
            else:
                actions_skipped.append(
                    f"uv installation failed; falling back to venv + pip. "
                    f"Details: {execution.stderr or execution.stdout or 'no output'}"
                )
                next_steps.append(f"Retry uv installation with {uv_install_display}.")

    if uv_binary is not None:
        package_manager = "uv"
        creation = command_runner(
            [
                uv_binary,
                "venv",
                "--allow-existing",
                "--python",
                bootstrap_python,
                str(workspace.venv_dir),
            ],
            workspace.root,
        )
        if creation.exit_code != 0:
            environment = {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
            }
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": environment,
                "manual_recovery_doc": manual_workspace_recovery_doc(),
                "next_steps": [
                    summarize_command_failure(["uv", "venv"], creation),
                    manual_recovery_next_step,
                ],
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                summarize_command_failure([uv_binary, "venv"], creation),
                manual_recovery_next_step,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        actions_performed.append("Created or repaired .venv with uv.")

        install = command_runner(
            [uv_binary, "pip", "install", "--python", str(workspace.venv_python), "-e", ".[dev]"],
            workspace.root,
        )
        if install.exit_code != 0:
            environment = {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
            }
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": environment,
                "manual_recovery_doc": manual_workspace_recovery_doc(),
                "next_steps": [
                    summarize_command_failure([uv_binary, "pip", "install"], install),
                    manual_recovery_next_step,
                ],
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                summarize_command_failure([uv_binary, "pip", "install"], install),
                manual_recovery_next_step,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        actions_performed.append(
            "Installed DocMason in editable mode with dev dependencies via uv."
        )
    else:
        status = DEGRADED
        package_manager = "pip"
        next_steps.append(f"Optional: install uv with {uv_install_display} later.")

        creation = command_runner(
            [bootstrap_python, "-m", "venv", str(workspace.venv_dir)],
            workspace.root,
        )
        if creation.exit_code != 0:
            environment = {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
            }
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": environment,
                "next_steps": [
                    summarize_command_failure(
                        [bootstrap_python, "-m", "venv"],
                        creation,
                    ),
                    manual_recovery_next_step,
                ],
                "manual_recovery_doc": manual_workspace_recovery_doc(),
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                summarize_command_failure([bootstrap_python, "-m", "venv"], creation),
                manual_recovery_next_step,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        actions_performed.append("Created or repaired .venv with venv.")

        venv_pip_ready, venv_pip_detail = ensure_python_pip(
            str(workspace.venv_python),
            cwd=workspace.root,
            command_runner=command_runner,
        )
        if not venv_pip_ready:
            environment = {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
            }
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": environment,
                "next_steps": [
                    f"Repair pip inside `.venv`. Details: {venv_pip_detail}",
                    manual_recovery_next_step,
                ],
                "manual_recovery_doc": manual_workspace_recovery_doc(),
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                f"Repair pip inside `.venv`. Details: {venv_pip_detail}",
                manual_recovery_next_step,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        if venv_pip_detail == "Restored pip with ensurepip.":
            actions_performed.append("Restored `.venv` pip with ensurepip.")

        upgrade = command_runner(
            [str(workspace.venv_python), "-m", "pip", "install", "--upgrade", "pip"],
            workspace.root,
        )
        if upgrade.exit_code != 0:
            environment = {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
            }
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": environment,
                "next_steps": [
                    summarize_command_failure(
                        [str(workspace.venv_python), "-m", "pip"],
                        upgrade,
                    ),
                    manual_recovery_next_step,
                ],
                "manual_recovery_doc": manual_workspace_recovery_doc(),
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                summarize_command_failure([str(workspace.venv_python), "-m", "pip"], upgrade),
                manual_recovery_next_step,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        actions_performed.append("Upgraded pip inside .venv.")

        install = command_runner(
            [str(workspace.venv_python), "-m", "pip", "install", "-e", ".[dev]"],
            workspace.root,
        )
        if install.exit_code != 0:
            environment = {
                "package_manager": package_manager,
                "python_executable": bootstrap_python,
            }
            payload = {
                "status": ACTION_REQUIRED,
                "actions_performed": actions_performed,
                "actions_skipped": actions_skipped,
                "environment": environment,
                "next_steps": [
                    summarize_command_failure(
                        [str(workspace.venv_python), "-m", "pip"],
                        install,
                    ),
                    manual_recovery_next_step,
                ],
                "manual_recovery_doc": manual_workspace_recovery_doc(),
            }
            lines = [
                f"Prepare status: {ACTION_REQUIRED}",
                summarize_command_failure([str(workspace.venv_python), "-m", "pip"], install),
                manual_recovery_next_step,
            ]
            return make_report(ACTION_REQUIRED, payload, lines)
        actions_performed.append(
            "Installed DocMason in editable mode with dev dependencies via pip."
        )

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

    office_snapshot = office_renderer_snapshot(workspace)
    if office_snapshot["required"] and not office_snapshot["ready"] and assume_yes:
        brew_plan = homebrew_auto_install_plan(command_runner=command_runner, cwd=workspace.root)
        if find_brew_binary() is None and brew_plan["feasible"]:
            brew_install = command_runner(brew_plan["install_command"], workspace.root)
            if brew_install.exit_code == 0:
                refreshed_brew = refresh_brew_binary_after_install(brew_plan)
                if refreshed_brew is not None:
                    brew_bin_dir = str(Path(refreshed_brew).parent)
                    os.environ["PATH"] = f"{brew_bin_dir}:{os.environ.get('PATH', '')}"
                    actions_performed.append(
                        "Installed Homebrew with the official unattended installer."
                    )
                else:
                    actions_skipped.append(
                        "Homebrew install completed, but the brew executable was not found "
                        "afterward."
                    )
            else:
                actions_skipped.append(
                    "Homebrew installation failed during prepare. Details: "
                    f"{brew_install.stderr or brew_install.stdout or 'no output'}"
                )
                next_steps.append(
                    "Install Homebrew manually or install LibreOffice from the official "
                    "macOS installer, then rerun `docmason prepare --yes`."
                )
        elif find_brew_binary() is None and not brew_plan["feasible"]:
            actions_skipped.append(
                "Skipped Homebrew installation because the host does not satisfy the official "
                f"unattended install preconditions. Details: {brew_plan['detail']}"
            )
        install_command, install_display = preferred_libreoffice_install_command()
        if install_command is not None and install_display is not None:
            execution = command_runner(install_command, workspace.root)
            if execution.exit_code == 0:
                actions_performed.append(f"Installed LibreOffice with {install_display}.")
                office_snapshot = office_renderer_snapshot(workspace)
            else:
                actions_skipped.append(
                    "LibreOffice installation failed during prepare. Details: "
                    f"{execution.stderr or execution.stdout or 'no output'}"
                )
    if office_snapshot["required"] and not office_snapshot["ready"]:
        status = DEGRADED
        next_steps.append(office_renderer_next_step())

    pdf_snapshot = pdf_renderer_snapshot()
    write_bootstrap_ready_marker(
        workspace,
        status=status,
        package_manager=package_manager,
        bootstrap_python=bootstrap_python,
        editable_install=editable_install,
        editable_detail=editable_detail,
        office_snapshot=office_snapshot,
        pdf_snapshot=pdf_snapshot,
    )
    refresh_generated_connector_manifests(workspace)
    actions_performed.append("Recorded bootstrap state in runtime/bootstrap_state.json.")

    payload = {
        "status": status,
        "actions_performed": actions_performed,
        "actions_skipped": actions_skipped,
        "manual_recovery_doc": manual_workspace_recovery_doc(),
        "environment": {
            "python_executable": bootstrap_python,
            "python_version": ".".join(str(part) for part in sys.version_info[:3]),
            "venv_python": str(workspace.venv_python.relative_to(workspace.root)),
            "package_manager": package_manager,
            "editable_install": editable_install,
            "editable_install_detail": editable_detail,
            "bootstrap_state": str(workspace.bootstrap_state_path.relative_to(workspace.root)),
            "manual_recovery_doc": manual_workspace_recovery_doc(),
        },
        "next_steps": deduplicate(next_steps),
    }
    lines = [
        f"Prepare status: {status}",
        f"Package workflow: {package_manager}",
        f"Virtual environment: {workspace.venv_dir.relative_to(workspace.root)}",
        editable_detail,
    ]
    if office_snapshot["required"] and not office_snapshot["ready"]:
        lines.append(office_snapshot["detail"])
    if next_steps:
        lines.append(f"Next steps: {', '.join(deduplicate(next_steps))}")
    return make_report(status, payload, lines)


def doctor_workspace(
    paths: WorkspacePaths | None = None,
    *,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
) -> CommandReport:
    """Inspect workspace readiness without mutating any repository state."""
    workspace = paths or locate_workspace()
    maybe_reconcile_active_thread(workspace)
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
            "Run `docmason prepare --yes` from the current workspace root to refresh the cached bootstrap marker.",
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
            "Run `docmason prepare --yes` to refresh the cached bootstrap marker to the current contract.",
        )
    else:
        add_check(
            "bootstrap-state",
            DEGRADED,
            bootstrap_detail,
            "Run `docmason prepare --yes` to record the current bootstrap marker.",
        )

    uv_binary = find_uv_binary()
    _install_command, uv_install_display = preferred_uv_install_command(str(Path(sys.executable)))
    if uv_binary:
        add_check("uv", READY, f"uv is available at {uv_binary}.")
    else:
        add_check(
            "uv",
            DEGRADED,
            "uv is not installed; `prepare` will fall back to venv + pip.",
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
                "Install the DocMason PDF dependencies and rerun `docmason doctor`.",
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
        add_check("claude-adapter", READY, "Claude adapter files are present and fresh.")
    elif claude["present"]:
        add_check(
            "claude-adapter",
            READY,
            "Claude adapter files are present but stale relative to canonical sources.",
            "If you plan to use Claude, run `docmason sync-adapters` to refresh the adapter.",
        )
    else:
        add_check(
            "claude-adapter",
            READY,
            (
                "Claude adapter files have not been generated yet. This is optional "
                "until that ecosystem is used."
            ),
        )

    # Claude Code hook configuration check.
    claude_code_settings = workspace.root / ".claude" / "settings.json"
    claude_code_hooks_dir = workspace.root / ".claude" / "hooks"
    if claude_code_settings.exists():
        hook_scripts = (
            sorted(claude_code_hooks_dir.glob("on-*.sh"))
            if claude_code_hooks_dir.exists()
            else []
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

    interaction = interaction_ingest_snapshot(workspace)
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

    overall = READY
    if any(check["status"] == ACTION_REQUIRED for check in checks):
        overall = ACTION_REQUIRED
    elif any(check["status"] == DEGRADED for check in checks):
        overall = DEGRADED

    if any(
        check["name"] == "platform" and check["status"] != READY for check in checks
    ):
        next_steps.append(manual_workspace_recovery_step())

    payload = {
        "status": overall,
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
    return make_report(overall, payload, lines)


def status_workspace(
    paths: WorkspacePaths | None = None,
    *,
    editable_install_probe: EditableInstallProbe = inspect_editable_install,
) -> CommandReport:
    """Report the current workspace stage and pending operator actions."""
    workspace = paths or locate_workspace()
    maybe_reconcile_active_thread(workspace)
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
    ]
    if pending_actions:
        lines.append(f"Pending actions: {', '.join(pending_actions)}")

    exit_code = 0 if stage == "knowledge-base-present" else 2
    return CommandReport(exit_code=exit_code, payload=payload, lines=lines)


def sync_workspace(
    paths: WorkspacePaths | None = None,
    *,
    autonomous: bool = True,
) -> CommandReport:
    """Stage, validate, and publish the Phase 4 knowledge base."""
    workspace = paths or locate_workspace()
    maybe_reconcile_active_thread(workspace)
    result = run_phase4_sync(workspace, autonomous=autonomous)
    status = validation_command_status(result["status"])
    next_workflows: list[str] = []
    next_steps: list[str] = []
    pending_work_path = None
    if result["status"] == "pending-synthesis":
        pending_work_path = "knowledge_base/staging/pending_work.json"
        next_workflows = ["knowledge-construction", "knowledge-base-sync"]
        next_steps.append(
            "Complete staged authoring from "
            "`knowledge_base/staging/pending_work.json`, then rerun "
            "`docmason sync` or `docmason workflow knowledge-base-sync`."
        )
    elif result["status"] == "blocking-errors":
        next_workflows = ["validation-repair", "knowledge-base-sync"]
        next_steps.append(
            "Repair staged validation blockers, then rerun `docmason sync` "
            "or `docmason workflow knowledge-base-sync`."
        )
    elif result["status"] == "action-required":
        next_steps.append(result["detail"])
    payload = {
        "status": status,
        "sync_status": result["status"],
        "detail": result["detail"],
        "pending_sources": result["pending_sources"],
        "validation": result["validation"],
        "published": result["published"],
        "interaction_ingest": result.get("interaction_ingest", {}),
        "rebuilt": result.get("rebuilt", False),
        "build_stats": result.get("build_stats", {}),
        "change_set": result.get("change_set", {}),
        "auto_repairs": result.get("auto_repairs", {}),
        "auto_authoring": result.get("auto_authoring", {}),
        "autonomous_steps": result.get("autonomous_steps", []),
        "required_capabilities": result.get("required_capabilities", []),
        "pending_work_path": pending_work_path,
        "next_workflows": next_workflows,
        "next_steps": next_steps,
    }
    lines = [
        f"Sync status: {status}",
        result["detail"],
        f"Staging rebuilt: {'yes' if result.get('rebuilt', False) else 'no'}",
        f"Published: {'yes' if result['published'] else 'no'}",
    ]
    build_stats = result.get("build_stats", {})
    if isinstance(build_stats, dict):
        lines.append(
            "Build stats: "
            f"reused={build_stats.get('reused_sources', 0)}, "
            f"rebuilt={build_stats.get('rebuilt_sources', 0)}"
        )
    auto_repairs = result.get("auto_repairs", {})
    if isinstance(auto_repairs, dict):
        lines.append("Auto repairs: " f"total={auto_repairs.get('repair_count', 0)}")
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
    if result["pending_sources"]:
        lines.append(
            "Pending synthesis: "
            + ", ".join(source["source_id"] for source in result["pending_sources"])
        )
    if next_steps:
        lines.append("Next steps: " + " ".join(next_steps))
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
    maybe_reconcile_active_thread(workspace)
    try:
        result = retrieve_corpus(
            workspace,
            query=query,
            top=max(top, 1),
            graph_hops=max(graph_hops, 0),
            document_types=document_types,
            source_ids=source_ids,
            include_renders=include_renders,
        )
    except FileNotFoundError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "retrieve_status": "artifacts-missing",
            "detail": str(exc),
        }
        lines = [
            f"Retrieve status: {ACTION_REQUIRED}",
            str(exc),
            "Next step: run `docmason sync` to rebuild retrieval artifacts.",
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    status = READY if result["results"] else DEGRADED
    payload = {
        "status": status,
        "retrieve_status": result["status"],
        **result,
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
    for index, item in enumerate(result["results"], start=1):
        lines.append(
            f"{index}. {item.get('title') or item['source_id']} [score={item['score']['total']}]"
        )
    if not result["results"]:
        lines.append("No grounded retrieval results were found for the query.")
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
    maybe_reconcile_active_thread(workspace)
    try:
        if source_id is not None:
            result = trace_source(workspace, source_id=source_id, unit_id=unit_id)
            status = READY
            payload = {"status": status, **result}
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
            return make_report(status, payload, lines)

        if answer_file is not None:
            answer_file_path = Path(answer_file)
            if not answer_file_path.is_absolute():
                answer_file_path = workspace.root / answer_file_path
            result = trace_answer_file(
                workspace,
                answer_file=answer_file_path,
                top=max(top, 1),
            )
        elif session_id is not None:
            result = trace_session(
                workspace,
                session_id=session_id,
                top=max(top, 1),
            )
        else:  # pragma: no cover - protected by argparse
            raise ValueError("One trace entrypoint must be selected.")
    except FileNotFoundError as exc:
        payload = {
            "status": ACTION_REQUIRED,
            "trace_status": "artifacts-missing",
            "detail": str(exc),
        }
        lines = [
            f"Trace status: {ACTION_REQUIRED}",
            str(exc),
            "Next step: run `docmason sync` to rebuild trace artifacts or logs.",
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    except KeyError as exc:
        payload = {"status": ACTION_REQUIRED, "trace_status": "not-found", "detail": str(exc)}
        lines = [
            f"Trace status: {ACTION_REQUIRED}",
            f"Unknown trace target: {exc}",
        ]
        return make_report(ACTION_REQUIRED, payload, lines)
    except ValueError as exc:
        payload = {"status": ACTION_REQUIRED, "trace_status": "invalid-input", "detail": str(exc)}
        lines = [
            f"Trace status: {ACTION_REQUIRED}",
            str(exc),
        ]
        return make_report(ACTION_REQUIRED, payload, lines)

    status = READY if result["status"] == "ready" else DEGRADED
    payload = {"status": status, **result}
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
    return make_report(status, payload, lines)


def validate_knowledge_base(
    paths: WorkspacePaths | None = None,
    *,
    target: str | None = None,
) -> CommandReport:
    """Validate the staged or published knowledge base."""
    workspace = paths or locate_workspace()
    maybe_reconcile_active_thread(workspace)
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

    validation = validate_workspace(workspace, target=resolved_target)
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
    return make_report(status, payload, lines)


def build_claude_root_content(paths: WorkspacePaths) -> str:
    """Render the generated root Claude memory file.

    The committed ``.claude/CLAUDE.md`` already imports ``@../AGENTS.md``
    and ``@../adapters/claude/project-memory.md``.  The generated root
    ``CLAUDE.md`` avoids duplicating those imports and instead focuses on
    summarizing the canonical adapter surface for quick reference.
    """
    return "\n".join(
        [
            "# DocMason Claude Adapter",
            "",
            "This file is generated by `docmason sync-adapters --target claude`.",
            "Do not edit it manually. Regenerate it from canonical committed sources.",
            "",
            "@AGENTS.md",
            "@adapters/claude/project-memory.md",
            "",
        ]
    )


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

    source_inputs = workspace.adapter_source_inputs()
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
    refresh_generated_connector_manifests(workspace)
    workspace.claude_root_path.write_text(build_claude_root_content(workspace), encoding="utf-8")
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


def review_runtime_logs(paths: WorkspacePaths | None = None) -> CommandReport:
    """Refresh and summarize the current runtime review artifacts."""
    workspace = paths or locate_workspace()
    maybe_reconcile_active_thread(workspace)
    summary = refresh_log_review_summary(workspace)
    recent_conversations = summary.get("conversations", {}).get("recent", [])
    recent_query_sessions = summary.get("query_sessions", {}).get("recent", [])
    candidates = read_json(workspace.benchmark_candidates_path).get("candidates", [])
    if not recent_conversations and not recent_query_sessions:
        payload = {
            "status": DEGRADED,
            "review_summary": summary,
            "benchmark_candidates": {"candidate_count": len(candidates), "candidates": candidates},
        }
        lines = [
            f"Runtime review status: {DEGRADED}",
            "No recent workflow-linked query, trace, or conversation activity is available yet.",
            (
                "Run retrieval, trace, ask, or workflow-linked repository activity "
                "before expecting a populated review summary."
            ),
        ]
        return make_report(DEGRADED, payload, lines)

    payload = {
        "status": READY,
        "review_summary": summary,
        "benchmark_candidates": {"candidate_count": len(candidates), "candidates": candidates},
    }
    lines = [
        f"Runtime review status: {READY}",
        f"Recent conversations: {len(recent_conversations)}",
        f"Recent query sessions: {len(recent_query_sessions)}",
        f"Benchmark candidates: {len(candidates)}",
    ]
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
        steps.append(("prepare", prepare_workspace(workspace)))
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
        operator_payload, operator_lines = run_operator_eval(workspace)
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
        if sync_status == "pending-synthesis":
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

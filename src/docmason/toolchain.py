"""Repo-local Python toolchain inspection helpers for prepared-workspace operation."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .project import WorkspacePaths

PREPARED_WORKSPACE_PYTHON_BASELINE = "3.13"
PREPARED_WORKSPACE_PYTHON_MINOR = (3, 13)
TOOLCHAIN_STATE_SCHEMA_VERSION = 1

TOOLCHAIN_MODE_VALUES = frozenset(
    {"repo-local-managed", "shared-host-bootstrap", "legacy-external", "missing"}
)
VENV_HEALTH_VALUES = frozenset(
    {"ready", "missing", "broken-symlink", "external-provenance", "import-failed"}
)
ENTRYPOINT_HEALTH_VALUES = frozenset(
    {"ready", "broken-shebang", "startup-silent", "module-import-failed"}
)
ISOLATION_GRADE_VALUES = frozenset({"self-contained", "mixed", "degraded"})
ENTRYPOINT_PROBE_TIMEOUT_SECONDS = 3.0
PYTHON_VERSION_PROBE_TIMEOUT_SECONDS = 3.0

_INSTALL_ROOT_VERSION_PATTERN = re.compile(r"(\d+\.\d+(?:\.\d+)?)")
_PYTHON_EXECUTABLE_NAME_PATTERN = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")


@dataclass(frozen=True)
class ProbeExecution:
    """Captured result of one bounded toolchain subprocess probe."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    os_error: str | None = None


ProbeRunner = Callable[[Sequence[str], Path, float], ProbeExecution]

def _resolved_path(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except OSError:
        return None
    except FileNotFoundError:
        return None


def _path_within(path: Path | None, root: Path) -> bool:
    if path is None:
        return False
    resolved_path = _resolved_path(path) or path.resolve(strict=False)
    resolved_root = _resolved_path(root) or root.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _read_shebang(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            first_line = handle.readline().strip()
    except UnicodeDecodeError:
        return None
    if not first_line.startswith("#!"):
        return None
    return first_line[2:].strip() or None


def _parse_python_version(value: str | None) -> tuple[int, int] | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def _default_probe_runner(
    command: Sequence[str], cwd: Path, timeout_seconds: float
) -> ProbeExecution:
    probe_env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME"}
    }
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=probe_env,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.strip() if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr.strip() if isinstance(exc.stderr, str) else ""
        return ProbeExecution(
            exit_code=-1,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    except OSError as exc:
        return ProbeExecution(
            exit_code=-1,
            stderr=exc.strerror or str(exc),
            os_error=exc.strerror or str(exc),
        )
    return ProbeExecution(
        exit_code=completed.returncode,
        stdout=completed.stdout.strip(),
        stderr=completed.stderr.strip(),
    )


def _same_path(left: Path | None, right: Path | None) -> bool:
    if left is None or right is None:
        return False
    left_path = _resolved_path(left) or left.resolve(strict=False)
    right_path = _resolved_path(right) or right.resolve(strict=False)
    return left_path == right_path


def _probe_failure_detail(execution: ProbeExecution) -> str:
    if execution.timed_out:
        return "probe timed out"
    if execution.os_error:
        return execution.os_error
    return execution.stderr or execution.stdout or "no output"


def _toolchain_install_root_mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _version_from_install_root(install_root: Path | None) -> str | None:
    if install_root is None:
        return None
    match = _INSTALL_ROOT_VERSION_PATTERN.search(install_root.name)
    if not match:
        return None
    return match.group(1)


def _managed_python_executable_from_root(install_root: Path | None) -> Path | None:
    if install_root is None:
        return None
    bin_dir = install_root / "bin"
    if not bin_dir.exists():
        return None
    install_version = _version_from_install_root(install_root)
    preferred: list[Path] = []
    if isinstance(install_version, str) and install_version:
        minor_version = ".".join(install_version.split(".")[:2])
        preferred.append(bin_dir / f"python{minor_version}")
    preferred.extend((bin_dir / "python3", bin_dir / "python"))
    for executable in preferred:
        if executable.exists():
            return executable
    candidates = sorted(
        (
            candidate
            for candidate in bin_dir.iterdir()
            if candidate.is_file()
            and _PYTHON_EXECUTABLE_NAME_PATTERN.match(candidate.name)
        ),
        key=lambda candidate: candidate.name,
    )
    return candidates[0] if candidates else None


def _latest_managed_python_root(paths: WorkspacePaths) -> Path | None:
    current_root = managed_python_install_root(paths)
    if current_root is not None:
        return current_root
    if not paths.toolchain_python_installs_dir.exists():
        return None
    candidates = sorted(
        (
            candidate
            for candidate in paths.toolchain_python_installs_dir.iterdir()
            if candidate.is_dir()
        ),
        key=_toolchain_install_root_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _probe_python_version(
    executable: str | None,
    *,
    cwd: Path,
    probe_runner: ProbeRunner | None = None,
) -> str | None:
    if not isinstance(executable, str) or not executable:
        return None
    runner = probe_runner or _default_probe_runner
    execution = runner(
        [
            executable,
            "-c",
            (
                "import sys; "
                "print('.'.join(str(part) for part in sys.version_info[:3]))"
            ),
        ],
        cwd,
        PYTHON_VERSION_PROBE_TIMEOUT_SECONDS,
    )
    if execution.timed_out or execution.exit_code != 0:
        return None
    version = execution.stdout.splitlines()[0].strip() if execution.stdout else ""
    return version or None


def inspect_entrypoint(
    paths: WorkspacePaths,
    *,
    probe_runner: ProbeRunner | None = None,
    timeout_seconds: float = ENTRYPOINT_PROBE_TIMEOUT_SECONDS,
) -> dict[str, str | None]:
    """Actively classify repo-local DocMason entrypoint health."""
    docmason_entry_shebang = _read_shebang(paths.venv_docmason)
    if not paths.venv_docmason.exists():
        return {
            "entrypoint_health": "module-import-failed",
            "detail": "The repo-local DocMason entrypoint executable is missing.",
        }
    if not docmason_entry_shebang:
        return {
            "entrypoint_health": "broken-shebang",
            "detail": "The repo-local DocMason entrypoint is missing a valid shebang.",
        }
    shebang_target = docmason_entry_shebang.split(" ", 1)[0]
    if not os.path.isabs(shebang_target):
        return {
            "entrypoint_health": "broken-shebang",
            "detail": "The repo-local DocMason entrypoint uses a non-absolute shebang target.",
        }
    if not Path(shebang_target).exists():
        return {
            "entrypoint_health": "broken-shebang",
            "detail": "The repo-local DocMason entrypoint shebang target no longer exists.",
        }

    runner = probe_runner or _default_probe_runner
    import_probe = runner(
        [str(paths.venv_python), "-c", "import docmason"],
        paths.root,
        timeout_seconds,
    )
    if import_probe.timed_out or import_probe.exit_code != 0:
        return {
            "entrypoint_health": "module-import-failed",
            "detail": (
                "The repo-local DocMason module import failed inside `.venv`: "
                f"{_probe_failure_detail(import_probe)}"
            ),
        }

    launcher_probe = runner(
        [str(paths.venv_docmason), "--help"],
        paths.root,
        timeout_seconds,
    )
    if launcher_probe.timed_out:
        return {
            "entrypoint_health": "startup-silent",
            "detail": (
                "The repo-local DocMason launcher timed out before returning a healthy "
                "startup response."
            ),
        }
    if launcher_probe.exit_code != 0:
        return {
            "entrypoint_health": "startup-silent",
            "detail": (
                "The repo-local DocMason launcher failed before returning a healthy startup "
                f"response: {_probe_failure_detail(launcher_probe)}"
            ),
        }
    if not (launcher_probe.stdout or launcher_probe.stderr):
        return {
            "entrypoint_health": "startup-silent",
            "detail": (
                "The repo-local DocMason launcher returned no output during the startup "
                "health probe."
            ),
        }
    return {"entrypoint_health": "ready", "detail": None}


def toolchain_repair_detail(toolchain: dict[str, Any]) -> str:
    """Return a concise operator-facing detail string for one non-ready toolchain state."""
    reason = str(toolchain.get("repair_reason") or "environment-not-ready")
    details = {
        "missing-venv": (
            "The repo-local virtual environment is missing and must be rebuilt against "
            "repo-local managed Python 3.13."
        ),
        "broken-venv-symlink": (
            "The repo-local virtual environment points to a broken interpreter path and must "
            "be rebuilt."
        ),
        "missing-managed-python": (
            "The repo-local managed Python 3.13 toolchain is missing and must be reprovisioned."
        ),
        "external-venv-provenance": (
            "The repo-local `.venv` is anchored to an external interpreter and must be rebuilt "
            "against repo-local managed Python 3.13."
        ),
        "entrypoint-broken": (
            "The repo-local DocMason entrypoint chain is broken and must be repaired."
        ),
        "baseline-version-drift": (
            "The prepared workspace no longer matches the repo-local Python 3.13 baseline and "
            "must be rebuilt."
        ),
        "package-install-drift": (
            "The editable DocMason install inside the repo-local `.venv` is drifted or missing "
            "and must be repaired."
        ),
    }
    return details.get(
        reason,
        "The cached bootstrap marker does not describe a self-contained workspace.",
    )


def managed_python_executable_path(paths: WorkspacePaths) -> Path:
    """Return the canonical repo-local managed Python executable path."""
    return (
        paths.toolchain_python_current_dir
        / "bin"
        / f"python{PREPARED_WORKSPACE_PYTHON_BASELINE}"
    )


def managed_python_install_root(paths: WorkspacePaths) -> Path | None:
    """Return the current managed Python install root when the symlink is usable."""
    resolved = _resolved_path(paths.toolchain_python_current_dir)
    if resolved is None or not resolved.exists():
        return None
    return resolved


def latest_managed_python_candidate(paths: WorkspacePaths) -> Path | None:
    """Return the newest repo-local managed Python candidate executable."""
    if not paths.toolchain_python_installs_dir.exists():
        return None
    candidates = sorted(
        paths.toolchain_python_installs_dir.glob(
            f"**/bin/python{PREPARED_WORKSPACE_PYTHON_BASELINE}"
        ),
        key=lambda candidate: candidate.stat().st_mtime_ns if candidate.exists() else -1,
        reverse=True,
    )
    return candidates[0] if candidates else None


def venv_base_executable(paths: WorkspacePaths, state: dict[str, Any] | None = None) -> str | None:
    """Return the best-known base executable behind `.venv/bin/python`."""
    if isinstance(state, dict):
        value = state.get("venv_base_executable")
        if isinstance(value, str) and value:
            return value
    resolved = _resolved_path(paths.venv_python)
    if resolved is not None:
        return str(resolved)
    return None


def _managed_python_origin(paths: WorkspacePaths, executable: Path | None) -> str:
    if executable is None:
        return "missing"
    if _path_within(executable, paths.toolchain_python_dir):
        return "repo-local-managed"
    return "shared-host"


def _shared_host_bootstrap_executable(paths: WorkspacePaths) -> Path | None:
    if not sys.executable:
        return None
    current_python = Path(sys.executable)
    resolved_current = _resolved_path(current_python) or current_python.resolve(strict=False)
    if _path_within(resolved_current, paths.toolchain_python_dir):
        return None
    if _path_within(resolved_current, paths.toolchain_bootstrap_dir):
        return None
    if _path_within(resolved_current, paths.venv_dir):
        return None
    return resolved_current


def inspect_toolchain(
    paths: WorkspacePaths,
    *,
    bootstrap_state: dict[str, Any] | None = None,
    editable_install: bool | None = None,
) -> dict[str, Any]:
    """Inspect prepared-workspace toolchain provenance and classify readiness."""
    state = bootstrap_state if isinstance(bootstrap_state, dict) else {}
    managed_python_root = _latest_managed_python_root(paths)
    managed_python = _managed_python_executable_from_root(managed_python_root)
    managed_python_executable = (
        str(managed_python)
        if managed_python is not None
        else (
            str(state.get("managed_python_executable"))
            if isinstance(state.get("managed_python_executable"), str)
            and state.get("managed_python_executable")
            else None
        )
    )
    managed_python_version = (
        _version_from_install_root(managed_python_root)
        or (
            str(state.get("managed_python_version"))
            if isinstance(state.get("managed_python_version"), str)
            and state.get("managed_python_version")
            and _same_path(
                (
                    Path(managed_python_executable)
                    if isinstance(managed_python_executable, str)
                    else None
                ),
                (
                    Path(str(state.get("managed_python_executable")))
                    if isinstance(state.get("managed_python_executable"), str)
                    and state.get("managed_python_executable")
                    else None
                ),
            )
            else None
        )
        or _probe_python_version(managed_python_executable, cwd=paths.root)
    )
    baseline_version = _parse_python_version(managed_python_version)
    managed_origin = _managed_python_origin(
        paths,
        Path(managed_python_executable) if isinstance(managed_python_executable, str) else None,
    )
    base_executable = venv_base_executable(paths, state=state)
    venv_exists = paths.venv_python.exists() or paths.venv_python.is_symlink()
    venv_python_target = _resolved_path(paths.venv_python)
    if not venv_exists:
        venv_health = "missing"
    elif venv_python_target is None:
        venv_health = "broken-symlink"
    elif not _path_within(venv_python_target, paths.toolchain_python_dir):
        venv_health = "external-provenance"
    else:
        venv_health = "ready"
    entrypoint = inspect_entrypoint(paths)
    entrypoint_health = str(entrypoint.get("entrypoint_health") or "module-import-failed")
    shared_host_bootstrap = _shared_host_bootstrap_executable(paths)
    if venv_health == "external-provenance":
        toolchain_mode = "legacy-external"
    elif managed_python_executable is None:
        if shared_host_bootstrap is not None:
            toolchain_mode = "shared-host-bootstrap"
        else:
            toolchain_mode = "missing"
    elif managed_origin == "repo-local-managed":
        toolchain_mode = "repo-local-managed"
    else:
        toolchain_mode = "legacy-external"
    shared_host_dependencies: list[str] = []
    if toolchain_mode == "shared-host-bootstrap" and shared_host_bootstrap is not None:
        shared_host_dependencies.append(str(shared_host_bootstrap))
    if managed_origin == "shared-host" and isinstance(managed_python_executable, str):
        shared_host_dependencies.append(managed_python_executable)
    if venv_health == "external-provenance" and isinstance(base_executable, str):
        shared_host_dependencies.append(base_executable)
    shared_host_dependencies.extend(
        value
        for value in state.get("shared_host_dependencies", [])
        if isinstance(value, str) and value
    )
    shared_host_dependencies = list(dict.fromkeys(shared_host_dependencies))
    repair_reason: str | None
    if venv_health == "missing":
        isolation_grade = "degraded"
        repair_reason = "missing-venv"
    elif venv_health == "broken-symlink":
        isolation_grade = "degraded"
        repair_reason = "broken-venv-symlink"
    elif toolchain_mode == "missing":
        isolation_grade = "degraded"
        repair_reason = "missing-managed-python"
    elif venv_health == "external-provenance":
        isolation_grade = "mixed"
        repair_reason = "external-venv-provenance"
    elif entrypoint_health != "ready":
        isolation_grade = "degraded"
        repair_reason = "entrypoint-broken"
    elif baseline_version is not None and baseline_version != PREPARED_WORKSPACE_PYTHON_MINOR:
        isolation_grade = "mixed"
        repair_reason = "baseline-version-drift"
    elif editable_install is False:
        isolation_grade = "degraded"
        repair_reason = "package-install-drift"
    else:
        isolation_grade = "self-contained"
        repair_reason = None
    return {
        "schema_version": TOOLCHAIN_STATE_SCHEMA_VERSION,
        "python_baseline": PREPARED_WORKSPACE_PYTHON_BASELINE,
        "toolchain_root": str(paths.toolchain_dir.relative_to(paths.root)),
        "toolchain_mode": toolchain_mode,
        "managed_python_executable": managed_python_executable,
        "managed_python_version": managed_python_version,
        "managed_python_origin": managed_origin,
        "managed_python_healthy": (
            toolchain_mode == "repo-local-managed" and managed_python is not None
        ),
        "venv_base_executable": base_executable,
        "venv_health": venv_health,
        "venv_healthy": venv_health == "ready",
        "entrypoint_health": entrypoint_health,
        "entrypoint_healthy": entrypoint_health == "ready",
        "isolation_grade": isolation_grade,
        "shared_host_dependency": bool(shared_host_dependencies),
        "shared_host_dependencies": shared_host_dependencies,
        "repair_required": isolation_grade != "self-contained",
        "repair_recommended": isolation_grade != "self-contained",
        "repair_reason": repair_reason,
        "repair_intrusion_class": (
            "repo-local" if repair_reason not in {None, "missing-office-renderer"} else None
        ),
        "toolchain_current_root": (
            str(managed_python_root)
            if managed_python_root is not None
            else None
        ),
        "uv_cache_dir": str(paths.toolchain_uv_cache_dir.relative_to(paths.root)),
        "pip_cache_dir": str(paths.toolchain_pip_cache_dir.relative_to(paths.root)),
    }

"""Shared LibreOffice probing and conversion helpers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

LIBREOFFICE_PROBE_CONTRACT = "libreoffice-docx-pdf-smoke-v1"
_SOFFICE_VERSION_TIMEOUT_SECONDS = 15.0
_SOFFICE_CONVERSION_TIMEOUT_SECONDS = 30.0
_MACOS_LIBREOFFICE_BINARY = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
_DIRECT_LAUNCHER = "direct"
_FULL_ACCESS_GUIDANCE = "Switch Codex to `Full access`, then continue the same task."


def _normalized_command_output(stdout: str | None, stderr: str | None) -> str:
    values: list[str] = []
    for raw in (stderr, stdout):
        text = (raw or "").strip()
        if text and text not in values:
            values.append(text)
    return "\n".join(values).strip()


def _signal_detail(returncode: int, output: str) -> str | None:
    if returncode < 0:
        return f"terminated by signal {-returncode}"
    lowered = output.lower()
    if returncode == 134 and (
        "abort trap: 6" in lowered or "signal 6" in lowered or "abort() called" in lowered
    ):
        return "terminated by signal 6"
    return None


def _format_process_failure(returncode: int, output: str) -> str:
    signal_detail = _signal_detail(returncode, output)
    if signal_detail:
        return signal_detail
    if output:
        return output
    return f"exit code {returncode}"


def current_host_execution_context() -> dict[str, Any]:
    """Return the current host execution context with a minimal fallback path.

    The shared LibreOffice runtime is imported by lightweight bootstrap probes and
    test fixtures that may not ship the full DocMason conversation module. Fall
    back to the small env-derived subset needed for the host-access guard.
    """
    permission_mode = str(os.environ.get("DOCMASON_PERMISSION_MODE") or "").strip()
    host_provider = str(os.environ.get("DOCMASON_AGENT_SURFACE") or "").strip()
    if host_provider or permission_mode:
        return {
            "host_provider": host_provider,
            "permission_mode": permission_mode,
            "full_machine_access": permission_mode == "full-access",
        }

    try:
        from .conversation import current_host_execution_context as conversation_host_context
    except ImportError:
        return {
            "host_provider": host_provider,
            "permission_mode": permission_mode,
            "full_machine_access": False,
        }
    try:
        context = conversation_host_context()
    except Exception:
        return {
            "host_provider": host_provider,
            "permission_mode": permission_mode,
            "full_machine_access": False,
        }
    return context if isinstance(context, dict) else {}


def soffice_candidate_paths() -> tuple[str, ...]:
    """Return existing candidate LibreOffice binaries in canonical search order."""
    candidates: list[str] = []
    seen: set[str] = set()
    for candidate in (
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        str(_MACOS_LIBREOFFICE_BINARY),
    ):
        if not candidate:
            continue
        normalized = str(Path(candidate))
        if normalized in seen:
            continue
        seen.add(normalized)
        if Path(normalized).exists():
            candidates.append(normalized)
    return tuple(candidates)


def discover_soffice_binary() -> str | None:
    """Locate the first existing LibreOffice command candidate."""
    candidates = soffice_candidate_paths()
    return candidates[0] if candidates else None


def _write_minimal_docx_probe(path: Path) -> None:
    """Write a minimal DOCX file for LibreOffice smoke tests."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override
    PartName="/word/document.xml"
    ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
  />
</Types>
""",
        )
        archive.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship
    Id="rId1"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
    Target="word/document.xml"
  />
</Relationships>
""",
        )
        archive.writestr(
            "word/document.xml",
            """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:wpc="http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas"
 xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math"
 xmlns:v="urn:schemas-microsoft-com:vml"
 xmlns:wp14="http://schemas.microsoft.com/office/word/2010/wordprocessingDrawing"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:w10="urn:schemas-microsoft-com:office:word"
 xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"
 xmlns:wpg="http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
 xmlns:wpi="http://schemas.microsoft.com/office/word/2010/wordprocessingInk"
 xmlns:wne="http://schemas.microsoft.com/office/word/2006/wordml"
 xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
 mc:Ignorable="w14 wp14">
  <w:body>
    <w:p>
      <w:r>
        <w:t>DocMason LibreOffice smoke test</w:t>
      </w:r>
    </w:p>
    <w:sectPr>
      <w:pgSz w:w="12240" w:h="15840"/>
      <w:pgMar
        w:top="1440"
        w:right="1440"
        w:bottom="1440"
        w:left="1440"
        w:header="720"
        w:footer="720"
        w:gutter="0"
      />
    </w:sectPr>
  </w:body>
</w:document>
""",
        )


def _isolated_runtime_environment(base_dir: Path, *, label: str) -> tuple[Path, dict[str, str]]:
    profile_dir = base_dir / f"profile-{label}"
    home_dir = base_dir / f"home-{label}"
    tmp_dir = base_dir / f"tmp-{label}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["HOME"] = str(home_dir)
    environment["TMPDIR"] = str(tmp_dir)
    return profile_dir, environment


def _output_path_for_conversion(source_path: Path, output_dir: Path, extension: str) -> Path | None:
    expected = output_dir / f"{source_path.stem}.{extension}"
    if expected.exists():
        return expected
    converted = sorted(
        output_dir.glob(f"*.{extension}"),
        key=lambda candidate: candidate.stat().st_mtime_ns,
    )
    if not converted:
        return None
    return converted[-1]


def _build_direct_conversion_command(
    binary: Path,
    *,
    source_path: Path,
    output_dir: Path,
    target_format: str,
    profile_dir: Path,
) -> list[str]:
    return [
        str(binary),
        f"-env:UserInstallation={profile_dir.resolve().as_uri()}",
        "--headless",
        "--norestore",
        "--nolockcheck",
        "--nodefault",
        "--convert-to",
        target_format,
        "--outdir",
        str(output_dir),
        str(source_path),
    ]


def _run_conversion_attempt(
    *,
    launcher: str,
    command: list[str],
    source_path: Path,
    output_dir: Path,
    extension: str,
    environment: dict[str, str],
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "launcher": launcher,
            "output_path": None,
            "cause": f"timed out after {timeout_seconds:.1f}s",
            "returncode": None,
            "output": "",
        }
    except OSError as exc:
        return {
            "success": False,
            "launcher": launcher,
            "output_path": None,
            "cause": f"failed to execute: {exc}",
            "returncode": None,
            "output": "",
        }

    output = _normalized_command_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        return {
            "success": False,
            "launcher": launcher,
            "output_path": None,
            "cause": _format_process_failure(completed.returncode, output),
            "returncode": completed.returncode,
            "output": output,
        }

    output_path = _output_path_for_conversion(source_path, output_dir, extension)
    if output_path is None:
        return {
            "success": False,
            "launcher": launcher,
            "output_path": None,
            "cause": f"completed without producing a .{extension} output",
            "returncode": completed.returncode,
            "output": output,
        }

    return {
        "success": True,
        "launcher": launcher,
        "output_path": output_path,
        "cause": None,
        "returncode": completed.returncode,
        "output": output,
    }


def _libreoffice_host_access_block(*, candidate_binary: str | None) -> dict[str, Any] | None:
    """Return a structured block when this Codex thread cannot spawn LibreOffice safely."""
    if sys.platform != "darwin":
        return None

    host_execution = current_host_execution_context()
    if str(host_execution.get("host_provider") or "") != "codex":
        return None
    if str(host_execution.get("permission_mode") or "") != "default-permissions":
        return None
    if bool(host_execution.get("full_machine_access")):
        return None

    candidate_detail = (
        f" Detected candidate: `{candidate_binary}`."
        if isinstance(candidate_binary, str) and candidate_binary
        else ""
    )
    return {
        "detail": (
            "DocMason is currently running in Codex `Default permissions` on macOS, so it "
            "needs `Full access` before it can continue Office rendering through LibreOffice."
            f"{candidate_detail}"
        ),
        "blocked_by_host_access": True,
        "host_access_required": True,
        "host_access_guidance": _FULL_ACCESS_GUIDANCE,
    }


def run_office_conversion(
    source_path: Path,
    output_dir: Path,
    soffice_binary: str,
    *,
    target_format: str,
    timeout_seconds: float = _SOFFICE_CONVERSION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run a LibreOffice conversion with isolated runtime state."""
    binary = Path(soffice_binary)
    extension = target_format.split(":", 1)[0].lower()
    output_dir.mkdir(parents=True, exist_ok=True)

    host_access_block = _libreoffice_host_access_block(candidate_binary=str(binary))
    if host_access_block is not None:
        return {
            "success": False,
            "launcher": None,
            "output_path": None,
            "cause": host_access_block["detail"],
            "attempts": [],
            "blocked_by_host_access": True,
            "host_access_required": True,
            "host_access_guidance": host_access_block["host_access_guidance"],
        }

    with tempfile.TemporaryDirectory(prefix="docmason-libreoffice-runtime-") as tempdir_name:
        runtime_root = Path(tempdir_name)
        direct_profile_dir, direct_environment = _isolated_runtime_environment(
            runtime_root,
            label=_DIRECT_LAUNCHER,
        )
        direct_command = _build_direct_conversion_command(
            binary,
            source_path=source_path,
            output_dir=output_dir,
            target_format=target_format,
            profile_dir=direct_profile_dir,
        )
        direct_attempt = _run_conversion_attempt(
            launcher=_DIRECT_LAUNCHER,
            command=direct_command,
            source_path=source_path,
            output_dir=output_dir,
            extension=extension,
            environment=direct_environment,
            timeout_seconds=timeout_seconds,
        )
        if direct_attempt["success"]:
            return {
                "success": True,
                "launcher": _DIRECT_LAUNCHER,
                "output_path": direct_attempt["output_path"],
                "cause": None,
                "attempts": [direct_attempt],
                "blocked_by_host_access": False,
                "host_access_required": False,
                "host_access_guidance": None,
            }

        cause = str(direct_attempt.get("cause") or "conversion failed")
        return {
            "success": False,
            "launcher": None,
            "output_path": None,
            "cause": cause,
            "attempts": [direct_attempt],
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }


def _validate_detected_soffice_binary(binary: Path) -> dict[str, Any]:
    if not binary.exists():
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": None,
            "detail": "The detected LibreOffice command path does not exist.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }
    if not os.access(binary, os.X_OK):
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": None,
            "detail": "The detected LibreOffice command path is not executable.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }

    host_access_block = _libreoffice_host_access_block(candidate_binary=str(binary))
    if host_access_block is not None:
        return {
            "ready": False,
            "binary": None,
            "candidate_binary": str(binary),
            "version": None,
            "detail": host_access_block["detail"],
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": True,
            "host_access_required": True,
            "host_access_guidance": host_access_block["host_access_guidance"],
        }

    try:
        completed = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SOFFICE_VERSION_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": None,
            "detail": (
                "The detected LibreOffice command timed out during the version probe "
                f"after {_SOFFICE_VERSION_TIMEOUT_SECONDS:.1f}s."
            ),
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }
    except OSError as exc:
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": None,
            "detail": f"The detected LibreOffice command failed to execute: {exc}.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }

    output = _normalized_command_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": None,
            "detail": (
                "The detected LibreOffice command failed the version probe: "
                f"{_format_process_failure(completed.returncode, output)}."
            ),
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }
    if "libreoffice" not in output.lower():
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": output or None,
            "detail": "The detected command did not identify itself as LibreOffice.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }

    with tempfile.TemporaryDirectory(prefix="docmason-libreoffice-probe-") as tempdir_name:
        tempdir = Path(tempdir_name)
        source_path = tempdir / "probe.docx"
        output_dir = tempdir / "out"
        _write_minimal_docx_probe(source_path)
        conversion = run_office_conversion(
            source_path,
            output_dir,
            str(binary),
            target_format="pdf",
        )
    if not conversion["success"]:
        return {
            "ready": False,
            "binary": str(binary),
            "candidate_binary": str(binary),
            "version": output or None,
            "detail": (
                "The detected LibreOffice command failed the conversion smoke test: "
                f"{conversion['cause']}."
            ),
            "launcher": conversion.get("launcher"),
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "blocked_by_host_access": bool(conversion.get("blocked_by_host_access")),
            "host_access_required": bool(conversion.get("host_access_required")),
            "host_access_guidance": conversion.get("host_access_guidance"),
        }
    return {
        "ready": True,
        "binary": str(binary),
        "candidate_binary": str(binary),
        "version": output or None,
        "detail": "Validated LibreOffice renderer capability.",
        "launcher": conversion.get("launcher"),
        "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        "blocked_by_host_access": False,
        "host_access_required": False,
        "host_access_guidance": None,
    }


def validate_soffice_binary(candidate: str | None) -> dict[str, Any]:
    """Validate that a candidate command is a usable LibreOffice renderer."""
    if candidate:
        validation = _validate_detected_soffice_binary(Path(candidate).expanduser())
        validation["attempts"] = [dict(validation)]
        validation["candidate_failures"] = [] if validation["ready"] else [dict(validation)]
        return validation

    candidates = soffice_candidate_paths()
    if not candidates:
        return {
            "ready": False,
            "binary": None,
            "candidate_binary": None,
            "version": None,
            "detail": "No LibreOffice command candidate was detected.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "attempts": [],
            "candidate_failures": [],
            "blocked_by_host_access": False,
            "host_access_required": False,
            "host_access_guidance": None,
        }

    attempts: list[dict[str, Any]] = []
    for candidate_path in candidates:
        validation = _validate_detected_soffice_binary(Path(candidate_path))
        attempts.append(dict(validation))
        if validation["ready"]:
            validation["attempts"] = attempts
            validation["candidate_failures"] = [
                attempt for attempt in attempts if not bool(attempt.get("ready"))
            ]
            return validation
        if validation.get("blocked_by_host_access"):
            validation["attempts"] = attempts
            validation["candidate_failures"] = attempts
            return validation

    first_failure = dict(attempts[0])
    first_failure["attempts"] = attempts
    first_failure["candidate_failures"] = attempts
    return first_failure


def find_soffice_binary() -> str | None:
    """Return the first validated LibreOffice binary from the canonical candidate order."""
    validation = validate_soffice_binary(None)
    if validation["ready"]:
        return str(validation["binary"])
    return None

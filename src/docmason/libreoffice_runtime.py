"""Shared LibreOffice probing and conversion helpers."""

from __future__ import annotations

import contextlib
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
_MACOS_LIBREOFFICE_APP = Path("/Applications/LibreOffice.app")
_MACOS_LIBREOFFICE_BINARY = _MACOS_LIBREOFFICE_APP / "Contents" / "MacOS" / "soffice"
_DIRECT_LAUNCHER = "direct"
_LAUNCHSERVICES_LAUNCHER = "launchservices"


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


def _is_known_macos_startup_abort(returncode: int | None, output: str) -> bool:
    if sys.platform != "darwin" or returncode is None:
        return False
    normalized_output = output.lower()
    if returncode in {-6, 134}:
        return True
    return (
        "abort trap: 6" in normalized_output
        or "terminated by signal 6" in normalized_output
        or "signal 6" in normalized_output
        or "abort() called" in normalized_output
    )


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


def _launchservices_app_path(binary: Path) -> Path | None:
    with contextlib.suppress(OSError):
        resolved = binary.resolve()
        if (
            resolved.name == "soffice"
            and resolved.parent.name == "MacOS"
            and resolved.parent.parent.name == "Contents"
        ):
            app_bundle = resolved.parent.parent.parent
            if app_bundle.name.endswith(".app") and app_bundle.exists():
                return app_bundle
    if _MACOS_LIBREOFFICE_APP.exists():
        return _MACOS_LIBREOFFICE_APP
    return None


def _build_launchservices_conversion_command(
    *,
    app_path: Path,
    source_path: Path,
    output_dir: Path,
    target_format: str,
    profile_dir: Path,
) -> list[str]:
    return [
        "/usr/bin/open",
        "-W",
        "-n",
        "-a",
        str(app_path),
        "--args",
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
            }

        attempts = [direct_attempt]
        if _is_known_macos_startup_abort(
            direct_attempt.get("returncode"),
            str(direct_attempt.get("output") or direct_attempt.get("cause") or ""),
        ):
            app_path = _launchservices_app_path(binary)
            if app_path is not None:
                fallback_profile_dir, fallback_environment = _isolated_runtime_environment(
                    runtime_root,
                    label=_LAUNCHSERVICES_LAUNCHER,
                )
                fallback_command = _build_launchservices_conversion_command(
                    app_path=app_path,
                    source_path=source_path,
                    output_dir=output_dir,
                    target_format=target_format,
                    profile_dir=fallback_profile_dir,
                )
                fallback_attempt = _run_conversion_attempt(
                    launcher=_LAUNCHSERVICES_LAUNCHER,
                    command=fallback_command,
                    source_path=source_path,
                    output_dir=output_dir,
                    extension=extension,
                    environment=fallback_environment,
                    timeout_seconds=timeout_seconds,
                )
                attempts.append(fallback_attempt)
                if fallback_attempt["success"]:
                    return {
                        "success": True,
                        "launcher": _LAUNCHSERVICES_LAUNCHER,
                        "output_path": fallback_attempt["output_path"],
                        "cause": None,
                        "attempts": attempts,
                    }

        cause = str(direct_attempt.get("cause") or "conversion failed")
        if len(attempts) > 1:
            fallback_attempt = attempts[-1]
            cause = (
                f"direct start failed with {direct_attempt['cause']}; "
                f"LaunchServices fallback failed with {fallback_attempt['cause']}"
            )
        return {
            "success": False,
            "launcher": None,
            "output_path": None,
            "cause": cause,
            "attempts": attempts,
        }


def _validate_detected_soffice_binary(binary: Path) -> dict[str, Any]:
    if not binary.exists():
        return {
            "ready": False,
            "binary": str(binary),
            "version": None,
            "detail": "The detected LibreOffice command path does not exist.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        }
    if not os.access(binary, os.X_OK):
        return {
            "ready": False,
            "binary": str(binary),
            "version": None,
            "detail": "The detected LibreOffice command path is not executable.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
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
            "version": None,
            "detail": (
                "The detected LibreOffice command timed out during the version probe "
                f"after {_SOFFICE_VERSION_TIMEOUT_SECONDS:.1f}s."
            ),
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        }
    except OSError as exc:
        return {
            "ready": False,
            "binary": str(binary),
            "version": None,
            "detail": f"The detected LibreOffice command failed to execute: {exc}.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        }
    output = _normalized_command_output(completed.stdout, completed.stderr)
    if completed.returncode != 0:
        return {
            "ready": False,
            "binary": str(binary),
            "version": None,
            "detail": (
                "The detected LibreOffice command failed the version probe: "
                f"{_format_process_failure(completed.returncode, output)}."
            ),
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        }
    if "libreoffice" not in output.lower():
        return {
            "ready": False,
            "binary": str(binary),
            "version": output or None,
            "detail": "The detected command did not identify itself as LibreOffice.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
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
            "version": output or None,
            "detail": (
                "The detected LibreOffice command failed the conversion smoke test: "
                f"{conversion['cause']}."
            ),
            "launcher": conversion.get("launcher"),
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
        }
    return {
        "ready": True,
        "binary": str(binary),
        "version": output or None,
        "detail": "Validated LibreOffice renderer capability.",
        "launcher": conversion.get("launcher"),
        "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
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
            "version": None,
            "detail": "No LibreOffice command candidate was detected.",
            "launcher": None,
            "probe_contract": LIBREOFFICE_PROBE_CONTRACT,
            "attempts": [],
            "candidate_failures": [],
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

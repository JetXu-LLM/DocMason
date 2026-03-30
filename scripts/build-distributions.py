#!/usr/bin/env python3
"""Build release-ready clean and demo workspace bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

COMMON_TOP_LEVEL_EXCLUDES = {
    ".docmason",
    ".git",
    ".githooks",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "build",
    "dist",
    "evals",
    "planning",
    "sample_corpus",
    "tests",
}
EXCLUDED_SUBTREES = {
    Path("skills") / "optional",
    Path("scripts") / "private",
}
INCLUDED_TRACKED_FILES = {
    Path(".github") / "copilot-instructions.md",
}

BUNDLE_CHANNELS = {
    "clean": {
        "asset_name": "DocMason-clean.zip",
        "inject_demo_corpus": False,
        "distribution_name": "clean",
    },
    "demo-ico-gcs": {
        "asset_name": "DocMason-demo-ico-gcs.zip",
        "inject_demo_corpus": True,
        "distribution_name": "demo-ico-gcs",
    },
}

PROTECTED_PATHS = ["original_doc", "knowledge_base", "runtime", "adapters"]
EXACT_PATH_EXCLUDES = {"scripts/install-git-hooks.sh"}
RELEASE_ENTRY_SCHEMA_VERSION = 1
RELEASE_ENTRY_STATE_SCHEMA_VERSION = 1
DEFAULT_RELEASE_ENTRY_SCOPE = "canonical-ask"
DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS = 20


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def tracked_files(repo_root: Path) -> list[Path]:
    git_dir = repo_root / ".git"
    if git_dir.exists():
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_root,
            check=True,
            capture_output=True,
        )
        tracked = [Path(item.decode("utf-8")) for item in result.stdout.split(b"\0") if item]
        for relative_path in INCLUDED_TRACKED_FILES:
            if (repo_root / relative_path).exists() and relative_path not in tracked:
                tracked.append(relative_path)
        return sorted(tracked)
    return sorted(
        path.relative_to(repo_root)
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )


def git_output(repo_root: Path, command: list[str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def default_source_commit(repo_root: Path) -> str | None:
    return git_output(repo_root, ["git", "rev-parse", "HEAD"])


def default_source_ref(repo_root: Path) -> str | None:
    symbolic = git_output(repo_root, ["git", "symbolic-ref", "-q", "HEAD"])
    if symbolic:
        return symbolic
    exact_tag = git_output(repo_root, ["git", "describe", "--tags", "--exact-match"])
    if exact_tag:
        return f"refs/tags/{exact_tag}"
    return None


def should_copy(relative_path: Path) -> bool:
    if not relative_path.parts:
        return False
    if relative_path in INCLUDED_TRACKED_FILES:
        return True
    if str(relative_path) in EXACT_PATH_EXCLUDES:
        return False
    for subtree in EXCLUDED_SUBTREES:
        try:
            relative_path.relative_to(subtree)
        except ValueError:
            continue
        return False
    return relative_path.parts[0] not in COMMON_TOP_LEVEL_EXCLUDES


def stage_bundle(
    repo_root: Path,
    staging_root: Path,
    *,
    version: str,
    channel: str,
    github_repo: str | None,
    update_service_url: str | None,
    source_commit: str | None,
    source_ref: str | None,
) -> None:
    config = BUNDLE_CHANNELS[channel]
    for relative_path in tracked_files(repo_root):
        if not should_copy(relative_path):
            continue
        source = repo_root / relative_path
        destination = staging_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    if config["inject_demo_corpus"]:
        demo_source = repo_root / "sample_corpus" / "ico-gcs"
        demo_target = staging_root / "original_doc"
        gitkeep = demo_target / ".gitkeep"
        if gitkeep.exists():
            gitkeep.unlink()
        for child_name in ("ico", "gcs"):
            shutil.copytree(demo_source / child_name, demo_target / child_name, dirs_exist_ok=True)

    manifest_path = staging_root / "distribution-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "distribution_channel": config["distribution_name"],
                "asset_name": config["asset_name"],
                "release_entry": {
                    "schema_version": RELEASE_ENTRY_SCHEMA_VERSION,
                    "update_service_url": update_service_url,
                    "distribution_channel": config["distribution_name"],
                    "automatic_check_scope": DEFAULT_RELEASE_ENTRY_SCOPE,
                    "automatic_check_cooldown_hours": DEFAULT_RELEASE_ENTRY_COOLDOWN_HOURS,
                    "automatic_check_enabled_by_default": True,
                    "asset_name": config["asset_name"],
                },
                "source_version": version,
                "source_repo": github_repo,
                "source_commit": source_commit,
                "source_ref": source_ref,
                "generated_at": utc_now(),
                "protected_paths": PROTECTED_PATHS,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    release_client_state_path = staging_root / "runtime" / "state" / "release-client.json"
    release_client_state_path.parent.mkdir(parents=True, exist_ok=True)
    release_client_state_path.write_text(
        json.dumps(
            {
                "schema_version": RELEASE_ENTRY_STATE_SCHEMA_VERSION,
                "automatic_check_enabled": True,
                "installation_hash": None,
                "created_at": None,
                "last_check_attempted_at": None,
                "next_eligible_at": None,
                "last_known_latest_version": None,
                "last_notified_version": None,
                "last_check_status": None,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def sha256_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_zip(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source_dir.rglob("*")):
            archive.write(path, path.relative_to(source_dir))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build DocMason distribution bundles.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="DocMason repository root. Defaults to the current checkout.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "dist",
        help="Directory where generated release bundles should be written.",
    )
    parser.add_argument(
        "--version",
        default="local-dev",
        help="Version string to record in distribution manifests and asset names.",
    )
    parser.add_argument(
        "--github-repo",
        default=None,
        help="GitHub repository slug to embed in bundle manifests, for example owner/DocMason.",
    )
    parser.add_argument(
        "--update-service-url",
        default=None,
        help=(
            "Bounded release-entry update-check endpoint embedded into release bundles. "
            "When omitted, automatic bundle update checks remain unconfigured."
        ),
    )
    parser.add_argument(
        "--source-commit",
        default=None,
        help="Git commit hash recorded in bundle manifests. Defaults to the current checkout HEAD.",
    )
    parser.add_argument(
        "--source-ref",
        default=None,
        help=(
            "Git ref recorded in bundle manifests. Defaults to the current checkout "
            "branch or tag."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    source_commit = args.source_commit or default_source_commit(repo_root)
    source_ref = args.source_ref or default_source_ref(repo_root)

    with tempfile.TemporaryDirectory() as tempdir_name:
        tempdir = Path(tempdir_name)
        for channel, config in BUNDLE_CHANNELS.items():
            staging_root = tempdir / channel
            staging_root.mkdir(parents=True, exist_ok=True)
            stage_bundle(
                repo_root,
                staging_root,
                version=args.version,
                channel=channel,
                github_repo=args.github_repo,
                update_service_url=args.update_service_url or None,
                source_commit=source_commit,
                source_ref=source_ref,
            )
            zip_path = output_dir / config["asset_name"]
            build_zip(staging_root, zip_path)
            digest = sha256_digest(zip_path)
            (output_dir / f"{config['asset_name']}.sha256").write_text(
                f"{digest}  {config['asset_name']}\n",
                encoding="utf-8",
            )
            print(f"Built {zip_path} ({digest}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

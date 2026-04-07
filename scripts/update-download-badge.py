#!/usr/bin/env python3
"""Generate a GitHub release-download badge payload."""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path

API_ROOT = "https://api.github.com"
DEFAULT_EXCLUDED_SUFFIXES = (".sha256",)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Query GitHub releases and write a Shields endpoint payload for installation downloads."
        )
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository in owner/name format.",
    )
    parser.add_argument(
        "--asset-name",
        action="append",
        dest="asset_names",
        default=None,
        help=(
            "Release asset name to include. Repeat the flag for multiple assets. "
            "When omitted, all non-excluded release assets are counted."
        ),
    )
    parser.add_argument(
        "--exclude-suffix",
        action="append",
        dest="exclude_suffixes",
        default=None,
        help=(
            "Asset suffix to exclude when --asset-name is omitted. "
            "Repeat the flag for multiple suffixes. Defaults to .sha256."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for the badge JSON payload.",
    )
    parser.add_argument(
        "--label",
        default="total downloads",
        help="Badge label.",
    )
    parser.add_argument(
        "--color",
        default="0DAFC6",
        help="Badge color.",
    )
    parser.add_argument(
        "--cache-seconds",
        type=int,
        default=21600,
        help="Suggested badge cache TTL in seconds.",
    )
    return parser


def github_headers(repo: str) -> dict[str, str]:
    """Build GitHub API headers."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{repo} download badge generator",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def next_link(link_header: str | None) -> str | None:
    """Extract the next-page URL from a GitHub Link header."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        if not section.startswith("<") or ">" not in section:
            continue
        return section[1 : section.index(">")]
    return None


def fetch_releases(repo: str) -> list[dict[str, object]]:
    """Fetch all releases for a repository."""
    releases: list[dict[str, object]] = []
    url = f"{API_ROOT}/repos/{repo}/releases?per_page=100"
    headers = github_headers(repo)

    while url:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
                link_header = response.headers.get("Link")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API request failed: {exc.code} {body}") from exc

        if not isinstance(payload, list):
            raise RuntimeError("GitHub releases API returned a non-list payload.")
        releases.extend(item for item in payload if isinstance(item, dict))
        url = next_link(link_header)
    return releases


def collect_downloads(
    releases: list[dict[str, object]],
    asset_names: tuple[str, ...] | None,
    exclude_suffixes: tuple[str, ...],
) -> tuple[int, OrderedDict[str, int]]:
    """Sum downloads for the selected or inferred release assets."""
    explicit_names = set(asset_names or ())
    counts: OrderedDict[str, int] = OrderedDict((name, 0) for name in asset_names or ())
    total = 0

    for release in releases:
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            if not isinstance(name, str):
                continue
            if asset_names:
                if name not in explicit_names:
                    continue
            elif any(name.endswith(suffix) for suffix in exclude_suffixes):
                continue
            download_count = asset.get("download_count")
            if not isinstance(download_count, int):
                continue
            if name not in counts:
                counts[name] = 0
            counts[name] += download_count
            total += download_count
    return total, counts


def build_badge_payload(
    repo: str,
    asset_names: tuple[str, ...],
    total_downloads: int,
    per_asset_downloads: OrderedDict[str, int],
    label: str,
    color: str,
    cache_seconds: int,
) -> dict[str, object]:
    """Build a Shields endpoint JSON payload."""
    return {
        "schemaVersion": 1,
        "label": label,
        "message": str(total_downloads),
        "color": color,
        "cacheSeconds": cache_seconds,
        "namedLogo": "github",
    }


def write_payload(output_path: Path, payload: dict[str, object]) -> None:
    """Write the JSON payload to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    """Run the badge generator."""
    parser = build_parser()
    args = parser.parse_args()
    asset_names = tuple(args.asset_names) if args.asset_names else None
    exclude_suffixes = tuple(args.exclude_suffixes or DEFAULT_EXCLUDED_SUFFIXES)

    releases = fetch_releases(args.repo)
    total_downloads, per_asset_downloads = collect_downloads(
        releases,
        asset_names=asset_names,
        exclude_suffixes=exclude_suffixes,
    )
    payload = build_badge_payload(
        repo=args.repo,
        asset_names=asset_names or tuple(per_asset_downloads.keys()),
        total_downloads=total_downloads,
        per_asset_downloads=per_asset_downloads,
        label=args.label,
        color=args.color,
        cache_seconds=args.cache_seconds,
    )
    write_payload(args.output.resolve(), payload)
    print(
        json.dumps(
            {
                "repo": args.repo,
                "downloads_total": total_downloads,
                "asset_counts": per_asset_downloads,
                "output": str(args.output),
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

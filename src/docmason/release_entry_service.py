"""Collector-side release-entry contract helpers mirrored by the Cloudflare worker."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

ADMIN_SCHEMA_VERSION = 1
CHECK_SCHEMA_VERSION = 1
SERVICE_RESPONSE_SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS release_current (
  distribution_channel TEXT PRIMARY KEY,
  latest_version TEXT NOT NULL,
  published_at TEXT NOT NULL,
  release_url TEXT NOT NULL,
  asset_url TEXT NOT NULL,
  asset_name TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_activity (
  event_day TEXT NOT NULL,
  installation_hash TEXT NOT NULL,
  distribution_channel TEXT NOT NULL,
  recorded_at TEXT NOT NULL,
  trigger TEXT NOT NULL,
  PRIMARY KEY (event_day, installation_hash, distribution_channel)
);
"""


def _utc_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


def _current_time(*, now: datetime | None = None) -> datetime:
    return (now or datetime.now(tz=UTC)).astimezone(UTC)


def _nonempty_string(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def initialize_release_entry_db(connection: sqlite3.Connection) -> None:
    """Create the collector tables used by the release-entry worker."""
    connection.executescript(SCHEMA_SQL)
    connection.commit()


def publish_release_current(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[dict[str, str]]:
    """Upsert the current release metadata for each shipped bundle channel."""
    recorded_at = _current_time(now=now).isoformat().replace("+00:00", "Z")
    latest_version = _nonempty_string(payload.get("release_version"))
    release_url = _nonempty_string(payload.get("release_url"))
    published_at = _nonempty_string(payload.get("published_at")) or recorded_at
    if latest_version is None or release_url is None:
        raise ValueError("Admin publish requires release_version and release_url.")
    channels = payload.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("Admin publish requires one or more channel payloads.")

    published_channels: list[dict[str, str]] = []
    for item in channels:
        if not isinstance(item, dict):
            raise ValueError("Admin channel payloads must be JSON objects.")
        distribution_channel = _nonempty_string(item.get("distribution_channel"))
        asset_name = _nonempty_string(item.get("asset_name"))
        asset_url = _nonempty_string(item.get("asset_url"))
        if distribution_channel is None or asset_name is None or asset_url is None:
            raise ValueError(
                "Each admin channel payload requires distribution_channel, "
                "asset_name, and asset_url."
            )
        connection.execute(
            """
            INSERT INTO release_current (
              distribution_channel,
              latest_version,
              published_at,
              release_url,
              asset_url,
              asset_name,
              updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(distribution_channel) DO UPDATE SET
              latest_version = excluded.latest_version,
              published_at = excluded.published_at,
              release_url = excluded.release_url,
              asset_url = excluded.asset_url,
              asset_name = excluded.asset_name,
              updated_at = excluded.updated_at
            """,
            (
                distribution_channel,
                latest_version,
                published_at,
                release_url,
                asset_url,
                asset_name,
                recorded_at,
            ),
        )
        published_channels.append(
            {
                "distribution_channel": distribution_channel,
                "latest_version": latest_version,
                "published_at": published_at,
                "release_url": release_url,
                "asset_url": asset_url,
                "asset_name": asset_name,
            }
        )
    connection.commit()
    return published_channels


def record_release_entry_check(
    connection: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one bounded DAU event and return the current release metadata."""
    distribution_channel = _nonempty_string(payload.get("distribution_channel"))
    source_version = _nonempty_string(payload.get("source_version"))
    installation_hash = _nonempty_string(payload.get("installation_hash"))
    trigger = _nonempty_string(payload.get("trigger"))
    if (
        distribution_channel is None
        or source_version is None
        or installation_hash is None
        or trigger is None
    ):
        raise ValueError(
            "Update-check payload requires distribution_channel, source_version, "
            "installation_hash, and trigger."
        )

    recorded_at = _current_time(now=now)
    event_day = recorded_at.date().isoformat()
    connection.execute(
        """
        INSERT OR IGNORE INTO daily_activity (
          event_day,
          installation_hash,
          distribution_channel,
          recorded_at,
          trigger
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (
            event_day,
            installation_hash,
            distribution_channel,
            recorded_at.isoformat().replace("+00:00", "Z"),
            trigger,
        ),
    )
    row = connection.execute(
        """
        SELECT
          distribution_channel,
          latest_version,
          published_at,
          release_url,
          asset_url,
          asset_name
        FROM release_current
        WHERE distribution_channel = ?
        """,
        (distribution_channel,),
    ).fetchone()
    connection.commit()
    if row is None:
        raise LookupError(f"No current release is published for `{distribution_channel}`.")
    return {
        "schema_version": SERVICE_RESPONSE_SCHEMA_VERSION,
        "current_release": {
            "distribution_channel": str(row[0]),
            "latest_version": str(row[1]),
            "published_at": str(row[2]),
            "release_url": str(row[3]),
            "asset_url": str(row[4]),
            "asset_name": str(row[5]),
            "update_available": str(row[1]) != source_version,
        },
    }

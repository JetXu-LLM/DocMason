"""Release-entry collector contract tests mirrored by the Cloudflare worker."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import UTC, datetime

from docmason.release_entry_service import (
    initialize_release_entry_db,
    publish_release_current,
    record_release_entry_check,
)


class ReleaseEntryWorkerContractTests(unittest.TestCase):
    """Verify the bounded release-entry collector behavior."""

    def make_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        initialize_release_entry_db(connection)
        return connection

    def publish_release(self, connection: sqlite3.Connection) -> None:
        publish_release_current(
            connection,
            {
                "schema_version": 1,
                "release_version": "v0.2.0",
                "published_at": "2026-03-30T12:00:00Z",
                "release_url": "https://github.com/example/DocMason/releases/tag/v0.2.0",
                "channels": [
                    {
                        "distribution_channel": "clean",
                        "asset_name": "DocMason-clean.zip",
                        "asset_url": (
                            "https://github.com/example/DocMason/releases/download/v0.2.0/"
                            "DocMason-clean.zip"
                        ),
                    },
                    {
                        "distribution_channel": "demo-ico-gcs",
                        "asset_name": "DocMason-demo-ico-gcs.zip",
                        "asset_url": (
                            "https://github.com/example/DocMason/releases/download/v0.2.0/"
                            "DocMason-demo-ico-gcs.zip"
                        ),
                    },
                ],
            },
            now=datetime(2026, 3, 30, 12, 5, tzinfo=UTC),
        )

    def test_admin_publish_updates_release_current(self) -> None:
        connection = self.make_connection()
        self.publish_release(connection)

        clean = connection.execute(
            """
            SELECT latest_version, release_url, asset_url, asset_name
            FROM release_current
            WHERE distribution_channel = 'clean'
            """
        ).fetchone()
        self.assertEqual(clean[0], "v0.2.0")
        self.assertEqual(
            clean[1],
            "https://github.com/example/DocMason/releases/tag/v0.2.0",
        )
        self.assertTrue(clean[2].endswith("/DocMason-clean.zip"))
        self.assertEqual(clean[3], "DocMason-clean.zip")

    def test_update_check_deduplicates_daily_activity_by_day_and_installation_hash(self) -> None:
        connection = self.make_connection()
        self.publish_release(connection)
        payload = {
            "schema_version": 1,
            "distribution_channel": "clean",
            "installation_hash": "hash-123",
            "trigger": "ask-auto",
        }

        first = record_release_entry_check(
            connection,
            payload,
            now=datetime(2026, 3, 30, 13, 0, tzinfo=UTC),
        )
        second = record_release_entry_check(
            connection,
            payload,
            now=datetime(2026, 3, 30, 18, 0, tzinfo=UTC),
        )
        third = record_release_entry_check(
            connection,
            payload,
            now=datetime(2026, 3, 31, 8, 0, tzinfo=UTC),
        )

        del first, second, third
        count = connection.execute(
            "SELECT COUNT(*) FROM daily_activity WHERE distribution_channel = 'clean'"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_update_check_returns_channel_specific_asset_shape(self) -> None:
        connection = self.make_connection()
        self.publish_release(connection)
        response = record_release_entry_check(
            connection,
            {
                "schema_version": 1,
                "distribution_channel": "demo-ico-gcs",
                "installation_hash": "hash-demo",
                "trigger": "ask-auto",
            },
            now=datetime(2026, 3, 30, 14, 0, tzinfo=UTC),
        )
        current_release = response["current_release"]
        self.assertEqual(current_release["distribution_channel"], "demo-ico-gcs")
        self.assertEqual(current_release["latest_version"], "v0.2.0")
        self.assertEqual(current_release["asset_name"], "DocMason-demo-ico-gcs.zip")
        self.assertTrue(current_release["asset_url"].endswith("/DocMason-demo-ico-gcs.zip"))
        self.assertNotIn("update_available", current_release)

    def test_manual_update_trigger_uses_the_same_collector_contract(self) -> None:
        connection = self.make_connection()
        self.publish_release(connection)
        response = record_release_entry_check(
            connection,
            {
                "schema_version": 1,
                "distribution_channel": "clean",
                "installation_hash": "hash-update-core",
                "trigger": "update-core",
            },
            now=datetime(2026, 3, 30, 15, 0, tzinfo=UTC),
        )
        current_release = response["current_release"]
        self.assertEqual(current_release["distribution_channel"], "clean")
        self.assertEqual(current_release["latest_version"], "v0.2.0")
        self.assertTrue(current_release["asset_url"].endswith("/DocMason-clean.zip"))

    def test_update_check_ignores_legacy_source_version_field(self) -> None:
        connection = self.make_connection()
        self.publish_release(connection)
        response = record_release_entry_check(
            connection,
            {
                "schema_version": 1,
                "distribution_channel": "clean",
                "source_version": "v0.1.0",
                "installation_hash": "hash-legacy",
                "trigger": "ask-auto",
            },
            now=datetime(2026, 3, 30, 16, 0, tzinfo=UTC),
        )
        current_release = response["current_release"]
        self.assertEqual(current_release["latest_version"], "v0.2.0")
        self.assertNotIn("update_available", current_release)


if __name__ == "__main__":
    unittest.main()

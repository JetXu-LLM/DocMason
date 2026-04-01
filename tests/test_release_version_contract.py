"""Tests for the committed release-version contract."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docmason.release_version import (
    read_project_version,
    release_tag_for_version,
    validate_release_tag,
    version_for_release_tag,
    write_project_version,
)


class ReleaseVersionContractTests(unittest.TestCase):
    """Cover the committed-file release-version helpers."""

    def test_release_tag_for_version_supports_stable_and_prerelease_values(self) -> None:
        self.assertEqual(release_tag_for_version("0.1.0"), "v0.1.0")
        self.assertEqual(release_tag_for_version("0.1.0rc2"), "v0.1.0-rc2")
        self.assertEqual(release_tag_for_version("0.1.0a1"), "v0.1.0-a1")

    def test_version_for_release_tag_normalizes_legacy_prerelease_shape(self) -> None:
        self.assertEqual(version_for_release_tag("v0.1.0"), "0.1.0")
        self.assertEqual(version_for_release_tag("v0.1.0-rc2"), "0.1.0rc2")

    def test_write_project_version_updates_pyproject(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir_name:
            pyproject_path = Path(tempdir_name) / "pyproject.toml"
            pyproject_path.write_text(
                '[project]\nname = "docmason"\nversion = "0.1.0a0"\n',
                encoding="utf-8",
            )

            write_project_version("0.1.0rc3", pyproject_path=pyproject_path)

            self.assertEqual(read_project_version(pyproject_path), "0.1.0rc3")
            self.assertIn('version = "0.1.0rc3"', pyproject_path.read_text(encoding="utf-8"))

    def test_validate_release_tag_rejects_drift(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match committed project version"):
            validate_release_tag("v0.1.0-rc2", expected_version="0.1.0")

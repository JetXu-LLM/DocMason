"""Delimited-source parsing must degrade to failures instead of raising."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from docmason.text_sources import parse_text_source


class DelimitedSourceParsingTests(unittest.TestCase):
    """A malformed delimited source must return a failure, never crash the caller."""

    def _write(self, name: str, content: str) -> Path:
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_oversized_csv_field_returns_failure_without_raising(self) -> None:
        # A single cell wider than csv's default field-size limit (131072) is
        # valid CSV data (JSON blobs, base64, long text columns), but csv.reader
        # raises _csv.Error on it. The public parser must surface a failure.
        oversized = "x" * 200_000
        path = self._write("data.csv", f"name,payload\nwidget,{oversized}\n")

        parsed = parse_text_source(path, document_type="csv")

        self.assertEqual(parsed.units, [])
        self.assertEqual(len(parsed.failures), 1)
        self.assertIn("Could not parse delimited source", parsed.failures[0])

    def test_well_formed_csv_still_parses(self) -> None:
        path = self._write("ok.csv", "name,value\nbudget,42\nrevenue,99\n")

        parsed = parse_text_source(path, document_type="csv")

        self.assertEqual(parsed.failures, [])
        self.assertEqual(len(parsed.units), 1)
        self.assertEqual(parsed.units[0].structure_data["header_names"], ["name", "value"])


if __name__ == "__main__":
    unittest.main()

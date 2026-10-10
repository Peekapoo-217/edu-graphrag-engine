"""Regression tests for the two-column combination catalogue."""

import unittest
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from crawl_combination_labels import parse_labels
from import_combination_labels import build_combination_index, load_database_url, prepare_links


class CombinationLabelTests(unittest.TestCase):
    def test_load_database_url_from_local_env_file(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "DATABASE_URL=postgresql://user:password@127.0.0.1:5432/database\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    load_database_url(env_path),
                    "postgresql://user:password@127.0.0.1:5432/database",
                )

    def test_exported_database_url_takes_precedence_over_env_file(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "DATABASE_URL=postgresql://file-value/database\n", encoding="utf-8"
            )
            with patch.dict(
                os.environ,
                {"DATABASE_URL": "postgresql://exported-value/database"},
                clear=True,
            ):
                self.assertEqual(
                    load_database_url(env_path),
                    "postgresql://exported-value/database",
                )

    def test_parse_only_requested_columns_and_expand_code_lists(self):
        html = """<table><tr><th>STT</th><th>Tổ hợp</th><th>Môn chi tiết</th>
        <th>Trường</th></tr>
        <tr><td>1</td><td>A01</td><td><a>Toán, Vật lí, Tiếng Anh</a></td><td>220 trường</td></tr>
        <tr><td>2</td><td>X26, K01</td><td>Toán, Tiếng Anh, Tin học</td><td>113 trường</td></tr>
        <tr><td>3</td><td>H06</td><td>Ngữ văn, Tiếng Anh, Vẽ mỹ thuật</td><td>11 trường</td></tr>
        <tr><td>4</td><td>H06</td><td>Toán, Tiếng Anh, Vẽ mỹ thuật</td><td>5 trường</td></tr>
        </table>"""
        rows, source_count = parse_labels(html)
        self.assertEqual(source_count, 4)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], {
            "combination_code": "A01", "subject_details": "Toán, Vật lí, Tiếng Anh"
        })
        self.assertEqual([row["combination_code"] for row in rows[1:3]], ["X26", "K01"])
        self.assertNotIn("220 trường", str(rows))

    def test_link_only_unique_three_subject_combinations(self):
        index = build_combination_index([
            ("ENGLISH_MATH_PHYSICS", 3, "SUBJECT", "ENGLISH"),
            ("ENGLISH_MATH_PHYSICS", 3, "SUBJECT", "MATH"),
            ("ENGLISH_MATH_PHYSICS", 3, "SUBJECT", "PHYSICS"),
            ("APTITUDE_COMBO", 3, "SUBJECT", "MATH"),
            ("APTITUDE_COMBO", 3, "SUBJECT", "LITERATURE"),
            ("APTITUDE_COMBO", 3, "APTITUDE", None),
        ])
        rows = [
            {"combination_code": "A01", "subject_details": "Toán, Vật lí, Tiếng Anh"},
            {"combination_code": "H06", "subject_details": "Ngữ văn, Tiếng Anh, Vẽ mỹ thuật"},
            {"combination_code": "H06", "subject_details": "Toán, Tiếng Anh, Vẽ mỹ thuật"},
            {"combination_code": "M00", "subject_details": "Ngữ văn, Toán, Đọc diễn cảm, Hát"},
        ]
        prepared, reasons = prepare_links(rows, index)
        self.assertEqual(prepared[0][2], "ENGLISH_MATH_PHYSICS")
        self.assertIsNone(prepared[1][2])
        self.assertIsNone(prepared[2][2])
        self.assertIsNone(prepared[3][2])
        self.assertEqual(reasons["conflicting_details"], 2)
        self.assertEqual(reasons["unsupported_component_count"], 1)

    def test_link_gdktpl_label_from_source(self):
        index = build_combination_index([
            ("ECONOMIC_LAW_LITERATURE_MATH", 3, "SUBJECT", "ECONOMIC_LAW"),
            ("ECONOMIC_LAW_LITERATURE_MATH", 3, "SUBJECT", "LITERATURE"),
            ("ECONOMIC_LAW_LITERATURE_MATH", 3, "SUBJECT", "MATH"),
        ])
        rows = [{"combination_code": "X01", "subject_details": "Ngữ văn, Toán, GDKTPL"}]
        prepared, reasons = prepare_links(rows, index)
        self.assertEqual(prepared[0][2], "ECONOMIC_LAW_LITERATURE_MATH")
        self.assertFalse(reasons)


if __name__ == "__main__":
    unittest.main()

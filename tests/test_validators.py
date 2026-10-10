#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_validators.py

Unit tests for CSV validators and legacy CSV compatibility.
"""

import csv
import os
import tempfile
import unittest
from crawlers.common.validators import validate_admissions_csv, validate_cutoffs_csv


class TestValidators(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_csv_without_component_type(self):
        """Test legacy CSV without component_type field in subjects_json is parsed correctly."""
        csv_path = os.path.join(self.temp_dir.name, "legacy_offers.csv")
        legacy_subjects_json = '[{"subject_code": "MATH", "subject_name": "Toán", "weight": 1, "source_label": "Toán"}, {"subject_code": "APTITUDE_EARLY_CHILDHOOD", "subject_name": "NK GDMN", "weight": 1, "source_label": "NK GDMN"}]'

        rows = [{
            "university_code": "DQN",
            "admission_year": "2026",
            "program_code": "7140114",
            "program_name": "Giáo dục Mầm non",
            "method_codes": "100",
            "quota": "50",
            "combination_id": "MATH_APTITUDE_EARLY_CHILDHOOD",
            "combination_display": "(Toán, NK GDMN)",
            "subjects_json": legacy_subjects_json,
            "source_url": "https://example.com",
            "retrieved_at": "2026-08-17T12:00:00+07:00",
        }]

        fields = list(rows[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

        valid_records, errors, summary = validate_admissions_csv(csv_path)
        self.assertEqual(len(errors), 0)
        self.assertEqual(len(valid_records), 1)

        rec = valid_records[0]
        items = rec["subjects_items"]
        self.assertEqual(items[0]["component_type"], "SUBJECT")
        self.assertEqual(items[1]["component_type"], "APTITUDE")

    def test_unknown_component_validation_error(self):
        """Test unclassified/unknown component code raises validation error."""
        csv_path = os.path.join(self.temp_dir.name, "invalid_offers.csv")
        invalid_subjects_json = '[{"subject_code": "UNKNOWN_XYZ", "subject_name": "Môn Không Tồn Tại", "weight": 1}]'

        rows = [{
            "university_code": "DQN",
            "admission_year": "2026",
            "program_code": "7999999",
            "program_name": "Ngành Giả Lập",
            "method_codes": "100",
            "quota": "10",
            "combination_id": "UNKNOWN_XYZ",
            "combination_display": "(Unknown)",
            "subjects_json": invalid_subjects_json,
            "source_url": "https://example.com",
            "retrieved_at": "2026-08-17T12:00:00+07:00",
        }]

        fields = list(rows[0].keys())
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

        valid_records, errors, summary = validate_admissions_csv(csv_path)
        self.assertGreater(len(errors), 0)
        self.assertIn("Unclassified component", errors[0])


if __name__ == "__main__":
    unittest.main()

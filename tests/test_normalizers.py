#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_normalizers.py

Unit tests for subject normalizer and combination normalizer.
"""

import unittest
from crawlers.common.subject_normalizer import normalize_subject_label
from crawlers.common.combination_normalizer import (
    generate_canonical_combination_id,
    format_combination_display,
)
from crawlers.common.models import SubjectItem


class TestNormalizers(unittest.TestCase):

    def test_canonical_combination_id_sorting(self):
        """Test two combinations with different component order produce same canonical_combination_id."""
        combo1 = ["MATH", "PHYSICS", "CHEMISTRY"]
        combo2 = ["MATH", "CHEMISTRY", "PHYSICS"]
        canonical1 = generate_canonical_combination_id(combo1)
        canonical2 = generate_canonical_combination_id(combo2)
        self.assertEqual(canonical1, "CHEMISTRY_MATH_PHYSICS")
        self.assertEqual(canonical2, "CHEMISTRY_MATH_PHYSICS")
        self.assertEqual(canonical1, canonical2)

    def test_economic_law_mapping(self):
        """Test ECONOMIC_LAW_ED and Vietnamese label mapped to ECONOMIC_LAW."""
        item1, err1 = normalize_subject_label("gdkt & pl")
        self.assertIsNone(err1)
        self.assertEqual(item1.subject_code, "ECONOMIC_LAW")

        item2, err2 = normalize_subject_label("ECONOMIC_LAW_ED")
        self.assertIsNone(err2)
        self.assertEqual(item2.subject_code, "ECONOMIC_LAW")

    def test_aptitude_identification(self):
        """Test APTITUDE_* code and label identified as APTITUDE component_type."""
        item1, err1 = normalize_subject_label("NK GDMN")
        self.assertIsNone(err1)
        self.assertEqual(item1.component_type, "APTITUDE")

        item2, err2 = normalize_subject_label("APTITUDE_MUSIC")
        self.assertIsNone(err2)
        self.assertEqual(item2.component_type, "APTITUDE")

    def test_subject_identification(self):
        """Test standard subject identified as SUBJECT component_type."""
        item1, err1 = normalize_subject_label("Toán")
        self.assertIsNone(err1)
        self.assertEqual(item1.subject_code, "MATH")
        self.assertEqual(item1.component_type, "SUBJECT")

    def test_unknown_subject_validation_error(self):
        """Test unknown subject label returns validation error."""
        item, err = normalize_subject_label("Môn Học Chưa Biết 999", university_code="DQN")
        self.assertIsNone(item)
        self.assertIsNotNone(err)
        self.assertIn("UNMAPPED SUBJECT", err)

    def test_combination_display_ordering(self):
        """Test combination display formats SUBJECT components first, APTITUDE after."""
        subjects = [
            SubjectItem("APTITUDE_EARLY_CHILDHOOD", "NK GDMN", "APTITUDE", 1.0, "NK GDMN"),
            SubjectItem("LITERATURE", "Ngữ văn", "SUBJECT", 1.0, "Văn"),
            SubjectItem("MATH", "Toán", "SUBJECT", 1.0, "Toán"),
        ]
        display = format_combination_display(subjects)
        self.assertEqual(display, "(Ngữ văn, Toán, NK GDMN)")


if __name__ == "__main__":
    unittest.main()

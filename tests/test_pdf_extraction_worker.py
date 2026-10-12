import unittest

from pdf_extraction_worker import validate_groups


class PdfExtractionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {"MATH", "LITERATURE", "PHYSICS", "CHEMISTRY", "BIOLOGY"}

    def test_accepts_four_catalog_subjects_with_valid_source_page(self):
        groups = [{"group_code": "A", "group_name": "Nhóm A",
                   "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "CHEMISTRY"],
                   "page_numbers": [2, 1, 2]}]

        self.assertEqual(validate_groups(groups, self.allowed, 3), [{
            "code": "A", "name": "Nhóm A",
            "subjects": ["MATH", "LITERATURE", "PHYSICS", "CHEMISTRY"], "pages": [1, 2]
        }])

    def test_rejects_unknown_subject_instead_of_trusting_model(self):
        groups = [{"group_code": "A", "group_name": "Nhóm A",
                   "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "UNKNOWN"],
                   "page_numbers": [1]}]

        with self.assertRaisesRegex(ValueError, "NO_VALID_GROUPS"):
            validate_groups(groups, self.allowed, 1)

    def test_rejects_duplicate_subjects_and_out_of_range_page(self):
        duplicate = {"group_code": "A", "group_name": "Nhóm A",
                     "subject_codes": ["MATH", "MATH", "PHYSICS", "CHEMISTRY"],
                     "page_numbers": [1]}
        bad_page = {"group_code": "B", "group_name": "Nhóm B",
                    "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "CHEMISTRY"],
                    "page_numbers": [4]}

        with self.assertRaisesRegex(ValueError, "NO_VALID_GROUPS"):
            validate_groups([duplicate, bad_page], self.allowed, 3)


if __name__ == "__main__":
    unittest.main()

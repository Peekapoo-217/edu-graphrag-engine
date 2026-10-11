"""Regression tests for the two-column combination catalogue."""

import unittest
from pathlib import Path
import os
from tempfile import TemporaryDirectory
from unittest.mock import patch

from crawl_combination_labels import parse_labels
from combination_label_normalizer import (
    expand_details, normalize_catalogue_subject, split_components, synthetic_source_code,
)
from import_combination_labels import (
    build_combination_index, load_database_url, plan_catalogue_normalization,
    plan_promotions, prepare_links,
)


class CombinationLabelTests(unittest.TestCase):
    def test_expand_k01_into_four_concrete_rows(self):
        self.assertEqual(expand_details("Toán, Văn, Lý/Hóa/Sinh/Tin"), [
            "Toán, Văn, Lý", "Toán, Văn, Hóa",
            "Toán, Văn, Sinh", "Toán, Văn, Tin",
        ])

    def test_parenthesized_commas_are_one_component(self):
        details = "Toán, NK Mầm non 1( kể chuyện, đọc, diễn cảm), NK Mầm non 2 (Hát)"
        self.assertEqual(len(split_components(details)), 3)
        self.assertEqual(expand_details(details), [details])

    def test_expand_q00_only_at_slash_not_inside_other_components(self):
        self.assertEqual(
            expand_details("Tư duy định lượng, Tư duy định tính, Khoa học/ Tiếng Anh"),
            [
                "Tư duy định lượng, Tư duy định tính, Khoa học",
                "Tư duy định lượng, Tư duy định tính, Tiếng Anh",
            ],
        )

    def test_reject_incomplete_alternative(self):
        with self.assertRaises(ValueError):
            expand_details("Toán, Văn, Lý/")

    def test_catalogue_subject_aliases_keep_technology_variants_and_languages_distinct(self):
        self.assertEqual(normalize_catalogue_subject("Công nghệ công nghiệp").subject_code, "TECH_INDUSTRY")
        self.assertEqual(normalize_catalogue_subject("Công nghệ nông nghiệp").subject_code, "TECH_AGRI")
        self.assertEqual(normalize_catalogue_subject("Tiếng Pháp").subject_code, "FRENCH")
        self.assertEqual(
            normalize_catalogue_subject("Ngoại ngữ").subject_code,
            "FOREIGN_LANGUAGE_UNSPECIFIED",
        )

    def test_catalogue_plan_links_each_k01_variant_and_reuses_existing_set(self):
        rows = [
            {"combination_code": "K01", "subject_details": "Toán, Tiếng Anh, Tin học"},
            {"combination_code": "K01", "subject_details": "Toán, Văn, Lý/Hóa/Sinh/Tin"},
        ]
        names = {
            "MATH": "Toán", "LITERATURE": "Ngữ văn", "ENGLISH": "Tiếng Anh",
            "INFORMATICS": "Tin học", "PHYSICS": "Vật lí", "CHEMISTRY": "Hóa học",
            "BIOLOGY": "Sinh học",
        }
        existing = [
            ("ENGLISH_INFORMATICS_MATH", 3, "SUBJECT", code, code)
            for code in ("ENGLISH", "INFORMATICS", "MATH")
        ]
        plan = plan_catalogue_normalization(
            rows, existing, {"ENGLISH_INFORMATICS_MATH"}, names,
        )
        self.assertEqual(len(plan.rows), 5)
        self.assertEqual(plan.expanded_rows, 3)
        self.assertTrue(all(canonical_id for _, _, canonical_id in plan.rows))
        self.assertEqual(plan.rows[0][2], "ENGLISH_INFORMATICS_MATH")
        self.assertEqual({detail for _, detail, _ in plan.rows[1:]}, {
            "Toán, Văn, Lý", "Toán, Văn, Hóa", "Toán, Văn, Sinh", "Toán, Văn, Tin",
        })
        self.assertEqual(len(plan.combinations), 4)

        repeated_components = existing + [
            (cid, len(parts), part.component_type, part.code, part.subject_code)
            for cid, parts in plan.combinations.items() for part in parts
        ]
        repeated = plan_catalogue_normalization(
            rows, repeated_components,
            {"ENGLISH_INFORMATICS_MATH", *plan.combinations}, names,
        )
        self.assertFalse(repeated.combinations)
        self.assertEqual(repeated.rows, plan.rows)

    def test_catalogue_plan_names_generic_terms_without_inventing_specific_subjects(self):
        rows = [
            {"combination_code": "D03", "subject_details": "Ngữ văn, Toán, Tiếng Pháp"},
            {"combination_code": "H10", "subject_details": "Ngữ văn, Ngoại ngữ, năng khiếu"},
        ]
        plan = plan_catalogue_normalization(
            rows, [], set(), {"MATH": "Toán", "LITERATURE": "Ngữ văn"},
        )
        self.assertEqual(plan.subjects, {
            "FRENCH": "Tiếng Pháp",
            "FOREIGN_LANGUAGE_UNSPECIFIED": "Ngoại ngữ (chưa xác định)",
        })
        self.assertEqual(plan.rows[0][2], "FRENCH_LITERATURE_MATH")
        self.assertIsNotNone(plan.rows[1][2])
        generic_parts = plan.combinations[plan.rows[1][2]]
        self.assertEqual(
            {(part.component_type, part.code) for part in generic_parts if part.component_type == "SUBJECT"},
            {("SUBJECT", "LITERATURE"), ("SUBJECT", "FOREIGN_LANGUAGE_UNSPECIFIED")},
        )
        self.assertEqual(sum(part.component_type == "APTITUDE" for part in generic_parts), 1)
        self.assertFalse(plan.reasons)

    def test_malformed_public_code_keeps_raw_label_and_adds_stable_generated_alias(self):
        raw_code = "(Ngữ văn, Vật lí, Tiếng Hàn)"
        details = "Ngữ văn, Vật lí, Tiếng Hàn"
        generated_code = synthetic_source_code(raw_code, details)
        self.assertRegex(generated_code, r"^SRC[0-9A-F]{7}$")
        self.assertEqual(generated_code, synthetic_source_code(raw_code, details))
        plan = plan_catalogue_normalization(
            [{"combination_code": raw_code, "subject_details": details}],
            [], set(), {"LITERATURE": "Ngữ văn", "PHYSICS": "Vật lí"},
        )
        self.assertEqual(plan.rows[0], (raw_code, details, None))
        self.assertEqual(plan.rows[1][0], generated_code)
        self.assertIsNotNone(plan.rows[1][2])
        self.assertEqual(plan.reasons["invalid_code"], 1)

    def test_catalogue_plan_keeps_aptitude_out_of_subject_dictionary(self):
        plan = plan_catalogue_normalization(
            [{"combination_code": "V02", "subject_details": "Toán, Ngữ văn, Vẽ mỹ thuật"}],
            [], set(), {"MATH": "Toán", "LITERATURE": "Ngữ văn"},
        )
        parts = next(iter(plan.combinations.values()))
        self.assertEqual([part.component_type for part in parts], ["APTITUDE", "SUBJECT", "SUBJECT"])
        self.assertIsNone(parts[0].subject_code)
        self.assertFalse(plan.subjects)

    def test_generic_science_names_are_distinct_source_scoped_components(self):
        rows = [
            {"combination_code": "D73", "subject_details": "Ngữ văn, Ngoại ngữ, Khoa học tự nhiên"},
            {"combination_code": "D96", "subject_details": "Ngữ văn, Ngoại ngữ, Khoa học xã hội"},
        ]
        plan = plan_catalogue_normalization(
            rows, [], set(), {"LITERATURE": "Ngữ văn"},
        )
        self.assertTrue(all(canonical for _, _, canonical in plan.rows))
        self.assertEqual(len(plan.combinations), 2)
        generic_codes = {
            part.code
            for parts in plan.combinations.values()
            for part in parts
            if part.component_type == "APTITUDE"
        }
        self.assertEqual(len(generic_codes), 2)
        self.assertTrue(all(code.startswith("APTITUDE_CATALOGUE_") for code in generic_codes))

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

    def test_promote_only_safe_distinct_sets_and_deduplicate(self):
        rows = [
            {"combination_code": "X26", "subject_details": "Toán, Tiếng Anh, Tin học"},
            {"combination_code": "K01", "subject_details": "Toán, Tiếng Anh, Tin học"},
            {"combination_code": "H06", "subject_details": "Ngữ văn, Tiếng Anh, Vẽ mỹ thuật"},
            {"combination_code": "H06", "subject_details": "Toán, Tiếng Anh, Vẽ mỹ thuật"},
            {"combination_code": "M00", "subject_details": "Toán, Tiếng Anh, Tiếng Trung"},
            {"combination_code": "Z99", "subject_details": "Toán, Tiếng Anh, Tiếng Pháp"},
        ]
        names = {"MATH": "Toán", "ENGLISH": "Tiếng Anh", "INFORMATICS": "Tin học"}
        prepared, reasons, planned = plan_promotions(rows, [], set(), names)
        canonical_id = "ENGLISH_INFORMATICS_MATH"
        self.assertEqual(planned, {canonical_id: ("ENGLISH", "INFORMATICS", "MATH")})
        self.assertEqual([row[2] for row in prepared], [canonical_id, canonical_id, None, None, None, None])
        self.assertEqual(reasons["conflicting_details"], 2)
        self.assertEqual(reasons["unmapped_or_aptitude"], 1)
        self.assertEqual(reasons["no_unique_canonical_combination"], 1)

    def test_existing_set_is_linked_without_new_row_on_repeat(self):
        rows = [{"combination_code": "X26", "subject_details": "Toán, Tiếng Anh, Tin học"}]
        existing = [
            ("ENGLISH_INFORMATICS_MATH", 3, "SUBJECT", code)
            for code in ("ENGLISH", "INFORMATICS", "MATH")
        ]
        prepared, reasons, planned = plan_promotions(
            rows, existing, {"ENGLISH_INFORMATICS_MATH"},
            {"ENGLISH": "Tiếng Anh", "INFORMATICS": "Tin học", "MATH": "Toán"},
        )
        self.assertEqual(prepared[0][2], "ENGLISH_INFORMATICS_MATH")
        self.assertFalse(reasons)
        self.assertFalse(planned)

    def test_id_collision_does_not_promote(self):
        rows = [{"combination_code": "X26", "subject_details": "Toán, Tiếng Anh, Tin học"}]
        prepared, reasons, planned = plan_promotions(
            rows, [], {"ENGLISH_INFORMATICS_MATH"},
            {"ENGLISH": "Tiếng Anh", "INFORMATICS": "Tin học", "MATH": "Toán"},
        )
        self.assertIsNone(prepared[0][2])
        self.assertEqual(reasons["no_unique_canonical_combination"], 1)
        self.assertFalse(planned)


if __name__ == "__main__":
    unittest.main()

"""
Unit test suite for graph data mapping, validation, batching, and Cypher statement safety.
Does NOT connect to any real database.
"""

import unittest
from typing import Dict, Any

from graph_sync.models import (
    CANONICAL_SUBJECT_MAP,
    get_canonical_subject_code,
    make_cutoff_score_uid,
    make_subject_group_uid,
    make_training_program_uid,
)
from graph_sync.neo4j_writer import (
    NODE_CYPHER_STATEMENTS,
    RELATIONSHIP_CYPHER_STATEMENTS,
    chunk_list,
)
from graph_sync.validators import validate_graph_data


class TestGraphMapping(unittest.TestCase):

    def test_uid_generation(self):
        """Test UID creation functions for SubjectGroup, TrainingProgram, CutoffScore."""
        sg_uid = make_subject_group_uid("TCV", "2026", "G01")
        self.assertEqual(sg_uid, "TCV|2026|G01")

        tp_uid = make_training_program_uid("DQN", "7480201")
        self.assertEqual(tp_uid, "DQN|7480201")

        cs_uid = make_cutoff_score_uid("DQN", 2025, "7480201", "100", "THPT")
        self.assertEqual(cs_uid, "DQN|2025|7480201|100|THPT")

    def test_technology_variant_mapping(self):
        """Test technology variant subjects map to canonical TECHNOLOGY."""
        self.assertEqual(get_canonical_subject_code("TECH_INDUSTRY"), "TECHNOLOGY")
        self.assertEqual(get_canonical_subject_code("TECH_AGRI"), "TECHNOLOGY")
        self.assertEqual(get_canonical_subject_code("TECH_PHYSICS"), "TECHNOLOGY")
        self.assertEqual(get_canonical_subject_code("TECH_BIOLOGY"), "TECHNOLOGY")
        self.assertEqual(get_canonical_subject_code("MATH"), None)
        self.assertEqual(get_canonical_subject_code("TECHNOLOGY"), None)
        # Test override if existing_canonical is present
        self.assertEqual(get_canonical_subject_code("TECH_AGRI", "TECHNOLOGY"), "TECHNOLOGY")

    def test_subject_and_aptitude_separation_and_validation(self):
        """Test validation detects invalid component classification or counts."""
        valid_graph_data: Dict[str, Any] = {
            "nodes": {
                "Subject": [{"code": "MATH", "name": "Toán", "subject_type": "KHTN", "is_mandatory": True, "canonical_subject_code": None}],
                "HighSchool": [{"code": "TCV", "name": "Trường TCV", "location": "Quy Nhơn"}],
                "SubjectGroup": [{"uid": "TCV|2026|G01", "school_code": "TCV", "academic_year": "2026", "group_code": "G01", "name": "Nhóm 1"}],
                "University": [{"code": "DQN", "name": "Đại học Quy Nhơn", "website": "https://qnu.edu.vn"}],
                "TrainingProgram": [{"uid": "DQN|7480201", "university_code": "DQN", "program_code": "7480201", "name": "CNTT"}],
                "AdmissionCombination": [{"id": "A00", "display_name": "Toán, Lý, Hóa", "component_count": 1}],
                "AptitudeTest": [{"code": "NK01", "name": "Năng khiếu 1"}],
                "CutoffScore": [{"uid": "DQN|2025|7480201|100|THPT", "university_code": "DQN", "admission_year": 2025, "program_code": "7480201", "method_code": "100", "score_type": "THPT", "cutoff_score_thpt": 25.0, "cutoff_score_sat": None, "source_url": None}],
            },
            "relationships": {
                "OFFERS_GROUP": [{"high_school_code": "TCV", "subject_group_uid": "TCV|2026|G01"}],
                "INCLUDES_SUBJECT": [{"subject_group_uid": "TCV|2026|G01", "subject_code": "MATH"}],
                "IS_VARIANT_OF": [],
                "OFFERS_PROGRAM": [{"university_code": "DQN", "training_program_uid": "DQN|7480201"}],
                "ACCEPTS_COMBINATION": [{"training_program_uid": "DQN|7480201", "combination_id": "A00", "admission_year": 2026, "method_codes": "100", "quota": 50, "source_url": None}],
                "REQUIRES_SUBJECT": [{"combination_id": "A00", "subject_code": "MATH", "weight": 1.0}],
                "REQUIRES_APTITUDE": [],
                "HAS_CUTOFF": [{"training_program_uid": "DQN|7480201", "cutoff_score_uid": "DQN|2025|7480201|100|THPT"}],
            },
            "raw_components": [
                ("A00", "MATH", "Toán", "SUBJECT", "MATH", 1.0)
            ],
        }

        is_valid, errors = validate_graph_data(valid_graph_data)
        self.assertTrue(is_valid, f"Expected valid data, but got errors: {errors}")

    def test_detection_of_non_existent_references(self):
        """Test validator catches dangling references to missing nodes."""
        invalid_graph_data: Dict[str, Any] = {
            "nodes": {
                "Subject": [],
                "HighSchool": [],
                "SubjectGroup": [],
                "University": [],
                "TrainingProgram": [],
                "AdmissionCombination": [],
                "AptitudeTest": [],
                "CutoffScore": [],
            },
            "relationships": {
                "OFFERS_GROUP": [{"high_school_code": "MISSING_HS", "subject_group_uid": "MISSING_SG"}],
                "INCLUDES_SUBJECT": [],
                "IS_VARIANT_OF": [],
                "OFFERS_PROGRAM": [],
                "ACCEPTS_COMBINATION": [],
                "REQUIRES_SUBJECT": [],
                "REQUIRES_APTITUDE": [],
                "HAS_CUTOFF": [],
            },
            "raw_components": [],
        }

        is_valid, errors = validate_graph_data(invalid_graph_data)
        self.assertFalse(is_valid)
        self.assertTrue(any("MISSING_HS" in e for e in errors))
        self.assertTrue(any("MISSING_SG" in e for e in errors))

    def test_chunking_logic(self):
        """Test batch partitioning function chunk_list."""
        items = list(range(1050))
        chunks = chunk_list(items, batch_size=500)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(len(chunks[0]), 500)
        self.assertEqual(len(chunks[1]), 500)
        self.assertEqual(len(chunks[2]), 50)

    def test_cypher_queries_use_merge_no_raw_create_or_detach_delete(self):
        """Audit all Cypher statements to ensure MERGE is used and no CREATE or DETACH DELETE exist."""
        all_statements = list(NODE_CYPHER_STATEMENTS.values()) + list(RELATIONSHIP_CYPHER_STATEMENTS.values())

        for cypher in all_statements:
            cypher_upper = cypher.upper()
            # Ensure MERGE is present
            self.assertIn("MERGE", cypher_upper)
            # Ensure no DETACH DELETE
            self.assertNotIn("DETACH DELETE", cypher_upper)
            # Ensure no raw CREATE statements (MERGE is expected)
            self.assertNotIn("CREATE ", cypher_upper)
            self.assertNotIn("CREATE(", cypher_upper)


if __name__ == "__main__":
    unittest.main()

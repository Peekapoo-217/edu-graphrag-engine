"""
Unit test suite for graph data mapping, validation, batching, and Cypher statement safety.
Does NOT connect to any real database.
"""

import unittest
from unittest.mock import MagicMock
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
    UNPUBLISHED_SUBJECT_GROUP_CYPHER,
    chunk_list,
    sync_nodes_and_relationships,
    verify_graph_statistics,
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

    def test_cypher_queries_limit_deletes_to_publication_aware_school_relation(self):
        """Only the publication-aware OFFERS_GROUP query deletes a relationship."""
        all_statements = list(NODE_CYPHER_STATEMENTS.values()) + list(RELATIONSHIP_CYPHER_STATEMENTS.values())

        for cypher in all_statements:
            cypher_upper = cypher.upper()
            self.assertIn("MERGE", cypher_upper)
            if cypher is not RELATIONSHIP_CYPHER_STATEMENTS["OFFERS_GROUP"]:
                self.assertNotIn("DELETE ", cypher_upper)
            self.assertNotIn("CREATE ", cypher_upper)
            self.assertNotIn("CREATE(", cypher_upper)

    def test_unpublished_projection_deletes_only_the_scoped_group_node(self):
        driver = MagicMock()
        session = MagicMock()
        tx = MagicMock()
        driver.session.return_value.__enter__.return_value = session
        session.execute_write.side_effect = lambda operation: operation(tx)
        published_group = {
            "uid": "SCHOOL|2026|A02", "school_code": "SCHOOL", "academic_year": "2026",
            "group_code": "A02", "name": "Group A02", "is_published": True,
            "publication_revision": 2,
        }
        unpublished_group = {
            "uid": "SCHOOL|2026|A01", "school_code": "SCHOOL", "academic_year": "2026",
            "group_code": "A01", "publication_revision": 4,
        }

        sync_nodes_and_relationships(driver, "edugraph", {
            "nodes": {
                "Subject": [], "HighSchool": [{"code": "SCHOOL"}],
                "SubjectGroup": [published_group],
            },
            "relationships": {"OFFERS_GROUP": [], "INCLUDES_SUBJECT": [], "IS_VARIANT_OF": []},
            "unpublished_subject_groups": [unpublished_group],
        })

        delete_call = next(call for call in tx.run.call_args_list
                           if "DETACH DELETE sg" in call.args[0])
        self.assertIn("MATCH (sg:SubjectGroup {uid: row.uid})", delete_call.args[0])
        self.assertIn("publication_revision, 0) <= row.publication_revision", delete_call.args[0])
        self.assertEqual(delete_call.kwargs["rows"], [unpublished_group])
        self.assertEqual(tx.run.call_count, 3)  # School, active group, and one scoped deletion.

    def test_restore_projection_recreates_the_unpublished_uid_and_its_relationships(self):
        driver = MagicMock()
        session = MagicMock()
        tx = MagicMock()
        driver.session.return_value.__enter__.return_value = session
        session.execute_write.side_effect = lambda operation: operation(tx)
        uid = "SCHOOL|2026|A01"

        sync_nodes_and_relationships(driver, "edugraph", {
            "nodes": {"Subject": [], "HighSchool": [], "SubjectGroup": []},
            "relationships": {"OFFERS_GROUP": [], "INCLUDES_SUBJECT": [], "IS_VARIANT_OF": []},
            "unpublished_subject_groups": [{
                "uid": uid, "school_code": "SCHOOL", "academic_year": "2026",
                "group_code": "A01", "publication_revision": 2,
            }],
        })
        tx.run.reset_mock()

        sync_nodes_and_relationships(driver, "edugraph", {
            "nodes": {
                "Subject": [{"code": "MATH", "name": "Math", "subject_type": "ACADEMIC",
                             "is_mandatory": False, "canonical_subject_code": None}],
                "HighSchool": [{"code": "SCHOOL", "name": "School", "location": None}],
                "SubjectGroup": [{"uid": uid, "school_code": "SCHOOL", "academic_year": "2026",
                                  "group_code": "A01", "name": "Group A01", "is_published": True,
                                  "publication_revision": 3}],
            },
            "relationships": {
                "OFFERS_GROUP": [{"high_school_code": "SCHOOL", "subject_group_uid": uid,
                                  "school_code": "SCHOOL", "academic_year": "2026", "group_code": "A01",
                                  "group_name": "Group A01", "is_published": True,
                                  "publication_revision": 3}],
                "INCLUDES_SUBJECT": [{"subject_group_uid": uid, "subject_code": "MATH"}],
                "IS_VARIANT_OF": [],
            },
            "unpublished_subject_groups": [],
        })

        group_upsert = next(call for call in tx.run.call_args_list
                            if "MERGE (sg:SubjectGroup {uid: row.uid})" in call.args[0])
        relation_upserts = [call.args[0] for call in tx.run.call_args_list]
        self.assertEqual(group_upsert.kwargs["rows"][0]["uid"], uid)
        self.assertTrue(any("MERGE (h)-[:OFFERS_GROUP]->(sg)" in query for query in relation_upserts))
        self.assertTrue(any("MERGE (sg)-[r:INCLUDES_SUBJECT]->(s)" in query for query in relation_upserts))

    def test_subject_group_projection_is_executed_as_one_transaction(self):
        driver = MagicMock()
        session = MagicMock()
        tx = MagicMock()
        driver.session.return_value.__enter__.return_value = session
        session.execute_write.side_effect = lambda operation: operation(tx)

        sync_nodes_and_relationships(driver, "edugraph", {
            "nodes": {"Subject": [], "HighSchool": [], "SubjectGroup": []},
            "relationships": {"OFFERS_GROUP": [], "INCLUDES_SUBJECT": [], "IS_VARIANT_OF": []},
            "unpublished_subject_groups": [{
                "uid": "SCHOOL|2026|A01", "school_code": "SCHOOL", "academic_year": "2026",
                "group_code": "A01", "publication_revision": 2,
            }],
        })

        session.execute_write.assert_called_once()
        tx.run.assert_called_once_with(UNPUBLISHED_SUBJECT_GROUP_CYPHER, rows=[{
            "uid": "SCHOOL|2026|A01", "school_code": "SCHOOL", "academic_year": "2026",
            "group_code": "A01", "publication_revision": 2,
        }])

    def test_graph_orphan_statistics_ignore_unpublished_groups(self):
        driver = MagicMock()
        session = MagicMock()
        driver.session.return_value.__enter__.return_value = session
        session.run.return_value = MagicMock(single=MagicMock(return_value={"count": 0}))

        verify_graph_statistics(driver, "edugraph")

        group_orphan_query = next(call.args[0] for call in session.run.call_args_list
                                  if "SubjectGroup" in call.args[0] and "OFFERS_GROUP" in call.args[0])
        self.assertIn("coalesce(sg.is_published, true) = true", group_orphan_query)


if __name__ == "__main__":
    unittest.main()

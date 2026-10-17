import unittest
from unittest.mock import MagicMock

from rag_api.retrieval.graph_retriever import Neo4jRetriever, SCHOOL_GROUPS_QUERY, _matching_school_groups


class FakeRecord(dict):
    def data(self):
        return dict(self)


class FakeResult(list):
    def single(self):
        return self[0] if self else None


class GraphRetrieverSubjectGroupFilterTests(unittest.TestCase):
    def test_public_group_query_excludes_unpublished_groups(self):
        self.assertIn("coalesce(sg.is_published, true) = true", SCHOOL_GROUPS_QUERY)

    def test_requires_every_combination_subject_or_a_mandatory_subject(self):
        group = {
            "high_school_code": "SCHOOL",
            "high_school_name": "THPT A",
            "academic_year": "2026-2027",
            "subject_group_code": "A01",
            "subject_group_name": "Nhóm A01",
            "subjects": [
                {"code": "MATH", "canonical_code": "MATH", "name": "Toán"},
                {"code": "PHYSICS", "canonical_code": "PHYSICS", "name": "Vật lí"},
            ],
        }

        self.assertEqual(
            _matching_school_groups(["MATH", "PHYSICS", "LITERATURE"], [group], ["LITERATURE"]),
            [group],
        )
        self.assertEqual(_matching_school_groups(["MATH", "CHEMISTRY"], [group], []), [])
        self.assertEqual(_matching_school_groups([], [group], []), [])

    def test_filters_program_combinations_against_all_subjects_in_latest_school_year(self):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        admission_rows = FakeResult([
            FakeRecord(
                program_name="Computer Science",
                program_code="7480101",
                university_name="University A",
                university_code="UA",
                combination_id="MATCH",
                combination_name="Math, Physics, Literature",
                required_subjects=["Math", "Physics", "Literature"],
                required_subject_codes=["MATH", "PHYSICS", "LITERATURE"],
                cutoff_scores=[],
                score=1.0,
            ),
            FakeRecord(
                program_name="Computer Science",
                program_code="7480101",
                university_name="University A",
                university_code="UA",
                combination_id="APTITUDE",
                combination_name="Math and aptitude test",
                required_subjects=["Math"],
                required_subject_codes=["MATH"],
                required_aptitude_codes=["APTITUDE_TEST"],
                cutoff_scores=[],
                score=1.0,
            ),
            FakeRecord(
                program_name="Computer Science",
                program_code="7480101",
                university_name="University A",
                university_code="UA",
                combination_id="PARTIAL",
                combination_name="Math, Chemistry",
                required_subjects=["Math", "Chemistry"],
                required_subject_codes=["MATH", "CHEMISTRY"],
                cutoff_scores=[],
                score=1.0,
            ),
        ])
        latest_group = FakeRecord(
            high_school_code="SCHOOL",
            high_school_name="THPT A",
            academic_year="2026-2027",
            subject_group_code="A01",
            subject_group_name="Nhóm A01",
            subjects=[
                {"code": "MATH", "canonical_code": "MATH", "name": "Toán"},
                {"code": "PHYSICS", "canonical_code": "PHYSICS", "name": "Vật lí"},
            ],
        )
        older_group = FakeRecord(
            high_school_code="SCHOOL",
            high_school_name="THPT A",
            academic_year="2025-2026",
            subject_group_code="OLD",
            subject_group_name="Nhóm cũ",
            subjects=[{"code": "CHEMISTRY", "canonical_code": "CHEMISTRY", "name": "Hóa học"}],
        )
        session.run.side_effect = [
            admission_rows,
            FakeResult([latest_group, older_group]),
            FakeResult([FakeRecord(subject_codes=["LITERATURE"])]),
        ]
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")
        retriever._driver = driver

        result = retriever.retrieve_context({"high_school": "THPT A"}, top_k=3)

        self.assertEqual(len(result), 1)
        self.assertEqual([item["id"] for item in result[0]["combinations"]], ["MATCH"])
        self.assertEqual(result[0]["subject_group_code"], "A01")
        self.assertEqual(result[0]["matching_subject_groups"][0]["academic_year"], "2026-2027")
        self.assertEqual(result[0]["combinations"][0]["matching_subject_groups"][0]["subject_group_code"], "A01")
        self.assertIn("Nhóm môn phù hợp: THPT A - Nhóm A01", retriever.format_for_llm(result))
        group_query_call = session.run.call_args_list[1]
        self.assertIn("OFFERS_GROUP", group_query_call.args[0])
        self.assertNotIn("THPT A", group_query_call.args[0])
        self.assertEqual(group_query_call.kwargs["high_school_name"], "thpt a")
        self.assertEqual(group_query_call.kwargs["high_school_code"], "THPT A")


if __name__ == "__main__":
    unittest.main()

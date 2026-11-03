import unittest
import time
from unittest.mock import MagicMock

from rag_api.retrieval.graph_retriever import (
    Neo4jRetriever, SCHOOL_GROUPS_QUERY, _matching_school_groups, _subject_overlap_groups,
    validate_evidence_dependencies,
)


class FakeRecord(dict):
    def data(self):
        return dict(self)


class FakeResult(list):
    def single(self):
        return self[0] if self else None


class GraphRetrieverSubjectGroupFilterTests(unittest.TestCase):
    def test_university_abbreviation_is_expanded_before_graph_filtering(self):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        session.run.side_effect = [FakeResult([]), FakeResult([]), FakeResult([])]
        retriever = Neo4jRetriever.__new__(Neo4jRetriever)
        retriever.driver = driver
        retriever.database = "neo4j"

        with unittest.mock.patch.object(retriever, "get_driver", return_value=driver), \
                unittest.mock.patch.object(retriever, "_get_embedding",
                                           side_effect=AssertionError("explicit program filters must not embed")):
            retriever.retrieve_context({
                "university": "ĐH Quy Nhơn",
                "program_names": ["Toán ứng dụng"],
                "keyword": "Toán ứng dụng",
            })

        self.assertEqual("Đại học Quy Nhơn", session.run.call_args_list[0].kwargs["university"])

    def test_audit_program_filter_does_not_match_unrelated_accounting_acca_candidate(self):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        session.run.side_effect = [FakeResult([])]
        retriever = Neo4jRetriever.__new__(Neo4jRetriever)
        retriever.driver = driver
        retriever.database = "neo4j"

        with unittest.mock.patch.object(retriever, "get_driver", return_value=driver), \
                unittest.mock.patch.object(retriever, "_get_embedding",
                                           side_effect=AssertionError("explicit program filter must not use vector candidates")):
            retriever.retrieve_context({"university": "DQN", "program_names": ["Ki\u1ec3m to\u00e1n"],
                                        "keyword": "Ki\u1ec3m to\u00e1n"}, top_k=20)

        query = " ".join(session.run.call_args.args[0].split())
        self.assertIn("requested_program_names", query)
        self.assertIn("any(requested_name IN $requested_program_names", query)
        self.assertEqual(["Ki\u1ec3m to\u00e1n"], session.run.call_args.kwargs["requested_program_names"])
        requested = "Ki\u1ec3m to\u00e1n".casefold()
        unrelated = "K\u1ebf to\u00e1n ACCA".casefold()
        self.assertNotIn(requested, unrelated)

    def test_explicit_program_name_constraints_bypass_vector_top_k_and_are_parameterized(self):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        session.run.side_effect = [FakeResult([])]
        retriever = Neo4jRetriever.__new__(Neo4jRetriever)
        retriever.driver = driver
        retriever.database = "neo4j"

        with unittest.mock.patch.object(retriever, "get_driver", return_value=driver), \
                unittest.mock.patch.object(retriever, "_get_embedding", side_effect=AssertionError("vector search is not used")):
            retriever.retrieve_context({
                "university": "DQN", "admission_year": "2026",
                "program_names": ["Công nghệ thông tin (An toàn, an ninh mạng)",
                                  "Công nghệ kỹ thuật ô tô"],
                "keyword": "Công nghệ thông tin và Công nghệ kỹ thuật ô tô",
            }, top_k=1)

        query = session.run.call_args.args[0]
        compact_query = " ".join(query.split())
        params = session.run.call_args.kwargs
        self.assertIn("requested_program_names", query)
        self.assertIn("any(requested_name IN $requested_program_names", query)
        self.assertIn("normalize(toLower(p.name), NFC) CONTAINS normalize(toLower(requested_name), NFC)",
                      compact_query)
        self.assertEqual(["Công nghệ thông tin (An toàn, an ninh mạng)",
                          "Công nghệ kỹ thuật ô tô"], params["requested_program_names"])
        self.assertEqual(2026, params["admission_year"])

    def test_scoped_partial_overlap_returns_only_requested_group_without_claiming_full_eligibility(self):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        session.run.side_effect = [
            FakeResult([
                FakeRecord(
                    program_name="Nông học", program_code="7620109", university_name="Đại học Quy Nhơn",
                    university_code="DQN", combination_id="BIOLOGY_TECHNOLOGY_MATH",
                    combination_name="(Sinh học, Công nghệ, Toán)",
                    required_subjects=["Sinh học", "Công nghệ", "Toán"],
                    required_subject_codes=["BIOLOGY", "TECHNOLOGY", "MATH"],
                    required_aptitude_codes=[], required_aptitude=[],
                    acceptance_records=[{"admission_year": 2026, "method_codes": "100", "source_url": "catalog"}],
                    cutoff_scores=[], score=0.91,
                ),
                FakeRecord(
                    program_name="Giáo dục Mầm non", program_code="7140201", university_name="Đại học Quy Nhơn",
                    university_code="DQN", combination_id="BIOLOGY_OTHER", combination_name="(Sinh học, khác)",
                    required_subjects=["Sinh học"], required_subject_codes=["BIOLOGY"],
                    required_aptitude_codes=[], required_aptitude=[], acceptance_records=[], cutoff_scores=[], score=0.92,
                ),
            ]),
            FakeResult([
                FakeRecord(high_school_code="TVQN", high_school_name="Trường THPT Trưng Vương",
                           academic_year="2026", subject_group_code="G02", subject_group_name="Nhóm môn 2",
                           subjects=[{"code": "MATH", "canonical_code": "MATH", "name": "Toán"}]),
                FakeRecord(high_school_code="TVQN", high_school_name="Trường THPT Trưng Vương",
                           academic_year="2026", subject_group_code="G03", subject_group_name="Nhóm môn 3",
                           subjects=[
                               {"code": "BIOLOGY", "canonical_code": "BIOLOGY", "name": "Sinh học"},
                               {"code": "TECH_BIOLOGY", "canonical_code": "TECHNOLOGY",
                                "variant_relationship_observed": True, "name": "Công nghệ Sinh"},
                           ]),
            ]),
            FakeResult([FakeRecord(subject_codes=["MATH", "LITERATURE", "ENGLISH", "HISTORY"])]),
        ]
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("reader", "secret"), database="edugraph")
        retriever._driver = driver

        rows = retriever.retrieve_context({
            "keyword": "Nông học", "university": "Đại học Quy Nhơn", "high_school_code": "TVQN",
            "high_school": "Trường THPT Trưng Vương", "academic_year": "2026",
            "subject_group_code": "G03", "answer_scope": "subject_overlap",
        }, top_k=20)
        rows = retriever.attach_evidence_provenance(rows, filters={
            "high_school_code": "TVQN", "academic_year": "2026", "subject_group_code": "G03",
            "answer_scope": "subject_overlap",
        })

        self.assertEqual(["7620109"], [row["program_code"] for row in rows])
        self.assertEqual(["G03"], [group["subject_group_code"] for group in rows[0]["matching_subject_groups"]])
        self.assertFalse(rows[0]["evidence_dependency_validation"]["status"] == "complete")
        overlaps = rows[0]["verified_subject_overlaps"]
        self.assertEqual({"BIOLOGY", "TECHNOLOGY"}, {row["subject_code"] for row in overlaps})
        variant = next(row for row in overlaps if row["subject_code"] == "TECHNOLOGY")
        self.assertEqual("TECH_BIOLOGY", variant["observed_group_subject_code"])
        self.assertIn("is-variant-of:", " ".join(variant["relationship_ids"]))
        self.assertTrue(all(row["validation_status"] == "verified" for row in overlaps))
        context = retriever.format_for_llm(rows)
        self.assertIn("Verified subject overlap", context)
        self.assertIn("does not establish complete admission eligibility", context)
        self.assertNotIn("G02", context)
        self.assertEqual("G03", session.run.call_args_list[1].kwargs["subject_group_code"])
        self.assertEqual("2026", session.run.call_args_list[1].kwargs["academic_year"])
        self.assertTrue(session.run.call_args_list[0].kwargs["partial_subject_overlap"])
        self.assertIn("toLower(p.name) CONTAINS toLower($keyword)", session.run.call_args_list[0].args[0])

    def test_public_group_query_excludes_unpublished_groups(self):
        self.assertIn("coalesce(sg.is_published, true) = true", SCHOOL_GROUPS_QUERY)

    def test_subject_overlap_requires_observed_directed_variant_relationship(self):
        group = {
            "high_school_code": "TVQN", "academic_year": "2026", "subject_group_code": "G03",
            "subjects": [{"code": "TECH_BIOLOGY", "canonical_code": "TECHNOLOGY",
                          "variant_relationship_observed": False}],
        }
        self.assertEqual([], _subject_overlap_groups(
            ["TECHNOLOGY"], [group], academic_year="2026", subject_group_code="G03",
        ))

    def test_subject_overlap_respects_school_group_year_and_program_scope(self):
        exact = {
            "high_school_code": "TVQN", "academic_year": "2026", "subject_group_code": "G03",
            "subjects": [{"code": "BIOLOGY", "canonical_code": "BIOLOGY"}],
        }
        other_year = {**exact, "academic_year": "2025"}
        other_group = {**exact, "subject_group_code": "G02"}
        matched = _subject_overlap_groups(
            ["BIOLOGY"], [exact, other_year, other_group], academic_year="2026", subject_group_code="G03",
        )
        self.assertEqual(["G03"], [row["subject_group_code"] for row in matched])

    def test_global_mandatory_hint_cannot_certify_scoped_group_membership(self):
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
            [],
        )
        self.assertEqual([group], _matching_school_groups(["MATH", "PHYSICS"], [group], []))
        self.assertEqual(_matching_school_groups(["MATH", "CHEMISTRY"], [group], []), [])
        self.assertEqual(_matching_school_groups([], [group], []), [])

    def test_explicit_academic_year_overrides_default_latest_year(self):
        groups = [
            {"high_school_code": "SCHOOL", "academic_year": "2026-2027", "subjects": ["MATH"]},
            {"high_school_code": "SCHOOL", "academic_year": "2025-2026", "subjects": ["MATH"]},
        ]

        self.assertEqual([groups[0]], _matching_school_groups(["MATH"], groups, []))
        self.assertEqual(
            [groups[1]],
            _matching_school_groups(["MATH"], groups, [], academic_year="2025-2026"),
        )

    def test_evidence_ids_are_stable_when_unordered_evidence_is_reordered(self):
        first = {
            "university_code": "UA", "program_code": "P1", "score": 0.8,
            "required_subject_codes": ["MATH", "PHYSICS"],
            "combinations": [{"id": "C1", "required_subject_codes": ["PHYSICS", "MATH"]}],
        }
        reordered = {
            "combinations": [{"required_subject_codes": ["MATH", "PHYSICS"], "id": "C1"}],
            "required_subject_codes": ["PHYSICS", "MATH"],
            "program_code": "P1", "score": 0.2, "university_code": "UA",
        }
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")

        first_result = retriever.attach_evidence_provenance([first])[0]
        second_result = retriever.attach_evidence_provenance([reordered])[0]

        self.assertEqual(first_result["evidence_id"], second_result["evidence_id"])
        self.assertTrue(first_result["relationship_ids"])
        self.assertTrue(first_result["evidence_path"])
        self.assertIn(first_result["evidence_id"], retriever.format_for_llm([first_result]))

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
                required_aptitude=[{"code": "APTITUDE_TEST", "name": "Aptitude test"}],
                acceptance_records=[{
                    "admission_year": 2025, "method_codes": "100",
                    "source_url": "https://university.test/admissions",
                }],
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

        result = retriever.retrieve_context({
            "high_school": "THPT A", "admission_year": 2025, "method_code": "100",
        }, top_k=3)

        self.assertEqual(len(result), 1)
        self.assertEqual([item["id"] for item in result[0]["combinations"]], ["APTITUDE"])
        self.assertEqual(result[0]["subject_group_code"], "A01")
        self.assertEqual(result[0]["matching_subject_groups"][0]["academic_year"], "2026-2027")
        self.assertEqual(result[0]["combinations"][0]["matching_subject_groups"][0]["subject_group_code"], "A01")
        self.assertIn("Nhóm môn phù hợp: THPT A - Nhóm A01", retriever.format_for_llm(result))
        group_query_call = session.run.call_args_list[1]
        self.assertIn("OFFERS_GROUP", group_query_call.args[0])
        self.assertNotIn("THPT A", group_query_call.args[0])
        self.assertEqual(group_query_call.kwargs["high_school_name"], "thpt a")
        self.assertIsNone(group_query_call.kwargs["high_school_code"])
        admission_query = session.run.call_args_list[0]
        self.assertEqual(2025, admission_query.kwargs["admission_year"])
        self.assertEqual("100", admission_query.kwargs["method_code"])
        self.assertIn("r_acc_f.admission_year = $admission_year", admission_query.args[0])
        self.assertIn("$method_code IN split", admission_query.args[0])
        self.assertIn("CASE WHEN apt IS NULL THEN null", admission_query.args[0])
        formatted = retriever.format_for_llm(result)
        self.assertIn("Required aptitude evidence: Aptitude test", formatted)
        self.assertIn("admission year 2025", formatted)
        self.assertIn("method codes 100", formatted)
        self.assertIn("https://university.test/admissions", formatted)

    def test_candidate_limit_is_applied_to_unique_programs_before_relationship_expansion(self):
        """Regression for U2: many combinations on one program must not starve other programs."""
        expanded_rows = []
        for program_code in ("P1", "P2"):
            for index in range(3):
                expanded_rows.append(FakeRecord(
                    program_name=f"Program {program_code}",
                    program_code=program_code,
                    university_name="University A",
                    university_code="UA",
                    combination_id=f"{program_code}-C{index}",
                    combination_name=f"Combination {index}",
                    required_subjects=["Math"],
                    required_subject_codes=["MATH"],
                    required_aptitude_codes=[],
                    required_aptitude=[],
                    acceptance_records=[{
                        "admission_year": 2026, "method_codes": "100", "source_url": "source"
                    }],
                    cutoff_scores=[],
                    score=1.0,
                ))

        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        session.run.return_value = FakeResult(expanded_rows)
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")
        retriever._driver = driver

        started = time.perf_counter()
        result = retriever.retrieve_context({"university": "UA"}, top_k=2)
        elapsed_ms = (time.perf_counter() - started) * 1000

        query = session.run.call_args.args[0]
        self.assertLess(query.index("WITH DISTINCT u, p"), query.index("OPTIONAL MATCH (p)-[r_acc"))
        self.assertLess(query.index("LIMIT $candidate_limit"), query.index("OPTIONAL MATCH (p)-[r_acc"))
        self.assertNotIn("LIMIT $candidate_limit\n            \"\"\"", query)
        self.assertEqual(2, session.run.call_args.kwargs["candidate_limit"])
        self.assertEqual({"P1", "P2"}, {row["program_code"] for row in result})
        self.assertEqual(1.0, len(result) / 2)  # top-k unique-program coverage on the fixture
        self.assertEqual([3, 3], sorted(len(row["combinations"]) for row in result))
        self.assertGreaterEqual(elapsed_ms, 0.0)  # offline Python overhead only; not a Neo4j latency claim

    def test_multi_hop_evidence_dependencies_require_each_path_edge(self):
        record = {
            "university_code": "UA",
            "program_code": "P1",
            "combinations": [{
                "id": "C1",
                "acceptance_records": [{"admission_year": 2026, "method_codes": "100"}],
                "required_subject_codes": ["MATH", "PHYSICS"],
                "required_aptitude_codes": ["ART"],
                "matching_subject_groups": [{
                    "high_school_code": "SCHOOL", "academic_year": "2026-2027",
                    "subject_group_code": "G1",
                    "subjects": [{"code": "MATH", "canonical_code": "MATH"},
                                 {"code": "PHYSICS", "canonical_code": "PHYSICS"}],
                }],
            }],
        }
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")
        enriched = retriever.attach_evidence_provenance([record])[0]

        complete = validate_evidence_dependencies(enriched, {
            "admission_year": 2026, "method_code": "100",
            "high_school_code": "SCHOOL", "academic_year": "2026-2027",
        })
        self.assertEqual("complete", complete["status"])
        self.assertEqual("unverified", complete["aptitude_coverage"])
        required_edge_count = len(complete["required_relationship_ids"])
        subject_edge = next(edge for edge in enriched["evidence_path"]
                            if edge["relationship_id"].startswith("requires-subject:"))
        incomplete_record = {
            **enriched,
            "evidence_path": [edge for edge in enriched["evidence_path"]
                             if edge is not subject_edge],
        }
        incomplete = validate_evidence_dependencies(incomplete_record)

        self.assertEqual(required_edge_count, len(complete["required_relationship_ids"]))
        self.assertEqual("incomplete", incomplete["status"])
        self.assertIn(subject_edge["relationship_id"], incomplete["missing_relationship_ids"])
        wrong_endpoint = {
            **enriched,
            "evidence_path": [
                {**edge, "from": "AdmissionCombination:WRONG"}
                if edge is subject_edge else edge
                for edge in enriched["evidence_path"]
            ],
        }
        invalid_path = validate_evidence_dependencies(wrong_endpoint)
        self.assertEqual("incomplete", invalid_path["status"])
        self.assertIn(subject_edge["relationship_id"], invalid_path["missing_relationship_ids"])
        partial_endpoint = {
            **enriched,
            "evidence_path": [
                {key: value for key, value in edge.items() if key != "to"}
                if edge is subject_edge else edge
                for edge in enriched["evidence_path"]
            ],
        }
        partial_validation = validate_evidence_dependencies(partial_endpoint)
        self.assertEqual("incomplete", partial_validation["status"])
        self.assertIn(subject_edge["relationship_id"], partial_validation["missing_relationship_ids"])
        duplicate_edge = {
            **enriched,
            "evidence_path": [*enriched["evidence_path"], dict(subject_edge)],
        }
        duplicate_validation = validate_evidence_dependencies(duplicate_edge)
        self.assertEqual("incomplete", duplicate_validation["status"])
        self.assertIn(subject_edge["relationship_id"], duplicate_validation["duplicate_relationship_ids"])
        self.assertIn(subject_edge["relationship_id"],
                      duplicate_validation["invalid_duplicate_relationship_ids"])
        wrong_scope = validate_evidence_dependencies(enriched, {
            "admission_year": 2026, "method_code": "200",
            "high_school_code": "SCHOOL", "academic_year": "2026-2027",
        })
        self.assertEqual("incomplete", wrong_scope["status"])
        self.assertTrue(any(item.startswith("acceptance_scope:")
                            for item in wrong_scope["missing_scope_dependencies"]))
        wrong_group_scope = validate_evidence_dependencies(enriched, {
            "admission_year": 2026, "method_code": "100",
            "high_school_code": "SCHOOL", "academic_year": "2026-2027",
            "subject_group_code": "G2",
        })
        self.assertEqual("incomplete", wrong_group_scope["status"])
        self.assertTrue(any(item.startswith("subject_group_scope:")
                            for item in wrong_group_scope["missing_scope_dependencies"]))

    def test_school_group_scope_accepts_catalog_name_without_optional_truong_prefix(self):
        record = {
            "university_code": "DQN",
            "program_code": "7340302",
            "combinations": [{
                "id": "ENGLISH_MATH_PHYSICS",
                "acceptance_records": [{"admission_year": 2026, "method_codes": "100"}],
                "required_subject_codes": ["MATH", "PHYSICS"],
                "matching_subject_groups": [{
                    "high_school_code": "TCV",
                    "high_school_name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n",
                    "academic_year": "2026",
                    "subject_group_code": "G01",
                    "subjects": [{"code": "MATH", "canonical_code": "MATH"},
                                 {"code": "PHYSICS", "canonical_code": "PHYSICS"}],
                }],
            }],
        }
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")
        enriched = retriever.attach_evidence_provenance([record])[0]

        result = validate_evidence_dependencies(enriched, {
            "high_school": "THPT Tr\u1ea7n Cao V\u00e2n",
            "academic_year": "2026",
        })

        self.assertEqual("complete", result["status"])
        self.assertNotIn("school_group_scope", result["missing_scope_dependencies"])


if __name__ == "__main__":
    unittest.main()

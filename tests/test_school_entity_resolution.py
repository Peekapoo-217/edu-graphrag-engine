import unittest
from unittest.mock import MagicMock, patch

from rag_api.main_api import (
    ChatRequest,
    _has_explicit_school_marker,
    _resolve_explicit_graph_group_scope,
    _resolve_explicit_school_filter,
    chat_endpoint,
)
from rag_api.retrieval.graph_retriever import Neo4jRetriever, SCHOOL_GROUPS_QUERY


class FakeRecord(dict):
    def data(self):
        return dict(self)


class FakeResult(list):
    def single(self):
        return self[0] if self else None


class SchoolEntityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.schools = [
            {"code": "TVQN", "name": "Trường THPT Trưng Vương"},
            {"code": "TCV", "name": "Trường THPT Trần Cao Vân"},
        ]

    def test_parser_raw_location_qualified_school_is_canonicalized_to_catalog_identity(self):
        parsed = {
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "THPT Trưng Vương ở Quy Nhơn",
                "desired_program": "Công nghệ thông tin",
                "academic_year": "2026-2027",
            },
        }

        with patch("rag_api.main_api._backend_get", return_value=self.schools):
            resolved = _resolve_explicit_school_filter(
                parsed,
                "THPT Trưng Vương ở Quy Nhơn có nhóm nào phù hợp với ngành Công nghệ thông tin?",
            )

        filters = resolved["extracted_filters"]
        self.assertEqual("TVQN", filters["high_school_code"])
        self.assertEqual(self.schools[0]["name"], filters["high_school"])
        self.assertEqual("2026-2027", filters["academic_year"])
        self.assertEqual("Công nghệ thông tin", filters["desired_program"])
        self.assertEqual("resolved", resolved["school_resolution"]["status"])

    def test_general_graph_route_uses_the_same_canonical_school_filter(self):
        with patch("rag_api.main_api._backend_get", return_value=self.schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "general", "extracted_filters": {"high_school": "THPT Trưng Vương"}},
                "THPT Trưng Vương có nhóm nào?",
            )

        self.assertEqual("TVQN", resolved["extracted_filters"]["high_school_code"])

    def test_school_group_route_uses_catalog_resolver_and_preserves_year(self):
        parsed = {
            "intent": "school_group_lookup",
            "extracted_filters": {
                "high_school": "THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n",
                "academic_year": "2026-2027",
            },
        }
        with patch("rag_api.main_api._backend_get", return_value=self.schools):
            resolved = _resolve_explicit_school_filter(
                parsed, "THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n c\u00f3 nh\u00f3m n\u00e0o?"
            )

        self.assertEqual("TVQN", resolved["extracted_filters"]["high_school_code"])
        self.assertEqual("2026-2027", resolved["extracted_filters"]["academic_year"])
        self.assertEqual("backend_high_school_catalog", resolved["school_resolution"]["source"])

    def test_school_group_route_does_not_pass_ambiguous_school_to_legacy_resolver(self):
        schools = [
            {"code": "TVQN", "name": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng"},
            {"code": "TVHN", "name": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng"},
        ]
        with patch("rag_api.main_api._backend_get", return_value=schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "school_group_lookup", "extracted_filters": {"high_school": "THPT Tr\u01b0ng V\u01b0\u01a1ng"}},
                "THPT Tr\u01b0ng V\u01b0\u01a1ng c\u00f3 nh\u00f3m n\u00e0o?",
            )

        self.assertEqual("ambiguous", resolved["school_resolution"]["status"])
        self.assertNotIn("high_school", resolved["extracted_filters"])

    def test_current_turn_school_identity_overrides_stale_parser_identity(self):
        parsed = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"},
        }
        with patch("rag_api.main_api._backend_get", return_value=self.schools):
            resolved = _resolve_explicit_school_filter(
                parsed,
                "Kh\u00f4ng ph\u1ea3i Tr\u1ea7n Cao V\u00e2n, \u00fd em l\u00e0 THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n",
                [{"role": "user", "content": "THPT Tr\u1ea7n Cao V\u00e2n c\u00f3 nh\u00f3m g\u00ec?"}],
            )

        self.assertEqual("TVQN", resolved["extracted_filters"]["high_school_code"])

    def test_university_name_does_not_count_as_explicit_high_school(self):
        self.assertFalse(_has_explicit_school_marker("Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n"))

    def test_university_only_question_does_not_call_school_catalog(self):
        parsed = {"intent": "general", "extracted_filters": {}}
        with patch("rag_api.main_api._backend_get") as backend_get:
            resolved = _resolve_explicit_school_filter(parsed, "Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n")

        backend_get.assert_not_called()
        self.assertNotIn("school_resolution", resolved)

    def test_malformed_catalog_fails_closed(self):
        parsed = {"intent": "school_group_lookup", "extracted_filters": {"high_school": "TVQN"}}
        with patch("rag_api.main_api._backend_get", return_value=[{"code": "TVQN"}]):
            resolved = _resolve_explicit_school_filter(parsed, "THPT Tr\u01b0ng V\u01b0\u01a1ng")

        self.assertEqual("catalog_unavailable", resolved["school_resolution"]["status"])
        self.assertNotIn("high_school_code", resolved["extracted_filters"])

    def test_school_group_request_reuses_catalog_resolution_without_legacy_lookup(self):
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {
                "high_school": "THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n",
                "academic_year": "2026-2027",
            },
        }
        backend_responses = [
            self.schools,
            ["2026-2027"],
            [{"groupCode": "G01", "groupName": "Nh\u00f3m 01", "subjects": [{"name": "To\u00e1n"}]}],
        ]
        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", MagicMock()),
            patch("rag_api.main_api.generator", MagicMock()),
            patch("rag_api.main_api._backend_get", side_effect=backend_responses) as backend_get,
        ):
            response = chat_endpoint(ChatRequest(
                user_message="THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n c\u00f3 nh\u00f3m n\u00e0o?"
            ))

        self.assertEqual("answer", response.data["response_mode"])
        self.assertEqual("backend_high_school_catalog", response.data["school_resolution"]["source"])
        self.assertEqual("TVQN", response.data["school"]["code"])
        self.assertEqual(3, backend_get.call_count)
        self.assertIn("/TVQN/academic-years", backend_get.call_args_list[1].args[0])
        self.assertIn("academicYear=2026-2027", backend_get.call_args_list[2].args[0])

    def test_location_qualifier_disambiguates_only_when_catalog_name_verifies_it(self):
        schools = [
            {"code": "TVQN", "name": "Trường THPT Trưng Vương Quy Nhơn"},
            {"code": "TVHN", "name": "Trường THPT Trưng Vương Hà Nội"},
        ]

        with patch("rag_api.main_api._backend_get", return_value=schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "pathway_lookup", "extracted_filters": {}},
                "THPT Trưng Vương ở Quy Nhơn có nhóm nào liên quan đến ngành này?",
            )

        self.assertEqual("TVQN", resolved["extracted_filters"]["high_school_code"])

    def test_location_metadata_from_catalog_can_disambiguate_a_shared_school_name(self):
        schools = [
            {"code": "TVQN", "name": "Trường THPT Trưng Vương", "location": "Quy Nhơn, Bình Định"},
            {"code": "TVHN", "name": "Trường THPT Trưng Vương", "location": "Hà Nội"},
        ]

        with patch("rag_api.main_api._backend_get", return_value=schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "pathway_lookup", "extracted_filters": {}},
                "THPT Trưng Vương ở Quy Nhơn có nhóm nào?",
            )

        self.assertEqual("TVQN", resolved["extracted_filters"]["high_school_code"])

    def test_exact_location_qualified_group_and_advice_question_routes_to_scoped_backend_lookup(self):
        question = (
            "Em dự định đăng ký vào trường THPT Trưng Vương ở Quy Nhơn năm 2026. "
            "Em muốn hỏi nhóm môn 2 (G02) của trường gồm những môn gì và học nhóm này "
            "thì phù hợp với những ngành nghề nào ạ?"
        )
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {}, "high_schools": [],
            "academic_year": None, "requested_group_codes": [],
            "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [], "grades_by_subject": [],
        }
        generator_mock = MagicMock()
        generator_mock.generate_answer.return_value = "Verified group lookup result."
        group_g01 = {"groupCode": "G01", "groupName": "Nhóm môn 1", "subjects": [{"name": "Sinh học"}]}
        group_g02 = {"groupCode": "G02", "groupName": "Nhóm môn 2", "subjects": [{"name": "Địa lí"}]}
        backend = [
            self.schools,
            ["2026"],
            [group_g01, group_g02],
        ]
        retriever_mock = MagicMock()
        retriever_mock.format_for_llm.return_value = ""
        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", generator_mock),
            patch("rag_api.main_api._backend_get", side_effect=backend) as backend_get,
        ):
            response = chat_endpoint(ChatRequest(user_message=question))

        self.assertEqual("answer", response.data["response_mode"])
        self.assertEqual("TVQN", response.data["school"]["code"])
        self.assertEqual("2026", response.data["academic_year"])
        self.assertEqual(["G02"], [row["groupCode"] for row in response.data["evidence"]])
        self.assertEqual("resolved", response.data["school_resolution"]["status"])
        self.assertTrue(any("academicYear=2026" in call.args[0] for call in backend_get.call_args_list))
        self.assertEqual("G02", response.data["group_code_resolutions"][0]["canonical_code"])
        self.assertNotIn("Em cho biết mã tổ hợp, trường THPT", response.message)

    def test_unique_unqualified_school_assumption_is_transparent_and_uses_catalog_location(self):
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "Trưng Vương"},
            "requested_group_codes": [],
        }
        schools = [{
            "code": "TVQN", "name": "Trường THPT Trưng Vương", "location": "Quy Nhơn",
        }]
        groups = [{"groupCode": "G02", "groupName": "Nhóm 2", "subjects": [{"name": "Sinh học"}]}]
        retriever_mock, generator_mock = MagicMock(), MagicMock()
        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", generator_mock),
            patch("rag_api.main_api._backend_get", side_effect=[schools, ["2026"], groups]),
        ):
            response = chat_endpoint(ChatRequest(
                user_message="Cho em xem các nhóm môn của trường Trưng Vương."
            ))

        self.assertEqual("answer", response.data["response_mode"])
        self.assertEqual("TVQN", response.data["school"]["code"])
        self.assertIn("Quy Nhơn", response.message)
        self.assertIn("tạm hiểu", response.message)

    def test_ambiguous_unqualified_school_name_still_requests_clarification(self):
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "Trưng Vương"},
            "requested_group_codes": [],
        }
        schools = [
            {"code": "TVQN", "name": "Trường THPT Trưng Vương", "location": "Quy Nhơn"},
            {"code": "TVHN", "name": "Trường THPT Trưng Vương", "location": "Hà Nội"},
        ]
        retriever_mock, generator_mock = MagicMock(), MagicMock()
        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", generator_mock),
            patch("rag_api.main_api._backend_get", return_value=schools) as backend_get,
        ):
            response = chat_endpoint(ChatRequest(
                user_message="Cho em xem các nhóm môn của trường Trưng Vương."
            ))

        self.assertEqual("clarification", response.data["response_mode"])
        self.assertIn("tỉnh/thành", response.message)
        self.assertEqual(1, backend_get.call_count)

    def test_graph_group_alias_resolves_only_within_school_and_year_catalog(self):
        intent = {
            "intent": "pathway_lookup", "answer_scope": "subject_overlap",
            "answer_scope_source": "deterministic_explicit_scope",
            "academic_year": "2026", "extracted_filters": {"high_school_code": "TVQN"},
            "requested_group_codes": [{"code": "2", "evidence": "nhóm môn 2"}],
        }
        groups = [{"groupCode": "G02", "groupName": "Nhóm môn 2", "academicYear": "2026"}]
        with patch("rag_api.main_api._backend_get", side_effect=[["2026"], groups]) as backend:
            resolved = _resolve_explicit_graph_group_scope(intent)
        self.assertEqual("resolved", resolved["graph_group_scope_resolution"]["status"])
        self.assertEqual("G02", resolved["resolved_subject_group_code"])
        self.assertEqual("2026", resolved["academic_year"])
        self.assertEqual(2, backend.call_count)

    def test_graph_group_numeric_alias_collision_stays_unresolved(self):
        intent = {
            "intent": "pathway_lookup", "answer_scope": "subject_overlap",
            "answer_scope_source": "deterministic_explicit_scope",
            "academic_year": "2026", "extracted_filters": {"high_school_code": "TVQN"},
            "requested_group_codes": [{"code": "2", "evidence": "nhóm môn 2"}],
        }
        groups = [
            {"groupCode": "G02", "groupName": "Nhóm môn 2", "academicYear": "2026"},
            {"groupCode": "2", "groupName": "Alternative 2", "academicYear": "2026"},
        ]
        with patch("rag_api.main_api._backend_get", side_effect=[["2026"], groups]):
            resolved = _resolve_explicit_graph_group_scope(intent)
        self.assertEqual("ambiguous", resolved["graph_group_scope_resolution"]["status"])
        self.assertNotIn("resolved_subject_group_code", resolved)

    def test_graph_partial_candidate_without_verified_overlap_does_not_generate(self):
        question = (
            "Trường THPT Trưng Vương ở Quy Nhơn có Nhóm môn 3 gồm những môn nào, và môn nào "
            "trong đó nằm trong tổ hợp xét tuyển ngành Nông học của Đại học Quy Nhơn?"
        )
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"desired_program": "Nông học",
            "university": "Đại học Quy Nhơn"}, "high_schools": [], "academic_year": "2026",
            "requested_group_codes": [], "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [], "grades_by_subject": [],
        }
        partial_record = {
            "program_code": "7620109", "answer_scope": "subject_overlap",
            "evidence_dependency_validation": {"status": "incomplete"},
            "verified_subject_overlaps": [], "evidence_path": [], "relationship_ids": [],
        }
        retriever_mock = MagicMock()
        retriever_mock.retrieve_context.return_value = [partial_record]
        retriever_mock.attach_evidence_provenance.return_value = [partial_record]
        groups = [{"groupCode": "G03", "groupName": "Nhóm môn 3", "academicYear": "2026"}]
        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", MagicMock()) as generator_mock,
            patch("rag_api.main_api._backend_get", side_effect=[self.schools, ["2026"], groups]),
        ):
            response = chat_endpoint(ChatRequest(user_message=question))
        self.assertEqual("incomplete", response.status)
        self.assertEqual("subject_overlap", response.data["filters"]["answer_scope"])
        self.assertEqual("G03", response.data["filters"]["subject_group_code"])
        self.assertFalse(response.data["generation_executed"])
        generator_mock.generate_answer.assert_not_called()

    def test_same_alias_without_catalog_location_stays_ambiguous_and_drops_raw_filter(self):
        schools = [
            {"code": "TVQN", "name": "Trường THPT Trưng Vương"},
            {"code": "TVHN", "name": "Trường THPT Trưng Vương"},
        ]
        raw_filter = "THPT Trưng Vương ở Quy Nhơn"

        with patch("rag_api.main_api._backend_get", return_value=schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "pathway_lookup", "extracted_filters": {"high_school": raw_filter}},
                raw_filter + " có nhóm môn nào?",
            )

        self.assertEqual("ambiguous", resolved["school_resolution"]["status"])
        self.assertNotIn("high_school", resolved["extracted_filters"])
        self.assertNotIn("high_school_code", resolved["extracted_filters"])
        self.assertEqual({"TVHN", "TVQN"}, {
            row["code"] for row in resolved["school_resolution"]["candidates"]
        })

    def test_unknown_explicit_school_is_not_forwarded_as_a_graph_filter(self):
        raw_filter = "THPT Trường Không Có Trong Danh Mục"
        with patch("rag_api.main_api._backend_get", return_value=self.schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "pathway_lookup", "extracted_filters": {"high_school": raw_filter}},
                raw_filter + " có nhóm nào?",
            )

        self.assertEqual("unknown", resolved["school_resolution"]["status"])
        self.assertNotIn("high_school", resolved["extracted_filters"])
        self.assertNotIn("high_school_code", resolved["extracted_filters"])

    def test_follow_up_school_reference_resolves_only_from_prior_user_message(self):
        prior_turns = [
            {"role": "user", "content": "THPT Trưng Vương ở Quy Nhơn có nhóm môn nào?"},
            {"role": "assistant", "content": "THPT Trần Cao Vân cũng có nhóm môn."},
        ]

        with patch("rag_api.main_api._backend_get", return_value=self.schools):
            resolved = _resolve_explicit_school_filter(
                {"intent": "pathway_lookup", "extracted_filters": {"high_school": "THPT Trưng Vương"}},
                "Ngành Công nghệ thông tin có liên kết với trường đó không?",
                prior_turns,
            )

        self.assertEqual("TVQN", resolved["extracted_filters"]["high_school_code"])

    def test_unknown_or_ambiguous_school_stops_before_graph_retrieval(self):
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "THPT Trưng Vương ở Quy Nhơn"},
        }
        retriever_mock = MagicMock()
        duplicate_schools = [
            {"code": "TVQN", "name": "Trường THPT Trưng Vương"},
            {"code": "TVHN", "name": "Trường THPT Trưng Vương"},
        ]

        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", MagicMock()),
            patch("rag_api.main_api._backend_get", return_value=duplicate_schools),
        ):
            response = chat_endpoint(ChatRequest(
                user_message="THPT Trưng Vương ở Quy Nhơn có nhóm nào phù hợp với ngành Công nghệ thông tin?"
            ))

        self.assertEqual("incomplete", response.status)
        self.assertEqual("clarification", response.data["response_mode"])
        retriever_mock.retrieve_context.assert_not_called()

    def test_resolved_school_catalog_provenance_is_returned_with_graph_answer(self):
        parser_mock = MagicMock()
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "THPT Trưng Vương ở Quy Nhơn",
                "desired_program": "Công nghệ thông tin",
            },
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock = MagicMock()
        retriever_mock.retrieve_context.return_value = [{"evidence_id": "graph:path-1"}]
        retriever_mock.format_for_llm.return_value = "Verified school-group and admission path."
        generator_mock = MagicMock()
        generator_mock.generate_answer.return_value = "One verified example."

        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", generator_mock),
            patch("rag_api.main_api._backend_get", return_value=self.schools),
        ):
            response = chat_endpoint(ChatRequest(
                user_message=("THPT Trưng Vương ở Quy Nhơn có nhóm môn nào liên quan "
                              "đến ngành Công nghệ thông tin?")
            ))

        self.assertEqual("TVQN", response.data["filters"]["high_school_code"])
        self.assertEqual("resolved", response.data["school_resolution"]["status"])
        self.assertEqual("TVQN", response.data["school_resolution"]["school"]["code"])
        self.assertEqual("backend_high_school_catalog", response.data["school_resolution"]["source"])


class SchoolGroupGraphFilterTests(unittest.TestCase):
    def _run_retrieval(self, groups, academic_year="2025-2026"):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        admission = FakeResult([FakeRecord(
            program_name="Computer Science",
            program_code="7480101",
            university_name="University A",
            university_code="UA",
            combination_id="MATH",
            combination_name="Math",
            required_subjects=["Math"],
            required_subject_codes=["MATH"],
            required_aptitude_codes=[],
            required_aptitude=[],
            acceptance_records=[],
            cutoff_scores=[],
            score=0.9,
        )])
        session.run.side_effect = [
            admission,
            FakeResult(groups),
            FakeResult([FakeRecord(subject_codes=[])]),
        ]
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")
        retriever._driver = driver
        result = retriever.retrieve_context({
            "keyword": "Computer Science",
            "high_school": "Trường THPT Trưng Vương",
            "high_school_code": "TVQN",
            "academic_year": academic_year,
        }, top_k=3)
        return result, session

    def test_resolved_school_code_is_the_authoritative_group_filter_and_year_is_preserved(self):
        groups = [
            FakeRecord(
                high_school_code="TVQN", high_school_name="Trường THPT Trưng Vương",
                academic_year="2025-2026", subject_group_code="G01", subject_group_name="Nhóm 01",
                subjects=[{"code": "MATH", "canonical_code": "MATH", "name": "Toán"}],
            ),
            FakeRecord(
                high_school_code="TVQN", high_school_name="Trường THPT Trưng Vương",
                academic_year="2026-2027", subject_group_code="G02", subject_group_name="Nhóm 02",
                subjects=[{"code": "MATH", "canonical_code": "MATH", "name": "Toán"}],
            ),
        ]

        result, session = self._run_retrieval(groups)

        self.assertEqual("TVQN", session.run.call_args_list[1].kwargs["high_school_code"])
        self.assertIn("$high_school_code IS NOT NULL", SCHOOL_GROUPS_QUERY)
        self.assertEqual(["2025-2026"], [
            group["academic_year"] for group in result[0]["matching_subject_groups"]
        ])

    def test_program_candidate_is_not_returned_as_a_school_path_without_matching_groups(self):
        result, session = self._run_retrieval([])

        self.assertEqual(3, session.run.call_count)  # program candidates, school groups, mandatory subjects
        self.assertEqual([], result)


if __name__ == "__main__":
    unittest.main()

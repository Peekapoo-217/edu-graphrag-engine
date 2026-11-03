import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from rag_api.llm.intent_parser import IntentParser
from rag_api.main_api import (
    ChatRequest,
    _lookup_combination,
    _normalize_query_filters,
    app,
    chat_endpoint,
)
from rag_api.retrieval.errors import RetrievalUnavailableError


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "parser_router_diagnostic_fixtures.json"


def _not_found():
    response = Mock(status_code=404)
    return requests.HTTPError(response=response)


def _catalog(ambiguous=False):
    electives = [
        {"code": "PHYSICS", "name": "Vật lí"},
        {"code": "CHEMISTRY", "name": "Hóa học"},
        {"code": "BIOLOGY", "name": "Sinh học"},
        {"code": "INFORMATICS", "name": "Tin học"},
        {"code": "ENGLISH", "name": "Tiếng Anh"},
        {"code": "LITERATURE", "name": "Ngữ văn"},
    ]
    if ambiguous:
        electives.extend([
            {"code": "HISTORY_A", "name": "Lịch sử"},
            {"code": "HISTORY_B", "name": "Lịch sử"},
        ])
    return {"mandatorySubjects": [{"code": "MATH", "name": "Toán"}], "electiveSubjects": electives}


class ParserRouterDiagnosticFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_all_twenty_diagnostic_cases_have_a_resolvable_expected_trace(self):
        self.assertEqual(20, len(self.fixtures))
        self.assertEqual(20, len({item["case_id"] for item in self.fixtures}))
        combination_fixtures = [item for item in self.fixtures if item.get("intent") != "pathway_lookup"]
        self.assertEqual(15, len(combination_fixtures))
        for item in combination_fixtures:
            with self.subTest(case=item["case_id"]):
                code_case = item["kind"] in {"code", "code_from_text", "code_not_found", "multiple_variants"}
                catalog = _catalog(ambiguous=item["kind"] == "ambiguous_subject")
                if code_case:
                    if item["kind"] == "code_not_found":
                        get = Mock(side_effect=_not_found())
                    elif item["kind"] == "school_group_code":
                        get = Mock(return_value=catalog)
                    else:
                        get = Mock(return_value={
                            "requestedCode": item.get("filters", {}).get("combination_code", "A01"),
                            "labels": ([
                                {"code": "K01-A", "subjectDetails": "Toán, Văn, Lý", "canonicalCombinationId": "K01"},
                                {"code": "K01-B", "subjectDetails": "Toán, Văn, Hóa", "canonicalCombinationId": "K01"},
                            ] if item["kind"] == "multiple_variants" else [
                                {"code": "A01", "subjectDetails": "Toán, Vật lí, Tiếng Anh", "canonicalCombinationId": "A01"},
                                {"code": "A01-X", "subjectDetails": "Toán, Vật lí, Tiếng Anh (mô tả khác)", "canonicalCombinationId": "A01"},
                            ] if item["kind"] == "multiple_labels" else [
                                {"code": "A01", "subjectDetails": "Toán, Vật lí, Tiếng Anh", "canonicalCombinationId": "A01"}
                            ]),
                        })
                    post = Mock()
                else:
                    get = Mock(return_value=catalog)
                    if item["kind"] == "backend_error":
                        post = Mock(side_effect=requests.ConnectionError("offline fixture"))
                    elif item["kind"] == "subject_set_not_found":
                        post = Mock(side_effect=_not_found())
                    else:
                        combinations = [
                            {"combinationId": "A01", "combinationDisplay": "Toán - Vật lí - Tiếng Anh",
                             "componentCount": 3,
                             "components": [
                                 {"type": "SUBJECT", "code": code, "name": code, "subjectCode": code}
                                 for code in ("MATH", "PHYSICS", "ENGLISH")
                             ],
                             "labels": [{"code": "A01", "subjectDetails": "Toán, Vật lí, Tiếng Anh"}]},
                            {"combinationId": "X26", "combinationDisplay": "Toán - Vật lí - Tin học",
                             "componentCount": 3,
                             "components": [
                                 {"type": "SUBJECT", "code": code, "name": code, "subjectCode": code}
                                 for code in ("MATH", "PHYSICS", "INFORMATICS")
                             ],
                             "labels": [{"code": "X26", "subjectDetails": "Toán, Vật lí, Tin học"}]},
                        ]
                        post = Mock(return_value={"matches": combinations, "resultScope": "exact_subject_set"})

                with patch("rag_api.main_api._backend_get", get), \
                     patch("rag_api.main_api._backend_post", post), \
                     patch("rag_api.main_api.parser") as parser_mock, \
                     patch("rag_api.main_api.retriever") as retriever_mock, \
                     patch("rag_api.main_api.generator") as generator_mock:
                    parser_mock.parse_intent.return_value = {
                        "intent": "combination_lookup", "extracted_filters": item.get("filters", {})
                    }
                    try:
                        response = chat_endpoint(ChatRequest(user_message=item["question"]))
                        actual_status = response.status
                    except HTTPException as exc:
                        actual_status = "service_error" if exc.status_code == 503 else str(exc.status_code)
                    parser_mock.parse_intent.assert_called_once()

                self.assertEqual(item["expected_status"], actual_status)
                if get.called and "/admissions/combinations/" in get.call_args.args[0]:
                    labels = response.data.get("evidence", {}).get("labels", []) if actual_status == "success" else []
                    if len(labels) > 1:
                        actual_stage = "preserve_all_variants"
                    else:
                        parser_code = item.get("filters", {}).get("combination_code")
                        actual_stage = "code_lookup" if parser_code else "explicit_code_resolution"
                elif post.called:
                    actual_stage = "backend_exact_subject_resolver"
                elif response.data and response.data.get("unresolved_subjects"):
                    actual_stage = "catalog_resolution"
                elif response.data and response.data.get("resolved_subject_codes") and len(
                    response.data["resolved_subject_codes"]
                ) not in range(2, 5):
                    actual_stage = "subject_count_validation"
                elif "nhom g" in item["question"].casefold():
                    actual_stage = "intent_or_group_disambiguation"
                elif "to hop" in item["question"].casefold() or "tổ hợp" in item["question"].casefold():
                    actual_stage = "intent_or_handler_clarification"
                else:
                    actual_stage = "subject_catalog_match"
                self.assertEqual(item["expected_stage"], actual_stage)
                if post.called:
                    payload = post.call_args.args[1]
                    self.assertLessEqual(len(payload["subjectCodes"]), 4)
                else:
                    post.assert_not_called()
                if item["kind"] == "multiple_variants" and actual_status == "success":
                    self.assertEqual(2, len(response.data["evidence"]["labels"]))

    def test_admission_diagnostic_cases_validate_filters_and_actual_router_stages(self):
        fixtures = [item for item in self.fixtures if item.get("intent") == "pathway_lookup"]
        self.assertEqual(5, len(fixtures))
        pure_parser = IntentParser(api_key="fixture-key", model="fixture-model")
        for item in fixtures:
            with self.subTest(case=item["case_id"]):
                raw = json.dumps({
                    "intent": item["intent"], "extracted_filters": item["filters"],
                }, ensure_ascii=False)
                normalized = pure_parser._clean_and_parse_json(raw, [item["question"]])
                for key, value in item["filters"].items():
                    self.assertEqual(value, normalized["extracted_filters"][key])
                expected_ready = item["expected_stage"] != "readiness_guard"
                self.assertEqual(expected_ready, pure_parser.is_ready_for_db(normalized))

                parser_mock = Mock()
                parser_mock.parse_intent.return_value = normalized
                parser_mock.is_ready_for_db.side_effect = pure_parser.is_ready_for_db
                retriever_mock = Mock()
                generator_mock = Mock()
                generator_mock.generate_answer.return_value = "Có dữ liệu tuyển sinh phù hợp trong nguồn đã truy xuất."
                if item["expected_stage"] == "graph_retrieval_error":
                    retriever_mock.retrieve_context.side_effect = RetrievalUnavailableError("offline fixture")
                elif item["expected_stage"] == "graph_retrieval_empty":
                    retriever_mock.retrieve_context.return_value = []
                else:
                    retriever_mock.retrieve_context.return_value = [{"id": "offer-1"}]
                    retriever_mock.format_for_llm.return_value = "Verified admission evidence."

                request = ChatRequest(user_message=item["question"])
                actual_stage = "readiness_guard"
                with patch("rag_api.main_api.parser", parser_mock), \
                     patch("rag_api.main_api.retriever", retriever_mock), \
                     patch("rag_api.main_api.generator", generator_mock):
                    try:
                        response = chat_endpoint(request)
                        actual_status = response.status
                        if retriever_mock.retrieve_context.called:
                            if not retriever_mock.retrieve_context.return_value:
                                actual_stage = "graph_retrieval_empty"
                            elif generator_mock.generate_answer.called:
                                actual_stage = "graph_retrieval_generation"
                    except HTTPException as exc:
                        actual_status = "service_error" if exc.status_code == 503 else str(exc.status_code)
                        actual_stage = "graph_retrieval_error"

                self.assertEqual(item["expected_status"], actual_status)
                self.assertEqual(item["expected_stage"], actual_stage)
                if expected_ready:
                    retriever_mock.retrieve_context.assert_called_once()
                    self.assertEqual(_normalize_query_filters(normalized),
                                     retriever_mock.retrieve_context.call_args.kwargs["filters"])
                else:
                    retriever_mock.retrieve_context.assert_not_called()
                if item["expected_stage"] == "graph_retrieval_generation":
                    generator_mock.generate_answer.assert_called_once()
                else:
                    generator_mock.generate_answer.assert_not_called()

    @patch("rag_api.main_api._lookup_combination")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_router_passes_the_original_user_question_to_combination_resolver(
        self, parser, _retriever, _generator, lookup
    ):
        parser.parse_intent.return_value = {
            "intent": "combination_lookup", "extracted_filters": {"combination_code": None}
        }
        parser.is_ready_for_db.return_value = False
        lookup.return_value = Mock()
        request = ChatRequest(user_message="Tổ hợp Toán, Vật lí và Tiếng Anh gồm môn nào?")

        from rag_api.main_api import chat_endpoint
        chat_endpoint(request)

        lookup.assert_called_once()
        self.assertEqual(request.user_message, lookup.call_args.kwargs["question"])

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_follow_up_code_is_parsed_with_prior_conversation_context(
        self, parser, _retriever, _generator, backend_get
    ):
        question = "Mã đó gồm những môn nào?"
        history = [
            {"role": "user", "content": "Tra cứu tổ hợp A01 giúp em."},
            {"role": "assistant", "content": "Em muốn biết phần nào về A01?"},
        ]
        parser.parse_intent.return_value = {
            "intent": "combination_lookup", "extracted_filters": {"combination_code": "A01"}
        }
        parser.is_ready_for_db.return_value = False
        backend_get.return_value = {
            "requestedCode": "A01",
            "labels": [{"code": "A01", "subjectDetails": "Toán, Vật lí, Tiếng Anh"}],
        }

        response = chat_endpoint(ChatRequest(user_message=question, context=history))

        parser.parse_intent.assert_called_once()
        parser_call = parser.parse_intent.call_args.kwargs
        self.assertEqual(question, parser_call["user_message"])
        self.assertEqual(history, parser_call["context"])
        self.assertTrue(uuid.UUID(parser_call["request_id"]))
        backend_get.assert_called_once_with("/api/v1/admissions/combinations/A01")
        self.assertEqual("success", response.status)

    @patch("rag_api.main_api._backend_get")
    def test_parser_inferred_code_without_user_evidence_is_ignored(self, backend_get):
        backend_get.return_value = _catalog()
        response = _lookup_combination(
            {"extracted_filters": {"combination_code": "A01"}},
            "diag-request",
            question="Ngành học nào có lộ trình tốt?",
        )

        self.assertEqual("incomplete", response.status)
        backend_get.assert_called_once_with("/api/v1/subjects/classifications")

    @patch("rag_api.main_api._backend_get")
    def test_assistant_only_code_is_not_accepted_as_user_provenance(self, backend_get):
        backend_get.return_value = _catalog()
        response = _lookup_combination(
            {"extracted_filters": {"combination_code": "A01"}},
            "diag-request",
            question="Mã đó gồm những môn nào?",
            context=[{"role": "assistant", "content": "Mã tổ hợp A01 gồm Toán, Vật lí, Tiếng Anh."}],
        )

        self.assertEqual("incomplete", response.status)
        backend_get.assert_called_once_with("/api/v1/subjects/classifications")

    @patch("rag_api.main_api._backend_get")
    def test_current_user_code_overrides_stale_parser_code(self, backend_get):
        backend_get.return_value = {
            "requestedCode": "A01",
            "labels": [{"code": "A01", "subjectDetails": "Toán, Vật lí, Tiếng Anh"}],
        }
        response = _lookup_combination(
            {"extracted_filters": {"combination_code": "K01"}},
            "diag-request",
            question="Tra cứu tổ hợp A01 giúp em.",
        )

        self.assertEqual("success", response.status)
        backend_get.assert_called_once_with("/api/v1/admissions/combinations/A01")

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_subject_set_uses_additive_exact_resolver_and_preserves_catalog_provenance(
        self, backend_get, backend_post
    ):
        backend_get.return_value = _catalog()
        backend_post.return_value = {
            "matches": [{
                "combinationId": "MATH_PHYSICS_ENGLISH",
                "combinationDisplay": "Toan - Vat li - Tieng Anh",
                "componentCount": 3,
                "components": [
                    {"type": "SUBJECT", "code": "MATH", "name": "Toan", "subjectCode": "MATH"},
                    {"type": "SUBJECT", "code": "PHYSICS", "name": "Vat li", "subjectCode": "PHYSICS"},
                    {"type": "SUBJECT", "code": "ENGLISH", "name": "Tieng Anh", "subjectCode": "ENGLISH"},
                ],
                "labels": [{
                    "code": "A01", "subjectDetails": "Toan, Vat li, Tieng Anh",
                    "sourceUrl": "https://catalog.test/combinations",
                }],
            }],
            "resultScope": "exact_subject_set",
        }

        response = _lookup_combination(
            {"intent": "combination_lookup", "extracted_filters": {}},
            "resolver-request", question="To hop Toan, Vat li va Tieng Anh gom mon nao?",
        )

        self.assertEqual("success", response.status)
        backend_post.assert_called_once_with(
            "/api/v1/admissions/combinations/resolve-by-subjects",
            {"subjectCodes": ["MATH", "PHYSICS", "ENGLISH"]},
        )
        self.assertEqual("backend_admission_combination_resolver", response.data["evidence_source"])
        self.assertEqual("exact_subject_set", response.data["result_scope"])
        self.assertEqual(
            "https://catalog.test/combinations",
            response.data["evidence"]["matches"][0]["labels"][0]["sourceUrl"],
        )

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_canonical_combination_code_uses_exact_catalog_subject_resolver(self, backend_get, backend_post):
        backend_get.return_value = {
            "mandatorySubjects": [{"code": "MATH", "name": "Toán"}],
            "electiveSubjects": [
                {"code": "GEOGRAPHY", "name": "Địa lí"},
                {"code": "LITERATURE", "name": "Ngữ văn"},
            ],
        }
        backend_post.return_value = {
            "matches": [{
                "combinationId": "GEOGRAPHY_LITERATURE_MATH",
                "combinationDisplay": "Địa lí - Ngữ văn - Toán",
                "componentCount": 3,
                "components": [
                    {"type": "SUBJECT", "code": code, "name": code, "subjectCode": code}
                    for code in ("GEOGRAPHY", "LITERATURE", "MATH")
                ],
                "labels": [],
            }],
            "resultScope": "exact_subject_set",
        }

        response = _lookup_combination(
            {"intent": "combination_lookup", "extracted_filters": {}},
            "canonical-code", question="Tổ hợp xét tuyển geography_literature_math gồm những môn nào?",
        )

        self.assertEqual("success", response.status)
        self.assertEqual(["GEOGRAPHY", "LITERATURE", "MATH"], response.data["resolved_subject_codes"])
        backend_post.assert_called_once_with(
            "/api/v1/admissions/combinations/resolve-by-subjects",
            {"subjectCodes": ["GEOGRAPHY", "LITERATURE", "MATH"]},
        )

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_exact_resolver_rejects_malformed_success_bodies(self, backend_get, backend_post):
        backend_get.return_value = _catalog()
        valid_match = {
            "combinationId": "A01", "combinationDisplay": "Toan, Vat li, Tieng Anh",
            "componentCount": 3,
            "components": [
                {"type": "SUBJECT", "code": code, "name": code, "subjectCode": code}
                for code in ("MATH", "PHYSICS", "ENGLISH")
            ],
            "labels": [{"code": "A01", "subjectDetails": "Toan, Vat li, Tieng Anh"}],
        }
        malformed = (
            None,
            {},
            {"matches": None, "resultScope": "exact_subject_set"},
            {"matches": "invalid", "resultScope": "exact_subject_set"},
            {"matches": [{}], "resultScope": "exact_subject_set"},
            {"matches": [{"combinationId": "A01", "labels": None}], "resultScope": "exact_subject_set"},
            {"matches": [{**valid_match, "componentCount": "3"}], "resultScope": "exact_subject_set"},
            {"matches": [{**valid_match, "components": [None]}], "resultScope": "exact_subject_set"},
            {"matches": [{**valid_match, "labels": [{}]}], "resultScope": "exact_subject_set"},
        )
        for body in malformed:
            with self.subTest(body=body):
                backend_post.return_value = body
                with self.assertRaises(HTTPException) as raised:
                    _lookup_combination(
                        {"intent": "combination_lookup", "extracted_filters": {}},
                        "bad-upstream", question="To hop Toan, Vat li va Tieng Anh gom mon nao?",
                    )
                self.assertEqual(502, raised.exception.status_code)

        backend_post.side_effect = ValueError("invalid JSON body")
        with self.assertRaises(HTTPException) as raised:
            _lookup_combination(
                {"intent": "combination_lookup", "extracted_filters": {}},
                "bad-json", question="To hop Toan, Vat li va Tieng Anh gom mon nao?",
            )
        self.assertEqual(502, raised.exception.status_code)

    @patch("rag_api.main_api._backend_post", return_value={"matches": [], "resultScope": "exact_subject_set"})
    @patch("rag_api.main_api._backend_get", return_value=_catalog())
    def test_exact_resolver_valid_empty_list_remains_no_data(self, _backend_get, _backend_post):
        response = _lookup_combination(
            {"intent": "combination_lookup", "extracted_filters": {}},
            "empty-upstream", question="To hop Toan, Vat li va Tieng Anh gom mon nao?",
        )
        self.assertEqual("no_data", response.status)

    @patch("rag_api.main_api._backend_post", return_value={"matches": None})
    @patch("rag_api.main_api._backend_get", return_value=_catalog())
    @patch("rag_api.main_api.parser")
    def test_chat_http_returns_502_without_upstream_body(self, parser_mock, _backend_get, _backend_post):
        parser_mock.parse_intent.return_value = {
            "intent": "combination_lookup", "extracted_filters": {},
        }
        with patch("rag_api.main_api.retriever"), patch("rag_api.main_api.generator"):
            response = TestClient(app).post(
                "/api/chat", json={"user_message": "To hop Toan, Vat li va Tieng Anh gom mon nao?"},
            )
        self.assertEqual(502, response.status_code)
        self.assertNotIn("matches", response.text)

    @patch("rag_api.main_api._backend_get")
    def test_explicit_correction_does_not_query_negated_combination_code(self, backend_get):
        backend_get.return_value = {
            "requestedCode": "K01",
            "labels": [{"code": "K01", "subjectDetails": "Toan, Van, Tin", "canonicalCombinationId": "K01"}],
        }

        response = _lookup_combination(
            {"extracted_filters": {"combination_code": "A01"}},
            "correction-request", question="Khong phai A01, em muon K01.",
        )

        self.assertEqual("success", response.status)
        backend_get.assert_called_once_with("/api/v1/admissions/combinations/K01")


if __name__ == "__main__":
    unittest.main()

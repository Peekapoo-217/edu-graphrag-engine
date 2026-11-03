import json
import hashlib
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from rag_api.llm.intent_parser import IntentParser
from rag_api.llm.llm_client import RAGGenerator
from rag_api.llm.provider_errors import LLMServiceUnavailable
from rag_api.main_api import (
    ChatRequest,
    _advise_without_school,
    _answer_knowledge_question,
    _chat_result,
    _apply_current_turn_precedence,
    _apply_deterministic_route_contract,
    _compare_subject_groups,
    _explicit_canonical_subject_codes,
    _filter_requested_groups,
    _graph_path_validation_payload,
    _history_group_codes,
    _normalize_query_filters,
    _resolve_combination_for_pathway,
    _resolve_comparison_scopes_from_catalog,
    _resolve_explicit_school_filter,
    _resolve_explicit_graph_group_scope,
    _lookup_school_groups,
    _prior_turns,
    _resolve_school,
    _resolve_subject,
    _stage_error_headers,
    _university_search_filter,
    _retrieve_pathway_comparison_scopes,
    chat_endpoint,
)
from rag_api.retrieval.errors import RetrievalUnavailableError
from rag_api.retrieval.graph_retriever import Neo4jRetriever


class IntentParserContractTests(unittest.TestCase):
    def setUp(self):
        self.parser = IntentParser(api_key="unit-test-key", model="unit-test-model")

    @patch("rag_api.llm.intent_parser.settings")
    def test_parser_default_timeout_uses_runtime_configuration(self, settings_mock):
        settings_mock.LLM_TIMEOUT = 45.0
        settings_mock.INTENT_PARSER_TIMEOUT = 45.0
        parser = IntentParser(api_key="unit-test-key", model="unit-test-model")
        self.assertEqual(45.0, parser.timeout)

    @patch("rag_api.llm.intent_parser.settings")
    def test_parser_uses_dedicated_timeout_when_configured(self, settings_mock):
        settings_mock.LLM_TIMEOUT = 30.0
        settings_mock.INTENT_PARSER_TIMEOUT = 40.0
        parser = IntentParser(api_key="unit-test-key", model="unit-test-model")
        self.assertEqual(40.0, parser.timeout)

    @patch("rag_api.utils.diagnostic_stage_trace.append_stage_event")
    @patch("rag_api.llm.intent_parser.requests.post")
    def test_intent_trace_records_only_sanitized_input_sizes_and_actual_timeout_code(
        self, post_mock, event_mock
    ):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace

        trace = DiagnosticStageTrace(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
        post_mock.side_effect = requests.ReadTimeout("private request and prompt data")
        context = [{"role": "user", "content": "Câu hỏi riêng tư"},
                   {"role": "assistant", "content": "Câu trả lời riêng tư"}]

        with patch("rag_api.llm.intent_parser.DiagnosticStageTrace") as trace_class:
            trace_class.current.return_value = trace
            with patch.object(trace, "enabled", True):
                with self.assertRaises(LLMServiceUnavailable) as raised:
                    self.parser.parse_intent("Câu hỏi tiếp theo", context=context)

        events = [call.args[0] for call in event_mock.call_args_list]
        intent_events = [event for event in events if event.get("stage") == "intent"]
        self.assertEqual("read_timeout", raised.exception.failure_code)
        self.assertTrue(any(event.get("event") == "input" for event in intent_events))
        self.assertTrue(any(event.get("event") == "error" and
                            event.get("transport_error_code") == "read_timeout"
                            for event in intent_events))
        serialized = json.dumps(intent_events, ensure_ascii=False)
        self.assertNotIn("Câu hỏi riêng tư", serialized)
        self.assertNotIn("Câu trả lời riêng tư", serialized)
        input_event = next(event for event in intent_events if event.get("event") == "input")
        self.assertEqual(4, input_event["message_count"])
        self.assertEqual(2, input_event["history_turn_count"])
        self.assertGreater(input_event["prompt_utf8_bytes"], 0)
        self.assertGreater(input_event["request_body_utf8_bytes"], 0)

    def test_rejects_subject_signal_that_only_appears_in_assistant_history(self):
        raw = json.dumps({
            "intent": "subject_group_comparison",
            "extracted_filters": {},
            "preferred_subjects": [{"subject": "Toán", "evidence": "Em thích Toán"}],
        })

        result = self.parser._clean_and_parse_json(raw, ["Em muốn tìm nhóm môn."])

        self.assertEqual([], result["preferred_subjects"])


    def test_rejects_a_filter_not_mentioned_by_the_user(self):
        raw = json.dumps({
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
        })

        result = self.parser._clean_and_parse_json(raw, ["Trường nào có nhóm môn?"])

        self.assertIsNone(result["extracted_filters"]["high_school"])

    def test_rejects_subject_claim_that_does_not_match_its_user_evidence(self):
        raw = json.dumps({
            "intent": "subject_group_comparison",
            "extracted_filters": {},
            "preferred_subjects": [{"subject": "Hóa học", "evidence": "Em thích Toán"}],
        })

        result = self.parser._clean_and_parse_json(raw, ["Em thích Toán"])

        self.assertEqual([], result["preferred_subjects"])

    def test_does_not_assume_a_scale_for_a_grade(self):
        raw = json.dumps({
            "intent": "subject_group_advice",
            "extracted_filters": {},
            "grades_by_subject": [{
                "subject": "Toán", "value": 9, "scale": 10,
                "assessment_type": "kiểm tra", "evidence": "Toán 9 điểm",
            }],
        })

        result = self.parser._clean_and_parse_json(raw, ["Toán 9 điểm"])

        self.assertEqual([], result["grades_by_subject"])
        self.assertEqual([{"subject": "Toán", "evidence": "Toán 9 điểm"}], result["grades_needing_scale"])

    def test_accepts_grade_when_user_explicitly_states_scale(self):
        raw = json.dumps({
            "intent": "subject_group_advice",
            "extracted_filters": {},
            "grades_by_subject": [{
                "subject": "Toán", "value": 9, "scale": 10,
                "assessment_type": "kiểm tra", "evidence": "Toán 9/10",
            }],
        })

        result = self.parser._clean_and_parse_json(raw, ["Toán 9/10"])

        self.assertEqual(9, result["grades_by_subject"][0]["value"])
        self.assertEqual(10, result["grades_by_subject"][0]["scale"])
        self.assertEqual([], result["grades_needing_scale"])

    def test_rejects_grade_value_that_does_not_match_the_user_evidence(self):
        raw = json.dumps({
            "intent": "subject_group_advice",
            "extracted_filters": {},
            "grades_by_subject": [{
                "subject": "Toán", "value": 8, "scale": 10,
                "assessment_type": None, "evidence": "Toán 9/10",
            }],
        })

        result = self.parser._clean_and_parse_json(raw, ["Toán 9/10"])

        self.assertEqual([], result["grades_by_subject"])
        self.assertEqual([], result["grades_needing_scale"])

    def test_rejects_confidence_value_that_does_not_match_user_evidence(self):
        raw = json.dumps({
            "intent": "subject_group_advice",
            "extracted_filters": {},
            "confidence_by_subject": [{
                "subject": "Toán", "value": 5, "evidence": "Em tự tin Toán 3/5",
            }],
        })

        result = self.parser._clean_and_parse_json(raw, ["Em tự tin Toán 3/5"])

        self.assertEqual([], result["confidence_by_subject"])

    def test_requested_group_codes_are_kept_only_when_verbatim_user_evidence_exists(self):
        raw = json.dumps({
            "intent": "school_group_lookup",
            "extracted_filters": {},
            "requested_group_codes": [
                {"code": "G01", "evidence": "Cho xem nhóm G01"},
                {"code": "G02", "evidence": "Cho xem nhóm G02"},
            ],
        })

        result = self.parser._clean_and_parse_json(raw, ["Cho xem nhóm G01"])

        self.assertEqual([{"code": "G01", "evidence": "Cho xem nhóm G01"}], result["requested_group_codes"])

    def test_school_group_pairs_require_both_names_and_codes_in_current_user_evidence(self):
        question = "So sánh nhóm G04 của THPT Trần Cao Vân với nhóm G03 của THPT Trưng Vương."
        raw = json.dumps({
            "intent": "subject_group_comparison", "extracted_filters": {},
            "high_schools": ["THPT Trần Cao Vân", "THPT Trưng Vương"],
            "requested_school_group_pairs": [
                {"school": "THPT Trần Cao Vân", "code": "G04", "evidence": "G04 của THPT Trần Cao Vân"},
                {"school": "THPT Trưng Vương", "code": "G03", "evidence": "G03 của THPT Trưng Vương"},
                {"school": "THPT Trần Cao Vân", "code": "G03", "evidence": "G03 của THPT Trần Cao Vân"},
            ],
        }, ensure_ascii=False)

        result = self.parser._clean_and_parse_json(raw, [question])

        self.assertEqual([
            {"school": "THPT Trần Cao Vân", "code": "G04", "evidence": "G04 của THPT Trần Cao Vân"},
            {"school": "THPT Trưng Vương", "code": "G03", "evidence": "G03 của THPT Trưng Vương"},
        ], result["requested_school_group_pairs"])

    def test_pathway_pair_contract_preserves_independent_group_program_year_scope(self):
        question = ("O THPT Tran Cao Van nam 2026, Nhom 4 voi to hop Hoa hoc-Toan-Vat li "
                    "cua nganh Toan ung dung; Nhom 1 voi to hop Dia li-Toan-Vat li "
                    "cua nganh Quan ly tai nguyen va moi truong tai DH Quy Nhon.")
        raw = json.dumps({
            "intent": "pathway_lookup", "extracted_filters": {"university": "DH Quy Nhon"},
            "requested_pathway_pairs": [
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G04", "program_name": "Toan ung dung",
                 "evidence": "Nhom 4 voi to hop Hoa hoc-Toan-Vat li cua nganh Toan ung dung"},
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G01", "program_name": "Quan ly tai nguyen va moi truong",
                 "evidence": "Nhom 1 voi to hop Dia li-Toan-Vat li cua nganh Quan ly tai nguyen va moi truong"},
            ],
        }, ensure_ascii=False)
        result = self.parser._clean_and_parse_json(raw, [question])
        self.assertEqual(2, len(result["requested_pathway_pairs"]))
        self.assertEqual([("G04", "Toan ung dung"), ("G01", "Quan ly tai nguyen va moi truong")],
                         [(pair["subject_group_code"], pair["program_name"])
                          for pair in result["requested_pathway_pairs"]])
        self.assertEqual(["2026", "2026"],
                         [pair["academic_year"] for pair in result["requested_pathway_pairs"]])

    def test_pathway_pair_with_evidence_from_different_pair_is_rejected(self):
        question = "Nhom 4 voi to hop Hoa-Toan-Ly cua nganh Toan ung dung; Nhom 1 voi nganh Moi truong."
        raw = json.dumps({"intent": "pathway_lookup", "extracted_filters": {},
                          "requested_pathway_pairs": [{
                              "high_school": "THPT Tran Cao Van", "academic_year": "2026",
                              "subject_group_code": "G04", "program_name": "Moi truong",
                              "evidence": "Nhom 4 voi to hop Hoa-Toan-Ly cua nganh Toan ung dung",
                          }]}, ensure_ascii=False)
        result = self.parser._clean_and_parse_json(raw, [question])
        self.assertEqual([], result["requested_pathway_pairs"])

    @patch("rag_api.llm.intent_parser.requests.post")
    @patch("rag_api.llm.intent_parser.settings")
    def test_parser_uses_configured_model_unchanged(self, settings_mock, post_mock):
        settings_mock.LLM_API_BASE = "http://llm.test/v1"
        settings_mock.LLM_API_KEY = "environment-key"
        settings_mock.LLM_MODEL = "ag/claude-sonnet-4-6(high)"
        settings_mock.LLM_TIMEOUT = 8
        post_mock.return_value = _llm_response({
            "intent": "greeting",
            "extracted_filters": {},
            "preferred_subjects": [],
            "disliked_subjects": [],
            "self_reported_strengths": [],
            "confidence_by_subject": [],
            "grades_by_subject": [],
        })

        IntentParser().parse_intent("Chào bạn")

        self.assertEqual("ag/claude-sonnet-4-6(high)", post_mock.call_args.kwargs["json"]["model"])

    def test_parser_keeps_only_user_verbatim_school_labels_for_comparison(self):
        parsed = self.parser._clean_and_parse_json(json.dumps({
            "intent": "subject_group_comparison",
            "high_schools": ["THPT Trưng Vương", "THPT Trần Cao Vân", "Invented School"],
            "extracted_filters": {},
        }, ensure_ascii=False), ["So sánh THPT Trưng Vương và THPT Trần Cao Vân"])

        self.assertEqual(["THPT Trưng Vương", "THPT Trần Cao Vân"], parsed["high_schools"])


class DiagnosticRunIsolationTests(unittest.TestCase):
    def test_evaluation_profile_rejects_missing_or_untrusted_run_auth_before_chat(self):
        active_run_id = "12345678-1234-4234-8234-123456789abc"
        with tempfile.TemporaryDirectory() as directory:
            trace_path = str(Path(directory) / "trace.jsonl")
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
                "RAG_DIAGNOSTIC_RUN_ID": active_run_id,
                "RAG_DIAGNOSTIC_PROXY_TOKEN": "synthetic-run-token",
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": trace_path,
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{active_run_id}.jsonl"),
            }), patch("rag_api.main_api._chat_endpoint_impl") as chat_impl:
                cases = [
                    (active_run_id, None),
                    ("abcdefab-cdef-4abc-8def-abcdefabcdef", "synthetic-run-token"),
                    (active_run_id, "wrong-token"),
                ]
                for run_id, token in cases:
                    with self.subTest(run_id=run_id, token_present=bool(token)):
                        with self.assertRaises(HTTPException) as raised:
                            chat_endpoint(ChatRequest(user_message="question"),
                                          diagnostic_run_id=run_id,
                                          diagnostic_run_token=token)
                        self.assertEqual(403, raised.exception.status_code)
                chat_impl.assert_not_called()

    def test_evaluation_profile_accepts_only_configured_run_with_internal_token(self):
        active_run_id = "12345678-1234-4234-8234-123456789abc"
        expected = ChatRequest(user_message="question")
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
                "RAG_DIAGNOSTIC_RUN_ID": active_run_id,
                "RAG_DIAGNOSTIC_PROXY_TOKEN": "synthetic-run-token",
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "trace.jsonl"),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{active_run_id}.jsonl"),
            }), patch("rag_api.main_api._chat_endpoint_impl", return_value="accepted") as chat_impl:
                result = chat_endpoint(expected, diagnostic_run_id=active_run_id,
                                       diagnostic_run_token="synthetic-run-token")
        self.assertEqual("accepted", result)
        self.assertEqual(1, chat_impl.call_count)

    def test_evaluation_profile_rejects_route_trace_path_not_bound_to_run(self):
        active_run_id = "12345678-1234-4234-8234-123456789abc"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
            "RAG_DIAGNOSTIC_RUN_ID": active_run_id,
            "RAG_DIAGNOSTIC_PROXY_TOKEN": "synthetic-run-token",
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "trace.jsonl"),
            "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / "route_events_wrong-run.jsonl"),
        }), patch("rag_api.main_api._chat_endpoint_impl") as chat_impl:
            with self.assertRaises(HTTPException) as raised:
                chat_endpoint(ChatRequest(user_message="question"), diagnostic_run_id=active_run_id,
                              diagnostic_run_token="synthetic-run-token")
        self.assertEqual(403, raised.exception.status_code)
        chat_impl.assert_not_called()


class RequestedGroupResolutionTests(unittest.TestCase):
    def test_exact_code_has_priority(self):
        groups = [{"groupCode": "G03"}, {"groupCode": "03"}]
        resolved, missing, details = _filter_requested_groups(
            groups, [{"code": "G03"}], school_code="TVQN", academic_year="2026"
        )
        self.assertEqual(["G03"], [row["groupCode"] for row in resolved])
        self.assertEqual([], missing)
        self.assertEqual({"raw_code": "G03", "canonical_code": "G03", "status": "exact",
                          "source": "school_year_group_catalog",
                          "scope": {"school_code": "TVQN", "academic_year": "2026"}}, details[0])

    def test_unique_catalog_alias_resolves_g03_to_03(self):
        resolved, missing, details = _filter_requested_groups(
            [{"groupCode": "03", "groupName": "Nhóm 03"}], [{"code": "G03"}],
            school_code="TVQN", academic_year="2026",
        )
        self.assertEqual("03", resolved[0]["groupCode"])
        self.assertEqual([], missing)
        self.assertEqual("resolved_alias", details[0]["status"])
        self.assertEqual("03", details[0]["canonical_code"])

    def test_numeric_alias_is_resolved_only_when_unique_in_school_year_catalog(self):
        resolved, missing, details = _filter_requested_groups(
            [{"groupCode": "03", "schoolCode": "TVQN", "academicYear": "2026"}],
            [{"code": "3"}], school_code="TVQN", academic_year="2026",
        )
        self.assertEqual(["03"], [row["groupCode"] for row in resolved])
        self.assertEqual([], missing)
        self.assertEqual("resolved_alias", details[0]["status"])

    def test_code_collision_is_ambiguous_and_does_not_choose_a_candidate(self):
        resolved, missing, details = _filter_requested_groups(
            [{"groupCode": "G03"}, {"groupCode": "03"}], [{"code": "3"}],
            school_code="TVQN", academic_year="2026",
        )
        self.assertEqual([], resolved)
        self.assertEqual([], missing)
        self.assertEqual("ambiguous", details[0]["status"])

    def test_mixed_found_and_missing_group_preserves_partial_result(self):
        resolved, missing, details = _filter_requested_groups(
            [{"groupCode": "01"}], [{"code": "01"}, {"code": "09"}],
            school_code="TVQN", academic_year="2026",
        )
        self.assertEqual(["01"], [row["groupCode"] for row in resolved])
        self.assertEqual(["09"], missing)
        self.assertEqual(["exact", "unknown"], [item["status"] for item in details])

    @patch("rag_api.main_api._backend_get")
    def test_school_group_handler_returns_alias_resolution_and_canonical_code(self, backend_get):
        backend_get.side_effect = [
            [{"code": "TVQN", "name": "THPT Trưng Vương"}],
            ["2026"],
            [{"groupCode": "03", "groupName": "Nhóm 03", "subjects": [{"name": "Toán"}]}],
        ]
        response = _lookup_school_groups({
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT Trưng Vương"},
            "requested_group_codes": [{"code": "G03", "evidence": "nhóm G03"}],
        }, "alias-handler", current_message="THPT Trưng Vương có nhóm G03 không?")

        self.assertEqual("success", response.status)
        self.assertEqual("03", response.data["evidence"][0]["groupCode"])
        self.assertEqual("resolved_alias", response.data["group_code_resolutions"][0]["status"])

    def test_alias_candidates_are_limited_to_supplied_school_and_year(self):
        groups = [
            {"groupCode": "03", "schoolCode": "TVQN", "academicYear": "2026"},
            {"groupCode": "G03", "schoolCode": "OTHER", "academicYear": "2026"},
            {"groupCode": "3", "schoolCode": "TVQN", "academicYear": "2025"},
        ]
        resolved, missing, details = _filter_requested_groups(
            groups, [{"code": "G03"}], school_code="TVQN", academic_year="2026",
        )
        self.assertEqual(["03"], [row["groupCode"] for row in resolved])
        self.assertEqual([], missing)
        self.assertEqual("resolved_alias", details[0]["status"])


class ChatRoutingHelperTests(unittest.TestCase):
    LIVE_OVERLAP_QUESTION = (
        "Em chuẩn bị vào lớp 10 và dự định đăng ký Nhóm môn 1 tại Trường THPT Trưng Vương "
        "(Quy Nhơn). Cho em hỏi trong nhóm môn này có môn nào thuộc tổ hợp xét tuyển "
        "(Toán, Vật lí, Công nghệ) vào ngành Kỹ thuật điện của Trường Đại học Quy Nhơn "
        "năm 2026 không?"
    )
    LIVE_FULL_PATH_QUESTION = (
        "Nếu em chọn học Nhóm 4 (năm học 2026) tại trường THPT Trần Cao Vân thì nhóm này "
        "có đáp ứng được trọn vẹn tổ hợp xét tuyển gồm Giáo dục kinh tế và pháp luật, "
        "Địa lí, Toán của ngành Quản lý tài nguyên và môi trường ở Trường Đại học Quy Nhơn "
        "không? Môn nào trong nhóm trùng khớp với tổ hợp này?"
    )
    LIVE_FOLLOWUP_QUESTION = (
        "Vậy Nhóm môn 1 đó có đáp ứng trọn vẹn tổ hợp xét tuyển (Toán, Vật lí, Công nghệ) "
        "của ngành Kỹ thuật điện tại Đại học Quy Nhơn không?"
    )

    def test_exact_live_overlap_question_selects_subject_overlap_scope(self):
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "Trường THPT Trưng Vương",
                "desired_program": "Kỹ thuật điện",
                "university": "Trường Đại học Quy Nhơn",
            },
            "high_schools": ["Trường THPT Trưng Vương"],
            "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}],
            "academic_year": "2026",
        }, self.LIVE_OVERLAP_QUESTION)

        filters = _normalize_query_filters(parsed, self.LIVE_OVERLAP_QUESTION)
        self.assertEqual("pathway_lookup", parsed["intent"])
        self.assertEqual("subject_overlap", parsed["answer_scope"])
        self.assertEqual("subject_overlap", filters["answer_scope"])

    def test_exact_live_full_path_question_is_not_downgraded_to_group_advice(self):
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "Trường THPT Trần Cao Vân",
                "desired_program": "Quản lý tài nguyên và môi trường",
                "university": "Trường Đại học Quy Nhơn",
            },
            "high_schools": ["Trường THPT Trần Cao Vân"],
            "academic_year": "2026",
        }, self.LIVE_FULL_PATH_QUESTION)

        filters = _normalize_query_filters(parsed, self.LIVE_FULL_PATH_QUESTION)
        self.assertEqual("pathway_lookup", parsed["intent"])
        self.assertEqual("full_path", parsed["answer_scope"])
        self.assertEqual("full_path", filters["answer_scope"])

    def test_exact_live_followup_uses_current_full_path_wording_not_prior_overlap_scope(self):
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "answer_scope": "subject_overlap",
            "answer_scope_source": "model_or_prior_context",
            "extracted_filters": {
                "high_school": "Trường THPT Trưng Vương",
                "desired_program": "Kỹ thuật điện",
                "university": "Đại học Quy Nhơn",
            },
            "high_schools": ["Trường THPT Trưng Vương"],
            "academic_year": "2026",
        }, self.LIVE_FOLLOWUP_QUESTION)

        filters = _normalize_query_filters(parsed, self.LIVE_FOLLOWUP_QUESTION)
        self.assertEqual("pathway_lookup", parsed["intent"])
        self.assertEqual("full_path", parsed["answer_scope"])
        self.assertEqual("full_path", filters["answer_scope"])

    def test_common_vietnamese_overlap_wordings_select_overlap_only_with_a_combination(self):
        for wording in (
            "Nhóm G01 có môn nào thuộc tổ hợp xét tuyển A01 không?",
            "Nhóm G01 có môn nào trùng khớp với tổ hợp xét tuyển A01 không?",
        ):
            with self.subTest(wording=wording):
                parsed = _apply_deterministic_route_contract({
                    "intent": "pathway_lookup",
                    "extracted_filters": {"high_school": "TVQN", "desired_program": "Ngành A"},
                    "academic_year": "2026",
                }, wording)
                self.assertEqual("subject_overlap", parsed["answer_scope"])

    def test_mixed_exact_live_question_keeps_full_path_over_overlap_clause(self):
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "Trường THPT Trần Cao Vân"},
            "academic_year": "2026",
        }, self.LIVE_FULL_PATH_QUESTION)

        self.assertEqual("pathway_lookup", parsed["intent"])
        self.assertEqual("full_path", parsed["answer_scope"])

    def test_chat_result_emits_server_route_and_retrieval_observations_without_filter_values(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace

        run_id = "12345678-1234-4234-8234-123456789abc"
        correlation_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        request_id = "fedcba98-7654-4321-8765-abcdefabcdef"
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / f"route_events_{run_id}.jsonl"
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(route_path),
            }):
                trace = DiagnosticStageTrace(run_id, correlation_id, request_id)
                with trace:
                    _chat_result("success", "answer", request_id, "pathway_lookup", {
                        "evidence_source": "neo4j_graph", "retrieval_executed": True,
                        "generation_executed": True, "filters": {
                            "academic_year": "2026", "private_filter": "secret-value"},
                        "retrieved_count": 1,
                    })
            events = [json.loads(line) for line in route_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["route_decision", "retrieval_result"], [event["event"] for event in events])
        self.assertEqual("unknown", events[0]["execution_kind"])
        self.assertEqual("graph_retrieval_generation", events[1]["execution_kind"])
        self.assertEqual("server_decision", events[0]["route_origin"])
        self.assertEqual("not_reached", events[0]["decision_status"])
        self.assertEqual("server_observed", events[1]["route_origin"])
        self.assertEqual({"academic_year"}, set(events[1]["filter_keys"]))
        self.assertNotIn('"filters"', json.dumps(events))
        self.assertNotIn("secret-value", json.dumps(events))

    def test_nested_chat_results_emit_only_outer_request_final_route(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace

        run_id = "12345678-1234-4234-8234-123456789abc"
        correlation_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        request_id = "fedcba98-7654-4321-8765-abcdefabcdef"
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / f"route_events_{run_id}.jsonl"
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(route_path),
            }):
                with DiagnosticStageTrace(run_id, correlation_id, request_id):
                    _chat_result("success", "child", request_id, "subject_group_comparison", {
                        "evidence_source": "backend_subject_group_comparison_api",
                        "backend_retrieval_executed": True,
                        "graph_retrieval_executed": False,
                    })
                    _chat_result("success", "outer", request_id, "subject_group_comparison", {
                        "evidence_source": "backend_subject_group_comparison_api",
                        "backend_retrieval_executed": True,
                        "graph_retrieval_executed": False,
                    })
            events = [json.loads(line) for line in route_path.read_text(encoding="utf-8").splitlines()]
        finals = [event for event in events if event.get("final_route") is True]
        self.assertEqual(1, len(finals))
        self.assertEqual("backend_lookup", finals[0]["execution_kind"])

    def test_route_writer_failure_does_not_change_chat_result(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace

        run_id = "12345678-1234-4234-8234-123456789abc"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
            "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
        }):
            trace = DiagnosticStageTrace(run_id, str(uuid.uuid4()), str(uuid.uuid4()))
            with trace, patch("rag_api.main_api.append_route_decision", side_effect=OSError("disk")):
                response = _chat_result("success", "answer", trace.request_id, "pathway_lookup", {
                    "evidence_source": "neo4j_graph", "retrieval_executed": True,
                })
        self.assertEqual("answer", response.message)

    def test_knowledge_intent_is_corrected_for_relational_admission_question(self):
        parsed = {"intent": "knowledge", "extracted_filters": {
            "high_school": "THPT Trần Cao Vân", "desired_program": "Quản trị kinh doanh",
            "university": "Đại học Quy Nhơn",
        }}

        result = _apply_deterministic_route_contract(
            parsed,
            "Hãy nêu một ví dụ nhóm môn THPT Trần Cao Vân có môn này rồi chỉ ra tổ hợp ngành Quản trị kinh doanh Đại học Quy Nhơn chấp nhận năm 2026.",
        )

        self.assertEqual("pathway_lookup", result["intent"])
        self.assertEqual(parsed["extracted_filters"], result["extracted_filters"])

    def test_knowledge_intent_routes_canonical_combination_lookup(self):
        result = _apply_deterministic_route_contract(
            {"intent": "knowledge", "extracted_filters": {}},
            "Tổ hợp xét tuyển GEOGRAPHY_LITERATURE_MATH gồm những môn nào?",
        )

        self.assertEqual("combination_lookup", result["intent"])

    def test_history_reference_with_group_membership_question_routes_to_school_lookup(self):
        result = _apply_deterministic_route_contract(
            {"intent": "knowledge", "extracted_filters": {}},
            "Nhóm đó có môn Tin học không, và gồm những môn nào ạ?",
        )

        self.assertEqual("school_group_lookup", result["intent"])

    def test_explicit_two_school_comparison_overrides_school_lookup_intent(self):
        result = _apply_deterministic_route_contract(
            {
                "intent": "school_group_lookup",
                "high_schools": ["THPT Tr\u01b0ng V\u01b0\u01a1ng", "THPT Tr\u1ea7n Cao V\u00e2n"],
                "extracted_filters": {},
            },
            "Nh\u00f3m G02 c\u1ee7a THPT Tr\u01b0ng V\u01b0\u01a1ng v\u00e0 G02 c\u1ee7a THPT Tr\u1ea7n Cao V\u00e2n n\u0103m 2026 kh\u00e1c nhau th\u1ebf n\u00e0o? C\u00f3 m\u00f4n n\u00e0o chung?",
        )

        self.assertEqual("subject_group_comparison", result["intent"])

    def test_explicit_group_advice_overrides_school_lookup_intent(self):
        result = _apply_deterministic_route_contract(
            {
                "intent": "school_group_lookup",
                "extracted_filters": {"high_school": "THPT Tr\u1ea7n Cao V\u00e2n"},
            },
            "Em th\u00edch Sinh h\u1ecdc v\u00e0 mu\u1ed1n theo ng\u00e0nh n\u00f4ng nghi\u1ec7p. Nh\u00f3m G02 c\u1ee7a THPT Tr\u1ea7n Cao V\u00e2n g\u1ed3m m\u00f4n g\u00ec, c\u00f3 ph\u00f9 h\u1ee3p \u0111\u1ec3 em c\u00e2n nh\u1eafc ch\u1ecdn kh\u00f4ng?",
        )

        self.assertEqual("subject_group_advice", result["intent"])

    def test_school_group_lookup_with_advice_request_keeps_both_requested_tasks(self):
        result = _apply_deterministic_route_contract({
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "high_schools": ["THPT A"],
            "preferred_subjects": [{"subject": "Toán", "evidence": "em thích Toán"}],
        }, "THPT A có những nhóm môn nào, em thích Toán thì nên cân nhắc nhóm nào?")

        self.assertEqual("subject_group_advice", result["intent"])
        self.assertEqual(["school_group_lookup", "subject_group_advice"], result["requested_tasks"])

    def test_advice_question_asking_what_subjects_group_has_keeps_lookup_task(self):
        question = (
            "Em mu\u1ed1n h\u1ecdc \u1edf tr\u01b0\u1eddng THPT T\u00f4n \u0110\u1ee9c Th\u1eafng "
            "t\u1ec9nh Gia Lai, n\u1ebfu em ch\u1ecdn nh\u00f3m m\u00f4n 02 th\u00ec s\u1ebd h\u1ecdc "
            "nh\u1eefng m\u00f4n g\u00ec v\u00e0 ph\u00f9 h\u1ee3p v\u1edbi h\u01b0\u1edbng \u0111i "
            "n\u00e0o \u1ea1?"
        )
        result = _apply_deterministic_route_contract({
            "intent": "subject_group_advice",
            "extracted_filters": {
                "high_school": "THPT T\u00f4n \u0110\u1ee9c Th\u1eafng",
                "high_school_code": "T\u0110T",
            },
            "high_schools": ["THPT T\u00f4n \u0110\u1ee9c Th\u1eafng"],
        }, question)

        self.assertEqual("subject_group_advice", result["intent"])
        self.assertEqual(["school_group_lookup", "subject_group_advice"], result["requested_tasks"])

    def test_pathway_recovers_missing_explicit_school_from_unique_backend_catalog_match(self):
        from rag_api.main_api import _resolve_explicit_school_filter

        schools = [
            {"code": "TCV", "name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"},
            {"code": "TVQN", "name": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng"},
        ]
        parsed = {"intent": "pathway_lookup", "extracted_filters": {"desired_program": "Váº­t lÃ½ ká»¹ thuáº­t"}}
        question = "H\u00e3y n\u00eau m\u1ed9t v\u00ed d\u1ee5 c\u1ee7a THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n c\u00f3 Sinh h\u1ecdc."

        with patch("rag_api.main_api._backend_get", return_value=schools) as backend_get:
            result = _resolve_explicit_school_filter(parsed, question)

        self.assertEqual("TVQN", result["extracted_filters"]["high_school_code"])
        self.assertEqual([schools[1]["name"]], result["high_schools"])
        backend_get.assert_called_once_with("/api/v1/high-schools")

    def test_pathway_does_not_guess_when_school_catalog_match_is_ambiguous(self):
        from rag_api.main_api import _resolve_explicit_school_filter

        schools = [
            {"code": "TV1", "name": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng A"},
            {"code": "TV2", "name": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng B"},
        ]
        parsed = {"intent": "pathway_lookup", "extracted_filters": {}}

        with patch("rag_api.main_api._backend_get", return_value=schools):
            result = _resolve_explicit_school_filter(
                parsed, "H\u00e3y n\u00eau m\u1ed9t v\u00ed d\u1ee5 c\u1ee7a THPT Tr\u01b0ng V\u01b0\u01a1ng \u1edf Quy Nh\u01a1n."
            )

        self.assertNotIn("high_school", result["extracted_filters"])

    def test_canonical_combination_code_resolves_only_catalog_components(self):
        catalog = {"mandatorySubjects": [{"code": "MATH", "name": "Toán"}],
                   "electiveSubjects": [{"code": "GEOGRAPHY", "name": "Địa lí"},
                                        {"code": "LITERATURE", "name": "Ngữ văn"}]}

        self.assertEqual(["GEOGRAPHY", "LITERATURE", "MATH"],
                         _explicit_canonical_subject_codes(
                             "Tổ hợp GEOGRAPHY_LITERATURE_MATH gồm môn gì?", catalog))
        self.assertEqual([], _explicit_canonical_subject_codes(
            "Tổ hợp UNKNOWN_LITERATURE_MATH gồm môn gì?", catalog))

    def test_subject_scanner_ignores_subject_token_inside_program_name(self):
        from rag_api.main_api import _subject_labels_in_question

        catalog = {"mandatorySubjects": [], "electiveSubjects": [
            {"code": "BIOLOGY", "name": "Sinh học"},
            {"code": "PHYSICS", "name": "Vật lí"},
        ]}
        question = ("Nhóm THPT Trưng Vương có Sinh học và tổ hợp chứa Sinh học được "
                    "ngành Vật lý kỹ thuật của Đại học Quy Nhơn chấp nhận không?")

        self.assertEqual(["Sinh học"], _subject_labels_in_question(question, catalog))

    def test_school_resolver_accepts_catalog_name_with_location_suffix(self):
        schools = [{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}]

        school, candidates = _resolve_school(
            "Trường THPT Trần Cao Vân tại Quy Nhơn", schools)

        self.assertEqual("TCV", school["code"])
        self.assertEqual(["TCV"], [candidate["code"] for candidate in candidates])

    def test_group_code_can_be_reused_only_from_user_history(self):
        history = [
            {"role": "user", "content": "Trường có nhóm môn 01 không?"},
            {"role": "assistant", "content": "Có nhóm 02."},
        ]

        self.assertEqual([{"code": "01", "evidence": "nhóm môn 01"}],
                         _history_group_codes(history))

    def test_query_filter_contract_maps_program_and_scopes_distinct_year_fields(self):
        filters = _normalize_query_filters({
            "extracted_filters": {
                "desired_program": "Công nghệ thông tin", "keyword": "legacy keyword",
                "university": "DQN", "score_max": 25,
                "combination_code": "A01", "canonical_combination_id": "MATH_PHYSICS_ENGLISH",
                "admission_year": "2026", "method_code": "100",
            },
            "academic_year": "2026-2027",
        })

        self.assertEqual("Công nghệ thông tin", filters["keyword"])
        self.assertEqual("MATH_PHYSICS_ENGLISH", filters["combination"])
        self.assertEqual("2026", filters["admission_year"])
        self.assertEqual("2026-2027", filters["academic_year"])
        self.assertNotIn("combination_code", filters)

    def test_current_turn_correction_overrides_stale_school_filter(self):
        parsed = {"extracted_filters": {"high_school": "THPT B", "university": "DQN"}}

        result = _apply_current_turn_precedence(
            parsed, "Không phải THPT A, chuyển sang THPT B",
            [{"role": "user", "content": "Tôi đang hỏi THPT A và Đại học Quy Nhơn"}],
        )

        self.assertEqual("THPT B", result["extracted_filters"]["high_school"])
        self.assertIsNone(result["extracted_filters"]["university"])

    def test_comparison_correction_keeps_prior_school_referenced_as_group_that(self):
        history = [{"role": "user", "content": "Cho mình hỏi nhóm môn 3 của trường THPT Trưng Vương Quy Nhơn gồm các môn nào?"}]
        parsed = {
            "intent": "subject_group_comparison",
            "high_schools": ["THPT Trưng Vương Quy Nhơn", "THPT Tôn Đức Thắng"],
            "requested_group_codes": [{"code": "3"}, {"code": "03"}],
            "requested_school_group_pairs": [
                {"school": "THPT Trưng Vương Quy Nhơn", "code": "3",
                 "evidence": "nhóm môn 3 của trường THPT Trưng Vương Quy Nhơn"},
                {"school": "THPT Tôn Đức Thắng", "code": "03",
                 "evidence": "nhóm 03 của THPT Tôn Đức Thắng"},
            ],
        }

        result = _apply_current_turn_precedence(
            parsed,
            "À nhầm, ý mình là muốn so sánh nhóm đó với nhóm 03 của THPT Tôn Đức Thắng cơ, hai nhóm này giống và khác nhau ở những môn tự chọn nào?",
            history,
        )

        self.assertEqual(parsed["high_schools"], result["high_schools"])

    def test_query_filter_contract_preserves_explicit_program_name_constraints(self):
        filters = _normalize_query_filters({
            "extracted_filters": {
                "university": "DQN",
                "requested_program_names": ["Công nghệ thông tin", "Công nghệ kỹ thuật ô tô"],
            },
            "admission_year": "2026",
        })

        self.assertEqual(["Công nghệ thông tin", "Công nghệ kỹ thuật ô tô"], filters["program_names"])

    def test_query_filter_contract_keeps_two_pathway_pairs_as_independent_scopes(self):
        intent = {
            "extracted_filters": {"university": "DQN"},
            "requested_pathway_pairs": [
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G04", "program_name": "Toan ung dung"},
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G01", "program_name": "Quan ly tai nguyen va moi truong"},
            ], "answer_scope": "subject_overlap",
        }
        filters = _normalize_query_filters(intent)
        self.assertEqual("subject_overlap", filters["answer_scope"])
        self.assertEqual("all_scopes_required", filters["comparison_scope_semantics"])
        self.assertEqual([
            {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
             "subject_group_code": "G04", "program_names": ["Toan ung dung"],
             "keyword": "Toan ung dung"},
            {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
             "subject_group_code": "G01", "program_names": ["Quan ly tai nguyen va moi truong"],
             "keyword": "Quan ly tai nguyen va moi truong"},
        ], filters["comparison_scopes"])
        filters["high_school"] = "THPT Trần Cao Vân"
        filters["high_school_code"] = "TCV"
        catalog_groups = [
            {"groupCode": "G04", "groupName": "Nhóm 4"},
            {"groupCode": "G01", "groupName": "Nhóm 1"},
        ]
        with patch("rag_api.main_api._backend_get", side_effect=[
            ["2026"], catalog_groups, ["2026"], catalog_groups,
        ]):
            resolution = _resolve_comparison_scopes_from_catalog(filters)
        self.assertEqual("resolved", resolution["status"])
        self.assertEqual(["G04", "G01"], [
            scope["subject_group_code"] for scope in resolution["scopes"]
        ])
        self.assertEqual([
            ["Toan ung dung"], ["Quan ly tai nguyen va moi truong"],
        ], [scope["program_names"] for scope in resolution["scopes"]])

    def test_parser_recovers_explicit_numbered_pathway_pairs_when_model_omits_them(self):
        question = (
            "Ở THPT Trần Cao Vân năm 2026, em muốn đối chiếu hai cặp: "
            "(1) bốn môn lựa chọn của Nhóm 4 với tổ hợp Hóa học–Toán–Vật lí của ngành Toán ứng dụng, "
            "và (2) bốn môn lựa chọn của Nhóm 1 với tổ hợp Địa lí–Toán–Vật lí của ngành "
            "Quản lý tài nguyên và môi trường tại ĐH Quy Nhơn. Với từng cặp, môn nào có trong cả nhóm "
            "và tổ hợp, môn nào không có trong danh sách môn lựa chọn của nhóm?"
        )
        raw_intent = json.dumps({
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "THPT Trần Cao Vân",
                "university": "ĐH Quy Nhơn",
            },
            "high_schools": ["THPT Trần Cao Vân"],
            "academic_year": "2026",
        }, ensure_ascii=False)

        parser = IntentParser(api_key="unit-test-key", model="unit-test-model")
        parsed = parser._clean_and_parse_json(raw_intent, [question])
        parsed = _apply_deterministic_route_contract(parsed, question)
        filters = _normalize_query_filters(parsed, question)

        self.assertEqual("subject_overlap", parsed["answer_scope"])
        self.assertEqual("all_scopes_required", filters["comparison_scope_semantics"])
        self.assertEqual([
            {
                "high_school": "THPT Trần Cao Vân",
                "academic_year": "2026",
                "subject_group_code": "4",
                "program_names": ["Toán ứng dụng"],
                "keyword": "Toán ứng dụng",
            },
            {
                "high_school": "THPT Trần Cao Vân",
                "academic_year": "2026",
                "subject_group_code": "1",
                "program_names": ["Quản lý tài nguyên và môi trường"],
                "keyword": "Quản lý tài nguyên và môi trường",
            },
        ], filters["comparison_scopes"])

    def test_single_explicit_pathway_does_not_become_a_pairwise_comparison(self):
        question = (
            "Ở THPT Trần Cao Vân năm 2026, Nhóm 4 có môn nào trùng với tổ hợp của ngành Toán ứng dụng "
            "tại ĐH Quy Nhơn?"
        )
        raw_intent = json.dumps({
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "THPT Trần Cao Vân",
                "university": "ĐH Quy Nhơn",
                "desired_program": "Toán ứng dụng",
            },
            "high_schools": ["THPT Trần Cao Vân"],
            "academic_year": "2026",
        }, ensure_ascii=False)

        parser = IntentParser(api_key="unit-test-key", model="unit-test-model")
        parsed = parser._clean_and_parse_json(raw_intent, [question])
        parsed = _apply_deterministic_route_contract(parsed, question)
        filters = _normalize_query_filters(parsed, question)

        self.assertEqual([], parsed["requested_pathway_pairs"])
        self.assertNotIn("comparison_scopes", filters)
        self.assertEqual("subject_overlap", filters["answer_scope"])

    def test_explicit_referential_subject_overlap_gets_overlap_scope(self):
        question = (
            "Nhóm môn nào ở trường đó có chứa môn nằm trong tổ hợp xét tuyển ngành "
            "Sư phạm Địa lý của Đại học Quy Nhơn?"
        )
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "Trường THPT Trưng Vương",
                "high_school_code": "TVQN",
                "desired_program": "Sư phạm Địa lý",
                "university": "Đại học Quy Nhơn",
            },
            "academic_year": "2026",
            "high_schools": ["Trường THPT Trưng Vương"],
        }, question)

        self.assertEqual("subject_overlap", parsed["answer_scope"])
        self.assertEqual("deterministic_explicit_scope", parsed["answer_scope_source"])
        self.assertEqual("subject_overlap", _normalize_query_filters(parsed, question)["answer_scope"])

    def test_explicit_overlap_does_not_require_the_word_program_or_major(self):
        question = "Ở THPT Trưng Vương năm 2026, nhóm G01 có môn nào trùng với tổ hợp A01?"
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school_code": "TVQN", "high_school": "TVQN"},
            "academic_year": "2026",
        }, question)

        self.assertEqual("subject_overlap", parsed["answer_scope"])
        self.assertEqual("subject_overlap", _normalize_query_filters(parsed, question)["answer_scope"])

    def test_mixed_overlap_and_admission_eligibility_uses_full_path(self):
        question = (
            "Nhóm G01 có môn nào trùng với tổ hợp xét tuyển ngành A, và như vậy có thể xét tuyển không?"
        )
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school_code": "TVQN", "high_school": "TVQN",
                                  "desired_program": "A", "university": "DQN"},
            "academic_year": "2026",
        }, question)

        self.assertEqual("full_path", parsed["answer_scope"])

    def test_explicit_overlap_without_academic_year_is_scoped_but_must_clarify(self):
        question = "Nhóm nào ở trường đó có môn chung với tổ hợp xét tuyển ngành A?"
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school_code": "TVQN", "desired_program": "A"},
        }, question)
        self.assertEqual("subject_overlap", parsed["answer_scope"])
        with patch("rag_api.main_api._backend_get") as backend_get:
            resolved = _resolve_explicit_graph_group_scope(parsed)
        self.assertEqual("missing_academic_year", resolved["graph_group_scope_resolution"]["status"])
        backend_get.assert_not_called()

    def test_explicit_overlap_without_school_is_scoped_but_must_clarify(self):
        question = "Nhóm nào có môn chung với tổ hợp xét tuyển ngành A?"
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"desired_program": "A"},
        }, question)
        self.assertEqual("subject_overlap", parsed["answer_scope"])
        resolved = _resolve_explicit_graph_group_scope(parsed)
        self.assertEqual("missing_school", resolved["graph_group_scope_resolution"]["status"])

    def test_explicit_group_overlap_does_not_guess_school_year(self):
        parsed = {
            "intent": "pathway_lookup", "answer_scope": "subject_overlap",
            "answer_scope_source": "deterministic_explicit_scope",
            "extracted_filters": {"high_school_code": "TVQN"},
            "requested_group_codes": [{"code": "G01", "evidence": "G01"}],
        }
        with patch("rag_api.main_api._backend_get") as backend_get:
            resolved = _resolve_explicit_graph_group_scope(parsed)
        self.assertEqual("missing_academic_year", resolved["graph_group_scope_resolution"]["status"])
        backend_get.assert_not_called()

    def test_explicit_admission_eligibility_uses_full_path_scope(self):
        question = (
            "Nhóm G01 có đủ toàn bộ môn trong tổ hợp xét tuyển ngành Sư phạm Địa lý không?"
        )
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "TVQN", "desired_program": "Sư phạm Địa lý"},
            "academic_year": "2026",
        }, question)

        self.assertEqual("full_path", parsed["answer_scope"])
        self.assertEqual("full_path", _normalize_query_filters(parsed, question)["answer_scope"])

    def test_ambiguous_subject_list_never_selects_overlap_scope(self):
        question = "Nhóm G01 với tổ hợp của ngành này có Toán, Địa lí và Hóa học không?"
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "TVQN", "desired_program": "Sư phạm Địa lý"},
            "academic_year": "2026",
        }, question)

        self.assertNotEqual("subject_overlap", parsed.get("answer_scope"))
        self.assertEqual("full_path", _normalize_query_filters(parsed, question)["answer_scope"])

    def test_parser_supplied_overlap_scope_is_not_trusted_without_explicit_wording(self):
        question = "Nhóm G01 của TVQN có phù hợp với ngành này không?"
        parsed = _apply_deterministic_route_contract({
            "intent": "pathway_lookup",
            "answer_scope": "subject_overlap",
            "answer_scope_source": "model",
            "extracted_filters": {"high_school": "TVQN", "desired_program": "Sư phạm Địa lý"},
        }, question)

        self.assertNotEqual("subject_overlap", parsed.get("answer_scope"))
        self.assertEqual("full_path", _normalize_query_filters(parsed, question)["answer_scope"])

    def test_pairwise_retrieval_executes_each_pair_with_its_own_group_and_program_filter(self):
        retriever = Mock()
        retriever.retrieve_context.side_effect = [
            [{"evidence_id": "path:g04-program-a", "program_code": "7460112", "evidence_path": []}],
            [{"evidence_id": "path:g01-program-b", "program_code": "7850101", "evidence_path": []}],
        ]
        filters = {
            "high_school_code": "TCV", "university": "DQN", "answer_scope": "subject_overlap",
            "comparison_scope_semantics": "all_scopes_required",
            "comparison_scopes": [
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G04", "program_names": ["Toan ung dung"],
                 "keyword": "Toan ung dung"},
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G01", "program_names": ["Quan ly tai nguyen va moi truong"],
                 "keyword": "Quan ly tai nguyen va moi truong"},
            ],
        }

        records, contract = _retrieve_pathway_comparison_scopes(retriever, filters, top_k=21)

        calls = retriever.retrieve_context.call_args_list
        self.assertEqual(["G04", "G01"], [call.kwargs["filters"]["subject_group_code"] for call in calls])
        self.assertEqual([["Toan ung dung"], ["Quan ly tai nguyen va moi truong"]],
                         [call.kwargs["filters"]["program_names"] for call in calls])
        self.assertEqual(["Toan ung dung", "Quan ly tai nguyen va moi truong"],
                         [call.kwargs["filters"]["keyword"] for call in calls])
        self.assertEqual(["2026", "2026"], [call.kwargs["filters"]["academic_year"] for call in calls])
        self.assertEqual(["path:g04-program-a", "path:g01-program-b"],
                         [record["evidence_id"] for record in records])
        self.assertEqual([0, 1], [record["requested_comparison_scope"] for record in records])
        self.assertEqual("all_scopes_required", contract["scope_semantics"])
        self.assertFalse(contract["complete"])
        self.assertEqual([0, 1], [scope["scope_index"] for scope in contract["scopes"]])

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_chat_executes_two_pathway_pairs_independently_and_keeps_incomplete_evidence(
        self, parser_mock, retriever_mock, generator_mock, backend_get_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {"university": "DQN", "high_school": "THPT Tran Cao Van"},
            "high_schools": ["THPT Tran Cao Van"],
            "requested_pathway_pairs": [
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G04", "program_name": "Toan ung dung"},
                {"high_school": "THPT Tran Cao Van", "academic_year": "2026",
                 "subject_group_code": "G01", "program_name": "Quan ly tai nguyen va moi truong"},
            ],
        }
        parser_mock.is_ready_for_db.return_value = True
        backend_get_mock.side_effect = lambda path: (
            [{"code": "TCV", "name": "THPT Tran Cao Van"}] if path == "/api/v1/high-schools"
            else ["2026"] if path.endswith("/academic-years")
            else [{"groupCode": "G04", "groupName": "group"},
                  {"groupCode": "G01", "groupName": "group"}]
        )
        retriever_mock.retrieve_context.side_effect = [
            [{"evidence_id": "path:g04", "program_code": "7460112",
              "verified_subject_overlaps": [{"validation_status": "verified"}], "evidence_path": []}],
            [{"evidence_id": "path:g01", "program_code": "7850101",
              "verified_subject_overlaps": [{"validation_status": "verified"}], "evidence_path": []}],
        ]
        retriever_mock.format_for_llm.return_value = "two scoped graph records"
        generator_mock.generate_answer.return_value = "Partial, scoped comparison."

        response = chat_endpoint(ChatRequest(
            user_message="Compare the two explicitly named group and program pairs for THPT Tran Cao Van in 2026."
        ))

        self.assertEqual("incomplete", response.status)
        self.assertEqual(2, retriever_mock.retrieve_context.call_count)
        self.assertEqual(["G04", "G01"], [call.kwargs["filters"]["subject_group_code"]
                                            for call in retriever_mock.retrieve_context.call_args_list])
        self.assertEqual([["Toan ung dung"], ["Quan ly tai nguyen va moi truong"]],
                         [call.kwargs["filters"]["program_names"]
                          for call in retriever_mock.retrieve_context.call_args_list])
        contract = response.data["graph_path_validation"]
        self.assertEqual("all_scopes_required", contract["scope_semantics"])
        self.assertFalse(contract["complete"])
        self.assertEqual(["path:g04", "path:g01"],
                         [row["evidence_id"] for row in response.data["retrieved_evidence"]])
        self.assertEqual("partial", response.data["answer_completeness"])
        generator_mock.generate_answer.assert_not_called()

    def test_single_program_name_is_a_hard_candidate_filter(self):
        filters = _normalize_query_filters({
            "extracted_filters": {"desired_program": "Kiem toan", "university": "DQN"},
        })
        self.assertEqual(["Kiem toan"], filters["program_names"])

    def test_keyword_only_candidate_discovery_stays_broad(self):
        filters = _normalize_query_filters({
            "intent": "pathway_lookup",
            "extracted_filters": {"keyword": "environmental fields", "university": "DQN"},
        })
        self.assertEqual("environmental fields", filters["keyword"])
        self.assertNotIn("program_names", filters)

    def test_explicit_single_group_scope_survives_query_filter_normalization(self):
        filters = _normalize_query_filters({
            "extracted_filters": {"high_school": "THPT Tran Cao Van", "high_school_code": "TCV",
                                  "desired_program": "Quan tri kinh doanh", "university": "DQN"},
            "academic_year": "2026",
            "requested_group_codes": [{"code": "G03", "evidence": "Nhom 3"}],
            "resolved_subject_group_code": "G03",
            "graph_group_scope_resolution": {"status": "resolved"},
            "answer_scope": "subject_overlap",
            "answer_scope_source": "deterministic_explicit_scope",
        })
        self.assertEqual("2026", filters["academic_year"])
        self.assertEqual("G03", filters["subject_group_code"])
        self.assertEqual("TCV", filters["high_school_code"])
        self.assertEqual("subject_overlap", filters["answer_scope"])

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_ambiguous_pathway_followup_clarifies_before_graph_or_generation(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {"desired_program": "Kiem toan", "university": "DQN"},
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = []
        history = [
            {"role": "user", "content": "Nam 2026 TCV co nhom mon nao chon Tin hoc?"},
            {"role": "assistant", "content": "Cac nhom gom G01, G02, G03 va G04."},
        ]
        response = chat_endpoint(ChatRequest(
            user_message="Vay neu em theo hoc nhom do thi co chac chan trung tuyen nganh Kiem toan khong?",
            context=history,
        ))
        self.assertEqual("incomplete", response.status)
        self.assertEqual("clarification", response.data["response_mode"])
        self.assertEqual("ambiguous_group_reference", response.data["clarification_reason"])
        self.assertFalse(response.data["actual_execution_route"]["graph_retrieval_executed"])
        retriever_mock.retrieve_context.assert_not_called()
        generator_mock.generate_answer.assert_not_called()

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_unique_prior_assistant_group_reference_reuses_catalog_scoped_group_and_year(
        self, parser_mock, _retriever_mock, _generator_mock, backend_get_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup", "extracted_filters": {},
            "requested_group_codes": [],
        }
        backend_get_mock.side_effect = [
            [{"code": "TCV", "name": "THPT Tran Cao Van"}],
            ["2026"],
            [{"groupCode": "G03", "groupName": "Nhom 3", "subjects": [{"code": "MATH", "name": "Toan"}]}],
        ]
        history = [
            {"role": "user", "content": "Nam 2026 TCV co nhom G03 khong?"},
            {"role": "assistant", "content": "THPT Tran Cao Van co nhom G03."},
        ]

        response = chat_endpoint(ChatRequest(user_message="Nhom do gom mon nao?", context=history))

        self.assertEqual("success", response.status)
        self.assertEqual("2026", response.data["academic_year"])
        self.assertEqual(["G03"], [row["groupCode"] for row in response.data["evidence"]])
        self.assertEqual("TCV", response.data["school"]["code"])

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_unique_prior_group_without_year_clarifies_instead_of_selecting_catalog_default(
        self, parser_mock, retriever_mock, generator_mock, backend_get_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "THPT Tran Cao Van", "high_school_code": "TCV",
                                  "university": "DQN", "desired_program": "Kiem toan",
                                  "admission_year": "2026"},
            "requested_group_codes": [],
            "school_resolution": {"status": "resolved", "school": {
                "code": "TCV", "name": "THPT Tran Cao Van",
            }},
        }
        parser_mock.is_ready_for_db.return_value = True
        backend_get_mock.return_value = [{"code": "TCV", "name": "THPT Tran Cao Van"}]
        retriever_mock.retrieve_context.return_value = []
        generator_mock.generate_answer.return_value = "Eligibility is not established by this empty retrieval."
        history = [
            {"role": "user", "content": "Em dang hoc THPT Tran Cao Van."},
            {"role": "assistant", "content": "Truong co nhom G03."},
        ]

        response = chat_endpoint(ChatRequest(
            user_message="Nhom do co du dieu kien xet tuyen DQN nganh Kiem toan nam 2026 khong?",
            context=history,
        ))

        self.assertEqual("incomplete", response.status)
        self.assertEqual("clarification", response.data["response_mode"])
        self.assertEqual("academic_year", response.data["clarification_reason"])
        self.assertEqual(["academic_year"], response.data["missing_eligibility_context"])
        self.assertFalse(response.data["actual_execution_route"]["graph_retrieval_executed"])
        self.assertEqual(["/api/v1/high-schools"], [call.args[0] for call in backend_get_mock.call_args_list])
        retriever_mock.retrieve_context.assert_not_called()
        generator_mock.generate_answer.assert_not_called()

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_conflicting_parsed_group_and_deictic_dialogue_scope_clarifies(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"desired_program": "Kiem toan"},
            "requested_group_codes": [{"code": "G02", "evidence": "nhom do"}],
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = []
        generator_mock.generate_answer.return_value = "Cannot verify eligibility from the available evidence."
        history = [
            {"role": "user", "content": "Nam 2026 TCV co nhom G03 khong?"},
            {"role": "assistant", "content": "Truong co nhom G03."},
        ]

        response = chat_endpoint(ChatRequest(
            user_message="Nhom do xet tuyen DQN nganh Kiem toan nam 2026?", context=history
        ))

        self.assertEqual("incomplete", response.status)
        self.assertEqual("clarification", response.data["response_mode"])
        self.assertEqual("dialogue_group_scope_conflict", response.data["clarification_reason"])
        self.assertFalse(response.data["actual_execution_route"]["graph_retrieval_executed"])
        retriever_mock.retrieve_context.assert_not_called()
        generator_mock.generate_answer.assert_not_called()

    @patch("rag_api.main_api._backend_get")
    def test_case2_explicit_school_year_group_scope_resolves_without_losing_filters(self, backend_get):
        backend_get.side_effect = [
            [{"code": "TCV", "name": "Truong THPT Tran Cao Van"}],
            ["2026"],
            [{"groupCode": "G03", "groupName": "Nhom 3"}],
        ]
        question = ("Em hoc truong THPT Tran Cao Van, chon Nhom 3 thi mon Giao duc kinh te va phap luat "
                    "trong nhom do co nam trong to hop xet tuyen nao cua nganh Quan tri kinh doanh "
                    "o Dai hoc Quy Nhon khong a?")
        parsed = {
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "Truong THPT Tran Cao Van", "desired_program": "Quan tri kinh doanh",
                                  "university": "Dai hoc Quy Nhon"},
            "high_schools": ["Truong THPT Tran Cao Van"], "academic_year": "2026",
            "requested_group_codes": [{"code": "3", "evidence": "Nhom 3"}],
        }
        parsed = _apply_current_turn_precedence(parsed, question, [])
        parsed = _apply_deterministic_route_contract(parsed, question)
        parsed = _resolve_explicit_school_filter(parsed, question, [])
        parsed = _resolve_explicit_graph_group_scope(parsed)
        filters = _normalize_query_filters(parsed, question)
        self.assertEqual("TCV", filters["high_school_code"])
        self.assertEqual("2026", filters["academic_year"])
        self.assertEqual("G03", filters["subject_group_code"])
        self.assertEqual("subject_overlap", filters["answer_scope"])

    def test_query_filter_extracts_each_explicitly_joined_program_from_current_question(self):
        question = ("Trường này năm 2026 có tuyển sinh ngành Công nghệ thông tin "
                    "(An toàn, an ninh mạng) và ngành Công nghệ kỹ thuật ô tô không?")

        filters = _normalize_query_filters(
            {"extracted_filters": {"university": "DQN", "keyword": "Công nghệ thông tin và Công nghệ kỹ thuật ô tô",
                                    "admission_year": "2026"}}, question)

        self.assertEqual(["Công nghệ thông tin (An toàn, an ninh mạng)", "Công nghệ kỹ thuật ô tô"],
                         filters["program_names"])

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._school_from_intent")
    def test_cross_school_comparison_uses_prior_pair_and_returns_both_scoped_groups(
            self, school_from_intent, backend_post, backend_get):
        history = [{"role": "user", "content":
                    "Cho mình hỏi nhóm môn 3 của trường THPT Trưng Vương Quy Nhơn gồm các môn nào?"}]
        question = ("À nhầm, ý mình là muốn so sánh nhóm đó với nhóm 03 của THPT Tôn Đức Thắng cơ, "
                    "hai nhóm này giống và khác nhau ở những môn tự chọn nào?")
        intent = {"intent": "subject_group_comparison", "high_schools": [
            "THPT Trưng Vương Quy Nhơn", "THPT Tôn Đức Thắng"], "extracted_filters": {},
            "requested_group_codes": [{"code": "3"}, {"code": "03"}],
            "requested_school_group_pairs": [
                {"school": "THPT Trưng Vương Quy Nhơn", "code": "3",
                 "evidence": "nhóm môn 3 của trường THPT Trưng Vương Quy Nhơn"},
                {"school": "THPT Tôn Đức Thắng", "code": "03",
                 "evidence": "nhóm 03 của THPT Tôn Đức Thắng"},
            ]}
        school_from_intent.side_effect = lambda one, *_args, **_kwargs: (
            {"code": "TVQN" if "Trưng Vương" in one["extracted_filters"]["high_school"] else "TĐT",
             "name": one["extracted_filters"]["high_school"]}, None)
        backend_get.side_effect = lambda url: (["2026"] if url.endswith("academic-years")
                                               else {"subjects": []} if url.endswith("classifications")
                                               else [])
        backend_post.side_effect = lambda url, _payload: {
            "academicYear": "2026", "groups": [{
                "groupCode": "G03" if "TVQN" in url else "03",
                "groupName": "group", "groupSubjects": [], "subjects": [],
                "mandatorySubjects": [], "matchingAdmissionCombinations": [],
                "matchedPreferredSubjects": [], "matchedDislikedSubjects": [],
                "gradeSignals": [], "strengthSignals": [], "confidenceSignals": [],
            }]}

        response = _compare_subject_groups(intent, "request-compare", context=history, current_message=question)

        self.assertEqual("success", response.status)
        self.assertEqual(2, len(response.data["comparisons"]))
        self.assertEqual(["TVQN", "TĐT"], [row["schoolCode"] for row in response.data["comparisons"]])
        self.assertTrue(all(len(row["groups"]) == 1 for row in response.data["comparisons"]))

    def test_selector_from_history_is_kept_only_for_explicit_follow_up(self):
        parsed = {"extracted_filters": {"high_school": "THPT A"}}
        history = [{"role": "user", "content": "Cho tôi xem nhóm môn trường THPT A"}]

        continued = _apply_current_turn_precedence(parsed, "Trường đó có nhóm nào?", history)
        unrelated = _apply_current_turn_precedence(parsed, "Tổ hợp A01 gồm môn nào?", history)

        self.assertEqual("THPT A", continued["extracted_filters"]["high_school"])
        self.assertIsNone(unrelated["extracted_filters"]["high_school"])

    def test_admission_year_clarification_answer_preserves_prior_pathway_scope(self):
        history = [
            {"role": "user", "content": (
                "Nếu em chọn học Nhóm 4 (năm học 2026) tại trường THPT Trần Cao Vân thì nhóm này có đáp ứng được "
                "trọn vẹn tổ hợp xét tuyển gồm Giáo dục kinh tế và pháp luật, Địa lí, Toán của ngành Quản lý tài nguyên "
                "và môi trường ở Trường Đại học Quy Nhơn không? Môn nào trong nhóm trùng khớp với tổ hợp này?"
            )},
            {"role": "assistant", "content": (
                "Để đối chiếu nhóm môn với điều kiện tuyển sinh, em bổ sung giúp mình năm tuyển sinh muốn đối chiếu. "
                "Khi thiếu các thông tin này, mình chưa thể kết luận nhóm môn có đáp ứng điều kiện xét tuyển hay không."
            )},
        ]
        parsed = {
            "intent": "pathway_lookup",
            "high_schools": ["Trường THPT Trần Cao Vân"],
            "requested_group_codes": [{"code": "G04", "evidence": "Nhóm 4"}],
            "extracted_filters": {
                "high_school": "Trường THPT Trần Cao Vân",
                "high_school_code": "TCV",
                "desired_program": "Quản lý tài nguyên và môi trường",
                "university": "Trường Đại học Quy Nhơn",
                "academic_year": "2026",
                "admission_year": "2026",
            },
            "academic_year": "2026",
        }

        result = _apply_current_turn_precedence(
            parsed, "Năm tuyển sinh mà em hỏi là 2026.", history,
        )

        self.assertEqual("Trường THPT Trần Cao Vân", result["extracted_filters"]["high_school"])
        self.assertEqual("Quản lý tài nguyên và môi trường", result["extracted_filters"]["desired_program"])
        self.assertEqual("Trường Đại học Quy Nhơn", result["extracted_filters"]["university"])
        self.assertEqual("2026", result["extracted_filters"]["academic_year"])
        self.assertEqual("2026", result["extracted_filters"]["admission_year"])
        self.assertEqual(["Trường THPT Trần Cao Vân"], result["high_schools"])

    def test_year_value_does_not_reuse_scope_without_prior_year_clarification(self):
        parsed = {
            "high_schools": ["Trường THPT Trần Cao Vân"],
            "extracted_filters": {"high_school": "Trường THPT Trần Cao Vân"},
        }
        history = [
            {"role": "user", "content": "Tôi đang hỏi về Trường THPT Trần Cao Vân."},
            {"role": "assistant", "content": "Bạn muốn xem các nhóm môn nào?"},
        ]

        result = _apply_current_turn_precedence(
            parsed, "Năm tuyển sinh là 2026.", history,
        )

        self.assertIsNone(result["extracted_filters"]["high_school"])
        self.assertEqual([], result["high_schools"])

    def test_admission_year_clarification_does_not_overwrite_distinct_academic_year(self):
        history = [
            {"role": "user", "content": (
                "Nh\u00f3m 4 n\u0103m h\u1ecdc 2025-2026 c\u1ee7a THPT Tr\u1ea7n Cao V\u00e2n c\u00f3 đủ tổ hợp của ngành X không?"
            )},
            {"role": "assistant", "content": "Em b\u1ed5 sung n\u0103m tuy\u1ec3n sinh mu\u1ed1n đối chiếu giúp mình."},
        ]
        parsed = {
            "academic_year": "2025-2026",
            "extracted_filters": {
                "high_school": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n",
                "academic_year": "2025-2026",
                "admission_year": "2027",
            },
            "high_schools": ["Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"],
        }

        result = _apply_current_turn_precedence(parsed, "N\u0103m tuy\u1ec3n sinh l\u00e0 2027.", history)

        self.assertEqual("2025-2026", result["extracted_filters"]["academic_year"])
        self.assertEqual("2027", result["extracted_filters"]["admission_year"])
        normalized = _normalize_query_filters(result)
        self.assertEqual("2025-2026", normalized["academic_year"])
        self.assertEqual("2027", normalized["admission_year"])

    @patch("rag_api.main_api._backend_get")
    def test_unrelated_latest_user_turn_does_not_reuse_an_older_school_group_scope(self, backend_get):
        history = [
            {"role": "user", "content": "Nh\u00f3m 3 n\u0103m h\u1ecdc 2025 c\u1ee7a THPT A c\u00f3 g\u1ed3m nh\u1eefng m\u00f4n n\u00e0o?"},
            {"role": "assistant", "content": "Nh\u00f3m 3 c\u1ee7a THPT A c\u00f3 c\u00e1c m\u00f4n ..."},
            {"role": "user", "content": "T\u00f4i mu\u1ed1n t\u00ecm hi\u1ec3u ng\u00e0nh K\u1ef9 thu\u1eadt \u0111i\u1ec7n."},
            {"role": "assistant", "content": "Em cho m\u00ecnh bi\u1ebft n\u0103m tuy\u1ec3n sinh mu\u1ed1n \u0111\u1ed1i chi\u1ebfu."},
        ]
        current = "N\u0103m tuy\u1ec3n sinh l\u00e0 2027."
        parsed = {
            "intent": "pathway_lookup",
            "extracted_filters": {"high_school": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng", "high_school_code": "TVQN"},
            "high_schools": ["THPT A"],
            "requested_group_codes": [{"code": "G03", "evidence": "Nh\u00f3m 3"}],
        }

        continued = _apply_current_turn_precedence(parsed, current, history)
        routed = _apply_deterministic_route_contract(continued, current)
        backend_get.assert_not_called()
        resolved = _resolve_explicit_school_filter(routed, current, history)
        backend_get.assert_not_called()
        scoped = _resolve_explicit_graph_group_scope(resolved, current, history)

        self.assertNotIn("graph_group_scope_resolution", scoped)
        backend_get.assert_not_called()

    def test_assistant_suggestion_is_not_reused_as_user_confirmed_selector(self):
        parsed = {"extracted_filters": {"high_school": "THPT B"}}
        history = [
            {"role": "user", "content": "Tôi đang tìm hiểu THPT A"},
            {"role": "assistant", "content": "Em có thể tham khảo thêm THPT B."},
        ]

        result = _apply_current_turn_precedence(parsed, "Trường đó có nhóm nào?", history)

        self.assertIsNone(result["extracted_filters"]["high_school"])

    def test_current_turn_corrects_a_negated_combination_code(self):
        parsed = {"extracted_filters": {"combination_code": "A01"}}

        result = _apply_current_turn_precedence(
            parsed, "Khong phai A01, em muon K01", []
        )

        self.assertEqual("K01", result["extracted_filters"]["combination_code"])

    @patch("rag_api.main_api._backend_get")
    def test_pathway_public_code_resolves_only_to_a_single_verified_canonical_id(self, backend_get):
        backend_get.return_value = {"labels": [
            {"code": "A01", "subjectDetails": "Toán, Lý, Anh", "canonicalCombinationId": "MATH_PHYSICS_ENGLISH"},
            {"code": "A01", "subjectDetails": "Toán, Vật lí, Tiếng Anh", "canonicalCombinationId": "MATH_PHYSICS_ENGLISH"},
        ]}

        canonical_id, response = _resolve_combination_for_pathway("A01", "request")

        self.assertEqual("MATH_PHYSICS_ENGLISH", canonical_id)
        self.assertEqual("backend_admission_combination_api", response["evidence_source"])

    def test_removes_current_question_if_legacy_client_included_it_in_history(self):
        request = ChatRequest(user_message="Trường A", context=[
            {"role": "user", "content": "Tôi thích Toán"},
            {"role": "assistant", "content": "Em quan tâm trường nào?"},
            {"role": "user", "content": "Trường A"},
        ])

        self.assertEqual(2, len(_prior_turns(request)))
        self.assertEqual("Tôi thích Toán", _prior_turns(request)[0]["content"])

    def test_exact_catalog_school_name_precedes_a_longer_alias(self):
        schools = [
            {"code": "A", "name": "THPT Nguyễn Du"},
            {"code": "B", "name": "THPT Nguyễn Du 2"},
        ]

        school, candidates = _resolve_school("Nguyễn Du", schools)

        self.assertEqual("A", school["code"])
        self.assertEqual(["A"], [candidate["code"] for candidate in candidates])

    def test_subject_match_does_not_select_first_ambiguous_partial(self):
        catalog = {"mandatorySubjects": [], "electiveSubjects": [
            {"code": "HISTORY", "name": "Lịch sử"},
            {"code": "HIST_GEO", "name": "Lịch sử và Địa lí"},
        ]}

        subject_code, candidates = _resolve_subject("Lịch", catalog)

        self.assertIsNone(subject_code)
        self.assertEqual([], candidates)

    def test_resolves_common_colloquial_name_to_the_catalog_label(self):
        catalog = {"mandatorySubjects": [{"code": "LITERATURE", "name": "Ngữ văn"}],
                   "electiveSubjects": []}

        subject_code, candidates = _resolve_subject("Văn", catalog)

        self.assertEqual("LITERATURE", subject_code)
        self.assertEqual(["LITERATURE"], candidates)

    def test_distinguishes_university_codes_from_name_fragments(self):
        self.assertEqual(("DQN", None), _university_search_filter("dqn"))
        self.assertEqual((None, "Đại học Quy Nhơn"), _university_search_filter("Đại học Quy Nhơn"))

    def test_chat_result_exposes_ordered_retrieval_units_with_provenance(self):
        response = _chat_result("success", "answer", "request-1", "pathway_lookup", {
            "evidence": [{"id": "one", "relationship_ids": ["edge-1"]}, {"id": "two"}],
            "evidence_source": "neo4j_graph",
            "generation_executed": True,
        })

        self.assertEqual("success", response.status)
        self.assertEqual("answer", response.message)
        self.assertEqual([{"id": "one", "relationship_ids": ["edge-1"]}, {"id": "two"}], response.data["evidence"])
        self.assertEqual("answer", response.data["response_mode"])
        self.assertEqual(["one", "two"], [unit["id"] for unit in response.data["retrieval_units"]])
        self.assertTrue(all(unit["source"] == "neo4j_graph" for unit in response.data["retrieval_units"]))
        self.assertEqual("graph_retrieval_generation", response.data["actual_execution_route"]["execution_kind"])
        self.assertTrue(response.data["actual_execution_route"]["graph_retrieval_executed"])
        self.assertEqual(["one", "two"], response.data["actual_execution_route"]["observed_entity_ids"])
        self.assertEqual(["edge-1"], response.data["actual_execution_route"]["observed_relationship_ids"])
        self.assertEqual("request-1", response.data["actual_execution_route"]["trace_ref"])

    def test_graph_route_without_generation_stage_flag_remains_unknown(self):
        response = _chat_result("success", "answer", "unclassified-graph", "pathway_lookup", {
            "evidence": [{"id": "one"}], "evidence_source": "neo4j_graph",
        })

        self.assertEqual("unknown", response.data["actual_execution_route"]["execution_kind"])
        self.assertIsNone(response.data["actual_execution_route"]["generation_executed"])

    def test_chat_result_distinguishes_backend_lookup_from_hybrid_comparison(self):
        backend = _chat_result("success", "answer", "request-2", "combination_lookup", {
            "evidence": [{"id": "A01"}],
            "evidence_source": "backend_admission_combination_api",
        })
        backend_groups = _chat_result("success", "answer", "request-2b", "school_group_lookup", {
            "evidence": {"groups": [{"groupCode": "01"}], "labels": [{"code": "A01"}]},
            "evidence_source": "backend_subject_group_api",
        })
        hybrid = _chat_result("success", "answer", "request-3", "subject_group_comparison", {
            "evidence": [{"id": "TDT-2026-01"}],
            "evidence_source": "backend_subject_group_comparison_api",
        })

        self.assertEqual("backend_lookup", backend.data["actual_execution_route"]["execution_kind"])
        self.assertFalse(backend.data["actual_execution_route"]["graph_retrieval_executed"])
        self.assertEqual(["01", "A01"], backend_groups.data["actual_execution_route"]["observed_entity_ids"])
        self.assertEqual("unknown", hybrid.data["actual_execution_route"]["execution_kind"])
        self.assertIsNone(hybrid.data["actual_execution_route"]["graph_retrieval_executed"])
        confirmed_hybrid = _chat_result("success", "answer", "request-3b", "subject_group_comparison", {
            "evidence": [{"id": "TDT-2026-01"}],
            "evidence_source": "backend_subject_group_comparison_api",
            "backend_retrieval_executed": True, "graph_retrieval_executed": True,
            "hybrid_composition_verified": None,
        })
        self.assertEqual("unknown", confirmed_hybrid.data["actual_execution_route"]["execution_kind"])
        composed_hybrid = _chat_result("success", "answer", "request-3c", "subject_group_comparison", {
            "evidence": [{"id": "TDT-2026-01", "evidence_origin": "backend_and_graph_composed"}],
            "evidence_source": "backend_subject_group_comparison_api",
            "backend_retrieval_executed": True, "graph_retrieval_executed": True,
            "hybrid_composition_verified": True,
        })
        self.assertEqual("hybrid", composed_hybrid.data["actual_execution_route"]["execution_kind"])
        self.assertNotIn("graph_path_validation", backend.data)
        self.assertNotIn("graph_path_validation", backend_groups.data)
        self.assertNotIn("graph_path_validation", hybrid.data)

    def test_graph_validation_payload_uses_only_validated_directed_path_evidence(self):
        record = {
            "university_code": "UA", "program_code": "P1",
            "combinations": [{
                "id": "C1",
                "acceptance_records": [{"admission_year": 2026, "method_codes": "100"}],
                "required_subject_codes": ["MATH"],
            }],
        }
        retriever = Neo4jRetriever(uri="bolt://localhost", auth=("user", "pass"), database="edugraph")
        enriched = retriever.attach_evidence_provenance([record])[0]
        payload = _graph_path_validation_payload([enriched], {"admission_year": 2026})

        self.assertEqual(2, payload["schema_version"])
        self.assertEqual("directed_edge_set", payload["path_semantics"])
        self.assertEqual("neo4j_graph", payload["source"])
        self.assertTrue(payload["complete"])
        self.assertEqual("complete", payload["records"][0]["status"])
        self.assertEqual(
            set(payload["records"][0]["required_relationship_ids"]),
            set(payload["records"][0]["relationship_ids"]),
        )

        reversed_edge = dict(enriched["evidence_path"][0])
        reversed_edge["from"], reversed_edge["to"] = reversed_edge["to"], reversed_edge["from"]
        tampered = {**enriched, "evidence_path": [reversed_edge, *enriched["evidence_path"][1:]]}
        incomplete_payload = _graph_path_validation_payload([tampered], {"admission_year": 2026})
        self.assertFalse(incomplete_payload["complete"])
        self.assertEqual("incomplete", incomplete_payload["records"][0]["status"])

    def test_empty_graph_validation_contract_is_incomplete(self):
        payload = _graph_path_validation_payload([])

        self.assertEqual([], payload["records"])
        self.assertFalse(payload["complete"])

    def test_chat_result_marks_non_retrieval_response_as_unknown_route(self):
        response = _chat_result("incomplete", "clarify", "request-4", "subject_group_advice")

        self.assertEqual("unknown", response.data["actual_execution_route"]["execution_kind"])
        self.assertIsNone(response.data["actual_execution_route"]["graph_retrieval_executed"])
        self.assertEqual("subject_group_advice", response.data["actual_execution_route"]["handler"])

    def test_execution_metadata_separates_handler_route_source_and_disposition(self):
        cases = [
            ("success", {"evidence_source": "backend_high_school_catalog", "retrieval_executed": True},
             "backend_lookup", False, "answer", "backend_high_school_catalog"),
            ("success", {"evidence_source": "backend_subject_group_comparison_api", "retrieval_executed": True},
             "unknown", None, "answer", "backend_subject_group_comparison_api"),
            ("success", {"evidence_source": "neo4j_graph", "retrieval_executed": True,
                         "generation_executed": True},
             "graph_retrieval_generation", True, "answer", "neo4j_graph"),
            ("incomplete", {"retrieval_executed": False, "clarification_reason": "missing_group"},
             "not_executed", False, "clarification", None),
            ("incomplete", {"evidence_source": "backend_high_school_catalog", "retrieval_executed": True,
                             "clarification_reason": "missing_group"},
             "backend_lookup", False, "clarification", "backend_high_school_catalog"),
            ("unsupported", {"retrieval_executed": False, "unsupported_reason": "unsupported_domain"},
             "not_executed", False, "unsupported", None),
            ("no_data", {"retrieval_executed": True, "evidence_source": "backend_high_school_catalog",
                          "abstention_reason": "no_matching_record"},
             "backend_lookup", False, "abstention", "backend_high_school_catalog"),
            ("success", {}, "unknown", None, "answer", None),
        ]
        for status, data, kind, graph_executed, disposition, source in cases:
            with self.subTest(status=status, data=data):
                response = _chat_result(status, "same answer text", "request-route", "pathway_lookup", data)
                route = response.data["actual_execution_route"]
                self.assertEqual("pathway_lookup", route["intent_handler"])
                self.assertEqual(kind, route["execution_kind"])
                self.assertEqual(graph_executed, route["graph_retrieval_executed"])
                self.assertEqual(source, route["evidence_source"])
                self.assertEqual(disposition, route["response_disposition"])
                self.assertEqual(data.get("clarification_reason") or data.get("abstention_reason") or
                                 data.get("unsupported_reason"), route["disposition_reason"])
                if kind == "unknown":
                    self.assertEqual("execution_paths_unverified" if source else "evidence_source_missing",
                                     route["unknown_route_reason"])
                else:
                    self.assertIsNone(route["unknown_route_reason"])

    @patch("rag_api.main_api._backend_get")
    def test_school_group_lookup_returns_only_explicitly_requested_group(self, backend_get):
        backend_get.side_effect = [
            [{"code": "S01", "name": "THPT A"}],
            ["2026"],
            [
                {"groupCode": "01", "groupName": "Nhóm 01", "subjects": [{"name": "Vật lí"}]},
                {"groupCode": "02", "groupName": "Nhóm 02", "subjects": [{"name": "Tin học"}]},
            ],
        ]
        intent = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "academic_year": "2026",
            "requested_group_codes": [{"code": "02", "evidence": "xem nhóm 02"}],
        }

        response = _lookup_school_groups(intent, "request-2")

        self.assertIn("Nhóm 02", response.message)
        self.assertNotIn("Nhóm 01", response.message)
        self.assertEqual(["02"], [group["groupCode"] for group in response.data["evidence"]])
        self.assertEqual("backend_subject_group_api", response.data["retrieval_units"][0]["source"])

    @patch("rag_api.main_api._backend_get")
    def test_school_group_lookup_keeps_available_facts_and_marks_missing_requested_group_partial(self, backend_get):
        backend_get.side_effect = [
            [{"code": "S01", "name": "THPT A"}],
            ["2026"],
            [{"groupCode": "01", "groupName": "Nhom 01", "subjects": [{"name": "Toan"}]}],
        ]
        intent = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "academic_year": "2026",
            "requested_group_codes": [
                {"code": "01", "evidence": "xem nhom 01"},
                {"code": "09", "evidence": "xem nhom 09"},
            ],
        }

        response = _lookup_school_groups(intent, "request-partial")

        self.assertEqual("success", response.status)
        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertEqual(["09"], response.data["unavailable_group_codes"])
        self.assertIn("Nhom 01", response.message)
        self.assertIn("09", response.message)
        self.assertIn("does not establish", response.message)

    @patch("rag_api.main_api._backend_get")
    def test_history_follow_up_resolves_user_school_and_requested_group(self, backend_get):
        backend_get.side_effect = [
            [{"code": "TDT", "name": "Trường THPT Tôn Đức Thắng"}],
            ["2026"],
            [
                {"groupCode": "01", "groupName": "Nhóm 01", "subjects": [{"name": "Tin học"}]},
                {"groupCode": "02", "groupName": "Nhóm 02", "subjects": [{"name": "Vật lí"}]},
            ],
        ]
        intent = {"intent": "school_group_lookup", "extracted_filters": {}, "academic_year": "2026"}
        context = [{"role": "user", "content":
                    "Em muốn tìm hiểu trường THPT Tôn Đức Thắng ở Gia Lai. Trường có nhóm môn 01 không ạ?"}]

        response = _lookup_school_groups(
            intent, "history-school", context=context,
            current_message="Nhóm đó có môn Tin học không, và gồm những môn nào ạ?",
        )

        self.assertEqual("success", response.status)
        self.assertEqual(["01"], [group["groupCode"] for group in response.data["evidence"]])
        self.assertIn("Tin học", response.message)
        self.assertNotIn("Nhóm 02", response.message)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_comparison_supports_two_explicit_schools_through_existing_api(self, backend_get, backend_post):
        schools = [
            {"code": "TV", "name": "THPT Trưng Vương"},
            {"code": "TCV", "name": "THPT Trần Cao Vân"},
        ]
        catalog = {"mandatorySubjects": [], "electiveSubjects": []}
        backend_get.side_effect = [[*schools], catalog, [*schools], catalog]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}], "subjects": [{"code": "MATH", "name": "Toán"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "LITERATURE", "name": "Ngữ văn"}], "subjects": [{"code": "LITERATURE", "name": "Ngữ văn"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
        ]
        intent = {"intent": "subject_group_comparison", "high_schools": [
            "THPT Trưng Vương", "THPT Trần Cao Vân"], "extracted_filters": {},
            "academic_year": "2026", "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [], "grades_by_subject": [],
            "grades_needing_scale": [], "requested_group_codes": [{"code": "G02", "evidence": "G02"}]}

        response = _compare_subject_groups(intent, "two-schools", current_message="So sánh G02 hai trường")

        self.assertEqual("success", response.status)
        self.assertEqual("cross_school", response.data["comparison_mode"])
        self.assertEqual(2, backend_post.call_count)
        self.assertIn("THPT Trưng Vương", response.message)
        self.assertIn("THPT Trần Cao Vân", response.message)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_chat_endpoint_routes_two_school_factual_comparison_to_independent_lookups(
        self, parser_mock, _retriever_mock, _generator_mock, backend_get, backend_post
    ):
        schools = [
            {"code": "TVQN", "name": "THPT Trưng Vương"},
            {"code": "TCV", "name": "THPT Trần Cao Vân"},
        ]
        catalog = {"mandatorySubjects": [], "electiveSubjects": []}
        backend_get.side_effect = [schools, catalog, schools, catalog]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}], "subjects": [],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}], "subjects": [],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
        ]
        parser_mock.parse_intent.return_value = {
            "intent": "subject_group_comparison",
            "high_schools": ["THPT Trưng Vương", "THPT Trần Cao Vân"],
            "extracted_filters": {}, "academic_year": "2026",
            "requested_group_codes": [],
            "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [],
            "grades_by_subject": [], "grades_needing_scale": [],
        }

        response = chat_endpoint(ChatRequest(user_message=(
            "So sánh các nhóm môn tại THPT Trưng Vương và THPT Trần Cao Vân")))

        self.assertEqual("success", response.status)
        self.assertEqual("cross_school", response.data["comparison_mode"])
        self.assertIn("Toán", response.message)
        self.assertIn("Sinh học", response.message)
        self.assertEqual(2, backend_post.call_count)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_two_school_comparison_returns_grounded_partial_when_one_school_has_no_data(
        self, backend_get, backend_post
    ):
        schools = [
            {"code": "TVQN", "name": "THPT Trưng Vương"},
            {"code": "TCV", "name": "THPT Trần Cao Vân"},
        ]
        catalog = {"mandatorySubjects": [], "electiveSubjects": []}
        backend_get.side_effect = [schools, catalog, schools, catalog]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}], "subjects": [],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
            {"academicYear": "2026", "groups": []},
        ]
        response = _compare_subject_groups({
            "intent": "subject_group_comparison",
            "high_schools": ["THPT Trưng Vương", "THPT Trần Cao Vân"],
            "extracted_filters": {}, "academic_year": "2026",
            "requested_group_codes": [{"code": "G02", "evidence": "G02"}],
            "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [],
            "grades_by_subject": [], "grades_needing_scale": [],
        }, "partial-two-school", current_message="So sánh G02 ở cả hai trường")

        self.assertEqual("success", response.status)
        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertEqual([{"school_code": "TCV", "group_code": "G02"}],
                         response.data["unavailable_group_requests"])
        self.assertIn("Toán", response.message)
        self.assertNotIn("Môn chung:", response.message)

    @patch("rag_api.main_api._backend_get")
    def test_singular_group_reference_after_multiple_prior_groups_still_clarifies(self, backend_get):
        backend_get.return_value = [{"code": "TVQN", "name": "THPT Trưng Vương"}]
        response = _lookup_school_groups(
            {"intent": "school_group_lookup", "extracted_filters": {}, "academic_year": "2026"},
            "ambiguous-history-group",
            context=[{"role": "user", "content":
                      "Trường THPT Trưng Vương có nhóm 01 và nhóm 02 không?"}],
            current_message="Nhóm đó gồm những môn nào?",
        )

        self.assertEqual("incomplete", response.status)
        self.assertEqual("clarification", response.data["response_mode"])
        self.assertIn("01, 02", response.message)
        self.assertEqual(1, backend_get.call_count)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_cross_school_comparison_keeps_each_requested_group_with_its_school(self, backend_get, backend_post):
        schools = [
            {"code": "TCV", "name": "THPT Trần Cao Vân"},
            {"code": "TVQN", "name": "THPT Trưng Vương"},
        ]
        catalog = {"mandatorySubjects": [], "electiveSubjects": []}
        backend_get.side_effect = [schools, catalog, schools, catalog]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [
                {"groupCode": "G04", "groupName": "Nhóm G04", "groupSubjects": [
                    {"code": "MATH", "name": "Toán"}, {"code": "PHYSICS", "name": "Vật lí"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
                {"groupCode": "G03", "groupName": "Nhóm G03", "groupSubjects": [
                    {"code": "HISTORY", "name": "Lịch sử"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
            ]},
            {"academicYear": "2026", "groups": [
                {"groupCode": "G03", "groupName": "Nhóm G03", "groupSubjects": [
                    {"code": "MATH", "name": "Toán"}, {"code": "BIOLOGY", "name": "Sinh học"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
                {"groupCode": "G04", "groupName": "Nhóm G04", "groupSubjects": [
                    {"code": "CHEMISTRY", "name": "Hóa học"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
            ]},
        ]
        intent = {
            "intent": "subject_group_comparison", "high_schools": [school["name"] for school in schools],
            "extracted_filters": {}, "academic_year": "2026", "preferred_subjects": [],
            "disliked_subjects": [], "self_reported_strengths": [], "confidence_by_subject": [],
            "grades_by_subject": [], "grades_needing_scale": [],
            "requested_group_codes": [],
            "requested_school_group_pairs": [
                {"school": schools[0]["name"], "code": "G04", "evidence": "G04 của THPT Trần Cao Vân"},
                {"school": schools[1]["name"], "code": "G03", "evidence": "G03 của THPT Trưng Vương"},
            ],
        }
        question = "So sánh nhóm G04 của THPT Trần Cao Vân với nhóm G03 của THPT Trưng Vương."

        response = _compare_subject_groups(intent, "pair-test", current_message=question)

        self.assertEqual("success", response.status)
        self.assertEqual(["G04", "G03"], [row["groups"][0]["groupCode"] for row in response.data["comparisons"]])
        self.assertEqual([1, 1], [len(row["groups"]) for row in response.data["comparisons"]])
        self.assertEqual(["MATH"], [row["code"] for row in response.data["shared_subjects"]])
        self.assertEqual(["PHYSICS"], [row["code"] for row in response.data["school_only_subjects"][0]])
        self.assertEqual(["BIOLOGY"], [row["code"] for row in response.data["school_only_subjects"][1]])

    @patch("rag_api.main_api._backend_get")
    def test_cross_school_comparison_clarifies_unpaired_different_group_codes(self, backend_get):
        intent = {
            "intent": "subject_group_comparison", "high_schools": ["THPT Trần Cao Vân", "THPT Trưng Vương"],
            "extracted_filters": {}, "requested_group_codes": [
                {"code": "G04", "evidence": "G04"}, {"code": "G03", "evidence": "G03"}],
        }

        response = _compare_subject_groups(intent, "ambiguous-pairs", current_message="So sánh G04 với G03 giữa hai trường")

        self.assertEqual("incomplete", response.status)
        backend_get.assert_not_called()

    @patch("rag_api.main_api._backend_get")
    def test_cross_school_comparison_clarifies_conflicting_pairs_in_user_message(self, backend_get):
        intent = {
            "intent": "subject_group_comparison",
            "high_schools": ["THPT Trần Cao Vân", "THPT Trưng Vương"],
            "extracted_filters": {},
            "requested_group_codes": [{"code": "G04"}, {"code": "G03"}],
            "requested_school_group_pairs": [
                {"school": "THPT Trần Cao Vân", "code": "G04", "evidence": "G04 của THPT Trần Cao Vân"},
                {"school": "THPT Trưng Vương", "code": "G03", "evidence": "G03 của THPT Trưng Vương"},
            ],
        }
        question = ("So sánh G04 của THPT Trần Cao Vân, G03 của THPT Trần Cao Vân "
                    "và G03 của THPT Trưng Vương.")

        response = _compare_subject_groups(intent, "conflicting-pairs", current_message=question)

        self.assertEqual("incomplete", response.status)
        backend_get.assert_not_called()

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_cross_school_comparison_recovers_explicit_pairs_when_parser_omits_them(self, backend_get, backend_post):
        schools = [{"code": "TCV", "name": "THPT Trần Cao Vân"},
                   {"code": "TVQN", "name": "THPT Trưng Vương"}]
        backend_get.side_effect = [schools, {"mandatorySubjects": [], "electiveSubjects": []},
                                   schools, {"mandatorySubjects": [], "electiveSubjects": []}]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [{"groupCode": "G04", "groupName": "G04",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
            {"academicYear": "2026", "groups": [{"groupCode": "G03", "groupName": "G03",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
        ]
        intent = {"intent": "subject_group_comparison", "high_schools": [row["name"] for row in schools],
                  "extracted_filters": {}, "academic_year": "2026", "preferred_subjects": [],
                  "disliked_subjects": [], "self_reported_strengths": [], "confidence_by_subject": [],
                  "grades_by_subject": [], "grades_needing_scale": [],
                  "requested_group_codes": [{"code": "G04"}, {"code": "G03"}]}

        response = _compare_subject_groups(
            intent, "recover-pairs",
            current_message="Em phân vân giữa nhóm G04 của trường THPT Trần Cao Vân và nhóm G03 của trường THPT Trưng Vương. Hai nhóm khác nhau ra sao?",
        )

        self.assertEqual("success", response.status)
        self.assertEqual(["G04", "G03"], [row["groups"][0]["groupCode"] for row in response.data["comparisons"]])
        self.assertIn("Môn chung", response.message)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_cross_school_comparison_recovers_group_code_missing_from_parser(self, backend_get, backend_post):
        schools = [{"code": "TCV", "name": "THPT Trần Cao Vân"},
                   {"code": "TVQN", "name": "THPT Trưng Vương"}]
        backend_get.side_effect = [schools, {"mandatorySubjects": [], "electiveSubjects": []},
                                   schools, {"mandatorySubjects": [], "electiveSubjects": []}]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [
                {"groupCode": "G04", "groupName": "G04",
                 "groupSubjects": [{"code": "PHYSICS", "name": "Vật lí"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
                {"groupCode": "G03", "groupName": "G03",
                 "groupSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
            ]},
            {"academicYear": "2026", "groups": [
                {"groupCode": "G03", "groupName": "G03",
                 "groupSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
                {"groupCode": "G04", "groupName": "G04",
                 "groupSubjects": [{"code": "PHYSICS", "name": "Vật lí"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
            ]},
        ]
        intent = {"intent": "subject_group_comparison", "high_schools": [row["name"] for row in schools],
                  "extracted_filters": {}, "academic_year": "2026", "preferred_subjects": [],
                  "disliked_subjects": [], "self_reported_strengths": [], "confidence_by_subject": [],
                  "grades_by_subject": [], "grades_needing_scale": [],
                  "requested_group_codes": [{"code": "G04", "evidence": "G04"}]}

        response = _compare_subject_groups(
            intent, "omitted-code",
            current_message="So sánh G04 của THPT Trần Cao Vân với G03 của THPT Trưng Vương.",
        )

        self.assertEqual("success", response.status)
        self.assertEqual(["G04", "G03"], [row["groups"][0]["groupCode"] for row in response.data["comparisons"]])
        self.assertEqual([1, 1], [len(row["groups"]) for row in response.data["comparisons"]])

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_two_school_group_collision_pairs_numeric_and_prefixed_codes_from_explicit_school_clauses(
        self, backend_get, backend_post
    ):
        schools = [
            {"code": "TVQN", "name": "THPT Trưng Vương"},
            {"code": "TDT", "name": "THPT Tôn Đức Thắng"},
        ]
        catalog = {"mandatorySubjects": [], "electiveSubjects": []}
        backend_get.side_effect = [schools, catalog, schools, catalog]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
            {"academicYear": "2026", "groups": [{"groupCode": "G02", "groupName": "Nhóm G02",
                "groupSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": []}]},
        ]
        question = (
            "Cho em hỏi trong năm học 2026, các môn học lựa chọn của Nhóm môn 2 (mã G02) "
            "trường THPT Trưng Vương và Nhóm 02 trường THPT Tôn Đức Thắng có điểm gì giống và khác nhau?"
        )
        intent = {
            "intent": "subject_group_comparison",
            "high_schools": [school["name"] for school in schools],
            "academic_year": "2026",
            "extracted_filters": {},
            "requested_group_codes": [
                {"code": "G02", "evidence": "mã G02"},
                {"code": "2", "evidence": "Nhóm môn 2"},
            ],
            "requested_school_group_pairs": [],
        }

        response = _compare_subject_groups(intent, "two-school-collision", current_message=question)

        self.assertEqual("success", response.status)
        self.assertEqual(["G02", "G02"], [row["groups"][0]["groupCode"] for row in response.data["comparisons"]])
        self.assertEqual([["MATH"], ["BIOLOGY"]], [
            [subject["code"] for subject in row["groups"][0]["groupSubjects"]]
            for row in response.data["comparisons"]
        ])

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_cross_school_comparison_does_not_use_legacy_effective_subjects_as_group_membership(
        self, backend_get, backend_post
    ):
        schools = [{"code": "TCV", "name": "THPT Trần Cao Vân"},
                   {"code": "TVQN", "name": "THPT Trưng Vương"}]
        backend_get.side_effect = [schools, {"mandatorySubjects": [], "electiveSubjects": []},
                                   schools, {"mandatorySubjects": [], "electiveSubjects": []}]
        backend_post.side_effect = [
            {"academicYear": "2026", "groups": [{
                "groupCode": "G04", "groupName": "G04",
                "subjects": [{"code": "MATH", "name": "Toán"}],
                "mandatorySubjects": [{"code": "MATH", "name": "Toán"}],
                "matchingAdmissionCombinations": [],
            }]},
            {"academicYear": "2026", "groups": [{
                "groupCode": "G03", "groupName": "G03",
                "groupSubjects": [{"code": "MATH", "name": "Toán"}],
                "subjects": [{"code": "MATH", "name": "Toán"}],
                "mandatorySubjects": [], "matchingAdmissionCombinations": [],
            }]},
        ]
        intent = {"intent": "subject_group_comparison", "high_schools": [row["name"] for row in schools],
                  "extracted_filters": {}, "academic_year": "2026", "preferred_subjects": [],
                  "disliked_subjects": [], "self_reported_strengths": [], "confidence_by_subject": [],
                  "grades_by_subject": [], "grades_needing_scale": [],
                  "requested_group_codes": [{"code": "G04"}, {"code": "G03"}]}

        response = _compare_subject_groups(
            intent, "missing-membership",
            current_message="So sánh G04 của THPT Trần Cao Vân với G03 của THPT Trưng Vương.",
        )

        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertEqual([], response.data["shared_subjects"])
        self.assertNotIn("Môn chung: Toán", response.message)
        self.assertIn("chưa có danh sách môn thành phần", response.message)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_comparison_formats_group_subjects_separately_and_omits_unrequested_combinations(
        self, backend_get, backend_post
    ):
        backend_get.side_effect = [
            [{"code": "TCV", "name": "THPT Trần Cao Vân"}],
            {"mandatorySubjects": [], "electiveSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}]},
        ]
        backend_post.return_value = {"academicYear": "2026", "groups": [{
            "groupCode": "G02", "groupName": "Nhóm G02",
            "groupSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}],
            "mandatorySubjects": [{"code": "LITERATURE", "name": "Ngữ văn"}],
            "subjects": [{"code": "BIOLOGY", "name": "Sinh học"},
                         {"code": "LITERATURE", "name": "Ngữ văn"}],
            "matchingAdmissionCombinations": [{
                "combinationDisplay": "Biology, Literature, Math", "publicLabels": [],
                "pathwaysForRequestedProgram": [],
            }],
        }]}
        intent = {
            "intent": "subject_group_advice", "extracted_filters": {"high_school": "THPT Trần Cao Vân"},
            "academic_year": "2026", "preferred_subjects": [{"subject": "Sinh học", "evidence": "thích Sinh học"}],
            "disliked_subjects": [], "self_reported_strengths": [], "confidence_by_subject": [],
            "grades_by_subject": [], "grades_needing_scale": [],
            "requested_group_codes": [{"code": "G02", "evidence": "G02"}],
        }

        response = _compare_subject_groups(
            intent, "group-subject-scope", current_message="Nhóm G02 có môn gì, có hợp sở thích không?",
        )

        self.assertIn("Sinh học", response.message)
        self.assertNotIn("Ngữ văn", response.message.split("Nhóm G02", 1)[1].split("\n", 1)[0])
        self.assertNotIn("Biology, Literature, Math", response.message)
        self.assertIn("Môn bắt buộc/common", response.message)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_self_reported_strength_can_support_tentative_group_match(self, backend_get, backend_post):
        backend_get.side_effect = [
            [{"code": "S01", "name": "THPT A"}],
            {"mandatorySubjects": [], "electiveSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}]},
        ]
        backend_post.return_value = {"academicYear": "2026", "groups": [{
            "groupCode": "G02", "groupName": "Nhóm G02",
            "groupSubjects": [{"code": "BIOLOGY", "name": "Sinh học"}],
            "subjects": [{"code": "BIOLOGY", "name": "Sinh học"}],
        }]}
        response = _compare_subject_groups({
            "intent": "subject_group_advice", "extracted_filters": {"high_school": "THPT A"},
            "academic_year": "2026", "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [{"subject": "Sinh học", "description": "em tự nhận học tốt"}],
            "confidence_by_subject": [], "grades_by_subject": [], "grades_needing_scale": [],
            "requested_group_codes": [],
        }, "strength-match")
        self.assertEqual(["G02"], response.data["tentative_candidate_groups"])
        self.assertIn("tự nhận thấy học tốt", response.message)

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    def test_objective_comparison_does_not_require_student_profile(self, backend_get, backend_post):
        backend_get.side_effect = [
            [{"code": "S01", "name": "THPT A"}],
            {"mandatorySubjects": [], "electiveSubjects": []},
        ]
        backend_post.return_value = {
            "academicYear": "2026",
            "groups": [{
                "groupCode": "01", "groupName": "Nhom 01",
                "groupSubjects": [{"code": "MATH", "name": "Toan"}],
                "subjects": [{"code": "MATH", "name": "Toan"}],
                "matchingAdmissionCombinations": [],
            }],
        }
        intent = {
            "intent": "subject_group_comparison",
            "academic_year": "2026-2027",
            "extracted_filters": {
                "high_school": "THPT A", "admission_year": "2025", "method_code": "100",
                "desired_program": "Computer Science",
            },
            "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [],
            "grades_by_subject": [], "grades_needing_scale": [],
            "requested_group_codes": [],
        }

        response = _compare_subject_groups(intent, "request-objective")

        self.assertEqual("success", response.status)
        self.assertEqual("objective", response.data["comparison_mode"])
        self.assertEqual("complete", response.data["answer_completeness"])
        payload = backend_post.call_args.args[1]
        self.assertEqual("2026-2027", payload["academicYear"])
        self.assertEqual("2025", payload["admissionYear"])
        self.assertEqual("100", payload["methodCode"])
        self.assertEqual("Computer Science", payload["desiredProgramName"])
        self.assertEqual([], payload["preferredSubjectCodes"])
        self.assertEqual([], payload["dislikedSubjectCodes"])
        self.assertIn("Nhom 01", response.message)

    @patch("rag_api.main_api._backend_get")
    def test_school_group_partial_answer_when_public_code_has_no_linked_components(self, backend_get):
        backend_get.return_value = {
            "requestedCode": "A01",
            "labels": [{"code": "A01", "subjectDetails": "Toan, Ly, Anh"}],
        }
        from rag_api.main_api import _lookup_combination_code

        response = _lookup_combination_code("A01", "request-label-only")

        self.assertEqual("success", response.status)
        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertIn("A01", response.message)

    @patch("rag_api.main_api._backend_get")
    def test_subject_catalog_question_returns_only_requested_subject_evidence(self, backend_get):
        backend_get.return_value = {
            "mandatorySubjects": [{"code": "MATH", "name": "Toán"}],
            "electiveSubjects": [
                {"code": "INFORMATICS", "name": "Tin học"},
                {"code": "PHYSICS", "name": "Vật lí"},
            ],
        }
        request = ChatRequest(user_message="Toán là môn bắt buộc hay tự chọn?")
        intent = {"knowledge_subjects": [{"subject": "Toán", "evidence": "Toán"}]}

        response = _answer_knowledge_question(request, intent, "request-3", "knowledge")

        self.assertIn("Toán", response.message)
        self.assertNotIn("Tin học", response.message)
        self.assertEqual(["MATH"], [unit["id"] for unit in response.data["retrieval_units"]])

    def test_advice_without_school_acknowledges_user_signals_and_asks_one_next_question(self):
        response = _advise_without_school({
            "intent": "subject_group_advice",
            "extracted_filters": {},
            "preferred_subjects": [{"subject": "Toán"}],
            "disliked_subjects": [],
            "self_reported_strengths": [],
            "confidence_by_subject": [],
            "grades_by_subject": [],
            "grades_needing_scale": [],
        }, "request-4")

        self.assertEqual("incomplete", response.status)
        self.assertIn("Môn em thích: Toán", response.message)
        self.assertIn("trường nào", response.message)
        self.assertNotIn("nhóm phù hợp nhất", response.message)

    def test_numeric_only_advice_reports_self_report_and_asks_values_without_repeating_metrics(self):
        response = _advise_without_school({
            "intent": "subject_group_advice", "extracted_filters": {},
            "preferred_subjects": [], "disliked_subjects": [], "self_reported_strengths": [],
            "confidence_by_subject": [{"subject": "Toan", "value": 4}],
            "grades_by_subject": [{"subject": "Vat li", "value": 8, "scale": 10}],
            "grades_needing_scale": [],
        }, "request-numeric")
        self.assertIn("8/10", response.message)
        self.assertIn("Em mu", response.message)
        self.assertNotIn("4/5?", response.message)

    @patch("rag_api.main_api._lookup_school_groups")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_mixed_group_lookup_without_preferences_returns_facts_and_targeted_question(
        self, parser_mock, _retriever_mock, _generator_mock, lookup_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "high_schools": ["THPT A"],
            "preferred_subjects": [], "disliked_subjects": [],
            "self_reported_strengths": [], "confidence_by_subject": [],
            "grades_by_subject": [], "grades_needing_scale": [],
        }
        lookup_mock.return_value = _chat_result(
            "success", "THPT A có nhóm G01 gồm Toán, Vật lí.", "lookup-mixed",
            "school_group_lookup", {"evidence": [{"groupCode": "G01"}],
                                     "evidence_source": "backend_subject_group_api"},
        )

        with patch("rag_api.main_api._backend_get", return_value=[{"code": "A", "name": "THPT A"}]):
            response = chat_endpoint(ChatRequest(user_message=(
                "THPT A có những nhóm môn nào, nhóm nào hợp với em?")))

        self.assertIn("\u0110\u1ec3 \u0111\u01b0a ra", response.message)
        self.assertEqual(["school_group_lookup", "subject_group_advice"], response.data["requested_tasks"])
        self.assertEqual(["subject_group_advice"], response.data["unanswered_tasks"])
        self.assertEqual("partial", response.data["answer_completeness"])
        lookup_mock.assert_called_once()

    @patch("rag_api.main_api._compare_subject_groups")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_mixed_group_lookup_with_preferences_returns_both_fact_and_advice_tasks(
        self, parser_mock, _retriever_mock, _generator_mock, compare_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "high_schools": ["THPT A"],
            "preferred_subjects": [{"subject": "Toán", "evidence": "em thích Toán"}],
        }
        compare_mock.return_value = _chat_result(
            "success", "Nhóm G01 có Toán; em đã nêu thích Toán nên có điểm tương đồng.",
            "advice-mixed", "subject_group_advice",
            {"evidence": {"groups": [{"groupCode": "G01", "groupSubjects": [
                {"code": "MATH", "name": "Toán"}]}]},
             "evidence_source": "backend_subject_group_comparison_api",
             "answer_completeness": "complete", "tentative_candidate_groups": ["G01"]},
        )

        with patch("rag_api.main_api._backend_get", return_value=[{"code": "A", "name": "THPT A"}]):
            response = chat_endpoint(ChatRequest(user_message=(
                "THPT A có những nhóm môn nào, em thích Toán thì nên cân nhắc nhóm nào?")))

        self.assertIn("G01", response.message)
        self.assertEqual(["school_group_lookup", "subject_group_advice"], response.data["requested_tasks"])
        self.assertEqual(["school_group_lookup", "subject_group_advice"], response.data["answered_tasks"])
        self.assertNotIn("đủ điều kiện", response.message.casefold())
        compare_mock.assert_called_once()

    @patch("rag_api.main_api._lookup_school_groups")
    @patch("rag_api.main_api._compare_subject_groups")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_mixed_lookup_keeps_facts_when_advice_evidence_needs_clarification(
        self, parser_mock, _retriever_mock, _generator_mock, compare_mock, lookup_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "high_schools": ["THPT A"],
            "preferred_subjects": [{"subject": "Môn lạ", "evidence": "em thích môn lạ"}],
        }
        compare_mock.return_value = _chat_result(
            "incomplete", "Mình chưa xác minh được môn em nêu.", "clarify-advice",
            "subject_group_advice",
        )
        lookup_mock.return_value = _chat_result(
            "success", "THPT A có nhóm G01 gồm Toán.", "lookup-after-clarify",
            "school_group_lookup", {"evidence": [{"groupCode": "G01"}],
                                     "evidence_source": "backend_subject_group_api"},
        )

        with patch("rag_api.main_api._backend_get", return_value=[{"code": "A", "name": "THPT A"}]):
            response = chat_endpoint(ChatRequest(user_message=(
                "THPT A có những nhóm môn nào, em thích môn lạ thì nên cân nhắc nhóm nào?")))

        self.assertEqual("success", response.status)
        self.assertIn("G01", response.message)
        self.assertIn("chưa xác minh", response.message)
        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertEqual(["subject_group_advice"], response.data["unanswered_tasks"])

class ChatEndpointFailureTests(unittest.TestCase):
    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_mixed_lookup_real_formatter_names_verified_preference_candidates_without_ranking(
        self, parser_mock, _retriever_mock, _generator_mock, backend_get_mock, backend_post_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "high_schools": ["THPT A"],
            "academic_year": "2026",
            "preferred_subjects": [{"subject": "Toán", "evidence": "em thích Toán"}],
            "disliked_subjects": [], "self_reported_strengths": [],
            "confidence_by_subject": [], "grades_by_subject": [], "grades_needing_scale": [],
        }
        backend_get_mock.side_effect = [
            [{"code": "A", "name": "THPT A"}],
            {"mandatorySubjects": [], "electiveSubjects": [{"code": "MATH", "name": "Toán"}]},
        ]
        backend_post_mock.return_value = {
            "academicYear": "2026",
            "groups": [
                {"groupCode": "G01", "groupName": "Nhóm G01",
                 "groupSubjects": [{"code": "MATH", "name": "Toán"}],
                 "matchedPreferredSubjects": [{"code": "MATH", "name": "Toán"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
                {"groupCode": "G02", "groupName": "Nhóm G02",
                 "groupSubjects": [{"code": "MATH", "name": "Toán"},
                                    {"code": "LITERATURE", "name": "Ngữ văn"}],
                 "matchedPreferredSubjects": [{"code": "MATH", "name": "Toán"}],
                 "mandatorySubjects": [], "matchingAdmissionCombinations": []},
            ],
        }

        response = chat_endpoint(ChatRequest(user_message=(
            "THPT A có những nhóm môn nào, em thích Toán thì nhóm nào hợp?")))

        self.assertEqual("success", response.status)
        self.assertIn("G01", response.message)
        self.assertIn("G02", response.message)
        self.assertIn("ứng viên tạm thời", response.message)
        self.assertIn("không xếp hạng", response.message)
        self.assertIn("x\u00e1c nh\u1eadn \u0111\u1ee7 \u0111i\u1ec1u ki\u1ec7n", response.message.casefold())
        self.assertIn("kh\u00f4ng b\u1ea3o \u0111\u1ea3m ph\u00f9 h\u1ee3p", response.message.casefold())
        self.assertEqual(["G01", "G02"], response.data["tentative_candidate_groups"])
        backend_post_mock.assert_called_once()

    @patch("rag_api.main_api._backend_post")
    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_mixed_lookup_without_preference_match_keeps_facts_and_asks_for_preference_evidence(
        self, parser_mock, _retriever_mock, _generator_mock, backend_get_mock, backend_post_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {"high_school": "THPT A"},
            "high_schools": ["THPT A"],
            "academic_year": "2026",
            "preferred_subjects": [{"subject": "Toán", "evidence": "em thích Toán"}],
            "disliked_subjects": [], "self_reported_strengths": [],
            "confidence_by_subject": [], "grades_by_subject": [], "grades_needing_scale": [],
        }
        backend_get_mock.side_effect = [
            [{"code": "A", "name": "THPT A"}],
            {"mandatorySubjects": [], "electiveSubjects": [{"code": "MATH", "name": "Toán"}]},
        ]
        backend_post_mock.return_value = {
            "academicYear": "2026",
            "groups": [{
                "groupCode": "G01", "groupName": "Nhóm G01",
                "groupSubjects": [{"code": "PHYSICS", "name": "Vật lí"}],
                "matchedPreferredSubjects": [], "mandatorySubjects": [],
                "matchingAdmissionCombinations": [],
            }],
        }

        response = chat_endpoint(ChatRequest(user_message=(
            "THPT A có những nhóm môn nào, em thích Toán thì nhóm nào hợp?")))

        self.assertEqual("success", response.status)
        self.assertIn("G01", response.message)
        self.assertIn("V\u1eadt l\u00ed", response.message)
        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertEqual(["school_group_lookup"], response.data["answered_tasks"])
        self.assertEqual(["subject_group_advice"], response.data["unanswered_tasks"])
        self.assertEqual([], response.data["tentative_candidate_groups"])
        self.assertIn("Em mu", response.message)
        self.assertNotIn("G01 l\u00e0 m\u1ed9t \u1ee9ng vi\u00ean", response.message)

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_pathway_chat_resolves_explicit_school_from_catalog_before_graph_retrieval(
        self, parser_mock, retriever_mock, generator_mock, backend_get_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {"desired_program": "Business Administration"},
            "high_schools": [],
        }
        parser_mock.is_ready_for_db.return_value = True
        backend_get_mock.return_value = [{
            "code": "TVQN",
            "name": "Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng",
        }]
        retriever_mock.retrieve_context.return_value = [{"evidence_id": "path:curated-02"}]
        retriever_mock.format_for_llm.return_value = "verified school-group and admission path"
        generator_mock.generate_answer.return_value = "One verified example."
        question = (
            "H\u00e3y t\u00ecm m\u1ed9t nh\u00f3m m\u00f4n c\u1ee7a THPT Tr\u01b0ng V\u01b0\u01a1ng "
            "t\u1ea1i Quy Nh\u01a1n, c\u00f3 Sinh h\u1ecdc v\u00e0 li\u00ean k\u1ebft v\u1edbi ng\u00e0nh K\u1ef9 thu\u1eadt v\u1eadt l\u00fd."
        )

        response = chat_endpoint(ChatRequest(user_message=question))

        self.assertEqual("success", response.status)
        self.assertEqual("TVQN", retriever_mock.retrieve_context.call_args.kwargs["filters"]["high_school_code"])
        self.assertEqual("Tr\u01b0\u1eddng THPT Tr\u01b0ng V\u01b0\u01a1ng",
                         retriever_mock.retrieve_context.call_args.kwargs["filters"]["high_school"])
        backend_get_mock.assert_called_once_with("/api/v1/high-schools")
        generator_mock.generate_answer.assert_called_once()

    @patch("rag_api.main_api._backend_get")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_relational_knowledge_misclassification_is_routed_to_graph_pathway_lookup(
        self, parser_mock, retriever_mock, generator_mock, backend_get_mock
    ):
        backend_get_mock.return_value = [{"code": "TCV", "name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"}]
        parser_mock.parse_intent.return_value = {
            "intent": "knowledge",
            "extracted_filters": {"high_school": "THPT Trần Cao Vân",
                                  "desired_program": "Quản trị kinh doanh",
                                  "university": "Đại học Quy Nhơn"},
            "academic_year": "2026",
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = [{"evidence_id": "path:one"}]
        retriever_mock.format_for_llm.return_value = "verified relationship context"
        generator_mock.generate_answer.return_value = "Một ví dụ có bằng chứng."
        question = (
            "Hãy nêu một ví dụ về nhóm môn của Trường THPT Trần Cao Vân có Giáo dục kinh tế và pháp luật, "
            "rồi chỉ ra một tổ hợp được ngành Quản trị kinh doanh của Đại học Quy Nhơn chấp nhận năm 2026."
        )

        response = chat_endpoint(ChatRequest(user_message=question))

        self.assertEqual("success", response.status)
        self.assertEqual("pathway_lookup", response.data["intent"])
        self.assertEqual("graph_retrieval_generation",
                         response.data["actual_execution_route"]["execution_kind"])
        retriever_mock.retrieve_context.assert_called_once()
        generator_mock.generate_answer.assert_called_once()

    @patch("rag_api.main_api._lookup_combination")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_combination_lookup_uses_normalized_legacy_chat_history(
        self, parser_mock, _retriever_mock, _generator_mock, lookup_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "combination_lookup", "extracted_filters": {}
        }
        lookup_mock.return_value = _chat_result(
            "incomplete", "Cần thêm thông tin", "request-history", "combination_lookup"
        )
        history = [
            {"role": "user", "content": "Tổ hợp A01 gồm môn nào?"},
            {"role": "assistant", "content": "A01 gồm Toán, Vật lí, Tiếng Anh."},
        ]

        chat_endpoint(ChatRequest(user_message="Mã đó dùng được ở đâu?", chat_history=history))

        self.assertEqual(history, lookup_mock.call_args.kwargs["context"])

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_graph_answer_is_marked_partial_when_required_path_dependency_is_missing(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "Computer Science"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = [{"program_code": "P1"}]
        retriever_mock.attach_evidence_provenance.side_effect = lambda rows, filters=None: [
            {**rows[0], "evidence_id": "graph:P1", "evidence_dependency_validation": {
                "status": "incomplete", "missing_relationship_ids": ["requires-subject:MATH"],
                "aptitude_coverage": "not_required",
            }}
        ]
        retriever_mock.format_for_llm.return_value = "Partial graph evidence"
        generator_mock.generate_answer.return_value = "Only the verified facts are available."

        response = chat_endpoint(ChatRequest(user_message="Tìm đường ngành Computer Science"))

        self.assertEqual("answer", response.data["response_mode"])
        self.assertEqual("partial", response.data["answer_completeness"])
        self.assertEqual(["graph:P1"], response.data["unresolved_path_dependencies"])
        self.assertEqual(2, response.data["graph_path_validation"]["schema_version"])
        self.assertEqual("directed_edge_set", response.data["graph_path_validation"]["path_semantics"])
        self.assertFalse(response.data["graph_path_validation"]["complete"])

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_empty_retrieval_is_returned_as_no_data(self, parser_mock, retriever_mock, _generator_mock):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "y khoa"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = []

        response = chat_endpoint(ChatRequest(user_message="Có ngành y khoa không?"))

        self.assertEqual("no_data", response.status)
        self.assertEqual(0, response.data["retrieved_count"])
        self.assertEqual([], response.data["evidence"])
        self.assertFalse(response.data["evidence_truncated"])
        self.assertEqual({"schema_version": 2, "source": "neo4j_graph",
                          "path_semantics": "directed_edge_set",
                          "complete": False, "records": []},
                         response.data["graph_path_validation"])
        self.assertEqual("graph_retrieval_only", response.data["actual_execution_route"]["execution_kind"])
        self.assertFalse(response.data["actual_execution_route"]["generation_executed"])

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_neo4j_failure_is_returned_as_service_unavailable(self, parser_mock, retriever_mock, _generator_mock):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "y khoa"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.side_effect = RetrievalUnavailableError("neo4j down")

        with self.assertRaises(HTTPException) as raised:
            chat_endpoint(ChatRequest(user_message="Có ngành y khoa không?"))

        self.assertEqual(503, raised.exception.status_code)
        self.assertEqual("retrieval", raised.exception.headers["X-Error-Stage"])
        self.assertEqual("RetrievalUnavailableError", raised.exception.headers["X-Error-Type"])
        self.assertTrue(raised.exception.headers["X-Request-ID"])

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_generation_timeout_is_exposed_as_sanitized_http_metadata(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "y khoa"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = [{"evidence_id": "graph:one"}]
        retriever_mock.format_for_llm.return_value = "verified evidence"
        generator_mock.generate_answer.side_effect = LLMServiceUnavailable(
            "private provider payload", failure_code="timeout")

        with self.assertRaises(HTTPException) as raised:
            chat_endpoint(ChatRequest(user_message="Find a program"))

        self.assertEqual(503, raised.exception.status_code)
        self.assertEqual("generation", raised.exception.headers["X-Error-Stage"])
        self.assertEqual("timeout", raised.exception.headers["X-Error-Code"])
        self.assertNotIn("private provider payload", str(raised.exception.headers))

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_rag_response_returns_at_most_twenty_records_and_marks_truncation(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "y khoa"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = [{"id": index} for index in range(21)]
        retriever_mock.attach_evidence_provenance.side_effect = lambda rows, filters=None: [
            {**row, "evidence_id": f"graph:{row['id']}"} for row in rows
        ]
        retriever_mock.format_for_llm.return_value = "complete formatted retrieved context"
        generator_mock.generate_answer.return_value = "Answer grounded in retrieved context"

        response = chat_endpoint(ChatRequest(user_message="Có ngành y khoa không?"))

        self.assertEqual("success", response.status)
        self.assertEqual(20, response.data["retrieved_count"])
        self.assertEqual(20, len(response.data["evidence"]))
        self.assertTrue(response.data["evidence_truncated"])
        self.assertEqual([f"graph:{index}" for index in range(20)], response.data["context_evidence_ids"])
        self.assertEqual(["graph:20"], response.data["omitted_evidence_ids"])
        self.assertEqual("complete formatted retrieved context", response.data["retrieved_context"])
        self.assertTrue(response.data["generation_context_exact"])
        self.assertTrue(response.data["actual_execution_route"]["generation_executed"])
        self.assertEqual(
            hashlib.sha256(b"complete formatted retrieved context").hexdigest(),
            response.data["generation_context_sha256"],
        )
        self.assertNotIn("generation_context", response.data)
        self.assertEqual(
            "complete formatted retrieved context",
            generator_mock.generate_answer.call_args.kwargs["graph_context"],
        )
        self.assertEqual(21, retriever_mock.retrieve_context.call_args.kwargs["top_k"])

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_generation_context_digest_matches_stripped_context_passed_to_generator(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "y khoa"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = [{"evidence_id": "graph:one"}]
        retriever_mock.format_for_llm.return_value = " \n exact evidence context \t "
        generator_mock.generate_answer.return_value = "Grounded answer"

        response = chat_endpoint(ChatRequest(user_message="Tìm hiểu ngành y khoa"))

        expected_context = "exact evidence context"
        self.assertEqual(expected_context, generator_mock.generate_answer.call_args.kwargs["graph_context"])
        self.assertEqual(expected_context, response.data["retrieved_context"])
        self.assertEqual(hashlib.sha256(expected_context.encode("utf-8")).hexdigest(),
                         response.data["generation_context_sha256"])
        self.assertIn("graph_path_validation", response.data)

    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_vietnamese_generation_context_hash_matches_exact_utf8_generator_input(
        self, parser_mock, retriever_mock, generator_mock
    ):
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup", "extracted_filters": {"keyword": "địa lý"}
        }
        parser_mock.is_ready_for_db.return_value = True
        retriever_mock.retrieve_context.return_value = [{"evidence_id": "graph:vietnamese-context"}]
        retriever_mock.format_for_llm.return_value = " \nBằng chứng: Hóa học, Địa lí, năm học 2026. \t"
        generator_mock.generate_answer.return_value = "Môn được ghi nhận là Hóa học."

        response = chat_endpoint(ChatRequest(user_message="Tra cứu bằng chứng địa lý"))

        exact_context = "Bằng chứng: Hóa học, Địa lí, năm học 2026."
        generator_input = generator_mock.generate_answer.call_args.kwargs["graph_context"]
        self.assertEqual(exact_context, generator_input)
        self.assertEqual(hashlib.sha256(exact_context.encode("utf-8")).hexdigest(),
                         response.data["generation_context_sha256"])
        self.assertTrue(response.data["generation_context_exact"])

    @patch("rag_api.main_api.logger")
    @patch("rag_api.main_api.generator")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.parser")
    def test_parse_failure_log_has_stage_duration_and_redacted_error(
        self, parser_mock, _retriever_mock, _generator_mock, logger_mock
    ):
        parser_mock.parse_intent.side_effect = ValueError("secret-bearing raw provider response")

        with self.assertRaises(HTTPException) as raised:
            chat_endpoint(ChatRequest(user_message="safe question"))

        self.assertTrue(raised.exception.headers["X-Request-ID"])
        self.assertEqual("intent", raised.exception.headers["X-Error-Stage"])
        self.assertEqual("ValueError", raised.exception.headers["X-Error-Type"])
        self.assertNotIn("secret-bearing raw provider response", str(raised.exception.headers))

        rendered_calls = " ".join(str(call) for call in logger_mock.method_calls)
        self.assertIn("stage=intent", rendered_calls)
        self.assertIn("error_type", rendered_calls)
        self.assertIn("ValueError", rendered_calls)
        self.assertIn("duration_ms", rendered_calls)
        self.assertNotIn("secret-bearing raw provider response", rendered_calls)

    @patch("rag_api.main_api.parser")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.generator")
    def test_chat_endpoint_persists_sanitized_correlation_for_diagnostic_request(
        self, _generator_mock, _retriever_mock, parser_mock,
    ):
        run_id = "21c1c551-ef18-4f99-a20e-d9256ad0b643"
        correlation_id = "92119a30-1a1d-49f5-a152-a775bb1b96ee"
        parser_mock.parse_intent.return_value = {"intent": "greeting", "extracted_filters": {}}
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "trace.jsonl")
            with patch.dict(os.environ, {"RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                                         "RAG_DIAGNOSTIC_TRACE_FILE": path}):
                response = chat_endpoint(
                    ChatRequest(user_message="Hello private phrase"),
                    diagnostic_run_id=run_id, diagnostic_correlation_id=correlation_id,
                )
            with open(path, encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream.read().splitlines()]
        self.assertEqual(correlation_id, response.data["diagnostic_correlation_id"])
        self.assertEqual({run_id}, {row["run_id"] for row in rows})
        self.assertEqual({correlation_id}, {row["correlation_id"] for row in rows})
        self.assertNotIn("Hello private phrase", json.dumps(rows))


class LLMProviderTests(unittest.TestCase):
    def test_intent_and_generation_calls_carry_only_internal_proxy_run_attribution(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace
        run_id = "12345678-1234-4234-8234-123456789abc"
        request_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        token = "synthetic-internal-token"
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
                "RAG_DIAGNOSTIC_RUN_ID": run_id,
                "RAG_DIAGNOSTIC_PROXY_TOKEN": token,
                "RAG_DIAGNOSTIC_PROXY_BASE_URL": "http://budget-proxy:20129/v1",
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "trace.jsonl"),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
                "RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_LIMIT": "2",
                "RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_FILE": str(Path(directory) / "attempts.jsonl"),
            }
            with patch.dict(os.environ, env):
                responses = [
                    _llm_response(json.dumps({
                        "intent": "greeting", "extracted_filters": {}, "reply_to_user": "Hello",
                    })),
                    _llm_response("Grounded answer"),
                ]
                with patch("requests.post", side_effect=responses) as provider_post:
                    with DiagnosticStageTrace(run_id, request_id, request_id):
                        IntentParser(api_base="http://budget-proxy:20129/v1", api_key="test-key",
                                     model="test-model").parse_intent("hello", request_id=request_id)
                        RAGGenerator(api_base="http://budget-proxy:20129/v1", api_key="test-key",
                                     model="test-model").generate_answer("question", "evidence",
                                                                          request_id=request_id)
        for stage, call in zip(("intent", "generation"), provider_post.call_args_list):
            headers = call.kwargs["headers"]
            self.assertEqual(run_id, headers["X-Evaluator-Run-ID"])
            self.assertEqual(token, headers["X-Diagnostic-Proxy-Token"])
            self.assertEqual(stage, headers["X-Evaluator-Stage"])
            self.assertEqual(request_id, headers["X-Evaluator-Request-ID"])
            self.assertEqual("http://budget-proxy:20129/v1/chat/completions", call.args[0])

    def test_strict_run_mode_rejects_direct_provider_route_before_post(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace
        run_id = "12345678-1234-4234-8234-123456789abc"
        request_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
            "RAG_DIAGNOSTIC_RUN_ID": run_id,
            "RAG_DIAGNOSTIC_PROXY_TOKEN": "synthetic-internal-token",
            "RAG_DIAGNOSTIC_PROXY_BASE_URL": "http://budget-proxy:20129/v1",
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "trace.jsonl"),
            "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
        }), patch("rag_api.llm.intent_parser.requests.post") as post:
            with DiagnosticStageTrace(run_id, request_id, request_id):
                with self.assertRaises(LLMServiceUnavailable):
                    IntentParser(api_base="https://provider.example/v1", api_key="test-key",
                                 model="test-model").parse_intent("hello", request_id=request_id)
            post.assert_not_called()

    @patch("rag_api.main_api.parser")
    @patch("rag_api.main_api.retriever")
    @patch("rag_api.main_api.generator")
    def test_intent_timeout_is_attributed_and_never_becomes_empty_graph_retrieval(
        self, _generator, retriever_mock, parser_mock
    ):
        parser_mock.parse_intent.side_effect = LLMServiceUnavailable(
            "sanitized timeout", failure_code="timeout"
        )

        with self.assertRaises(HTTPException) as raised:
            chat_endpoint(ChatRequest(user_message="Trường THPT có nhóm môn nào?"))

        self.assertEqual(503, raised.exception.status_code)
        self.assertEqual("intent", raised.exception.headers["X-Error-Stage"])
        self.assertEqual("timeout", raised.exception.headers["X-Error-Code"])
        retriever_mock.retrieve_context.assert_not_called()

    @patch("rag_api.llm.intent_parser.requests.post")
    def test_intent_parser_preserves_upstream_429_and_makes_only_one_completion_attempt(self, post_mock):
        response = Mock(status_code=429)
        error = requests.HTTPError("private provider body", response=response)
        response.raise_for_status.side_effect = error
        post_mock.return_value = response
        parser = IntentParser(api_base="http://router.test/v1", api_key="unit-test-key", model="test-model")
        with self.assertRaises(LLMServiceUnavailable) as raised:
            parser.parse_intent("question", request_id="request-safe")
        self.assertEqual(429, raised.exception.upstream_status_code)
        self.assertEqual(1, post_mock.call_count)
        self.assertFalse(post_mock.call_args.kwargs.get("allow_redirects", True))
        self.assertNotIn("private provider body", str(raised.exception))

    @patch("rag_api.llm.intent_parser.requests.post", side_effect=requests.Timeout("private timeout"))
    def test_intent_parser_timeout_uses_bounded_parser_budget_and_is_sanitized(self, post_mock):
        parser = IntentParser(api_base="http://router.test/v1", api_key="unit-test-key",
                              model="test-model", timeout=40.0)
        with self.assertRaises(LLMServiceUnavailable) as raised:
            parser.parse_intent("question", request_id="request-safe")
        self.assertEqual("timeout", raised.exception.failure_code)
        self.assertEqual(40.0, post_mock.call_args.kwargs["timeout"])
        self.assertEqual(1, post_mock.call_count)
        self.assertNotIn("private timeout", str(raised.exception))

    @patch("rag_api.llm.intent_parser.requests.post", side_effect=requests.ConnectTimeout("private connect detail"))
    def test_intent_connect_timeout_keeps_transport_class_without_exposing_details(self, post_mock):
        parser = IntentParser(api_base="http://router.test/v1", api_key="unit-test-key",
                              model="test-model", timeout=40.0)
        with self.assertRaises(LLMServiceUnavailable) as raised:
            parser.parse_intent("question", request_id="request-safe")
        self.assertEqual("connect_timeout", raised.exception.failure_code)
        self.assertIsInstance(raised.exception.__cause__, requests.ConnectTimeout)
        self.assertNotIn("private connect detail", str(raised.exception))
        self.assertEqual("connect_timeout", _stage_error_headers("request-safe", "intent", raised.exception)["X-Error-Code"])
        self.assertEqual(1, post_mock.call_count)

    @patch("rag_api.llm.intent_parser.requests.post", side_effect=requests.ReadTimeout("private read detail"))
    def test_intent_read_timeout_keeps_transport_class_without_exposing_details(self, post_mock):
        parser = IntentParser(api_base="http://router.test/v1", api_key="unit-test-key",
                              model="test-model", timeout=40.0)
        with self.assertRaises(LLMServiceUnavailable) as raised:
            parser.parse_intent("question", request_id="request-safe")
        self.assertEqual("read_timeout", raised.exception.failure_code)
        self.assertIsInstance(raised.exception.__cause__, requests.ReadTimeout)
        self.assertNotIn("private read detail", str(raised.exception))
        self.assertEqual("read_timeout", _stage_error_headers("request-safe", "intent", raised.exception)["X-Error-Code"])
        self.assertEqual(1, post_mock.call_count)

    @patch("rag_api.llm.llm_client.requests.post")
    def test_generator_preserves_upstream_429_and_makes_only_one_completion_attempt(self, post_mock):
        response = Mock(status_code=429)
        error = requests.HTTPError("private provider body", response=response)
        response.raise_for_status.side_effect = error
        post_mock.return_value = response
        generator = RAGGenerator(api_base="http://router.test/v1", api_key="unit-test-key", model="test-model")
        with self.assertRaises(LLMServiceUnavailable) as raised:
            generator.generate_answer("question", "evidence", request_id="request-safe")
        self.assertEqual(429, raised.exception.upstream_status_code)
        self.assertEqual(1, post_mock.call_count)
        self.assertFalse(post_mock.call_args.kwargs.get("allow_redirects", True))

    def test_upstream_status_is_exposed_only_as_a_sanitized_numeric_header(self):
        exc = LLMServiceUnavailable("private provider body", upstream_status_code=429)
        headers = _stage_error_headers("request-safe", "intent", exc)
        self.assertEqual("429", headers["X-Upstream-Status"])
        self.assertNotIn("private provider body", str(headers))

    @patch("rag_api.llm.llm_client.requests.post", side_effect=requests.Timeout("private timeout detail"))
    def test_generator_timeout_has_sanitized_failure_code(self, _post_mock):
        with self.assertRaises(LLMServiceUnavailable) as raised:
            RAGGenerator(api_base="http://llm.test/v1", api_key="unit-test-key", model="test-model").generate_answer(
                "question", "evidence")
        self.assertEqual("timeout", raised.exception.failure_code)
        self.assertNotIn("private timeout detail", str(raised.exception))

    @patch("rag_api.llm.llm_client.requests.post")
    @patch("rag_api.llm.llm_client.settings")
    def test_generator_uses_configured_model_unchanged(self, settings_mock, post_mock):
        settings_mock.LLM_API_BASE = "http://llm.test/v1"
        settings_mock.LLM_API_KEY = "environment-key"
        settings_mock.LLM_MODEL = "ag/claude-sonnet-4-6(high)"
        settings_mock.LLM_TIMEOUT = 8
        post_mock.return_value = _llm_response("Grounded answer")

        answer = RAGGenerator().generate_answer("question", "evidence")

        self.assertEqual("Grounded answer", answer)
        self.assertEqual("ag/claude-sonnet-4-6(high)", post_mock.call_args.kwargs["json"]["model"])

    @patch("rag_api.llm.llm_client.requests.post", side_effect=requests.ConnectionError("offline"))
    def test_provider_failure_does_not_return_mock_answer(self, _post_mock):
        generator = RAGGenerator(api_base="http://llm.test/v1", api_key="unit-test-key", model="test-model")

        with self.assertRaises(LLMServiceUnavailable) as raised:
            generator.generate_answer("question", "evidence")
        self.assertEqual("provider_failure", raised.exception.failure_code)


def _llm_response(content):
    response = Mock()
    response.headers = {"content-type": "application/json"}
    response.json.return_value = {
        "choices": [{"message": {"content": content if isinstance(content, str) else json.dumps(content)}}]
    }
    response.raise_for_status.return_value = None
    return response


class ApprovedPathwayClarificationTests(unittest.TestCase):
    @patch("rag_api.main_api._backend_get")
    def test_explicit_group_followup_inherits_school_and_academic_year_from_prior_user_turn(self, backend_get):
        parser_mock = Mock()
        parser_mock.parse_intent.return_value = {
            "intent": "school_group_lookup",
            "extracted_filters": {},
            "high_schools": [],
            "requested_group_codes": [{"code": "G04", "evidence": "G04"}],
            "academic_year": None,
        }
        backend_get.side_effect = [
            [{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}],
            ["2026"],
            [{"groupCode": "G04", "groupName": "Nhóm G04",
              "subjects": [{"name": "Vật lí"}, {"name": "Tin học"}]}],
        ]
        history = [
            {"role": "user", "content":
             "Trường THPT Trần Cao Vân có những nhóm môn nào cho năm học 2026?"},
            {"role": "assistant", "content":
             "Nhóm G04 có môn Vật lí và Tin học."},
        ]
        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", Mock()),
            patch("rag_api.main_api.generator", Mock()),
        ):
            response = chat_endpoint(ChatRequest(
                user_message="Riêng nhóm G04 thì có môn Vật lí không?",
                context=history,
            ))

        self.assertEqual("success", response.status)
        self.assertEqual("TCV", response.data["school"]["code"])
        self.assertEqual("2026", response.data["academic_year"])
        self.assertEqual(["G04"], [item["groupCode"] for item in response.data["evidence"]])
        self.assertEqual(
            "/api/v1/high-schools/TCV/subject-groups?academicYear=2026",
            backend_get.call_args_list[-1].args[0],
        )

    def test_subject_overlap_does_not_require_admission_year_for_non_guarantee_answer(self):
        parser_mock = Mock()
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {
                "high_school": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n",
                "university": "Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n",
                "desired_program": "S\u01b0 ph\u1ea1m Tin h\u1ecdc",
            },
            "requested_group_codes": [{"code": "G04", "evidence": "Nh\u00f3m 4 (G04)"}],
            "academic_year": None,
        }
        retriever_mock = Mock()
        retriever_mock.retrieve_context.return_value = []
        generator_mock = Mock()
        question = (
            "Vi\u1ec7c c\u00f3 m\u1ed9t m\u00f4n chung gi\u1eefa Nh\u00f3m 4 (G04) c\u1ee7a THPT Tr\u1ea7n Cao V\u00e2n v\u00e0 t\u1ed5 h\u1ee3p x\u00e9t tuy\u1ec3n "
            "ng\u00e0nh S\u01b0 ph\u1ea1m Tin h\u1ecdc c\u1ee7a \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n c\u00f3 \u0111\u1ea3m b\u1ea3o em \u0111\u1ee7 \u0111i\u1ec1u ki\u1ec7n x\u00e9t tuy\u1ec3n "
            "ho\u1eb7c tr\u00fang tuy\u1ec3n kh\u00f4ng? V\u00ec sao?"
        )
        authentic_history = [
            {"role": "user", "content": "N\u1ebfu em ch\u1ecdn h\u1ecdc Nh\u00f3m 4 t\u1ea1i Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n th\u00ec c\u00f3 \u0111\u01b0\u1ee3c h\u1ecdc m\u00f4n V\u1eadt l\u00ed \u0111\u1ec3 x\u00e9t tuy\u1ec3n v\u00e0o ng\u00e0nh S\u01b0 ph\u1ea1m Tin h\u1ecdc c\u1ee7a Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n theo t\u1ed5 h\u1ee3p Ti\u1ebfng Anh, To\u00e1n, V\u1eadt l\u00ed kh\u00f4ng?"},
            {"role": "assistant", "content": "Nh\u00f3m m\u00f4n c\u00f3 \u0111\u1ed9 ph\u1ee7 m\u00f4n ph\u00f9 h\u1ee3p nh\u01b0ng kh\u00f4ng b\u1ea3o \u0111\u1ea3m \u0111i\u1ec1u ki\u1ec7n ho\u1eb7c k\u1ebft qu\u1ea3 tr\u00fang tuy\u1ec3n."},
        ]

        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", generator_mock),
            patch("rag_api.main_api._resolve_explicit_school_filter", side_effect=lambda result, *_args: result),
        ):
            response = chat_endpoint(ChatRequest(user_message=question, context=authentic_history))

        self.assertEqual("success", response.status)
        self.assertEqual("answer", response.data["response_mode"])
        self.assertEqual("not_guaranteed_by_subject_overlap", response.data["eligibility_conclusion"])
        self.assertFalse(response.data["actual_execution_route"]["retrieval_executed"])
        self.assertEqual("not_executed", response.data["actual_execution_route"]["execution_kind"])
        self.assertIn("m\u1ed9t m\u00f4n chung", response.message)
        self.assertIn("kh\u00f4ng b\u1ea3o \u0111\u1ea3m tr\u00fang tuy\u1ec3n", response.message)
        self.assertIn("n\u0103m tuy\u1ec3n sinh", response.message)
        retriever_mock.retrieve_context.assert_not_called()
        generator_mock.generate_answer.assert_not_called()

    def test_eligibility_question_without_school_or_admission_year_clarifies_before_graph_or_generation(self):
        parser_mock = Mock()
        parser_mock.parse_intent.return_value = {
            "intent": "pathway_lookup",
            "extracted_filters": {
                "university": "Trường Đại học Quy Nhơn",
                "desired_program": "Sư phạm Lịch sử - Địa lý",
            },
            "requested_group_codes": [{"code": "G03", "evidence": "Nhóm 3"}],
            "academic_year": "2026",
        }
        retriever_mock = Mock()
        retriever_mock.retrieve_context.return_value = []
        generator_mock = Mock()
        question = (
            "Em dự định sau này xét tuyển vào ngành Sư phạm Lịch sử - Địa lý của "
            "Trường Đại học Quy Nhơn. Nếu lên lớp 10 em đăng ký học Nhóm 3 thì có "
            "đáp ứng được tổ hợp môn xét tuyển không ạ?"
        )

        with (
            patch("rag_api.main_api.parser", parser_mock),
            patch("rag_api.main_api.retriever", retriever_mock),
            patch("rag_api.main_api.generator", generator_mock),
            patch("rag_api.main_api._backend_get") as backend_get,
        ):
            response = chat_endpoint(ChatRequest(user_message=question))

        self.assertEqual("incomplete", response.status)
        self.assertEqual("clarification", response.data["response_mode"])
        self.assertIn("trường THPT", response.message)
        self.assertIn("năm tuyển sinh", response.message)
        self.assertNotIn("Đại học Quy Nhơn", response.message)
        self.assertEqual(["school", "admission_year"], response.data["missing_eligibility_context"])
        self.assertEqual("2026", response.data["known_eligibility_context"]["academic_year"])
        self.assertEqual("not_evaluated", response.data["eligibility_conclusion"])
        retriever_mock.retrieve_context.assert_not_called()
        generator_mock.generate_answer.assert_not_called()
        backend_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()

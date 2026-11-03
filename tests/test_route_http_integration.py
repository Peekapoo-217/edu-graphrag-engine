import json
import os
import tempfile
import threading
import unittest
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from rag_api import main_api
from rag_api.retrieval.graph_retriever import Neo4jRetriever
from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace


class _Parser:
    def __init__(self, response):
        self.response = response

    def parse_intent(self, **_kwargs):
        if isinstance(self.response, BaseException):
            raise self.response
        return dict(self.response)

    def is_ready_for_db(self, _intent):
        return True


class RouteHttpIntegrationTests(unittest.TestCase):
    def _request_with_trace(self, question, intent_result, *, retrieved=None, generation_error=None,
                            backend_get_data=None, backend_get_error=None, backend_post_data=None,
                            conversation_context=None, formatted_context="safe fixture context",
                            attach_evidence_provenance=None):
        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            stage_path = Path(directory) / f"stage_events_{run_id}.jsonl"
            route_path = Path(directory) / f"route_events_{run_id}.jsonl"
            environment = {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(stage_path),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(route_path),
            }
            retriever = Mock()
            if isinstance(retrieved, BaseException):
                retriever.retrieve_context.side_effect = retrieved
            else:
                retriever.retrieve_context.return_value = retrieved if retrieved is not None else [
                    {"evidence_id": "graph:e1", "program_name": "Program", "program_code": "P1",
                     "university_code": "U1", "combinations": []}
                ]
            retriever.format_for_llm.return_value = formatted_context
            if attach_evidence_provenance is not None:
                retriever.attach_evidence_provenance.side_effect = attach_evidence_provenance
            else:
                retriever.attach_evidence_provenance.return_value = None
            generator = Mock()
            generator.generate_answer.side_effect = generation_error
            if generation_error is None:
                generator.generate_answer.return_value = "grounded fixture answer"
            def backend_get(_url, **_kwargs):
                if backend_get_error:
                    raise backend_get_error
                response = Mock()
                payload = backend_get_data(_url) if callable(backend_get_data) else backend_get_data
                response.json.return_value = {"data": payload}
                return response
            def backend_post(_url, **_kwargs):
                response = Mock()
                payload = backend_post_data(_url) if callable(backend_post_data) else backend_post_data
                response.json.return_value = {"data": payload}
                return response
            with patch.dict(os.environ, environment), \
                    patch.object(main_api, "parser", _Parser(intent_result)), \
                    patch.object(main_api, "retriever", retriever), \
                    patch.object(main_api, "generator", generator), \
                    patch("rag_api.main_api.requests.get", side_effect=backend_get), \
                    patch("rag_api.main_api.requests.post", side_effect=backend_post):
                response = TestClient(main_api.app, raise_server_exceptions=False).post(
                    "/api/chat", json={"user_message": question, "context": conversation_context or []},
                    headers={"X-Diagnostic-Run-ID": run_id},
                )
            rows = [json.loads(line) for line in route_path.read_text(encoding="utf-8").splitlines()]
            stages = [json.loads(line) for line in stage_path.read_text(encoding="utf-8").splitlines()]
        return response, rows, stages, retriever, generator

    @staticmethod
    def _case_c_admission_record(*, acceptance_records=None, university_code="DQN", program_code="7810103",
                                 program_name="Quản trị dịch vụ du lịch và lữ hành",
                                 admission_year=2026, combination_codes=None):
        source_url = (
            "https://tuyensinh.qnu.edu.vn/vi/tuyen-sinh-dai-hoc-chinh-quy-2027/"
            "tb-1906-thong-tin-tuyen-sinh-dai-hoc-nam-2026-cap-nhat-hinh-thuc-dao-tao-chinh-quy-1208"
        )
        combination = {
            "id": "INFORMATICS_LITERATURE_MATH",
            "display_name": "(Tin học, Ngữ văn, Toán)",
            "required_subjects": ["Tin học", "Ngữ văn", "Toán"],
            "required_subject_codes": combination_codes or ["INFORMATICS", "LITERATURE", "MATH"],
            "required_aptitude_codes": [],
            "acceptance_records": acceptance_records if acceptance_records is not None else [{
                "admission_year": admission_year,
                "method_codes": "100, 200, 402A, 402B",
                "source_url": source_url,
            }],
            "matching_subject_groups": [{
                "high_school_code": "TVQN",
                "high_school_name": "Trường THPT Trưng Vương",
                "academic_year": "2026",
                "subject_group_code": "G01",
                "subject_group_name": "Nhóm môn 1",
                "subjects": [{"code": "INFORMATICS", "canonical_code": "INFORMATICS", "name": "Tin học"}],
            }],
        }
        return {
            "program_name": program_name,
            "program_code": program_code,
            "university_name": "Trường Đại học Quy Nhơn",
            "university_code": university_code,
            "combination_id": combination["id"],
            "combination_name": combination["display_name"],
            "required_subjects": combination["required_subjects"],
            "required_subject_codes": combination["required_subject_codes"],
            "acceptance_records": combination["acceptance_records"],
            "combinations": [combination],
            "verified_subject_overlaps": [{
                "validation_status": "verified",
                "subject_code": "INFORMATICS",
                "subject_name": "Tin học",
                "observed_group_subject_code": "INFORMATICS",
                "school_code": "TVQN",
                "academic_year": "2026",
                "subject_group_code": "G01",
                "program_code": "7810103",
                "university_code": "DQN",
                "relationship_ids": [
                    "requires-subject:fixture:INFORMATICS",
                    "includes-subject:fixture:TVQN:2026:G01:INFORMATICS",
                ],
            }],
        }

    def test_case_c_http_answer_reports_verified_acceptance_separately_from_partial_group_eligibility(self):
        question = (
            "Cho em hỏi ngành Quản trị dịch vụ du lịch và lữ hành tại Trường Đại học Quy Nhơn năm 2026 có "
            "xét tuyển tổ hợp (Tin học, Ngữ văn, Toán) không? Nếu em chọn học Nhóm môn 1 năm 2026 ở "
            "Trường THPT Trưng Vương (Quy Nhơn) thì có môn nào trong nhóm môn này nằm trong tổ hợp xét tuyển đó không?"
        )
        record = self._case_c_admission_record()
        provenance = Neo4jRetriever.__new__(Neo4jRetriever)

        response, _, _, retriever, generator = self._request_with_trace(
            question,
            {"intent": "pathway_lookup", "extracted_filters": {
                "high_school": "Trường THPT Trưng Vương", "desired_program": "Quản trị dịch vụ du lịch và lữ hành",
                "university": "Trường Đại học Quy Nhơn", "admission_year": "2026"},
             "high_schools": ["Trường THPT Trưng Vương"], "academic_year": "2026",
             "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}]},
            retrieved=[record],
            backend_get_data=lambda url: (["2026"] if "academic-years" in url else
                                          [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
                                          if "subject-groups" in url else
                                          [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]),
            attach_evidence_provenance=lambda rows, filters=None: provenance.attach_evidence_provenance(
                rows, filters=filters),
        )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        answer = body["message"]
        self.assertIn("tổ hợp (Tin học, Ngữ văn, Toán)", answer,
                      repr(body["data"]["filters"]) + repr(body["data"]["evidence"]))
        self.assertIn("năm 2026", answer)
        self.assertIn("tuyensinh.qnu.edu.vn", answer)
        self.assertIn("Tin học", answer)
        self.assertIn("không phải danh sách đầy đủ", answer)
        self.assertIn("không xác nhận", answer)
        self.assertFalse(body["data"]["graph_path_validation"]["complete"])
        self.assertEqual("partial", body["data"]["answer_completeness"])
        self.assertTrue(any(
            edge.get("from") == "TrainingProgram:DQN|7810103"
            and edge.get("to") == "AdmissionCombination:INFORMATICS_LITERATURE_MATH"
            and str(edge.get("relationship_id", "")).startswith("accepts-combination:")
            for edge in body["data"]["evidence"][0]["evidence_path"]
        ))
        self.assertEqual("G01", retriever.retrieve_context.call_args.kwargs["filters"]["subject_group_code"])
        self.assertFalse(generator.generate_answer.called)

    def test_case_c_does_not_claim_admission_acceptance_without_matching_verified_record(self):
        question = (
            "Cho em hỏi ngành Quản trị dịch vụ du lịch và lữ hành tại Trường Đại học Quy Nhơn năm 2026 có "
            "xét tuyển tổ hợp (Tin học, Ngữ văn, Toán) không? Nếu em chọn học Nhóm môn 1 năm 2026 ở "
            "Trường THPT Trưng Vương (Quy Nhơn) thì có môn nào trong nhóm môn này nằm trong tổ hợp xét tuyển đó không?"
        )
        cases = [
            (self._case_c_admission_record(acceptance_records=[]), "missing acceptance record"),
            (self._case_c_admission_record(university_code="OTHER"), "wrong university"),
            (self._case_c_admission_record(program_code="9999999", program_name="Wrong program"), "wrong program"),
            (self._case_c_admission_record(admission_year=2025), "wrong admission year"),
            (self._case_c_admission_record(combination_codes=["CHEMISTRY", "LITERATURE", "MATH"]),
             "wrong canonical combination"),
            (self._case_c_admission_record(acceptance_records=[{
                "admission_year": 2026, "method_codes": "100", "source_url": "https://unrelated.example/admission"}],
            ), "untrusted source"),
            (self._case_c_admission_record(acceptance_records=[
                {"admission_year": 2026, "method_codes": "100, 200, 402A, 402B",
                 "source_url": "https://tuyensinh.qnu.edu.vn/notice", "accepted": True},
                {"admission_year": 2026, "method_codes": "100, 200, 402A, 402B",
                 "source_url": "https://tuyensinh.qnu.edu.vn/notice", "accepted": False},
            ]), "contradictory admission status"),
        ]
        for record, reason in cases:
            with self.subTest(reason=reason):
                provenance = Neo4jRetriever.__new__(Neo4jRetriever)
                response, _, _, _, _ = self._request_with_trace(
                    question,
                    {"intent": "pathway_lookup", "extracted_filters": {
                        "high_school": "Trường THPT Trưng Vương", "desired_program": "Quản trị dịch vụ du lịch và lữ hành",
                        "university": "Trường Đại học Quy Nhơn", "admission_year": "2026"},
                     "high_schools": ["Trường THPT Trưng Vương"], "academic_year": "2026",
                     "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}]},
                    retrieved=[record],
                    backend_get_data=lambda url: (["2026"] if "academic-years" in url else
                                                  [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
                                                  if "subject-groups" in url else
                                                  [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]),
                    attach_evidence_provenance=lambda rows, filters=None: provenance.attach_evidence_provenance(
                        rows, filters=filters),
                )
                self.assertEqual(200, response.status_code, response.text)
                answer = response.json()["message"].casefold()
                self.assertNotIn("tuyensinh.qnu.edu.vn", answer)
                self.assertIn("chưa xác nhận", answer)
                self.assertIn("tin học", answer)

    def test_case_c_conflicting_duplicate_combination_identity_fails_closed(self):
        question = (
            "Cho em hỏi ngành Quản trị dịch vụ du lịch và lữ hành tại Trường Đại học Quy Nhơn năm 2026 có "
            "xét tuyển tổ hợp (Tin học, Ngữ văn, Toán) không? Nếu em chọn học Nhóm môn 1 năm 2026 ở "
            "Trường THPT Trưng Vương (Quy Nhơn) thì có môn nào trong nhóm môn này nằm trong tổ hợp xét tuyển đó không?"
        )
        record = self._case_c_admission_record()
        conflicting = dict(record["combinations"][0])
        conflicting["required_subject_codes"] = ["CHEMISTRY", "LITERATURE", "MATH"]
        record["combinations"].append(conflicting)
        provenance = Neo4jRetriever.__new__(Neo4jRetriever)
        response, _, _, _, _ = self._request_with_trace(
            question,
            {"intent": "pathway_lookup", "extracted_filters": {
                "high_school": "Trường THPT Trưng Vương", "desired_program": "Quản trị dịch vụ du lịch và lữ hành",
                "university": "Trường Đại học Quy Nhơn", "admission_year": "2026"},
             "high_schools": ["Trường THPT Trưng Vương"], "academic_year": "2026",
             "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}]},
            retrieved=[record],
            backend_get_data=lambda url: (["2026"] if "academic-years" in url else
                                          [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
                                          if "subject-groups" in url else
                                          [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]),
            attach_evidence_provenance=lambda rows, filters=None: provenance.attach_evidence_provenance(
                rows, filters=filters),
        )
        self.assertEqual(200, response.status_code, response.text)
        answer = response.json()["message"].casefold()
        self.assertNotIn("tuyensinh.qnu.edu.vn", answer)
        self.assertIn("chưa xác nhận", answer)

    def test_admission_acceptance_requires_unambiguous_university_and_program_scope(self):
        provenance = Neo4jRetriever.__new__(Neo4jRetriever)
        record = provenance.attach_evidence_provenance(
            [self._case_c_admission_record()], filters={"high_school_code": "TVQN", "academic_year": "2026"}
        )[0]
        explicit_tuple = {"tinhoc", "nguvan", "toan"}
        base = {"admission_year": "2026"}
        rejected_scopes = [
            {**base, "program_names": ["Quản trị dịch vụ du lịch và lữ hành"]},
            {**base, "university": "Trường Đại học Quy Nhơn"},
            {**base, "university": "Quy Nhơn", "program_names": ["Quản trị dịch vụ du lịch và lữ hành"]},
            {**base, "university": "Trường Đại học Quy Nhơn", "program_names": ["Quản trị dịch vụ"]},
        ]
        for filters in rejected_scopes:
            with self.subTest(filters=filters):
                self.assertEqual([], main_api._verified_admission_acceptances(record, filters, explicit_tuple))

    def test_phase76_exact_g01_followup_is_restricted_to_explicit_group(self):
        question = (
            "Quay lại ngành và tổ hợp xét tuyển em vừa hỏi ở lượt trước, nếu em vào học Nhóm 1 năm 2026 "
            "của THPT Trần Cao Vân thì có môn lựa chọn nào trùng với tổ hợp đó không?"
        )
        history = [
            {
                "role": "user",
                "content": "Dạ cho em hỏi ngành Sư phạm Vật lý tại Trường Đại học Quy Nhơn có xét tuyển bằng tổ hợp (Hóa học, Toán, Vật lí) năm 2026 không ạ?",
            },
            {
                "role": "assistant",
                "content": "Các tổ hợp trong dữ liệu có thành phần khớp với những môn em nêu:\n- **(Hóa học, Toán, Vật lí)** — A00: Toán, Vật lí, Hóa học (source: https://diemthi.tuyensinh247.com/to-hop-mon.html)",
            },
        ]
        partial = {
            "evidence_id": "path:TCV:2026:G01:overlap",
            "answer_scope": "subject_overlap",
            "verified_subject_overlaps": [{
                "validation_status": "verified", "subject_code": "PHYSICS",
                "subject_name": "Vật lí", "subject_group_code": "G01",
                "school_code": "TCV", "academic_year": "2026",
            }],
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
        }

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [
                    {"groupCode": "G01", "groupName": "Nhóm môn 1"},
                    {"groupCode": "G02", "groupName": "Nhóm môn 2"},
                    {"groupCode": "G03", "groupName": "Nhóm môn 3"},
                    {"groupCode": "G04", "groupName": "Nhóm môn 4"},
                ]
            return [{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}]

        response, _, _, retriever, _ = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trần Cao Vân",
                    "high_school_code": "TCV",
                    "desired_program": "Sư phạm Vật lý",
                    "university": "Trường Đại học Quy Nhơn",
                    "admission_year": "2026",
                },
                "high_schools": ["Trường THPT Trần Cao Vân"],
                "academic_year": "2026",
                "answer_scope": "subject_overlap",
                "answer_scope_source": "deterministic_explicit_scope",
            },
            retrieved=[partial],
            conversation_context=history,
            backend_get_data=backend_catalog,
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("G01", retriever.retrieve_context.call_args.kwargs["filters"].get("subject_group_code"))
        self.assertEqual("2026", retriever.retrieve_context.call_args.kwargs["filters"]["academic_year"])
        self.assertEqual("subject_overlap", retriever.retrieve_context.call_args.kwargs["filters"]["answer_scope"])

    def test_phase76_exact_g04_followup_overrides_stale_parser_group_code(self):
        question = (
            "Dạ vậy tổ hợp này có môn nào trùng với các môn học lựa chọn của Nhóm 4 tại "
            "Trường THPT Trần Cao Vân (năm học 2026) không ạ?"
        )
        history = [
            {
                "role": "user",
                "content": "Chào thầy cô, em đang tìm hiểu tuyển sinh ngành Sư phạm Lịch sử của Trường Đại học Quy Nhơn năm 2026. Em thấy có hai tổ hợp được nhắc đến là C00 (Ngữ văn, Lịch sử, Địa lí) và tổ hợp (Giáo dục kinh tế và pháp luật, Lịch sử, Ngữ văn).",
            },
            {
                "role": "assistant",
                "content": "Các bản ghi mới nhất cho mã **C00**:\n- C00: Ngữ văn, Lịch sử, Địa lí (tổ hợp chuẩn: `GEOGRAPHY_HISTORY_LITERATURE`) (source: https://diemthi.tuyensinh247.com/to-hop-mon.html)",
            },
        ]
        partial = {
            "evidence_id": "path:TCV:2026:G04:overlap",
            "answer_scope": "subject_overlap",
            "verified_subject_overlaps": [{
                "validation_status": "verified", "subject_code": "GEOGRAPHY",
                "subject_name": "Địa lí", "subject_group_code": "G04",
                "school_code": "TCV", "academic_year": "2026",
            }],
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
        }

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [
                    {"groupCode": "G01", "groupName": "Nhóm môn 1"},
                    {"groupCode": "G02", "groupName": "Nhóm môn 2"},
                    {"groupCode": "G03", "groupName": "Nhóm môn 3"},
                    {"groupCode": "G04", "groupName": "Nhóm môn 4"},
                ]
            return [{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}]

        response, _, _, retriever, _ = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trần Cao Vân",
                    "high_school_code": "TCV",
                    "desired_program": "Sư phạm Lịch sử",
                    "university": "Trường Đại học Quy Nhơn",
                    "admission_year": "2026",
                },
                "high_schools": ["Trường THPT Trần Cao Vân"],
                "requested_group_codes": [{"code": "G02", "evidence": "stale parser slot"}],
                "academic_year": "2026",
                "answer_scope": "subject_overlap",
                "answer_scope_source": "deterministic_explicit_scope",
            },
            retrieved=[partial],
            conversation_context=history,
            backend_get_data=backend_catalog,
        )

        self.assertEqual(200, response.status_code, response.text)
        filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("G04", filters.get("subject_group_code"))
        self.assertEqual("subject_overlap", filters["answer_scope"])

    def test_subject_overlap_response_uses_only_verified_partial_overlap_claims(self):
        question = (
            "Cho em hỏi ngành Quản trị dịch vụ du lịch và lữ hành tại Trường Đại học Quy Nhơn năm 2026 "
            "có xét tuyển tổ hợp (Tin học, Ngữ văn, Toán) không? Nếu em chọn học Nhóm môn 1 năm 2026 "
            "ở Trường THPT Trưng Vương (Quy Nhơn) thì có môn nào trong nhóm môn này nằm trong tổ hợp "
            "xét tuyển đó không?"
        )
        record = {
            "evidence_id": "graph:phase76-g01-overlap",
            "program_name": "Quản trị dịch vụ du lịch và lữ hành",
            "program_code": "7810103",
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            "verified_subject_overlaps": [
                {"validation_status": "verified", "subject_name": "Hóa học", "subject_code": "CHEMISTRY",
                 "subject_group_code": "G01", "school_code": "TVQN", "academic_year": "2026"},
                {"validation_status": "verified", "subject_name": "Tin học", "subject_code": "INFORMATICS",
                 "subject_group_code": "G01", "school_code": "TVQN", "academic_year": "2026"},
                {"validation_status": "verified", "subject_name": "Vật lí", "subject_code": "PHYSICS",
                 "subject_group_code": "G01", "school_code": "TVQN", "academic_year": "2026"},
                {"validation_status": "verified", "subject_name": "Văn học", "subject_code": "LITERATURE_VARIANT",
                 "subject_group_code": "G01", "school_code": "TVQN", "academic_year": "2026"},
            ],
        }
        response, _, _, _, generator = self._request_with_trace(
            question,
            {"intent": "pathway_lookup", "answer_scope": "subject_overlap",
             "answer_scope_source": "deterministic_explicit_scope",
             "extracted_filters": {"high_school": "Trường THPT Trưng Vương", "high_school_code": "TVQN",
                                   "desired_program": "Quản trị dịch vụ du lịch và lữ hành",
                                   "university": "Trường Đại học Quy Nhơn"},
             "high_schools": ["Trường THPT Trưng Vương"],
             "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}],
             "academic_year": "2026"},
            retrieved=[record],
            backend_get_data=lambda url: (["2026"] if "academic-years" in url else
                                          [{"groupCode": "G01", "groupName": "Nhóm môn 1"}] if "subject-groups" in url else
                                          [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]),
        )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("incomplete", body["status"])
        self.assertIn("Tin học", body["message"])
        # The user-supplied tuple is Informatics/Literature/Math. The retriever
        # also returned program overlaps for Chemistry/Physics from other
        # combinations; those are not members of the tuple being asked about.
        self.assertNotIn("Hóa học", body["message"])
        self.assertNotIn("Vật lí", body["message"])
        self.assertNotIn("Văn học", body["message"])
        self.assertIn("không phải danh sách đầy đủ", body["message"].casefold())
        self.assertNotIn("được xét tuyển", body["message"].casefold())
        self.assertIn("chưa xác nhận", body["message"].casefold())
        self.assertFalse(body["data"]["graph_path_validation"]["complete"])
        self.assertEqual("partial", body["data"]["answer_completeness"])
        self.assertFalse(generator.generate_answer.called)

        decomposed = unicodedata.normalize("NFD", question)
        nfd_response, _, _, _, _ = self._request_with_trace(
            decomposed,
            {"intent": "pathway_lookup", "answer_scope": "subject_overlap",
             "answer_scope_source": "deterministic_explicit_scope",
             "extracted_filters": {"high_school": "Trường THPT Trưng Vương", "high_school_code": "TVQN",
                                   "desired_program": "Quản trị dịch vụ du lịch và lữ hành",
                                   "university": "Trường Đại học Quy Nhơn"},
             "high_schools": ["Trường THPT Trưng Vương"],
             "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}],
             "academic_year": "2026"},
            retrieved=[record],
            backend_get_data=lambda url: (["2026"] if "academic-years" in url else
                                          [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
                                          if "subject-groups" in url else
                                          [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]),
        )
        self.assertEqual(200, nfd_response.status_code, nfd_response.text)
        nfd_body = nfd_response.json()
        self.assertEqual("incomplete", nfd_body["status"])
        self.assertIn("Tin học", nfd_body["message"])
        self.assertNotIn("Hóa học", nfd_body["message"])
        self.assertNotIn("Vật lí", nfd_body["message"])
        self.assertNotIn("Văn học", nfd_body["message"])
        self.assertFalse(nfd_body["data"]["graph_path_validation"]["complete"])

    def test_subject_overlap_with_only_unrelated_groups_does_not_claim_target_absent(self):
        question = (
            "Dạ vậy tổ hợp này có môn nào trùng với các môn học lựa chọn của Nhóm 4 tại "
            "Trường THPT Trần Cao Vân (năm học 2026) không ạ?"
        )
        record = {
            "evidence_id": "graph:unrelated-g02",
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            "verified_subject_overlaps": [{"validation_status": "verified", "subject_name": "Địa lí",
                 "subject_code": "GEOGRAPHY", "subject_group_code": "G02", "school_code": "TCV",
                 "academic_year": "2026"}],
        }
        response, _, _, _, generator = self._request_with_trace(
            question,
            {"intent": "pathway_lookup", "answer_scope": "subject_overlap",
             "answer_scope_source": "deterministic_explicit_scope",
             "extracted_filters": {"high_school": "Trường THPT Trần Cao Vân", "high_school_code": "TCV",
                                   "university": "Trường Đại học Quy Nhơn", "admission_year": "2026"},
             "high_schools": ["Trường THPT Trần Cao Vân"],
             "requested_group_codes": [{"code": "G04", "evidence": "Nhóm 4"}], "academic_year": "2026"},
            retrieved=[record],
            backend_get_data=lambda url: (["2026"] if "academic-years" in url else
                                          [{"groupCode": f"G0{i}", "groupName": f"Nhóm môn {i}"}
                                           for i in range(1, 5)] if "subject-groups" in url else
                                          [{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}]),
        )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("incomplete", body["status"])
        self.assertNotIn("cơ sở dữ liệu không có", body["message"].casefold())
        self.assertIn("chưa cung cấp bằng chứng", body["message"].casefold())
        self.assertFalse(generator.generate_answer.called)

    def test_graph_route_composition_uses_post_provenance_evidence_identifiers(self):
        question = (
            "Ở Trường THPT Trưng Vương năm 2026, Nhóm 1 có môn nào chung với tổ hợp xét tuyển "
            "của ngành Kỹ thuật điện không?"
        )
        raw_record = {
            "id": "raw-row-17",
            "verified_subject_overlaps": [{"validation_status": "verified", "subject_name": "Vật lí",
                 "subject_code": "PHYSICS", "subject_group_code": "G01", "school_code": "TVQN",
                 "academic_year": "2026"}],
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
        }

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
            return [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]

        response, events, _, retriever, _ = self._request_with_trace(
            question,
            {"intent": "pathway_lookup", "extracted_filters": {
                "high_school": "Trường THPT Trưng Vương", "high_school_code": "TVQN",
                "desired_program": "Kỹ thuật điện", "university": "Đại học Quy Nhơn"},
             "high_schools": ["Trường THPT Trưng Vương"], "academic_year": "2026",
             "requested_group_codes": [{"code": "G01", "evidence": "Nhóm 1"}]},
            retrieved=[raw_record], backend_get_data=backend_catalog,
            attach_evidence_provenance=lambda rows, filters=None: [
                {**row, "evidence_id": "graph:enriched-17"} for row in rows
            ],
        )

        self.assertEqual(200, response.status_code, response.text)
        filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("G01", filters["subject_group_code"])
        self.assertTrue(response.json()["data"]["actual_execution_route"]["graph_retrieval_executed"])
        self.assertEqual("graph_retrieval_only",
                         response.json()["data"]["actual_execution_route"]["execution_kind"])
        graph_events = [event for event in events if event.get("retrieval_system") == "fastapi_graph"]
        produced = next(event for event in graph_events if event["event"] == "evidence_produced")
        composed = next(event for event in graph_events if event["event"] == "evidence_composed")
        self.assertIn("graph:enriched-17", produced["evidence_reference_ids"])
        self.assertEqual(["graph:enriched-17", "raw-row-17"], composed["evidence_reference_ids"])
        self.assertEqual("verified", composed["composition_status"])

    def test_exact_live_overlap_question_propagates_partial_scope_through_http_pipeline(self):
        question = (
            "Em chuẩn bị vào lớp 10 và dự định đăng ký Nhóm môn 1 tại Trường THPT Trưng Vương "
            "(Quy Nhơn). Cho em hỏi trong nhóm môn này có môn nào thuộc tổ hợp xét tuyển "
            "(Toán, Vật lí, Công nghệ) vào ngành Kỹ thuật điện của Trường Đại học Quy Nhơn "
            "năm 2026 không?"
        )
        partial = {
            "evidence_id": "path:TVQN:2026:G01:PHYSICS",
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            "verified_subject_overlaps": [{"validation_status": "verified", "subject_code": "PHYSICS",
                                            "subject_name": "Vật lí", "subject_group_code": "G01",
                                            "school_code": "TVQN", "academic_year": "2026"}],
            "evidence_path": [{"relationship_id": "includes-subject:TVQN:2026:G01:PHYSICS"}],
        }

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
            return [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]

        response, rows, _, retriever, generator = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trưng Vương",
                    "desired_program": "Kỹ thuật điện",
                    "university": "Trường Đại học Quy Nhơn",
                },
                "high_schools": ["Trường THPT Trưng Vương"],
                "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}],
                "academic_year": "2026",
            },
            retrieved=[partial],
            backend_get_data=backend_catalog,
            formatted_context="Verified overlap: PHYSICS. This is not a complete admission pathway.",
        )

        self.assertEqual(200, response.status_code, response.text)
        filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("subject_overlap", filters["answer_scope"])
        self.assertEqual("TVQN", filters["high_school_code"])
        self.assertEqual("2026", filters["academic_year"])
        self.assertEqual("G01", filters["subject_group_code"])
        self.assertEqual("partial", response.json()["data"]["answer_completeness"])
        self.assertFalse(response.json()["data"]["graph_path_validation"]["complete"])
        self.assertEqual("path:TVQN:2026:G01:PHYSICS",
                         response.json()["data"]["evidence"][0]["evidence_id"])
        self.assertFalse(generator.generate_answer.called)
        self.assertIn("Vật lí", response.json()["message"])
        self.assertIn("không phải danh sách đầy đủ", response.json()["message"])
        self.assertIn("chưa xác nhận", response.json()["message"])
        finals = [row for row in rows if row.get("event") == "retrieval_result" and row.get("final_route")]
        self.assertEqual(1, len(finals))
        self.assertEqual("graph_retrieval_only", finals[0]["execution_kind"])

    def test_overlap_missing_school_year_clarification_is_valid_utf8_vietnamese(self):
        question = (
            "Em chuẩn bị vào lớp 10 và dự định đăng ký Nhóm môn 1 tại Trường THPT Trưng Vương "
            "(Quy Nhơn). Cho em hỏi trong nhóm môn này có môn nào thuộc tổ hợp xét tuyển "
            "(Toán, Vật lí, Công nghệ) vào ngành Kỹ thuật điện của Trường Đại học Quy Nhơn "
            "năm 2026 không?"
        )
        response, _, _, retriever, generator = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "answer_scope": "subject_overlap",
                "answer_scope_source": "deterministic_explicit_scope",
                "extracted_filters": {
                    "high_school": "Trường THPT Trưng Vương",
                    "high_school_code": "TVQN",
                    "desired_program": "Kỹ thuật điện",
                    "university": "Trường Đại học Quy Nhơn",
                    "admission_year": "2026",
                },
                "high_schools": ["Trường THPT Trưng Vương"],
                "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}],
                "school_resolution": {
                    "status": "resolved",
                    "school": {"code": "TVQN", "name": "Trường THPT Trưng Vương"},
                },
                "academic_year": None,
            },
            backend_get_data=[{"code": "TVQN", "name": "Trường THPT Trưng Vương"}],
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("incomplete", response.json()["status"])
        self.assertEqual(
            "Em cho mình biết năm học áp dụng cho nhóm môn ở trường này nhé.",
            response.json()["message"],
        )
        self.assertFalse(retriever.retrieve_context.called)
        self.assertFalse(generator.generate_answer.called)

    def test_exact_live_full_path_question_reaches_graph_with_full_path_scope(self):
        question = (
            "Nếu em chọn học Nhóm 4 (năm học 2026) tại trường THPT Trần Cao Vân thì nhóm này "
            "có đáp ứng được trọn vẹn tổ hợp xét tuyển gồm Giáo dục kinh tế và pháp luật, "
            "Địa lí, Toán của ngành Quản lý tài nguyên và môi trường ở Trường Đại học Quy Nhơn "
            "không? Môn nào trong nhóm trùng khớp với tổ hợp này?"
        )

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G04", "groupName": "Nhóm môn 4"}]
            return [{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}]

        response, rows, _, retriever, _ = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trần Cao Vân",
                    "desired_program": "Quản lý tài nguyên và môi trường",
                    "university": "Trường Đại học Quy Nhơn",
                },
                "high_schools": ["Trường THPT Trần Cao Vân"],
                "requested_group_codes": [{"code": "G04", "evidence": "Nhóm 4"}],
                "academic_year": "2026",
                "admission_year": "2026",
            },
            backend_get_data=backend_catalog,
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(retriever.retrieve_context.called, response.text)
        filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("full_path", filters["answer_scope"])
        self.assertEqual("TCV", filters["high_school_code"])
        self.assertEqual("G04", filters["subject_group_code"])
        self.assertEqual("graph_retrieval_generation",
                         response.json()["data"]["actual_execution_route"]["execution_kind"])
        self.assertEqual("pathway_lookup", response.json()["data"]["intent"])
        finals = [row for row in rows if row.get("event") == "retrieval_result" and row.get("final_route")]
        self.assertEqual(1, len(finals))
        self.assertEqual("graph_retrieval_generation", finals[0]["execution_kind"])

    def test_full_path_for_explicit_group_without_academic_year_clarifies_before_graph(self):
        question = (
            "Nhóm 4 tại Trường THPT Trần Cao Vân có đáp ứng trọn vẹn tổ hợp xét tuyển "
            "Giáo dục kinh tế và pháp luật, Địa lí, Toán của ngành Quản lý tài nguyên và môi trường "
            "tại Đại học Quy Nhơn cho năm tuyển sinh 2026 không?"
        )
        response, _, _, retriever, generator = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trần Cao Vân",
                    "high_school_code": "TCV",
                    "desired_program": "Quản lý tài nguyên và môi trường",
                    "university": "Đại học Quy Nhơn",
                    "admission_year": "2026",
                },
                "high_schools": ["Trường THPT Trần Cao Vân"],
                "requested_group_codes": [{"code": "G04", "evidence": "Nhóm 4"}],
                "school_resolution": {
                    "status": "resolved",
                    "school": {"code": "TCV", "name": "Trường THPT Trần Cao Vân"},
                },
                "academic_year": None,
            },
            backend_get_data=[{"code": "TCV", "name": "Trường THPT Trần Cao Vân"}],
        )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("incomplete", body["status"])
        self.assertEqual("missing_academic_year", body["data"]["graph_group_scope_resolution"]["status"])
        self.assertIn("n\u0103m h\u1ecdc", body["message"].lower())
        self.assertFalse(body["data"]["actual_execution_route"]["graph_retrieval_executed"])
        retriever.retrieve_context.assert_not_called()
        generator.generate_answer.assert_not_called()

    def test_admission_year_clarification_answer_keeps_real_prior_pathway_scope_over_http(self):
        question = "N\u0103m tuy\u1ec3n sinh m\u00e0 em h\u1ecfi l\u00e0 2026."
        original_question = (
            "N\u1ebfu em ch\u1ecdn h\u1ecdc Nh\u00f3m 4 (n\u0103m h\u1ecdc 2026) t\u1ea1i tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n th\u00ec nh\u00f3m n\u00e0y c\u00f3 \u0111\u00e1p \u1ee9ng \u0111\u01b0\u1ee3c "
            "tr\u1ecdn v\u1eb9n t\u1ed5 h\u1ee3p x\u00e9t tuy\u1ec3n g\u1ed3m Gi\u00e1o d\u1ee5c kinh t\u1ebf v\u00e0 ph\u00e1p lu\u1eadt, \u0110\u1ecba l\u00ed, To\u00e1n "
            "c\u1ee7a ng\u00e0nh Qu\u1ea3n l\u00fd t\u00e0i nguy\u00ean v\u00e0 m\u00f4i tr\u01b0\u1eddng \u1edf Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n kh\u00f4ng?"
        )
        clarification = (
            "\u0110\u1ec3 \u0111\u1ed1i chi\u1ebfu nh\u00f3m m\u00f4n v\u1edbi \u0111i\u1ec1u ki\u1ec7n tuy\u1ec3n sinh, em b\u1ed5 sung gi\u00fap m\u00ecnh n\u0103m tuy\u1ec3n sinh mu\u1ed1n \u0111\u1ed1i chi\u1ebfu."
        )
        history = [
            {"role": "user", "content": original_question},
            {"role": "assistant", "content": clarification},
        ]
        retrieved = [{
            "evidence_id": "path:TCV:2026:G04:complete",
            "evidence_dependency_validation": {"status": "complete", "complete": True},
        }]

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G04", "groupName": "Nh\u00f3m 4"}]
            return [{"code": "TCV", "name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"}]

        response, _, _, retriever, _ = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n",
                    "high_school_code": "TCV",
                    "desired_program": "Qu\u1ea3n l\u00fd t\u00e0i nguy\u00ean v\u00e0 m\u00f4i tr\u01b0\u1eddng",
                    "university": "Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n",
                    "admission_year": "2026",
                },
                "high_schools": ["Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"],
                "requested_group_codes": [{"code": "G04", "evidence": "Nh\u00f3m 4"}],
                "school_resolution": {
                    "status": "resolved",
                    "school": {"code": "TCV", "name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"},
                },
                "academic_year": "2026",
            },
            retrieved=retrieved,
            conversation_context=history,
            backend_get_data=backend_catalog,
        )

        self.assertEqual(200, response.status_code, response.text)
        filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("full_path", filters["answer_scope"])
        self.assertEqual("TCV", filters["high_school_code"])
        self.assertEqual("2026", filters["academic_year"])
        self.assertEqual("2026", filters["admission_year"])
        self.assertEqual("G04", filters["subject_group_code"])

    def test_exact_live_admission_year_reply_restores_group_code_when_intent_omits_it(self):
        question = "N\u0103m tuy\u1ec3n sinh m\u00e0 em h\u1ecfi l\u00e0 2026."
        original_question = (
            "N\u1ebfu em ch\u1ecdn h\u1ecdc Nh\u00f3m 4 (n\u0103m h\u1ecdc 2026) t\u1ea1i tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n th\u00ec nh\u00f3m n\u00e0y c\u00f3 \u0111\u00e1p \u1ee9ng \u0111\u01b0\u1ee3c "
            "tr\u1ecdn v\u1eb9n t\u1ed5 h\u1ee3p x\u00e9t tuy\u1ec3n g\u1ed3m Gi\u00e1o d\u1ee5c kinh t\u1ebf v\u00e0 ph\u00e1p lu\u1eadt, \u0110\u1ecba l\u00ed, To\u00e1n "
            "c\u1ee7a ng\u00e0nh Qu\u1ea3n l\u00fd t\u00e0i nguy\u00ean v\u00e0 m\u00f4i tr\u01b0\u1eddng \u1edf Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n kh\u00f4ng? M\u00f4n n\u00e0o trong nh\u00f3m tr\u00f9ng kh\u1edbp v\u1edbi t\u1ed5 h\u1ee3p n\u00e0y?"
        )
        clarification = (
            "\u0110\u1ec3 \u0111\u1ed1i chi\u1ebfu nh\u00f3m m\u00f4n v\u1edbi \u0111i\u1ec1u ki\u1ec7n tuy\u1ec3n sinh, em b\u1ed5 sung gi\u00fap m\u00ecnh n\u0103m tuy\u1ec3n sinh mu\u1ed1n \u0111\u1ed1i chi\u1ebfu."
        )
        history = [
            {"role": "user", "content": original_question},
            {"role": "assistant", "content": clarification},
        ]
        incomplete_record = {
            "evidence_id": "path:TCV:2026:G04:partial",
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            "evidence_path": [],
        }

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G04", "groupName": "Nh\u00f3m 4"}]
            return [{"code": "TCV", "name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"}]

        response, _, _, retriever, _ = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "answer_scope": "full_path",
                "answer_scope_source": "deterministic_pathway_default",
                "extracted_filters": {
                    "high_school": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n",
                    "high_school_code": "TCV",
                    "desired_program": "Qu\u1ea3n l\u00fd t\u00e0i nguy\u00ean v\u00e0 m\u00f4i tr\u01b0\u1eddng",
                    "university": "Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n",
                    "academic_year": "2026",
                    "admission_year": "2026",
                },
                "high_schools": ["Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"],
                "academic_year": "2026",
            },
            retrieved=[incomplete_record],
            conversation_context=history,
            backend_get_data=backend_catalog,
        )

        self.assertEqual(200, response.status_code, response.text)
        filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("full_path", filters["answer_scope"])
        self.assertEqual("TCV", filters["high_school_code"])
        self.assertEqual("2026", filters["academic_year"])
        self.assertEqual("2026", filters["admission_year"])
        self.assertEqual("G04", filters.get("subject_group_code"))
        self.assertFalse(response.json()["data"]["graph_path_validation"]["complete"])

    def test_full_path_with_unresolved_explicit_group_clarifies_without_graph_or_generation(self):
        question = "N\u0103m tuy\u1ec3n sinh m\u00e0 em h\u1ecfi l\u00e0 2026."
        history = [
            {"role": "user", "content": "Nh\u00f3m 99 n\u0103m h\u1ecdc 2026 c\u1ee7a THPT Tr\u1ea7n Cao V\u00e2n c\u00f3 đủ tổ hợp của ngành X không?"},
            {"role": "assistant", "content": "Em bổ sung năm tuyển sinh muốn đối chiếu giúp mình."},
        ]

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G04", "groupName": "Nh\u00f3m 4"}]
            return [{"code": "TCV", "name": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"}]

        response, _, _, retriever, generator = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n",
                    "desired_program": "Ng\u00e0nh X",
                    "university": "Tr\u01b0\u1eddng \u0110\u1ea1i h\u1ecdc Quy Nh\u01a1n",
                    "admission_year": "2026",
                },
                "high_schools": ["Tr\u01b0\u1eddng THPT Tr\u1ea7n Cao V\u00e2n"],
                "requested_group_codes": [{"code": "G99", "evidence": "Nh\u00f3m 99"}],
                "academic_year": "2026",
            },
            conversation_context=history,
            backend_get_data=backend_catalog,
        )

        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("incomplete", body["status"])
        self.assertEqual("unresolved", body["data"]["graph_group_scope_resolution"]["status"])
        retriever.retrieve_context.assert_not_called()
        generator.generate_answer.assert_not_called()

    def test_exact_live_followup_keeps_authentic_history_but_uses_current_full_path_scope(self):
        question = (
            "Vậy Nhóm môn 1 đó có đáp ứng trọn vẹn tổ hợp xét tuyển (Toán, Vật lí, Công nghệ) "
            "của ngành Kỹ thuật điện tại Đại học Quy Nhơn không?"
        )
        history = [
            {"role": "user", "content": (
                "Em chuẩn bị vào lớp 10 và dự định đăng ký Nhóm môn 1 tại Trường THPT Trưng Vương "
                "(Quy Nhơn). Cho em hỏi trong nhóm môn này có môn nào thuộc tổ hợp xét tuyển "
                "(Toán, Vật lí, Công nghệ) vào ngành Kỹ thuật điện của Trường Đại học Quy Nhơn "
                "năm 2026 không?"
            )},
            {"role": "assistant", "content": (
                "Chưa tìm thấy dữ liệu phù hợp với trường, tổ hợp hoặc ngành em nêu. "
                "Em kiểm tra lại tên/mã hoặc phạm vi năm học nhé."
            )},
        ]

        def backend_catalog(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G01", "groupName": "Nhóm môn 1"}]
            return [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]

        response, _, _, retriever, generator = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "answer_scope": "subject_overlap",
                "answer_scope_source": "prior_or_model_value",
                "extracted_filters": {
                    "high_school": "Trường THPT Trưng Vương",
                    "desired_program": "Kỹ thuật điện",
                    "university": "Đại học Quy Nhơn",
                },
                "high_schools": ["Trường THPT Trưng Vương"],
                "requested_group_codes": [{"code": "G01", "evidence": "Nhóm môn 1"}],
                "academic_year": "2026",
                "admission_year": "2026",
            },
            retrieved=[{
                "evidence_id": "path:TVQN:2026:G01:full-path-check",
                "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            }],
            backend_get_data=backend_catalog,
            conversation_context=history,
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("full_path", retriever.retrieve_context.call_args.kwargs["filters"]["answer_scope"])
        self.assertEqual(history, generator.generate_answer.call_args.kwargs["context"])

    def test_explicit_followup_overlap_scope_reaches_graph_retriever_without_certifying_path(self):
        question = (
            "Nhóm môn nào ở trường đó có chứa môn nằm trong tổ hợp xét tuyển ngành "
            "Sư phạm Địa lý của Đại học Quy Nhơn?"
        )
        history = [
            {"role": "user", "content": "Trường THPT Trưng Vương ở Quy Nhơn có những nhóm môn nào cho năm 2026?"},
            {"role": "assistant", "content": "Các nhóm môn đang công bố tại trường này năm 2026 gồm G01, G02, G03 và G04."},
        ]
        partial = {
            "evidence_id": "path:TVQN:2026:G01:P1",
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            "verified_subject_overlaps": [{"validation_status": "verified", "subject_code": "CHEMISTRY",
                                            "subject_name": "Hóa học", "subject_group_code": "G01",
                                            "school_code": "TVQN", "academic_year": "2026"}],
            "evidence_path": [{"relationship_id": "includes-subject:TVQN:2026:G01:CHEMISTRY"}],
        }
        response, _, _, retriever, generator = self._request_with_trace(
            question,
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trưng Vương",
                    "desired_program": "Sư phạm Địa lý",
                    "university": "Đại học Quy Nhơn",
                },
                "high_schools": ["Trường THPT Trưng Vương"],
                "academic_year": "2026",
            },
            retrieved=[partial],
            backend_get_data=lambda url: (
                ["2026"] if "academic-years" in url else
                [{"code": "TVQN", "name": "Trường THPT Trưng Vương"}]
            ),
            conversation_context=history,
            formatted_context=(
                "Verified subject overlap: CHEMISTRY. This does not establish complete admission eligibility."
            ),
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(retriever.retrieve_context.called)
        graph_filters = retriever.retrieve_context.call_args.kwargs["filters"]
        self.assertEqual("subject_overlap", graph_filters["answer_scope"])
        self.assertEqual("TVQN", graph_filters["high_school_code"])
        self.assertEqual("2026", graph_filters["academic_year"])
        self.assertFalse(generator.generate_answer.called)
        self.assertIn("Hóa học", response.json()["message"])
        self.assertIn("không phải danh sách đầy đủ", response.json()["message"])
        self.assertFalse(response.json()["data"]["graph_path_validation"]["complete"])
        self.assertEqual("partial", response.json()["data"]["answer_completeness"])

    def test_explicit_overlap_without_academic_year_clarifies_before_graph_retrieval(self):
        response, _, _, retriever, generator = self._request_with_trace(
            "Ở THPT Trưng Vương, nhóm nào có môn chung với tổ hợp xét tuyển ngành Sư phạm Địa lý?",
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": "Trường THPT Trưng Vương",
                    "desired_program": "Sư phạm Địa lý",
                    "university": "Đại học Quy Nhơn",
                },
                "high_schools": ["Trường THPT Trưng Vương"],
            },
            backend_get_data=[{"code": "TVQN", "name": "Trường THPT Trưng Vương"}],
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("incomplete", response.json()["status"])
        self.assertEqual("missing_academic_year",
                         response.json()["data"]["graph_group_scope_resolution"]["status"])
        retriever.retrieve_context.assert_not_called()
        generator.generate_answer.assert_not_called()

    def test_explicit_overlap_without_school_clarifies_before_graph_retrieval(self):
        response, _, _, retriever, generator = self._request_with_trace(
            "Nhóm nào có môn chung với tổ hợp xét tuyển ngành Sư phạm Địa lý?",
            {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "desired_program": "Sư phạm Địa lý",
                    "university": "Đại học Quy Nhơn",
                },
            },
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual("incomplete", response.json()["status"])
        self.assertEqual("missing_school",
                         response.json()["data"]["graph_group_scope_resolution"]["status"])
        retriever.retrieve_context.assert_not_called()
        generator.generate_answer.assert_not_called()

    def test_concurrent_overlap_and_eligibility_requests_keep_scopes_isolated(self):
        overlap_question = "Nhóm môn nào ở trường đó có chứa môn trong tổ hợp xét tuyển ngành A?"
        full_question = (
            "Ở THPT Trưng Vương năm 2026, nhóm G01 có đủ toàn bộ môn trong tổ hợp xét tuyển ngành A không?"
        )
        school = "Trường THPT Trưng Vương"
        histories = [{"role": "user", "content": f"Trường {school} có nhóm môn năm 2026?"}]
        parser = type("QuestionParser", (), {
            "parse_intent": lambda _self, user_message, **_kwargs: {
                "intent": "pathway_lookup",
                "extracted_filters": {
                    "high_school": school, "desired_program": "A", "university": "DQN",
                },
                "high_schools": [school], "academic_year": "2026",
                **({"requested_group_codes": [{"code": "G01", "evidence": "G01"}]}
                   if "G01" in user_message else {}),
            },
            "is_ready_for_db": lambda _self, _intent: True,
        })()
        seen_filters = []
        seen_lock = threading.Lock()
        overlap = {
            "evidence_id": "overlap:e1",
            "evidence_dependency_validation": {"status": "incomplete", "complete": False},
            "verified_subject_overlaps": [{"validation_status": "verified", "subject_code": "CHEMISTRY"}],
        }
        complete = {
            "evidence_id": "full:e1",
            "evidence_dependency_validation": {"status": "complete", "complete": True},
            "verified_subject_overlaps": [],
        }
        retriever = Mock()
        def retrieve_context(*, filters, **_kwargs):
            with seen_lock:
                seen_filters.append(dict(filters))
            return [overlap if filters.get("answer_scope") == "subject_overlap" else complete]
        retriever.retrieve_context.side_effect = retrieve_context
        retriever.format_for_llm.return_value = "safe fixture context"
        generator = Mock()
        generator.generate_answer.return_value = "Grounded answer."

        def backend_get(url):
            if "academic-years" in url:
                return ["2026"]
            if "subject-groups" in url:
                return [{"groupCode": "G01", "groupName": "Group 1"}]
            return [{"code": "TVQN", "name": school}]

        with patch.object(main_api, "parser", parser), \
                patch.object(main_api, "retriever", retriever), \
                patch.object(main_api, "generator", generator), \
                patch.object(main_api, "_backend_get", side_effect=backend_get):
            # Do not enter TestClient as a context manager: that would run the
            # production lifespan and replace the explicitly injected fakes.
            client = TestClient(main_api.app, raise_server_exceptions=False)
            barrier = threading.Barrier(2)
            def send(question):
                barrier.wait(timeout=5)
                return client.post("/api/chat", json={"user_message": question, "context": histories})
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(send, question) for question in (overlap_question, full_question)]
                responses = [future.result(timeout=10) for future in futures]

        self.assertEqual([200, 200], [response.status_code for response in responses])
        self.assertEqual(2, len(seen_filters))
        scopes = {filters["answer_scope"] for filters in seen_filters}
        self.assertEqual({"subject_overlap", "full_path"}, scopes)
        overlap_filters = next(filters for filters in seen_filters if filters["answer_scope"] == "subject_overlap")
        full_filters = next(filters for filters in seen_filters if filters["answer_scope"] == "full_path")
        self.assertNotIn("subject_group_code", overlap_filters)
        self.assertEqual("full_path", full_filters["answer_scope"])


    def test_empty_graph_retrieval_is_not_reported_as_consumed_graph_evidence(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Find a pathway", {"intent": "general", "extracted_filters": {"desired_program": "Program"}},
            retrieved=[],
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("unknown", response.json()["data"]["actual_execution_route"]["execution_kind"])
        operation_rows = [row for row in rows if row.get("event") == "evidence_produced"]
        self.assertTrue(operation_rows)
        self.assertEqual([], operation_rows[0]["evidence_reference_ids"])

    def test_graph_answer_records_consumed_evidence_and_one_final_route(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Find a pathway", {"intent": "general", "extracted_filters": {"desired_program": "Program"}},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("graph_retrieval_generation",
                         response.json()["data"]["actual_execution_route"]["execution_kind"])
        request_id = response.json()["data"]["request_id"]
        final_rows = [row for row in rows if row.get("event") == "retrieval_result" and row.get("final_route") is True]
        self.assertEqual(1, len(final_rows))
        self.assertEqual(request_id, final_rows[0]["request_id"])
        events = [row["event"] for row in rows if row.get("request_id") == request_id]
        self.assertLess(events.index("evidence_produced"), events.index("evidence_composed"))

    def test_unsupported_request_is_observed_as_not_executing_retrieval(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Tell me an unrelated fact", {"intent": "unsupported", "extracted_filters": {}},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("not_executed",
                         response.json()["data"]["actual_execution_route"]["execution_kind"])
        self.assertFalse(any(row.get("event", "").startswith("operation_") for row in rows))

    def test_backend_only_http_request_consumes_backend_evidence(self):
        response, rows, _, _, _ = self._request_with_trace(
            "A01 gồm những môn nào?", {"intent": "combination_lookup", "extracted_filters": {}},
            backend_get_data={"requestedCode": "A01", "labels": [
                {"code": "A01", "subjectDetails": "Toan, Ly, Anh", "canonicalCombinationId": "A01"}
            ]},
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("backend_lookup", response.json()["data"]["actual_execution_route"]["execution_kind"],
                         json.dumps(response.json(), ensure_ascii=False))
        backend_events = [row for row in rows if row.get("retrieval_system") == "backend_api"]
        self.assertTrue(any(row["event"] == "evidence_composed" for row in backend_events))

    def test_backend_and_graph_calls_without_joint_composition_are_not_hybrid(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Find a program at THPT A", {
                "intent": "general", "high_schools": ["THPT A"],
                "extracted_filters": {"high_school": "THPT A", "desired_program": "Program"},
            }, backend_get_data=[{"code": "A", "name": "THPT A"}],
        )
        self.assertEqual(200, response.status_code)
        systems = {row.get("retrieval_system") for row in rows
                   if row.get("event") == "operation_started"}
        self.assertEqual({"backend_api", "fastapi_graph"}, systems)
        self.assertNotEqual("hybrid", response.json()["data"]["actual_execution_route"]["execution_kind"])

    def test_clarification_before_retrieval_has_no_retrieval_operation(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Em có đủ điều kiện xét tuyển không?", {
                "intent": "pathway_lookup", "extracted_filters": {},
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual("incomplete", response.json()["status"])
        self.assertFalse(any(row.get("event", "").startswith("operation_") for row in rows))
        self.assertEqual("not_executed",
                         response.json()["data"]["actual_execution_route"]["execution_kind"])

    def test_backend_failure_has_failed_operation_and_one_error_final_event(self):
        import requests

        response, rows, _, _, _ = self._request_with_trace(
            "A01 gồm những môn nào?", {"intent": "combination_lookup", "extracted_filters": {}},
            backend_get_error=requests.ConnectionError("secret upstream diagnostic"),
        )
        self.assertEqual(503, response.status_code)
        operation_rows = [row for row in rows if row.get("event", "").startswith("operation_")]
        self.assertEqual(["operation_started", "operation_failed"], [row["event"] for row in operation_rows])
        final_rows = [row for row in rows if row.get("final_route") is True]
        self.assertEqual(1, len(final_rows))
        self.assertEqual("unknown", final_rows[0]["execution_kind"])
        self.assertNotIn("secret upstream diagnostic", json.dumps(rows))

    def test_graph_failure_has_failed_operation_and_one_error_final_event(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Find a pathway", {"intent": "general", "extracted_filters": {"desired_program": "Program"}},
            retrieved=RuntimeError("secret graph details"),
        )
        self.assertEqual(500, response.status_code)
        operation_rows = [row for row in rows if row.get("event", "").startswith("operation_")]
        self.assertEqual(["operation_started", "operation_failed"], [row["event"] for row in operation_rows])
        final_rows = [row for row in rows if row.get("final_route") is True]
        self.assertEqual(1, len(final_rows))
        self.assertNotIn("secret graph details", json.dumps(rows))

    def test_generation_failure_preserves_error_and_completed_retrieval_trace(self):
        response, rows, _, _, _ = self._request_with_trace(
            "Find a pathway", {"intent": "general", "extracted_filters": {"desired_program": "Program"}},
            generation_error=RuntimeError("secret generation details"),
        )
        self.assertEqual(500, response.status_code)
        self.assertTrue(any(row.get("event") == "operation_completed" and
                            row.get("retrieval_system") == "fastapi_graph" for row in rows))
        final_rows = [row for row in rows if row.get("final_route") is True]
        self.assertEqual(1, len(final_rows))
        self.assertNotIn("secret generation details", json.dumps(rows))

    def test_operation_trace_writer_failure_does_not_break_http_response_or_repeat_generation(self):
        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
            }
            retriever = Mock()
            retriever.retrieve_context.return_value = [{"evidence_id": "graph:e1"}]
            retriever.format_for_llm.return_value = "ctx"
            generator = Mock()
            generator.generate_answer.return_value = "answer"
            with patch.dict(os.environ, environment), \
                    patch.object(main_api, "parser", _Parser({
                        "intent": "general", "extracted_filters": {"desired_program": "Program"}})), \
                    patch.object(main_api, "retriever", retriever), \
                    patch.object(main_api, "generator", generator), \
                    patch("rag_api.utils.diagnostic_stage_trace.append_operation_event",
                          side_effect=OSError("private trace storage")):
                response = TestClient(main_api.app, raise_server_exceptions=False).post(
                    "/api/chat", json={"user_message": "Find a pathway"},
                    headers={"X-Diagnostic-Run-ID": run_id},
                )
            self.assertEqual(200, response.status_code)
            generator.generate_answer.assert_called_once()

    def test_nested_cross_school_http_comparison_has_one_request_final_event(self):
        catalog = [
            {"code": "A", "name": "THPT A"},
            {"code": "B", "name": "THPT B"},
        ]
        def get_payload(url):
            if url.endswith("/api/v1/high-schools"):
                return catalog
            if url.endswith("/api/v1/subjects/classifications"):
                return {"mandatorySubjects": [], "electiveSubjects": []}
            raise AssertionError("unexpected Backend GET path")
        post_payload = {
            "academicYear": "2026",
            "groups": [{"groupCode": "G01", "groupName": "G01",
                        "groupSubjects": [{"code": "MATH", "name": "Math"}],
                        "mandatorySubjects": [], "matchingAdmissionCombinations": []}],
        }
        response, rows, _, _, _ = self._request_with_trace(
            "So sánh nhóm môn ở THPT A và THPT B", {
                "intent": "subject_group_comparison",
                "high_schools": ["THPT A", "THPT B"],
                "extracted_filters": {"academic_year": "2026"},
            }, backend_get_data=get_payload, backend_post_data=post_payload,
        )
        self.assertEqual(200, response.status_code)
        request_id = response.json()["data"]["request_id"]
        final_rows = [row for row in rows if row.get("event") == "retrieval_result" and row.get("final_route") is True]
        self.assertEqual(1, len(final_rows))
        self.assertEqual(request_id, final_rows[0]["request_id"])
        child_operations = [row for row in rows if row.get("event") == "operation_started"]
        self.assertGreaterEqual(len(child_operations), 4)
        self.assertEqual(len(child_operations), len({row["operation_id"] for row in child_operations}))
        self.assertTrue(all(row["parent_operation_id"] == request_id for row in child_operations))

    def test_final_route_writer_failure_does_not_change_delivered_response(self):
        run_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            environment = {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
            }
            with patch.dict(os.environ, environment), \
                    patch.object(main_api, "parser", _Parser({"intent": "unsupported", "extracted_filters": {}})), \
                    patch.object(main_api, "retriever", Mock()), \
                    patch.object(main_api, "generator", Mock()), \
                    patch("rag_api.utils.diagnostic_stage_trace.append_route_observation",
                          side_effect=OSError("private final trace failure")):
                response = TestClient(main_api.app, raise_server_exceptions=False).post(
                    "/api/chat", json={"user_message": "Tell me an unrelated fact"},
                    headers={"X-Diagnostic-Run-ID": run_id},
                )
            self.assertEqual(200, response.status_code)
            self.assertEqual("unsupported", response.json()["status"])
            rows = [json.loads(line) for line in Path(environment["RAG_DIAGNOSTIC_ROUTE_EVENT_FILE"])
                    .read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(row.get("final_route") is True for row in rows))

    def test_intent_exception_returns_existing_http_error_and_one_sanitized_final_event(self):
        response, rows, _, _, _ = self._request_with_trace(
            "question", RuntimeError("private intent exception detail"),
        )
        self.assertEqual(500, response.status_code)
        final_rows = [row for row in rows if row.get("final_route") is True]
        self.assertEqual(1, len(final_rows))
        self.assertEqual("unknown", final_rows[0]["execution_kind"])
        self.assertNotIn("private intent exception detail", json.dumps(rows))

    def test_concurrent_backend_and_graph_requests_keep_trace_state_isolated(self):
        run_id = str(uuid.uuid4())
        parser_barrier = threading.Barrier(2)
        backend_entered = threading.Event()
        graph_entered = threading.Event()

        class InterleavedParser:
            def parse_intent(self, *, user_message, **_kwargs):
                parser_barrier.wait(timeout=5)
                if user_message == "backend request A01":
                    return {"intent": "combination_lookup", "extracted_filters": {}}
                return {"intent": "general", "extracted_filters": {"desired_program": "Program"}}

            def is_ready_for_db(self, _intent):
                return True

        class InterleavedRetriever:
            def retrieve_context(self, **_kwargs):
                graph_entered.set()
                if not backend_entered.wait(timeout=5):
                    raise AssertionError("Backend request did not reach its controlled boundary")
                return [{"evidence_id": "graph:concurrent"}]

            def format_for_llm(self, _records):
                return "context"

        class InterleavedGenerator:
            def generate_answer(self, **_kwargs):
                return "graph answer"

        def backend_get(_url, **_kwargs):
            backend_entered.set()
            if not graph_entered.wait(timeout=5):
                raise AssertionError("Graph request did not reach its controlled boundary")
            response = Mock()
            response.json.return_value = {"data": {
                "requestedCode": "A01", "labels": [{"code": "A01", "subjectDetails": "Toan, Ly, Anh"}]
            }}
            return response

        with tempfile.TemporaryDirectory() as directory:
            stage_path = Path(directory) / f"stage_events_{run_id}.jsonl"
            route_path = Path(directory) / f"route_events_{run_id}.jsonl"
            environment = {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(stage_path),
                "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(route_path),
            }
            with patch.dict(os.environ, environment), \
                    patch.object(main_api, "parser", InterleavedParser()), \
                    patch.object(main_api, "retriever", InterleavedRetriever()), \
                    patch.object(main_api, "generator", InterleavedGenerator()), \
                    patch("rag_api.main_api.requests.get", side_effect=backend_get):
                def send(question):
                    return TestClient(main_api.app, raise_server_exceptions=False).post(
                        "/api/chat", json={"user_message": question},
                        headers={"X-Diagnostic-Run-ID": run_id},
                    )

                with ThreadPoolExecutor(max_workers=2) as pool:
                    backend_future = pool.submit(send, "backend request A01")
                    graph_future = pool.submit(send, "graph request")
                    responses = [backend_future.result(timeout=10), graph_future.result(timeout=10)]

            rows = [json.loads(line) for line in route_path.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(all(response.status_code == 200 for response in responses))
        payloads = [response.json() for response in responses]
        request_ids = {payload["data"]["request_id"] for payload in payloads}
        self.assertEqual(2, len(request_ids))
        finals = [row for row in rows if row.get("event") == "retrieval_result" and row.get("final_route") is True]
        self.assertEqual(2, len(finals))
        self.assertEqual(request_ids, {row["request_id"] for row in finals})
        starts = [row for row in rows if row.get("event") == "operation_started"]
        self.assertEqual(2, len(starts))
        operation_ids = {row["operation_id"] for row in starts}
        self.assertEqual(2, len(operation_ids))
        self.assertTrue(all(row["parent_operation_id"] == row["request_id"] for row in starts))
        self.assertEqual(request_ids, {row["request_id"] for row in starts})
        for operation_id in operation_ids:
            operation_events = [row for row in rows if row.get("operation_id") == operation_id]
            self.assertEqual(1, len({row["request_id"] for row in operation_events}))
            event_names = [row["event"] for row in operation_events]
            self.assertLess(event_names.index("operation_started"), event_names.index("evidence_produced"))
            self.assertLess(event_names.index("operation_completed"), event_names.index("evidence_composed"))
        route_by_request = {row["request_id"]: row["execution_kind"] for row in finals}
        self.assertEqual({"backend_lookup", "graph_retrieval_generation"}, set(route_by_request.values()))


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""
main_api.py

FastAPI Application Entry Point & RAG Orchestrator API.
Connects IntentParser (Router), Neo4jRetriever (Hybrid Search), and RAGGenerator (Claude Sonnet).
Fully compliant with system architecture and coding guidelines.
"""

from contextlib import asynccontextmanager, nullcontext
import hashlib
import hmac
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from urllib.parse import quote, urlsplit
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError
import uvicorn
import requests

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.llm.intent_parser import IntentParser
from rag_api.llm.llm_client import RAGGenerator
from rag_api.llm.provider_errors import LLMServiceUnavailable
from rag_api.retrieval.graph_retriever import Neo4jRetriever, validate_evidence_dependencies
from rag_api.retrieval.errors import RetrievalUnavailableError
from rag_api.utils.config import settings
from rag_api.utils.logger import logger
from rag_api.utils.diagnostic_stage_trace import (
    DiagnosticStageTrace, append_route_decision, append_route_observation, record_response_stage,
    validate_route_event_configuration,
)

# Global singleton instances for AI services
parser: Optional[IntentParser] = None
retriever: Optional[Neo4jRetriever] = None
generator: Optional[RAGGenerator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    FastAPI Lifespan Manager.
    Initializes AI service instances once at startup and manages resource cleanup.
    """
    global parser, retriever, generator
    logger.info("Initializing Hybrid GraphRAG Admissions Advisor Services...")

    # Preserve services injected by tests or an embedding application. Only
    # instances created here are owned by this lifespan and eligible for
    # startup checks or cleanup.
    owned_parser = parser is None
    owned_retriever = retriever is None
    owned_generator = generator is None
    if owned_parser:
        parser = IntentParser()
    if owned_retriever:
        retriever = Neo4jRetriever()
    if owned_generator:
        generator = RAGGenerator()
    owned_parser_instance = parser if owned_parser else None
    owned_retriever_instance = retriever if owned_retriever else None
    owned_generator_instance = generator if owned_generator else None

    # Pre-verify Neo4j connectivity on startup
    if owned_retriever:
        try:
            retriever.get_driver()
            logger.info("Neo4j Database connection successfully verified on startup.")
        except Exception as e:
            logger.warning(f"Neo4j connection startup check warning: {e}")

    try:
        yield
    finally:
        logger.info("Shutting down Hybrid GraphRAG Admissions Advisor Services...")
        try:
            if owned_retriever_instance:
                owned_retriever_instance.close()
        finally:
            if owned_parser and parser is owned_parser_instance:
                parser = None
            if owned_retriever and retriever is owned_retriever_instance:
                retriever = None
            if owned_generator and generator is owned_generator_instance:
                generator = None


app = FastAPI(
    title="Hybrid GraphRAG Admissions Advisor API",
    description="API Hệ thống Tư vấn Hướng nghiệp Tuyển sinh Đại học kết hợp Neo4j, BGE-M3 và Claude Sonnet.",
    version="1.0.0",
    lifespan=lifespan
)

# Configure CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _stage_error_headers(request_id: str, stage: str, exc: Exception) -> Dict[str, str]:
    """Return correlation and sanitized error metadata without exception details."""
    failure_code = getattr(exc, "failure_code", "error")
    if failure_code not in {"timeout", "connect_timeout", "read_timeout", "provider_failure"}:
        failure_code = "error"
    headers = {
        "X-Request-ID": request_id,
        "X-Error-Stage": stage,
        "X-Error-Type": type(exc).__name__,
        "X-Error-Code": failure_code,
    }
    upstream_status = getattr(exc, "upstream_status_code", None)
    if isinstance(upstream_status, int) and not isinstance(upstream_status, bool) and 400 <= upstream_status <= 599:
        headers["X-Upstream-Status"] = str(upstream_status)
    trace = DiagnosticStageTrace.current()
    if trace is not None and trace.enabled:
        headers["X-Diagnostic-Correlation-ID"] = trace.correlation_id
    return headers


# Pydantic Schemas for Request / Response
class ChatRequest(BaseModel):
    user_message: str = Field(
        ...,
        description="Tin nhắn câu hỏi mới của người dùng",
        json_schema_extra={"example": "Em muốn học ngành Công nghệ thông tin trường Quy Nhơn điểm bao nhiêu?"}
    )
    context: List[Dict[str, str]] = Field(
        default_factory=list,
        max_length=40,
        description="Lịch sử ngữ cảnh hội thoại trước đó [{\"role\": \"user\"|\"assistant\", \"content\": \"...\"}]"
    )
    chat_history: Optional[List[Dict[str, str]]] = Field(
        default=None,
        max_length=40,
        description="Tên cũ của ngữ cảnh (tùy chọn dự phòng tương thích)"
    )
    score_threshold: Optional[float] = Field(
        default=None,
        description="Ngưỡng độ tương đồng tối thiểu cho vector search (mặc định từ settings.VECTOR_SIMILARITY_THRESHOLD = 0.65)"
    )
    def get_conversation_context(self) -> List[Dict[str, str]]:
        """Return conversation context supporting both 'context' and legacy 'chat_history' fields."""
        if self.context and len(self.context) > 0:
            return self.context
        return self.chat_history or []


class ChatResponse(BaseModel):
    status: str = Field(..., description="Trạng thái: success, incomplete, no_data, unsupported hoặc error")
    message: str = Field(..., description="Nội dung phản hồi cho người dùng")
    data: Optional[Dict[str, Any]] = Field(default=None, description="Dữ liệu chi tiết đính kèm (filters, records_count...)")


class _ResolvedComponent(BaseModel):
    model_config = ConfigDict(strict=True)

    type: str = Field(min_length=1)
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    subjectCode: str = Field(min_length=1)


class _ResolvedLabel(BaseModel):
    model_config = ConfigDict(strict=True)

    code: str = Field(min_length=1)
    subjectDetails: str = Field(min_length=1)
    sourceUrl: Optional[str] = None
    sourcePeriod: Optional[str] = None
    retrievedAt: Optional[str] = None


class _ResolvedMatch(BaseModel):
    model_config = ConfigDict(strict=True)

    combinationId: str = Field(min_length=1)
    combinationDisplay: str = Field(min_length=1)
    componentCount: int = Field(gt=0)
    components: List[_ResolvedComponent]
    labels: List[_ResolvedLabel]


class _CombinationResolveData(BaseModel):
    model_config = ConfigDict(strict=True)

    matches: List[_ResolvedMatch]
    resultScope: str = Field(min_length=1)


@app.post(
    "/api/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Xử lý câu hỏi RAG Tư vấn Tuyển sinh",
    description="Định tuyến câu hỏi, tra cứu Backend/Neo4j khi cần và trả bằng chứng cùng câu trả lời.",
    responses={
        400: {"description": "Request không hợp lệ"},
        502: {"description": "Backend trả về dữ liệu tra cứu tổ hợp sai hợp đồng"},
        503: {"description": "LLM, Backend hoặc Neo4j tạm thời không sẵn sàng"},
    },
)
def chat_endpoint(
    request: ChatRequest,
    diagnostic_run_id: Optional[str] = Header(default=None, alias="X-Diagnostic-Run-ID"),
    diagnostic_correlation_id: Optional[str] = Header(default=None, alias="X-Diagnostic-Correlation-ID"),
    diagnostic_run_token: Optional[str] = Header(default=None, alias="X-Diagnostic-Run-Token"),
) -> ChatResponse:
    """Establish diagnostic correlation before running the existing chat flow."""
    strict_run_isolation = os.getenv(
        "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED", "false"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if strict_run_isolation:
        configured_run_id = os.getenv("RAG_DIAGNOSTIC_RUN_ID", "").strip()
        configured_token = os.getenv("RAG_DIAGNOSTIC_PROXY_TOKEN", "")
        if (not _is_uuid4(configured_run_id) or diagnostic_run_id != configured_run_id
                or not configured_token or not isinstance(diagnostic_run_token, str)
                or not hmac.compare_digest(diagnostic_run_token, configured_token)
                or os.getenv("RAG_DIAGNOSTIC_TRACE_ENABLED", "false").strip().casefold()
                in {"0", "false", "no", "off", ""}
                or not os.getenv("RAG_DIAGNOSTIC_TRACE_FILE", "").strip()
                or not validate_route_event_configuration(
                    os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", ""), configured_run_id)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Diagnostic run authorization or tracing is unavailable")
    request_id = str(uuid.uuid4())
    run_id = diagnostic_run_id if isinstance(diagnostic_run_id, str) and _is_uuid4(diagnostic_run_id) else str(uuid.uuid4())
    correlation_id = (diagnostic_correlation_id if isinstance(diagnostic_correlation_id, str)
                      and _is_uuid4(diagnostic_correlation_id) else str(uuid.uuid4()))
    trace = DiagnosticStageTrace(run_id, correlation_id, request_id)
    with trace:
        return _chat_endpoint_impl(request, request_id, trace)


def _is_uuid4(value: str) -> bool:
    try:
        return uuid.UUID(value).version == 4
    except (ValueError, AttributeError, TypeError):
        return False


def _chat_endpoint_impl(request: ChatRequest, request_id: str, trace: DiagnosticStageTrace) -> ChatResponse:
    """
    POST /api/chat Orchestrator Endpoint.

    Synchronous external/database clients run in FastAPI's worker pool for this sync route.
    """
    if not request.user_message or not request.user_message.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tin nhắn người dùng không được để trống."
        )

    if parser is None or retriever is None or generator is None:
        logger.error("AI Services are not properly initialized.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Hệ thống AI chưa sẵn sàng. Vui lòng thử lại sau."
        )

    conversation_context = _prior_turns(request)
    started = time.perf_counter()
    try:
        with trace.stage("intent"):
            intent_result = parser.parse_intent(
                user_message=request.user_message,
                context=conversation_context,
                request_id=request_id,
            )
        with trace.stage("entity_resolution"):
            intent_result = _apply_current_turn_precedence(
                intent_result, request.user_message, conversation_context
            )
            intent_result = _apply_deterministic_route_contract(intent_result, request.user_message)
            intent_result = _resolve_explicit_school_filter(
                intent_result, request.user_message, conversation_context
            )
            intent_result = _resolve_explicit_graph_group_scope(
                intent_result, request.user_message, conversation_context,
            )
        intent = intent_result.get("intent", "unsupported")
        extracted_filters = intent_result.get("extracted_filters", {})
        logger.info(
            "chat request_id=%s intent=%s filter_keys=%s high_school_count=%d history_turns=%d intent_ms=%.1f",
            request_id, intent,
            sorted(key for key, value in extracted_filters.items() if value not in (None, "", [], {}))
            if isinstance(extracted_filters, dict) else [],
            len(intent_result.get("high_schools", [])), len(conversation_context),
            (time.perf_counter() - started) * 1000,
        )
        generic_guarantee_answer = _generic_subject_overlap_guarantee_answer(
            intent_result, request.user_message,
        )
        if generic_guarantee_answer:
            if trace.enabled and os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip():
                append_route_decision({
                    "run_id": trace.run_id, "correlation_id": trace.correlation_id,
                    "request_id": request_id,
                    "decision_branch": _diagnostic_decision_branch(intent),
                    "filter_keys": list(extracted_filters) if isinstance(extracted_filters, dict) else [],
                })
            return _chat_result(
                "success", generic_guarantee_answer, request_id,
                intent_result.get("intent", "pathway_lookup"),
                data={
                "response_mode": "answer",
                    "eligibility_conclusion": "not_guaranteed_by_subject_overlap",
                    "retrieval_executed": False,
                    "generation_executed": False,
                },
            )
    except LLMServiceUnavailable as exc:
        logger.warning("chat request_id=%s stage=intent failure=provider_unavailable error_type=%s duration_ms=%.1f",
                       request_id, type(exc).__name__, (time.perf_counter() - started) * 1000)
        raise HTTPException(status_code=503, detail="Dịch vụ tư vấn tạm thời chưa xử lý được yêu cầu.", headers=_stage_error_headers(request_id, "intent", exc)) from exc
    except Exception as exc:
        logger.warning("chat request_id=%s stage=intent failure=error error_type=%s duration_ms=%.1f",
                       request_id, type(exc).__name__, (time.perf_counter() - started) * 1000)
        raise HTTPException(status_code=500, detail="Không thể phân tích yêu cầu tư vấn.", headers=_stage_error_headers(request_id, "intent", exc)) from exc

    assistant_group_codes = _prior_assistant_group_codes(conversation_context)
    has_singular_group_reference = _has_singular_group_reference(request.user_message)
    if (has_singular_group_reference and len(assistant_group_codes) > 1
            and not _explicit_group_code_in_message(request.user_message)):
        options = ", ".join(item["code"] for item in assistant_group_codes)
        return _chat_result(
            "incomplete",
            f"Em đang nhắc đến nhiều nhóm ({options}). Em muốn hỏi nhóm nào?",
            request_id, intent_result.get("intent", "pathway_lookup"),
            data={"response_mode": "clarification",
                  "clarification_reason": "ambiguous_group_reference",
                  "requested_group_codes": assistant_group_codes,
                  "retrieval_executed": False,
                  "eligibility_conclusion": "not_evaluated"},
        )
    if (has_singular_group_reference and len(assistant_group_codes) == 1
            and not _explicit_group_code_in_message(request.user_message)):
        parsed_group_codes = intent_result.get("requested_group_codes")
        parsed_group_codes = parsed_group_codes if isinstance(parsed_group_codes, list) else []
        assistant_group_code = str(assistant_group_codes[0].get("code", "")).strip().casefold()
        parsed_codes = {
            str(item.get("code", "")).strip().casefold()
            for item in parsed_group_codes if isinstance(item, dict) and item.get("code")
        }
        if parsed_codes and assistant_group_code not in parsed_codes:
            return _chat_result(
                "incomplete",
                "Mình thấy mã nhóm trong ngữ cảnh trước chưa khớp với nhóm được phân tích cho câu này. Em xác nhận lại mã nhóm nhé.",
                request_id, intent_result.get("intent", "pathway_lookup"),
                data={"response_mode": "clarification",
                      "clarification_reason": "dialogue_group_scope_conflict",
                      "missing_eligibility_context": ["subject_group"],
                      "requested_group_codes": assistant_group_codes,
                      "retrieval_executed": False,
                      "eligibility_conclusion": "not_evaluated"},
            )
        if not parsed_group_codes:
            intent_result["requested_group_codes"] = assistant_group_codes
        extracted_filters = intent_result.get("extracted_filters")
        extracted_filters = extracted_filters if isinstance(extracted_filters, dict) else {}
        scoped_year = intent_result.get("academic_year") or extracted_filters.get("academic_year")
        if not scoped_year:
            scoped_year = _academic_year_from_group_context(request.user_message)
        if not scoped_year:
            for turn in reversed(conversation_context):
                if turn.get("role") == "user":
                    scoped_year = _academic_year_from_group_context(str(turn.get("content", "")))
                    break
        if not scoped_year:
            return _chat_result(
                "incomplete",
                "Mình đã xác định được nhóm đang nhắc tới, nhưng chưa rõ năm học áp dụng. Em cho mình biết năm học của nhóm nhé.",
                request_id, intent_result.get("intent", "pathway_lookup"),
                data={"response_mode": "clarification",
                      "clarification_reason": "academic_year",
                      "missing_eligibility_context": ["academic_year"],
                      "requested_group_codes": assistant_group_codes,
                      "retrieval_executed": False,
                      "eligibility_conclusion": "not_evaluated"},
            )
        intent_result["academic_year"] = str(scoped_year)

    school_resolution = intent_result.get("school_resolution")
    if isinstance(school_resolution, dict):
        resolution_status = school_resolution.get("status")
        if resolution_status == "catalog_unavailable":
            raise HTTPException(
                status_code=503,
                detail="Danh mục trường THPT tạm thời chưa sẵn sàng để xác minh tên trường.",
            )
        if resolution_status in {"unknown", "ambiguous"}:
            if resolution_status == "ambiguous":
                message = (
                    "Mình tìm thấy nhiều trường có tên gần giống. Em cho biết tỉnh/thành "
                    "hoặc mã trường để mình tra đúng nhóm môn nhé."
                )
            else:
                message = (
                    "Mình chưa xác định được trường trong danh mục hiện có. Em kiểm tra lại "
                    "tên trường hoặc gửi mã trường giúp mình nhé."
                )
            return _chat_result(
                "incomplete", message, request_id, intent_result.get("intent", "pathway_lookup"),
                data={"query_resolution": school_resolution,
                      "evidence_source": "backend_high_school_catalog"},
            )

    graph_group_resolution = intent_result.get("graph_group_scope_resolution")
    if isinstance(graph_group_resolution, dict) and graph_group_resolution.get("status") != "resolved":
        if graph_group_resolution.get("status") == "missing_school":
            return _chat_result(
                "incomplete", "Em cho mình biết trường THPT cần tra nhé.",
                request_id, intent_result.get("intent", "pathway_lookup"),
                data={"graph_group_scope_resolution": graph_group_resolution,
                      "evidence_source": "backend_school_year_group_catalog"},
            )
        if graph_group_resolution.get("status") == "missing_academic_year":
            return _chat_result(
                "incomplete",
                "Em cho mình biết năm học áp dụng cho nhóm môn ở trường này nhé.",
                request_id, intent_result.get("intent", "pathway_lookup"),
                data={"graph_group_scope_resolution": graph_group_resolution,
                      "school_resolution": school_resolution,
                      "evidence_source": "backend_school_year_group_catalog"},
            )
        if graph_group_resolution.get("status") == "catalog_unavailable":
            exc = requests.RequestException()
            raise HTTPException(
                status_code=503, detail="Danh mục nhóm môn tạm thời chưa sẵn sàng để xác minh phạm vi.",
                headers=_stage_error_headers(request_id, "entity_resolution", exc),
            ) from exc
        return _chat_result(
            "incomplete",
            "Mình chưa xác minh được duy nhất nhóm môn trong danh mục của trường và năm học đã nêu. Em xác nhận lại mã nhóm giúp mình nhé.",
            request_id, intent_result.get("intent", "pathway_lookup"),
            data={"graph_group_scope_resolution": graph_group_resolution,
                  "school_resolution": school_resolution,
                  "evidence_source": "backend_school_year_group_catalog"},
        )

    eligibility_clarification = _missing_pathway_eligibility_context(
        intent_result, request.user_message,
    )
    if eligibility_clarification:
        return _chat_result(
            "incomplete", eligibility_clarification["message"], request_id,
            intent_result.get("intent", "pathway_lookup"),
            data={
                "response_mode": "clarification",
                "clarification_reason": "eligibility_context_missing",
                "missing_eligibility_context": eligibility_clarification["missing"],
                "known_eligibility_context": eligibility_clarification["known"],
                "eligibility_conclusion": "not_evaluated",
            },
        )

    with trace.stage("routing"):
        intent = intent_result.get("intent", "unsupported")
    if trace.enabled and os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip():
        append_route_decision({
            "run_id": trace.run_id, "correlation_id": trace.correlation_id,
            "request_id": request_id, "decision_branch": _diagnostic_decision_branch(intent),
            "filter_keys": list(extracted_filters) if isinstance(extracted_filters, dict) else [],
        })
    if intent == "greeting":
        return _chat_result("success", "Chào em! Mình có thể giúp tìm hiểu nhóm môn của trường THPT, đối chiếu sở thích và năng lực, hoặc xem dữ liệu ngành có liên kết với tổ hợp nào.", request_id, intent)
    if intent == "unsupported":
        return _chat_result("unsupported", "Mình hỗ trợ tư vấn nhóm môn THPT và tra cứu đường liên kết tới tổ hợp, trường, ngành trong dữ liệu hiện có.", request_id, intent)
    if intent == "knowledge":
        return _answer_knowledge_question(request, intent_result, request_id, intent)
    if intent == "combination_lookup":
        return _lookup_combination(
            intent_result,
            request_id,
            question=request.user_message,
            context=conversation_context,
        )
    if intent == "school_group_lookup":
        return _lookup_school_groups(intent_result, request_id, context=conversation_context,
                                     current_message=request.user_message)
    if intent == "subject_group_advice" and "school_group_lookup" in intent_result.get("requested_tasks", []):
        return _answer_school_group_lookup_and_advice(
            intent_result, request_id, context=conversation_context,
            current_message=request.user_message,
        )
    if intent == "subject_group_advice" and not (
        intent_result.get("extracted_filters", {}).get("high_school")
        or intent_result.get("high_schools")
    ):
        return _advise_without_school(intent_result, request_id)
    if intent in {"subject_group_advice", "subject_group_comparison"}:
        return _compare_subject_groups(intent_result, request_id, context=conversation_context,
                                      current_message=request.user_message)
    query_resolution: Dict[str, Any] = {}
    if intent in {"general", "pathway_lookup"} and isinstance(extracted_filters, dict):
        combination_code = extracted_filters.get("combination_code")
        if combination_code:
            resolved_combination, resolution_or_response = _resolve_combination_for_pathway(
                str(combination_code), request_id
            )
            if isinstance(resolution_or_response, ChatResponse):
                return resolution_or_response
            query_resolution = resolution_or_response or {}
            if resolved_combination:
                query_resolution = {**query_resolution, "canonical_combination_id": resolved_combination}
                extracted_filters = {**extracted_filters, "canonical_combination_id": resolved_combination}
                intent_result = {**intent_result, "extracted_filters": extracted_filters}
    if intent not in {"general", "pathway_lookup"} or not parser.is_ready_for_db(intent_result):
        return _chat_result("incomplete", intent_result.get("reply_to_user") or
                            "Em cho biết mã tổ hợp, trường THPT, hoặc ngành/lĩnh vực em muốn tìm hiểu nhé.",
                            request_id, intent)

    try:
        retrieval_started = time.perf_counter()
        query_filters = _normalize_query_filters(intent_result, request.user_message)
        if query_filters.get("comparison_scopes"):
            scope_resolution = _resolve_comparison_scopes_from_catalog(query_filters)
            if scope_resolution.get("status") != "resolved":
                return _chat_result(
                    "incomplete",
                    "Mình chưa xác minh được riêng từng cặp trường, năm, nhóm và ngành. Em xác nhận lại phạm vi của cả hai cặp giúp mình nhé.",
                    request_id, intent,
                    data={"response_mode": "clarification",
                          "clarification_reason": "comparison_scope_unresolved",
                          "comparison_scope_resolution": scope_resolution,
                          "retrieval_executed": False,
                          "eligibility_conclusion": "not_evaluated"},
                )
            query_filters["comparison_scopes"] = scope_resolution["scopes"]
        with trace.stage("retrieval"):
            scoped_graph_validation = None
            if query_filters.get("comparison_scopes"):
                records, scoped_graph_validation = _retrieve_pathway_comparison_scopes(
                    retriever, query_filters, top_k=21, score_threshold=request.score_threshold,
                )
            else:
                records = _fastapi_graph_retrieve(retriever, query_filters, top_k=21,
                                                  score_threshold=request.score_threshold)
        evidence_truncated = len(records) > 20
        omitted_evidence_ids = [str(row.get("evidence_id") or row.get("id"))
                                for row in records[20:] if isinstance(row, dict)]
        records = records[:20]
        unresolved_path_dependencies = [
            row.get("evidence_id") or row.get("program_code") or "unknown-record"
            for row in records if isinstance(row, dict)
            and isinstance(row.get("evidence_dependency_validation"), dict)
            and row["evidence_dependency_validation"].get("status") != "complete"
        ]
        if scoped_graph_validation:
            unresolved_path_dependencies.extend(
                f"comparison_scope:{scope['scope_index']}:{'no_matching_records' if not scope['record_count'] else 'incomplete'}"
                for scope in scoped_graph_validation["scopes"] if not scope["complete"]
            )
        logger.info("chat request_id=%s stage=retrieval_ms=%.1f result_count=%d", request_id,
                    (time.perf_counter() - retrieval_started) * 1000, len(records))
    except RetrievalUnavailableError as exc:
        logger.warning("chat request_id=%s stage=retrieval failure=unavailable error_type=%s duration_ms=%.1f",
                       request_id, type(exc).__name__, (time.perf_counter() - retrieval_started) * 1000)
        raise HTTPException(status_code=503, detail="Dịch vụ tra cứu dữ liệu tạm thời chưa sẵn sàng.", headers=_stage_error_headers(request_id, "retrieval", exc)) from exc
    except Exception as exc:
        logger.warning("chat request_id=%s stage=retrieval failure=error error_type=%s duration_ms=%.1f",
                       request_id, type(exc).__name__, (time.perf_counter() - retrieval_started) * 1000)
        raise HTTPException(status_code=500, detail="Retrieval failed.", headers=_stage_error_headers(request_id, "retrieval", exc)) from exc
    if not records:
        if query_filters.get("answer_scope") == "subject_overlap":
            return _chat_result(
                "no_data",
                "Lượt truy xuất này không trả về bằng chứng phù hợp cho phạm vi đã nêu. Kết quả rỗng "
                "không chứng minh rằng bản ghi tương ứng không tồn tại trong cơ sở dữ liệu.",
                request_id, intent,
                data={"filters": query_filters, "query_resolution": query_resolution,
                      "school_resolution": school_resolution,
                      "graph_group_scope_resolution": intent_result.get("graph_group_scope_resolution"),
                      "retrieved_count": 0, "retrieved_context": "", "evidence": [],
                      "evidence_source": "neo4j_graph", "evidence_truncated": False,
                      "retrieval_executed": True, "generation_executed": False,
                      "graph_path_validation": scoped_graph_validation or _graph_path_validation_payload(
                          [], query_filters),
                      "answer_completeness": "partial"},
            )
        return _chat_result("no_data", "Chưa tìm thấy dữ liệu phù hợp với trường, tổ hợp hoặc ngành em nêu. Em kiểm tra lại tên/mã hoặc phạm vi năm học nhé.", request_id, intent,
                            data={"filters": _normalize_query_filters(intent_result, request.user_message),
                                  "query_resolution": query_resolution,
                                  "school_resolution": school_resolution,
                                  "graph_group_scope_resolution": intent_result.get("graph_group_scope_resolution"),
                                  "retrieved_count": 0, "retrieved_context": "",
                                  "evidence": [], "evidence_source": "neo4j_graph", "evidence_truncated": False,
                                  "graph_path_validation": scoped_graph_validation or _graph_path_validation_payload(
                                      [], _normalize_query_filters(intent_result, request.user_message)),
                                  "generation_executed": False})

    if query_filters.get("answer_scope") == "subject_overlap":
        overlap_answer = _build_subject_overlap_answer(records, query_filters, request.user_message)
        graph_validation = scoped_graph_validation or _graph_path_validation_payload(records, query_filters)
        if overlap_answer is None:
            target_group = query_filters.get("subject_group_code")
            scope_label = f" cho {target_group}" if target_group else " trong phạm vi trường/năm đã nêu"
            return _chat_result(
                "incomplete",
                "Các bản ghi trả về chưa cung cấp bằng chứng giao nhau đã xác minh với đủ thông tin phạm vi" +
                scope_label + ". Điều này không chứng minh rằng bản ghi hoặc môn tương ứng không tồn tại "
                "trong cơ sở dữ liệu.",
                request_id, intent,
                data={"filters": query_filters, "school_resolution": school_resolution,
                      "graph_group_scope_resolution": intent_result.get("graph_group_scope_resolution"),
                      "retrieved_count": len(records), "retrieved_evidence": records, "evidence": [],
                      "evidence_source": "neo4j_graph", "retrieval_executed": True,
                      "generation_executed": False, "graph_path_validation": graph_validation,
                      "answer_completeness": "partial"},
            )
        answer_text, scoped_records = overlap_answer
        unresolved = [
            row.get("evidence_id") or row.get("program_code") or "unknown-record"
            for row in scoped_records if isinstance(row, dict)
            and isinstance(row.get("evidence_dependency_validation"), dict)
            and row["evidence_dependency_validation"].get("status") != "complete"
        ]
        return _chat_result(
            "incomplete", answer_text, request_id, intent,
            data={"filters": query_filters, "school_resolution": school_resolution,
                  "graph_group_scope_resolution": intent_result.get("graph_group_scope_resolution"),
                  "retrieved_count": len(scoped_records),
                  "context_evidence_ids": [row.get("evidence_id") for row in scoped_records
                                            if row.get("evidence_id")],
                  "evidence": scoped_records, "evidence_source": "neo4j_graph",
                  "retrieval_executed": True, "generation_executed": False,
                  "graph_path_validation": graph_validation, "answer_completeness": "partial",
                  "unresolved_path_dependencies": unresolved},
        )

    with trace.stage("context_formatting"):
        formatted_context = retriever.format_for_llm(records)
        generation_context = formatted_context.strip()
    try:
        generation_started = time.perf_counter()
        with trace.stage("generation"):
            answer_text = generator.generate_answer(
                user_query=request.user_message,
                graph_context=generation_context,
                context=conversation_context,
                request_id=request_id,
            )
        logger.info("chat request_id=%s stage=generation_ms=%.1f", request_id,
                    (time.perf_counter() - generation_started) * 1000)
    except LLMServiceUnavailable as exc:
        logger.warning("chat request_id=%s stage=generation failure=provider_unavailable error_type=%s duration_ms=%.1f",
                       request_id, type(exc).__name__, (time.perf_counter() - generation_started) * 1000)
        raise HTTPException(status_code=503, detail="Dịch vụ tạo câu trả lời tạm thời chưa sẵn sàng.", headers=_stage_error_headers(request_id, "generation", exc)) from exc
    except Exception as exc:
        logger.warning("chat request_id=%s stage=generation failure=error error_type=%s duration_ms=%.1f",
                       request_id, type(exc).__name__, (time.perf_counter() - generation_started) * 1000)
        raise HTTPException(status_code=500, detail="Generation failed.", headers=_stage_error_headers(request_id, "generation", exc)) from exc
    return _chat_result("success", answer_text, request_id, intent,
                        data={"filters": query_filters, "query_resolution": query_resolution,
                              "school_resolution": school_resolution,
                              "graph_group_scope_resolution": intent_result.get("graph_group_scope_resolution"),
                              "retrieved_count": len(records), "context_evidence_ids": [
                                  row.get("evidence_id") for row in records if isinstance(row, dict)
                                  and row.get("evidence_id")],
                              "retrieved_context": generation_context, "evidence": records,
                              # This is the exact graph_context argument passed above. The
                              # evaluator verifies its digest against retrieved_context;
                              # no second copy of the context is exposed in the response.
                              "generation_context_exact": True,
                              "generation_executed": True,
                              "generation_context_sha256": hashlib.sha256(
                                  generation_context.encode("utf-8")).hexdigest(),
                              "evidence_source": "neo4j_graph",
                              "graph_path_validation": scoped_graph_validation or _graph_path_validation_payload(records, query_filters),
                              "evidence_truncated": evidence_truncated,
                               "omitted_evidence_ids": omitted_evidence_ids,
                               "answer_completeness": "partial" if unresolved_path_dependencies else "complete",
                               "unresolved_path_dependencies": unresolved_path_dependencies})


def _explicit_requested_program_names(user_message: str) -> List[str]:
    """Extract separately named programs only when the user explicitly joins them with 'and'."""
    if not isinstance(user_message, str) or not user_message.strip():
        return []
    clauses = re.split(r"\s+(?:và|and)\s+(?=(?:ngành|program|major)\s+)", user_message,
                       flags=re.IGNORECASE)
    if len(clauses) < 2:
        return []
    names = []
    for clause in clauses:
        match = re.search(r"\b(?:ngành|program|major)\s+(.+)$", clause, flags=re.IGNORECASE)
        if not match:
            continue
        name = re.sub(r"[?!.。]+$", "", match.group(1)).strip()
        name = re.sub(r"\s+(?:không|ạ|nhé|nha)$", "", name, flags=re.IGNORECASE).strip()
        if name:
            names.append(name)
    return list(dict.fromkeys(names)) if len(names) >= 2 else []


def _retrieve_pathway_comparison_scopes(
    retriever_instance: Any, filters: Dict[str, Any], top_k: int,
    score_threshold: Optional[float] = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Retrieve each requested pathway pair independently and validate each scope separately."""
    scopes = filters.get("comparison_scopes")
    if not isinstance(scopes, list) or not scopes:
        return [], _graph_path_validation_payload([])
    shared_filters = {key: value for key, value in filters.items()
                      if key not in {"comparison_scopes", "comparison_scope_semantics"}}
    all_records: List[Dict[str, Any]] = []
    scope_results = []
    for index, scope in enumerate(scopes):
        if not isinstance(scope, dict):
            continue
        scope_filters = {**shared_filters, **scope}
        if filters.get("high_school_code"):
            scope_filters["high_school_code"] = filters["high_school_code"]
        scope_filters["answer_scope"] = "subject_overlap"
        rows = _fastapi_graph_retrieve(retriever_instance, scope_filters, top_k, score_threshold)
        rows = [dict(row, requested_comparison_scope=index)
                for row in rows if isinstance(row, dict)]
        validation = _graph_path_validation_payload(rows, scope_filters)
        scope_results.append({
            "scope_index": index,
            "filters": scope_filters,
            "record_count": len(rows),
            "complete": bool(validation["complete"]),
            "graph_path_validation": validation,
        })
        all_records.extend(rows)

    return all_records, {
        "schema_version": 2,
        "source": "neo4j_graph",
        "path_semantics": "directed_edge_set",
        "scope_semantics": "all_scopes_required",
        "complete": bool(scope_results) and all(scope["complete"] for scope in scope_results),
        "scopes": scope_results,
        "records": [record for scope in scope_results
                    for record in scope["graph_path_validation"]["records"]],
    }


def _fastapi_graph_retrieve(retriever_instance: Any, filters: Dict[str, Any], top_k: int,
                            score_threshold: Optional[float]) -> List[Dict[str, Any]]:
    def retrieve_and_attach_provenance() -> List[Dict[str, Any]]:
        rows = retriever_instance.retrieve_context(filters=filters, top_k=top_k,
                                                   score_threshold=score_threshold)
        provenance_attacher = getattr(retriever_instance, "attach_evidence_provenance", None)
        if callable(provenance_attacher):
            enriched = provenance_attacher(rows, filters=filters)
            if isinstance(enriched, list):
                rows = enriched
        return rows

    trace = DiagnosticStageTrace.current()
    if trace and trace.enabled:
        with trace.retrieval_operation("fastapi_graph") as operation:
            rows = retrieve_and_attach_provenance()
            operation.evidence_produced(_safe_evidence_reference_ids(rows))
            return rows
    return retrieve_and_attach_provenance()


def _resolve_comparison_scopes_from_catalog(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve every pair's school/year/group against the Backend catalog before Graph retrieval."""
    scopes = filters.get("comparison_scopes")
    scopes = scopes if isinstance(scopes, list) else []
    school_code = str(filters.get("high_school_code") or "").strip()
    canonical_school_name = str(filters.get("high_school") or "").strip()
    if len(scopes) < 2 or not school_code or not canonical_school_name:
        return {"status": "unresolved", "reason": "school_identity_not_resolved", "scopes": []}

    resolved = []
    for index, scope in enumerate(scopes):
        if not isinstance(scope, dict):
            return {"status": "unresolved", "reason": "invalid_scope_record", "scope_index": index,
                    "scopes": resolved}
        if _school_catalog_match_key(str(scope.get("high_school") or "")) != _school_catalog_match_key(canonical_school_name):
            return {"status": "unresolved", "reason": "school_scope_mismatch", "scope_index": index,
                    "scopes": resolved}
        year = str(scope.get("academic_year") or "").strip()
        group_code = str(scope.get("subject_group_code") or "").strip()
        try:
            years = _backend_get(f"/api/v1/high-schools/{quote(school_code, safe='')}/academic-years")
            groups = _backend_get(
                f"/api/v1/high-schools/{quote(school_code, safe='')}/subject-groups?academicYear={quote(year, safe='')}"
            ) if isinstance(years, list) and year in {str(value) for value in years} else None
        except requests.RequestException:
            return {"status": "catalog_unavailable", "school_code": school_code,
                    "academic_year": year, "scope_index": index, "scopes": resolved}
        if not isinstance(years, list) or year not in {str(value) for value in years}:
            return {"status": "unresolved", "reason": "academic_year_not_in_catalog", "scope_index": index,
                    "school_code": school_code, "academic_year": year, "scopes": resolved}
        if not isinstance(groups, list):
            return {"status": "catalog_unavailable", "school_code": school_code,
                    "academic_year": year, "scope_index": index, "scopes": resolved}
        selected, missing, details = _filter_requested_groups(
            groups, [{"code": group_code}], school_code=school_code, academic_year=year,
        )
        if len(selected) != 1 or missing or not details or details[0].get("status") not in {"exact", "resolved_alias"}:
            return {"status": "unresolved", "reason": "group_not_uniquely_resolved", "scope_index": index,
                    "school_code": school_code, "academic_year": year,
                    "group_code": group_code, "group_resolution": details, "scopes": resolved}
        resolved.append({
            **scope,
            "high_school": canonical_school_name,
            "high_school_code": school_code,
            "academic_year": year,
            "subject_group_code": str(selected[0].get("groupCode") or "").strip(),
            "group_resolution": details[0],
        })
    return {"status": "resolved", "source": "backend_school_year_group_catalog", "scopes": resolved}


def _normalize_query_filters(intent_result: Dict[str, Any], user_message: str = "") -> Dict[str, Any]:
    """Map parser fields to the stable retrieval contract; public codes resolve separately."""
    filters = intent_result.get("extracted_filters", {})
    filters = filters if isinstance(filters, dict) else {}
    requested_program_names = filters.get("requested_program_names")
    if not isinstance(requested_program_names, list):
        requested_program_names = _explicit_requested_program_names(user_message)
    desired_program = filters.get("desired_program")
    if not requested_program_names and isinstance(desired_program, str) and desired_program.strip():
        requested_program_names = [desired_program.strip()]

    requested_pairs = intent_result.get("requested_pathway_pairs")
    requested_pairs = requested_pairs if isinstance(requested_pairs, list) else []
    comparison_scopes = []
    for pair in requested_pairs[:4]:
        if not isinstance(pair, dict):
            continue
        school, year = pair.get("high_school"), pair.get("academic_year")
        group_code, program = pair.get("subject_group_code"), pair.get("program_name")
        if not all(isinstance(value, str) and value.strip()
                   for value in (school, year, group_code, program)):
            continue
        comparison_scopes.append({
            "high_school": school.strip(),
            "academic_year": year.strip(),
            "subject_group_code": group_code.strip().upper(),
            "program_names": [program.strip()],
            "keyword": program.strip(),
        })

    answer_scope = None
    scope_source = intent_result.get("answer_scope_source")
    requested_scope = intent_result.get("answer_scope")
    if len(comparison_scopes) >= 2:
        answer_scope = "subject_overlap"
    elif (scope_source in {"deterministic_explicit_scope", "deterministic_pathway_default"}
          and requested_scope in {"subject_overlap", "full_path"}):
        answer_scope = requested_scope
    elif intent_result.get("intent") == "pathway_lookup":
        answer_scope = "full_path"

    normalized = {
        "keyword": desired_program or filters.get("keyword"),
        "program_names": requested_program_names,
        "university": filters.get("university"),
        "high_school": filters.get("high_school"),
        "high_school_code": filters.get("high_school_code"),
        "score_max": filters.get("score_max"),
        "combination": filters.get("canonical_combination_id"),
        "admission_year": filters.get("admission_year"),
        "method_code": filters.get("method_code"),
        "academic_year": intent_result.get("academic_year") or filters.get("academic_year"),
        "subject_group_code": (intent_result.get("resolved_subject_group_code")
                               if isinstance(intent_result.get("graph_group_scope_resolution"), dict)
                               and intent_result["graph_group_scope_resolution"].get("status") == "resolved"
                               else None),
        "answer_scope": answer_scope,
    }
    if len(comparison_scopes) >= 2:
        normalized["comparison_scopes"] = comparison_scopes
        normalized["comparison_scope_semantics"] = "all_scopes_required"
    return {key: value for key, value in normalized.items()
            if value is not None and value != "" and value != []}


def _official_admission_source_url(university_code: str, source_url: Any) -> Optional[str]:
    """Return a source URL only when it uses the configured university's HTTPS domain."""
    if not isinstance(source_url, str) or not source_url.strip():
        return None
    try:
        source = urlsplit(source_url.strip())
        if (source.scheme.casefold() != "https" or not source.hostname or source.username or source.password
                or source.port not in {None, 443}):
            return None
        with open(os.path.join(BASE_DIR, "config", "universities.json"), "r", encoding="utf-8") as handle:
            universities = json.load(handle)
        entry = universities.get(str(university_code or "").strip().upper())
        if not isinstance(entry, dict):
            return None
        official_host = urlsplit(str(entry.get("website") or "")).hostname
        source_host = source.hostname.casefold().rstrip(".")
        official_host = (official_host or "").casefold().rstrip(".")
        if not official_host or not (source_host == official_host or source_host.endswith("." + official_host)):
            return None
        return source_url.strip()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _verified_admission_acceptances(
    record: Dict[str, Any], filters: Dict[str, Any], explicit_tuple_members: set[str]
) -> List[tuple[Dict[str, Any], Dict[str, Any], str]]:
    """Find exact, scoped admission acceptances independently of school-path completeness."""
    if not explicit_tuple_members:
        return []
    university_code = str(record.get("university_code") or "").strip().upper()
    program_code = str(record.get("program_code") or "").strip()
    program_name = str(record.get("program_name") or "").strip()
    university_name = str(record.get("university_name") or "").strip()
    requested_year = str(filters.get("admission_year") or "").strip()
    if not university_code or not program_code or not program_name or not university_name or not requested_year:
        return []

    requested_university_code = str(filters.get("university_code") or "").strip().upper()
    requested_university = filters.get("university")
    if requested_university_code:
        if requested_university_code != university_code:
            return []
    elif not requested_university:
        return []
    if requested_university:
        requested = _normalize_label(str(requested_university))
        observed = _normalize_label(university_name)
        if not requested or not observed or requested != observed:
            return []
    requested_program_code = str(filters.get("program_code") or "").strip()
    requested_programs = filters.get("program_names") or []
    if isinstance(requested_programs, str):
        requested_programs = [requested_programs]
    if requested_program_code:
        if requested_program_code != program_code:
            return []
    elif not requested_programs:
        return []
    if requested_programs:
        observed = _normalize_label(program_name)
        if not observed or not any(
            _normalize_label(str(value)) and _normalize_label(str(value)) == observed
            for value in requested_programs
        ):
            return []

    combinations = [item for item in record.get("combinations") or [] if isinstance(item, dict)]
    if not combinations and record.get("combination_id"):
        combinations = [{
            "id": record.get("combination_id"),
            "required_subjects": record.get("required_subjects") or [],
            "required_subject_codes": record.get("required_subject_codes") or [],
            "acceptance_records": record.get("acceptance_records") or [],
        }]

    # Duplicate rows for one canonical combination must agree before one can support a claim.
    identity_codes: Dict[str, set[tuple[str, ...]]] = {}
    for item in combinations:
        combination_id = str(item.get("id") or "").strip()
        if combination_id:
            codes = tuple(sorted(str(code).strip().upper() for code in item.get("required_subject_codes") or []
                                 if isinstance(code, str) and code.strip()))
            identity_codes.setdefault(combination_id, set()).add(codes)

    observed_edges = {
        (str(edge.get("relationship_id") or ""), str(edge.get("from") or ""), str(edge.get("to") or ""))
        for edge in record.get("evidence_path") or [] if isinstance(edge, dict)
    }
    conflicting_edge_ids = {
        str(conflict.get("relationship_id") or "")
        for conflict in record.get("evidence_path_conflicts") or [] if isinstance(conflict, dict)
    }
    result = []
    for item in combinations:
        combination_id = str(item.get("id") or "").strip()
        if not combination_id or len(identity_codes.get(combination_id, set())) != 1:
            continue
        required_codes = [str(code).strip().upper() for code in item.get("required_subject_codes") or []
                          if isinstance(code, str) and code.strip()]
        required_names = [str(name).strip() for name in item.get("required_subjects") or []
                          if isinstance(name, str) and name.strip()]
        if (not required_codes or len(required_codes) != len(set(required_codes))
                or len(required_names) != len(required_codes)
                or "_".join(sorted(required_codes)) != combination_id
                or {_normalize_label(name) for name in required_names} != explicit_tuple_members):
            continue

        acceptance_records = [row for row in item.get("acceptance_records") or [] if isinstance(row, dict)]
        has_scoped_contradiction = any(
            str(row.get("admission_year") or "") == requested_year
            and (row.get("accepted") is False or str(row.get("status") or "").casefold() in {
                "rejected", "not_accepted", "not accepted"
            })
            for row in acceptance_records
        )
        if has_scoped_contradiction:
            continue
        for acceptance in acceptance_records:
            if not isinstance(acceptance, dict) or str(acceptance.get("admission_year") or "") != requested_year:
                continue
            # The current graph contract encodes positive acceptance as a directed edge. If
            # an explicit contradictory status is ever supplied, fail closed for this scope.
            if acceptance.get("accepted") is False or str(acceptance.get("status") or "").casefold() in {
                "rejected", "not_accepted", "not accepted"
            }:
                continue
            method_codes = acceptance.get("method_codes")
            if not isinstance(method_codes, str) or not method_codes.strip():
                continue
            source_url = _official_admission_source_url(university_code, acceptance.get("source_url"))
            if not source_url:
                continue
            edge_id = "accepts-combination:" + Neo4jRetriever._stable_digest([
                university_code, program_code, combination_id, acceptance.get("admission_year"), method_codes
            ])
            expected_edge = (
                edge_id,
                f"TrainingProgram:{university_code}|{program_code}",
                f"AdmissionCombination:{combination_id}",
            )
            if edge_id in conflicting_edge_ids or expected_edge not in observed_edges:
                continue
            result.append((item, acceptance, source_url))
    return result


def _build_subject_overlap_answer(
    records: List[Dict[str, Any]], filters: Dict[str, Any], user_message: str = ""
) -> Optional[tuple[str, List[Dict[str, Any]]]]:
    """Build a bounded answer from verified, correctly scoped overlap edges only."""
    requested_group = str(filters.get("subject_group_code") or "").strip().casefold()
    requested_school = str(filters.get("high_school_code") or "").strip().casefold()
    requested_year = str(filters.get("academic_year") or "").strip()
    # A current-turn subject tuple is a narrower target than every subject
    # required by every retrieved program combination. Keep only named members.
    decomposed_message = unicodedata.normalize("NFD", user_message.casefold().replace("đ", "d"))
    normalized_message = "".join(
        char for char in decomposed_message if unicodedata.category(char) != "Mn"
    )
    explicit_tuple_members = {
        _normalize_label(member)
        for match in re.finditer(
            r"(?:to\s+hop|combination)\s*[（(]([^()（）]+)[)）]",
            normalized_message,
            flags=re.IGNORECASE,
        )
        for member in re.split(r"[,;/]+", match.group(1))
        if _normalize_label(member)
    }
    scoped_records: List[Dict[str, Any]] = []
    grouped_subjects: Dict[tuple[str, str, str, str], set[str]] = {}

    for record in records:
        if not isinstance(record, dict):
            continue
        scoped_overlaps = []
        for overlap in record.get("verified_subject_overlaps") or []:
            if not isinstance(overlap, dict) or overlap.get("validation_status") != "verified":
                continue
            group_code = str(overlap.get("subject_group_code") or "").strip()
            school_code = str(overlap.get("school_code") or "").strip()
            academic_year = str(overlap.get("academic_year") or "").strip()
            if not group_code or not school_code or not academic_year:
                continue
            if requested_group and group_code.casefold() != requested_group:
                continue
            if requested_school and school_code.casefold() != requested_school:
                continue
            if requested_year and academic_year != requested_year:
                continue
            subject = str(overlap.get("subject_name") or overlap.get("subject_code") or "").strip()
            if not subject:
                continue
            if explicit_tuple_members and _normalize_label(subject) not in explicit_tuple_members:
                continue
            scoped_overlaps.append(overlap)
            key = (school_code, academic_year, group_code,
                   str(record.get("program_name") or record.get("program_code") or "ngành trong bản ghi"))
            grouped_subjects.setdefault(key, set()).add(subject)
        if scoped_overlaps:
            scoped_record = dict(record)
            scoped_record["verified_subject_overlaps"] = scoped_overlaps
            scoped_records.append(scoped_record)

    if not grouped_subjects:
        return None

    claims = []
    for (school_code, academic_year, group_code, program), subjects in sorted(grouped_subjects.items()):
        subject_list = ", ".join(sorted(subjects, key=str.casefold))
        claims.append(
            f"{group_code} tại {school_code}, năm học {academic_year}: {subject_list} "
            f"(bằng chứng ngành: {program})"
        )
    admission_claims = []
    for record in scoped_records:
        for combination, acceptance, source_url in _verified_admission_acceptances(
            record, filters, explicit_tuple_members
        ):
            display_name = str(combination.get("display_name") or "tổ hợp được nêu")
            methods = str(acceptance.get("method_codes") or "").strip()
            method_note = f" (phương thức {methods})" if methods else ""
            claim = (
                f"Theo thông tin tuyển sinh có nguồn chính thức, tổ hợp {display_name} được ghi nhận "
                f"cho ngành {record['program_name']} của {record['university_name']} năm "
                f"{acceptance['admission_year']}{method_note} (nguồn: {source_url})"
            )
            if claim not in admission_claims:
                admission_claims.append(claim)

    message = (
        "Bằng chứng Graph đã xác minh các môn giao nhau sau trong những bản ghi đã truy xuất: "
        + "; ".join(claims)
        + ". Đây chỉ là phần giao nhau được quan sát, không phải danh sách đầy đủ môn của nhóm. "
        + ((" ".join(admission_claims) + ". ") if admission_claims else
           "Bằng chứng hiện có chưa xác nhận tổ hợp cụ thể trong câu hỏi được ngành/trường chấp nhận. ")
        + "Dù tổ hợp được chấp nhận, bằng chứng giao nhau này không xác nhận rằng nhóm môn đáp ứng đầy đủ "
        "điều kiện xét tuyển."
    )
    return message, scoped_records


def _apply_deterministic_route_contract(
    intent_result: Dict[str, Any], user_message: str
) -> Dict[str, Any]:
    """Correct high-confidence route mismatches without inventing extracted entities."""
    result = dict(intent_result) if isinstance(intent_result, dict) else {}
    if result.get("intent") not in {
        "knowledge", "combination_lookup", "general", "school_group_lookup",
        "subject_group_advice", "subject_group_comparison", "pathway_lookup"
    }:
        return result
    # Model-provided scope is not authoritative. The route contract below assigns
    # a request-local scope from the current wording and already-resolved slots.
    result.pop("answer_scope", None)
    result.pop("answer_scope_source", None)
    pathway_pairs = result.get("requested_pathway_pairs")
    if isinstance(pathway_pairs, list) and len(pathway_pairs) >= 2:
        valid_pairs = [pair for pair in pathway_pairs if isinstance(pair, dict)
                       and all(isinstance(pair.get(key), str) and pair[key].strip()
                               for key in ("high_school", "academic_year", "subject_group_code", "program_name"))]
        if len(valid_pairs) >= 2:
            result["intent"] = "pathway_lookup"
            result["answer_scope"] = "subject_overlap"
            result["answer_scope_source"] = "deterministic_explicit_scope"
            schools = list(dict.fromkeys(pair["high_school"].strip() for pair in valid_pairs))
            years = list(dict.fromkeys(pair["academic_year"].strip() for pair in valid_pairs))
            if len(schools) == 1:
                result["high_schools"] = schools
                current_filters = result.get("extracted_filters")
                current_filters = dict(current_filters) if isinstance(current_filters, dict) else {}
                current_filters.setdefault("high_school", schools[0])
                result["extracted_filters"] = current_filters
            if len(years) == 1:
                result["academic_year"] = years[0]
            return result
    query = _normalize_label(user_message)
    decomposed = unicodedata.normalize("NFD", user_message.casefold().replace("đ", "d"))
    query_words = " " + re.sub(
        r"[^a-z0-9]+", " ",
        "".join(char for char in decomposed if unicodedata.category(char) != "Mn"),
    ).strip() + " "
    has_school_group = "nhom" in query and ("thpt" in query or "truongdo" in query or "nhomdo" in query)
    has_canonical_combination = bool(re.search(
        r"\b[A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*){2,}\b", user_message, re.IGNORECASE
    ))
    has_public_combination = bool(_explicit_combination_codes(user_message))
    asks_combination_components = any(term in query for term in ("gommon", "gomnhungmon", "thanhphan"))
    explicit_subject_overlap = _asks_explicit_subject_overlap(query)
    asks_admission_path = (
        " nganh " in query_words
        and any(term in query for term in ("daihoc", "chapnhan", "tuyensinh", "tohop"))
        and (" thpt " in query_words or " nhom " in query_words or " tohop " in query_words)
    ) or explicit_subject_overlap
    filters = result.get("extracted_filters")
    filters = filters if isinstance(filters, dict) else {}
    named_schools = result.get("high_schools")
    named_schools = named_schools if isinstance(named_schools, list) else []
    if filters.get("high_school"):
        named_schools = [*named_schools, filters["high_school"]]
    explicit_schools = list(dict.fromkeys(
        str(label).strip() for label in named_schools
        if isinstance(label, str) and label.strip() and _phrase_in_message(label, user_message)
    ))
    # A group named in the current school-scoped turn takes precedence over any
    # stale group slot returned by Intent or inherited from an earlier turn.
    # Canonical catalog resolution below still verifies the code within school/year.
    if has_school_group and _has_explicit_school_marker(user_message):
        explicit_codes = _explicit_subject_group_codes(user_message)
        if explicit_codes:
            result["requested_group_codes"] = explicit_codes
    asks_comparison = any(term in query for term in ("sosanh", "doichieu", "khacnhau", "monchung", "naochung"))
    if len(explicit_schools) >= 2 and asks_comparison:
        result["intent"] = "subject_group_comparison"
        result["high_schools"] = explicit_schools
    elif (has_school_group and _has_explicit_school_marker(user_message)
          and not asks_admission_path
          and any(term in query for term in (
              "phuhop", "hopvoiem", "hopvoi", "cannhac", "muontheo", "emthich", "thich",
              "hocgioi", "hoctot", "sothich", "nangluc", "nenchon", "dinhhuong",
              "nganhnghe", "nganhnao",
          ))):
        result["intent"] = "subject_group_advice"
        if any(term in query for term in (
            "gommon", "gomnhungmon", "nhungmon", "thanhphan", "comon", "nhungnhom", "nhomnao",
        )):
            result["requested_tasks"] = ["school_group_lookup", "subject_group_advice"]
    elif asks_admission_path:
        result["intent"] = "pathway_lookup"
    elif (result.get("intent") in {"school_group_lookup", "subject_group_advice"} and has_school_group
          and (filters.get("high_school") or len(explicit_schools) == 1)
          and any(term in query for term in (
              "phuhop", "hopvoiem", "hopvoi", "cannhac", "muontheo", "emthich", "thich",
              "hocgioi", "hoctot", "sothich", "nangluc", "nenchon", "dinhhuong",
          ))):
        result["intent"] = "subject_group_advice"
        asks_group_facts = any(term in query for term in (
            "gommon", "gomnhungmon", "nhungmon", "thanhphan", "comon", "nhungnhom", "nhomnao",
        ))
        if asks_group_facts:
            result["requested_tasks"] = ["school_group_lookup", "subject_group_advice"]
    elif (has_canonical_combination or has_public_combination) and asks_combination_components:
        result["intent"] = "combination_lookup"
    elif has_school_group and any(term in query for term in ("gommon", "gomnhungmon", "thanhphan", "comon")):
        result["intent"] = "school_group_lookup"
    if result.get("intent") == "pathway_lookup":
        explicit_overlap = _asks_explicit_subject_overlap(query)
        explicit_full_path = _asks_explicit_full_path(query)
        if explicit_overlap and not explicit_full_path:
            result["answer_scope"] = "subject_overlap"
            result["answer_scope_source"] = "deterministic_explicit_scope"
        else:
            result["answer_scope"] = "full_path"
            result["answer_scope_source"] = (
                "deterministic_explicit_scope" if explicit_full_path
                else "deterministic_pathway_default"
            )
    return result


def _asks_explicit_subject_overlap(normalized_question: str) -> bool:
    """Recognize an explicit intersection request without inferring from subject count."""
    has_admission_combination = any(term in normalized_question for term in ("tohop", "xettuyen"))
    has_intersection_wording = any(term in normalized_question for term in (
        "monchung", "naochung", "chungmon", "naotrung", "trungvoito",
        "cochuamon", "comonnamtrong", "monnaonamtrong", "namtrongtohop",
        "comonnaothuoc", "monnaothuoc", "trungkhopvoi",
        "cochua", "giaonhau", "phanngiao", "trunglap", "diemchung",
    ))
    return has_admission_combination and has_intersection_wording


def _asks_explicit_full_path(normalized_question: str) -> bool:
    """Recognize complete-path/eligibility wording that must retain strict filtering."""
    strong_full_path_wording = any(term in normalized_question for term in (
        "dapung", "dudieukien", "thoaman", "coduocxettuyen", "cothexettuyen",
        "dutoanbo", "toanbo", "toanbomondieu", "tatcamon", "daydu",
        "dapungdaydu", "dapungtohop", "trungtuyen", "duocxettuyen",
        "dambaotuyensinh", "dubaodam", "dudieukienxettuyen",
    ))
    if strong_full_path_wording:
        return True
    generic_fit_wording = "hopvoitohopxettuyen" in normalized_question
    return generic_fit_wording and not _asks_explicit_subject_overlap(normalized_question)


def _school_catalog_match_key(value: str) -> str:
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    decomposed = unicodedata.normalize("NFD", value.casefold())
    plain = "".join(char for char in decomposed if unicodedata.category(char) != "Mn").replace("đ", "d")
    words = re.findall(r"[a-z0-9]+", plain)
    while words and words[0] in {"truong", "thpt", "pho", "thong"}:
        words.pop(0)
    return " ".join(words)


def _school_catalog_query_key(value: str) -> str:
    """Normalize a user mention while ignoring Vietnamese location connectors."""
    return " ".join(
        word for word in _school_catalog_match_key(value).split()
        if word not in {"o", "tai"}
    )


def _has_explicit_school_marker(value: str) -> bool:
    normalized = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    padded = f" {re.sub(r'[^a-z0-9]+', ' ', normalized).strip()} "
    if " thpt " not in padded and " truong " not in padded:
        return False
    if " thpt " not in padded and any(f" {kind} " in padded for kind in (
        "dai hoc", "cao dang", "hoc vien",
    )):
        return False
    return not any(f" {reference} " in padded for reference in (
        "truong do", "truong nay", "truong ay", "truong kia", "truong tren", "truong vua neu",
        "thpt do", "thpt nay", "thpt ay", "thpt kia", "thpt tren",
    ))


def _school_catalog_mention_keys(name: str) -> List[str]:
    """Return conservative leading aliases from a verified catalog name."""
    words = _school_catalog_match_key(name).split()
    # Longer aliases can include a location that is part of the verified catalog name.
    return [" ".join(words[:size]) for size in range(min(len(words), 5), 1, -1)]


def _school_catalog_location(school: Dict[str, Any]) -> Optional[str]:
    for key in ("location", "city", "province", "district"):
        value = school.get(key)
        if isinstance(value, str) and value.strip():
            return _school_catalog_query_key(value)
        if isinstance(value, dict):
            label = value.get("name") or value.get("label")
            if isinstance(label, str) and label.strip():
                return _school_catalog_query_key(label)
    return None


def _school_catalog_location_display(school: Dict[str, Any]) -> Optional[str]:
    """Return the original catalog location label for user-facing disclosure."""
    for key in ("location", "city", "province", "district"):
        value = school.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            label = value.get("name") or value.get("label")
            if isinstance(label, str) and label.strip():
                return label.strip()
    return None


def _school_catalog_location_is_mentioned(mention: str, location: str) -> bool:
    query = f" {_school_catalog_query_key(mention)} "
    return any(
        f" {alias} " in query
        for alias in _school_catalog_mention_keys(location)
    )


def _school_catalog_matches(mention: str, catalog: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return best catalog matches; never resolve a shared short alias arbitrarily."""
    query = f" {_school_catalog_query_key(mention)} "
    if not query.strip():
        return []

    query_key = _school_catalog_query_key(mention)
    exact_matches = [school for school in catalog if query_key in {
        _school_catalog_query_key(str(school["code"])),
        _school_catalog_query_key(str(school["name"])),
    }]
    if exact_matches:
        return list({str(school["code"]): school for school in exact_matches}.values())

    code_matches = [school for school in catalog
                    if f" {_school_catalog_query_key(str(school['code']))} " in query]
    if code_matches:
        return code_matches

    scored: List[tuple[int, Dict[str, Any]]] = []
    for school in catalog:
        matching_lengths = [
            len(alias.split())
            for alias in _school_catalog_mention_keys(str(school["name"]))
            if f" {alias} " in query
        ]
        if matching_lengths:
            scored.append((max(matching_lengths), school))
    if not scored:
        return []

    best_length = max(score for score, _ in scored)
    best_matches = [school for score, school in scored if score == best_length]
    if len(best_matches) > 1:
        locations = [_school_catalog_location(school) for school in best_matches]
        # Location can disambiguate only when every same-name candidate has verified
        # location metadata from the catalog and exactly one location is mentioned.
        if all(locations):
            located_matches = [
                school for school, location in zip(best_matches, locations)
                if location and _school_catalog_location_is_mentioned(mention, location)
            ]
            if located_matches:
                best_matches = located_matches
    unique = {str(school["code"]): school for school in best_matches}
    return list(unique.values())


def _validated_school_catalog(catalog: Any) -> Optional[List[Dict[str, Any]]]:
    """Validate and deduplicate the authoritative Backend school catalog."""
    if not isinstance(catalog, list):
        return None
    catalog_by_code: Dict[str, Dict[str, Any]] = {}
    for school in catalog:
        if (not isinstance(school, dict)
                or not isinstance(school.get("code"), str) or not school["code"].strip()
                or not isinstance(school.get("name"), str) or not school["name"].strip()):
            return None
        normalized = {**school, "code": school["code"].strip(), "name": school["name"].strip()}
        existing = catalog_by_code.get(normalized["code"])
        if existing and (
            _school_catalog_match_key(existing["name"]) != _school_catalog_match_key(normalized["name"])
            or _school_catalog_location(existing) != _school_catalog_location(normalized)
        ):
            return None
        catalog_by_code[normalized["code"]] = normalized
    return list(catalog_by_code.values())


def _school_resolution(mention: str, catalog: Any) -> Dict[str, Any]:
    """Resolve a school mention against a validated authoritative catalog."""
    validated = _validated_school_catalog(catalog)
    resolution: Dict[str, Any] = {
        "status": "catalog_unavailable" if validated is None else "unknown",
        "source": "backend_high_school_catalog",
    }
    if validated is None:
        return resolution
    matches = _school_catalog_matches(mention, validated)
    if len(matches) == 1:
        school = matches[0]
        resolved_school = {"code": school["code"], "name": school["name"]}
        location = _school_catalog_location_display(school)
        if location:
            resolved_school["location"] = location
        resolution.update({"status": "resolved", "school": resolved_school})
    elif len(matches) > 1:
        resolution.update({"status": "ambiguous", "candidates": [
            {"code": school["code"], "name": school["name"]} for school in matches
        ]})
    return resolution


def _catalog_unique_school_assumption_note(
    school_resolution: Any, current_message: str,
) -> Optional[str]:
    """Disclose a catalog-based school assumption when the user omitted location/code."""
    if not isinstance(school_resolution, dict) or school_resolution.get("status") != "resolved":
        return None
    school = school_resolution.get("school")
    if not isinstance(school, dict):
        return None
    code = str(school.get("code") or "").strip()
    name = str(school.get("name") or "").strip()
    if not code or not name:
        return None

    message_key = f" {_school_catalog_query_key(current_message)} "
    if f" {_school_catalog_query_key(code)} " in message_key:
        return None
    location = school.get("location")
    if isinstance(location, str) and location.strip() and _school_catalog_location_is_mentioned(
        current_message, location,
    ):
        return None

    location_text = f" tại {location}" if isinstance(location, str) and location.strip() else ""
    return (
        f"Trong danh mục trường hiện có, tên em nêu khớp duy nhất với **{name} ({code})**"
        f"{location_text}; mình tạm hiểu em muốn hỏi trường này. Nếu em muốn hỏi trường khác, "
        "em cho mình biết tỉnh/thành hoặc mã trường nhé."
    )


def _generic_subject_overlap_guarantee_answer(
    intent_result: Dict[str, Any], user_message: str,
) -> Optional[str]:
    """Answer a general no-guarantee question without inventing year-scoped eligibility."""
    if not isinstance(intent_result, dict) or intent_result.get("intent") != "pathway_lookup":
        return None
    query = _normalize_label(user_message)
    if not (
        "motmonchung" in query
        and "dambao" in query
        and ("dudieukien" in query or "trungtuyen" in query)
    ):
        return None
    return (
        "Kh\u00f4ng. Vi\u1ec7c c\u00f3 m\u1ed9t m\u00f4n chung ch\u1ec9 cho th\u1ea5y c\u00f3 ph\u1ea7n giao nhau v\u1ec1 m\u00f4n h\u1ecdc; "
        "ri\u00eang \u0111i\u1ec1u \u0111\u00f3 kh\u00f4ng ch\u1ee9ng minh em \u0111\u00e3 \u0111\u00e1p \u1ee9ng to\u00e0n b\u1ed9 \u0111i\u1ec1u ki\u1ec7n x\u00e9t tuy\u1ec3n "
        "v\u00e0 kh\u00f4ng b\u1ea3o \u0111\u1ea3m tr\u00fang tuy\u1ec3n. Mu\u1ed1n ki\u1ec3m tra c\u00e1c \u0111i\u1ec1u ki\u1ec7n c\u1ee5 th\u1ec3 c\u1ee7a ng\u00e0nh, "
        "c\u1ea7n \u0111\u1ed1i chi\u1ebfu quy \u0111\u1ecbnh \u0111\u00fang n\u0103m tuy\u1ec3n sinh."
    )


def _missing_pathway_eligibility_context(
    intent_result: Dict[str, Any], user_message: str,
) -> Optional[Dict[str, Any]]:
    """Ask for missing scoped inputs before an admission-eligibility conclusion."""
    if not isinstance(intent_result, dict) or intent_result.get("intent") != "pathway_lookup":
        return None
    query = _normalize_label(user_message)
    eligibility_terms = (
        "dapung", "dudieukien", "thoaman", "coduocxettuyen",
        "cothexettuyen", "hopvoitohopxettuyen",
    )
    if not any(term in query for term in eligibility_terms):
        return None

    filters = intent_result.get("extracted_filters")
    filters = filters if isinstance(filters, dict) else {}
    school_resolution = intent_result.get("school_resolution")
    resolved_school = (
        isinstance(school_resolution, dict)
        and school_resolution.get("status") == "resolved"
        and isinstance(school_resolution.get("school"), dict)
    )
    school_known = bool(
        resolved_school or filters.get("high_school_code") or filters.get("high_school")
        or intent_result.get("high_schools")
    )
    university = filters.get("university") or filters.get("university_code")
    program = filters.get("desired_program") or filters.get("program_name")
    admission_year = filters.get("admission_year") or intent_result.get("admission_year")
    academic_year = intent_result.get("academic_year") or filters.get("academic_year")
    requested_group_codes = intent_result.get("requested_group_codes")
    requested_group_codes = requested_group_codes if isinstance(requested_group_codes, list) else []
    school_group_scope = bool(
        requested_group_codes or filters.get("subject_group_code")
        or intent_result.get("resolved_subject_group_code")
        or _explicit_group_code_in_message(user_message)
    )

    missing = []
    if not school_known:
        missing.append("school")
    if not university:
        missing.append("university")
    if not program:
        missing.append("program")
    if not admission_year:
        missing.append("admission_year")
    if school_known and school_group_scope and not academic_year:
        missing.append("academic_year")
    if not missing:
        return None

    labels = {
        "school": "tên hoặc mã trường THPT",
        "university": "trường đại học",
        "program": "ngành hoặc chương trình muốn xét tuyển",
        "admission_year": "năm tuyển sinh muốn đối chiếu",
        "academic_year": "năm học áp dụng cho nhóm môn",
    }
    requested = ", ".join(labels[key] for key in missing)
    message = (
        f"Để đối chiếu nhóm môn với điều kiện tuyển sinh, em bổ sung giúp mình {requested}. "
        "Khi thiếu các thông tin này, mình chưa thể kết luận nhóm môn có đáp ứng điều kiện xét tuyển hay không."
    )
    known = {
        key: value for key, value in {
            "school_code": filters.get("high_school_code"),
            "school_name": filters.get("high_school"),
            "university": university,
            "program": program,
            "admission_year": admission_year,
            "academic_year": academic_year,
        }.items() if value not in (None, "", [], {})
    }
    return {"message": message, "missing": missing, "known": known}


def _resolve_school(label: str, schools: List[Dict[str, Any]]) -> tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Backward-compatible wrapper around the shared catalog-backed resolver."""
    resolution = _school_resolution(label, schools)
    if resolution.get("status") == "resolved":
        return resolution["school"], [resolution["school"]]
    return None, list(resolution.get("candidates", []))


def _current_school_resolution_text(message: str) -> str:
    """Use only the corrected value when the current turn explicitly corrects a school."""
    normalized = unicodedata.normalize("NFD", message.casefold().replace("đ", "d"))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    correction = re.search(r"\b(?:y em la|y minh la|minh muon noi|dinh chinh|la)\b", normalized)
    if correction and any(token in normalized[:correction.start()] for token in (
        "khong phai", "nham roi", "dinh chinh",
    )):
        return message[correction.end():].strip(" ,:;.-")
    return message


def _resolve_explicit_school_filter(
    intent_result: Dict[str, Any], user_message: str,
    context: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Resolve parser school mentions to one verified identity before graph retrieval."""
    result = dict(intent_result) if isinstance(intent_result, dict) else {}
    if result.get("intent") not in {
        "general", "pathway_lookup", "school_group_lookup",
        "subject_group_advice", "subject_group_comparison",
    }:
        return result
    filters = result.get("extracted_filters")
    filters = dict(filters) if isinstance(filters, dict) else {}
    high_schools = result.get("high_schools")
    if (result.get("intent") == "subject_group_comparison"
            and isinstance(high_schools, list)
            and len({str(label).strip() for label in high_schools if str(label).strip()}) > 1):
        # The comparison handler resolves each school independently; a singular
        # resolver must not collapse a valid two-school request into ambiguity.
        return result

    has_parser_school = bool(
        filters.get("high_school") or filters.get("high_school_code") or result.get("high_schools")
    )
    has_school_marker = _has_explicit_school_marker(user_message)
    explicit_group_follow_up = _is_explicit_group_follow_up(result, user_message)
    if not has_parser_school and not has_school_marker and not explicit_group_follow_up:
        return result

    try:
        catalog = _backend_get("/api/v1/high-schools")
    except requests.RequestException as exc:
        logger.warning("School catalog resolution unavailable error_type=%s", type(exc).__name__)
        catalog = None
    normalized_catalog = _validated_school_catalog(catalog)
    if normalized_catalog is None:
        result["school_resolution"] = {
            "status": "catalog_unavailable", "source": "backend_high_school_catalog",
        }
        return result

    resolution_text = _current_school_resolution_text(user_message)
    matches = _school_catalog_matches(resolution_text, normalized_catalog)
    if not matches and not has_school_marker and (has_parser_school or explicit_group_follow_up):
        for turn in reversed(context or []):
            if not isinstance(turn, dict) or turn.get("role") != "user":
                continue
            prior_message = turn.get("content")
            if not isinstance(prior_message, str) or not prior_message.strip():
                continue
            matches = _school_catalog_matches(prior_message, normalized_catalog)
            if matches or _has_explicit_school_marker(prior_message):
                resolution_text = prior_message
                break

    resolution = _school_resolution(resolution_text, normalized_catalog)
    if resolution["status"] == "resolved":
        school = resolution["school"]
        filters["high_school"] = school["name"]
        filters["high_school_code"] = school["code"]
        result["extracted_filters"] = filters
        result["high_schools"] = [school["name"]]
        result["school_resolution"] = resolution
        return result

    resolution.setdefault("candidates", [])
    # Fail closed: an unresolved raw phrase must not reach the fuzzy Cypher name filter.
    filters.pop("high_school", None)
    filters.pop("high_school_code", None)
    result["extracted_filters"] = filters
    result["high_schools"] = []
    result["school_resolution"] = resolution
    return result


def _resolve_combination_for_pathway(
    public_code: str, request_id: str
) -> tuple[Optional[str], Optional[Dict[str, Any]] | ChatResponse]:
    """Resolve a public catalogue code to one verified canonical graph ID."""
    try:
        result = _backend_get(f"/api/v1/admissions/combinations/{quote(public_code.strip(), safe='')}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return None, _chat_result(
                "no_data", f"ChÆ°a tÃ¬m tháº¥y mÃ£ tá»• há»£p {public_code} trong danh má»¥c Ä‘Ã£ thu tháº­p.",
                request_id, "pathway_lookup", {"combination_code": public_code,
                                                "evidence_source": "backend_admission_combination_api"})
        raise HTTPException(status_code=503, detail="Dá»‹ch vá»¥ danh má»¥c tá»• há»£p táº¡m thá»i chÆ°a sáºµn sÃ ng.") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Dá»‹ch vá»¥ danh má»¥c tá»• há»£p táº¡m thá»i chÆ°a sáºµn sÃ ng.") from exc
    labels = result.get("labels") if isinstance(result, dict) else None
    labels = [row for row in labels if isinstance(row, dict)] if isinstance(labels, list) else []
    canonical_ids = {str(row["canonicalCombinationId"]) for row in labels
                     if row.get("canonicalCombinationId")}
    has_unlinked_variant = any(not row.get("canonicalCombinationId") for row in labels)
    resolution = {"combination_code": public_code, "labels": labels,
                  "evidence_source": "backend_admission_combination_api"}
    if len(canonical_ids) != 1 or has_unlinked_variant:
        reason = "nhiá»u biáº¿n thá»ƒ chuáº©n" if len(canonical_ids) > 1 else "chÆ°a Ä‘Æ°á»£c xÃ¡c minh Ä‘áº§y Ä‘á»§"
        return None, _chat_result(
            "incomplete",
            f"MÃ£ {public_code} cÃ³ nhÃ£n tá»• há»£p {reason}. MÃ¬nh chÆ°a dÃ¹ng mÃ£ nÃ y Ä‘á»ƒ lá»c Ä‘Æ°á»ng ngÃ nh; em cÃ³ thá»ƒ nÃªu tÃªn cÃ¡c mÃ´n cá»§a tá»• há»£p Ä‘á»ƒ mÃ¬nh Ä‘á»‘i chiáº¿u.",
            request_id, "pathway_lookup", {"query_resolution": resolution})
    return next(iter(canonical_ids)), resolution


def _apply_current_turn_precedence(
    intent_result: Dict[str, Any], current_message: str, history: List[Dict[str, str]]
) -> Dict[str, Any]:
    """Drop stale selector filters unless the current turn explicitly refers to them."""
    if not isinstance(intent_result, dict):
        return {}
    result = dict(intent_result)
    filters = result.get("extracted_filters", {})
    filters = dict(filters) if isinstance(filters, dict) else {}
    current = _normalize_label(current_message)
    latest_user = next((turn.get("content", "") for turn in reversed(history)
                        if turn.get("role") == "user" and turn.get("content")), "")
    correction = any(term in current for term in (
        "khongphai", "yemla", "yminhla", "dinhchinh", "doisang", "thaybang", "nhamroi", "sualai"
    ))
    explicit_group_follow_up = _is_explicit_group_follow_up(result, current_message)
    clarification_year_answer = _answers_prior_year_clarification(current_message, history)
    follow_up = explicit_group_follow_up or clarification_year_answer or any(term in current for term in (
        "mado", "manay", "truongdo", "truongnay", "nganhdo", "nganhnay", "tohopdo", "tohopnay",
        "nhomdo", "nhomnay", "nhuvay", "vay", "conve", "noiotren"
    ))
    for key, value in filters.items():
        if value is None or key == "canonical_combination_id":
            continue
        if key in {"combination_code", "combination"}:
            negated_codes = _negated_combination_codes(current_message)
            if _normalize_label(str(value)) in negated_codes:
                positive_codes = _explicit_combination_codes(current_message)
                filters[key] = positive_codes[0] if len(positive_codes) == 1 else None
                continue
        if _phrase_in_message(str(value), current_message):
            continue
        if correction or not follow_up or not _phrase_in_message(str(value), latest_user):
            filters[key] = None
    result["extracted_filters"] = filters
    if explicit_group_follow_up and not result.get("academic_year"):
        scoped_year = _academic_year_from_group_context(latest_user)
        if scoped_year:
            result["academic_year"] = scoped_year
    school_labels = result.get("high_schools")
    if isinstance(school_labels, list):
        comparison_refers_to_prior_group = (
            result.get("intent") == "subject_group_comparison"
            and any(token in current for token in ("nhomdo", "nhomnay", "tohopdo", "tohopnay"))
            and any(token in current for token in ("sosanh", "doichieu", "giong", "khac"))
        )
        result["high_schools"] = [
            label for label in school_labels if isinstance(label, str)
            and (_phrase_in_message(label, current_message)
                 or ((follow_up and not correction or comparison_refers_to_prior_group)
                     and _phrase_in_message(label, latest_user)))
        ]
    if correction:
        for key in ("preferred_subjects", "disliked_subjects", "self_reported_strengths",
                    "confidence_by_subject", "grades_by_subject", "grades_needing_scale"):
            values = result.get(key)
            if isinstance(values, list):
                result[key] = [item for item in values if isinstance(item, dict)
                               and _phrase_in_message(str(item.get("evidence", "")), current_message)]
    return result


def _answers_prior_year_clarification(
    current_message: str, history: List[Dict[str, str]],
) -> bool:
    """Recognize a year supplied in direct response to the previous assistant's year question.

    This preserves the user's already established pathway selectors while keeping the year itself
    explicit in the current turn; academic and admission years are never inferred from each other.
    """
    latest_assistant = next((turn.get("content", "") for turn in reversed(history)
                             if turn.get("role") == "assistant"
                             and isinstance(turn.get("content"), str)
                             and turn.get("content", "").strip()), "")
    assistant_text = _normalize_label(latest_assistant)
    asked_for_year = any(term in assistant_text for term in (
        "nambotuyen", "namtuyensinh", "namxettuyen", "namhoc", "namapdung",
    ))
    supplied_year = re.search(r"(?<!\d)(?:19|20)\d{2}(?:\s*[-/]\s*(?:19|20)?\d{2})?(?!\d)",
                              current_message) is not None
    return asked_for_year and supplied_year


def _negated_combination_codes(message: str) -> set[str]:
    normalized = unicodedata.normalize("NFD", message.casefold().replace("đ", "d"))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    normalized = re.sub(r"\s+", " ", normalized)
    return {
        code.casefold()
        for code in re.findall(
            r"\b(?:khong\s+phai|not)\s+(?:ma\s+)?([a-z]{1,3}\d{1,3})\b",
            normalized,
            flags=re.IGNORECASE,
        )
    }


def _explicit_combination_codes(user_text: str) -> List[str]:
    normalized_text = _normalize_label(user_text)
    is_group_reference = bool(
        re.search(r"(?:nhom|group)(?:mon)?[a-z]{0,3}\d{1,3}", normalized_text)
    )
    is_combination_context = any(
        token in normalized_text for token in ("tohop", "xettuyen", "monthi")
    )
    if is_group_reference and not is_combination_context:
        return []
    negated = _negated_combination_codes(user_text)
    return list(dict.fromkeys(
        match.group(1)
        for match in re.finditer(
            r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{1,3})(?![A-Za-z0-9])", user_text
        )
        if match.group(1).casefold() not in negated
    ))


def _phrase_in_message(phrase: str, message: str) -> bool:
    normalized_phrase = _normalize_label(phrase)
    normalized_message = _normalize_label(message)
    if not normalized_phrase or not normalized_message:
        return False
    if normalized_phrase in normalized_message:
        return True
    for prefix in ("thpt", "truong", "daihoc", "nganh", "tohop", "ma"):
        remainder = normalized_phrase[len(prefix):] if normalized_phrase.startswith(prefix) else ""
        if len(remainder) >= 3 and remainder in normalized_message:
            return True
    return False


def _explicit_subject_group_codes(message: str) -> List[Dict[str, str]]:
    """Extract group identifiers explicitly named in this turn, without using history."""
    if not isinstance(message, str) or not message.strip():
        return []
    normalized = _normalize_label(message)
    # _normalize_label intentionally removes whitespace and punctuation, so a
    # phrase boundary cannot survive ("học Nhóm 1" becomes "hocnhom1"). The
    # school-scoped caller constrains this compact marker to group questions.
    numbers = re.findall(r"nhom(?:mon)?(?:so)?0*(\d{1,3})", normalized)
    numbers.extend(re.findall(r"g0*(\d{1,3})", normalized))
    codes = list(dict.fromkeys(f"G{int(number):02d}" for number in numbers))
    return [{"code": code, "evidence": "explicit_current_turn_group_reference"} for code in codes]


def _normalize_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold().replace("đ", "d").replace("Ä‘", "d"))
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", without_marks.replace("đ", "d"))


def _api_data(response: requests.Response) -> Any:
    response.raise_for_status()
    body = response.json()
    return body.get("data") if isinstance(body, dict) else None


def _prior_turns(request: ChatRequest) -> List[Dict[str, str]]:
    turns = request.get_conversation_context()
    valid = [turn for turn in turns if isinstance(turn, dict)
             and turn.get("role") in {"user", "assistant"}
             and isinstance(turn.get("content"), str) and turn["content"].strip()]
    if valid and valid[-1]["role"] == "user" and valid[-1]["content"].strip() == request.user_message.strip():
        valid.pop()
    return valid


def _chat_result(
    result_status: str,
    message: str,
    request_id: str,
    intent: str,
    data: Optional[Dict[str, Any]] = None,
) -> ChatResponse:
    response_data: Dict[str, Any] = {"request_id": request_id, "intent": intent}
    if data:
        response_data.update(data)
    response_data.setdefault("response_mode", {
        "success": "answer",
        "incomplete": "clarification",
        "no_data": "abstention",
        "unsupported": "unsupported",
    }.get(result_status, "answer"))
    if "evidence" in response_data and "retrieval_units" not in response_data:
        response_data["retrieval_units"] = _retrieval_units(
            response_data.get("evidence"),
            str(response_data.get("evidence_source") or "backend_api"),
        )
    response_data.setdefault(
        "actual_execution_route",
        _actual_execution_route(response_data, request_id, intent),
    )
    response_data["actual_execution_route"].setdefault(
        "response_disposition", response_data.get("response_mode", "unknown")
    )
    trace = DiagnosticStageTrace.current()
    if trace is not None and trace.enabled:
        # The active trace observes instrumented retrieval entry points. When an
        # operation ran, use only its positive evidence-composition result for
        # Backend/Graph route attribution. Preserve explicit legacy route facts
        # for callers that construct _chat_result outside those operation helpers.
        if trace._retrieval_attempted:
            response_data["retrieval_executed"] = True
        elif not isinstance(response_data.get("retrieval_executed"), bool):
            response_data["retrieval_executed"] = (
                response_data.get("backend_retrieval_executed") is True
                or response_data.get("graph_retrieval_executed") is True
            )
        evidence_refs = _safe_evidence_reference_ids(response_data.get("evidence"))
        source = str(response_data.get("evidence_source") or "")
        evidence_system = ("fastapi_graph" if source == "neo4j_graph" else
                           "backend_api" if source.startswith("backend_") else None)
        consumed_systems = trace.compose_evidence(
            {evidence_system: evidence_refs} if evidence_system and evidence_refs else {},
            composition_id=request_id,
        )
        # In diagnostic mode, classify only positively consumed retrieval evidence.
        # The evidence_source label alone is not proof that retrieval output reached
        # the answer pipeline (for example, an empty Graph result is an abstention).
        if trace._retrieval_attempted:
            response_data["graph_retrieval_executed"] = "fastapi_graph" in consumed_systems
            response_data["backend_retrieval_executed"] = "backend_api" in consumed_systems
        composition = response_data.get("evidence_composition")
        if isinstance(composition, dict):
            by_system = {key: value for key, value in composition.items()
                         if key in {"backend_api", "fastapi_graph"} and isinstance(value, list)}
            composed = trace.compose_evidence(by_system, composition_id=request_id)
            response_data["hybrid_composition_verified"] = len(composed) == 2
        else:
            response_data["hybrid_composition_verified"] = None
        response_data["actual_execution_route"] = _actual_execution_route(
            response_data, request_id, intent
        )
        response_data["actual_execution_route"].setdefault(
            "response_disposition", response_data.get("response_mode", "unknown")
        )
        response_data["diagnostic_correlation_id"] = trace.correlation_id
        if os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip():
            if not trace.route_decision_recorded:
                try:
                    append_route_decision({
                        "run_id": trace.run_id, "correlation_id": trace.correlation_id,
                        "request_id": request_id, "decision_branch": "unknown",
                        "decision_status": "not_reached",
                    })
                except Exception:
                    pass
            trace.set_final_route({
                "run_id": trace.run_id, "correlation_id": trace.correlation_id, "request_id": request_id,
                "execution_kind": response_data["actual_execution_route"].get("execution_kind"),
                "response_mode": response_data.get("response_mode"),
                "retrieval_executed": response_data.get("retrieval_executed"),
                "backend_retrieval_executed": response_data["actual_execution_route"].get(
                    "backend_retrieval_executed"),
                "graph_retrieval_executed": response_data["actual_execution_route"].get(
                    "graph_retrieval_executed"),
                "hybrid_composition_verified": response_data["actual_execution_route"].get(
                    "hybrid_composition_verified"),
                "generation_executed": response_data.get("generation_executed"),
                "evidence_source": response_data.get("evidence_source"),
                "filters": response_data.get("filters") or response_data.get("query_filters"),
                "evidence_count": response_data.get("retrieved_count"),
            })
        record_response_stage()
    trace = response_data["actual_execution_route"]
    logger.info(
        "chat request_id=%s selected_intent=%s requested_tasks=%s handler=%s execution_kind=%s evidence_entity_count=%d relationship_count=%d response_mode=%s",
        request_id, intent, response_data.get("requested_tasks", [intent]),
        trace.get("handler"), trace.get("execution_kind"),
        len(trace.get("observed_entity_ids", [])), len(trace.get("observed_relationship_ids", [])),
        response_data.get("response_mode"),
    )
    return ChatResponse(status=result_status, message=message, data=response_data)


def _diagnostic_decision_branch(intent: Any) -> str:
    """Map parsed intent to a bounded routing branch label, not an execution claim."""
    if intent in {"greeting", "unsupported", "knowledge", "combination_lookup",
                  "school_group_lookup", "subject_group_advice", "subject_group_comparison"}:
        return str(intent)
    if intent in {"general", "pathway_lookup"}:
        return "general_pathway"
    return "unknown"


def _actual_execution_route(data: Dict[str, Any], request_id: str, intent: str) -> Dict[str, Any]:
    """Expose only retrieval paths supported by execution evidence."""
    source = str(data.get("evidence_source") or "")
    retrieval_executed = data.get("retrieval_executed")
    if not isinstance(retrieval_executed, bool):
        retrieval_executed = True if source in {
            "neo4j_graph", "backend_subject_group_comparison_api"
        } or source.startswith("backend_") else None
    generation_executed = data.get("generation_executed")
    if not isinstance(generation_executed, bool):
        generation_executed = False if source.startswith("backend_") else None
    backend_executed = data.get("backend_retrieval_executed")
    if not isinstance(backend_executed, bool):
        backend_executed = True if source.startswith("backend_") else False if source == "neo4j_graph" else None
    graph_executed = data.get("graph_retrieval_executed")
    if not isinstance(graph_executed, bool):
        graph_executed = True if source == "neo4j_graph" else False if source.startswith("backend_") \
            and source != "backend_subject_group_comparison_api" else None
    if retrieval_executed is False:
        backend_executed = False
        graph_executed = False
    hybrid_composition_verified = data.get("hybrid_composition_verified")
    if backend_executed is True and graph_executed is True and hybrid_composition_verified is True:
        execution_kind = "hybrid"
        handler = source or intent
    elif source == "neo4j_graph" and graph_executed is True:
        execution_kind = {
            True: "graph_retrieval_generation",
            False: "graph_retrieval_only",
            None: "unknown",
        }[generation_executed]
        handler = {
            True: "neo4j_graph_retrieval_generation",
            False: "neo4j_graph_retrieval_only",
            None: "neo4j_graph_unclassified",
        }[generation_executed]
    elif backend_executed is True and graph_executed is False:
        execution_kind = "backend_lookup"
        handler = source or intent
    else:
        if retrieval_executed is False:
            execution_kind = "not_executed"
            handler = intent
        else:
            execution_kind = "unknown"
            handler = intent

    evidence = data.get("evidence")
    if isinstance(evidence, list):
        evidence_rows = evidence
    elif isinstance(evidence, dict):
        evidence_rows = [
            row
            for key in ("groups", "labels", "mandatorySubjects", "electiveSubjects")
            for row in (evidence.get(key) if isinstance(evidence.get(key), list) else [])
        ]
        if not evidence_rows and evidence:
            evidence_rows = [evidence]
    else:
        evidence_rows = []
    entity_ids = sorted({
        str(row[key])
        for row in evidence_rows
        if isinstance(row, dict)
        for key in ("evidence_id", "id", "uid", "code", "groupCode", "combinationId", "canonicalCombinationId")
        if row.get(key) is not None
    })
    relationship_ids = sorted({
        str(identifier)
        for row in evidence_rows
        if isinstance(row, dict)
        for key in ("relationship_ids", "relationshipIds", "edge_ids", "edgeIds")
        for identifier in (row.get(key) if isinstance(row.get(key), list) else [])
        if identifier is not None
    })

    return {
        "intent": intent,
        "intent_handler": intent,
        "handler": handler,
        "execution_kind": execution_kind,
        "retrieval_executed": retrieval_executed,
        "retrieval_route": execution_kind if retrieval_executed is True else None,
        "retrieval_status": "executed" if retrieval_executed is True else
                            "not_executed" if retrieval_executed is False else "unknown",
        "evidence_source": source or None,
        "response_disposition": data.get("response_mode"),
        "disposition_reason": next((data.get(key) for key in (
            "clarification_reason", "abstention_reason", "unsupported_reason"
        ) if isinstance(data.get(key), str) and data.get(key).strip()), None),
        "unknown_route_reason": ("evidence_source_missing" if not source else
                                 "execution_paths_unverified") if execution_kind == "unknown" else None,
        "backend_retrieval_executed": backend_executed,
        "graph_retrieval_executed": graph_executed,
        "hybrid_composition_verified": hybrid_composition_verified
        if isinstance(hybrid_composition_verified, bool) else None,
        "generation_executed": generation_executed,
        "request_id": request_id,
        "trace_ref": request_id,
        "observed_entity_ids": entity_ids,
        "observed_relationship_ids": relationship_ids,
    }


def _retrieval_units(evidence: Any, source: str) -> List[Dict[str, Any]]:
    """Expose ordered, individually scoreable evidence units with provenance."""
    rows: List[tuple[str, Any]] = []
    if isinstance(evidence, list):
        rows = [("record", row) for row in evidence]
    elif isinstance(evidence, dict):
        for key, label in (("groups", "subject_group"), ("labels", "combination_label"),
                           ("mandatorySubjects", "mandatory_subject"),
                           ("electiveSubjects", "elective_subject")):
            values = evidence.get(key)
            if isinstance(values, list):
                rows.extend((label, row) for row in values)
        if not rows and evidence:
            rows = [("record", evidence)]
    units = []
    for index, (record_type, row) in enumerate(rows, 1):
        try:
            text = __import__("json").dumps(
                {"record_type": record_type, "record": row},
                ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(row)
        identifier = (row.get("evidence_id") or row.get("id") or row.get("groupCode") or row.get("code")
                      if isinstance(row, dict) else None)
        if not identifier:
            canonical = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            identifier = "evidence:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
        units.append({"id": str(identifier or f"{record_type}:{index}"), "source": source,
                      "record_type": record_type, "text": text})
    return units


def _graph_path_validation_payload(
    records: List[Dict[str, Any]], filters: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the versioned public validation contract from Neo4j evidence only."""
    # Revalidate the exact canonical edge set being returned. The Retriever may also
    # validate raw repeated occurrences before collapsing shared edges; never reuse that
    # stale result if response records have since changed.
    normalized_records = []
    for row in records:
        if not isinstance(row, dict):
            continue
        normalized = dict(row)
        normalized["evidence_dependency_validation"] = validate_evidence_dependencies(
            normalized, filters or {})
        normalized_records.append(normalized)
    records[:] = normalized_records

    contract_records: List[Dict[str, Any]] = []
    for row in records:
        if not isinstance(row, dict):
            continue
        validation = row.get("evidence_dependency_validation")
        if not isinstance(validation, dict):
            validation = validate_evidence_dependencies(row, filters or {})
        raw_path = row.get("evidence_path")
        raw_path = raw_path if isinstance(raw_path, list) else []
        path_edges = [
            {"from": edge["from"].strip(), "relationship_id": str(edge["relationship_id"]).strip(),
             "to": edge["to"].strip()}
            for edge in raw_path if isinstance(edge, dict)
            and isinstance(edge.get("from"), str) and edge["from"].strip()
            and edge.get("relationship_id") is not None and str(edge["relationship_id"]).strip()
            and isinstance(edge.get("to"), str) and edge["to"].strip()
        ]
        required = validation.get("required_relationship_ids", [])
        missing = validation.get("missing_relationship_ids", [])
        contract_records.append({
            "evidence_id": str(row.get("evidence_id") or ""),
            "scope": {
                "university_code": row.get("university_code"),
                "program_code": row.get("program_code"),
                "combinations": [{
                    "combination_id": item.get("id"),
                    "acceptance_records": item.get("acceptance_records") or [],
                } for item in row.get("combinations", []) if isinstance(item, dict)],
                "school_groups": [{
                    "high_school_code": group.get("high_school_code"),
                    "academic_year": group.get("academic_year"),
                    "subject_group_code": group.get("subject_group_code"),
                } for item in row.get("combinations", []) if isinstance(item, dict)
                  for group in item.get("matching_subject_groups", []) if isinstance(group, dict)],
            },
            "status": validation.get("status", "incomplete"),
            "relationship_ids": list(validation.get("observed_relationship_ids") or []),
            "required_relationship_ids": list(required) if isinstance(required, list) else [],
            "missing_relationship_ids": list(missing) if isinstance(missing, list) else [],
            "evidence_path": path_edges,
            "evidence_path_conflicts": row.get("evidence_path_conflicts") or [],
            "malformed_relationship_ids": validation.get("malformed_relationship_ids", []),
            "duplicate_relationship_ids": validation.get("duplicate_relationship_ids", []),
            "shared_relationship_ids": validation.get("shared_relationship_ids", []),
            "invalid_duplicate_relationship_ids": validation.get("invalid_duplicate_relationship_ids", []),
            "conflicting_relationship_ids": validation.get("conflicting_relationship_ids", []),
            "missing_scope_dependencies": validation.get("missing_scope_dependencies", []),
            "dependencies": validation.get("dependencies", []),
        })
    return {
        "schema_version": 2,
        "source": "neo4j_graph",
        "path_semantics": "directed_edge_set",
        "complete": bool(contract_records) and all(
            item["status"] == "complete" for item in contract_records
        ),
        "records": contract_records,
    }


def _backend_get(path: str) -> Any:
    trace = DiagnosticStageTrace.current()
    if trace and trace.enabled:
        with trace.retrieval_operation("backend_api") as operation:
            with trace.stage("backend_lookup"):
                response = requests.get(
                    f"{settings.SUBJECT_PATHWAY_API_URL}{path}",
                    timeout=settings.SUBJECT_PATHWAY_API_TIMEOUT,
                )
                result = _api_data(response)
                operation.evidence_produced(_safe_evidence_reference_ids(result))
                return result
    response = requests.get(f"{settings.SUBJECT_PATHWAY_API_URL}{path}",
                            timeout=settings.SUBJECT_PATHWAY_API_TIMEOUT)
    return _api_data(response)


def _backend_post(path: str, payload: Dict[str, Any]) -> Any:
    trace = DiagnosticStageTrace.current()
    if trace and trace.enabled:
        with trace.retrieval_operation("backend_api") as operation:
            with trace.stage("backend_lookup"):
                response = requests.post(
                    f"{settings.SUBJECT_PATHWAY_API_URL}{path}",
                    json=payload,
                    timeout=settings.SUBJECT_PATHWAY_API_TIMEOUT,
                )
                result = _api_data(response)
                operation.evidence_produced(_safe_evidence_reference_ids(result))
                return result
    response = requests.post(f"{settings.SUBJECT_PATHWAY_API_URL}{path}", json=payload,
                             timeout=settings.SUBJECT_PATHWAY_API_TIMEOUT)
    return _api_data(response)


def _safe_evidence_reference_ids(value: Any) -> List[str]:
    """Extract bounded identifiers only; never place source text in trace events."""
    identifiers: set[str] = set()
    stack = [value]
    while stack and len(identifiers) < 200:
        current = stack.pop()
        if isinstance(current, dict):
            for key in ("evidence_id", "id", "uid", "code", "groupCode", "programCode", "combinationId"):
                item = current.get(key)
                if isinstance(item, (str, int)) and not isinstance(item, bool) and str(item).strip():
                    identifiers.add(str(item).strip()[:160])
            stack.extend(item for item in current.values() if isinstance(item, (dict, list)))
        elif isinstance(current, list):
            stack.extend(current)
    return sorted(identifiers)


def _backend_resolve_exact_combination(subject_codes: List[str], request_id: str) -> Dict[str, Any]:
    """Validate the exact resolver's successful response before treating [] as no data."""
    try:
        data = _backend_post(
            "/api/v1/admissions/combinations/resolve-by-subjects",
            {"subjectCodes": subject_codes},
        )
        _CombinationResolveData.model_validate(data)
    except (ValueError, ValidationError) as exc:
        logger.warning("chat request_id=%s stage=backend_resolver invalid_contract=%s",
                       request_id, type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="Dịch vụ tra cứu tổ hợp trả về dữ liệu không hợp lệ. Vui lòng thử lại sau.",
        ) from exc
    return data


def _resolve_subject(label: str, catalog: Dict[str, Any]) -> tuple[Optional[str], List[str]]:
    query = _normalize_label(label)
    subject_aliases = {
        "van": {"nguvan"},
        "su": {"lichsu"},
        "dia": {"diali", "dialy"},
        "li": {"vatli", "vatly"},
        "ly": {"vatli", "vatly"},
        "hoa": {"hoahoc"},
        "sinh": {"sinhhoc"},
        "tin": {"tinhoc"},
        "anh": {"tienganh", "ngoaingu"},
        "ngoaingu": {"tienganh", "ngoaingu"},
    }
    accepted_labels = {query, *subject_aliases.get(query, set())}
    matches = set()
    for field in ("mandatorySubjects", "electiveSubjects"):
        for subject in catalog.get(field, []) if isinstance(catalog, dict) else []:
            if query and accepted_labels.intersection({
                _normalize_label(str(subject.get("code", ""))),
                _normalize_label(str(subject.get("name", ""))),
            }):
                if subject.get("code"):
                    matches.add(str(subject["code"]))
    values = sorted(matches)
    return (values[0] if len(values) == 1 else None), values


def _subject_labels_in_question(question: str, catalog: Dict[str, Any]) -> List[str]:
    """Find catalog subjects explicitly named in a question, preserving catalog labels."""
    def normalize_phrase(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
        without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
        return re.sub(r"[^a-z0-9]+", " ", without_marks).strip()

    normalized_question = f" {normalize_phrase(question)} "
    # A subject name inside a program title (for example, "ngành Vật lý kỹ thuật")
    # is not evidence that the student selected that subject as a combination filter.
    masked_question = re.sub(
        r"\bnganh\b(.{0,120}?)(?=\b(?:cua|tai|o|chap nhan|tuyen sinh)\b|[,.?;]|$)",
        lambda match: " " * len(match.group(0)),
        normalized_question,
    )
    aliases = {
        "nguvan": ("van",), "tienganh": ("anhvan", "ngoai ngu"),
        "vatli": ("vatly", "li", "ly"), "vatly": ("vatli", "li", "ly"),
        "hoahoc": ("hoa",), "sinhhoc": ("sinh",), "tinhoc": ("tin",),
        "lichsu": ("su",), "dialy": ("diali", "dia"), "diali": ("dialy", "dia"),
    }
    found: Dict[str, str] = {}
    for field in ("mandatorySubjects", "electiveSubjects"):
        for row in catalog.get(field, []) if isinstance(catalog, dict) else []:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code", ""))
            name = str(row.get("name", code))
            terms = {normalize_phrase(code), normalize_phrase(name)}
            for term in tuple(terms):
                terms.update(normalize_phrase(alias) for alias in aliases.get(_normalize_label(term), ()))
            if any(term and f" {term} " in masked_question for term in terms):
                found[code or name] = name
    return list(found.values())


def _filter_requested_groups(
    groups: List[Dict[str, Any]], requested_codes: List[Dict[str, Any]], *,
    school_code: Optional[str] = None, academic_year: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    """Resolve requested group codes against the current school's year-scoped result set."""
    labels = [str(item.get("code", "")).strip()
              for item in requested_codes if isinstance(item, dict) and item.get("code")]
    if not labels:
        return groups, [], []

    def in_scope(group: Dict[str, Any]) -> bool:
        record_school = group.get("schoolCode", group.get("highSchoolCode"))
        record_year = group.get("academicYear", group.get("academic_year"))
        return not (
            (school_code and record_school and str(record_school).casefold() != school_code.casefold())
            or (academic_year and record_year and str(record_year) != academic_year)
        )

    def alias_keys(value: str) -> set[str]:
        normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())
        keys = {normalized} if normalized else set()
        match = re.fullmatch(r"g?(\d{1,3})", normalized)
        if match is None:
            match = re.search(r"(?:group|nhom)g?(\d{1,3})$", normalized)
        if match:
            digits = match.group(1)
            keys.add(digits)
            keys.add(str(int(digits)))
        return keys

    scoped_groups = [group for group in groups if isinstance(group, dict) and in_scope(group)]
    selected: List[Dict[str, Any]] = []
    resolutions: List[Dict[str, Any]] = []
    unavailable: List[str] = []
    for raw_code in labels:
        exact = [group for group in scoped_groups
                 if str(group.get("groupCode", "")).strip().casefold() == raw_code.casefold()]
        candidates = exact
        status = "exact"
        numeric_alias = bool(re.fullmatch(r"\d{1,3}", re.sub(r"[^a-z0-9]+", "", raw_code.casefold())))
        if numeric_alias:
            requested_aliases = alias_keys(raw_code)
            alias_candidates = []
            for group in scoped_groups:
                catalog_values = [str(group.get("groupCode", ""))]
                display_name = group.get("groupName")
                if isinstance(display_name, str):
                    catalog_values.append(display_name)
                if requested_aliases.intersection(
                    key for value in catalog_values for key in alias_keys(value)
                ):
                    alias_candidates.append(group)
            if len(alias_candidates) > 1:
                candidates = alias_candidates
                status = "ambiguous"
        if len(exact) == 0:
            requested_aliases = alias_keys(raw_code)
            if not candidates or status == "exact":
                candidates = []
                for group in scoped_groups:
                    catalog_values = [str(group.get("groupCode", ""))]
                    display_name = group.get("groupName")
                    if isinstance(display_name, str):
                        catalog_values.append(display_name)
                    if requested_aliases.intersection(
                        key for value in catalog_values for key in alias_keys(value)
                    ):
                        candidates.append(group)
                status = "resolved_alias"
        if len(candidates) == 1:
            group = candidates[0]
            selected.append(group)
            resolutions.append({
                "raw_code": raw_code,
                "canonical_code": str(group.get("groupCode", "")),
                "status": status,
                "source": "school_year_group_catalog",
                "scope": {"school_code": school_code, "academic_year": academic_year},
            })
        elif len(candidates) > 1:
            resolutions.append({
                "raw_code": raw_code, "canonical_code": None, "status": "ambiguous",
                "source": "school_year_group_catalog",
                "candidate_codes": [str(group.get("groupCode", "")) for group in candidates],
                "scope": {"school_code": school_code, "academic_year": academic_year},
            })
        else:
            unavailable.append(raw_code)
            resolutions.append({
                "raw_code": raw_code, "canonical_code": None, "status": "unknown",
                "source": "school_year_group_catalog",
                "scope": {"school_code": school_code, "academic_year": academic_year},
            })
    unique_selected = {str(group.get("groupCode", "")).casefold(): group for group in selected}
    return list(unique_selected.values()), unavailable, resolutions


def _resolve_explicit_graph_group_scope(
    intent: Dict[str, Any], current_message: str = "",
    context: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """Resolve an explicit Graph group against the authoritative school/year catalog."""
    result = dict(intent) if isinstance(intent, dict) else {}
    pathway_pairs = result.get("requested_pathway_pairs")
    if isinstance(pathway_pairs, list) and len(pathway_pairs) >= 2:
        return result
    scope = result.get("answer_scope")
    source = result.get("answer_scope_source")
    requested_codes = result.get("requested_group_codes")
    requested_codes = requested_codes if isinstance(requested_codes, list) else []
    user_messages = [current_message, *[
        str(turn.get("content") or "") for turn in (context or [])
        if isinstance(turn, dict) and turn.get("role") == "user"
    ]]
    if (scope == "full_path" and not requested_codes
            and _answers_prior_year_clarification(current_message, context or [])):
        # A direct answer to a year clarification completes the existing pathway
        # request. Preserve its explicitly named group for catalog resolution;
        # multiple historical group mentions remain ambiguous and fail closed.
        requested_codes = _history_group_codes(context)
        if requested_codes:
            result["requested_group_codes"] = requested_codes
    current_normalized = _normalize_label(current_message)
    refers_to_prior_group_scope = any(term in current_normalized for term in (
        "nhomdo", "nhomnay", "nhomay", "truongdo", "truongnay", "truongay",
    )) or _answers_prior_year_clarification(current_message, context or [])
    latest_prior_user = user_messages[-1] if len(user_messages) > 1 else ""
    scope_messages = [current_message] if _has_explicit_school_marker(current_message) else (
        [latest_prior_user] if refers_to_prior_group_scope else []
    )
    has_explicit_school_group_scope = bool(requested_codes) and any(
        _has_explicit_school_marker(message)
        and re.search(r"(?:nhom(?:mon)?0*\d{1,3}|g0*\d{1,3})", _normalize_label(message))
        for message in scope_messages
    )
    supported_scope = (
        scope == "subject_overlap" and source == "deterministic_explicit_scope"
    ) or (
        scope == "full_path"
        and source in {"deterministic_explicit_scope", "deterministic_pathway_default"}
        and has_explicit_school_group_scope
    )
    if not supported_scope:
        return result
    filters = result.get("extracted_filters")
    filters = dict(filters) if isinstance(filters, dict) else {}
    school_code = str(filters.get("high_school_code") or "").strip()
    if not school_code:
        result["graph_group_scope_resolution"] = {"status": "missing_school"}
        return result
    supplied_year = result.get("academic_year") or filters.get("academic_year")
    if not supplied_year:
        result["graph_group_scope_resolution"] = {
            "status": "missing_academic_year", "school_code": school_code,
        }
        return result
    try:
        years = _backend_get(f"/api/v1/high-schools/{quote(school_code, safe='')}/academic-years") or []
    except requests.RequestException:
        result["graph_group_scope_resolution"] = {
            "status": "catalog_unavailable", "school_code": school_code,
        }
        return result
    if not isinstance(years, list):
        result["graph_group_scope_resolution"] = {
            "status": "catalog_unavailable", "school_code": school_code,
        }
        return result
    available_years = [str(year) for year in years]
    year = str(supplied_year)
    if not year or year not in available_years:
        result["graph_group_scope_resolution"] = {
            "status": "unknown_academic_year", "school_code": school_code,
            "academic_year": year, "available_years": available_years,
        }
        return result
    if not requested_codes:
        # An explicit "which groups" overlap question means all groups in the
        # verified school/year scope. No group code is invented or narrowed.
        result["academic_year"] = year
        result["graph_group_scope_resolution"] = {
            "status": "resolved", "source": "backend_school_year_group_catalog",
            "school_code": school_code, "academic_year": year,
            "scope_kind": "all_groups",
        }
        return result
    try:
        groups = _backend_get(
            f"/api/v1/high-schools/{quote(school_code, safe='')}/subject-groups?academicYear={quote(year, safe='')}"
        ) or []
    except requests.RequestException:
        result["graph_group_scope_resolution"] = {
            "status": "catalog_unavailable", "school_code": school_code, "academic_year": year,
        }
        return result
    if not isinstance(groups, list):
        result["graph_group_scope_resolution"] = {
            "status": "catalog_unavailable", "school_code": school_code, "academic_year": year,
        }
        return result
    selected, missing, resolutions = _filter_requested_groups(
        groups, requested_codes, school_code=school_code, academic_year=year,
    )
    if len(selected) != 1 or missing or any(item.get("status") == "ambiguous" for item in resolutions):
        status = "ambiguous" if any(item.get("status") == "ambiguous" for item in resolutions) else "unresolved"
        result["graph_group_scope_resolution"] = {
            "status": status, "school_code": school_code, "academic_year": year,
            "resolutions": resolutions,
        }
        return result
    canonical_code = str(selected[0].get("groupCode") or "").strip()
    result["academic_year"] = year
    result["resolved_subject_group_code"] = canonical_code
    result["graph_group_scope_resolution"] = {
        "status": "resolved", "source": "backend_school_year_group_catalog",
        "school_code": school_code, "academic_year": year, "canonical_group_code": canonical_code,
        "resolution": resolutions[0],
    }
    return result


def _history_group_codes(context: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """Extract group identifiers only from the latest user turn that explicitly named one."""
    pattern = re.compile(
        r"\b(?:nh[oó]m(?:\s+m[oô]n)?|group)\s*(?:s[oố]\s*)?([a-z]?\d{1,3})\b",
        re.IGNORECASE,
    )
    for turn in reversed(context or []):
        if turn.get("role") != "user":
            continue
        content = str(turn.get("content", ""))
        matches = list(pattern.finditer(content))
        if matches:
            return [{"code": match.group(1).upper(), "evidence": match.group(0)} for match in matches]
    return []


def _prior_assistant_group_codes(context: Optional[List[Dict[str, str]]]) -> List[Dict[str, str]]:
    """Read group identifiers from the latest assistant answer for ambiguous deictic follow-ups."""
    for turn in reversed(context or []):
        if turn.get("role") != "assistant":
            continue
        content = str(turn.get("content", ""))
        matches = re.findall(r"\bg\s*0*(\d{1,3})\b", content, re.IGNORECASE)
        if not matches:
            content = _normalize_label(content)
            matches = re.findall(
                r"(?:nhom(?:mon)?|group)(?:so)?0*(\d{1,3})",
                content, re.IGNORECASE,
            )
        codes = list(dict.fromkeys(f"G{int(number):02d}" for number in matches))
        return [{"code": code, "evidence": "assistant_history_group_listing"} for code in codes]
    return []


def _explicit_group_code_in_message(message: str) -> bool:
    normalized = _normalize_label(message)
    return bool(re.search(r"\bg\s*0*\d{1,3}\b|\bnhom(?:mon)?\s*(?:so\s*)?0*\d{1,3}\b", normalized))


def _is_explicit_group_follow_up(intent_result: Dict[str, Any], message: str) -> bool:
    """Recognize a narrow group-specific follow-up that may reuse the prior user scope."""
    if not isinstance(intent_result, dict) or intent_result.get("intent") != "school_group_lookup":
        return False
    normalized = _normalize_label(message)
    if "rieng" not in normalized or "nhom" not in normalized:
        return False
    codes = intent_result.get("requested_group_codes")
    codes = codes if isinstance(codes, list) else []
    has_current_code = any(
        isinstance(item, dict) and item.get("code")
        and _phrase_in_message(str(item["code"]), message)
        for item in codes
    )
    if not has_current_code:
        has_current_code = bool(re.search(r"nhom(?:mon)?(?:g)?0*\d{1,3}", normalized))
    correction = any(term in normalized for term in (
        "khongphai", "yemla", "yminhla", "dinhchinh", "doisang", "thaybang", "nhamroi", "sualai"
    ))
    return has_current_code and not correction


def _singular_group_reference_is_ambiguous(message: str, requested_codes: List[Dict[str, Any]]) -> bool:
    distinct_codes = {
        str(item.get("code", "")).strip().casefold()
        for item in requested_codes if isinstance(item, dict) and item.get("code")
    }
    return _has_singular_group_reference(message) and len(distinct_codes) > 1


def _has_singular_group_reference(message: str) -> bool:
    normalized = _normalize_label(message).replace(" ", "")
    return any(reference in normalized for reference in (
        "nhomdo", "nhomnay", "nhomay", "nhomkia", "nhomtren",
    ))


def _academic_year_from_group_context(message: str) -> Optional[str]:
    """Use years only when the text scopes a school-group lookup, not an admission year."""
    normalized = _normalize_label(message)
    years = list(dict.fromkeys(re.findall(r"\b20\d{2}\b", message)))
    if len(years) != 1:
        return None
    if "namhoc" in normalized:
        return years[0]
    has_school_name_or_code = (
        "thpt" in normalized or "truong" in normalized
        or bool(re.search(r"\b[A-Z][A-Z0-9_-]{1,7}\b", message))
    )
    if ("nhom" in normalized and has_school_name_or_code
            and not any(term in normalized for term in ("tuyensinh", "xettuyen", "trungtuyen", "admission"))):
        return years[0]
    return None


def _university_search_filter(label: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    value = label.strip() if isinstance(label, str) else ""
    if not value:
        return None, None
    if re.fullmatch(r"[A-Za-z0-9_-]{2,20}", value):
        return value.upper(), None
    return None, value


def _school_from_intent(
    intent: Dict[str, Any], request_id: str,
    context: Optional[List[Dict[str, str]]] = None,
    current_message: str = "",
) -> tuple[Optional[Dict[str, Any]], Optional[ChatResponse]]:
    filters = intent.get("extracted_filters", {})
    school_label = filters.get("high_school") if isinstance(filters, dict) else None
    if not school_label:
        schools_filter = intent.get("high_schools")
        if isinstance(schools_filter, list) and len(schools_filter) == 1:
            school_label = schools_filter[0]
    if not school_label and not _is_history_school_reference(current_message):
        return None, _chat_result(
            "incomplete", "Em đang học hoặc muốn tìm hiểu trường THPT nào? Em cho mình tên hoặc mã trường nhé.",
            request_id, str(intent.get("intent")),
        )
    try:
        resolution = intent.get("school_resolution")
        if isinstance(resolution, dict) and resolution.get("status") == "resolved":
            school = resolution.get("school")
            candidates: List[Dict[str, Any]] = []
        else:
            if not school_label and _is_history_school_reference(current_message):
                resolution_text = ""
                for turn in reversed(context or []):
                    if turn.get("role") != "user":
                        continue
                    resolution_text = str(turn.get("content", ""))
                    if resolution_text:
                        school_label = resolution_text
                        break
            else:
                resolution_text = str(school_label or current_message)
            if isinstance(resolution, dict):
                # The request-level resolver has already classified this mention.
                pass
            else:
                catalog = _validated_school_catalog(_backend_get("/api/v1/high-schools"))
                resolution = _school_resolution(resolution_text, catalog)
            if resolution.get("status") == "catalog_unavailable":
                raise HTTPException(status_code=503,
                                    detail="Dịch vụ danh mục trường học chưa sẵn sàng để xác minh.")
            school = resolution.get("school") if resolution.get("status") == "resolved" else None
            candidates = resolution.get("candidates", [])
        if not school_label and school:
            school_label = school.get("name", "")
        if not school_label:
            return None, _chat_result(
                "incomplete", "Em đang học hoặc muốn tìm hiểu trường THPT nào? Em cho mình tên hoặc mã trường nhé.",
                request_id, str(intent.get("intent")),
            )
        if not school:
            message = ("Tên trường khớp với nhiều trường. Em cho biết tỉnh/thành hoặc mã trường để mình tra đúng nhé: "
                       if candidates else "Mình chưa xác định được trường trong danh mục hiện có. Em kiểm tra lại tên hoặc gửi mã trường nhé.")
            options = [{"code": item.get("code"), "name": item.get("name")} for item in candidates]
            data = {"school_query": school_label, "school_candidates": options,
                    "school_resolution": resolution}
            return None, _chat_result("incomplete", message, request_id, str(intent.get("intent")), data)
        return school, None
    except requests.RequestException as exc:
        logger.warning("chat request_id=%s backend=school_catalog unavailable", request_id)
        raise HTTPException(status_code=503, detail="Dịch vụ dữ liệu trường học tạm thời chưa sẵn sàng.") from exc


def _lookup_school_groups(
    intent: Dict[str, Any], request_id: str,
    context: Optional[List[Dict[str, str]]] = None,
    current_message: str = "",
) -> ChatResponse:
    school, error = _school_from_intent(intent, request_id, context, current_message)
    if error:
        return error
    assert school is not None
    school_resolution = intent.get("school_resolution")
    requested_codes = intent.get("requested_group_codes", [])
    if not requested_codes and _is_history_school_reference(current_message):
        requested_codes = _history_group_codes(context)
        if _singular_group_reference_is_ambiguous(current_message, requested_codes):
            options = ", ".join(dict.fromkeys(item["code"] for item in requested_codes))
            return _chat_result("incomplete", f"Em đang nhắc đến nhiều nhóm trước đó ({options}). Em muốn hỏi nhóm nào?",
                                request_id, "school_group_lookup",
                                {"school": school, "academic_year": intent.get("academic_year"),
                                 "ambiguous_group_reference": True,
                                 "requested_group_codes": requested_codes,
                                 "school_resolution": school_resolution})
    try:
        years = _backend_get(f"/api/v1/high-schools/{quote(str(school['code']), safe='')}/academic-years") or []
        requested_year = intent.get("academic_year")
        year = str(requested_year) if requested_year else (str(years[0]) if years else None)
        if not year or year not in {str(item) for item in years}:
            return _chat_result("no_data", f"Chưa có dữ liệu nhóm môn công bố cho {school['name']} trong năm học {year or 'được yêu cầu'}.", request_id, "school_group_lookup", {"school": school, "school_resolution": school_resolution})
        groups = _backend_get(
            f"/api/v1/high-schools/{quote(str(school['code']), safe='')}/subject-groups?academicYear={quote(year, safe='')}"
        ) or []
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Dịch vụ dữ liệu nhóm môn tạm thời chưa sẵn sàng.") from exc
    if not groups:
        return _chat_result("no_data", f"Chưa tìm thấy nhóm môn đang công bố cho {school['name']} trong năm học {year}.", request_id, "school_group_lookup", {"school": school, "academic_year": year, "school_resolution": school_resolution})
    all_groups = groups
    groups, missing_codes, group_code_resolutions = _filter_requested_groups(
        all_groups, requested_codes, school_code=str(school["code"]), academic_year=year,
    )
    ambiguous_resolutions = [item for item in group_code_resolutions if item["status"] == "ambiguous"]
    if ambiguous_resolutions:
        codes = ", ".join(item["raw_code"] for item in ambiguous_resolutions)
        return _chat_result("incomplete", f"Mã nhóm {codes} có nhiều cách khớp trong danh mục của trường và năm học này. Em xác nhận mã nhóm giúp mình nhé.",
                            request_id, "school_group_lookup",
                            {"school": school, "academic_year": year,
                             "group_code_resolutions": group_code_resolutions,
                             "school_resolution": school_resolution})
    if missing_codes and not groups:
        available = [str(group.get("groupCode")) for group in all_groups]
        return _chat_result("incomplete", "Mình chưa tìm thấy mã nhóm " + ", ".join(missing_codes)
                            + ". Em kiểm tra lại mã nhóm nhé.", request_id, "school_group_lookup",
                            {"school": school, "academic_year": year,
                             "available_group_codes": available,
                             "group_code_resolutions": group_code_resolutions,
                             "school_resolution": school_resolution})
    if not groups:
        return _chat_result("no_data", f"Không có mã nhóm được yêu cầu tại {school['name']} trong năm học {year}.",
                            request_id, "school_group_lookup",
                            {"school": school, "academic_year": year, "available_group_codes": [],
                             "group_code_resolutions": group_code_resolutions,
                             "school_resolution": school_resolution})
    assumption_note = _catalog_unique_school_assumption_note(school_resolution, current_message)
    lines = []
    if assumption_note:
        lines.extend([assumption_note, ""])
    lines.append(f"Nhóm môn đang công bố tại **{school['name']}** · năm học **{year}**:")
    for group in groups:
        subject_names = ", ".join(subject.get("name", subject.get("code", "")) for subject in group.get("subjects", []))
        lines.append(f"- **{group.get('groupName', group.get('groupCode'))}** ({group.get('groupCode')}): {subject_names}")
    if missing_codes:
        lines.append("\nMissing requested group codes in the current result: " + ", ".join(missing_codes)
                     + ". This does not establish that those groups do not exist.")
    return _chat_result("success", "\n".join(lines), request_id, "school_group_lookup",
                        {"school": school, "academic_year": year, "evidence": groups,
                         "evidence_source": "backend_subject_group_api",
                         "school_resolution": school_resolution,
                         "group_code_resolutions": group_code_resolutions,
                         "answer_completeness": "partial" if missing_codes else "complete",
                         "unavailable_group_codes": missing_codes})


def _answer_school_group_lookup_and_advice(
    intent: Dict[str, Any], request_id: str,
    context: Optional[List[Dict[str, str]]] = None,
    current_message: str = "",
) -> ChatResponse:
    """Return requested school facts, then give grounded advice or ask for missing preferences."""
    filters = intent.get("extracted_filters", {})
    filters = filters if isinstance(filters, dict) else {}
    has_advice_signals = any(intent.get(key) for key in (
        "preferred_subjects", "disliked_subjects", "self_reported_strengths",
        "confidence_by_subject", "grades_by_subject",
    )) or bool(filters.get("desired_program"))
    advice_clarification = None
    if has_advice_signals:
        advice_intent = {**intent, "intent": "subject_group_advice"}
        response = _compare_subject_groups(
            advice_intent, request_id, context=context, current_message=current_message,
        )
        if response.status == "success":
            data = dict(response.data or {})
            data["requested_tasks"] = ["school_group_lookup", "subject_group_advice"]
            if data.get("tentative_candidate_groups"):
                data["answered_tasks"] = ["school_group_lookup", "subject_group_advice"]
            else:
                data["answered_tasks"] = ["school_group_lookup"]
                data["unanswered_tasks"] = ["subject_group_advice"]
            return _chat_result(response.status, response.message, request_id,
                                "subject_group_advice", data)
        advice_clarification = response.message
    else:
        unscaled = intent.get("grades_needing_scale", [])
        if unscaled:
            subjects = ", ".join(item.get("subject", "môn học") for item in unscaled
                                  if isinstance(item, dict))
            advice_clarification = (
                f"Em cho biết thang điểm của {subjects or 'môn em nêu'} để mình cân nhắc thông tin đó nhé."
            )

    lookup_intent = {**intent, "intent": "school_group_lookup"}
    response = _lookup_school_groups(
        lookup_intent, request_id, context=context, current_message=current_message,
    )
    data = dict(response.data or {})
    data["requested_tasks"] = ["school_group_lookup", "subject_group_advice"]
    data["answered_tasks"] = ["school_group_lookup"] if response.status == "success" else []
    data["unanswered_tasks"] = ["subject_group_advice"]
    if response.status != "success":
        if advice_clarification:
            return _chat_result(response.status, response.message + "\n\n" + advice_clarification,
                                request_id, "subject_group_advice", data)
        return _chat_result(response.status, response.message, request_id,
                            "subject_group_advice", data)
    data["answer_completeness"] = "partial"
    clarification = advice_clarification or (
        "\n\nĐể đưa ra gợi ý tạm thời dựa trên nhóm môn đã nêu, em cho biết môn mình thích, "
        "không thích hoặc tự thấy học tốt nhé. Đây chỉ là hướng tham khảo, không xác nhận điều kiện hay kết quả tuyển sinh."
    )
    if not clarification.startswith("\n"):
        clarification = "\n\n" + clarification
    return _chat_result("success", response.message + clarification, request_id,
                        "subject_group_advice", data)


def _lookup_combination(
    intent: Dict[str, Any],
    request_id: str,
    question: str = "",
    context: Optional[List[Any]] = None,
) -> ChatResponse:
    """Look up an explicitly named code or resolve one exact subject-only composition."""
    filters = intent.get("extracted_filters", {})
    filters = filters if isinstance(filters, dict) else {}
    parser_code = filters.get("combination_code") or filters.get("combination")

    current_codes = _explicit_combination_codes(question)
    historical_user_codes: List[str] = []
    for turn in context or []:
        role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
        content = turn.get("content") if isinstance(turn, dict) else getattr(turn, "content", None)
        if role == "user" and isinstance(content, str):
            historical_user_codes.extend(_explicit_combination_codes(content))

    parser_code_text = str(parser_code).strip() if isinstance(parser_code, (str, int)) else ""
    if parser_code_text and current_codes:
        # The current user turn takes precedence over stale or hallucinated parser filters.
        code = next(
            (item for item in current_codes if item.casefold() == parser_code_text.casefold()),
            current_codes[0] if len(current_codes) == 1 else None,
        )
    elif parser_code_text:
        unique_history_codes = list(dict.fromkeys(code.casefold() for code in historical_user_codes))
        code = next((item for item in historical_user_codes if item.casefold() == parser_code_text.casefold()), None) if (
            len(unique_history_codes) == 1
            and unique_history_codes[0] == parser_code_text.casefold()
        ) else None
    else:
        code = current_codes[0] if len(current_codes) == 1 else None

    if len(current_codes) > 1:
        return _chat_result(
            "incomplete",
            "Em đang nhắc nhiều mã tổ hợp. Em muốn tra mã nào?",
            request_id,
            "combination_lookup",
            {"explicit_combination_codes": current_codes},
        )
    if code:
        return _lookup_combination_code(str(code), request_id)

    try:
        catalog = _backend_get("/api/v1/subjects/classifications") or {}
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Danh mục môn học tạm thời chưa sẵn sàng.") from exc
    canonical_subject_codes = _explicit_canonical_subject_codes(question, catalog)
    labels = _subject_labels_in_question(question, catalog) if not canonical_subject_codes else []
    if not labels and not canonical_subject_codes:
        return _chat_result(
            "incomplete",
            "Em gửi mã tổ hợp (ví dụ A01), tên tổ hợp kèm các môn, hoặc danh sách 2–4 môn muốn tra nhé.",
            request_id, "combination_lookup",
        )

    subject_codes: List[str] = list(canonical_subject_codes)
    unresolved: List[str] = []
    for label in labels:
        subject_code, candidates = _resolve_subject(label, catalog)
        if subject_code:
            subject_codes.append(subject_code)
        else:
            unresolved.append(label)
            if len(candidates) > 1:
                logger.info("chat request_id=%s combination subject label has ambiguous catalog matches", request_id)
    subject_codes = list(dict.fromkeys(subject_codes))
    if unresolved:
        return _chat_result(
            "incomplete",
            "Mình chưa xác định duy nhất được môn " + ", ".join(unresolved)
            + ". Em dùng tên đầy đủ theo danh mục môn học nhé.",
            request_id, "combination_lookup", {"unresolved_subjects": unresolved},
        )
    if len(subject_codes) < 2 or len(subject_codes) > 4:
        return _chat_result(
            "incomplete", "Em chọn từ 2 đến 4 môn để mình tra tổ hợp phù hợp nhé.",
            request_id, "combination_lookup", {"resolved_subject_codes": subject_codes},
        )

    try:
        result = _backend_resolve_exact_combination(subject_codes, request_id)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return _chat_result(
                "no_data",
                "Chưa tìm thấy tổ hợp phù hợp trong dữ liệu hiện có cho các môn đã nêu. Điều này chưa xác nhận rằng không tồn tại tổ hợp nào.",
                request_id, "combination_lookup", {"resolved_subject_codes": subject_codes},
            )
        raise HTTPException(status_code=503, detail="Dịch vụ tra cứu tổ hợp tạm thời chưa sẵn sàng.") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Dịch vụ tra cứu tổ hợp tạm thời chưa sẵn sàng.") from exc

    combinations = result["matches"]
    if not combinations:
        return _chat_result(
            "no_data",
            "Chưa có tổ hợp phù hợp trong dữ liệu hiện có cho các môn đã nêu; dữ liệu trống không chứng minh tổ hợp không tồn tại.",
            request_id, "combination_lookup", {"resolved_subject_codes": subject_codes},
        )
    lines = ["Các tổ hợp trong dữ liệu có thành phần khớp với những môn em nêu:"]
    for combination in combinations:
        display = combination.get("combinationDisplay") or combination.get("combinationId") or "Tổ hợp chưa có tên"
        labels_for_combination = combination.get("labels") or []
        label_text = "; ".join(
            f"{label.get('code')}: {label.get('subjectDetails')}"
            + (f" (source: {label.get('sourceUrl')})" if label.get("sourceUrl") else "")
            for label in labels_for_combination if isinstance(label, dict)
        )
        suffix = f" — {label_text}" if label_text else " — chưa có nhãn mô tả được liên kết"
        lines.append(f"- **{display}**{suffix}")
    return _chat_result(
        "success", "\n".join(lines), request_id, "combination_lookup",
        {"evidence": result, "evidence_source": "backend_admission_combination_resolver",
         "resolved_subject_codes": subject_codes,
         "result_scope": result.get("resultScope") if isinstance(result, dict) else None,
         "answer_completeness": "complete"},
    )


def _explicit_canonical_subject_codes(question: str, catalog: Dict[str, Any]) -> List[str]:
    """Resolve a canonical underscore-separated combination ID through the subject catalog."""
    candidates = re.findall(
        r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]*(?:_[A-Z][A-Z0-9]*){2,})(?![A-Za-z0-9])",
        question, flags=re.IGNORECASE,
    )
    if len(candidates) != 1:
        return []
    catalog_codes = {
        str(row.get("code", "")).upper()
        for key in ("mandatorySubjects", "electiveSubjects")
        for row in (catalog.get(key, []) if isinstance(catalog, dict) else [])
        if isinstance(row, dict) and row.get("code")
    }
    parts = [part.upper() for part in candidates[0].split("_")]
    if len(parts) < 2 or any(part not in catalog_codes for part in parts):
        return []
    return parts


def _is_history_school_reference(message: str) -> bool:
    normalized = _normalize_label(message)
    return any(token in normalized for token in ("truongdo", "truongnay", "nhomdo", "nhomnay"))


def _lookup_combination_code(code: str, request_id: str) -> ChatResponse:
    code = code.strip()
    try:
        result = _backend_get(f"/api/v1/admissions/combinations/{quote(code, safe='')}")
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return _chat_result("no_data", f"Chưa tìm thấy mã {code} trong danh mục đã thu thập.", request_id, "combination_lookup")
        raise HTTPException(status_code=503, detail="Dịch vụ danh mục tổ hợp tạm thời chưa sẵn sàng.") from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Dịch vụ danh mục tổ hợp tạm thời chưa sẵn sàng.") from exc
    if not isinstance(result, dict):
        return _chat_result("no_data", f"Chưa có nhãn tổ hợp cho mã {code} trong dữ liệu hiện có.", request_id, "combination_lookup")
    lines = [f"Các bản ghi mới nhất cho mã **{result.get('requestedCode', code)}**:"]
    labels = result.get("labels")
    labels = labels if isinstance(labels, list) else []
    labels = [label for label in labels if isinstance(label, dict)]
    if not labels:
        return _chat_result("no_data", f"ChÆ°a cÃ³ nhÃ£n chi tiáº¿t cho mÃ£ {code} trong snapshot danh má»¥c hiá»‡n cÃ³.",
                            request_id, "combination_lookup",
                            {"evidence": result, "evidence_source": "backend_admission_combination_api",
                             "result_scope": "latest imported label snapshot"})
    for label in labels:
        canonical = label.get("canonicalCombinationId")
        mapping = f" (tổ hợp chuẩn: `{canonical}`)" if canonical else " (chưa có mã tổ hợp chuẩn được xác minh)"
        source = f" (source: {label.get('sourceUrl')})" if label.get("sourceUrl") else ""
        lines.append(f"- {label.get('code')}: {label.get('subjectDetails')}{mapping}{source}")
    incomplete_mappings = any(not label.get("canonicalCombinationId") for label in labels)
    return _chat_result("success", "\n".join(lines), request_id, "combination_lookup",
                        {"evidence": result, "evidence_source": "backend_admission_combination_api",
                         "answer_completeness": "partial" if incomplete_mappings else "complete"})


def _answer_knowledge_question(request: ChatRequest, intent: Dict[str, Any], request_id: str, route: str) -> ChatResponse:
    query = _normalize_label(request.user_message)
    if "tohop" in query and ("nhommon" in query or "thpt" in query):
        answer = ("**Nhóm môn THPT** là các môn lựa chọn mà một trường công bố cho một năm học. "
                  "**Tổ hợp xét tuyển** là các môn mà một ngành/trường đại học ghi nhận trong dữ liệu tuyển sinh. "
                  "Một nhóm môn chỉ hỗ trợ một đường tuyển sinh khi bao phủ đủ thành phần tổ hợp; "
                  "yêu cầu năng khiếu vẫn phải xét riêng.")
        return _chat_result("success", answer, request_id, route)
    try:
        catalog = _backend_get("/api/v1/subjects/classifications") or {}
    except requests.RequestException as exc:
        raise HTTPException(status_code=503, detail="Danh mục môn học tạm thời chưa sẵn sàng.") from exc
    mandatory_rows = catalog.get("mandatorySubjects", []) if isinstance(catalog, dict) else []
    elective_rows = catalog.get("electiveSubjects", []) if isinstance(catalog, dict) else []
    mandatory_rows = [row for row in mandatory_rows if isinstance(row, dict)]
    elective_rows = [row for row in elective_rows if isinstance(row, dict)]
    mentioned = [item.get("subject", "") for item in intent.get("knowledge_subjects", [])
                 if isinstance(item, dict) and item.get("subject")]
    mentioned.extend(_subject_labels_in_question(request.user_message, catalog))
    requested_codes: List[str] = []
    unresolved: List[str] = []
    for label in dict.fromkeys(mentioned):
        code, _ = _resolve_subject(str(label), catalog)
        if code:
            requested_codes.append(code)
        else:
            unresolved.append(str(label))
    if unresolved:
        return _chat_result("incomplete", "Mình chưa xác định được duy nhất môn "
                            + ", ".join(dict.fromkeys(unresolved))
                            + " trong danh mục. Em dùng tên đầy đủ của môn nhé.", request_id, route,
                            {"unresolved_subjects": list(dict.fromkeys(unresolved))})

    is_count_question = any(term in query for term in ("baonhieu", "soluong", "tongso", "dem"))
    asks_mandatory = "batbuoc" in query
    asks_elective = "tuchon" in query or "luachon" in query
    matching_rows = []
    for row in mandatory_rows:
        if not requested_codes or str(row.get("code", "")) in requested_codes:
            matching_rows.append(("bắt buộc", row))
    for row in elective_rows:
        if not requested_codes or str(row.get("code", "")) in requested_codes:
            matching_rows.append(("tự chọn", row))

    if requested_codes:
        if is_count_question:
            answer_lines = ["Theo danh mục môn đang dùng trong hệ thống:"]
            for code in requested_codes:
                subject = next((row for _, row in matching_rows if str(row.get("code", "")) == code), None)
                if subject:
                    answer_lines.append(f"- {subject.get('name', code)}: có trong danh mục")
                else:
                    answer_lines.append(f"- {code}: chưa có trong danh mục")
        else:
            answer_lines = ["Theo danh mục môn đang dùng trong hệ thống:"]
            answer_lines.extend(f"- {row.get('name', row.get('code'))}: được đánh dấu {category}"
                                for category, row in matching_rows)
            if not matching_rows:
                answer_lines.append("- Chưa có phân loại cho môn này trong dữ liệu hệ thống.")
    elif asks_mandatory or asks_elective:
        selected = []
        if asks_mandatory:
            selected.extend(("bắt buộc", row) for row in mandatory_rows)
        if asks_elective:
            selected.extend(("tự chọn", row) for row in elective_rows)
        if is_count_question:
            counts = []
            if asks_mandatory:
                counts.append(f"{len(mandatory_rows)} môn được đánh dấu bắt buộc")
            if asks_elective:
                counts.append(f"{len(elective_rows)} môn được đánh dấu tự chọn")
            answer_lines = ["Theo danh mục môn đang dùng trong hệ thống có " + " và ".join(counts) + "."]
        else:
            answer_lines = ["Theo danh mục môn đang dùng trong hệ thống:"]
            answer_lines.extend(f"- {row.get('name', row.get('code'))} ({row.get('code', '')})"
                                for _, row in selected)
            if not selected:
                answer_lines.append("- Danh mục chưa có bản ghi cho nhóm môn em hỏi.")
    else:
        return _chat_result("incomplete", "Em muốn hỏi về môn cụ thể nào, hay muốn xem danh sách môn bắt buộc/tự chọn?",
                            request_id, route)

    answer_lines.append("Đây là phân loại trong dữ liệu hệ thống; câu hỏi về quy định chính thức cần đối chiếu văn bản nguồn.")
    relevant_codes = set(requested_codes)
    if relevant_codes:
        relevant_mandatory = [row for row in mandatory_rows if str(row.get("code", "")) in relevant_codes]
        relevant_elective = [row for row in elective_rows if str(row.get("code", "")) in relevant_codes]
    else:
        relevant_mandatory = mandatory_rows if asks_mandatory or not asks_elective else []
        relevant_elective = elective_rows if asks_elective or not asks_mandatory else []
    return _chat_result("success", "\n".join(answer_lines), request_id, route,
                        {"evidence": {"mandatorySubjects": relevant_mandatory,
                                       "electiveSubjects": relevant_elective},
                         "evidence_source": "backend_subject_classification_api"})


def _advise_without_school(intent: Dict[str, Any], request_id: str) -> ChatResponse:
    """Give a grounded next step from stated student signals before a school is selected."""
    filters = intent.get("extracted_filters", {})
    preferred = intent.get("preferred_subjects", [])
    disliked = intent.get("disliked_subjects", [])
    strengths = intent.get("self_reported_strengths", [])
    confidence = intent.get("confidence_by_subject", [])
    grades = intent.get("grades_by_subject", [])
    unscaled = intent.get("grades_needing_scale", [])
    desired_program = filters.get("desired_program") if isinstance(filters, dict) else None
    if not any((preferred, disliked, strengths, confidence, grades, unscaled, desired_program)):
        return _chat_result("incomplete",
                            "Em đang nhắm tới trường THPT nào? Nếu chưa chọn trường, em có thể chia sẻ môn mình thích, môn học tốt hoặc môn không muốn học để mình bắt đầu đối chiếu.",
                            request_id, str(intent.get("intent", "subject_group_advice")))

    lines = ["Mình ghi nhận thông tin em vừa chia sẻ:"]
    if preferred:
        lines.append("- Môn em thích: " + ", ".join(item["subject"] for item in preferred))
    if disliked:
        lines.append("- Môn em không thích: " + ", ".join(item["subject"] for item in disliked))
    if strengths:
        lines.append("- Môn em tự nhận thấy học tốt: " + "; ".join(
            f"{item['subject']} ({item['description']})" for item in strengths))
    if confidence:
        lines.append("- Mức tự tin em tự khai: " + ", ".join(
            f"{item['subject']} {item['value']}/5" for item in confidence))
    if grades:
        lines.append("- Điểm em tự khai: " + ", ".join(
            f"{item['subject']} {item['value']}/{item['scale']}" for item in grades)
            + ". Mình chỉ ghi nhận, chưa dùng điểm này để suy ra năng lực hay xếp hạng nhóm.")
    if unscaled:
        lines.append("- Chưa dùng điểm cho đến khi rõ thang điểm: "
                     + ", ".join(item.get("subject", "môn học") for item in unscaled))
    if desired_program:
        lines.append(f"- Ngành/lĩnh vực em đang quan tâm: {desired_program}")
    numeric_only = bool(confidence or grades or unscaled) and not (preferred or disliked or strengths or desired_program)
    if numeric_only:
        lines.append("Chưa có tên trường THPT nên mình chưa thể đối chiếu nhóm môn. "
                     "Em muốn ưu tiên môn nào, hoặc tiêu chí nào khi cân nhắc nhóm môn?")
    else:
        lines.append("Chưa có tên trường THPT nên mình chưa thể đối chiếu các nhóm môn thực tế của trường. "
                     "Em đang nhắm tới trường nào? Khi có tên trường, mình sẽ so sánh theo đúng các tiêu chí em đã nêu.")
    return _chat_result("incomplete", "\n".join(lines), request_id,
                        str(intent.get("intent", "subject_group_advice")))


def _comparison_group_code_key(value: Any) -> str:
    """Normalize comparison aliases such as G02 and 2 without resolving catalog identity."""
    text = str(value or "").strip().upper()
    match = re.fullmatch(r"G?0*(\d{1,3})", text)
    return str(int(match.group(1))) if match else text.casefold()


def _explicit_group_codes_by_school(message: str, school_labels: List[str]) -> Dict[str, set[str]]:
    """Associate explicit group wording with nearby school clauses from the user turn.

    This only extracts user wording. The comparison handler still resolves each
    alias against that school's year-scoped catalog before returning evidence.
    """
    result = {label: set() for label in school_labels}
    normalized = IntentParser._normalize_evidence_text(message)
    for clause in re.split(r"\b(?:va|voi)\b|[,;]", normalized):
        labels_in_clause = [
            label for label in school_labels
            if IntentParser._normalize_evidence_text(label)
            and IntentParser._normalize_evidence_text(label) in clause
        ]
        # If a clause names multiple schools, its group mention cannot be paired
        # safely without a more explicit user phrase or parser-verified pair.
        if len(labels_in_clause) != 1:
            continue
        label = labels_in_clause[0]
        numbers = re.findall(r"\bnhom(?: mon)?\s+(?:g\s*)?0*(\d{1,3})\b", clause)
        numbers.extend(re.findall(r"\bma\s+g\s*0*(\d{1,3})\b", clause))
        result[label].update(str(int(number)) for number in numbers)
    return result


def _compare_subject_groups(
    intent: Dict[str, Any], request_id: str,
    context: Optional[List[Dict[str, str]]] = None,
    current_message: str = "",
) -> ChatResponse:
    school_labels = intent.get("high_schools")
    if isinstance(school_labels, list):
        unique_labels = list(dict.fromkeys(str(label).strip() for label in school_labels if str(label).strip()))
        if len(unique_labels) > 1:
            if len(unique_labels) != 2:
                return _chat_result(
                    "incomplete", "Mình có thể đối chiếu hai trường mỗi lần. Em chọn hai trường muốn so sánh nhé.",
                    request_id, str(intent.get("intent")), {"school_queries": unique_labels},
                )
            raw_codes = intent.get("requested_group_codes", [])
            pair_evidence_messages = [current_message, *[
                str(turn.get("content") or "") for turn in (context or [])
                if isinstance(turn, dict) and turn.get("role") == "user"
            ]]
            verified_pairs = IntentParser._verified_school_group_pairs(
                intent.get("requested_school_group_pairs"), pair_evidence_messages
            )
            distinct_codes = {_comparison_group_code_key(item.get("code"))
                              for item in raw_codes if isinstance(item, dict) and item.get("code")}
            distinct_codes.update(_comparison_group_code_key(pair["code"]) for pair in verified_pairs)
            normalized_message = IntentParser._normalize_evidence_text(current_message)
            explicit_pairs = _explicit_group_codes_by_school(current_message, unique_labels)
            for label in unique_labels:
                normalized_school = IntentParser._normalize_evidence_text(label)
                explicit_pairs[label].update(_comparison_group_code_key(code) for code in re.findall(
                    rf"\b(?:nhom\s+)?([a-z]?\d{{1,3}})\s+cua\s+(?:truong\s+)?{re.escape(normalized_school)}\b",
                    normalized_message,
                ))
            distinct_codes.update(code for codes in explicit_pairs.values() for code in codes)
            pair_codes: Dict[str, str] = {}
            if len(distinct_codes) > 1 or any(explicit_pairs.values()):
                for pair in verified_pairs:
                    matches = [label for label in unique_labels
                               if _school_catalog_match_key(label) == _school_catalog_match_key(pair["school"])]
                    if len(matches) != 1:
                        continue
                    school_key = matches[0]
                    if school_key in pair_codes and pair_codes[school_key] != pair["code"]:
                        pair_codes = {}
                        break
                    pair_codes[school_key] = _comparison_group_code_key(pair["code"])
                # The parser may omit the optional pair field. Recover only an
                # explicit "G04 của THPT A" clause from the current user turn.
                for label in unique_labels:
                    matching_codes = explicit_pairs[label]
                    if len(matching_codes) > 1:
                        pair_codes = {}
                        break
                    if len(matching_codes) == 1:
                        pair_codes[label] = next(iter(matching_codes))
                if len(pair_codes) != 2 or set(pair_codes.values()) != distinct_codes:
                    return _chat_result(
                        "incomplete",
                        "Em cho mình biết rõ mã nhóm nào thuộc từng trường trong hai trường này nhé.",
                        request_id, str(intent.get("intent")),
                        {"comparison_status": "ambiguous_pairs", "requested_schools": unique_labels},
                    )
            results = []
            for label in unique_labels:
                filters_for_school = dict(intent.get("extracted_filters", {}))
                filters_for_school["high_school"] = label
                single_school_intent = {**intent, "high_schools": [label],
                                        "extracted_filters": filters_for_school}
                if pair_codes:
                    single_school_intent["requested_group_codes"] = [
                        {"code": pair_codes[label], "evidence": current_message}
                    ]
                result = _compare_subject_groups(
                    single_school_intent, request_id, context=context, current_message=current_message
                )
                results.append(result)
            successful_results = [result for result in results if result.status == "success"]
            unavailable_results = [result for result in results if result.status != "success"]
            if unavailable_results:
                if not successful_results:
                    return _chat_result(
                        "incomplete", "Mình chưa tìm thấy dữ liệu đã xác minh cho hai trường trong phạm vi yêu cầu.",
                        request_id, str(intent.get("intent")),
                        {"comparison_status": "no_verified_school_data", "requested_schools": unique_labels,
                         "partial_comparisons": [result.data for result in results]},
                    )
                def missing_group_result(result: ChatResponse) -> bool:
                    data = result.data if isinstance(result.data, dict) else {}
                    resolutions = data.get("group_code_resolutions", [])
                    return (result.status == "incomplete"
                            and isinstance(data.get("school"), dict)
                            and isinstance(resolutions, list)
                            and any(isinstance(item, dict) and item.get("status") == "unknown"
                                    for item in resolutions))

                if any(not missing_group_result(result) and result.status != "no_data"
                       for result in unavailable_results):
                    return _chat_result(
                        "incomplete", "Mình cần làm rõ trường hoặc nhóm môn chưa được xác định trước khi đối chiếu.",
                        request_id, str(intent.get("intent")),
                        {"comparison_status": "unresolved_school_or_group", "requested_schools": unique_labels,
                         "partial_comparisons": [result.data for result in results]},
                    )
                sections = []
                for result in results:
                    school_data = (result.data or {}).get("school", {})
                    school_name = school_data.get("name", school_data.get("code", "Trường chưa xác định"))
                    if result.status == "success":
                        sections.append(f"**{school_name}**\n{result.message}")
                    elif result.status == "no_data":
                        sections.append(f"**{school_name}**\nChưa có dữ liệu nhóm môn đã xác minh cho phạm vi này.")
                    elif missing_group_result(result):
                        codes = ", ".join(item["raw_code"] for item in result.data["group_code_resolutions"]
                                          if item.get("status") == "unknown")
                        sections.append(f"**{school_name}**\nChưa tìm thấy nhóm {codes} trong kết quả đã tra; điều này chưa kết luận rằng trường không có nhóm đó.")
                    else:
                        sections.append(f"**{school_name}**\nChưa xác định hoặc chưa đủ thông tin để tra cứu trường này.")
                return _chat_result(
                    "success", "\n\n".join(sections), request_id, str(intent.get("intent")),
                    {"comparisons": [
                         {**result.data.get("comparison", {}),
                          "schoolCode": (result.data.get("school") or {}).get("code"),
                          "schoolName": (result.data.get("school") or {}).get("name")}
                         for result in successful_results],
                     "evidence": [
                         {**result.data.get("comparison", {}),
                          "schoolCode": (result.data.get("school") or {}).get("code"),
                          "schoolName": (result.data.get("school") or {}).get("name")}
                         for result in successful_results],
                     "evidence_source": "backend_subject_group_comparison_api",
                     "comparison_mode": "cross_school",
                     "answer_completeness": "partial",
                     "unavailable_school_codes": [
                         str((result.data or {}).get("school", {}).get("code", label))
                         for label, result in zip(unique_labels, results)
                         if result.status == "no_data"
                     ],
                     "unavailable_group_requests": [
                         {"school_code": str(result.data["school"].get("code", label)),
                          "group_code": item["raw_code"]}
                         for label, result in zip(unique_labels, results)
                         if missing_group_result(result)
                         for item in result.data["group_code_resolutions"]
                         if item.get("status") == "unknown"
                     ],
                     "requested_schools": unique_labels},
                )
            sections = []
            for result in results:
                school_data = result.data.get("school", {})
                sections.append(f"**{school_data.get('name', school_data.get('code'))}**\n{result.message}")
            comparisons = [result.data.get("comparison", {}) for result in results]
            selected_groups = [comparison["groups"][0] for comparison in comparisons
                               if len(comparison.get("groups", [])) == 1]
            subject_maps = []
            for group in selected_groups:
                subjects = group.get("groupSubjects")
                if not isinstance(subjects, list):
                    subjects = []
                subject_maps.append({str(subject["code"]): subject for subject in subjects
                                     if isinstance(subject, dict) and subject.get("code")})
            shared_subjects: List[Dict[str, Any]] = []
            school_only_subjects: List[List[Dict[str, Any]]] = [[], []]
            if len(subject_maps) == 2 and all(subject_maps):
                shared_codes = set(subject_maps[0]) & set(subject_maps[1])
                shared_subjects = [subject_maps[0][code] for code in sorted(shared_codes)]
                school_only_subjects = [
                    [subjects[code] for code in sorted(set(subjects) - shared_codes)]
                    for subjects in subject_maps
                ]
                shared_names = ", ".join(str(row.get("name") or row["code"]) for row in shared_subjects)
                sections.append("**Đối chiếu hai nhóm đã hỏi**\n- Môn chung: "
                                + (shared_names or "không có trong hai danh sách đã xác minh"))
                for index, result in enumerate(results):
                    school = result.data["school"]
                    group = selected_groups[index]
                    only_names = ", ".join(str(row.get("name") or row["code"])
                                           for row in school_only_subjects[index])
                    sections.append(f"- Chỉ có trong {school.get('name')} · {group.get('groupCode')}: "
                                    + (only_names or "không có trong hai danh sách đã xác minh"))
            return _chat_result(
                "success", "\n\n".join(sections), request_id, str(intent.get("intent")),
                {"comparisons": comparisons, "evidence": comparisons,
                 "evidence_source": "backend_subject_group_comparison_api",
                 "comparison_mode": "cross_school",
                 "shared_subjects": shared_subjects,
                 "school_only_subjects": school_only_subjects,
                 "answer_completeness": "complete" if len(subject_maps) == 2 and all(subject_maps)
                 else "partial"},
            )

    school, error = _school_from_intent(intent, request_id, context, current_message)
    if error:
        return error
    assert school is not None
    preferred: Dict[str, str] = {}
    disliked: Dict[str, str] = {}
    grades: Dict[str, Dict[str, Any]] = {}
    strengths: Dict[str, str] = {}
    confidence: Dict[str, int] = {}
    filters = intent.get("extracted_filters", {})
    desired_program = filters.get("desired_program")
    university_code, university_name = _university_search_filter(filters.get("university"))
    unscaled = intent.get("grades_needing_scale", [])
    try:
        catalog = _backend_get("/api/v1/subjects/classifications") or {}
        signals = [*intent.get("preferred_subjects", []), *intent.get("disliked_subjects", []),
                   *intent.get("self_reported_strengths", []), *intent.get("confidence_by_subject", []),
                   *intent.get("grades_by_subject", [])]
        resolved: Dict[str, str] = {}
        unresolved: List[str] = []
        for signal in signals:
            label = signal.get("subject", "")
            code, matches = _resolve_subject(label, catalog)
            if code:
                resolved[label] = code
            else:
                unresolved.append(label)
        if unresolved:
            return _chat_result("incomplete", "Mình chưa xác định duy nhất được các môn sau: "
                                + ", ".join(dict.fromkeys(unresolved))
                                + ". Em dùng tên đầy đủ trong danh mục môn học nhé.", request_id,
                                str(intent.get("intent")), {"unresolved_subjects": list(dict.fromkeys(unresolved))})

        for signal in intent.get("preferred_subjects", []):
            preferred[resolved[signal["subject"]]] = signal.get("subject", "")
        for signal in intent.get("disliked_subjects", []):
            disliked[resolved[signal["subject"]]] = signal.get("subject", "")
        conflicts = set(preferred) & set(disliked)
        if conflicts:
            names = ", ".join(preferred[code] for code in conflicts)
            return _chat_result("incomplete", f"Em vừa nêu {names} là môn thích và không thích. Em muốn chọn ý nào?", request_id, str(intent.get("intent")))
        for signal in intent.get("self_reported_strengths", []):
            strengths[resolved[signal["subject"]]] = signal["description"]
        for signal in intent.get("confidence_by_subject", []):
            confidence[resolved[signal["subject"]]] = signal["value"]
        for signal in intent.get("grades_by_subject", []):
            grades[resolved[signal["subject"]]] = {
                "value": signal["value"], "scale": signal["scale"],
                "assessmentType": signal.get("assessment_type"),
            }

        has_profile_signals = bool(preferred or disliked or grades or strengths or confidence)
        objective_comparison = intent.get("intent") == "subject_group_comparison" and not has_profile_signals
        if not (has_profile_signals or desired_program or objective_comparison):
            if unscaled:
                subjects = ", ".join(item.get("subject", "môn học") for item in unscaled)
                prompt = f"Em đang dùng thang điểm nào cho {subjects} (ví dụ 10 hoặc 100)?"
            else:
                prompt = "Em chia sẻ môn yêu thích/không yêu thích, môn em thấy học tốt, mức tự tin 1–5, điểm kèm thang điểm, hoặc ngành đang quan tâm nhé."
            return _chat_result("incomplete", prompt, request_id, str(intent.get("intent")))

        payload = {
            "academicYear": intent.get("academic_year"),
            "admissionYear": filters.get("admission_year"),
            "methodCode": filters.get("method_code"),
            "preferredSubjectCodes": list(preferred),
            "dislikedSubjectCodes": list(disliked),
            "gradesBySubjectCode": grades,
            "selfReportedStrengthBySubjectCode": strengths,
            "confidenceBySubjectCode": confidence,
            "desiredProgramName": desired_program,
            "universityCode": university_code,
            "universityName": university_name,
        }
        result = _backend_post(
            f"/api/v1/high-schools/{quote(str(school['code']), safe='')}/subject-groups/comparisons", payload)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return _chat_result("no_data", "Chưa có nhóm môn đang công bố phù hợp với trường và năm học này.", request_id,
                                str(intent.get("intent")), {"school": school, "academic_year": intent.get("academic_year")})
        if exc.response is not None and exc.response.status_code == 400:
            return _chat_result("incomplete", "Một vài thông tin môn học chưa khớp với danh mục. Em kiểm tra tên môn hoặc thang điểm rồi gửi lại nhé.", request_id, str(intent.get("intent")))
        logger.warning("chat request_id=%s backend=group_comparison http_error", request_id)
        raise HTTPException(status_code=503, detail="Dịch vụ đối chiếu nhóm môn tạm thời chưa sẵn sàng.") from exc
    except requests.RequestException as exc:
        logger.warning("chat request_id=%s backend=group_comparison unavailable", request_id)
        raise HTTPException(status_code=503, detail="Dịch vụ đối chiếu nhóm môn tạm thời chưa sẵn sàng.") from exc

    objective_comparison = intent.get("intent") == "subject_group_comparison" and not (
        preferred or disliked or grades or strengths or confidence
    )
    lines = [f"Đối chiếu các nhóm môn tại **{school['name']}** · năm học **{result['academicYear']}**:"]
    if objective_comparison:
        lines.append("Đây là phần đối chiếu thành phần môn và các tổ hợp có liên kết; chưa dùng hồ sơ cá nhân để tư vấn.")
    if unscaled:
        lines.append("\nĐiểm chưa được đưa vào đối chiếu vì chưa rõ thang điểm: "
                     + ", ".join(item.get("subject", "môn học") for item in unscaled) + ".")
    requested_codes = intent.get("requested_group_codes", [])
    groups, missing_codes, group_code_resolutions = _filter_requested_groups(
        result.get("groups", []), requested_codes,
        school_code=str(school["code"]), academic_year=str(result.get("academicYear", "")),
    )
    if any(item["status"] == "ambiguous" for item in group_code_resolutions):
        codes = ", ".join(item["raw_code"] for item in group_code_resolutions
                           if item["status"] == "ambiguous")
        return _chat_result("incomplete", f"Mã nhóm {codes} có nhiều cách khớp tại trường và năm học này. Em xác nhận mã giúp mình nhé.",
                            request_id, str(intent.get("intent")),
                            {"school": school, "academic_year": result.get("academicYear"),
                             "group_code_resolutions": group_code_resolutions})
    if missing_codes and not groups:
        available = [str(group.get("groupCode")) for group in result.get("groups", [])]
        return _chat_result("incomplete", "Mình chưa tìm thấy mã nhóm " + ", ".join(missing_codes)
                            + ". Các mã nhóm hiện có trong kết quả là: " + (", ".join(available) or "không có") + ".",
                            request_id, str(intent.get("intent")),
                            {"school": school, "academic_year": result.get("academicYear"),
                             "available_group_codes": available,
                             "group_code_resolutions": group_code_resolutions})
    if not groups:
        return _chat_result("no_data", "Không tìm thấy nhóm môn được yêu cầu tại trường này.", request_id,
                            str(intent.get("intent")), {"school": school,
                                                        "academic_year": result.get("academicYear"),
                                                        "group_code_resolutions": group_code_resolutions})
    result = {**result, "groups": groups,
              "schoolCode": str(school.get("code") or ""),
              "schoolName": str(school.get("name") or "")}
    incomplete_group_membership = False
    tentative_candidates: List[Dict[str, Any]] = []
    stated_strengths = {resolved[item["subject"]]: item.get("subject", "")
                        for item in intent.get("self_reported_strengths", [])
                        if isinstance(item, dict) and item.get("subject") in resolved}
    for group in groups:
        group_subjects = group.get("groupSubjects")
        verified_preferred: List[Dict[str, Any]] = []
        if not isinstance(group_subjects, list) or not group_subjects:
            incomplete_group_membership = True
            names = "chưa có danh sách môn thành phần được xác minh"
        else:
            names = ", ".join(subject.get("name", subject.get("code", ""))
                              for subject in group_subjects if isinstance(subject, dict))
            if not names:
                incomplete_group_membership = True
                names = "chưa có danh sách môn thành phần được xác minh"
        lines.append(f"\n**{group.get('groupName')} ({group.get('groupCode')})** — {names}")
        if intent.get("intent") == "subject_group_advice" and isinstance(group_subjects, list):
            verified_preferred = [subject for subject in group_subjects
                                  if isinstance(subject, dict) and str(subject.get("code", "")) in preferred]
            verified_strengths = [subject for subject in group_subjects
                                  if isinstance(subject, dict) and str(subject.get("code", "")) in stated_strengths]
            if verified_preferred or verified_strengths:
                tentative_candidates.append({
                    "groupCode": str(group.get("groupCode", "")),
                    "matchedSubjects": list(dict.fromkeys(
                        [str(subject.get("name") or preferred[str(subject["code"])])
                         for subject in verified_preferred]
                        + [str(subject.get("name") or stated_strengths[str(subject["code"])])
                           for subject in verified_strengths]
                    )),
                })
        mandatory_subjects = group.get("mandatorySubjects")
        if isinstance(mandatory_subjects, list) and mandatory_subjects:
            mandatory_names = ", ".join(item.get("name", item.get("code", ""))
                                         for item in mandatory_subjects if isinstance(item, dict))
            lines.append(f"- Môn bắt buộc/common (tách riêng với thành phần nhóm): {mandatory_names}")
        liked = ", ".join(item.get("name", "") for item in group.get("matchedPreferredSubjects", []))
        if intent.get("intent") == "subject_group_advice":
            liked = ", ".join(str(item.get("name") or preferred.get(str(item.get("code", "")), ""))
                               for item in verified_preferred)
        disliked_names = ", ".join(item.get("name", "") for item in group.get("matchedDislikedSubjects", []))
        if preferred:
            lines.append(f"- Có môn em thích: {liked or 'chưa có môn nào trong nhóm'}")
        if disliked:
            lines.append(f"- Có môn em không thích: {disliked_names or 'không có môn em đã nêu'}")
        if group.get("gradeSignals"):
            grades_text = ", ".join(
                f"{item.get('subjectName')} {item.get('value')}/{item.get('scale')}"
                + (f" ({item['assessmentType']})" if item.get("assessmentType") else " (chưa ghi loại điểm)")
                for item in group["gradeSignals"])
            lines.append(f"- Điểm em đã khai: {grades_text}")
        if group.get("strengthSignals"):
            strengths_text = ", ".join(f"{item.get('subjectName')}: {item.get('description')}"
                                        for item in group["strengthSignals"])
            lines.append(f"- Em tự nhận xét: {strengths_text}")
        if group.get("confidenceSignals"):
            confidence_text = ", ".join(f"{item.get('subjectName')}: {item.get('value')}/5"
                                         for item in group["confidenceSignals"])
            lines.append(f"- Mức tự tin em tự khai: {confidence_text}")
        query_scope = _normalize_label(current_message)
        asks_combinations = any(term in query_scope for term in ("tohop", "xettuyen", "tuyensinh"))
        combinations = group.get("matchingAdmissionCombinations", [])
        if not asks_combinations:
            combinations = [item for item in combinations
                            if isinstance(item, dict) and item.get("pathwaysForRequestedProgram")]
        for combination in combinations:
            labels = ", ".join(f"{label.get('code')} ({label.get('subjectDetails')})"
                                for label in combination.get("publicLabels", []))
            lines.append(f"- Tổ hợp có đủ môn trong nhóm: {combination.get('combinationDisplay')}"
                         + (f"; mã công khai: {labels}" if labels else ""))
            for pathway in combination.get("pathwaysForRequestedProgram", []):
                source = pathway.get("sourceUrl")
                source_text = f" [Nguồn tuyển sinh {pathway.get('admissionYear')}]({source})" if source else f" (dữ liệu {pathway.get('admissionYear')})"
                lines.append(f"  - Có offer tới {pathway.get('programName')} · {pathway.get('universityName')}{source_text}")
    if intent.get("intent") == "subject_group_advice":
        if tentative_candidates:
            group_codes = [candidate["groupCode"] for candidate in tentative_candidates]
            matched_names = list(dict.fromkeys(
                name for candidate in tentative_candidates for name in candidate["matchedSubjects"]
            ))
            if len(group_codes) == 1:
                candidate_text = f"Nhóm {group_codes[0]} là một ứng viên tạm thời để em cân nhắc"
            else:
                candidate_text = ("Các nhóm " + ", ".join(group_codes)
                                  + " là những ứng viên tạm thời để em cân nhắc")
            lines.append("\n" + candidate_text + ", vì có môn " + ", ".join(matched_names)
                         + " trong thành phần đã xác minh và em đã nêu là môn yêu thích hoặc tự nhận thấy học tốt. "
                         + "Đây là gợi ý tạm thời, không xếp hạng các nhóm.")
        else:
            lines.append("\nChưa đủ bằng chứng để nêu nhóm nào là ứng viên tạm thời. "
                         + "Em muốn ưu tiên môn nào hoặc tiêu chí nào để mình đối chiếu tiếp?")
    lines.append("\nM\u1ee9c ph\u00f9 h\u1ee3p ch\u1ec9 l\u00e0 g\u1ee3i \u00fd \u0111\u1ec3 c\u00e2n nh\u1eafc d\u1ef1a tr\u00ean s\u1edf th\u00edch v\u00e0 c\u00e1c m\u00f4n \u0111\u00e3 x\u00e1c minh; kh\u00f4ng b\u1ea3o \u0111\u1ea3m ph\u00f9 h\u1ee3p, kh\u00f4ng ph\u1ea3i th\u1ee9 h\u1ea1ng, x\u00e1c su\u1ea5t \u0111\u1ed7 hay x\u00e1c nh\u1eadn \u0111\u1ee7 \u0111i\u1ec1u ki\u1ec7n tuy\u1ec3n sinh.")
    return _chat_result("success", "\n".join(lines), request_id, str(intent.get("intent")),
                        {"school": school, "academic_year": result.get("academicYear"),
                         "comparison": result, "evidence": result,
                         "evidence_source": "backend_subject_group_comparison_api",
                         "comparison_mode": "objective" if objective_comparison else "personalized",
                         "tentative_candidate_groups": [candidate["groupCode"] for candidate in tentative_candidates],
                         "group_code_resolutions": group_code_resolutions,
                         "answer_completeness": "partial" if missing_codes or incomplete_group_membership
                         or (intent.get("intent") == "subject_group_advice" and not tentative_candidates)
                         else "complete",
                         "unavailable_group_codes": missing_codes})


@app.get("/health", tags=["Health Check"], summary="Kiểm tra trạng thái hệ thống")
async def health_check() -> Dict[str, Any]:
    """Health check endpoint checking service and Neo4j connectivity."""
    neo4j_status = "connected"
    try:
        if retriever:
            retriever.get_driver()
    except Exception:
        neo4j_status = "disconnected"

    return {
        "status": "healthy",
        "service": "Hybrid GraphRAG Admissions Advisor API",
        "neo4j_status": neo4j_status,
        "ollama_host": settings.OLLAMA_HOST,
        "llm_model": settings.LLM_MODEL
    }


if __name__ == "__main__":
    uvicorn.run(
        "rag_api.main_api:app",
        host=settings.API_HOST,
        port=settings.API_PORT,
        reload=True
    )

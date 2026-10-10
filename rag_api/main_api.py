# -*- coding: utf-8 -*-
"""
main_api.py

FastAPI Application Entry Point & RAG Orchestrator API.
Connects IntentParser (Router), Neo4jRetriever (Hybrid Search), and RAGGenerator (Claude Sonnet).
Fully compliant with system architecture and coding guidelines.
"""

from contextlib import asynccontextmanager
import os
import re
import sys
import unicodedata
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
import requests

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.llm.intent_parser import IntentParser
from rag_api.llm.llm_client import RAGGenerator
from rag_api.retrieval.graph_retriever import Neo4jRetriever
from rag_api.utils.config import settings
from rag_api.utils.logger import logger

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

    parser = IntentParser()
    retriever = Neo4jRetriever()
    generator = RAGGenerator()

    # Pre-verify Neo4j connectivity on startup
    try:
        retriever.get_driver()
        logger.info("Neo4j Database connection successfully verified on startup.")
    except Exception as e:
        logger.warning(f"Neo4j connection startup check warning: {e}")

    yield

    logger.info("Shutting down Hybrid GraphRAG Admissions Advisor Services...")
    if retriever:
        retriever.close()


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


# Pydantic Schemas for Request / Response
class ChatRequest(BaseModel):
    user_message: str = Field(
        ...,
        description="Tin nhắn câu hỏi mới của người dùng",
        json_schema_extra={"example": "Em muốn học ngành Công nghệ thông tin trường Quy Nhơn điểm bao nhiêu?"}
    )
    context: List[Dict[str, str]] = Field(
        default_factory=list,
        description="Lịch sử ngữ cảnh hội thoại trước đó [{\"role\": \"user\"|\"assistant\", \"content\": \"...\"}]"
    )
    chat_history: Optional[List[Dict[str, str]]] = Field(
        default=None,
        description="Tên cũ của ngữ cảnh (tùy chọn dự phòng tương thích)"
    )
    score_threshold: Optional[float] = Field(
        default=None,
        description="Ngưỡng độ tương đồng tối thiểu cho vector search (mặc định từ settings.VECTOR_SIMILARITY_THRESHOLD = 0.65)"
    )
    override_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Cấu hình ghi đè tùy chọn từ client (api_key, model, base_url)"
    )

    def get_conversation_context(self) -> List[Dict[str, str]]:
        """Return conversation context supporting both 'context' and legacy 'chat_history' fields."""
        if self.context and len(self.context) > 0:
            return self.context
        return self.chat_history or []


class ChatResponse(BaseModel):
    status: str = Field(..., description="Trạng thái xử lý: 'ready', 'incomplete', hoặc 'success'")
    message: str = Field(..., description="Nội dung phản hồi cho người dùng")
    data: Optional[Dict[str, Any]] = Field(default=None, description="Dữ liệu chi tiết đính kèm (filters, records_count...)")


@app.post(
    "/api/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Xử lý câu hỏi RAG Tư vấn Tuyển sinh",
    description="Luồng xử lý: Parse Intent -> Check Completeness -> Retrieve Neo4j Context -> Generate Answer."
)
async def chat_endpoint(request: ChatRequest) -> ChatResponse:
    """
    POST /api/chat Orchestrator Endpoint.

    Step 1: Pass user_message & conversation context to parser.parse_intent().
    Step 2: Check status:
            - If 'incomplete': Return reply_to_user immediately to Frontend without querying Database.
            - If 'ready': Extract filters and proceed.
    Step 3: Call retriever.retrieve_context(filters) & format_for_llm(records).
    Step 4: Call generator.generate_answer(user_message, graph_context, context).
    Step 5: Return ChatResponse to client.
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

    conversation_context = request.get_conversation_context()

    # Step 1 & 2: Parse Intent & Query Routing
    try:
        intent_result = parser.parse_intent(
            user_message=request.user_message,
            context=conversation_context
        )
        intent_status = intent_result.get("status", "incomplete")
        extracted_filters = intent_result.get("extracted_filters", {})
        reply_to_user = intent_result.get("reply_to_user")

        if intent_result.get("intent") == "subject_group_recommendation":
            return _recommend_subject_group(intent_result)

        if intent_status == "incomplete" or not parser.is_ready_for_db(intent_result):
            msg = reply_to_user or "Bạn đang quan tâm đến ngành học/lĩnh vực nào hoặc trường đại học nào để mình hỗ trợ tra cứu chi tiết nhé?"
            return ChatResponse(
                status="incomplete",
                message=msg,
                data={"extracted_filters": extracted_filters}
            )

    except Exception as e:
        logger.error(f"Error during Intent Parsing step: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi khi phân tích ý định người dùng: {str(e)}"
        )

    # Step 3: Retrieve Context from Neo4j Knowledge Graph
    try:
        score_threshold = request.score_threshold
        records = retriever.retrieve_context(filters=extracted_filters, top_k=3, score_threshold=score_threshold)
        formatted_context = retriever.format_for_llm(records)
    except Exception as e:
        logger.error(f"Error during Neo4j Retrieval step: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi khi truy vấn Cơ sở dữ liệu Đồ thị: {str(e)}"
        )

    # Step 4: Generate LLM Answer
    try:
        active_generator = generator
        if request.override_config:
            ov = request.override_config
            if ov.get("api_key") or ov.get("model") or ov.get("base_url"):
                active_generator = RAGGenerator(
                    api_base=ov.get("base_url") or None,
                    api_key=ov.get("api_key") or None,
                    model=ov.get("model") or None
                )

        answer_text = active_generator.generate_answer(
            user_query=request.user_message,
            graph_context=formatted_context,
            context=conversation_context
        )
    except Exception as e:
        logger.error(f"Error during Answer Generation step: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi khi sinh câu trả lời AI: {str(e)}"
        )

    # Step 5: Return Success ChatResponse
    return ChatResponse(
        status="success",
        message=answer_text,
        data={
            "filters": extracted_filters,
            "retrieved_count": len(records),
            "context_summary": formatted_context[:200] + "..." if len(formatted_context) > 200 else formatted_context
        }
    )


def _normalize_label(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value.casefold())
    without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"[^a-z0-9]+", "", without_marks.replace("đ", "d"))


def _api_data(response: requests.Response) -> Any:
    response.raise_for_status()
    body = response.json()
    return body.get("data") if isinstance(body, dict) else None


def _recommend_subject_group(intent: Dict[str, Any]) -> ChatResponse:
    """Resolve model-extracted labels against trusted catalogs, then delegate ranking to Spring."""
    filters = intent.get("extracted_filters", {})
    school_label = filters.get("high_school") if isinstance(filters, dict) else None
    preferred_labels = intent.get("preferred_subjects", [])
    confidence_labels = intent.get("confidence_by_subject", {})
    if not school_label:
        return ChatResponse(status="incomplete", message="Em đang học hoặc muốn tìm hiểu trường THPT nào?", data=None)
    if not preferred_labels and not confidence_labels:
        return ChatResponse(status="incomplete", message="Em hãy cho biết vài môn mình yêu thích hoặc tự đánh giá mức tự tin từ 1 đến 5 nhé.", data=None)

    base_url = settings.SUBJECT_PATHWAY_API_URL
    timeout = settings.SUBJECT_PATHWAY_API_TIMEOUT
    try:
        schools = _api_data(requests.get(f"{base_url}/api/v1/high-schools", timeout=timeout)) or []
        school_key = _normalize_label(str(school_label))
        school = next((item for item in schools if school_key in {
            _normalize_label(str(item.get("code", ""))), _normalize_label(str(item.get("name", "")))
        } or school_key in _normalize_label(str(item.get("name", "")))), None)
        if not school:
            return ChatResponse(status="incomplete", message="Mình chưa khớp được tên trường đó với danh mục. Em gửi mã trường hoặc tên đầy đủ nhé.", data={"school_query": school_label})

        years = _api_data(requests.get(
            f"{base_url}/api/v1/high-schools/{school['code']}/academic-years", timeout=timeout)) or []
        requested_year = intent.get("academic_year")
        year = next((item for item in years if str(item) == str(requested_year)), None) if requested_year else (years[0] if years else None)
        if not year:
            return ChatResponse(status="incomplete", message="Trường này chưa có năm học trong dữ liệu để gợi ý. Em thử chọn trường khác hoặc hỏi lại sau nhé.", data={"school": school})

        catalog = _api_data(requests.get(f"{base_url}/api/v1/subjects/classifications", timeout=timeout)) or {}
        subjects = (catalog.get("mandatorySubjects", []) + catalog.get("electiveSubjects", [])) if isinstance(catalog, dict) else []
        lookup = {}
        for subject in subjects:
            for candidate in (subject.get("code", ""), subject.get("name", "")):
                if candidate:
                    lookup[_normalize_label(str(candidate))] = str(subject.get("code", ""))

        def resolve_subject(label: str) -> Optional[str]:
            key = _normalize_label(label)
            if key in lookup:
                return lookup[key]
            matches = {code for candidate, code in lookup.items() if key and (key in candidate or candidate in key)}
            return next(iter(matches)) if len(matches) == 1 else None

        preferred_codes = list(dict.fromkeys(code for label in preferred_labels if isinstance(label, str)
                                               for code in [resolve_subject(label)] if code))
        confidence_codes = {}
        if isinstance(confidence_labels, dict):
            for label, score in confidence_labels.items():
                code = resolve_subject(str(label))
                if code and type(score) is int and 1 <= score <= 5:
                    confidence_codes[code] = score
        if len(preferred_codes) != len(preferred_labels) or (isinstance(confidence_labels, dict) and len(confidence_codes) != len(confidence_labels)):
            return ChatResponse(status="incomplete", message="Mình chưa nhận diện chắc chắn một số tên môn. Em dùng tên môn trong chương trình THPT (ví dụ Vật lí, Hóa học, Tin học) nhé.", data=None)

        result = _api_data(requests.post(
            f"{base_url}/api/v1/high-schools/{school['code']}/subject-groups/recommendations",
            json={"academicYear": year, "preferredSubjectCodes": preferred_codes,
                  "confidenceBySubjectCode": confidence_codes, "topK": 3}, timeout=timeout))
        rows = result.get("recommendations", []) if isinstance(result, dict) else []
        if not rows:
            message = f"Mình chưa tìm thấy nhóm đủ 4 môn cho {school['name']} trong năm học {year}."
        else:
            lines = [f"Gợi ý nhóm môn lớp 10 tại {school['name']} · năm học {year} (Top {len(rows)}):"]
            for index, row in enumerate(rows, 1):
                names = ", ".join(item.get("name", "") for item in row.get("subjects", []))
                lines.append(f"{index}. **{row.get('groupName', row.get('groupCode'))}** — phù hợp {row.get('score')}%\n   {names}")
                low = ", ".join(item.get("name", "") for item in row.get("lowConfidenceSubjects", []))
                if low:
                    lines.append(f"   Có thể tìm hiểu thêm: {low}.")
            lines.append("\nĐiểm này chỉ giúp khám phá lựa chọn, không dự đoán kết quả tuyển sinh hay mức độ đủ điều kiện.")
            message = "\n".join(lines)
        return ChatResponse(status="success", message=message, data={"subject_group_recommendations": result})
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {400, 404}:
            return ChatResponse(status="incomplete", message="Các môn em nêu chưa khớp với nhóm môn của trường trong năm học đó. Em thử điều chỉnh môn yêu thích hoặc năm học nhé.", data=None)
        logger.error(f"Subject-group recommendation API returned an HTTP error: {exc}")
        raise HTTPException(status_code=503, detail="Dịch vụ dữ liệu trường học hiện chưa kết nối được.")
    except requests.RequestException as exc:
        logger.error(f"Subject-group recommendation API unavailable: {exc}")
        raise HTTPException(status_code=503, detail="Dịch vụ dữ liệu trường học hiện chưa kết nối được.")


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

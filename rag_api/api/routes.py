# -*- coding: utf-8 -*-
"""
api/routes.py

FastAPI router declaring the POST /api/chat endpoint for Hybrid GraphRAG queries.
"""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

from rag_api.retrieval.graph_retriever import Neo4jRetriever
from rag_api.llm.llm_client import LLMService
from rag_api.utils.logger import logger

router = APIRouter(prefix="/api", tags=["GraphRAG Chatbot"])

# Global service instances (initialized in main_api.py lifecycle)
retriever_instance: Optional[Neo4jRetriever] = None
llm_instance: Optional[LLMService] = None


class ChatRequest(BaseModel):
    query: str = Field(
        ...,
        description="User query string",
        example="Ngành Công nghệ thông tin trường Quy Nhơn lấy điểm chuẩn bao nhiêu?"
    )
    keyword_override: Optional[str] = Field(
        None,
        description="Optional custom keyword for Graph Search override"
    )
    score_threshold: Optional[float] = Field(
        default=None,
        description="Minimum similarity score for vector search (default from settings.VECTOR_SIMILARITY_THRESHOLD = 0.65)"
    )


class ChatResponse(BaseModel):
    query: str
    answer: str
    graph_context: str
    retrieved_count: int


@router.post(
    "/chat",
    response_model=ChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Execute Hybrid GraphRAG Chat Query",
    description="Extracts multi-hop subgraph context from Neo4j and generates AI answer using LLM."
)
async def chat_endpoint(request: ChatRequest):
    """
    POST /api/chat endpoint handler.

    Flow:
    1. Parse user query & extract search keyword.
    2. Traverse Neo4j Knowledge Graph via multi-hop Cypher.
    3. Format graph records into context text.
    4. Pass context and prompt to LLM.
    5. Return structured JSON response.
    """
    if not request.query.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Query string cannot be empty."
        )

    # Determine graph search keyword
    keyword = request.keyword_override or request.query.strip()

    # 1. Neo4j Multi-hop Traversal
    try:
        if retriever_instance is None:
            retriever = Neo4jRetriever()
        else:
            retriever = retriever_instance

        raw_records = retriever.retrieve_context(
            filters={"keyword": keyword},
            top_k=5,
            score_threshold=request.score_threshold
        )
        formatted_context = retriever.format_for_llm(raw_records)
    except RuntimeError as e:
        logger.error(f"Neo4j Retrieval Failure: {e}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Cơ sở dữ liệu Đồ thị Neo4j không khả dụng: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error during Neo4j traversal: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi hệ thống khi truy vấn đồ thị: {str(e)}"
        )

    # 2. LLM Answer Generation
    try:
        if llm_instance is None:
            llm = LLMService()
        else:
            llm = llm_instance

        answer = llm.generate_answer(query=request.query, graph_context=formatted_context)
    except RuntimeError as e:
        logger.error(f"LLM Generation Failure: {e}")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Lỗi khi gọi mô hình AI (LLM Timeout/Error): {str(e)}"
        )
    except Exception as e:
        logger.error(f"Unexpected error during LLM generation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Lỗi hệ thống khi sinh câu trả lời AI: {str(e)}"
        )

    return ChatResponse(
        query=request.query,
        answer=answer,
        graph_context=formatted_context,
        retrieved_count=len(raw_records)
    )

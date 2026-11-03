# -*- coding: utf-8 -*-
"""
llm/llm_client.py

Generator module for Hybrid GraphRAG Admissions Advisor system.
Uses 9router API Gateway (Claude Sonnet model) to generate grounded, fact-based answers
strictly based on retrieved Neo4j Graph Context.
Fully compliant with system architecture and coding guidelines.
"""

import json
import hashlib
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional
import requests

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger
from rag_api.llm.provider_errors import LLMServiceUnavailable
from rag_api.utils.provider_attempt_budget import provider_run_headers, record_provider_attempt
from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace

RAG_SYSTEM_PROMPT = """Bạn là trợ lý giúp học sinh lớp 9 tìm hiểu nhóm môn THPT và các hướng ngành có dữ liệu liên kết.

Quy tắc trả lời:
1. Trả lời thẳng câu hỏi bằng tiếng Việt, phù hợp với học sinh và phụ huynh.
2. Chỉ khẳng định dữ kiện có trong ngữ cảnh bằng chứng phía dưới. Không tự tạo trường, nhóm, mã môn,
   tổ hợp, offer, năm tuyển sinh hoặc điểm chuẩn.
3. Phân biệt môn yêu thích, điểm học tập, tự nhận xét năng lực và mức tự tin. Điểm số do học sinh cung cấp
   là thông tin tự khai, không phải điểm chuẩn hay căn cứ kết luận trúng tuyển.
4. Nhóm môn THPT và tổ hợp xét tuyển là hai dữ liệu khác nhau. Chỉ nêu hướng ngành khi ngữ cảnh có offer
   nối tổ hợp với trường/ngành. Yêu cầu năng khiếu phải được nêu riêng.
5. Ghi rõ năm học THPT và năm tuyển sinh theo đúng trường dữ liệu. Lịch sử tuyển sinh không bảo đảm kết quả tương lai.
6. Nếu thiếu bằng chứng, nói rõ phần nào chưa có dữ liệu và hỏi tối đa một thông tin giúp tiếp tục.

[NGỮ CẢNH BẰNG CHỨNG ĐÃ TRUY XUẤT]:
{context}
"""


class RAGGenerator:
    """Generator class handling LLM RAG text generation via 9router API Gateway."""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> None:
        """Initialize RAGGenerator using centralized configuration."""
        self.api_base: str = (api_base or settings.LLM_API_BASE).rstrip('/')
        self.api_key: Optional[str] = api_key or settings.LLM_API_KEY
        self.model: str = model or settings.LLM_MODEL
        self.timeout: float = timeout or settings.LLM_TIMEOUT

    def generate_answer(
        self,
        user_query: str,
        graph_context: str,
        context: Optional[List[Dict[str, str]]] = None,
        chat_history: Optional[List[Dict[str, str]]] = None,
        request_id: Optional[str] = None,
    ) -> str:
        """
        Generate grounded answer using 9router API Gateway based on query and graph context.

        Args:
            user_query (str): User input question.
            graph_context (str): Markdown formatted graph context string retrieved from Neo4j.
            context (Optional[List[Dict[str, str]]]): Conversation history context.
            chat_history (Optional[List[Dict[str, str]]]): Legacy alias for context.

        Returns:
            str: Generated text answer string from LLM.
        """
        if not user_query or not user_query.strip():
            return "Câu hỏi người dùng không được để trống."

        if not self.api_key or self.api_key.startswith("your_"):
            raise LLMServiceUnavailable("LLM provider is not configured")

        history: List[Dict[str, str]] = context or chat_history or []
        system_content = RAG_SYSTEM_PROMPT.format(
            context=graph_context.strip() if graph_context and graph_context.strip() else "Không có dữ liệu."
        )

        messages: List[Dict[str, str]] = [{"role": "system", "content": system_content}]

        # Inject conversation history turns
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role in ["user", "assistant"] and content:
                messages.append({"role": role, "content": content})

        # Append current user query
        messages.append({"role": "user", "content": user_query.strip()})

        endpoint = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False
        }

        started = time.perf_counter()
        try:
            trace = DiagnosticStageTrace.current()
            if trace is not None and trace.enabled:
                exact_context = graph_context.strip() if graph_context and graph_context.strip() else ""
                prompt_text = "".join(message["content"] for message in messages)
                trace.generation_input(
                    context_chars=len(exact_context),
                    context_utf8_bytes=len(exact_context.encode("utf-8")),
                    context_sha256=hashlib.sha256(exact_context.encode("utf-8")).hexdigest(),
                    prompt_chars=len(prompt_text),
                    prompt_utf8_bytes=len(prompt_text.encode("utf-8")),
                    message_count=len(messages),
                    history_turn_count=len(messages) - 2,
                    provider_timeout_seconds=self.timeout,
                )
            headers.update(provider_run_headers(endpoint=endpoint, stage="generation", request_id=request_id or ""))
            record_provider_attempt(stage="generation", request_id=request_id, endpoint=endpoint,
                                    request_headers=headers)
            response = requests.post(endpoint, headers=headers, json=payload, timeout=self.timeout,
                                     allow_redirects=False)
            status_code = _safe_status_code(response) or 200
            if isinstance(status_code, int) and not 200 <= status_code < 300:
                response.raise_for_status()
                raise requests.HTTPError(response=response)
            logger.info("provider_call request_id=%s stage=generation attempt=1 outcome=response http_status=%d duration_ms=%.1f",
                        _safe_request_id(request_id), status_code,
                        (time.perf_counter() - started) * 1000)
            answer_text = self._extract_text_from_response(response)
            if not answer_text or not answer_text.strip():
                raise LLMServiceUnavailable("LLM provider returned an empty response")
            return answer_text.strip()

        except requests.exceptions.Timeout:
            logger.info("provider_call request_id=%s stage=generation attempt=1 outcome=timeout http_status=none duration_ms=%.1f",
                        _safe_request_id(request_id), (time.perf_counter() - started) * 1000)
            logger.warning("9router timed out during answer generation")
            raise LLMServiceUnavailable("LLM provider timed out", failure_code="timeout")
        except requests.exceptions.RequestException as e:
            status_code = _safe_status_code(getattr(e, "response", None)) or _safe_status_code(
                locals().get("response"))
            logger.info("provider_call request_id=%s stage=generation attempt=1 outcome=provider_error http_status=%s duration_ms=%.1f",
                        _safe_request_id(request_id), status_code or "none",
                        (time.perf_counter() - started) * 1000)
            logger.warning("9router request failed during answer generation: %s", type(e).__name__)
            raise LLMServiceUnavailable("LLM provider request failed", upstream_status_code=status_code) from e
        except LLMServiceUnavailable:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as e:
            logger.warning("9router returned an unusable answer response: %s", type(e).__name__)
            raise LLMServiceUnavailable("LLM provider returned an unusable response") from e
    def _extract_text_from_response(self, response: requests.Response) -> str:
        """Robustly extract text response from either standard JSON or SSE stream response."""
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                res_data = response.json()
                if "choices" in res_data and len(res_data["choices"]) > 0:
                    choice = res_data["choices"][0]
                    if "message" in choice and "content" in choice["message"]:
                        return choice["message"]["content"] or ""
                    elif "delta" in choice and "content" in choice["delta"]:
                        return choice["delta"]["content"] or ""
            except Exception:
                pass

        # Fallback parsing for SSE event streams or raw text chunks
        text_chunks = []
        for line in response.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk_data = json.loads(data_str)
                    choices = chunk_data.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content") or choices[0].get("message", {}).get("content")
                        if content:
                            text_chunks.append(content)
                except json.JSONDecodeError:
                    continue

        if text_chunks:
            return "".join(text_chunks)

        res_data = response.json()
        return res_data["choices"][0]["message"]["content"]


def _safe_status_code(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None


def _safe_request_id(value: Optional[str]) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) else "none"

if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=== TESTING RAG GENERATOR MODULE WITH REAL 9ROUTER API ===")

    mock_graph_context = """### NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC (NEO4J KNOWLEDGE GRAPH)

#### 1. Ngành: Kỹ thuật phần mềm (Mã ngành: 7480103)
- **Trường Đại học:** Đại học Quy Nhơn (DQN)
- **Tổ hợp môn xét tuyển:**
  - Tổ hợp **A00** (Toán, Vật lý, Hóa học)
  - Tổ hợp **A01** (Toán, Vật lý, Tiếng Anh)
- **Lịch sử điểm chuẩn:**
  - Năm 2025 (Phương thức 100): Điểm THPT: **24.5**
"""

    mock_query = "Em được 24 điểm thì có đỗ ngành phần mềm ở Quy Nhơn không?"
    generator = RAGGenerator()
    answer = generator.generate_answer(user_query=mock_query, graph_context=mock_graph_context)
    print(f"\n[Generated Answer]:\n{answer}")

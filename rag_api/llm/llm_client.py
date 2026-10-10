# -*- coding: utf-8 -*-
"""
llm/llm_client.py

Generator module for Hybrid GraphRAG Admissions Advisor system.
Uses 9router API Gateway (Claude Sonnet model) to generate grounded, fact-based answers
strictly based on retrieved Neo4j Graph Context.
Fully compliant with system architecture and coding guidelines.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional
import requests

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger

RAG_SYSTEM_PROMPT = """Bạn là một Chuyên gia Kỹ sư Tư vấn Tuyển sinh Đại học AI am hiểu sâu sắc về thông tin tuyển sinh, ngành học và điểm chuẩn tại Việt Nam.

Nhiệm vụ của bạn:
1. Trả lời câu hỏi của người dùng một cách chính xác, rõ ràng, lịch sự và chân thành.
2. NGUYÊN TẮC TỐI THƯỢNG: Bạn TUYỆT ĐỐI CHỈ ĐƯỢC trả lời dựa vào thông tin có trong [NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC] được cung cấp bên dưới.
3. Nếu [NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC] bị trống hoặc KHÔNG có thông tin giải đáp câu hỏi của người dùng, bạn BẮT BUỘC phải thành thật xin lỗi và thông báo rằng hệ thống chưa có dữ liệu cho câu hỏi này. Tuyệt đối KHÔNG tự bịa đặt, suy đoán hoặc bịa ra điểm số/ngành học/tổ hợp môn (No Hallucination).

[NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC]:
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
        chat_history: Optional[List[Dict[str, str]]] = None
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

        # Fallback if no valid API key is set
        if not self.api_key or self.api_key.startswith("your_"):
            logger.warning("No valid LLM_API_KEY set for 9router. Running in Fallback Response mode.")
            return self._mock_fallback_answer(user_query, graph_context)

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

        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()
            answer_text = self._extract_text_from_response(response)
            return answer_text.strip()

        except requests.exceptions.Timeout:
            logger.error(f"9router API request timed out after {self.timeout} seconds.")
            return "Xin lỗi bạn, kết nối đến mô hình AI tạm thời bị quá thời gian chờ (Timeout). Vui lòng thử lại sau."
        except requests.exceptions.RequestException as e:
            logger.error(f"9router API HTTP Request Error in RAGGenerator: {e}")
            return self._mock_fallback_answer(user_query, graph_context)
        except (KeyError, IndexError, TypeError) as e:
            logger.error(f"Unexpected JSON structure response from 9router: {e}")
            return "Xin lỗi bạn, phản hồi từ mô hình AI bị lỗi định dạng."

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

    def _mock_fallback_answer(self, user_query: str, graph_context: str) -> str:
        """Safe fallback generator when 9router API endpoint is unavailable."""
        if not graph_context or "Không tìm thấy" in graph_context:
            return (
                "Xin lỗi bạn, hiện tại hệ thống chưa có dữ liệu về thông tin tuyển sinh này "
                "để tư vấn cho bạn. Vui lòng tra cứu thêm tại trang thông tin tuyển sinh chính thức của trường."
            )
        return (
            f"Dựa trên dữ liệu Đồ thị tri thức tuyển sinh:\n\n{graph_context}\n\n"
            f"Trả lời cho câu hỏi '{user_query}': Dữ liệu ngành học và điểm chuẩn đã được trích xuất ở trên. "
            f"Vui lòng đối chiếu điểm số của bạn với lịch sử điểm chuẩn để có lựa chọn phù hợp nhất."
        )


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

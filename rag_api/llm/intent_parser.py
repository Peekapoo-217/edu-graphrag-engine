# -*- coding: utf-8 -*-
"""
llm/intent_parser.py

Query Router & Intent Parser module for Admissions Advisory RAG system.
Uses 9router API (Claude Sonnet) to analyze conversation context and determine if query is ready for database retrieval.
Includes support for High School ('high_school') filter extractions and robust stream/json parsing.
"""

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
import requests

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger

INTENT_SYSTEM_PROMPT = """Bạn là một Chuyên gia Điều phối Thông tin (Query Router) cho Hệ thống RAG Tư vấn Tuyển sinh Đại học và Định hướng THPT.

Nhiệm vụ của bạn:
1. Đọc tin nhắn mới của người dùng cùng lịch sử trò chuyện (ngữ cảnh hội thoại).
2. Trích xuất thông tin điều kiện tìm kiếm (filters) bao gồm:
   - "keyword": Tên ngành học, chuyên ngành hoặc lĩnh vực sở thích (string hoặc null).
   - "university": Tên hoặc mã trường đại học (string hoặc null).
   - "high_school": Tên hoặc mã trường THPT mà người dùng đề cập (string hoặc null).
   - "score_max": Điểm số tối đa/điểm thi người dùng có (float hoặc null).
   - "combination": Khối thi / Tổ hợp môn xét tuyển (string hoặc null, ví dụ: A00, A01, D01).
3. Đánh giá trạng thái (status):
   - Nếu tìm thấy ÍT NHẤT 1 trong 3 thông tin: "keyword", "university", HOẶC "high_school", đặt "status": "ready" và đặt "reply_to_user": null.
   - Nếu THIẾU CẢ 3 thông tin trên, đặt "status": "incomplete" và viết một câu hỏi gợi mở thân thiện, lịch sự vào "reply_to_user" để hỏi người dùng về ngành học, trường đại học hoặc trường THPT quan tâm.

QUY TẮC BẮT BUỘC:
- LUÔN LUÔN trả về DUY NHẤT một chuỗi JSON hợp lệ.
- KHÔNG thêm bất kỳ lời giải thích, văn bản thừa hay thẻ markdown (như ```json) bên ngoài chuỗi JSON.

Cấu trúc JSON đầu ra bắt buộc:
{
  "status": "ready" hoặc "incomplete",
  "extracted_filters": {
     "keyword": "từ khóa ngành/sở thích (string hoặc null)",
     "university": "tên/mã trường đại học (string hoặc null)",
     "high_school": "tên/mã trường THPT (string hoặc null)",
     "score_max": "điểm số tối đa (float hoặc null)",
     "combination": "khối thi (string hoặc null)"
  },
  "reply_to_user": "câu hỏi gợi mở nếu incomplete (string hoặc null)"
}
"""


class IntentParser:
    """Class executing Intent Parsing & Query Routing using Claude Sonnet via 9router API."""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 30.0
    ):
        self.api_base = (api_base or settings.LLM_API_BASE).rstrip('/')
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.timeout = timeout or settings.LLM_TIMEOUT

    def parse_intent(
        self,
        user_message: str,
        context: Optional[List[Dict[str, str]]] = None,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Analyze user message and conversation context to parse intent and extracted filters.

        Args:
            user_message (str): New message from user.
            context (Optional[List[Dict[str, str]]]): Conversation history context.
            chat_history (Optional[List[Dict[str, str]]]): Legacy alias for context.

        Returns:
            Dict[str, Any]: Parsed JSON containing status, extracted_filters, and reply_to_user.
        """
        history = context or chat_history or []

        # Fallback handling if no valid API key is set
        if not self.api_key or self.api_key.startswith("your_"):
            logger.warning("No valid LLM_API_KEY set for 9router. Executing heuristic mock intent parser.")
            return self._heuristic_mock_parse(user_message, history)

        recommendation_contract = """
Bo sung cho luong chon mon THPT lop 9: neu nguoi dung muon chon/gioi thieu nhom 4 mon hoc cua mot truong THPT, them cac truong JSON:
"intent":"subject_group_recommendation"; "academic_year": nam hoc neu duoc neu hoac null;
"preferred_subjects": mang ten/ma cac mon hoc sinh yeu thich; "confidence_by_subject": object ten/ma mon -> diem tu tin nguyen (so nguyen 1..5).
Neu khong phai yeu cau nay, intent="general", preferred_subjects=[] va confidence_by_subject={}.
Khong tu suy dien diem tu tin. Neu thieu truong THPT, nam hoc, hoac ca hai tin hieu so thich/tu tin thi dat status="incomplete" va hoi bo sung.
Du lieu trong cac truong nay chi la y dinh de ung dung xac thuc; khong tao SQL/Cypher va khong tu tinh/xep hang ket qua.
"""
        messages = [{"role": "system", "content": INTENT_SYSTEM_PROMPT + "\n" + recommendation_contract}]
        for turn in history:
            role = turn.get("role", "user")
            content = turn.get("content", "")
            if role in ["user", "assistant"] and content:
                messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": user_message})

        endpoint = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
            "stream": False
        }

        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=self.timeout)
            response.raise_for_status()
            raw_content = self._extract_text_from_response(response)
            return self._clean_and_parse_json(raw_content)

        except requests.exceptions.RequestException as e:
            logger.error(f"9router API HTTP Request Error in IntentParser: {e}")
            return self._heuristic_mock_parse(user_message, history)
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            logger.error(f"Failed to parse Intent JSON response from 9router: {e}")
            return self._heuristic_mock_parse(user_message, history)

    def is_ready_for_db(self, parsed_json: Dict[str, Any]) -> bool:
        """
        Check if intent parsing result contains sufficient filters (keyword, university, or high_school) to query DB.

        Args:
            parsed_json (Dict[str, Any]): Dictionary returned by parse_intent.

        Returns:
            bool: True if status is 'ready' and at least 1 primary filter is present.
        """
        if parsed_json.get("status") != "ready":
            return False
        filters = parsed_json.get("extracted_filters", {})
        return bool(
            filters.get("keyword") or
            filters.get("university") or
            filters.get("high_school")
        )

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

    def _clean_and_parse_json(self, raw_text: str) -> Dict[str, Any]:
        """Parse and strictly normalize untrusted model output to the accepted intent contract."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict) and data.get("status") in {"ready", "incomplete"}:
                raw_filters = data.get("extracted_filters")
                raw_filters = raw_filters if isinstance(raw_filters, dict) else {}
                filters = {
                    key: raw_filters.get(key) if isinstance(raw_filters.get(key), (str, int, float)) else None
                    for key in ("keyword", "university", "high_school", "score_max", "combination")
                }
                raw_subjects = data.get("preferred_subjects", [])
                preferred_subjects = [item.strip()[:100] for item in raw_subjects[:9]
                                      if isinstance(item, str) and item.strip()] if isinstance(raw_subjects, list) else []
                raw_confidence = data.get("confidence_by_subject", {})
                confidence = {}
                if isinstance(raw_confidence, dict):
                    for subject, score in list(raw_confidence.items())[:9]:
                        if isinstance(subject, str) and subject.strip() and type(score) is int and 1 <= score <= 5:
                            confidence[subject.strip()[:100]] = score
                intent = data.get("intent")
                if intent not in {"general", "subject_group_recommendation"}:
                    intent = "general"
                year = data.get("academic_year")
                return {
                    "status": data["status"],
                    "extracted_filters": filters,
                    "intent": intent,
                    "academic_year": year.strip()[:30] if isinstance(year, str) and year.strip() else None,
                    "preferred_subjects": preferred_subjects,
                    "confidence_by_subject": confidence,
                    "reply_to_user": data.get("reply_to_user") if isinstance(data.get("reply_to_user"), str) else None,
                }
        except json.JSONDecodeError:
            pass
        raise json.JSONDecodeError("Invalid JSON structure", cleaned, 0)

    def _heuristic_mock_parse(self, user_message: str, history: List[Dict[str, str]]) -> Dict[str, Any]:
        """Heuristic rule-based fallback parser when LLM API is unavailable."""
        combined_text = " ".join([h.get("content", "") for h in history] + [user_message]).lower()

        recommendation_request = bool(re.search(
            r"(chọn.{0,24}(môn|nhóm)|gợi ý.{0,24}môn|định hướng.{0,24}môn|nhóm.{0,12}4 môn|nhóm môn|top\s*3.{0,20}nhóm|môn.{0,24}(lớp 10|thpt)|lớp 9.{0,30}(môn|nhóm))",
            combined_text))
        if recommendation_request:
            subject_aliases = {
                "Toán": ("toán",), "Ngữ văn": ("ngữ văn", "văn"),
                "Tiếng Anh": ("tiếng anh", "ngoại ngữ", "anh văn"),
                "Lịch sử": ("lịch sử", "sử"), "Địa lí": ("địa lí", "địa lý", "địa"),
                "Vật lí": ("vật lí", "vật lý", "môn lý", "môn lí"),
                "Hóa học": ("hóa học", "hóa"), "Sinh học": ("sinh học", "sinh"),
                "Tin học": ("tin học", "môn tin"), "Công nghệ": ("công nghệ",),
                "Giáo dục kinh tế và pháp luật": ("giáo dục kinh tế", "kinh tế và pháp luật", "gdktpl"),
            }
            preferred = [name for name, aliases in subject_aliases.items()
                         if any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", combined_text)
                                for alias in aliases)]
            academic_year_match = re.search(r"\b(20\d{2}\s*[-/]\s*20\d{2})\b", combined_text)
            academic_year = re.sub(r"\s*([-/])\s*", r"\1", academic_year_match.group(1)) if academic_year_match else None
            fallback_schools = ["tcv", "trần cao vân", "quốc học", "chu văn an", "lê hồng phong"]
            recommendation_school = next((school for school in fallback_schools if school in combined_text), None)
            complete = bool(recommendation_school and preferred)
            return {
                "status": "ready" if complete else "incomplete",
                "extracted_filters": {"keyword": None, "university": None, "high_school": recommendation_school,
                                      "score_max": None, "combination": None},
                "intent": "subject_group_recommendation",
                "academic_year": academic_year,
                "preferred_subjects": preferred,
                "confidence_by_subject": {},
                "reply_to_user": None if complete else "Em cho biết tên trường THPT và ít nhất một vài môn em yêu thích nhé."
            }

        keywords = ["lập trình", "cntt", "công nghệ thông tin", "luật", "kinh tế", "bác sĩ", "y khoa", "sư phạm", "kỹ thuật"]
        universities = ["quy nhơn", "qnu", "bách khoa", "hust", "y dược", "khtn"]
        high_schools = ["tcv", "trần cao vân", "quốc học", "chu văn an", "lê hồng phong"]

        found_kw = next((kw for kw in keywords if kw in combined_text), None)
        found_univ = next((u for u in universities if u in combined_text), None)
        found_hs = next((hs for hs in high_schools if hs in combined_text), None)

        if found_kw or found_univ or found_hs:
            return {
                "status": "ready",
                "extracted_filters": {
                    "keyword": found_kw,
                    "university": found_univ,
                    "high_school": found_hs,
                    "score_max": None,
                    "combination": None
                },
                "reply_to_user": None
            }
        else:
            return {
                "status": "incomplete",
                "extracted_filters": {
                    "keyword": None,
                    "university": None,
                    "high_school": None,
                    "score_max": None,
                    "combination": None
                },
                "reply_to_user": "Chào bạn! Bạn đang quan tâm đến ngành học/lĩnh vực nào, trường đại học nào hoặc trường THPT nào để mình hỗ trợ tra cứu chi tiết nhé?"
            }


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = IntentParser()
    conv_context: List[Dict[str, str]] = []

    print("=== TESTING INTENT PARSER WITH REAL 9ROUTER API ===")
    test_msg = "Em học trường Trần Cao Vân muốn xét tuyển ngành Công nghệ thông tin Quy Nhơn"
    parsed = parser.parse_intent(test_msg, context=conv_context)
    print(f"[Parsed JSON]:\n{json.dumps(parsed, ensure_ascii=False, indent=2)}")
    print(f"[Is Ready for DB?]: {parser.is_ready_for_db(parsed)}")

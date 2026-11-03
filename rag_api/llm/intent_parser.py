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
import time
import unicodedata
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

CHAT_ROUTING_PROMPT = """Bạn điều phối chatbot cho học sinh lớp 9 tìm hiểu nhóm môn THPT và hướng ngành.
Chọn đúng một intent trong: greeting, unsupported, knowledge, school_group_lookup,
combination_lookup, pathway_lookup, subject_group_advice, subject_group_comparison.

Quy tắc nghiệp vụ:
- Chỉ trích xuất điều người dùng nói rõ; mọi bộ lọc và năm học phải khớp một cụm trong tin nhắn user.
  Tên môn trong signal phải tương ứng với môn được nêu trong evidence. Không suy ra môn yêu thích từ điểm, điểm từ sở thích,
  hay mức tự tin từ điểm. Không biến điểm từng môn thành score_max.
- Mỗi subject preference, disliked subject, confidence và grade phải có evidence là một đoạn
  trích nguyên văn từ tin nhắn user. Không lấy lời assistant làm dữ kiện về học sinh.
- Với điểm, chỉ lấy scale nếu người dùng nói rõ; không mặc định thang điểm. Không quy đổi.
- Phân biệt academic_year của trường THPT với admission_year tuyển sinh.
- Nếu người dùng chỉ định mã nhóm cần xem/so sánh, trích xuất đúng mã đó và evidence nguyên văn.
- Khi câu hỏi so sánh các trường THPT, trích xuất tất cả tên trường được nêu vào high_schools; không gộp hai trường thành một nhãn.
- Khi so sánh mã nhóm khác nhau ở hai trường, trích xuất requested_school_group_pairs theo từng cụm nguyên văn
  như "G04 của THPT A". Chỉ ghép trường với mã nhóm nếu câu hiện tại chỉ rõ cặp đó.
- Với câu hỏi phân loại môn trong danh mục, ghi môn được hỏi vào knowledge_subjects; không mở rộng sang toàn bộ danh mục.
- Câu hỏi về nhóm môn, tổ hợp, ngành/trường đại học và mối liên hệ tuyển sinh là tra cứu dữ liệu, không phải câu hỏi kiến thức chung.
- Không khẳng định điều kiện đỗ. Không tạo SQL, Cypher, mã trường hoặc mã môn.
- Nếu thiếu trường cho việc so sánh nhóm cụ thể, hãy hỏi tên trường. Người dùng vẫn có thể
  khám phá sở thích trước khi chọn trường.

Chỉ trả về JSON theo cấu trúc:
{
  "intent": "một giá trị trong danh sách trên",
  "extracted_filters": {
    "high_school": null, "university": null, "desired_program": null,
    "combination_code": null, "keyword": null,
    "admission_year": null, "method_code": null, "score_max": null
  },
  "high_schools": [],
  "academic_year": null,
  "preferred_subjects": [{"subject": "", "evidence": ""}],
  "disliked_subjects": [{"subject": "", "evidence": ""}],
  "self_reported_strengths": [{"subject": "", "description": "", "evidence": ""}],
  "confidence_by_subject": [{"subject": "", "value": 1, "evidence": ""}],
  "grades_by_subject": [{"subject": "", "value": 0, "scale": null,
    "assessment_type": null, "evidence": ""}],
  "requested_group_codes": [{"code": "", "evidence": ""}],
  "requested_school_group_pairs": [{"school": "", "code": "", "evidence": ""}],
  "requested_pathway_pairs": [{"high_school": "", "academic_year": "",
    "subject_group_code": "", "program_name": "", "evidence": ""}],
  "knowledge_subjects": [{"subject": "", "evidence": ""}],
  "reply_to_user": null
}

For a question comparing multiple school-group/program pairs, put each independently scoped pair in requested_pathway_pairs. Keep one pair per object; do not combine group codes or program names across objects. Each evidence value must be a contiguous quote from user messages that contains that pair's group and program. Never infer a school, academic year, or program that is absent from user messages.
"""


class IntentParser:
    """Class executing Intent Parsing & Query Routing using Claude Sonnet via 9router API."""

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None
    ):
        self.api_base = (api_base or settings.LLM_API_BASE).rstrip('/')
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.timeout = settings.INTENT_PARSER_TIMEOUT if timeout is None else timeout

    def parse_intent(
        self,
        user_message: str,
        context: Optional[List[Dict[str, str]]] = None,
        chat_history: Optional[List[Dict[str, str]]] = None,
        request_id: Optional[str] = None,
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

        if not self.api_key or self.api_key.startswith("your_"):
            raise LLMServiceUnavailable("LLM provider is not configured")

        messages = [{"role": "system", "content": CHAT_ROUTING_PROMPT}]
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

        started = time.perf_counter()
        trace = DiagnosticStageTrace.current()
        if trace is not None and trace.enabled:
            serialized_body = requests.models.complexjson.dumps(payload, allow_nan=False).encode("utf-8")
            trace.intent_input(
                message_count=len(messages), history_turn_count=len(history),
                prompt_utf8_bytes=sum(len(str(message["content"]).encode("utf-8")) for message in messages),
                request_body_utf8_bytes=len(serialized_body),
                configured_timeout_seconds=float(self.timeout),
            )
        upstream_status = None
        try:
            headers.update(provider_run_headers(endpoint=endpoint, stage="intent", request_id=request_id or ""))
            record_provider_attempt(stage="intent", request_id=request_id, endpoint=endpoint,
                                    request_headers=headers)
            response = requests.post(endpoint, headers=headers, json=payload, timeout=self.timeout,
                                     allow_redirects=False)
            upstream_status = _safe_status_code(response) or 200
            if isinstance(upstream_status, int) and not 200 <= upstream_status < 300:
                response.raise_for_status()
                raise requests.HTTPError(response=response)
            logger.info("provider_call request_id=%s stage=intent attempt=1 outcome=response http_status=%d duration_ms=%.1f",
                        _safe_request_id(request_id), upstream_status,
                        (time.perf_counter() - started) * 1000)
            if trace is not None and trace.enabled:
                trace.intent_terminal(outcome="response", duration_ms=(time.perf_counter() - started) * 1000)
            raw_content = self._extract_text_from_response(response)
            user_texts = [turn.get("content", "") for turn in history
                          if turn.get("role", "user") == "user" and turn.get("content")]
            user_texts.append(user_message)
            return self._clean_and_parse_json(raw_content, user_texts)

        except requests.exceptions.Timeout as e:
            failure_code = (
                "connect_timeout" if isinstance(e, requests.exceptions.ConnectTimeout)
                else "read_timeout" if isinstance(e, requests.exceptions.ReadTimeout)
                else "timeout"
            )
            logger.info("provider_call request_id=%s stage=intent attempt=1 outcome=%s http_status=none "
                        "transport_error_type=%s duration_ms=%.1f",
                        _safe_request_id(request_id), failure_code, type(e).__name__,
                        (time.perf_counter() - started) * 1000)
            if trace is not None and trace.enabled:
                trace.intent_terminal(outcome="timeout", duration_ms=(time.perf_counter() - started) * 1000,
                                      transport_error_code=failure_code)
            logger.warning("9router timed out during intent parsing transport_kind=%s", failure_code)
            raise LLMServiceUnavailable("LLM provider timed out", failure_code=failure_code) from e
        except requests.exceptions.RequestException as e:
            status_code = _safe_status_code(getattr(e, "response", None)) or _safe_status_code(
                locals().get("response"))
            logger.info("provider_call request_id=%s stage=intent attempt=1 outcome=provider_error http_status=%s duration_ms=%.1f",
                        _safe_request_id(request_id), status_code or "none",
                        (time.perf_counter() - started) * 1000)
            if trace is not None and trace.enabled:
                trace.intent_terminal(outcome="provider_error",
                                      duration_ms=(time.perf_counter() - started) * 1000,
                                      transport_error_code="provider_failure")
            logger.error("9router request failed during intent parsing: %s", type(e).__name__)
            raise LLMServiceUnavailable("LLM provider request failed", upstream_status_code=status_code) from e
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as e:
            logger.error("9router returned an unusable intent response: %s", type(e).__name__)
            raise LLMServiceUnavailable("LLM provider returned an unusable response") from e
    def is_ready_for_db(self, parsed_json: Dict[str, Any]) -> bool:
        """
        Check if intent parsing result contains sufficient filters (keyword, university, or high_school) to query DB.

        Args:
            parsed_json (Dict[str, Any]): Dictionary returned by parse_intent.

        Returns:
            bool: True if status is 'ready' and at least 1 primary filter is present.
        """
        filters = parsed_json.get("extracted_filters", {})
        intent = parsed_json.get("intent", "general")
        if intent in {"school_group_lookup", "subject_group_comparison"}:
            return bool(filters.get("high_school"))
        if intent == "subject_group_advice":
            return bool(filters.get("high_school") or parsed_json.get("preferred_subjects")
                        or parsed_json.get("disliked_subjects")
                        or parsed_json.get("self_reported_strengths")
                        or parsed_json.get("confidence_by_subject")
                        or parsed_json.get("grades_by_subject")
                        or parsed_json.get("grades_needing_scale"))
        if intent == "combination_lookup":
            return bool(filters.get("combination_code"))
        if intent == "pathway_lookup":
            return bool(filters.get("desired_program") or filters.get("keyword")
                        or filters.get("university") or filters.get("combination_code"))
        if intent in {"greeting", "unsupported", "knowledge"}:
            return False
        return bool(filters.get("keyword") or filters.get("university") or filters.get("high_school"))

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

    def _clean_and_parse_json(
        self, raw_text: str, allowed_user_texts: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Parse and strictly normalize untrusted model output to the accepted intent contract."""
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                source_user_texts = allowed_user_texts or []
                raw_filters = data.get("extracted_filters")
                raw_filters = raw_filters if isinstance(raw_filters, dict) else {}
                filters = {
                    key: raw_filters.get(key) if isinstance(raw_filters.get(key), (str, int, float)) else None
                    for key in ("keyword", "university", "high_school", "desired_program",
                                "combination_code", "admission_year", "method_code", "score_max")
                }
                raw_high_schools = data.get("high_schools")
                high_schools = []
                if isinstance(raw_high_schools, list):
                    for label in raw_high_schools[:4]:
                        if (isinstance(label, str) and label.strip()
                                and self._is_user_phrase(label, source_user_texts)):
                            normalized_label = self._normalize_evidence_text(label)
                            if normalized_label and all(
                                self._normalize_evidence_text(existing) != normalized_label
                                for existing in high_schools
                            ):
                                high_schools.append(label.strip()[:160])
                if not high_schools and filters.get("high_school"):
                    high_schools = [str(filters["high_school"])[:160]]
                filters = {
                    key: value if value is None or self._is_user_phrase(str(value), source_user_texts) else None
                    for key, value in filters.items()
                }
                if filters["admission_year"] is not None:
                    year = str(filters["admission_year"]).strip()
                    filters["admission_year"] = year if re.fullmatch(r"\d{4}", year) else None
                if filters["method_code"] is not None:
                    method_code = str(filters["method_code"]).strip()
                    filters["method_code"] = method_code if re.fullmatch(r"[A-Za-z0-9_-]{1,20}", method_code) else None
                if filters["keyword"] is not None and re.fullmatch(r"\d{4}", str(filters["keyword"]).strip()):
                    filters["keyword"] = None
                if filters["score_max"] is not None:
                    try:
                        score_max = float(filters["score_max"])
                    except (TypeError, ValueError):
                        score_max = None
                    filters["score_max"] = score_max if score_max is not None and 0 <= score_max <= 100 else None
                intent = data.get("intent")
                if intent == "subject_group_recommendation":
                    intent = "subject_group_advice"
                allowed_intents = {
                    "greeting", "unsupported", "knowledge", "school_group_lookup",
                    "combination_lookup", "pathway_lookup", "subject_group_advice",
                    "subject_group_comparison", "general",
                }
                if intent not in allowed_intents:
                    raise ValueError("Unknown intent")
                year = data.get("academic_year")
                year = year if isinstance(year, str) and self._is_user_phrase(year, source_user_texts) else None
                pathway_pairs = self._verified_pathway_pairs(
                    data.get("requested_pathway_pairs"), source_user_texts)
                if len(pathway_pairs) < 2:
                    recovered_pairs = self._recover_numbered_pathway_pairs(
                        filters=filters,
                        high_schools=high_schools,
                        academic_year=year,
                        user_texts=source_user_texts,
                    )
                    verified_recovered_pairs = self._verified_pathway_pairs(
                        recovered_pairs, source_user_texts[-1:] if source_user_texts else [])
                    if len(verified_recovered_pairs) >= 2:
                        pathway_pairs = verified_recovered_pairs
                return {
                    "extracted_filters": filters,
                    "high_schools": high_schools,
                    "intent": intent,
                    "academic_year": year.strip()[:30] if year and year.strip() else None,
                    "preferred_subjects": self._verified_subject_signals(
                        data.get("preferred_subjects"), source_user_texts),
                    "disliked_subjects": self._verified_subject_signals(
                        data.get("disliked_subjects"), source_user_texts),
                    "self_reported_strengths": self._verified_descriptions(
                        data.get("self_reported_strengths"), source_user_texts),
                    "confidence_by_subject": self._verified_numeric_signals(
                        data.get("confidence_by_subject"), source_user_texts, 1, 5,
                        integer_only=True, numeric_kind="confidence"),
                    "grades_by_subject": self._verified_grades(
                        data.get("grades_by_subject"), source_user_texts),
                    "grades_needing_scale": self._grades_needing_scale(
                        data.get("grades_by_subject"), source_user_texts),
                    "requested_group_codes": self._verified_group_codes(
                        data.get("requested_group_codes"), source_user_texts),
                    "requested_school_group_pairs": self._verified_school_group_pairs(
                        data.get("requested_school_group_pairs"), source_user_texts),
                    "requested_pathway_pairs": pathway_pairs,
                    "knowledge_subjects": self._verified_subject_signals(
                        data.get("knowledge_subjects"), source_user_texts),
                    "reply_to_user": data.get("reply_to_user") if isinstance(data.get("reply_to_user"), str) else None,
                }
        except json.JSONDecodeError:
            pass
        raise json.JSONDecodeError("Invalid JSON structure", cleaned, 0)

    @staticmethod
    def _evidence_is_from_user(evidence: Any, allowed_user_texts: Optional[List[str]]) -> bool:
        return (
            isinstance(evidence, str) and len(evidence.strip()) >= 3
            and any(evidence.strip().casefold() in text.casefold() for text in (allowed_user_texts or []))
        )

    @staticmethod
    def _normalize_evidence_text(value: str) -> str:
        decomposed = unicodedata.normalize("NFD", value.casefold().replace("đ", "d"))
        without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", without_marks)).strip()

    @classmethod
    def _is_user_phrase(cls, phrase: str, allowed_user_texts: Optional[List[str]]) -> bool:
        candidate = cls._normalize_evidence_text(phrase)
        return bool(candidate) and any(
            candidate in cls._normalize_evidence_text(source)
            for source in (allowed_user_texts or []) if isinstance(source, str)
        )

    @classmethod
    def _subject_is_in_evidence(cls, subject: Any, evidence: Any) -> bool:
        if not isinstance(subject, str) or not isinstance(evidence, str):
            return False
        normalized_subject = cls._normalize_evidence_text(subject)
        normalized_evidence = cls._normalize_evidence_text(evidence)
        if not normalized_subject:
            return False
        aliases = {
            "ngu van": ("ngu van", "van"),
            "tieng anh": ("tieng anh", "anh van", "ngoai ngu", "anh"),
            "lich su": ("lich su", "su"),
            "dia li": ("dia li", "dia ly", "dia"),
            "vat li": ("vat li", "vat ly", "li", "ly"),
            "hoa hoc": ("hoa hoc", "hoa"),
            "sinh hoc": ("sinh hoc", "sinh"),
            "tin hoc": ("tin hoc", "tin"),
        }
        terms = aliases.get(normalized_subject, (normalized_subject,))
        return any(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", normalized_evidence)
                   for term in terms)

    @classmethod
    def _verified_subject_signals(cls, raw: Any, user_texts: Optional[List[str]]) -> List[Dict[str, str]]:
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:9]:
            if not isinstance(item, dict):
                continue
            subject, evidence = item.get("subject"), item.get("evidence")
            if (isinstance(subject, str) and subject.strip()
                    and cls._evidence_is_from_user(evidence, user_texts)
                    and cls._subject_is_in_evidence(subject, evidence)):
                result.append({"subject": subject.strip()[:100], "evidence": evidence.strip()[:240]})
        return result

    @classmethod
    def _verified_group_codes(cls, raw: Any, user_texts: Optional[List[str]]) -> List[Dict[str, str]]:
        """Keep only short group identifiers explicitly present in user evidence."""
        if not isinstance(raw, list):
            return []
        result = []
        seen = set()
        for item in raw[:12]:
            if not isinstance(item, dict):
                continue
            code, evidence = item.get("code"), item.get("evidence")
            if not (isinstance(code, str) and isinstance(evidence, str)
                    and re.fullmatch(r"[A-Za-z0-9_-]{1,20}", code.strip())
                    and cls._evidence_is_from_user(evidence, user_texts)
                    and cls._is_user_phrase(code.strip(), [evidence])):
                continue
            normalized = code.strip().casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            result.append({"code": code.strip().upper(), "evidence": evidence.strip()[:240]})
        return result

    @classmethod
    def _verified_school_group_pairs(
        cls, raw: Any, user_texts: Optional[List[str]]
    ) -> List[Dict[str, str]]:
        """Accept only a school and group code joined in a quoted user clause."""
        if not isinstance(raw, list):
            return []
        result = []
        seen = set()
        for item in raw[:4]:
            if not isinstance(item, dict):
                continue
            school, code, evidence = item.get("school"), item.get("code"), item.get("evidence")
            if not (isinstance(school, str) and school.strip()
                    and isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,20}", code.strip())
                    and cls._evidence_is_from_user(evidence, user_texts)):
                continue
            normalized = cls._normalize_evidence_text(evidence)
            normalized_school = cls._normalize_evidence_text(school)
            normalized_code = cls._normalize_evidence_text(code)
            paired = (
                re.search(rf"\b{re.escape(normalized_code)}\s+cua\s+(?:truong\s+)?{re.escape(normalized_school)}\b", normalized)
                or re.search(rf"\b{re.escape(normalized_school)}\s+(?:nhom\s+)?{re.escape(normalized_code)}\b", normalized)
            )
            if not paired:
                continue
            key = (normalized_school, normalized_code)
            if key not in seen:
                seen.add(key)
                result.append({"school": school.strip()[:160], "code": code.strip().upper(),
                               "evidence": evidence.strip()[:240]})
        return result

    @classmethod
    def _recover_numbered_pathway_pairs(
        cls,
        *,
        filters: Dict[str, Any],
        high_schools: List[str],
        academic_year: Optional[str],
        user_texts: Optional[List[str]],
    ) -> List[Dict[str, str]]:
        """Recover fully explicit numbered group/program pairs when structured output omits them."""
        current_text = next((text for text in reversed(user_texts or [])
                             if isinstance(text, str) and text.strip()), "")
        if not current_text:
            return []

        university = filters.get("university") if isinstance(filters, dict) else None
        if not (isinstance(university, str) and cls._is_user_phrase(university, [current_text])):
            return []

        school_candidates = []
        raw_school = filters.get("high_school") if isinstance(filters, dict) else None
        for value in [raw_school, *(high_schools if isinstance(high_schools, list) else [])]:
            if (isinstance(value, str) and value.strip()
                    and cls._is_user_phrase(value, [current_text])):
                normalized = cls._normalize_evidence_text(value)
                if normalized and all(cls._normalize_evidence_text(existing) != normalized
                                      for existing in school_candidates):
                    school_candidates.append(value.strip())
        if len(school_candidates) != 1:
            return []

        explicit_years = list(dict.fromkeys(re.findall(r"(?<!\d)(20\d{2})(?!\d)", current_text)))
        if len(explicit_years) != 1:
            return []
        resolved_year = explicit_years[0]
        if academic_year and str(academic_year) != resolved_year:
            return []

        markers = list(re.finditer(r"\(\s*(\d{1,2})\s*\)", current_text))
        marker_numbers = [int(match.group(1)) for match in markers]
        if len(markers) < 2 or len(markers) > 4 or marker_numbers != list(range(1, len(markers) + 1)):
            return []

        pairs = []
        group_pattern = re.compile(
            r"\bnh[oó]m(?:\s+m[oô]n)?\s*(?:s[oố]\s*)?0*(\d{1,3})\b"
            r"|\bg\s*0*(\d{1,3})\b",
            flags=re.IGNORECASE,
        )
        program_pattern = re.compile(
            r"\b(?:ngành|program|major)\s+(.+?)(?=\s+tại\s+|[,;.!?]|$)",
            flags=re.IGNORECASE,
        )
        for index, marker in enumerate(markers):
            end = markers[index + 1].start() if index + 1 < len(markers) else len(current_text)
            clause = current_text[marker.end():end].strip()
            group_numbers = {
                str(int(match.group(1) or match.group(2)))
                for match in group_pattern.finditer(clause)
            }
            program_names = [match.group(1).strip() for match in program_pattern.finditer(clause)
                             if match.group(1).strip()]
            if len(group_numbers) != 1 or len(program_names) != 1:
                return []
            pairs.append({
                "high_school": school_candidates[0],
                "academic_year": resolved_year,
                # Keep the user-specified ordinal; the existing school/year catalog
                # resolver must map it to one canonical group code before retrieval.
                "subject_group_code": next(iter(group_numbers)),
                "program_name": program_names[0],
                "evidence": clause,
            })
        return pairs

    @classmethod
    def _verified_pathway_pairs(
        cls, raw: Any, user_texts: Optional[List[str]]
    ) -> List[Dict[str, str]]:
        """Keep independently scoped school/group/program pairs only with user evidence."""
        if not isinstance(raw, list):
            return []
        result = []
        seen = set()
        for item in raw[:4]:
            if not isinstance(item, dict):
                continue
            school = item.get("high_school")
            year = item.get("academic_year")
            group_code = item.get("subject_group_code")
            program = item.get("program_name")
            evidence = item.get("evidence")
            if not all(isinstance(value, str) and value.strip()
                       for value in (school, year, group_code, program)):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", group_code.strip()):
                continue
            if not re.fullmatch(r"\d{4}", year.strip()):
                continue
            if not cls._evidence_is_from_user(evidence, user_texts):
                continue
            source_text = " ".join(user_texts or [])
            normalized_evidence = cls._normalize_evidence_text(evidence)
            normalized_source = cls._normalize_evidence_text(source_text)
            normalized_school = cls._normalize_evidence_text(school)
            normalized_program = cls._normalize_evidence_text(program)
            if normalized_school not in normalized_source or normalized_program not in normalized_evidence:
                continue
            if not re.search(rf"\b{re.escape(year.strip())}\b", source_text):
                continue
            code_number = re.sub(r"\D", "", group_code)
            group_numbers = re.findall(r"(?:nhom(?:\s+mon)?|group)\s*(?:so\s*)?g?\s*0*(\d{1,3})",
                                       normalized_evidence)
            if not code_number or str(int(code_number)) not in {str(int(number)) for number in group_numbers}:
                continue
            key = (normalized_school, year.strip(), group_code.strip().casefold(), normalized_program)
            if key in seen:
                continue
            seen.add(key)
            result.append({
                "high_school": school.strip()[:160], "academic_year": year.strip(),
                "subject_group_code": group_code.strip().upper(),
                "program_name": program.strip()[:160], "evidence": evidence.strip()[:320],
            })
        return result

    @classmethod
    def _verified_descriptions(cls, raw: Any, user_texts: Optional[List[str]]) -> List[Dict[str, str]]:
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:9]:
            if not isinstance(item, dict):
                continue
            subject, description, evidence = item.get("subject"), item.get("description"), item.get("evidence")
            if (isinstance(subject, str) and subject.strip() and isinstance(description, str)
                    and description.strip() and cls._evidence_is_from_user(evidence, user_texts)
                    and cls._subject_is_in_evidence(subject, evidence)):
                result.append({"subject": subject.strip()[:100], "description": description.strip()[:120],
                               "evidence": evidence.strip()[:240]})
        return result

    @classmethod
    def _verified_numeric_signals(
        cls, raw: Any, user_texts: Optional[List[str]], minimum: int, maximum: int,
        integer_only: bool = False, numeric_kind: str = "numeric",
    ) -> List[Dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:9]:
            if not isinstance(item, dict):
                continue
            subject, value, evidence = item.get("subject"), item.get("value"), item.get("evidence")
            valid_number = type(value) is int if integer_only else type(value) in (int, float)
            if (isinstance(subject, str) and subject.strip() and valid_number
                    and minimum <= value <= maximum and cls._evidence_is_from_user(evidence, user_texts)
                    and cls._subject_is_in_evidence(subject, evidence)
                    and cls._numeric_value_is_evidenced(value, evidence, numeric_kind=numeric_kind)):
                result.append({"subject": subject.strip()[:100], "value": value, "evidence": evidence.strip()[:240]})
        return result

    @classmethod
    def _verified_grades(cls, raw: Any, user_texts: Optional[List[str]]) -> List[Dict[str, Any]]:
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:9]:
            if not isinstance(item, dict):
                continue
            subject, value, scale, evidence = (
                item.get("subject"), item.get("value"), item.get("scale"), item.get("evidence")
            )
            if not (isinstance(subject, str) and subject.strip()
                    and type(value) in (int, float) and type(scale) in (int, float)
                    and 0 <= value <= scale and scale > 0
                    and cls._evidence_is_from_user(evidence, user_texts)
                    and cls._subject_is_in_evidence(subject, evidence)
                    and cls._scale_is_explicit(scale, evidence)
                    and cls._grade_value_is_evidenced(value, scale, evidence)):
                continue
            assessment_type = item.get("assessment_type")
            result.append({
                "subject": subject.strip()[:100], "value": value, "scale": scale,
                "assessment_type": assessment_type[:100] if isinstance(assessment_type, str) else None,
                "evidence": evidence.strip()[:240],
            })
        return result

    @classmethod
    def _grades_needing_scale(cls, raw: Any, user_texts: Optional[List[str]]) -> List[Dict[str, str]]:
        if not isinstance(raw, list):
            return []
        result = []
        for item in raw[:9]:
            if not isinstance(item, dict):
                continue
            subject, value, evidence = item.get("subject"), item.get("value"), item.get("evidence")
            scale = item.get("scale")
            if (isinstance(subject, str) and subject.strip() and type(value) in (int, float)
                    and 0 <= value <= 100
                    and (type(scale) not in (int, float) or scale <= 0
                         or not cls._scale_is_explicit(scale, evidence))
                    and cls._evidence_is_from_user(evidence, user_texts)
                    and cls._subject_is_in_evidence(subject, evidence)
                    and cls._numeric_value_is_evidenced(value, evidence, numeric_kind="grade")):
                result.append({"subject": subject.strip()[:100], "evidence": evidence.strip()[:240]})
        return result

    @staticmethod
    def _scale_is_explicit(scale: Any, evidence: Any) -> bool:
        if type(scale) not in (int, float) or not isinstance(evidence, str):
            return False
        scale_text = ("%g" % scale)
        escaped = re.escape(scale_text)
        patterns = (
            rf"\bthang(?:\s*diem)?\s*{escaped}\b",
            rf"/\s*{escaped}\b",
            rf"\b{escaped}\s*diem\b",
            rf"\bdiem(?:\s*so)?\s*{escaped}\b",
            rf"\b{escaped}\s*points?\b",
        )
        return any(re.search(pattern, evidence.casefold()) for pattern in patterns)

    @classmethod
    def _grade_value_is_evidenced(cls, value: Any, scale: Any, evidence: Any) -> bool:
        if not isinstance(evidence, str):
            return False
        value_text = cls._format_number(value)
        scale_text = cls._format_number(scale)
        normalized = cls._normalize_numeric_evidence_text(evidence)
        # A supplied scale is accepted only when the exact value/scale pair is in
        # the user's quoted evidence, preventing a nearby grade from being bound
        # to the wrong subject or assessment.
        pair_pattern = rf"(?<!\d){re.escape(value_text)}\s*/\s*{re.escape(scale_text)}(?!\d)"
        if re.search(pair_pattern, normalized):
            return True
        return cls._numeric_value_is_evidenced(value, evidence, numeric_kind="grade")

    @classmethod
    def _numeric_value_is_evidenced(cls, value: Any, evidence: Any, numeric_kind: str) -> bool:
        if not isinstance(evidence, str):
            return False
        value_text = cls._format_number(value)
        normalized = cls._normalize_numeric_evidence_text(evidence)
        number = rf"(?<!\d){re.escape(value_text)}(?!\d)"
        if numeric_kind == "confidence":
            return bool(re.search(rf"{number}\s*/\s*5(?!\d)", normalized)
                        or re.search(rf"(?:tu tin|muc tu tin|muc do tu tin)\D{{0,12}}{number}", normalized))
        # For grades, require a score cue next to the number. This excludes a
        # class/year number elsewhere in the same evidence quote.
        return bool(
            re.search(rf"{number}\s*(?:diem|/\s*\d+)", normalized)
            or re.search(rf"(?:diem|duoc|dat|trung binh)\D{{0,12}}{number}", normalized)
            or re.search(rf"{number}\D{{0,12}}(?:diem|duoc|dat|trung binh)", normalized)
        )

    @staticmethod
    def _format_number(value: Any) -> str:
        if type(value) in (int, float):
            return ("%g" % value)
        return str(value).strip()

    @staticmethod
    def _normalize_numeric_evidence_text(evidence: str) -> str:
        decomposed = unicodedata.normalize("NFD", evidence.casefold().replace("đ", "d"))
        without_marks = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
        return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9/.,]+", " ", without_marks)).strip().replace(",", ".")


def _safe_status_code(response: Any) -> int | None:
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None


def _safe_request_id(value: Optional[str]) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value) else "none"

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

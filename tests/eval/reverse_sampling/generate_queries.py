# -*- coding: utf-8 -*-
"""
tests/eval/reverse_sampling/generate_queries.py

LLM-enhanced query generator for reverse sampling evaluation.
Uses 9router API (via existing RAGGenerator) to generate natural Vietnamese queries
from extracted Neo4j paths. Falls back to template-based generation if LLM unavailable.
"""

import json
import os
import sys
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, asdict

# Ensure data-scrawler root directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger
from rag_api.llm.llm_client import RAGGenerator
from .extract_paths import ExtractedPath


@dataclass
class GoldenQuery:
    """A single golden query record for evaluation."""
    query_id: str
    query: str
    category: str
    filters: Dict[str, Any]
    expected_programs: List[str]
    expected_combinations: List[str]
    expected_answer: str
    source_path: Dict[str, Any]


# Template-based fallback generators
TEMPLATE_GENERATORS = {
    "factual": [
        lambda p: f"Điểm chuẩn ngành {p.prog_name} trường {p.univ_name} năm {p.cutoffs[0]['year'] if p.cutoffs else 2025} là bao nhiêu?",
        lambda p: f"Tổ hợp xét tuyển ngành {p.prog_name} {p.univ_name} có những tổ hợp nào?",
        lambda p: f"Thông tin ngành {p.prog_name} mã {p.prog_code} tại {p.univ_name} năm {p.cutoffs[0]['year'] if p.cutoffs else 2025}",
    ],
    "multi_hop": [
        lambda p: f"Em học trường {p.hs_name} muốn xét tuyển ngành {p.prog_name} {p.univ_name} thì chọn nhóm môn nào?",
        lambda p: f"Học sinh THPT {p.hs_name} khối {p.combo_id.split('_')[0] if '_' in p.combo_id else p.combo_id[:3]} được xét tuyển ngành gì ở {p.univ_name}?",
        lambda p: f"Trường {p.hs_name} có nhóm môn {p.sg_name} ({p.sg_code}) phù hợp cho ngành {p.prog_name} {p.univ_name} không?",
    ],
    "rule_check": [
        lambda p: f"Em {max(1.0, (p.cutoffs[0]['score'] - 2.0) if p.cutoffs else 15.0):.1f} điểm, học trường {p.hs_name}, có đỗ ngành {p.prog_name} {p.univ_name} không?",
        lambda p: f"Học sinh khối {p.combo_id} muốn học {p.prog_name} {p.univ_name} thì có được không?",
    ],
    "edge_case": [
        lambda p: f"Ngành Hàng không Vũ trụ Đại học {p.univ_name}",
        lambda p: f"Điểm chuẩn trường Đại học Không Tồn Tại năm 2025",
        lambda p: f"Em 15 điểm học trường {p.hs_name} muốn học {p.prog_name} {p.univ_name} tổ hợp {p.combo_id}",
    ],
}


def build_llm_prompt(path: ExtractedPath, category: str) -> str:
    """Build prompt for LLM to generate natural Vietnamese query."""
    cutoff_info = ""
    if path.cutoffs:
        cutoff_info = f"Điểm chuẩn: năm {path.cutoffs[0]['year']} phương thức {path.cutoffs[0]['method']} = {path.cutoffs[0]['score']} ({path.cutoffs[0]['type']})"
    else:
        cutoff_info = "Điểm chuẩn: chưa có dữ liệu"

    aptitude_info = ", ".join(path.aptitude_codes) if path.aptitude_codes else "Không có"

    category_instructions = {
        "factual": "Hỏi trực tiếp về điểm chuẩn, tổ hợp xét tuyển, hoặc thông tin chung của ngành/trường.",
        "multi_hop": "Hỏi từ góc độ học sinh lớp 9 tại trường THPT cụ thể, muốn biết nhóm môn/năng khiếu phù hợp cho ngành mục tiêu.",
        "rule_check": "Hỏi kiểm tra điều kiện: điểm thấp hơn điểm chuẩn, tổ hợp sai, thiếu môn, hoặc năng khiếu không phù hợp.",
        "edge_case": "Tạo câu hỏi giả định: ngành không tồn tại, trường không có trong dữ liệu, hoặc điều kiện mâu thuẫn (điểm quá thấp, tổ hợp sai).",
    }

    prompt = f"""Bạn là một học sinh lớp 9 tại Việt Nam đang tìm hiểu thông tin tuyển sinh đại học.

Thông tin đường đi thực tế trong cơ sở dữ liệu:
- Trường THPT: {path.hs_name} ({path.hs_code})
- Nhóm môn: {path.sg_name} ({path.sg_code}) - Các môn: {', '.join(path.subject_names)}
- Tổ hợp xét tuyển: {path.combo_display} (ID chuẩn: {path.combo_id})
- Ngành mục tiêu: {path.prog_name} (Mã: {path.prog_code}) - Trường: {path.univ_name} ({path.univ_code})
- {cutoff_info}
- Năng khiếu: {aptitude_info}

Hãy viết MỘT câu hỏi tự nhiên tiếng Việt theo phong cách học sinh lớp 9 cho loại: {category}
Yêu cầu: {category_instructions[category]}
Trả về CHỈ câu hỏi, KHÔNG giải thích, KHÔNG markdown, KHÔNG in đậm."""

    return prompt


def generate_with_llm(path: ExtractedPath, category: str, generator: RAGGenerator) -> Optional[str]:
    """Generate query using LLM via 9router API."""
    try:
        prompt = build_llm_prompt(path, category)
        # Use the generator with minimal context - we just want the raw LLM response
        # We'll call the API directly for more control
        import requests

        api_base = settings.LLM_API_BASE.rstrip('/')
        api_key = settings.LLM_API_KEY
        model = settings.LLM_MODEL

        if not api_key or api_key.startswith("your_"):
            return None

        endpoint = f"{api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "Bạn là một học sinh lớp 9 Việt Nam viết câu hỏi tuyển sinh tự nhiên. Chỉ trả về câu hỏi, không giải thích."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.7,
            "stream": False
        }

        response = requests.post(endpoint, headers=headers, json=payload, timeout=settings.LLM_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        if "choices" in data and data["choices"]:
            content = data["choices"][0].get("message", {}).get("content", "").strip()
            return content

    except Exception as e:
        logger.warning(f"LLM generation failed for category '{category}': {e}")

    return None


def generate_with_template(path: ExtractedPath, category: str, template_index: int = 0) -> str:
    """Generate query using template-based fallback."""
    templates = TEMPLATE_GENERATORS.get(category, TEMPLATE_GENERATORS["factual"])
    idx = min(template_index, len(templates) - 1)
    return templates[idx](path)


def build_filters(path: ExtractedPath, category: str, wrong_combo: Optional[str] = None) -> Dict[str, Any]:
    """Build retriever filters from path based on category."""
    filters = {
        "keyword": None,
        "university": None,
        "score_max": None,
        "combination": None,
        "high_school": None,
    }

    if category == "factual":
        filters["keyword"] = path.prog_name
        filters["university"] = path.univ_name
    elif category == "multi_hop":
        filters["high_school"] = path.hs_name
        filters["keyword"] = path.prog_name
    elif category == "rule_check":
        # Use score below cutoff
        if path.cutoffs:
            filters["score_max"] = max(1.0, path.cutoffs[0]["score"] - 2.0)
        else:
            filters["score_max"] = 15.0
        filters["high_school"] = path.hs_name
        if wrong_combo:
            filters["combination"] = wrong_combo
        else:
            filters["combination"] = path.combo_id
    elif category == "edge_case":
        # Edge cases handled separately in build_golden.py
        pass

    # Remove None values
    return {k: v for k, v in filters.items() if v is not None}


def build_expected_answer(path: ExtractedPath, category: str) -> str:
    """Build expected answer summary from path data."""
    parts = []
    if path.cutoffs:
        for c in path.cutoffs:
            parts.append(f"Năm {c['year']} (Phương thức {c['method']}): Điểm {c['type']} = {c['score']}")
    else:
        parts.append("Chưa có dữ liệu điểm chuẩn")

    combos = [path.combo_display] if path.combo_display else [path.combo_id]
    parts.append(f"Tổ hợp: {', '.join(combos)}")

    if path.subject_names:
        parts.append(f"Môn bắt buộc: {', '.join(path.subject_names)}")

    return ". ".join(parts)


def generate_golden_query(
    path: ExtractedPath,
    category: str,
    query_id: str,
    generator: Optional[RAGGenerator] = None,
    template_index: int = 0,
    wrong_combo: Optional[str] = None,
) -> GoldenQuery:
    """Generate a single golden query from a path."""
    # Try LLM first
    query_text = None
    if generator:
        query_text = generate_with_llm(path, category, generator)
        if query_text:
            logger.info(f"LLM generated query for {query_id} ({category})")

    # Fallback to template
    if not query_text:
        query_text = generate_with_template(path, category, template_index)
        logger.warning(f"Using template fallback for {query_id} ({category})")

    # Build filters
    filters = build_filters(path, category, wrong_combo)

    # Expected programs and combinations depend on category
    # For rule_check and edge_case: expected empty results due to contradictory constraints
    if category in ("rule_check", "edge_case"):
        expected_programs = []
        expected_combinations = []
        expected_answer = "Không đủ điều kiện xét tuyển (điểm số hoặc tổ hợp môn không thỏa mãn yêu cầu của ngành)."
    else:
        # Expected programs (UID format: univ_code|prog_code)
        expected_programs = [f"{path.univ_code}|{path.prog_code}"]
        # Expected combinations (canonical IDs)
        expected_combinations = [path.combo_id]
        # Expected answer
        expected_answer = build_expected_answer(path, category)

    # Source path for traceability
    source_path = {
        "hs_code": path.hs_code,
        "hs_name": path.hs_name,
        "sg_uid": path.sg_uid,
        "sg_code": path.sg_code,
        "sg_name": path.sg_name,
        "subject_codes": path.subject_codes,
        "subject_names": path.subject_names,
        "combo_id": path.combo_id,
        "combo_display": path.combo_display,
        "tp_uid": path.tp_uid,
        "prog_code": path.prog_code,
        "prog_name": path.prog_name,
        "univ_code": path.univ_code,
        "univ_name": path.univ_name,
        "cutoffs": path.cutoffs,
        "aptitude_codes": path.aptitude_codes,
    }

    return GoldenQuery(
        query_id=query_id,
        query=query_text,
        category=category,
        filters=filters,
        expected_programs=expected_programs,
        expected_combinations=expected_combinations,
        expected_answer=expected_answer,
        source_path=source_path,
    )


def generate_edge_case_queries(base_path: ExtractedPath, start_id: int) -> List[GoldenQuery]:
    """Generate 3 synthetic edge case queries."""
    queries = []

    # Edge case 1: Non-existent program
    q1 = GoldenQuery(
        query_id=f"Q{start_id:03d}",
        query=f"Ngành Hàng không Vũ trụ Đại học {base_path.univ_name}",
        category="edge_case",
        filters={"keyword": "Hàng không Vũ trụ", "university": base_path.univ_name},
        expected_programs=[],
        expected_combinations=[],
        expected_answer="Không tìm thấy thông tin ngành học phù hợp.",
        source_path={"type": "synthetic", "description": "Non-existent program"}
    )
    queries.append(q1)

    # Edge case 2: Non-existent university
    q2 = GoldenQuery(
        query_id=f"Q{start_id+1:03d}",
        query="Điểm chuẩn trường Đại học Không Tồn Tại năm 2025",
        category="edge_case",
        filters={"university": "Đại học Không Tồn Tại"},
        expected_programs=[],
        expected_combinations=[],
        expected_answer="Không tìm thấy thông tin trường đại học này.",
        source_path={"type": "synthetic", "description": "Non-existent university"}
    )
    queries.append(q2)

    # Edge case 3: Contradictory constraints (low score + wrong combo)
    q3 = GoldenQuery(
        query_id=f"Q{start_id+2:03d}",
        query=f"Em 15 điểm học trường {base_path.hs_name} muốn học {base_path.prog_name} {base_path.univ_name} tổ hợp {base_path.combo_id}",
        category="edge_case",
        filters={
            "score_max": 15.0,
            "high_school": base_path.hs_name,
            "keyword": base_path.prog_name,
            "university": base_path.univ_name,
            "combination": base_path.combo_id,
        },
        expected_programs=[],
        expected_combinations=[],
        expected_answer="Điểm 15 thấp hơn điểm chuẩn, không đủ điều kiện xét tuyển.",
        source_path={"type": "synthetic", "description": "Contradictory constraints"}
    )
    queries.append(q3)

    return queries


def main():
    """Standalone test: generate sample queries."""
    from .extract_paths import get_neo4j_driver, extract_all_paths, stratified_sample

    driver = get_neo4j_driver()
    try:
        paths = extract_all_paths(driver)
        sampled = stratified_sample(paths, per_category=2)

        generator = RAGGenerator()

        all_queries = []
        query_counter = 1

        for category, cat_paths in sampled.items():
            for i, path in enumerate(cat_paths):
                gq = generate_golden_query(path, category, f"Q{query_counter:03d}", generator, template_index=i)
                all_queries.append(gq)
                query_counter += 1
                print(f"\n{gq.query_id} [{gq.category}]: {gq.query}")
                print(f"  Filters: {gq.filters}")
                print(f"  Expected programs: {gq.expected_programs}")
                print(f"  Expected combos: {gq.expected_combinations}")

        # Add edge cases
        if paths:
            edge_queries = generate_edge_case_queries(paths[0], query_counter)
            for eq in edge_queries:
                all_queries.append(eq)
                print(f"\n{eq.query_id} [{eq.category}]: {eq.query}")

        print(f"\nTotal generated: {len(all_queries)} queries")

    finally:
        driver.close()


if __name__ == "__main__":
    main()
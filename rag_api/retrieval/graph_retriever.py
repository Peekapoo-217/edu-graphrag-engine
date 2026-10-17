# -*- coding: utf-8 -*-
"""
retrieval/graph_retriever.py

Neo4j Knowledge Graph & Vector Hybrid Retriever module.
Combines Vector Similarity Search (bge-m3 via Ollama) and Parameterized Cypher Multi-hop Graph Traversal
down to High School Subject Groups and detailed subject lists.
Fully compliant with system architecture and coding guidelines.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
import neo4j

# Ensure data-scrawler root directory is in sys.path for direct script execution
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger


SCHOOL_GROUPS_QUERY = """
    MATCH (hs:HighSchool)-[:OFFERS_GROUP]->(sg:SubjectGroup)-[:INCLUDES_SUBJECT]->(subject:Subject)
    WHERE toLower(hs.name) CONTAINS $high_school_name
       OR hs.code = $high_school_code
    WITH hs, sg, subject
    WHERE coalesce(sg.is_published, true) = true
    WITH hs, sg,
         collect(DISTINCT {code: subject.code,
                           canonical_code: coalesce(subject.canonical_subject_code, subject.code),
                           name: subject.name}) AS subjects
    RETURN hs.code AS high_school_code,
           hs.name AS high_school_name,
           sg.academic_year AS academic_year,
           sg.group_code AS subject_group_code,
           sg.name AS subject_group_name,
           subjects
    ORDER BY high_school_code, academic_year DESC, subject_group_code
"""

MANDATORY_SUBJECTS_QUERY = """
    MATCH (subject:Subject)
    WHERE subject.is_mandatory = true
    RETURN collect(DISTINCT coalesce(subject.canonical_subject_code, subject.code)) AS subject_codes
"""


def _canonical_subject_code(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("canonical_code") or value.get("canonical_subject_code") or value.get("code")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _matching_school_groups(required_subject_codes: List[str], groups: List[Dict[str, Any]],
                            mandatory_subject_codes: List[str]) -> List[Dict[str, Any]]:
    """Return latest-year school groups that cover every subject in an admission combination."""
    required = {_canonical_subject_code(code) for code in required_subject_codes}
    required.discard(None)
    if not required:
        return []

    mandatory = {_canonical_subject_code(code) for code in mandatory_subject_codes}
    mandatory.discard(None)
    latest_year_by_school: Dict[str, str] = {}
    for group in groups:
        school_code = str(group.get("high_school_code") or "")
        academic_year = str(group.get("academic_year") or "")
        if school_code and academic_year > latest_year_by_school.get(school_code, ""):
            latest_year_by_school[school_code] = academic_year

    matches = []
    for group in groups:
        school_code = str(group.get("high_school_code") or "")
        if not school_code or group.get("academic_year") != latest_year_by_school.get(school_code):
            continue
        subjects = group.get("subjects") or []
        offered = {_canonical_subject_code(subject) for subject in subjects}
        offered.discard(None)
        if required.issubset(offered | mandatory):
            matches.append(group)
    return matches


class Neo4jRetriever:
    """Hybrid GraphRAG Retriever combining Neo4j Vector Search and Multi-hop Traversal down to High School data."""

    def __init__(
        self,
        uri: Optional[str] = None,
        auth: Optional[tuple] = None,
        database: Optional[str] = None
    ) -> None:
        """Initialize Neo4j connection settings using centralized configuration."""
        self.uri: str = uri or settings.NEO4J_URI
        self.auth: tuple = auth or (settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD or "")
        self.database: str = database or settings.NEO4J_DATABASE
        self._driver: Optional[neo4j.Driver] = None

    def get_driver(self) -> neo4j.Driver:
        """Lazy initialization of Neo4j Driver instance."""
        if self._driver is None:
            try:
                self._driver = neo4j.GraphDatabase.driver(self.uri, auth=self.auth)
                self._driver.verify_connectivity()
                logger.info(f"Connected to Neo4j database '{self.database}' at {self.uri}")
            except Exception as e:
                logger.error(f"Failed to connect to Neo4j database: {e}")
                raise RuntimeError(f"Neo4j Connection Error: {e}") from e
        return self._driver

    def close(self) -> None:
        """Close active Neo4j Driver connection."""
        if self._driver is not None:
            self._driver.close()
            self._driver = None
            logger.info("Closed Neo4j Driver connection.")

    def _get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        ollama_url: Optional[str] = None,
        timeout: Optional[float] = None
    ) -> List[float]:
        """
        Fetch 1024-dimensional embedding vector from Ollama REST API (bge-m3 model).

        Args:
            text (str): Input text string.
            model (Optional[str]): Ollama model name.
            ollama_url (Optional[str]): Ollama API base URL.
            timeout (Optional[float]): Request timeout in seconds.

        Returns:
            List[float]: 1024-dim embedding vector, or empty list on error.
        """
        if not text or not text.strip():
            return []

        model_name = model or settings.OLLAMA_EMBED_MODEL
        host_url = ollama_url or settings.OLLAMA_HOST
        timeout_val = timeout or settings.OLLAMA_TIMEOUT

        endpoint = f"{host_url.rstrip('/')}/api/embeddings"
        payload = json.dumps({"model": model_name, "prompt": text.strip()}).encode("utf-8")
        req = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_val) as response:
                if response.status == 200:
                    res_body = json.loads(response.read().decode("utf-8"))
                    embedding = res_body.get("embedding")
                    if isinstance(embedding, list) and len(embedding) > 0:
                        return [float(v) for v in embedding]
                    logger.warning("Ollama API returned an empty embedding vector.")
                else:
                    logger.error(f"Ollama HTTP error status: {response.status}")
        except urllib.error.URLError as e:
            logger.error(f"Failed to connect to Ollama REST API at {endpoint}: {e}")
        except Exception as e:
            logger.error(f"Unexpected error in _get_embedding: {e}")

        return []

    def retrieve_context(
        self,
        filters: Dict[str, Any],
        top_k: int = 3,
        score_threshold: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Execute Hybrid Retrieval using Parameterized Cypher queries traversing University -> Combination -> Subject -> HighSchool & SubjectGroup.

        Args:
            filters (Dict[str, Any]): Search filters parsed by Intent Parser.
                - 'keyword': (str|None)
                - 'university': (str|None)
                - 'score_max': (float|None)
                - 'combination': (str|None)
                - 'high_school': (str|None)
            top_k (int): Number of top vector matches to retrieve.
            score_threshold (Optional[float]): Minimum similarity score for vector search (default from settings.VECTOR_SIMILARITY_THRESHOLD or 0.65).

        Returns:
            List[Dict[str, Any]]: List of structured graph records (deduplicated by program).
        """
        keyword: Optional[str] = filters.get("keyword")
        university: Optional[str] = filters.get("university")
        score_max: Optional[float] = filters.get("score_max")
        combination: Optional[str] = filters.get("combination")
        high_school: Optional[str] = filters.get("high_school") or filters.get("high_school_code")

        # Resolve similarity threshold from settings or use default 0.65
        threshold = score_threshold if score_threshold is not None else getattr(settings, 'VECTOR_SIMILARITY_THRESHOLD', 0.65)

        vector: List[float] = []
        if keyword:
            vector = self._get_embedding(keyword)

        # Build dynamic WHERE clauses for hard filters
        combination_filter = ""
        score_max_filter = ""
        
        if combination:
            combination_filter = """
            MATCH (p)-[r_acc_f:ACCEPTS_COMBINATION]->(:AdmissionCombination {id: $combination})
            """
        
        if score_max is not None:
            if combination:
                # Combination-specific cutoff: match through the ACCEPTS_COMBINATION relationship
                # to get the correct admission_year and method_code for that combination
                score_max_filter = """
                MATCH (p)-[r_acc_cs:ACCEPTS_COMBINATION]->(:AdmissionCombination {id: $combination})
                MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)
                WHERE cs.cutoff_score_thpt IS NOT NULL
                  AND cs.cutoff_score_thpt <= $score_max
                  AND cs.admission_year = r_acc_cs.admission_year
                  AND cs.method_code IN split(r_acc_cs.method_codes, ',')
                """
            else:
                # General cutoff filter (any combination)
                score_max_filter = """
                MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)
                WHERE cs.cutoff_score_thpt IS NOT NULL AND cs.cutoff_score_thpt <= $score_max
                """

        candidate_limit = min(max(top_k, 1) * (10 if high_school else 1), 100)
        if vector:
            cypher_query = f"""
            CALL db.index.vector.queryNodes('program_vector_index', $candidate_limit, $vector) YIELD node AS p, score AS similarity_score
            WHERE similarity_score >= $score_threshold
            MATCH (u:University)-[:OFFERS_PROGRAM]->(p)
            WHERE ($university IS NULL OR toLower(u.name) CONTAINS toLower($university) OR toLower(u.code) CONTAINS toLower($university))

            {combination_filter}
            {score_max_filter}

            OPTIONAL MATCH (p)-[r_acc:ACCEPTS_COMBINATION]->(c:AdmissionCombination)
            WHERE ($combination IS NULL OR c.id = $combination)

            OPTIONAL MATCH (c)-[r_req:REQUIRES_SUBJECT]->(s:Subject)
            OPTIONAL MATCH (c)-[:REQUIRES_APTITUDE]->(apt:AptitudeTest)

            OPTIONAL MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)

            WITH u, p, similarity_score, c, r_acc,
                 collect(DISTINCT s.name) AS required_subjects,
                 collect(DISTINCT coalesce(s.canonical_subject_code, s.code)) AS required_subject_codes,
                 collect(DISTINCT apt.code) AS required_aptitude_codes,
                 collect(DISTINCT {{
                     admission_year: cs.admission_year,
                     score_thpt: cs.cutoff_score_thpt,
                     score_sat: cs.cutoff_score_sat,
                     method_code: cs.method_code
                 }}) AS cutoff_scores

            RETURN
                p.name AS program_name,
                p.program_code AS program_code,
                u.name AS university_name,
                u.code AS university_code,
                c.id AS combination_id,
                c.display_name AS combination_name,
                required_subjects,
                required_subject_codes,
                required_aptitude_codes,
                cutoff_scores,
                similarity_score AS score
            ORDER BY score DESC
            LIMIT $candidate_limit
            """
        else:
            cypher_query = f"""
            MATCH (u:University)-[:OFFERS_PROGRAM]->(p:TrainingProgram)
            WHERE ($university IS NULL OR toLower(u.name) CONTAINS toLower($university) OR toLower(u.code) CONTAINS toLower($university))
              AND ($keyword IS NULL OR toLower(p.name) CONTAINS toLower($keyword) OR toLower(p.program_code) CONTAINS toLower($keyword))

            {combination_filter}
            {score_max_filter}

            OPTIONAL MATCH (p)-[r_acc:ACCEPTS_COMBINATION]->(c:AdmissionCombination)
            WHERE ($combination IS NULL OR c.id = $combination)

            OPTIONAL MATCH (c)-[r_req:REQUIRES_SUBJECT]->(s:Subject)
            OPTIONAL MATCH (c)-[:REQUIRES_APTITUDE]->(apt:AptitudeTest)

            OPTIONAL MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)

            WITH u, p, c, r_acc,
                 collect(DISTINCT s.name) AS required_subjects,
                 collect(DISTINCT coalesce(s.canonical_subject_code, s.code)) AS required_subject_codes,
                 collect(DISTINCT apt.code) AS required_aptitude_codes,
                 collect(DISTINCT {{
                     admission_year: cs.admission_year,
                     score_thpt: cs.cutoff_score_thpt,
                     score_sat: cs.cutoff_score_sat,
                     method_code: cs.method_code
                 }}) AS cutoff_scores

            RETURN
                p.name AS program_name,
                p.program_code AS program_code,
                u.name AS university_name,
                u.code AS university_code,
                c.id AS combination_id,
                c.display_name AS combination_name,
                required_subjects,
                required_subject_codes,
                required_aptitude_codes,
                cutoff_scores,
                1.0 AS score
            LIMIT $candidate_limit
            """

        query_params = {
            "candidate_limit": candidate_limit,
            "vector": vector if vector else [],
            "keyword": keyword,
            "university": university,
            "score_max": score_max,
            "combination": combination,
            "score_threshold": threshold
        }

        records: List[Dict[str, Any]] = []
        school_groups: List[Dict[str, Any]] = []
        mandatory_subject_codes: List[str] = []
        try:
            driver = self.get_driver()
            with driver.session(database=self.database) as session:
                result = session.run(cypher_query, **query_params)
                for record in result:
                    records.append(record.data())
                if high_school:
                    school_groups = [record.data() for record in session.run(
                        SCHOOL_GROUPS_QUERY,
                        high_school_name=high_school.casefold(),
                        high_school_code=high_school.strip().upper())]
                    mandatory_record = session.run(MANDATORY_SUBJECTS_QUERY).single()
                    if mandatory_record:
                        mandatory_subject_codes = mandatory_record.get("subject_codes") or []
        except Exception as e:
            logger.error(f"Error executing Neo4j retrieval Cypher query: {e}")
            return []

        # Deduplicate by program UID (university_code|program_code) and merge combinations
        deduped = {}
        for r in records:
            matching_groups = []
            if high_school:
                if r.get("required_aptitude_codes"):
                    continue
                matching_groups = _matching_school_groups(
                    r.get("required_subject_codes") or [], school_groups, mandatory_subject_codes)
                if not matching_groups:
                    continue
                r["matching_subject_groups"] = matching_groups
                first_group = matching_groups[0]
                r["high_school_name"] = first_group.get("high_school_name")
                r["subject_group_name"] = first_group.get("subject_group_name")
                r["subject_group_code"] = first_group.get("subject_group_code")
                r["group_subjects"] = [subject.get("name", "") for subject in first_group.get("subjects", [])]

            univ_code = r.get("university_code")
            prog_code = r.get("program_code")
            if not univ_code or not prog_code:
                continue
            uid = f"{univ_code}|{prog_code}"
            combination = None
            comb_id = r.get("combination_id")
            if comb_id:
                combination = {
                    "id": comb_id,
                    "display_name": r.get("combination_name"),
                    "required_subjects": r.get("required_subjects") or [],
                    "required_subject_codes": r.get("required_subject_codes") or [],
                    "matching_subject_groups": matching_groups,
                }
            if uid not in deduped:
                deduped[uid] = r
                deduped[uid]["combinations"] = [combination] if combination else []
                deduped[uid]["matching_subject_groups"] = matching_groups
            else:
                current = deduped[uid]
                if combination:
                    existing_combination = next((item for item in current["combinations"] if item["id"] == comb_id), None)
                    if existing_combination is None:
                        current["combinations"].append(combination)
                    else:
                        existing_group_keys = {
                            (item.get("high_school_code"), item.get("academic_year"), item.get("subject_group_code"))
                            for item in existing_combination.get("matching_subject_groups", [])
                        }
                        for group in matching_groups:
                            group_key = (group.get("high_school_code"), group.get("academic_year"), group.get("subject_group_code"))
                            if group_key not in existing_group_keys:
                                existing_combination["matching_subject_groups"].append(group)
                                existing_group_keys.add(group_key)
                current_groups = {(item.get("high_school_code"), item.get("academic_year"), item.get("subject_group_code"))
                                  for item in current.get("matching_subject_groups", [])}
                for group in matching_groups:
                    group_key = (group.get("high_school_code"), group.get("academic_year"), group.get("subject_group_code"))
                    if group_key not in current_groups:
                        current["matching_subject_groups"].append(group)
                        current_groups.add(group_key)

        # Convert back to list, keep first combination as primary for backward compatibility
        final_records = []
        for r in deduped.values():
            if r.get("combinations"):
                # Keep first as primary fields
                primary = r["combinations"][0]
                r["combination_id"] = primary["id"]
                r["combination_name"] = primary["display_name"]
            final_records.append(r)

        return final_records[:top_k]

    def format_for_llm(self, records: List[Dict[str, Any]]) -> str:
        """
        Format retrieved graph records into clean Markdown text context for LLM generation.

        Args:
            records (List[Dict[str, Any]]): Structured records returned by retrieve_context.

        Returns:
            str: Markdown formatted string context.
        """
        if not records:
            return "Không tìm thấy thông tin ngành học phù hợp với điều kiện tìm kiếm trong Đồ thị tri thức."

        lines: List[str] = ["### NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC (NEO4J KNOWLEDGE GRAPH)\n"]

        for idx, rec in enumerate(records, 1):
            univ_name = rec.get("university_name", "N/A")
            univ_code = rec.get("university_code", "N/A")
            prog_name = rec.get("program_name", "N/A")
            prog_code = rec.get("program_code", "N/A")
            score = rec.get("score")
            score_str = f" (Độ tương đồng Vector: {score:.4f})" if score is not None else ""

            lines.append(f"#### {idx}. Ngành: {prog_name} (Mã ngành: {prog_code}){score_str}")
            lines.append(f"- **Trường Đại học:** {univ_name} ({univ_code})")

            # Admission combinations retained here have been checked against every
            # required subject when a high-school filter is present.
            combinations = rec.get("combinations") or []
            if not combinations and rec.get("combination_id"):
                combinations = [{"id": rec.get("combination_id"),
                                 "display_name": rec.get("combination_name"),
                                 "required_subjects": rec.get("required_subjects", [])}]
            for item in combinations:
                required = item.get("required_subjects", [])
                subj_info = f" -> Môn yêu cầu: {', '.join(required)}" if required else ""
                lines.append(f"- **Tổ hợp môn xét tuyển:** {item.get('id') or ''} ({item.get('display_name') or ''}){subj_info}")
                groups = item.get("matching_subject_groups") or []
                if not groups and (rec.get("high_school_name") or rec.get("subject_group_name")):
                    groups = [{"high_school_name": rec.get("high_school_name"),
                               "subject_group_name": rec.get("subject_group_name"),
                               "subject_group_code": rec.get("subject_group_code"),
                               "subjects": [{"name": name} for name in rec.get("group_subjects", [])]}]
                for group in groups:
                    group_subjects = [subject.get("name", "") for subject in group.get("subjects", [])]
                    subj_list = f" -> Môn trong nhóm: **{', '.join(group_subjects)}**" if group_subjects else ""
                    lines.append(f"  - Nhóm môn phù hợp: {group.get('high_school_name') or 'Trường THPT'} - "
                                 f"{group.get('subject_group_name') or ''} (Mã: {group.get('subject_group_code') or 'N/A'}, "
                                 f"năm học {group.get('academic_year') or 'không rõ'}){subj_list}")
                    lines.append("  - Phạm vi khớp chỉ là độ phủ môn học; không xác nhận thí sinh đủ điều kiện tuyển sinh.")

            # Cutoff Scores
            cutoffs = rec.get("cutoff_scores", [])
            valid_cutoffs = [cs for cs in cutoffs if cs.get("admission_year")]
            if valid_cutoffs:
                lines.append("- **Lịch sử điểm chuẩn:**")
                for cs in valid_cutoffs:
                    yr = cs.get("admission_year")
                    thpt = cs.get("score_thpt")
                    sat = cs.get("score_sat")
                    method = cs.get("method_code", "N/A")
                    score_items = []
                    if thpt is not None:
                        score_items.append(f"Điểm THPT: **{thpt}**")
                    if sat is not None:
                        score_items.append(f"Điểm SAT: **{sat}**")
                    details = ", ".join(score_items) if score_items else "Chưa có thông tin điểm"
                    lines.append(f"  - Năm {yr} (Phương thức {method}): {details}")

            lines.append("")

        return "\n".join(lines)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print("=== TESTING HYBRID GRAPH RETRIEVER (WITH DETAILED SUBJECT GROUP SUBJECTS) ===")

    # Test mock filter payload with high_school
    test_filters = {
        "keyword": "máy tính",
        "university": "Bách Khoa",
        "score_max": 25.0,
        "high_school": "Trần Cao Vân"
    }

    print(f"\n[Input Filters]: {json.dumps(test_filters, ensure_ascii=False)}")

    retriever = Neo4jRetriever()
    try:
        results = retriever.retrieve_context(filters=test_filters, top_k=3)
        print(f"\n[Retrieved Records Count]: {len(results)}")
        formatted_context = retriever.format_for_llm(results)
        print(f"\n[Formatted LLM Context]:\n{formatted_context}")
    except Exception as err:
        logger.error(f"Main execution test error: {err}")
    finally:
        retriever.close()

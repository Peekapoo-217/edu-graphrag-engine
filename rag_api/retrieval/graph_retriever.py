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

        # Soft MATCH clause for optional HighSchool & SubjectGroup filtering with detailed subjects
        hs_match_clause = ""
        hs_return_clause = ""
        hs_with_clause = ""
        if high_school:
            hs_match_clause = """
            MATCH (hs:HighSchool)-[:OFFERS_GROUP]->(sg:SubjectGroup)-[:INCLUDES_SUBJECT]->(s)
            WHERE ($high_school IS NULL OR toLower(hs.name) CONTAINS toLower($high_school) OR toLower(hs.code) CONTAINS toLower($high_school))
            OPTIONAL MATCH (sg)-[:INCLUDES_SUBJECT]->(all_sg_subj:Subject)
            """
            hs_with_clause = ", hs, sg, collect(DISTINCT all_sg_subj.name) AS group_subjects"
            hs_return_clause = ", hs.name AS high_school_name, sg.name AS subject_group_name, sg.group_code AS subject_group_code, group_subjects"

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

        if vector:
            cypher_query = f"""
            CALL db.index.vector.queryNodes('program_vector_index', $top_k, $vector) YIELD node AS p, score AS similarity_score
            WHERE similarity_score >= $score_threshold
            MATCH (u:University)-[:OFFERS_PROGRAM]->(p)
            WHERE ($university IS NULL OR toLower(u.name) CONTAINS toLower($university) OR toLower(u.code) CONTAINS toLower($university))

            {combination_filter}
            {score_max_filter}

            OPTIONAL MATCH (p)-[r_acc:ACCEPTS_COMBINATION]->(c:AdmissionCombination)
            WHERE ($combination IS NULL OR c.id = $combination)

            OPTIONAL MATCH (c)-[r_req:REQUIRES_SUBJECT]->(s:Subject)

            {hs_match_clause}

            OPTIONAL MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)

            WITH u, p, similarity_score, c, r_acc {hs_with_clause},
                 collect(DISTINCT s.name) AS required_subjects,
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
                cutoff_scores,
                similarity_score AS score
                {hs_return_clause}
            ORDER BY score DESC
            LIMIT $top_k
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

            {hs_match_clause}

            OPTIONAL MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)

            WITH u, p, c, r_acc {hs_with_clause},
                 collect(DISTINCT s.name) AS required_subjects,
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
                cutoff_scores,
                1.0 AS score
                {hs_return_clause}
            LIMIT $top_k
            """

        query_params = {
            "top_k": top_k,
            "vector": vector if vector else [],
            "keyword": keyword,
            "university": university,
            "score_max": score_max,
            "combination": combination,
            "high_school": high_school,
            "score_threshold": threshold
        }

        records: List[Dict[str, Any]] = []
        try:
            driver = self.get_driver()
            with driver.session(database=self.database) as session:
                result = session.run(cypher_query, **query_params)
                for record in result:
                    records.append(record.data())
        except Exception as e:
            logger.error(f"Error executing Neo4j retrieval Cypher query: {e}")
            return []

        # Deduplicate by program UID (university_code|program_code) and merge combinations
        deduped = {}
        for r in records:
            univ_code = r.get("university_code")
            prog_code = r.get("program_code")
            if not univ_code or not prog_code:
                continue
            uid = f"{univ_code}|{prog_code}"
            if uid not in deduped:
                deduped[uid] = r
                # Initialize combinations list
                comb_id = r.get("combination_id")
                comb_name = r.get("combination_name")
                if comb_id:
                    deduped[uid]["combinations"] = [{"id": comb_id, "display_name": comb_name}]
                else:
                    deduped[uid]["combinations"] = []
            else:
                # Merge additional combination
                comb_id = r.get("combination_id")
                comb_name = r.get("combination_name")
                if comb_id and comb_id not in [c["id"] for c in deduped[uid]["combinations"]]:
                    deduped[uid]["combinations"].append({"id": comb_id, "display_name": comb_name})

        # Convert back to list, keep first combination as primary for backward compatibility
        final_records = []
        for r in deduped.values():
            if r.get("combinations"):
                # Keep first as primary fields
                primary = r["combinations"][0]
                r["combination_id"] = primary["id"]
                r["combination_name"] = primary["display_name"]
            final_records.append(r)

        return final_records

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

            # Combinations & Required Subjects
            comb_id = rec.get("combination_id")
            comb_name = rec.get("combination_name")
            req_subjs = rec.get("required_subjects", [])
            if comb_id or comb_name:
                subj_info = f" -> Các môn bắt buộc: {', '.join(req_subjs)}" if req_subjs else ""
                lines.append(f"- **Tổ hợp môn xét tuyển:** {comb_id or ''} ({comb_name or ''}){subj_info}")

            # High School & Subject Group info with detailed subjects list
            hs_name = rec.get("high_school_name")
            sg_name = rec.get("subject_group_name")
            sg_code = rec.get("subject_group_code")
            grp_subjs = rec.get("group_subjects", [])
            if hs_name or sg_name:
                subj_list_str = f" -> Các môn học thuộc nhóm môn THPT này: **{', '.join(grp_subjs)}**" if grp_subjs else ""
                lines.append(f"- **Khuyên chọn Nhóm môn THPT:** {hs_name or 'Trường THPT'} - Nhóm môn: **{sg_name or ''}** (Mã nhóm: {sg_code or 'N/A'}){subj_list_str}")

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

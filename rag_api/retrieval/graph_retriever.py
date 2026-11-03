# -*- coding: utf-8 -*-
"""
retrieval/graph_retriever.py

Neo4j Knowledge Graph & Vector Hybrid Retriever module.
Combines Vector Similarity Search (bge-m3 via Ollama) and Parameterized Cypher Multi-hop Graph Traversal
down to High School Subject Groups and detailed subject lists.
Fully compliant with system architecture and coding guidelines.
"""

import json
import hashlib
import os
import re
import sys
import unicodedata
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
from rag_api.retrieval.errors import RetrievalUnavailableError


SCHOOL_GROUPS_QUERY = """
    MATCH (hs:HighSchool)-[:OFFERS_GROUP]->(sg:SubjectGroup)-[:INCLUDES_SUBJECT]->(subject:Subject)
    WHERE ($high_school_code IS NOT NULL AND hs.code = $high_school_code)
       OR ($high_school_code IS NULL AND toLower(hs.name) CONTAINS $high_school_name)
    WITH hs, sg, subject
    WHERE coalesce(sg.is_published, true) = true
      AND ($academic_year IS NULL OR toString(sg.academic_year) = $academic_year)
      AND ($subject_group_code IS NULL OR toLower(sg.group_code) = toLower($subject_group_code))
    OPTIONAL MATCH (subject)-[variant_rel:IS_VARIANT_OF]->(canonical:Subject)
    WITH hs, sg, subject, variant_rel, canonical
    WITH hs, sg,
         collect(DISTINCT {code: subject.code,
                           canonical_code: coalesce(subject.canonical_subject_code, subject.code),
                           variant_relationship_observed:
                               variant_rel IS NOT NULL
                               AND canonical.code = coalesce(subject.canonical_subject_code, subject.code),
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


def _normalize_university_filter(value: Any) -> Optional[str]:
    """Expand the common Vietnamese university abbreviation for name substring matching."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return re.sub(r"^(?:đh|dh)\.?(?=\s|$)", "Đại học", value, count=1, flags=re.IGNORECASE)


def _canonical_subject_code(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("canonical_code") or value.get("canonical_subject_code") or value.get("code")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _subject_member_codes(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Return the observed Subject node code and its canonical code without collapsing identity."""
    if isinstance(value, dict):
        raw = value.get("code")
        canonical = value.get("canonical_code") or value.get("canonical_subject_code") or raw
    else:
        raw = value
        canonical = value
    raw_code = raw.strip() if isinstance(raw, str) and raw.strip() else None
    canonical_code = canonical.strip() if isinstance(canonical, str) and canonical.strip() else None
    return raw_code, canonical_code


def _matching_school_groups(required_subject_codes: List[str], groups: List[Dict[str, Any]],
                            mandatory_subject_codes: List[str],
                            academic_year: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return scoped groups whose observed membership covers every required subject.

    ``mandatory_subject_codes`` is retained for call compatibility but is only a global
    catalogue hint and cannot prove school/year/group membership.
    """
    required = {_canonical_subject_code(code) for code in required_subject_codes}
    required.discard(None)
    if not required:
        return []

    latest_year_by_school: Dict[str, str] = {}
    if academic_year is None:
        for group in groups:
            school_code = str(group.get("high_school_code") or "")
            group_year = str(group.get("academic_year") or "")
            if school_code and group_year > latest_year_by_school.get(school_code, ""):
                latest_year_by_school[school_code] = group_year

    matches = []
    for group in groups:
        school_code = str(group.get("high_school_code") or "")
        expected_year = academic_year if academic_year is not None else latest_year_by_school.get(school_code)
        if not school_code or group.get("academic_year") != expected_year:
            continue
        subjects = group.get("subjects") or []
        offered = {_canonical_subject_code(subject) for subject in subjects}
        offered.discard(None)
        if required.issubset(offered):
            matches.append(group)
    return matches


def _subject_overlap_groups(
    required_subject_codes: List[str], groups: List[Dict[str, Any]], *,
    academic_year: Optional[str], subject_group_code: Optional[str],
) -> List[Dict[str, Any]]:
    """Keep only the explicitly scoped groups with source-observed subject identity overlap."""
    required = {_canonical_subject_code(code) for code in required_subject_codes}
    required.discard(None)
    matches = []
    for group in groups:
        if academic_year and str(group.get("academic_year") or "") != academic_year:
            continue
        if subject_group_code and str(group.get("subject_group_code") or "").casefold() != subject_group_code.casefold():
            continue
        overlaps = set()
        for subject in group.get("subjects") or []:
            observed, canonical = _subject_member_codes(subject)
            if not canonical or _canonical_subject_code(canonical) not in required:
                continue
            if observed and observed != canonical and not (
                isinstance(subject, dict) and subject.get("variant_relationship_observed") is True
            ):
                continue
            overlaps.add(_canonical_subject_code(canonical))
        if overlaps:
            selected = dict(group)
            selected["subject_overlap_codes"] = sorted(code.upper() for code in overlaps if code)
            matches.append(selected)
    return matches


def validate_evidence_dependencies(
    record: Dict[str, Any], filters: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Validate a scoped directed edge set and its per-combination/group dependencies.

    A retrieved program record may combine several admission combinations and school groups.
    Its evidence_path is therefore an edge set, not one ordered walk. Repeated references to
    the same directed edge are shared dependencies; an identity reused for other endpoints
    remains a conflict and keeps the record incomplete.
    """
    filters = filters or {}
    requested_year = filters.get("admission_year")
    requested_method = str(filters.get("method_code") or "").strip()
    requested_school = str(filters.get("high_school_code") or filters.get("high_school") or "").strip()
    requested_academic_year = str(filters.get("academic_year") or "").strip()
    requested_group_code = str(filters.get("subject_group_code") or "").strip()
    actual_paths: Dict[str, set[tuple[str, str]]] = {}
    occurrence_counts: Dict[str, int] = {}
    malformed_relationship_ids: set[str] = set()
    path = record.get("evidence_path")
    if not isinstance(path, list):
        path = []
        malformed_relationship_ids.add("evidence_path")
    for index, edge in enumerate(path):
        if not isinstance(edge, dict):
            malformed_relationship_ids.add(f"path-index:{index}")
            continue
        relationship_id = edge.get("relationship_id")
        edge_id = str(relationship_id).strip() if relationship_id is not None else ""
        source = edge.get("from")
        target = edge.get("to")
        if not edge_id or not isinstance(source, str) or not source.strip() \
                or not isinstance(target, str) or not target.strip():
            malformed_relationship_ids.add(edge_id or f"path-index:{index}")
            continue
        actual_paths.setdefault(edge_id, set()).add((source.strip(), target.strip()))
        occurrence_counts[edge_id] = occurrence_counts.get(edge_id, 0) + 1
    # Conflict variants are kept separately when attach_evidence_provenance canonicalizes
    # the public edge set. They are never collapsed into a supposedly valid edge.
    for conflict in record.get("evidence_path_conflicts") or []:
        if not isinstance(conflict, dict):
            malformed_relationship_ids.add("evidence_path_conflicts")
            continue
        edge_id = str(conflict.get("relationship_id") or "").strip()
        variants = conflict.get("endpoints")
        if not edge_id or not isinstance(variants, list) or len(variants) < 2:
            malformed_relationship_ids.add(edge_id or "evidence_path_conflicts")
            continue
        for variant in variants:
            if not isinstance(variant, dict) or not all(
                isinstance(variant.get(key), str) and variant[key].strip() for key in ("from", "to")
            ):
                malformed_relationship_ids.add(edge_id)
                continue
            actual_paths.setdefault(edge_id, set()).add((variant["from"].strip(), variant["to"].strip()))
        occurrence_counts[edge_id] = max(occurrence_counts.get(edge_id, 0), len(variants))

    expected_paths: Dict[str, tuple[str, str]] = {}
    required_occurrences: Dict[str, int] = {}
    missing_scope_dependencies: set[str] = set()
    dependencies: List[Dict[str, Any]] = []
    conflicting_expected_ids: set[str] = set()

    def require(edge_id: str, source: str, target: str) -> None:
        previous = expected_paths.get(edge_id)
        if previous is not None and previous != (source, target):
            conflicting_expected_ids.add(edge_id)
        expected_paths[edge_id] = (source, target)
        required_occurrences[edge_id] = required_occurrences.get(edge_id, 0) + 1

    university = str(record.get("university_code") or "").strip()
    program = str(record.get("program_code") or "").strip()
    if not university or not program:
        missing_scope_dependencies.add("university_program_scope")
    else:
        program_edge_id = "offers-program:" + Neo4jRetriever._stable_digest([university, program])
        require(program_edge_id, f"University:{university}", f"TrainingProgram:{university}|{program}")

    combinations = record.get("combinations") or []
    if not combinations and record.get("combination_id"):
        combinations = [{
            "id": record.get("combination_id"),
            "required_subject_codes": record.get("required_subject_codes") or [],
            "required_aptitude_codes": record.get("required_aptitude_codes") or [],
            "acceptance_records": record.get("acceptance_records") or [],
            "matching_subject_groups": record.get("matching_subject_groups") or [],
        }]
    if not combinations:
        missing_scope_dependencies.add("admission_combination_scope")
    for combination in combinations:
        if not isinstance(combination, dict) or not combination.get("id"):
            continue
        combination_id = str(combination["id"])
        acceptances = combination.get("acceptance_records") or []
        scoped_acceptances = []
        for acceptance in acceptances:
            if not isinstance(acceptance, dict):
                continue
            if requested_year is not None and str(acceptance.get("admission_year")) != str(requested_year):
                continue
            methods = {part.strip() for part in str(acceptance.get("method_codes") or "").split(",")
                       if part.strip()}
            if requested_method and requested_method not in methods:
                continue
            scoped_acceptances.append(acceptance)
        if not scoped_acceptances:
            missing_scope_dependencies.add(f"acceptance_scope:{combination_id}")
        for subject_code in combination.get("required_subject_codes") or []:
            require("requires-subject:" + Neo4jRetriever._stable_digest([
                combination_id, subject_code
            ]), f"AdmissionCombination:{combination_id}", f"Subject:{subject_code}")
        for aptitude_code in combination.get("required_aptitude_codes") or []:
            require("requires-aptitude:" + Neo4jRetriever._stable_digest([
                combination_id, aptitude_code
            ]), f"AdmissionCombination:{combination_id}", f"AptitudeTest:{aptitude_code}")
        groups = combination.get("matching_subject_groups") or []
        requested_groups = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            school = str(group.get("high_school_code") or "")
            year = str(group.get("academic_year") or "")
            group_code = str(group.get("subject_group_code") or "")
            if requested_school and not _school_scope_matches(
                requested_school, school, str(group.get("high_school_name") or "")
            ):
                missing_scope_dependencies.add(f"school_group_scope:{school}:{year}:{group_code}")
                continue
            if requested_academic_year and year != requested_academic_year:
                missing_scope_dependencies.add(f"academic_year_scope:{school}:{year}:{group_code}")
                continue
            if requested_group_code and group_code.casefold() != requested_group_code.casefold():
                missing_scope_dependencies.add(f"subject_group_scope:{school}:{year}:{group_code}")
                continue
            if not school or not group_code or not year:
                missing_scope_dependencies.add(f"school_group_scope:{school}:{year}:{group_code}")
                continue
            requested_groups.append(group)

        for acceptance in scoped_acceptances:
            admission_year = acceptance.get("admission_year")
            if admission_year is None:
                missing_scope_dependencies.add(f"admission_year_scope:{combination_id}")
                continue
            accept_edge_id = "accepts-combination:" + Neo4jRetriever._stable_digest([
                university, program, combination_id, admission_year, acceptance.get("method_codes")
            ])
            require(accept_edge_id, f"TrainingProgram:{university}|{program}",
                    f"AdmissionCombination:{combination_id}")

        combination_required: List[str] = []
        for acceptance in scoped_acceptances:
            admission_year = acceptance.get("admission_year")
            if admission_year is None:
                continue
            acceptance_id = "accepts-combination:" + Neo4jRetriever._stable_digest([
                university, program, combination_id, admission_year, acceptance.get("method_codes")
            ])
            combination_required.append(acceptance_id)
        for subject_code in combination.get("required_subject_codes") or []:
            combination_required.append("requires-subject:" + Neo4jRetriever._stable_digest([
                combination_id, subject_code
            ]))
        for aptitude_code in combination.get("required_aptitude_codes") or []:
            combination_required.append("requires-aptitude:" + Neo4jRetriever._stable_digest([
                combination_id, aptitude_code
            ]))

        if requested_school and not requested_groups:
            missing_scope_dependencies.add(f"school_group_scope:{combination_id}")
        if requested_group_code and not any(
            str(group.get("subject_group_code") or "").casefold() == requested_group_code.casefold()
            for group in requested_groups
        ):
            missing_scope_dependencies.add(f"subject_group_scope:{combination_id}:{requested_group_code}")
        for group in requested_groups:
            school = str(group["high_school_code"]).strip()
            year = str(group["academic_year"]).strip()
            group_code = str(group["subject_group_code"]).strip()
            group_uid = f"{school}|{year}|{group_code}"
            offers_group_id = "offers-group:" + Neo4jRetriever._stable_digest([school, year, group_code])
            require(offers_group_id, f"HighSchool:{school}", f"SubjectGroup:{group_uid}")
            group_required = list(combination_required) + [offers_group_id]
            subject_codes: Dict[str, Dict[Optional[str], bool]] = {}
            for subject in group.get("subjects") or []:
                observed_code, canonical_code = _subject_member_codes(subject)
                if canonical_code:
                    variant_observed = (isinstance(subject, dict)
                                        and subject.get("variant_relationship_observed") is True)
                    subject_codes.setdefault(canonical_code, {})[observed_code] = variant_observed
            mandatory_codes = {
                str(code).strip() for code in (
                    group.get("mandatory_subject_codes") or record.get("mandatory_subject_codes") or []
                ) if isinstance(code, str) and code.strip()
            }
            coverage = []
            required_subjects = {str(code).strip() for code in combination.get("required_subject_codes") or []}
            for subject_code in sorted(required_subjects):
                member_codes = subject_codes.get(subject_code, {})
                observed_member_codes = sorted(code for code in member_codes if code)
                if observed_member_codes:
                    # Prefer an exact canonical node when present. Otherwise, preserve the
                    # actual variant membership and require its directed IS_VARIANT_OF edge.
                    observed_code = subject_code if subject_code in observed_member_codes else observed_member_codes[0]
                    includes_id = "includes-subject:" + Neo4jRetriever._stable_digest(
                        [group_uid, observed_code])
                    require(includes_id, f"SubjectGroup:{group_uid}", f"Subject:{observed_code}")
                    group_required.append(includes_id)
                    if observed_code != subject_code:
                        if member_codes[observed_code]:
                            variant_id = "is-variant-of:" + Neo4jRetriever._stable_digest(
                                [observed_code, subject_code])
                            require(variant_id, f"Subject:{observed_code}", f"Subject:{subject_code}")
                            group_required.append(variant_id)
                            coverage.append({"subject_code": subject_code, "status": "group_member_via_variant",
                                             "observed_subject_code": observed_code,
                                             "relationship_ids": [includes_id, variant_id]})
                        else:
                            missing_scope_dependencies.add(
                                f"variant_relationship_provenance:{school}:{year}:{group_code}:{observed_code}:{subject_code}")
                            coverage.append({"subject_code": subject_code, "status": "unverified",
                                             "reason": "canonical_alias_relationship_unobserved",
                                             "observed_subject_code": observed_code,
                                             "relationship_ids": [includes_id]})
                    else:
                        coverage.append({"subject_code": subject_code, "status": "group_member",
                                         "relationship_id": includes_id})
                elif member_codes:
                    missing_scope_dependencies.add(
                        f"subject_membership_identity:{school}:{year}:{group_code}:{subject_code}")
                    coverage.append({"subject_code": subject_code, "status": "unverified",
                                     "reason": "observed_subject_code_unavailable"})
                elif subject_code in mandatory_codes:
                    # is_mandatory is a global catalog flag in the current source schema. It
                    # has no school/year applicability or source provenance, so it cannot
                    # complete a scoped Graph dependency on its own.
                    missing_scope_dependencies.add(f"mandatory_subject_provenance:{subject_code}")
                    coverage.append({"subject_code": subject_code, "status": "unverified",
                                     "reason": "mandatory_scope_or_provenance_unavailable"})
                else:
                    missing_scope_dependencies.add(f"subject_coverage:{school}:{year}:{group_code}:{subject_code}")
                    coverage.append({"subject_code": subject_code, "status": "unverified"})
            dependencies.append({
                "scope": {"university_code": university, "program_code": program,
                          "combination_id": combination_id,
                          "admission_years": sorted({str(item.get("admission_year")) for item in scoped_acceptances
                                                      if item.get("admission_year") is not None}),
                          "method_codes": sorted({part.strip() for item in scoped_acceptances
                                                   for part in str(item.get("method_codes") or "").split(",")
                                                   if part.strip()}),
                          "high_school_code": school, "academic_year": year,
                          "subject_group_code": group_code},
                "scope_complete": bool(scoped_acceptances and university and program and school and year and group_code),
                "required_relationship_ids": sorted(set(group_required)),
                "subject_coverage": coverage,
            })

    if requested_school and not any(dependency["scope"].get("high_school_code") for dependency in dependencies):
        missing_scope_dependencies.add("school_group_scope")

    shared_relationship_ids = {edge_id for edge_id, required_count in required_occurrences.items()
                                if required_count > 1
                                and occurrence_counts.get(edge_id, 0) <= required_count
                                and edge_id in actual_paths
                                and len(actual_paths[edge_id]) == 1}
    conflicting_relationship_ids = {edge_id for edge_id, endpoints in actual_paths.items() if len(endpoints) > 1}
    conflicting_relationship_ids.update(conflicting_expected_ids)
    duplicate_relationship_ids = {
        edge_id for edge_id, count in occurrence_counts.items() if count > 1
    } | {edge_id for edge_id, count in required_occurrences.items() if count > 1}
    invalid_duplicate_relationship_ids = {
        edge_id for edge_id, count in occurrence_counts.items()
        if count > required_occurrences.get(edge_id, 0)
    }
    missing = sorted(edge_id for edge_id, endpoints in expected_paths.items()
                     if endpoints not in actual_paths.get(edge_id, set())
                     or edge_id in conflicting_relationship_ids
                     or edge_id in invalid_duplicate_relationship_ids)
    observed_ids = set(actual_paths)
    for dependency in dependencies:
        dependency["missing_relationship_ids"] = sorted(
            edge_id for edge_id in dependency["required_relationship_ids"]
            if edge_id not in observed_ids or edge_id in conflicting_relationship_ids
            or edge_id in invalid_duplicate_relationship_ids
        )
        dependency["status"] = "complete" if not dependency["missing_relationship_ids"] and not any(
            item["status"] == "unverified" for item in dependency["subject_coverage"]
        ) and dependency["scope_complete"] else "incomplete"
    return {
        "status": "complete" if (expected_paths and not missing and not missing_scope_dependencies
                                   and not malformed_relationship_ids and not conflicting_relationship_ids
                                   and not invalid_duplicate_relationship_ids) else "incomplete",
        "required_relationship_ids": sorted(expected_paths),
        "observed_relationship_ids": sorted(observed_ids),
        "missing_relationship_ids": missing,
        "missing_scope_dependencies": sorted(missing_scope_dependencies),
        "duplicate_relationship_ids": sorted(duplicate_relationship_ids),
        "shared_relationship_ids": sorted(shared_relationship_ids),
        "invalid_duplicate_relationship_ids": sorted(invalid_duplicate_relationship_ids),
        "conflicting_relationship_ids": sorted(conflicting_relationship_ids),
        "malformed_relationship_ids": sorted(malformed_relationship_ids),
        "dependencies": dependencies,
        # The graph contains aptitude requirements, but school-group coverage is not evidence
        # that a student has completed or passed an aptitude test.
        "aptitude_coverage": "unverified" if any(
            isinstance(item, dict) and item.get("required_aptitude_codes")
            for item in combinations
        ) else "not_required",
    }


def _school_scope_key(value: str) -> str:
    try:
        value = value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    decomposed = unicodedata.normalize("NFD", value.casefold())
    plain = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    words = re.findall(r"[a-z0-9]+", plain)
    while words and words[0] in {"truong", "thpt", "pho", "thong"}:
        words.pop(0)
    return " ".join(words)


def _school_scope_matches(requested: str, code: str, name: str) -> bool:
    request_key = _school_scope_key(requested)
    return bool(request_key) and request_key in {
        _school_scope_key(code), _school_scope_key(name)
    }


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
        keyword: Optional[str] = filters.get("keyword") or filters.get("desired_program")
        university: Optional[str] = _normalize_university_filter(filters.get("university"))
        score_max: Optional[float] = filters.get("score_max")
        combination: Optional[str] = filters.get("combination")
        high_school_code = filters.get("high_school_code")
        high_school_code = str(high_school_code).strip().upper() if high_school_code else None
        high_school: Optional[str] = high_school_code or filters.get("high_school")
        academic_year = str(filters.get("academic_year")) if filters.get("academic_year") is not None else None
        subject_group_code = str(filters.get("subject_group_code")).strip() if filters.get("subject_group_code") else None
        answer_scope = str(filters.get("answer_scope") or "")
        partial_subject_overlap = answer_scope == "subject_overlap"
        raw_admission_year = filters.get("admission_year")
        admission_year = int(raw_admission_year) if raw_admission_year is not None else None
        method_code = str(filters.get("method_code")).strip() if filters.get("method_code") else None
        requested_program_names = list(dict.fromkeys(
            str(value).strip() for value in filters.get("program_names", [])
            if isinstance(value, str) and value.strip()
        ))

        # Resolve similarity threshold from settings or use default 0.65
        threshold = score_threshold if score_threshold is not None else getattr(settings, 'VECTOR_SIMILARITY_THRESHOLD', 0.65)

        vector: List[float] = []
        if keyword and not requested_program_names:
            vector = self._get_embedding(keyword)

        # Build dynamic WHERE clauses for hard filters
        combination_filter = ""
        score_max_filter = ""
        acceptance_conditions = []
        if combination:
            acceptance_conditions.append("c_filter.id = $combination")
        if admission_year is not None:
            acceptance_conditions.append("r_acc_f.admission_year = $admission_year")
        if method_code:
            acceptance_conditions.append(
                "$method_code IN split(replace(coalesce(r_acc_f.method_codes, ''), ' ', ''), ',')"
            )
        if acceptance_conditions:
            combination_filter = """
            MATCH (p)-[r_acc_f:ACCEPTS_COMBINATION]->(c_filter:AdmissionCombination)
            WHERE """ + " AND ".join(acceptance_conditions)
        
        if score_max is not None:
            if combination or admission_year is not None or method_code:
                # Combination-specific cutoff: match through the ACCEPTS_COMBINATION relationship
                # to get the correct admission_year and method_code for that combination
                score_max_filter = """
                MATCH (p)-[r_acc_cs:ACCEPTS_COMBINATION]->(c_score:AdmissionCombination)
                MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)
                WHERE cs.cutoff_score_thpt IS NOT NULL
                  AND cs.cutoff_score_thpt <= $score_max
                  AND cs.admission_year = r_acc_cs.admission_year
                  AND cs.method_code IN split(r_acc_cs.method_codes, ',')
                  AND ($combination IS NULL OR c_score.id = $combination)
                  AND ($admission_year IS NULL OR cs.admission_year = $admission_year)
                  AND ($method_code IS NULL OR cs.method_code = $method_code)
                """
            else:
                # General cutoff filter (any combination)
                score_max_filter = """
                MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)
                WHERE cs.cutoff_score_thpt IS NOT NULL AND cs.cutoff_score_thpt <= $score_max
                  AND ($admission_year IS NULL OR cs.admission_year = $admission_year)
                  AND ($method_code IS NULL OR cs.method_code = $method_code)
                """

        result_limit = min(max(max(top_k, 1), len(requested_program_names)), 100)
        candidate_limit = min(result_limit * (10 if high_school else 1), 100)
        requested_program_filter = """
              AND any(requested_name IN $requested_program_names
                      WHERE normalize(toLower(p.name), NFC)
                            CONTAINS normalize(toLower(requested_name), NFC))
        """ if requested_program_names else ""
        if vector:
            cypher_query = f"""
            CALL db.index.vector.queryNodes('program_vector_index', $candidate_limit, $vector) YIELD node AS p, score AS similarity_score
            WHERE similarity_score >= $score_threshold
            MATCH (u:University)-[:OFFERS_PROGRAM]->(p)
            WHERE ($university IS NULL OR toLower(u.name) CONTAINS toLower($university) OR toLower(u.code) CONTAINS toLower($university))
              AND (NOT $partial_subject_overlap OR $keyword IS NULL
                   OR toLower(p.name) CONTAINS toLower($keyword)
                   OR toLower(p.program_code) CONTAINS toLower($keyword))
              {requested_program_filter}

            {combination_filter}
            {score_max_filter}

            // Select unique programs before expanding each program into combination/subject rows.
            WITH DISTINCT u, p, similarity_score
            ORDER BY similarity_score DESC, u.code, p.program_code
            LIMIT $candidate_limit

            OPTIONAL MATCH (p)-[r_acc:ACCEPTS_COMBINATION]->(c:AdmissionCombination)
            WHERE ($combination IS NULL OR c.id = $combination)
              AND ($admission_year IS NULL OR r_acc.admission_year = $admission_year)
              AND ($method_code IS NULL OR $method_code IN split(replace(coalesce(r_acc.method_codes, ''), ' ', ''), ','))

            OPTIONAL MATCH (c)-[r_req:REQUIRES_SUBJECT]->(s:Subject)
            OPTIONAL MATCH (c)-[:REQUIRES_APTITUDE]->(apt:AptitudeTest)

            OPTIONAL MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)
            WHERE ($admission_year IS NULL OR cs.admission_year = $admission_year)
              AND ($method_code IS NULL OR cs.method_code = $method_code)

            WITH u, p, similarity_score, c, r_acc,
                 collect(DISTINCT s.name) AS required_subjects,
                 collect(DISTINCT coalesce(s.canonical_subject_code, s.code)) AS required_subject_codes,
                 collect(DISTINCT apt.code) AS required_aptitude_codes,
                 collect(DISTINCT CASE WHEN apt IS NULL THEN null
                                       ELSE {{code: apt.code, name: apt.name}} END) AS required_aptitude,
                 collect(DISTINCT {{admission_year: r_acc.admission_year,
                                   method_codes: r_acc.method_codes,
                                   source_url: r_acc.source_url}}) AS acceptance_records,
                 collect(DISTINCT {{
                     admission_year: cs.admission_year,
                     score_thpt: cs.cutoff_score_thpt,
                     score_sat: cs.cutoff_score_sat,
                     method_code: cs.method_code,
                     source_url: cs.source_url
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
                required_aptitude,
                acceptance_records,
                cutoff_scores,
                similarity_score AS score
            ORDER BY score DESC, university_code, program_code, combination_id
            """
        else:
            cypher_query = f"""
            MATCH (u:University)-[:OFFERS_PROGRAM]->(p:TrainingProgram)
            WHERE ($university IS NULL OR toLower(u.name) CONTAINS toLower($university) OR toLower(u.code) CONTAINS toLower($university))
              AND ($requested_program_names IS NOT NULL OR $keyword IS NULL
                   OR toLower(p.name) CONTAINS toLower($keyword) OR toLower(p.program_code) CONTAINS toLower($keyword))
              {requested_program_filter}

            {combination_filter}
            {score_max_filter}

            // Apply the result budget to unique programs, not expanded relationship rows.
            WITH DISTINCT u, p
            ORDER BY u.code, p.program_code
            LIMIT $candidate_limit

            OPTIONAL MATCH (p)-[r_acc:ACCEPTS_COMBINATION]->(c:AdmissionCombination)
            WHERE ($combination IS NULL OR c.id = $combination)
              AND ($admission_year IS NULL OR r_acc.admission_year = $admission_year)
              AND ($method_code IS NULL OR $method_code IN split(replace(coalesce(r_acc.method_codes, ''), ' ', ''), ','))

            OPTIONAL MATCH (c)-[r_req:REQUIRES_SUBJECT]->(s:Subject)
            OPTIONAL MATCH (c)-[:REQUIRES_APTITUDE]->(apt:AptitudeTest)

            OPTIONAL MATCH (p)-[:HAS_CUTOFF]->(cs:CutoffScore)
            WHERE ($admission_year IS NULL OR cs.admission_year = $admission_year)
              AND ($method_code IS NULL OR cs.method_code = $method_code)

            WITH u, p, 1.0 AS similarity_score, c, r_acc,
                 collect(DISTINCT s.name) AS required_subjects,
                 collect(DISTINCT coalesce(s.canonical_subject_code, s.code)) AS required_subject_codes,
                 collect(DISTINCT apt.code) AS required_aptitude_codes,
                 collect(DISTINCT CASE WHEN apt IS NULL THEN null
                                       ELSE {{code: apt.code, name: apt.name}} END) AS required_aptitude,
                 collect(DISTINCT {{admission_year: r_acc.admission_year,
                                   method_codes: r_acc.method_codes,
                                   source_url: r_acc.source_url}}) AS acceptance_records,
                 collect(DISTINCT {{
                     admission_year: cs.admission_year,
                     score_thpt: cs.cutoff_score_thpt,
                     score_sat: cs.cutoff_score_sat,
                     method_code: cs.method_code,
                     source_url: cs.source_url
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
                required_aptitude,
                acceptance_records,
                cutoff_scores,
                similarity_score AS score
            ORDER BY university_code, program_code, combination_id
            """

        query_params = {
            "candidate_limit": candidate_limit,
            "vector": vector if vector else [],
            "keyword": keyword,
            "university": university,
            "score_max": score_max,
            "combination": combination,
            "admission_year": admission_year,
            "method_code": method_code,
            "score_threshold": threshold,
            "partial_subject_overlap": partial_subject_overlap,
            "requested_program_names": requested_program_names or None,
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
                        high_school_name=(str(filters.get("high_school") or "").casefold()
                                          if high_school_code else str(high_school).casefold()),
                        high_school_code=high_school_code,
                        academic_year=academic_year,
                        subject_group_code=subject_group_code)]
                    mandatory_record = session.run(MANDATORY_SUBJECTS_QUERY).single()
                    if mandatory_record:
                        mandatory_subject_codes = mandatory_record.get("subject_codes") or []
                    for group in school_groups:
                        group["mandatory_subject_codes"] = sorted({
                            str(code).strip() for code in mandatory_subject_codes
                            if isinstance(code, str) and code.strip()
                        })
        except Exception as e:
            logger.error("Neo4j retrieval failed: %s", type(e).__name__)
            raise RetrievalUnavailableError("Graph query failed") from e

        # Deduplicate by program UID (university_code|program_code) and merge combinations
        deduped = {}
        for r in records:
            matching_groups = []
            if high_school:
                if r.get("required_aptitude_codes"):
                    r["aptitude_requirements_unverified"] = True
                if partial_subject_overlap:
                    matching_groups = _subject_overlap_groups(
                        r.get("required_subject_codes") or [], school_groups,
                        academic_year=academic_year, subject_group_code=subject_group_code,
                    )
                    program_name = str(r.get("program_name") or "").casefold()
                    program_code = str(r.get("program_code") or "").casefold()
                    if keyword and keyword.casefold() not in program_name and keyword.casefold() not in program_code:
                        continue
                    r["answer_scope"] = "subject_overlap"
                else:
                    matching_groups = _matching_school_groups(
                        r.get("required_subject_codes") or [], school_groups, mandatory_subject_codes,
                        academic_year=academic_year)
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
                    "required_aptitude_codes": r.get("required_aptitude_codes") or [],
                    "required_aptitude": r.get("required_aptitude") or [],
                    "acceptance_records": [item for item in r.get("acceptance_records", [])
                                           if isinstance(item, dict) and item.get("admission_year") is not None],
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

        return final_records[:result_limit]

    @staticmethod
    def _canonical_evidence_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: Neo4jRetriever._canonical_evidence_value(value[key]) for key in sorted(value)}
        if isinstance(value, (list, tuple, set)):
            items = [Neo4jRetriever._canonical_evidence_value(item) for item in value]
            return sorted(items, key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
        return value

    @staticmethod
    def _stable_digest(value: Any) -> str:
        canonical_value = Neo4jRetriever._canonical_evidence_value(value)
        canonical = json.dumps(canonical_value, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]

    def attach_evidence_provenance(
        self, records: List[Dict[str, Any]], filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Attach stable graph edge identities derived only from query-returned facts.

        The public evidence_path is a directed edge set. Repeated use of a shared edge is
        recorded by validation metadata; conflicting endpoint variants remain explicit.
        """
        enriched: List[Dict[str, Any]] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            row = dict(record)
            id_material = {key: value for key, value in row.items()
                           if key not in {"score", "evidence_id", "relationship_ids", "evidence_path",
                                          "evidence_path_conflicts", "evidence_dependency_validation"}}
            row["evidence_id"] = "graph:" + self._stable_digest(id_material)
            relationship_ids = set()
            path_occurrences = []

            def add_edge(edge_type: str, identity: Any, source: str, target: str) -> None:
                edge_id = f"{edge_type}:{self._stable_digest(identity)}"
                relationship_ids.add(edge_id)
                path_occurrences.append({"from": source, "relationship_id": edge_id, "to": target})

            university = str(row.get("university_code") or "")
            program = str(row.get("program_code") or "")
            if university and program:
                add_edge("offers-program", [university, program], f"University:{university}",
                         f"TrainingProgram:{university}|{program}")
            combinations = row.get("combinations") or []
            if not combinations and row.get("combination_id"):
                combinations = [{"id": row.get("combination_id"),
                                 "required_subject_codes": row.get("required_subject_codes") or [],
                                 "required_aptitude_codes": row.get("required_aptitude_codes") or [],
                                 "acceptance_records": row.get("acceptance_records") or []}]
            for combination in combinations:
                if not isinstance(combination, dict) or not combination.get("id"):
                    continue
                combination_id = str(combination["id"])
                acceptance_records = combination.get("acceptance_records") or []
                for acceptance in acceptance_records:
                    if not isinstance(acceptance, dict) or acceptance.get("admission_year") is None:
                        continue
                    identity = [
                        university, program, combination_id,
                        acceptance.get("admission_year"), acceptance.get("method_codes")]
                    add_edge("accepts-combination", identity, f"TrainingProgram:{university}|{program}",
                             f"AdmissionCombination:{combination_id}")
                for subject_code in combination.get("required_subject_codes") or []:
                    add_edge("requires-subject", [combination_id, subject_code],
                             f"AdmissionCombination:{combination_id}", f"Subject:{subject_code}")
                for aptitude_code in combination.get("required_aptitude_codes") or []:
                    add_edge("requires-aptitude", [combination_id, aptitude_code],
                             f"AdmissionCombination:{combination_id}", f"AptitudeTest:{aptitude_code}")
                for group in combination.get("matching_subject_groups") or []:
                    if not isinstance(group, dict):
                        continue
                    school = str(group.get("high_school_code") or "")
                    year = str(group.get("academic_year") or "")
                    group_code = str(group.get("subject_group_code") or "")
                    if school and year and group_code:
                        group_uid = f"{school}|{year}|{group_code}"
                        add_edge("offers-group", [school, year, group_code], f"HighSchool:{school}",
                                 f"SubjectGroup:{group_uid}")
                        # These subjects came from SCHOOL_GROUPS_QUERY, whose MATCH traverses
                        # actual (:SubjectGroup)-[:INCLUDES_SUBJECT]->(:Subject) relationships.
                        required_subject_codes = {
                            str(code).strip() for code in combination.get("required_subject_codes") or []
                            if isinstance(code, str) and code.strip()
                        }
                        members_by_canonical: Dict[str, set[str]] = {}
                        variant_relationships: Dict[tuple[str, str], bool] = {}
                        for subject in group.get("subjects") or []:
                            observed_code, canonical_code = _subject_member_codes(subject)
                            if observed_code and canonical_code in required_subject_codes:
                                members_by_canonical.setdefault(canonical_code, set()).add(observed_code)
                                variant_relationships[(observed_code, canonical_code)] = (
                                    observed_code == canonical_code
                                    or (isinstance(subject, dict)
                                        and subject.get("variant_relationship_observed") is True)
                                )
                        for canonical_code, observed_codes in sorted(members_by_canonical.items()):
                            # The group membership edge targets the exact Subject node returned
                            # by Neo4j. Canonical aliases require the real directed variant edge
                            # as a second dependency; do not synthesize a direct membership edge.
                            observed_code = canonical_code if canonical_code in observed_codes else sorted(observed_codes)[0]
                            add_edge("includes-subject", [group_uid, observed_code],
                                     f"SubjectGroup:{group_uid}", f"Subject:{observed_code}")
                            if observed_code != canonical_code and variant_relationships.get(
                                (observed_code, canonical_code), False
                            ):
                                add_edge("is-variant-of", [observed_code, canonical_code],
                                         f"Subject:{observed_code}", f"Subject:{canonical_code}")
            row["relationship_ids"] = sorted(relationship_ids)
            row["evidence_path"] = path_occurrences
            validation = validate_evidence_dependencies(row, filters)
            # Canonicalize only identity-consistent repeated references. Conflicting endpoint
            # variants are carried explicitly and can never be mistaken for a valid edge.
            variants: Dict[str, set[tuple[str, str]]] = {}
            for edge in path_occurrences:
                variants.setdefault(edge["relationship_id"], set()).add((edge["from"], edge["to"]))
            canonical_edges = []
            conflicts = []
            for edge_id in sorted(variants):
                endpoints = sorted(variants[edge_id])
                if len(endpoints) == 1:
                    canonical_edges.append({"from": endpoints[0][0], "relationship_id": edge_id,
                                            "to": endpoints[0][1]})
                else:
                    conflicts.append({"relationship_id": edge_id,
                                      "endpoints": [{"from": source, "to": target}
                                                    for source, target in endpoints]})
            row["evidence_path"] = canonical_edges
            row["evidence_path_conflicts"] = conflicts
            row["evidence_dependency_validation"] = validation
            if row.get("answer_scope") == "subject_overlap":
                observed_edges = {
                    (edge.get("relationship_id"), edge.get("from"), edge.get("to"))
                    for edge in canonical_edges
                }
                verified_overlaps = []
                for combination in row.get("combinations") or []:
                    if not isinstance(combination, dict) or not combination.get("id"):
                        continue
                    combination_id = str(combination["id"])
                    required_codes = [str(code).strip() for code in combination.get("required_subject_codes") or []
                                      if isinstance(code, str) and code.strip()]
                    for group in combination.get("matching_subject_groups") or []:
                        if not isinstance(group, dict):
                            continue
                        school = str(group.get("high_school_code") or "").strip()
                        year = str(group.get("academic_year") or "").strip()
                        group_code = str(group.get("subject_group_code") or "").strip()
                        if not school or not year or not group_code:
                            continue
                        group_uid = f"{school}|{year}|{group_code}"
                        for required_code in required_codes:
                            requires_id = "requires-subject:" + self._stable_digest([combination_id, required_code])
                            for subject in group.get("subjects") or []:
                                observed_code, canonical_code = _subject_member_codes(subject)
                                if canonical_code != required_code or not observed_code:
                                    continue
                                if observed_code != required_code and not (
                                    isinstance(subject, dict) and subject.get("variant_relationship_observed") is True
                                ):
                                    continue
                                includes_id = "includes-subject:" + self._stable_digest([group_uid, observed_code])
                                required_edges = [
                                    (requires_id, f"AdmissionCombination:{combination_id}", f"Subject:{required_code}"),
                                    (includes_id, f"SubjectGroup:{group_uid}", f"Subject:{observed_code}"),
                                ]
                                relationship_ids = [requires_id, includes_id]
                                if observed_code != required_code:
                                    variant_id = "is-variant-of:" + self._stable_digest([observed_code, required_code])
                                    required_edges.append((variant_id, f"Subject:{observed_code}", f"Subject:{required_code}"))
                                    relationship_ids.append(variant_id)
                                if all(edge in observed_edges for edge in required_edges):
                                    verified_overlaps.append({
                                        "subject_code": required_code,
                                        "subject_name": next((item.get("name") for item in group.get("subjects") or []
                                                               if isinstance(item, dict)
                                                               and item.get("code") == observed_code), observed_code),
                                        "observed_group_subject_code": observed_code,
                                        "school_code": school, "academic_year": year,
                                        "subject_group_code": group_code,
                                        "program_code": row.get("program_code"),
                                        "university_code": row.get("university_code"),
                                        "relationship_ids": relationship_ids,
                                        "validation_status": "verified",
                                    })
                row["verified_subject_overlaps"] = verified_overlaps
            enriched.append(row)
        return enriched

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
            if rec.get("evidence_id"):
                lines.append(f"Evidence ID: {rec['evidence_id']}")
            dependency_validation = rec.get("evidence_dependency_validation")
            if isinstance(dependency_validation, dict):
                lines.append(
                    "Path dependency validation: "
                    f"{dependency_validation.get('status', 'unknown')}; "
                    f"aptitude coverage: {dependency_validation.get('aptitude_coverage', 'unknown')}"
                )
                missing_edges = dependency_validation.get("missing_relationship_ids") or []
                if missing_edges:
                    lines.append("Missing required path edges: " + ", ".join(missing_edges))
            score_str = f" (Độ tương đồng Vector: {score:.4f})" if score is not None else ""

            lines.append(f"#### {idx}. Ngành: {prog_name} (Mã ngành: {prog_code}){score_str}")
            lines.append(f"- **Trường Đại học:** {univ_name} ({univ_code})")

            if rec.get("answer_scope") == "subject_overlap":
                overlaps = rec.get("verified_subject_overlaps") or []
                if overlaps:
                    lines.append("- Verified subject overlap (scoped evidence only):")
                    for overlap in overlaps:
                        group_code = overlap.get("subject_group_code") or "unknown group"
                        year = overlap.get("academic_year") or "unknown year"
                        subject_name = overlap.get("subject_name") or overlap.get("subject_code")
                        observed_code = overlap.get("observed_group_subject_code")
                        variant_note = (
                            f" (observed group code {observed_code}, linked by the recorded variant edge "
                            f"to admission subject {overlap.get('subject_code')})"
                            if observed_code != overlap.get("subject_code") else
                            f" (subject code {overlap.get('subject_code')})"
                        )
                        lines.append(
                            f"  - {subject_name}{variant_note}; group {group_code}, school "
                            f"{overlap.get('school_code')}, academic year {year}; "
                            f"relationship IDs: {', '.join(overlap.get('relationship_ids') or [])}"
                        )
                    lines.append(
                        "- This verified overlap does not establish complete admission eligibility, "
                        "all mandatory-subject applicability, or satisfaction of other admission conditions."
                    )
                else:
                    lines.append(
                        "- No subject overlap passed directed evidence validation. This is not proof "
                        "that the program has no applicable admission combination."
                    )
                lines.append("")
                continue

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
                for accepted in item.get("acceptance_records") or []:
                    if not isinstance(accepted, dict) or accepted.get("admission_year") is None:
                        continue
                    lines.append(f"  - Offer scope: admission year {accepted.get('admission_year')}; "
                                 f"method codes {accepted.get('method_codes') or 'unknown'}"
                                 + (f"; source {accepted['source_url']}" if accepted.get("source_url") else ""))
                aptitude = item.get("required_aptitude") or []
                if aptitude or item.get("required_aptitude_codes"):
                    aptitude_names = ", ".join(str(row.get("name") or row.get("code")) for row in aptitude
                                                if isinstance(row, dict))
                    if not aptitude_names:
                        aptitude_names = ", ".join(str(code) for code in item.get("required_aptitude_codes", []))
                    lines.append(f"  - Required aptitude evidence: {aptitude_names}; subject-group coverage does not verify this requirement.")
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

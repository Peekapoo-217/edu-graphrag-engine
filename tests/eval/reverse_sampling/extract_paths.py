# -*- coding: utf-8 -*-
"""
tests/eval/reverse_sampling/extract_paths.py

Extract real paths from Neo4j for reverse sampling evaluation.
Connects to Neo4j using centralized config, runs Cypher query to get
complete paths from HighSchool -> SubjectGroup -> Subject <- Combination <- Program <- University.
"""

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import neo4j

# Ensure data-scrawler root directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger


@dataclass
class ExtractedPath:
    """Complete path data from HighSchool to University with all related entities."""
    hs_code: str
    hs_name: str
    sg_uid: str
    sg_code: str
    sg_name: str
    subject_codes: List[str]
    subject_names: List[str]
    combo_id: str
    combo_display: str
    tp_uid: str
    prog_code: str
    prog_name: str
    univ_code: str
    univ_name: str
    cutoffs: List[Dict[str, Any]]
    aptitude_codes: List[str]


def get_neo4j_driver() -> neo4j.Driver:
    """Create and verify Neo4j driver connection using centralized settings."""
    if not settings.NEO4J_PASSWORD:
        raise RuntimeError("NEO4J_PASSWORD environment variable is not set")

    try:
        driver = neo4j.GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD)
        )
        driver.verify_connectivity()
        logger.info(f"Connected to Neo4j database '{settings.NEO4J_DATABASE}' at {settings.NEO4J_URI}")
        return driver
    except Exception as e:
        logger.error(f"Failed to connect to Neo4j: {e}")
        raise RuntimeError(f"Neo4j Connection Error: {e}") from e


def extract_all_paths(driver: neo4j.Driver, database: Optional[str] = None) -> List[ExtractedPath]:
    """
    Extract all valid paths from Neo4j matching the pattern:
    HighSchool -> SubjectGroup -> Subject <- Combination <- Program <- University
    with optional CutoffScore and AptitudeTest.
    """
    db_name = database or settings.NEO4J_DATABASE

    cypher_query = """
    MATCH (hs:HighSchool)-[:OFFERS_GROUP]->(sg:SubjectGroup)-[:INCLUDES_SUBJECT]->(s:Subject)
          <-[:REQUIRES_SUBJECT]-(c:AdmissionCombination)
          <-[:ACCEPTS_COMBINATION]-(tp:TrainingProgram)
          <-[:OFFERS_PROGRAM]-(u:University)
    OPTIONAL MATCH (tp)-[:HAS_CUTOFF]->(cs:CutoffScore)
    OPTIONAL MATCH (c)-[:REQUIRES_APTITUDE]->(at:AptitudeTest)
    WHERE cs.cutoff_score_thpt IS NOT NULL
      AND u.code = 'DQN'
    RETURN 
        hs.code AS hs_code, hs.name AS hs_name,
        sg.uid AS sg_uid, sg.group_code AS sg_code, sg.name AS sg_name,
        collect(DISTINCT s.code) AS subject_codes,
        collect(DISTINCT s.name) AS subject_names,
        c.id AS combo_id, c.display_name AS combo_display,
        tp.uid AS tp_uid, tp.program_code AS prog_code, tp.name AS prog_name,
        u.code AS univ_code, u.name AS univ_name,
        collect(DISTINCT {year: cs.admission_year, score: cs.cutoff_score_thpt, method: cs.method_code, type: cs.score_type}) AS cutoffs,
        collect(DISTINCT at.code) AS aptitude_codes
    """

    paths: List[ExtractedPath] = []
    with driver.session(database=db_name) as session:
        result = session.run(cypher_query)
        for record in result:
            # Filter out None values from collections
            subject_codes = [c for c in record["subject_codes"] if c is not None]
            subject_names = [n for n in record["subject_names"] if n is not None]
            cutoffs = [c for c in record["cutoffs"] if c.get("year") is not None]
            aptitude_codes = [a for a in record["aptitude_codes"] if a is not None]

            path = ExtractedPath(
                hs_code=record["hs_code"] or "",
                hs_name=record["hs_name"] or "",
                sg_uid=record["sg_uid"] or "",
                sg_code=record["sg_code"] or "",
                sg_name=record["sg_name"] or "",
                subject_codes=subject_codes,
                subject_names=subject_names,
                combo_id=record["combo_id"] or "",
                combo_display=record["combo_display"] or "",
                tp_uid=record["tp_uid"] or "",
                prog_code=record["prog_code"] or "",
                prog_name=record["prog_name"] or "",
                univ_code=record["univ_code"] or "",
                univ_name=record["univ_name"] or "",
                cutoffs=cutoffs,
                aptitude_codes=aptitude_codes,
            )
            paths.append(path)

    logger.info(f"Extracted {len(paths)} valid paths from Neo4j")
    return paths


def stratified_sample(paths: List[ExtractedPath], seed: int = 42, per_category: int = 4) -> Dict[str, List[ExtractedPath]]:
    """
    Perform stratified sampling to get balanced representation across categories.
    Categories: factual, multi_hop, rule_check (4 each = 12 total).
    Edge cases are synthetic, not sampled from DB.
    """
    import random
    random.seed(seed)

    # Shuffle paths
    shuffled = paths[:]
    random.shuffle(shuffled)

    # We need at least 12 paths for 3 categories × 4
    if len(shuffled) < 12:
        logger.warning(f"Only {len(shuffled)} paths available, need 12 for stratified sampling. Using all available.")
        per_category = max(1, len(shuffled) // 3)

    sampled = {
        "factual": shuffled[:per_category],
        "multi_hop": shuffled[per_category:per_category * 2],
        "rule_check": shuffled[per_category * 2:per_category * 3],
    }

    for cat, cat_paths in sampled.items():
        logger.info(f"Category '{cat}': sampled {len(cat_paths)} paths")

    return sampled


def get_all_combination_ids(driver: neo4j.Driver, database: Optional[str] = None) -> List[str]:
    """Fetch all valid combination IDs from Neo4j for rule-check wrong combo selection."""
    db_name = database or settings.NEO4J_DATABASE
    query = "MATCH (c:AdmissionCombination) RETURN c.id AS combo_id ORDER BY c.id"

    with driver.session(database=db_name) as session:
        result = session.run(query)
        return [record["combo_id"] for record in result if record["combo_id"]]


def get_program_accepted_combinations(driver: neo4j.Driver, tp_uid: str, database: Optional[str] = None) -> List[str]:
    """Get combination IDs accepted by a specific training program."""
    db_name = database or settings.NEO4J_DATABASE
    query = """
    MATCH (tp:TrainingProgram {uid: $tp_uid})-[:ACCEPTS_COMBINATION]->(c:AdmissionCombination)
    RETURN c.id AS combo_id
    """

    with driver.session(database=db_name) as session:
        result = session.run(query, tp_uid=tp_uid)
        return [record["combo_id"] for record in result]


def main():
    """Standalone test: extract paths and print summary."""
    driver = get_neo4j_driver()
    try:
        paths = extract_all_paths(driver)
        print(f"Total paths extracted: {len(paths)}")
        for i, p in enumerate(paths[:3]):
            print(f"\n--- Path {i+1} ---")
            print(f"  HS: {p.hs_name} ({p.hs_code})")
            print(f"  SG: {p.sg_name} ({p.sg_code})")
            print(f"  Subjects: {p.subject_names} ({p.subject_codes})")
            print(f"  Combo: {p.combo_display} ({p.combo_id})")
            print(f"  Program: {p.prog_name} ({p.prog_code})")
            print(f"  University: {p.univ_name} ({p.univ_code})")
            print(f"  Cutoffs: {p.cutoffs}")
            print(f"  Aptitudes: {p.aptitude_codes}")

        sampled = stratified_sample(paths)
        for cat, cat_paths in sampled.items():
            print(f"\n{cat}: {len(cat_paths)} paths")

    finally:
        driver.close()


if __name__ == "__main__":
    main()
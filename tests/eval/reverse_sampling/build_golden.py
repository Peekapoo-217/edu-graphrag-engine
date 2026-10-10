# -*- coding: utf-8 -*-
"""
tests/eval/reverse_sampling/build_golden.py

Orchestrates the reverse sampling pipeline:
1. Extract real paths from Neo4j
2. Stratified sample across categories (factual, multi_hop, rule_check)
3. Generate natural Vietnamese queries using LLM (with template fallback)
4. Add synthetic edge cases
5. Write 15 records to golden_dataset/queries.jsonl
"""

import os
import sys
import json
import logging
from typing import List, Optional

# Ensure data-scrawler root directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.utils.config import settings
from rag_api.utils.logger import logger
from rag_api.llm.llm_client import RAGGenerator

import neo4j

from .extract_paths import (
    get_neo4j_driver,
    extract_all_paths,
    stratified_sample,
    get_all_combination_ids,
    get_program_accepted_combinations,
)
from .generate_queries import (
    generate_golden_query,
    generate_edge_case_queries,
    GoldenQuery,
)


def select_wrong_combination(
    driver: neo4j.Driver,
    tp_uid: str,
    accepted_combos: List[str],
) -> Optional[str]:
    """Select a random valid combination ID that is NOT accepted by this program."""
    all_combos = get_all_combination_ids(driver)
    if not all_combos:
        return None

    # Filter out accepted combinations
    wrong_combos = [c for c in all_combos if c not in accepted_combos]
    if not wrong_combos:
        logger.warning("All combinations are accepted by this program, cannot select wrong combo")
        return None

    import random
    return random.choice(wrong_combos)


def validate_golden_query(gq: GoldenQuery) -> List[str]:
    """Validate a golden query record. Returns list of error messages (empty if valid)."""
    errors = []

    if not gq.query_id or not gq.query_id.startswith("Q"):
        errors.append(f"Invalid query_id format: {gq.query_id}")

    if not gq.query or not gq.query.strip():
        errors.append("Query text is empty")

    valid_categories = {"factual", "multi_hop", "rule_check", "edge_case"}
    if gq.category not in valid_categories:
        errors.append(f"Invalid category: {gq.category}")

    if not isinstance(gq.filters, dict):
        errors.append("filters must be a dict")

    if not isinstance(gq.expected_programs, list):
        errors.append("expected_programs must be a list")

    if not isinstance(gq.expected_combinations, list):
        errors.append("expected_combinations must be a list")

    # Validate program UID format (univ_code|prog_code)
    for prog in gq.expected_programs:
        if "|" not in prog:
            errors.append(f"Invalid program UID format (expected 'UNIV|PROG'): {prog}")

    # For non-edge-case and non-rule-check, expected_programs should not be empty
    if gq.category not in ("rule_check", "edge_case") and len(gq.expected_programs) == 0:
        errors.append(f"Category '{gq.category}' should have at least one expected program")

    return errors


def write_jsonl(queries: List[GoldenQuery], output_path: str) -> None:
    """Write queries to JSONL file."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for q in queries:
            record = {
                "query_id": q.query_id,
                "query": q.query,
                "category": q.category,
                "filters": q.filters,
                "expected_programs": q.expected_programs,
                "expected_combinations": q.expected_combinations,
                "expected_answer": q.expected_answer,
                "source_path": q.source_path,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    logger.info(f"Written {len(queries)} records to {output_path}")


def build_golden_dataset(
    output_path: str = "tests/eval/golden_dataset/queries.jsonl",
    seed: int = 42,
    per_category: int = 4,
) -> List[GoldenQuery]:
    """
    Main pipeline: extract -> sample -> generate -> validate -> write.
    Returns list of validated GoldenQuery objects.
    """
    driver = get_neo4j_driver()
    generator = RAGGenerator()

    try:
        # Step 1: Extract all valid paths
        logger.info("Step 1/5: Extracting paths from Neo4j...")
        paths = extract_all_paths(driver)

        if len(paths) == 0:
            raise RuntimeError("No valid paths found in Neo4j. Check database content and Cypher query.")

        if len(paths) < per_category * 3:
            logger.warning(f"Only {len(paths)} paths available, reducing per_category from {per_category} to {max(1, len(paths)//3)}")
            per_category = max(1, len(paths) // 3)

        # Step 2: Stratified sampling
        logger.info("Step 2/5: Stratified sampling...")
        sampled = stratified_sample(paths, seed=seed, per_category=per_category)

        # Step 3: Generate queries for each category
        logger.info("Step 3/5: Generating queries with LLM (fallback to templates)...")
        all_queries: List[GoldenQuery] = []
        query_counter = 1

        categories = ["factual", "multi_hop", "rule_check"]
        for category in categories:
            cat_paths = sampled.get(category, [])
            for i, path in enumerate(cat_paths):
                # For rule_check, get a wrong combination
                wrong_combo = None
                if category == "rule_check":
                    accepted = get_program_accepted_combinations(driver, path.tp_uid)
                    wrong_combo = select_wrong_combination(driver, path.tp_uid, accepted)
                    if wrong_combo:
                        logger.info(f"Rule-check: using wrong combo '{wrong_combo}' for {path.prog_name}")

                gq = generate_golden_query(
                    path=path,
                    category=category,
                    query_id=f"Q{query_counter:03d}",
                    generator=generator,
                    template_index=i,
                    wrong_combo=wrong_combo,
                )

                # Validate
                errors = validate_golden_query(gq)
                if errors:
                    logger.error(f"Validation failed for {gq.query_id}: {errors}")
                    raise ValueError(f"Invalid golden query {gq.query_id}: {errors}")

                all_queries.append(gq)
                query_counter += 1
                logger.info(f"Generated {gq.query_id} [{gq.category}]: {gq.query[:80]}...")

        # Step 4: Add edge cases
        logger.info("Step 4/5: Adding synthetic edge cases...")
        if paths:
            edge_queries = generate_edge_case_queries(paths[0], query_counter)
            for eq in edge_queries:
                errors = validate_golden_query(eq)
                if errors:
                    raise ValueError(f"Invalid edge case query {eq.query_id}: {errors}")
                all_queries.append(eq)
                logger.info(f"Generated {eq.query_id} [{eq.category}]: {eq.query[:80]}...")

        # Step 5: Write output
        logger.info("Step 5/5: Writing JSONL output...")
        write_jsonl(all_queries, output_path)

        logger.info(f"Successfully built golden dataset with {len(all_queries)} queries")
        return all_queries

    finally:
        driver.close()


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Build golden dataset for retrieval evaluation")
    parser.add_argument(
        "--output",
        default="tests/eval/golden_dataset/queries.jsonl",
        help="Output JSONL file path"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for stratified sampling"
    )
    parser.add_argument(
        "--per-category",
        type=int,
        default=4,
        help="Number of samples per category (factual, multi_hop, rule_check)"
    )

    args = parser.parse_args()

    print("=== BUILDING GOLDEN DATASET ===")
    print(f"Output: {args.output}")
    print(f"Seed: {args.seed}")
    print(f"Per category: {args.per_category}")

    try:
        queries = build_golden_dataset(
            output_path=args.output,
            seed=args.seed,
            per_category=args.per_category,
        )

        # Print summary
        print(f"\n✅ Successfully generated {len(queries)} queries")
        for cat in ["factual", "multi_hop", "rule_check", "edge_case"]:
            count = sum(1 for q in queries if q.category == cat)
            print(f"  {cat}: {count}")

        # Show all queries
        print("\n--- ALL QUERIES ---")
        for q in queries:
            print(f"{q.query_id} [{q.category}]: {q.query}")
            print(f"  Filters: {q.filters}")
            print(f"  Expected programs: {q.expected_programs}")
            print(f"  Expected combos: {q.expected_combinations}")
            print()

    except Exception as e:
        logger.error(f"Build failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
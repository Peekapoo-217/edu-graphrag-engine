# -*- coding: utf-8 -*-
"""
tests/eval/retrieval_eval.py

Retrieval evaluation metrics computation for Hybrid GraphRAG.
Reads golden dataset JSONL, runs retriever, computes Hit Rate@k, Recall@k, MRR.
Handles edge cases: empty expected_programs with empty retrieved = Hit@k = 1.0
"""

import os
import sys
import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass

# Ensure data-scrawler root directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.retrieval.graph_retriever import Neo4jRetriever
from rag_api.utils.logger import logger


@dataclass
class QueryMetrics:
    """Metrics for a single query."""
    query_id: str
    category: str
    hit_at_k: Dict[int, float]
    recall_at_k: Dict[int, float]
    mrr: float
    retrieved_programs: List[str]
    expected_programs: List[str]


@dataclass
class CategorySummary:
    """Aggregated metrics for a category."""
    category: str
    count: int
    hit_at_k: Dict[int, float]
    recall_at_k: Dict[int, float]
    mrr: float


@dataclass
class EvaluationResult:
    """Complete evaluation results."""
    per_query: List[QueryMetrics]
    per_category: List[CategorySummary]
    overall: CategorySummary


def load_golden_dataset(dataset_path: str) -> List[Dict[str, Any]]:
    """Load golden dataset from JSONL file."""
    queries = []
    with open(dataset_path, "r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                queries.append(json.loads(line))
            except json.JSONDecodeError as e:
                logger.warning(f"Line {line_num}: Invalid JSON - {e}")
    logger.info(f"Loaded {len(queries)} queries from {dataset_path}")
    return queries


def extract_retrieved_programs(records: List[Dict[str, Any]]) -> List[str]:
    """Extract program UIDs (univ_code|prog_code) from retriever records."""
    programs = []
    for r in records:
        univ_code = r.get("university_code")
        prog_code = r.get("program_code")
        if univ_code and prog_code:
            programs.append(f"{univ_code}|{prog_code}")
        else:
            logger.debug(f"Record missing univ_code or prog_code: {r}")
    return programs


def compute_query_metrics(
    query_id: str,
    category: str,
    retrieved: List[str],
    expected: List[str],
    top_k_values: List[int],
) -> QueryMetrics:
    """Compute metrics for a single query."""
    hit_at_k = {}
    recall_at_k = {}

    # Edge case: expected is empty
    if len(expected) == 0:
        # If retriever also returns empty, it's correct (Hit=1.0, Recall=1.0, MRR=1.0)
        # If retriever returns non-empty, it's incorrect (Hit=0.0, Recall=0.0, MRR=0.0)
        is_correct = len(retrieved) == 0
        for k in top_k_values:
            hit_at_k[k] = 1.0 if is_correct else 0.0
            recall_at_k[k] = 1.0 if is_correct else 0.0
        mrr = 1.0 if is_correct else 0.0
    else:
        # Normal case: expected is not empty
        for k in top_k_values:
            top_k = retrieved[:k]
            # Hit Rate@k: 1 if any expected in top_k
            hit = 1.0 if any(p in top_k for p in expected) else 0.0
            hit_at_k[k] = hit

            # Recall@k: |top_k ∩ expected| / |expected|
            intersection = len(set(top_k) & set(expected))
            recall = intersection / len(expected) if expected else 0.0
            recall_at_k[k] = recall

        # MRR: 1 / rank of first hit
        mrr = 0.0
        for i, prog in enumerate(retrieved, 1):
            if prog in expected:
                mrr = 1.0 / i
                break

    return QueryMetrics(
        query_id=query_id,
        category=category,
        hit_at_k=hit_at_k,
        recall_at_k=recall_at_k,
        mrr=mrr,
        retrieved_programs=retrieved,
        expected_programs=expected,
    )


def aggregate_by_category(metrics: List[QueryMetrics], top_k_values: List[int]) -> List[CategorySummary]:
    """Aggregate metrics by category."""
    from collections import defaultdict

    cat_data = defaultdict(list)
    for m in metrics:
        cat_data[m.category].append(m)

    summaries = []
    for cat, cat_metrics in sorted(cat_data.items()):
        count = len(cat_metrics)
        hit_at_k = {}
        recall_at_k = {}

        for k in top_k_values:
            hit_at_k[k] = sum(m.hit_at_k.get(k, 0) for m in cat_metrics) / count
            recall_at_k[k] = sum(m.recall_at_k.get(k, 0) for m in cat_metrics) / count

        mrr = sum(m.mrr for m in cat_metrics) / count

        summaries.append(CategorySummary(
            category=cat,
            count=count,
            hit_at_k=hit_at_k,
            recall_at_k=recall_at_k,
            mrr=mrr,
        ))

    return summaries


def compute_overall(metrics: List[QueryMetrics], top_k_values: List[int]) -> CategorySummary:
    """Compute overall metrics across all queries."""
    count = len(metrics)
    hit_at_k = {}
    recall_at_k = {}

    for k in top_k_values:
        hit_at_k[k] = sum(m.hit_at_k.get(k, 0) for m in metrics) / count
        recall_at_k[k] = sum(m.recall_at_k.get(k, 0) for m in metrics) / count

    mrr = sum(m.mrr for m in metrics) / count

    return CategorySummary(
        category="OVERALL",
        count=count,
        hit_at_k=hit_at_k,
        recall_at_k=recall_at_k,
        mrr=mrr,
    )


def format_results_table(result: EvaluationResult, top_k_values: List[int]) -> str:
    """Format evaluation results as a pretty table."""
    lines = []
    lines.append("╔════════════════════════════════════════════════════════════════════════╗")
    lines.append("║                    RETRIEVAL EVALUATION RESULTS                       ║")
    lines.append("╠═══════════════╦═══════╦══════════╦══════════╦══════════╦═════════════╣")
    header = "║ Category      ║ Count ║"
    for k in top_k_values:
        header += f" Hit@{k}    ║"
    header += " Recall@5 ║ MRR      ║"
    lines.append(header)
    lines.append("╠═══════════════╬═══════╬══════════╬══════════╬══════════╬═════════════╣")

    # Per category
    for cat_sum in result.per_category:
        row = f"║ {cat_sum.category:<13} ║ {cat_sum.count:>5} ║"
        for k in top_k_values:
            row += f" {cat_sum.hit_at_k.get(k, 0):.2f}     ║"
        row += f" {cat_sum.recall_at_k.get(5, 0):.2f}     ║ {cat_sum.mrr:.2f}      ║"
        lines.append(row)

    lines.append("╠═══════════════╬═══════╬══════════╬══════════╬══════════╬═════════════╣")

    # Overall
    overall = result.overall
    row = f"║ {overall.category:<13} ║ {overall.count:>5} ║"
    for k in top_k_values:
        row += f" {overall.hit_at_k.get(k, 0):.2f}     ║"
    row += f" {overall.recall_at_k.get(5, 0):.2f}     ║ {overall.mrr:.2f}      ║"
    lines.append(row)

    lines.append("╚═══════════════╩═══════╩══════════╩══════════╩══════════╩═════════════╝")

    return "\n".join(lines)


def evaluate_retrieval(
    retriever: Neo4jRetriever,
    dataset_path: str,
    top_k_values: List[int] = None,
    score_threshold: Optional[float] = None,
) -> EvaluationResult:
    """
    Main evaluation function.
    Runs retriever on each query in golden dataset and computes metrics.
    """
    if top_k_values is None:
        top_k_values = [3, 5]

    # Load dataset
    golden_queries = load_golden_dataset(dataset_path)

    per_query_metrics = []

    for q in golden_queries:
        query_id = q.get("query_id", "UNKNOWN")
        category = q.get("category", "unknown")
        filters = q.get("filters", {})
        expected_programs = q.get("expected_programs", [])

        logger.info(f"Evaluating {query_id} [{category}]: filters={filters}")

        try:
            # Run retriever with max top_k
            max_k = max(top_k_values)
            records = retriever.retrieve_context(filters=filters, top_k=max_k, score_threshold=score_threshold)
            retrieved_programs = extract_retrieved_programs(records)

            logger.debug(f"  Retrieved: {retrieved_programs}")
            logger.debug(f"  Expected:  {expected_programs}")

            # Compute metrics
            qm = compute_query_metrics(
                query_id=query_id,
                category=category,
                retrieved=retrieved_programs,
                expected=expected_programs,
                top_k_values=top_k_values,
            )
            per_query_metrics.append(qm)

        except Exception as e:
            logger.error(f"Error evaluating {query_id}: {e}")
            # Create zero metrics for failed query
            hit_at_k = {k: 0.0 for k in top_k_values}
            recall_at_k = {k: 0.0 for k in top_k_values}
            qm = QueryMetrics(
                query_id=query_id,
                category=category,
                hit_at_k=hit_at_k,
                recall_at_k=recall_at_k,
                mrr=0.0,
                retrieved_programs=[],
                expected_programs=expected_programs,
            )
            per_query_metrics.append(qm)

    # Aggregate
    per_category = aggregate_by_category(per_query_metrics, top_k_values)
    overall = compute_overall(per_query_metrics, top_k_values)

    return EvaluationResult(
        per_query=per_query_metrics,
        per_category=per_category,
        overall=overall,
    )


def main():
    """Standalone test."""
    from rag_api.utils.config import settings

    # Quick test with mock data
    print("=== Retrieval Evaluation Module ===")
    print("Run via: python -m tests.eval.runner")


if __name__ == "__main__":
    main()
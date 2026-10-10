#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/eval/runner.py

CLI entry point for retrieval evaluation.
Usage: python -m tests.eval.runner [--top-k 3 5] [--dataset PATH] [--seed 42]
"""

import argparse
import sys
import os

# Ensure data-scrawler root directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.retrieval.graph_retriever import Neo4jRetriever
from rag_api.utils.logger import logger
from .retrieval_eval import evaluate_retrieval, format_results_table


def main():
    parser = argparse.ArgumentParser(
        description="Run retrieval evaluation on golden dataset",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--top-k",
        nargs="+",
        type=int,
        default=[3, 5],
        help="Top-k values for Hit Rate and Recall computation"
    )
    parser.add_argument(
        "--dataset",
        default="tests/eval/golden_dataset/queries.jsonl",
        help="Path to golden dataset JSONL file"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (used for golden dataset generation, not evaluation)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--score-threshold",
        type=float,
        default=None,
        help="Minimum similarity score for vector search (default from settings.VECTOR_SIMILARITY_THRESHOLD = 0.65)"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel("DEBUG")

    # Resolve dataset path relative to data-scrawler root
    if not os.path.isabs(args.dataset):
        dataset_path = os.path.join(BASE_DIR, args.dataset)
    else:
        dataset_path = args.dataset

    if not os.path.exists(dataset_path):
        logger.error(f"Dataset not found: {dataset_path}")
        logger.error("Run 'python -m tests.eval.reverse_sampling.build_golden' first to generate it.")
        sys.exit(1)

    logger.info(f"Loading dataset: {dataset_path}")
    logger.info(f"Top-k values: {args.top_k}")
    if args.score_threshold is not None:
        logger.info(f"Score threshold: {args.score_threshold}")

    retriever = Neo4jRetriever()
    try:
        logger.info("Running retrieval evaluation...")
        result = evaluate_retrieval(
            retriever=retriever,
            dataset_path=dataset_path,
            top_k_values=args.top_k,
            score_threshold=args.score_threshold,
        )

        # Print results table
        print("\n")
        print(format_results_table(result, args.top_k))
        print("\n")

        # Print per-query details for failures
        failed_queries = [qm for qm in result.per_query if qm.mrr == 0.0]
        if failed_queries:
            print(f"=== FAILED QUERIES ({len(failed_queries)}) ===")
            for qm in failed_queries:
                print(f"  {qm.query_id} [{qm.category}]: Retrieved={qm.retrieved_programs}, Expected={qm.expected_programs}")
            print()

        # Exit with error code if overall MRR is too low (optional threshold)
        if result.overall.mrr < 0.3:
            logger.warning(f"Overall MRR ({result.overall.mrr:.2f}) is below 0.3 threshold")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Evaluation failed: {e}")
        sys.exit(1)
    finally:
        retriever.close()

    logger.info("Evaluation completed successfully")
    sys.exit(0)


if __name__ == "__main__":
    main()
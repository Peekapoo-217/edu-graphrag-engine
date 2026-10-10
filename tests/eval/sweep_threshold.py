#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/eval/sweep_threshold.py

Empirical threshold sweep for vector similarity score.
Scans tau in [0.50, 0.85] with step 0.02, computes TPR, TNR, F1, and Accuracy
on the golden dataset to find optimal threshold.
"""

import os
import sys
import json
import argparse
from typing import List, Dict, Any, Tuple

# Ensure data-scrawler root directory is in sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from rag_api.retrieval.graph_retriever import Neo4jRetriever
from rag_api.utils.logger import logger
from rag_api.utils.config import settings


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
    return programs


def evaluate_at_threshold(
    retriever: Neo4jRetriever,
    queries: List[Dict[str, Any]],
    threshold: float,
    top_k: int = 5
) -> Tuple[float, float, float, float]:
    """
    Evaluate retriever at a specific similarity threshold.
    
    Returns:
        (tpr, tnr, f1, accuracy)
    """
    # Categorize queries
    in_domain_queries = [q for q in queries if q.get("category") in ("factual", "multi_hop")]
    out_domain_queries = [q for q in queries if q.get("category") in ("edge_case", "rule_check")]
    
    # True Positive Rate (Recall for in-domain): at least 1 expected program in retrieved
    tp = 0
    for q in in_domain_queries:
        expected = q.get("expected_programs", [])
        if not expected:
            continue
        filters = q.get("filters", {})
        records = retriever.retrieve_context(filters=filters, top_k=top_k, score_threshold=threshold)
        retrieved = extract_retrieved_programs(records)
        if any(p in retrieved for p in expected):
            tp += 1
    tpr = tp / len(in_domain_queries) if in_domain_queries else 0.0
    
    # True Negative Rate (Specificity for out-domain): correctly returns empty
    tn = 0
    for q in out_domain_queries:
        filters = q.get("filters", {})
        records = retriever.retrieve_context(filters=filters, top_k=top_k, score_threshold=threshold)
        retrieved = extract_retrieved_programs(records)
        if len(retrieved) == 0:
            tn += 1
    tnr = tn / len(out_domain_queries) if out_domain_queries else 0.0
    
    # F1 Score
    if tpr + tnr > 0:
        f1 = 2 * tpr * tnr / (tpr + tnr)
    else:
        f1 = 0.0
    
    # Overall Accuracy
    total_correct = tp + tn
    total_queries = len(in_domain_queries) + len(out_domain_queries)
    accuracy = total_correct / total_queries if total_queries > 0 else 0.0
    
    return tpr, tnr, f1, accuracy


def print_results_table(results: List[Dict[str, Any]]) -> None:
    """Print sweep results as a formatted table."""
    try:
        from tabulate import tabulate
        headers = ["Threshold (τ)", "TPR (In-Domain Recall)", "TNR (Out-Domain Specificity)", "F1-Score", "Accuracy"]
        table_data = [
            [f"{r['threshold']:.2f}", f"{r['tpr']:.4f}", f"{r['tnr']:.4f}", f"{r['f1']:.4f}", f"{r['accuracy']:.4f}"]
            for r in results
        ]
        print(tabulate(table_data, headers=headers, tablefmt="grid"))
    except ImportError:
        # Fallback without tabulate
        print(f"{'Threshold':>12} | {'TPR':>10} | {'TNR':>10} | {'F1':>8} | {'Acc':>8}")
        print("-" * 58)
        for r in results:
            print(f"{r['threshold']:>12.2f} | {r['tpr']:>10.4f} | {r['tnr']:>10.4f} | {r['f1']:>8.4f} | {r['accuracy']:>8.4f}")


def find_optimal_threshold(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Find threshold with maximum F1-score."""
    if not results:
        return {}
    best = max(results, key=lambda x: x['f1'])
    return best


def main():
    parser = argparse.ArgumentParser(
        description="Sweep vector similarity threshold to find optimal value",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument(
        "--dataset",
        default="tests/eval/golden_dataset/queries.jsonl",
        help="Path to golden dataset JSONL file"
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="Top-k for retrieval"
    )
    parser.add_argument(
        "--min-threshold",
        type=float,
        default=0.50,
        help="Minimum threshold to sweep"
    )
    parser.add_argument(
        "--max-threshold",
        type=float,
        default=0.85,
        help="Maximum threshold to sweep"
    )
    parser.add_argument(
        "--step",
        type=float,
        default=0.02,
        help="Step size for threshold sweep"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional JSON output file for results"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel("DEBUG")

    # Resolve dataset path
    if not os.path.isabs(args.dataset):
        dataset_path = os.path.join(BASE_DIR, args.dataset)
    else:
        dataset_path = args.dataset

    if not os.path.exists(dataset_path):
        logger.error(f"Dataset not found: {dataset_path}")
        logger.error("Run 'python -m tests.eval.reverse_sampling.build_golden' first to generate it.")
        sys.exit(1)

    logger.info(f"Loading dataset: {dataset_path}")
    logger.info(f"Sweeping threshold: {args.min_threshold} to {args.max_threshold} step {args.step}")

    # Load dataset
    queries = load_golden_dataset(dataset_path)

    # Initialize retriever
    retriever = Neo4jRetriever()

    try:
        results = []
        threshold = args.min_threshold
        while threshold <= args.max_threshold + 1e-9:  # epsilon for float comparison
            logger.info(f"Evaluating threshold τ = {threshold:.2f}...")
            tpr, tnr, f1, accuracy = evaluate_at_threshold(
                retriever=retriever,
                queries=queries,
                threshold=threshold,
                top_k=args.top_k
            )
            results.append({
                "threshold": threshold,
                "tpr": tpr,
                "tnr": tnr,
                "f1": f1,
                "accuracy": accuracy
            })
            threshold += args.step

        # Print results
        print("\n=== THRESHOLD SWEEP RESULTS ===")
        print(f"Dataset: {dataset_path}")
        print(f"Top-K: {args.top_k}")
        print(f"Range: [{args.min_threshold:.2f}, {args.max_threshold:.2f}] step {args.step:.2f}")
        print()
        print_results_table(results)

        # Find and print optimal
        best = find_optimal_threshold(results)
        if best:
            print("\n=== OPTIMAL THRESHOLD ===")
            print(f"τ* = {best['threshold']:.2f}")
            print(f"TPR (In-Domain Recall):     {best['tpr']:.4f}")
            print(f"TNR (Out-Domain Specificity): {best['tnr']:.4f}")
            print(f"F1-Score (Harmonic Mean):    {best['f1']:.4f}")
            print(f"Overall Accuracy:           {best['accuracy']:.4f}")

            # Also show top 3
            sorted_results = sorted(results, key=lambda x: x['f1'], reverse=True)
            print("\n=== TOP 3 THRESHOLDS ===")
            for i, r in enumerate(sorted_results[:3], 1):
                print(f"  {i}. τ = {r['threshold']:.2f} | F1 = {r['f1']:.4f} | TPR = {r['tpr']:.4f} | TNR = {r['tnr']:.4f}")

        # Optional JSON output
        if args.output:
            output_path = os.path.join(BASE_DIR, args.output) if not os.path.isabs(args.output) else args.output
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sweep_config": {
                        "min_threshold": args.min_threshold,
                        "max_threshold": args.max_threshold,
                        "step": args.step,
                        "top_k": args.top_k,
                        "dataset": dataset_path
                    },
                    "results": results,
                    "optimal": best
                }, f, ensure_ascii=False, indent=2)
            logger.info(f"Results saved to {output_path}")

    except Exception as e:
        logger.error(f"Sweep failed: {e}")
        sys.exit(1)
    finally:
        retriever.close()

    logger.info("Threshold sweep completed successfully")
    sys.exit(0)


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_postgres_to_neo4j.py

POC Data Synchronization module from PostgreSQL (edu_guidance) to Neo4j (edugraph).
- Read-only PostgreSQL extraction
- Comprehensive graph model data validation (--dry-run)
- Parameterized Cypher UNWIND MERGE sync
- Post-sync verification (--verify)
"""

import argparse
import os
import sys
from typing import Dict, Any

from graph_sync.postgres_reader import (
    fetch_all_graph_data_from_postgres,
    get_postgres_connection,
    verify_postgres_schema,
)
from graph_sync.validators import validate_graph_data
from graph_sync.neo4j_writer import (
    get_neo4j_driver,
    sync_nodes_and_relationships,
    verify_graph_statistics,
    verify_neo4j_constraints,
)
from graph_sync.projection_lock import graph_projection_lock

def load_env_file(env_path: str = ".env"):
    """Load environment variables from .env file into os.environ if present."""
    if os.path.exists(env_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
        except ImportError:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, val = line.split("=", 1)
                        os.environ.setdefault(key.strip(), val.strip().strip('"\''))


def read_and_validate_graph_data(database_url: str) -> Dict[str, Any]:
    print("[1/5] Connecting to PostgreSQL (Read-Only mode)...")
    try:
        pg_conn = get_postgres_connection(database_url)
    except Exception as error:
        raise RuntimeError(f"Failed to connect to PostgreSQL: {error}") from error

    try:
        print("[2/5] Inspecting PostgreSQL schema via information_schema...")
        valid_schema, schema_errors = verify_postgres_schema(pg_conn)
        if not valid_schema:
            raise ValueError("PostgreSQL schema validation failed: " + "; ".join(schema_errors))

        print("[3/5] Extracting and normalizing PostgreSQL data...")
        graph_data = fetch_all_graph_data_from_postgres(pg_conn)
    finally:
        pg_conn.close()

    print("[4/5] Running graph data validation & integrity checks...")
    is_valid, validation_errors = validate_graph_data(graph_data)
    if not is_valid:
        raise ValueError("Graph data validation failed: " + "; ".join(validation_errors))
    return graph_data


def main():
    load_env_file()
    parser = argparse.ArgumentParser(description="Synchronize PostgreSQL data to Neo4j Knowledge Graph.")
    parser.add_argument("--dry-run", action="store_true", help="Perform extraction & validation without writing to Neo4j.")
    parser.add_argument("--verify", action="store_true", help="Execute Neo4j verification queries and print graph stats.")
    parser.add_argument("--batch-size", type=int, default=500, help="Batch size for UNWIND MERGE execution (default: 500).")
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL")
    neo4j_uri = os.environ.get("NEO4J_URI", "neo4j://127.0.0.1:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.environ.get("NEO4J_PASSWORD")
    neo4j_database = os.environ.get("NEO4J_DATABASE", "edugraph")

    # If --verify mode requested directly without dry-run extraction
    if args.verify and not args.dry_run and not database_url:
        print("[INFO] Running standalone --verify mode on Neo4j...")
        if not neo4j_password:
            print("[ERROR] Missing required environment variable: NEO4J_PASSWORD", file=sys.stderr)
            sys.exit(1)
        
        try:
            driver = get_neo4j_driver(neo4j_uri, (neo4j_user, neo4j_password))
            stats = verify_graph_statistics(driver, neo4j_database)
            driver.close()
            print_verification_report(stats)
            sys.exit(0)
        except Exception as e:
            print(f"[ERROR] Failed during Neo4j verification: {e}", file=sys.stderr)
            sys.exit(1)

    # Validate mandatory PostgreSQL environment variable
    if not database_url:
        print("[ERROR] Missing required environment variable: DATABASE_URL", file=sys.stderr)
        print("Example: $env:DATABASE_URL=\"postgresql://user:password@localhost:5432/edu_guidance\"", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        try:
            graph_data = read_and_validate_graph_data(database_url)
        except Exception as error:
            print(f"[ERROR] {error}", file=sys.stderr)
            sys.exit(1)
        print("\n=== DRY RUN VALIDATION SUCCESSFUL ===")
        print_dry_run_summary(graph_data, is_valid=True)
        return

    # Real Sync Mode
    if not neo4j_password:
        print("[ERROR] Missing required environment variable: NEO4J_PASSWORD", file=sys.stderr)
        sys.exit(1)

    print("[5/5] Connecting to Neo4j and verifying constraints...")
    try:
        driver = get_neo4j_driver(neo4j_uri, (neo4j_user, neo4j_password))
    except Exception as e:
        print(f"[ERROR] Failed to connect to Neo4j: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        valid_constraints, missing_constraints = verify_neo4j_constraints(driver, neo4j_database)
        if not valid_constraints:
            print("[ERROR] Missing Neo4j Constraints:", file=sys.stderr)
            for mc in missing_constraints:
                print(f"  - {mc}", file=sys.stderr)
            sys.exit(1)

        with graph_projection_lock(database_url):
            # Read source state only after taking the same lock as lifecycle jobs.
            graph_data = read_and_validate_graph_data(database_url)
            print(f"[*] Synchronizing data to Neo4j (database: '{neo4j_database}', batch_size: {args.batch_size})...")
            sync_nodes_and_relationships(driver, neo4j_database, graph_data, batch_size=args.batch_size)
            print("[SUCCESS] Data synchronization completed successfully.")

            print("\n[*] Running post-sync verification stats...")
            stats = verify_graph_statistics(driver, neo4j_database)
            print_verification_report(stats)

            check_reference_counts(stats, graph_data)
    except Exception as e:
        print(f"[ERROR] Synchronization failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        driver.close()


def print_dry_run_summary(graph_data: Dict[str, Any], is_valid: bool):
    nodes = graph_data["nodes"]
    rels = graph_data["relationships"]

    print("\n--- Summary of Extracted Node Counts ---")
    for label, items in nodes.items():
        print(f"  {label:<22}: {len(items):>5}")

    print(f"  {'SubjectGroup deletions':<22}: {len(graph_data.get('unpublished_subject_groups', [])):>5}")

    print("\n--- Summary of Extracted Relationship Counts ---")
    for rel_type, items in rels.items():
        print(f"  {rel_type:<22}: {len(items):>5}")

    status_str = "PASSED" if is_valid else "FAILED"
    print(f"\nData Validation Status: {status_str}")


def print_verification_report(stats: Dict[str, Any]):
    print("\n================ NEO4J GRAPH VERIFICATION REPORT ================")
    print("Node Labels & Totals:")
    for label, count in stats.get("nodes", {}).items():
        print(f"  {label:<22}: {count:>5}")

    print("\nRelationship Types & Totals:")
    for rel_type, count in stats.get("relationships", {}).items():
        print(f"  {rel_type:<22}: {count:>5}")

    print("\nOrphan Node Diagnostics:")
    orphans = stats.get("orphans", {})
    all_zero = True
    for key, count in orphans.items():
        symbol = "[OK]" if count == 0 else "[WARNING]"
        print(f"  {symbol} {key:<40}: {count:>5}")
        if count != 0:
            all_zero = False
    if all_zero:
        print("  --> No orphan nodes detected in graph!")
    print("=================================================================\n")


def check_reference_counts(stats: Dict[str, Any], graph_data: Dict[str, Any]):
    actual_nodes = stats.get("nodes", {})
    actual_rels = stats.get("relationships", {})

    mismatch = False
    for label, rows in graph_data["nodes"].items():
        ref_cnt = len(rows)
        act_cnt = actual_nodes.get(label, 0)
        if act_cnt != ref_cnt:
            print(f"[WARNING] Reference node count mismatch for '{label}': expected {ref_cnt}, found {act_cnt}")
            mismatch = True

    for rel_type, rows in graph_data["relationships"].items():
        ref_cnt = len(rows)
        act_cnt = actual_rels.get(rel_type, 0)
        if act_cnt != ref_cnt:
            print(f"[WARNING] Projected relationship count mismatch for '{rel_type}': expected {ref_cnt}, found {act_cnt}")
            mismatch = True

    if not mismatch:
        print("[INFO] Neo4j counts match the PostgreSQL projection.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_to_postgres.py

Multi-university PostgreSQL Data Importer for Admissions Offers and Cutoff Scores.
Performs data validation, DB schema inspection via information_schema,
and idempotent upsert transactions.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from crawlers.common.validators import (
    validate_admissions_csv,
    validate_cutoffs_csv,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config", "universities.json")


def load_university_config(univ_code: str) -> Dict[str, Any]:
    """Load metadata for a given university from config/universities.json."""
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        configs = json.load(f)

    code = univ_code.upper()
    if code not in configs:
        raise ValueError(
            f"University code '{code}' not found in {CONFIG_PATH}. "
            f"Configured codes: {list(configs.keys())}"
        )
    return configs[code]


def verify_db_schema(conn) -> Tuple[bool, List[str]]:
    """
    Inspect PostgreSQL database using information_schema to verify
    required tables and columns exist.
    """
    required_schema = {
        "universities": ["university_code", "university_name", "website"],
        "admission_offers": [
            "university_code", "admission_year", "program_code", "program_name",
            "method_codes", "quota", "combination_id", "combination_display",
            "canonical_combination_id", "subjects_json", "source_url", "retrieved_at"
        ],
        "cutoff_scores": [
            "university_code", "admission_year", "program_code", "program_name",
            "method_code", "score_type", "cutoff_score_thpt", "cutoff_score_sat"
        ],
        "subjects": ["code"],
        "admission_combinations": [
            "combination_id", "combination_display", "component_count"
        ],
        "admission_combination_components": [
            "combination_id", "component_code", "component_name",
            "component_type", "subject_code", "weight"
        ],
    }

    missing_errors = []
    with conn.cursor() as cur:
        # Check tables
        for table_name, req_cols in required_schema.items():
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s);",
                (table_name,)
            )
            table_exists = cur.fetchone()[0]
            if not table_exists:
                missing_errors.append(f"Missing required DB table: '{table_name}'")
                continue

            # Check columns
            for col_name in req_cols:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s AND column_name = %s);",
                    (table_name, col_name)
                )
                col_exists = cur.fetchone()[0]
                if not col_exists:
                    missing_errors.append(f"Missing required column '{col_name}' in table '{table_name}'")

    return len(missing_errors) == 0, missing_errors


def fetch_existing_db_subjects(conn) -> Set[str]:
    """Query set of existing subject codes in DB subjects table."""
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM subjects;")
        rows = cur.fetchall()
        return {r[0] for r in rows}


def import_to_postgres(
    db_url: str,
    univ_meta: Dict[str, Any],
    admissions_data: List[Dict[str, Any]],
    cutoffs_data: Optional[List[Dict[str, Any]]] = None
) -> None:
    """Connect to PostgreSQL and perform upserts within a single transaction."""
    try:
        import psycopg
        from psycopg.types.json import Jsonb
    except ImportError:
        print("[ERROR] psycopg version 3 is required. Run: pip install 'psycopg[binary]'", file=sys.stderr)
        sys.exit(1)

    print(f"[DB] Connecting to PostgreSQL database...")
    with psycopg.connect(db_url) as conn:
        # 1. Verify schema existence
        schema_ok, schema_errors = verify_db_schema(conn)
        if not schema_ok:
            print("[ABORT] Database schema validation failed:", file=sys.stderr)
            for err in schema_errors:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)
        print("[DB] Database schema verification passed.")

        # 2. Check DB subjects for SUBJECT components
        db_subjects = fetch_existing_db_subjects(conn)
        missing_db_subjects = set()

        for offer in admissions_data:
            items = offer.get("subjects_items", [])
            for item in items:
                if item.get("component_type") == "SUBJECT":
                    s_code = item.get("subject_code")
                    if s_code not in db_subjects:
                        missing_db_subjects.add(s_code)

        if missing_db_subjects:
            print("[ABORT] The following SUBJECT components do not exist in database 'subjects' table:", file=sys.stderr)
            for s_code in sorted(missing_db_subjects):
                print(f"  - {s_code}", file=sys.stderr)
            print("Please add missing subjects to DB 'subjects' table manually before import.", file=sys.stderr)
            sys.exit(1)

        # 3. Transaction for Upserts
        with conn.transaction():
            with conn.cursor() as cur:
                # A. Upsert University Metadata
                univ_code = univ_meta["university_code"]
                univ_name = univ_meta["university_name"]
                website = univ_meta.get("website", "")
                print(f"[DB] Upserting university '{univ_code}' ({univ_name})...")
                cur.execute(
                    """
                    INSERT INTO universities (university_code, university_name, website, updated_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (university_code)
                    DO UPDATE SET
                        university_name = EXCLUDED.university_name,
                        website = EXCLUDED.website,
                        updated_at = CURRENT_TIMESTAMP;
                    """,
                    (univ_code, univ_name, website)
                )

                # B. Build & Upsert admission_combinations & components
                combo_dict: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
                for offer in admissions_data:
                    c_id = offer["canonical_combination_id"]
                    c_display = offer["combination_display"]
                    s_items = offer["subjects_items"]
                    if c_id and c_id not in combo_dict:
                        combo_dict[c_id] = (c_display, s_items)

                print(f"[DB] Upserting {len(combo_dict)} unique combinations into admission_combinations...")
                combo_upsert_query = """
                    INSERT INTO admission_combinations (
                        combination_id, combination_display, component_count, created_at, updated_at
                    ) VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (combination_id)
                    DO UPDATE SET
                        combination_display = EXCLUDED.combination_display,
                        component_count = EXCLUDED.component_count,
                        updated_at = CURRENT_TIMESTAMP;
                """
                comp_upsert_query = """
                    INSERT INTO admission_combination_components (
                        combination_id, component_code, component_name, component_type, subject_code, weight
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (combination_id, component_code)
                    DO UPDATE SET
                        component_name = EXCLUDED.component_name,
                        component_type = EXCLUDED.component_type,
                        subject_code = EXCLUDED.subject_code,
                        weight = EXCLUDED.weight;
                """

                for c_id, (c_display, s_items) in combo_dict.items():
                    cur.execute(combo_upsert_query, (c_id, c_display, len(s_items)))
                    for item in s_items:
                        comp_type = item["component_type"]
                        s_code = item["subject_code"]
                        subj_fk = s_code if comp_type == "SUBJECT" else None
                        cur.execute(
                            comp_upsert_query,
                            (
                                c_id,
                                s_code,
                                item["subject_name"],
                                comp_type,
                                subj_fk,
                                item.get("weight", 1.0)
                            )
                        )

                # C. Upsert admission_offers
                print(f"[DB] Upserting {len(admissions_data)} rows into admission_offers...")
                offers_upsert_query = """
                    INSERT INTO admission_offers (
                        university_code, admission_year, program_code, program_name,
                        method_codes, quota, combination_id, combination_display,
                        canonical_combination_id, subjects_json, source_url, retrieved_at, loaded_at
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (university_code, admission_year, program_code, combination_id)
                    DO UPDATE SET
                        program_name = EXCLUDED.program_name,
                        method_codes = EXCLUDED.method_codes,
                        quota = EXCLUDED.quota,
                        combination_display = EXCLUDED.combination_display,
                        canonical_combination_id = EXCLUDED.canonical_combination_id,
                        subjects_json = EXCLUDED.subjects_json,
                        source_url = EXCLUDED.source_url,
                        retrieved_at = EXCLUDED.retrieved_at,
                        loaded_at = CURRENT_TIMESTAMP;
                """
                for row in admissions_data:
                    cur.execute(
                        offers_upsert_query,
                        (
                            row["university_code"],
                            row["admission_year"],
                            row["program_code"],
                            row["program_name"],
                            row["method_codes"],
                            row["quota"],
                            row["combination_id"],
                            row["combination_display"],
                            row["canonical_combination_id"],
                            Jsonb(json.loads(row["subjects_json"])),
                            row["source_url"],
                            row["retrieved_at"],
                        )
                    )

                # D. Upsert cutoff_scores (if provided)
                if cutoffs_data:
                    print(f"[DB] Upserting {len(cutoffs_data)} rows into cutoff_scores...")
                    cutoffs_upsert_query = """
                        INSERT INTO cutoff_scores (
                            university_code, admission_year, program_code, program_name,
                            method_code, score_type, cutoff_score_thpt, cutoff_score_sat,
                            source_url, retrieved_at, loaded_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (university_code, admission_year, program_code, method_code, score_type)
                        DO UPDATE SET
                            program_name = EXCLUDED.program_name,
                            cutoff_score_thpt = EXCLUDED.cutoff_score_thpt,
                            cutoff_score_sat = EXCLUDED.cutoff_score_sat,
                            source_url = EXCLUDED.source_url,
                            retrieved_at = EXCLUDED.retrieved_at,
                            loaded_at = CURRENT_TIMESTAMP;
                    """
                    for row in cutoffs_data:
                        cur.execute(
                            cutoffs_upsert_query,
                            (
                                row["university_code"],
                                row["admission_year"],
                                row["program_code"],
                                row["program_name"],
                                row["method_code"],
                                row["score_type"],
                                row["cutoff_score_thpt"],
                                row["cutoff_score_sat"],
                                row["source_url"],
                                row["retrieved_at"],
                            )
                        )

        print("\n" + "=" * 50)
        print("IMPORT SUMMARY")
        print("=" * 50)
        print(f"University:                  {univ_code}")
        print(f"Admission offers processed:  {len(admissions_data)}")
        print(f"Unique combinations upserted: {len(combo_dict)}")
        if cutoffs_data:
            print(f"Cutoff scores processed:    {len(cutoffs_data)}")
        print("Database transaction committed successfully!")


def main():
    parser = argparse.ArgumentParser(description="Multi-University PostgreSQL Admissions Data Importer")
    parser.add_argument(
        "--university",
        required=True,
        help="University code (e.g. DQN)"
    )
    parser.add_argument(
        "--offers",
        required=True,
        help="Path to admission offers CSV file"
    )
    parser.add_argument(
        "--cutoffs",
        default=None,
        help="Path to cutoff scores CSV file (optional)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform validation without connecting to or writing to DB"
    )

    args = parser.parse_args()
    univ_code = args.university.upper()

    print("=== Multi-University PostgreSQL Data Importer ===")
    print(f"University Code: {univ_code}")
    print(f"Offers CSV:      {args.offers}")
    print(f"Cutoffs CSV:     {args.cutoffs if args.cutoffs else 'None'}")
    print(f"Dry-run mode:    {args.dry_run}")

    # 1. Load university config
    try:
        univ_meta = load_university_config(univ_code)
    except Exception as e:
        print(f"[ERROR] Failed to load university config: {e}", file=sys.stderr)
        sys.exit(1)

    # 2. Validate admissions CSV
    print(f"\n[Validation] Checking offers CSV: {args.offers}...")
    valid_admissions, admissions_errors, admissions_stats = validate_admissions_csv(args.offers)
    if admissions_errors:
        print(f"[ERRORS] Found {len(admissions_errors)} errors in offers CSV:", file=sys.stderr)
        for err in admissions_errors:
            print(f"  - {err}", file=sys.stderr)

    # Verify university_code inside CSV matches CLI parameter
    for row in valid_admissions:
        if row["university_code"] != univ_code:
            admissions_errors.append(
                f"CSV row university_code '{row['university_code']}' does not match CLI parameter '{univ_code}'"
            )
            break

    # 3. Validate cutoffs CSV if provided
    valid_cutoffs: List[Dict[str, Any]] = []
    cutoffs_errors: List[str] = []
    if args.cutoffs:
        print(f"\n[Validation] Checking cutoffs CSV: {args.cutoffs}...")
        valid_cutoffs, cutoffs_errors = validate_cutoffs_csv(args.cutoffs)
        if cutoffs_errors:
            print(f"[ERRORS] Found {len(cutoffs_errors)} errors in cutoffs CSV:", file=sys.stderr)
            for err in cutoffs_errors:
                print(f"  - {err}", file=sys.stderr)

        for row in valid_cutoffs:
            if row["university_code"] != univ_code:
                cutoffs_errors.append(
                    f"Cutoffs CSV row university_code '{row['university_code']}' does not match CLI parameter '{univ_code}'"
                )
                break

    has_errors = bool(admissions_errors or cutoffs_errors)

    if args.dry_run:
        print("\n" + "=" * 50)
        print("DRY-RUN VALIDATION SUMMARY")
        print("=" * 50)
        print(f"University Code:              {univ_code}")
        print(f"Admission offers CSV:         {len(valid_admissions)} valid rows, {len(admissions_errors)} errors.")
        print(f"Unique canonical combo IDs:   {admissions_stats.get('unique_canonical_ids_count', 0)}")
        print(f"Total SUBJECT components:     {admissions_stats.get('total_subject_components', 0)}")
        print(f"Total APTITUDE components:    {admissions_stats.get('total_aptitude_components', 0)}")
        print(f"Unmapped subject labels:      {len(admissions_stats.get('unmapped_subject_labels', []))}")
        print(f"Unclassified components:      {len(admissions_stats.get('unclassified_components', []))}")
        if args.cutoffs:
            print(f"Cutoff scores CSV:            {len(valid_cutoffs)} valid rows, {len(cutoffs_errors)} errors.")

        if has_errors:
            print("\nDry-run completed with VALIDATION ERRORS.", file=sys.stderr)
            sys.exit(1)
        else:
            print("\nDry-run SUCCESSFUL! All records passed validation.")
            sys.exit(0)

    if has_errors:
        print("\n[ABORT] Cannot proceed with database import due to CSV validation errors.", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("\n[ERROR] DATABASE_URL environment variable is not set.", file=sys.stderr)
        print("Example (PowerShell): $env:DATABASE_URL=\"postgresql://postgres:password@localhost:5432/edu_guidance\"", file=sys.stderr)
        sys.exit(1)

    import_to_postgres(
        db_url=db_url,
        univ_meta=univ_meta,
        admissions_data=valid_admissions,
        cutoffs_data=valid_cutoffs if args.cutoffs else None
    )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Validate and upsert an independent admission-combination label snapshot."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any

from dotenv import load_dotenv

from crawlers.common.subject_normalizer import normalize_subject_label
from crawl_combination_labels import CSV_COLUMNS, clean_text


VALID_CODE = re.compile(r"[A-Z0-9]{2,10}\Z")
DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
REQUIRED_COLUMNS = {
    "admission_combinations": {"combination_id", "component_count"},
    "admission_combination_components": {
        "combination_id", "component_type", "subject_code"
    },
    "admission_combination_labels": {
        "code", "subject_details", "canonical_combination_id", "source_url",
        "source_period", "retrieved_at"
    },
}


def load_snapshot(csv_path: Path, manifest_path: Path) -> tuple[list[dict[str, str]], dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("source_url", "source_period", "retrieved_at", "csv_sha256", "label_count"):
        if not manifest.get(key):
            raise ValueError(f"Manifest is missing {key}")
    if not str(manifest["source_url"]).startswith("https://"):
        raise ValueError("source_url must use HTTPS")
    timestamp = datetime.fromisoformat(manifest["retrieved_at"])
    if timestamp.tzinfo is None:
        raise ValueError("retrieved_at must include a timezone")
    digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    if digest != manifest["csv_sha256"]:
        raise ValueError("CSV SHA-256 does not match the manifest")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != list(CSV_COLUMNS):
            raise ValueError(f"CSV must contain exactly {CSV_COLUMNS}")
        rows = []
        for line_number, row in enumerate(reader, start=2):
            code = clean_text(row["combination_code"])
            details = clean_text(row["subject_details"])
            if not code or not details or len(code) > 100:
                raise ValueError(f"Invalid code or subject details on CSV line {line_number}")
            rows.append({"combination_code": code, "subject_details": details})
    if not rows or len(rows) != manifest["label_count"]:
        raise ValueError("CSV row count does not match the manifest")
    return rows, manifest


def load_database_url(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """Load local configuration while preserving an explicitly exported value."""
    load_dotenv(dotenv_path=env_path, override=False)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            f"DATABASE_URL is required. Set it in the environment or in {env_path}"
        )
    return database_url


def build_combination_index(component_rows: list[tuple[str, int, str, str | None]]) -> dict[frozenset[str], list[str]]:
    components: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    counts: dict[str, int] = {}
    for combination_id, component_count, component_type, subject_code in component_rows:
        counts[combination_id] = component_count
        components[combination_id].append((component_type, subject_code))

    index: dict[frozenset[str], list[str]] = defaultdict(list)
    for combination_id, parts in components.items():
        codes = [code for kind, code in parts if kind == "SUBJECT" and code]
        if len(parts) == counts[combination_id] == len(codes) == len(set(codes)) == 3:
            index[frozenset(codes)].append(combination_id)
    return dict(index)


def prepare_links(
    rows: list[dict[str, str]],
    combination_index: dict[frozenset[str], list[str]],
) -> tuple[list[tuple[str, str, str | None]], Counter[str]]:
    details_by_code: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        details_by_code[row["combination_code"]].add(row["subject_details"])

    prepared = []
    reasons: Counter[str] = Counter()
    for row in rows:
        code, details = row["combination_code"], row["subject_details"]
        reason = None
        canonical_id = None
        if not VALID_CODE.fullmatch(code):
            reason = "invalid_code"
        elif len(details_by_code[code]) > 1:
            reason = "conflicting_details"
        else:
            parts = [clean_text(part) for part in details.split(",")]
            if len(parts) != 3:
                reason = "unsupported_component_count"
            else:
                normalized = [normalize_subject_label(part) for part in parts]
                if any(item is None or item.component_type != "SUBJECT" for item, _ in normalized):
                    reason = "unmapped_or_aptitude"
                else:
                    subject_codes = frozenset(item.subject_code for item, _ in normalized)
                    matches = combination_index.get(subject_codes, [])
                    if len(subject_codes) != 3 or len(matches) != 1:
                        reason = "no_unique_canonical_combination"
                    else:
                        canonical_id = matches[0]
        if reason:
            reasons[reason] += 1
        prepared.append((code, details, canonical_id))
    return prepared, reasons


def verify_db_schema(cursor: Any) -> None:
    cursor.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
    """, (list(REQUIRED_COLUMNS),))
    present: dict[str, set[str]] = defaultdict(set)
    for table, column in cursor.fetchall():
        present[table].add(column)
    missing = {
        table: sorted(columns - present[table])
        for table, columns in REQUIRED_COLUMNS.items()
        if columns - present[table]
    }
    if missing:
        raise RuntimeError(f"Database schema is missing columns: {missing}. Apply Flyway V3 first.")


def import_snapshot(rows: list[dict[str, str]], manifest: dict[str, Any], database_url: str) -> Counter[str]:
    import psycopg

    with psycopg.connect(database_url) as connection:
        with connection.transaction():
            with connection.cursor() as cursor:
                verify_db_schema(cursor)
                cursor.execute("""
                    SELECT MAX(retrieved_at) FROM admission_combination_labels
                    WHERE source_url = %s AND source_period = %s
                """, (manifest["source_url"], manifest["source_period"]))
                previous = cursor.fetchone()[0]
                retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
                if previous is not None and retrieved_at < previous:
                    raise ValueError("Snapshot is older than the latest imported snapshot")

                cursor.execute("""
                    SELECT c.combination_id, c.component_count, comp.component_type, comp.subject_code
                    FROM admission_combinations c
                    JOIN admission_combination_components comp
                      ON comp.combination_id = c.combination_id
                    ORDER BY c.combination_id
                """)
                prepared, reasons = prepare_links(rows, build_combination_index(cursor.fetchall()))
                for code, details, canonical_id in prepared:
                    cursor.execute("""
                        INSERT INTO admission_combination_labels
                            (code, subject_details, canonical_combination_id,
                             source_url, source_period, retrieved_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (source_url, source_period, code, subject_details)
                        DO UPDATE SET canonical_combination_id = EXCLUDED.canonical_combination_id,
                                      retrieved_at = EXCLUDED.retrieved_at
                    """, (
                        code, details, canonical_id, manifest["source_url"],
                        manifest["source_period"], retrieved_at,
                    ))
    return reasons


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=Path("data/combination_labels/combination_labels.csv"))
    parser.add_argument("--manifest", type=Path, default=Path("data/combination_labels/source_manifest.json"))
    parser.add_argument("--dry-run", action="store_true", help="Validate files without a database write")
    args = parser.parse_args()

    rows, manifest = load_snapshot(args.csv, args.manifest)
    print(f"Validated {len(rows)} labels from {manifest['source_url']}")
    if args.dry_run:
        conflicts = sum(
            len({row["subject_details"] for row in rows if row["combination_code"] == code}) > 1
            for code in {row["combination_code"] for row in rows}
        )
        print(f"Dry run: {conflicts} codes have conflicting descriptions; no database write")
        return

    database_url = load_database_url()
    reasons = import_snapshot(rows, manifest, database_url)
    print(f"Imported {len(rows)} labels; linked {len(rows) - sum(reasons.values())}")
    print("Unlinked by reason: " + json.dumps(reasons, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

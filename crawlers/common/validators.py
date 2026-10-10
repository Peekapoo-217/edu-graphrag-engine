#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/common/validators.py

Validation logic for admission offers and cutoff scores CSV files.
Supports legacy CSV parsing without component_type.
"""

import csv
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from crawlers.common.subject_normalizer import (
    VALID_STANDARD_SUBJECT_CODES,
    normalize_subject_label,
)
from crawlers.common.combination_normalizer import (
    generate_canonical_combination_id,
    format_combination_display,
)
from crawlers.common.models import SubjectItem


def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 datetime string."""
    if not dt_str or not dt_str.strip():
        return None
    try:
        return datetime.fromisoformat(dt_str.strip())
    except ValueError:
        return None


def validate_admissions_csv(
    filepath: str,
    db_subject_codes: Optional[Set[str]] = None
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """
    Validate admission offers CSV file.
    Returns (valid_records, errors_list, summary_stats).
    """
    if not os.path.exists(filepath):
        return [], [f"File not found: {filepath}"], {}

    valid_records: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_keys: Set[Tuple[str, str, str, str]] = set()

    # Summary statistics for dry-run
    total_subject_components = 0
    total_aptitude_components = 0
    unique_canonical_ids: Set[str] = set()
    unmapped_subject_labels: Set[str] = set()
    unclassified_components: Set[str] = set()

    required_fields = [
        "university_code",
        "admission_year",
        "program_code",
        "program_name",
        "combination_id",
        "combination_display",
        "subjects_json",
    ]

    valid_subject_codes = set(VALID_STANDARD_SUBJECT_CODES)
    if db_subject_codes:
        valid_subject_codes.update(db_subject_codes)

    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            row_errors = []

            # 1. Required fields
            for rf in required_fields:
                val = row.get(rf)
                if val is None or str(val).strip() == "":
                    row_errors.append(f"Missing required field '{rf}'")

            # 2. admission_year int check
            admission_year = None
            if row.get("admission_year"):
                try:
                    admission_year = int(str(row["admission_year"]).strip())
                except ValueError:
                    row_errors.append(f"Invalid integer for admission_year: '{row['admission_year']}'")

            # 3. quota int or empty
            quota = None
            raw_quota = row.get("quota")
            if raw_quota is not None and str(raw_quota).strip() != "":
                try:
                    quota = int(str(raw_quota).strip())
                except ValueError:
                    row_errors.append(f"Invalid integer for quota: '{raw_quota}'")

            # 4. subjects_json parsing & validation
            raw_subj = row.get("subjects_json")
            subjects_items: List[SubjectItem] = []
            if raw_subj:
                try:
                    subj_data = json.loads(raw_subj)
                    if not isinstance(subj_data, list):
                        row_errors.append("subjects_json must be a JSON array")
                    else:
                        for idx, s in enumerate(subj_data):
                            code = str(s.get("subject_code", "")).strip()
                            name = str(s.get("subject_name", "")).strip()
                            comp_type = s.get("component_type")
                            weight = s.get("weight", 1.0)
                            source_label = s.get("source_label", name)

                            # Handle legacy code conversion
                            if code == "ECONOMIC_LAW_ED":
                                code = "ECONOMIC_LAW"

                            # Determine component_type if missing (Legacy CSV compatibility)
                            if not comp_type:
                                if code.startswith("APTITUDE_"):
                                    comp_type = "APTITUDE"
                                elif code in valid_subject_codes:
                                    comp_type = "SUBJECT"
                                else:
                                    unclassified_components.add(f"{code} ({name})")
                                    row_errors.append(
                                        f"Unclassified component: '{code}' (missing component_type and not in standard subjects)"
                                    )
                                    continue

                            comp_type = str(comp_type).upper().strip()

                            # Validate component_type
                            if comp_type == "SUBJECT":
                                if code not in valid_subject_codes:
                                    unmapped_subject_labels.add(f"{code} ({source_label})")
                                    row_errors.append(
                                        f"Subject code '{code}' is not in valid standard subjects"
                                    )
                                else:
                                    total_subject_components += 1
                            elif comp_type == "APTITUDE":
                                if not code.startswith("APTITUDE_"):
                                    row_errors.append(
                                        f"Aptitude code '{code}' must start with 'APTITUDE_'"
                                    )
                                else:
                                    total_aptitude_components += 1
                            else:
                                unclassified_components.add(f"{code} (type: {comp_type})")
                                row_errors.append(
                                    f"Invalid component_type '{comp_type}' for subject '{code}'"
                                )
                                continue

                            subjects_items.append(SubjectItem(
                                subject_code=code,
                                subject_name=name,
                                component_type=comp_type,
                                weight=float(weight),
                                source_label=source_label
                            ))
                except json.JSONDecodeError as e:
                    row_errors.append(f"Invalid JSON for subjects_json: {e}")

            # Re-generate subjects_json with normalized items
            normalized_subjects_json = json.dumps(
                [item.to_dict() for item in subjects_items],
                ensure_ascii=False
            )

            # Generate canonical_combination_id
            subject_codes_list = [item.subject_code for item in subjects_items]
            canonical_combo_id = generate_canonical_combination_id(subject_codes_list)
            if canonical_combo_id:
                unique_canonical_ids.add(canonical_combo_id)

            # Generate combination_display ordering
            combination_display = row.get("combination_display", "").strip()
            if subjects_items:
                combination_display = format_combination_display(subjects_items)

            # 5. retrieved_at check
            raw_retrieved = row.get("retrieved_at")
            if raw_retrieved and str(raw_retrieved).strip() != "":
                if parse_iso_datetime(raw_retrieved) is None:
                    row_errors.append(f"Invalid ISO timestamp for retrieved_at: '{raw_retrieved}'")

            # 6. Duplicate key check
            univ_code = (row.get("university_code") or "").strip()
            year_str = str(admission_year) if admission_year is not None else (row.get("admission_year") or "").strip()
            prog_code = (row.get("program_code") or "").strip()
            combo_id = (row.get("combination_id") or "").strip()

            key = (univ_code, year_str, prog_code, combo_id)
            if key in seen_keys:
                row_errors.append(f"Duplicate unique key in CSV: {key}")
            else:
                seen_keys.add(key)

            if row_errors:
                errors.append(f"Row {row_idx}: " + "; ".join(row_errors))
            else:
                valid_records.append({
                    "university_code": univ_code,
                    "admission_year": admission_year,
                    "program_code": prog_code,
                    "program_name": (row.get("program_name") or "").strip(),
                    "method_codes": (row.get("method_codes") or "").strip() or None,
                    "quota": quota,
                    "combination_id": combo_id,
                    "combination_display": combination_display,
                    "canonical_combination_id": canonical_combo_id,
                    "subjects_json": normalized_subjects_json,
                    "subjects_items": [item.to_dict() for item in subjects_items],
                    "source_url": (row.get("source_url") or "").strip() or None,
                    "retrieved_at": (raw_retrieved or "").strip() or None,
                })

    summary_stats = {
        "valid_rows_count": len(valid_records),
        "error_rows_count": len(errors),
        "unique_canonical_ids_count": len(unique_canonical_ids),
        "total_subject_components": total_subject_components,
        "total_aptitude_components": total_aptitude_components,
        "unmapped_subject_labels": list(unmapped_subject_labels),
        "unclassified_components": list(unclassified_components),
    }

    return valid_records, errors, summary_stats


def validate_cutoffs_csv(filepath: str) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Validate cutoff scores CSV file."""
    if not os.path.exists(filepath):
        return [], [f"File not found: {filepath}"]

    valid_records: List[Dict[str, Any]] = []
    errors: List[str] = []
    seen_keys: Set[Tuple[str, str, str, str, str]] = set()

    required_fields = [
        "university_code",
        "admission_year",
        "program_code",
        "program_name",
        "method_code",
        "score_type",
    ]

    with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row_idx, row in enumerate(reader, start=2):
            row_errors = []

            for rf in required_fields:
                val = row.get(rf)
                if val is None or str(val).strip() == "":
                    row_errors.append(f"Missing required field '{rf}'")

            admission_year = None
            if row.get("admission_year"):
                try:
                    admission_year = int(str(row["admission_year"]).strip())
                except ValueError:
                    row_errors.append(f"Invalid integer for admission_year: '{row['admission_year']}'")

            cutoff_thpt = None
            raw_thpt = row.get("cutoff_score_thpt")
            if raw_thpt is not None and str(raw_thpt).strip() != "":
                try:
                    cutoff_thpt = float(str(raw_thpt).strip())
                except ValueError:
                    row_errors.append(f"Invalid numeric for cutoff_score_thpt: '{raw_thpt}'")

            cutoff_sat = None
            raw_sat = row.get("cutoff_score_sat")
            if raw_sat is not None and str(raw_sat).strip() != "":
                try:
                    cutoff_sat = float(str(raw_sat).strip())
                except ValueError:
                    row_errors.append(f"Invalid numeric for cutoff_score_sat: '{raw_sat}'")

            raw_retrieved = row.get("retrieved_at")
            if raw_retrieved and str(raw_retrieved).strip() != "":
                if parse_iso_datetime(raw_retrieved) is None:
                    row_errors.append(f"Invalid ISO timestamp for retrieved_at: '{raw_retrieved}'")

            univ_code = (row.get("university_code") or "").strip()
            year_str = str(admission_year) if admission_year is not None else (row.get("admission_year") or "").strip()
            prog_code = (row.get("program_code") or "").strip()
            method_code = (row.get("method_code") or "").strip()
            score_type = (row.get("score_type") or "").strip()

            key = (univ_code, year_str, prog_code, method_code, score_type)
            if key in seen_keys:
                row_errors.append(f"Duplicate unique key in CSV: {key}")
            else:
                seen_keys.add(key)

            if row_errors:
                errors.append(f"Row {row_idx}: " + "; ".join(row_errors))
            else:
                valid_records.append({
                    "university_code": univ_code,
                    "admission_year": admission_year,
                    "program_code": prog_code,
                    "program_name": (row.get("program_name") or "").strip(),
                    "method_code": method_code,
                    "score_type": score_type,
                    "cutoff_score_thpt": cutoff_thpt,
                    "cutoff_score_sat": cutoff_sat,
                    "source_url": (row.get("source_url") or "").strip() or None,
                    "retrieved_at": (raw_retrieved or "").strip() or None,
                })

    return valid_records, errors

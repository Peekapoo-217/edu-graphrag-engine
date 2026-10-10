#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_qnu_to_postgres.py

Backward-compatibility wrapper for QNU PostgreSQL Data Importer.
Delegates validation and import to generalized import_to_postgres module.
"""

import argparse
import os
import sys
import import_to_postgres


def main():
    parser = argparse.ArgumentParser(description="QNU Admissions Data PostgreSQL Importer (Wrapper)")
    parser.add_argument(
        "--admissions-csv",
        default="upload/qnu_admission_offers_2026.csv",
        help="Path to admission offers CSV file (default: upload/qnu_admission_offers_2026.csv)"
    )
    parser.add_argument(
        "--cutoffs-csv",
        default="upload/qnu_cutoff_scores_2025.csv",
        help="Path to cutoff scores CSV file (default: upload/qnu_cutoff_scores_2025.csv)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform validation without connecting to or writing to the database"
    )

    args = parser.parse_args()

    # If default upload path doesn't exist, check qnu_data/ or data/DQN/
    offers_path = args.admissions_csv
    if not os.path.exists(offers_path):
        alt_offers = os.path.join("data", "DQN", "dqn_admission_offers_2026.csv")
        if os.path.exists(alt_offers):
            offers_path = alt_offers
        else:
            alt_offers_qnu = os.path.join("qnu_data", "qnu_admission_offers_2026.csv")
            if os.path.exists(alt_offers_qnu):
                offers_path = alt_offers_qnu

    cutoffs_path = args.cutoffs_csv
    if not os.path.exists(cutoffs_path):
        alt_cutoffs = os.path.join("data", "DQN", "dqn_cutoff_scores_2025.csv")
        if os.path.exists(alt_cutoffs):
            cutoffs_path = alt_cutoffs
        else:
            alt_cutoffs_qnu = os.path.join("qnu_data", "qnu_cutoff_scores_2025.csv")
            if os.path.exists(alt_cutoffs_qnu):
                cutoffs_path = alt_cutoffs_qnu

    # Forward to import_to_postgres CLI
    sys.argv = [
        "import_to_postgres.py",
        "--university", "DQN",
        "--offers", offers_path,
        "--cutoffs", cutoffs_path,
    ]
    if args.dry_run:
        sys.argv.append("--dry-run")

    import_to_postgres.main()


if __name__ == "__main__":
    main()

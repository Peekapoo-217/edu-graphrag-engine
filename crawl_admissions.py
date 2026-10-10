#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawl_admissions.py

Unified CLI interface for crawling admissions data from various universities.
Uses CrawlerRegistry to look up adapter based on --university code.
"""

import argparse
import sys
from crawlers.registry import get_crawler, list_registered_crawlers


def main():
    parser = argparse.ArgumentParser(description="Multi-University Admissions Crawler CLI")
    parser.add_argument(
        "--university",
        required=True,
        help=f"University code to crawl (e.g. DQN). Available: {list_registered_crawlers()}"
    )
    parser.add_argument(
        "--admission-year",
        type=int,
        default=2026,
        help="Admission offers year (default: 2026)"
    )
    parser.add_argument(
        "--cutoff-year",
        type=int,
        default=2025,
        help="Official cutoff scores year (default: 2025)"
    )
    parser.add_argument(
        "--output-dir",
        default="data",
        help="Output root directory (default: data)"
    )

    args = parser.parse_args()
    univ_code = args.university.upper()

    print(f"=== Multi-University Admissions Crawler ===")
    print(f"University:     {univ_code}")
    print(f"Admission Year: {args.admission_year}")
    print(f"Cutoff Year:    {args.cutoff_year}")
    print(f"Output Dir:     {args.output_dir}")

    try:
        crawler = get_crawler(univ_code, output_dir=args.output_dir)
        print(f"\n[Crawler] Initialized {crawler.__class__.__name__} for {univ_code}...")
        result_files = crawler.run(
            admission_year=args.admission_year,
            cutoff_year=args.cutoff_year
        )
        print("\nCrawling completed successfully!")
        print("Generated files:")
        for k, v in result_files.items():
            print(f"  - {k}: {v}")
    except Exception as e:
        print(f"\n[ERROR] Crawling failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

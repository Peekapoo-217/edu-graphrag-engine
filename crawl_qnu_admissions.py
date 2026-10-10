#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawl_qnu_admissions.py

Backward-compatibility wrapper for QNU admissions crawler.
Delegates crawling logic to QNUCrawler adapter via CrawlerRegistry.
"""

import argparse
import os
import sys
from crawlers.registry import get_crawler


def main():
    parser = argparse.ArgumentParser(description="QNU Admissions Data Crawler (Wrapper)")
    parser.add_argument(
        "--output-dir",
        default="qnu_data",
        help="Output directory for generated files (default: qnu_data)"
    )
    args = parser.parse_args()

    print("Starting QNU Admissions Crawler (Wrapper)...")
    # Execute QNU crawler
    crawler = get_crawler("DQN", output_dir=args.output_dir)
    # Re-route output to qnu_data directly for full legacy path compatibility if output-dir is default
    if args.output_dir == "qnu_data":
        crawler.univ_data_dir = os.path.abspath("qnu_data")
        crawler.raw_dir = os.path.join(crawler.univ_data_dir, "raw")

    crawler.run(admission_year=2026, cutoff_year=2025)
    print("\nCrawling completed successfully!")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/base.py

Abstract base class interface for university admission crawlers.
"""

from abc import ABC, abstractmethod
import csv
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
import urllib.request
from typing import Any, Dict, List, Optional

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
MAX_RETRIES = 3
RETRY_DELAY = 2.0
REQUEST_TIMEOUT = 15.0


def get_iso_now() -> str:
    """Return ISO 8601 string with local timezone offset."""
    return datetime.now(timezone.utc).astimezone().isoformat()


class BaseCrawler(ABC):
    """Abstract interface for all university crawler adapters."""

    def __init__(self, university_code: str, output_dir: str = "data"):
        self.university_code = university_code.upper()
        self.output_dir = os.path.abspath(output_dir)
        self.univ_data_dir = os.path.join(self.output_dir, self.university_code)
        self.raw_dir = os.path.join(self.univ_data_dir, "raw")

    def fetch_url(
        self,
        url: str,
        retries: int = MAX_RETRIES,
        timeout: float = REQUEST_TIMEOUT
    ) -> str:
        """Fetch URL content with retry logic and standard User-Agent."""
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        last_error = None
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as response:
                    content_bytes = response.read()
                    return content_bytes.decode("utf-8", errors="replace")
            except Exception as e:
                last_error = e
                print(
                    f"[FETCH ERROR] Attempt {attempt}/{retries} for {url} failed: {e}",
                    file=sys.stderr
                )
                if attempt < retries:
                    time.sleep(RETRY_DELAY * attempt)
        raise last_error

    def write_csv(
        self,
        filepath: str,
        rows: List[Dict[str, Any]],
        fieldnames: List[str]
    ) -> None:
        """Write list of dicts to CSV file with UTF-8 BOM encoding."""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    @abstractmethod
    def run(
        self,
        admission_year: int = 2026,
        cutoff_year: Optional[int] = 2025
    ) -> Dict[str, Any]:
        """Execute crawling pipeline for university and return generated file paths."""
        pass

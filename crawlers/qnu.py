#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/qnu.py

Quy Nhon University (QNU - DQN) Admissions Crawler Adapter.
"""

import hashlib
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
from lxml import html

from crawlers.base import BaseCrawler, get_iso_now
from crawlers.registry import register_crawler
from crawlers.common.subject_normalizer import normalize_subject_label
from crawlers.common.combination_normalizer import (
    generate_canonical_combination_id,
    format_combination_display,
)
from crawlers.common.models import SubjectItem

URL_2026 = "https://tuyensinh.qnu.edu.vn/vi/tuyen-sinh-dai-hoc-chinh-quy-2027/tb-1906-thong-tin-tuyen-sinh-dai-hoc-nam-2026-cap-nhat-hinh-thuc-dao-tao-chinh-quy-1208"
URL_2025 = "https://tuyensinh.qnu.edu.vn/vi/diem-chuan-cac-nam/diem-chuan-trung-tuyen-dai-hoc-he-chinh-quy-nam-2025"

METHOD_MAP = {
    "1": "100",
    "2": "200",
    "3": "402A",
    "4": "402B",
    "5": "405",
}


def map_method_codes(raw_methods: str) -> str:
    """Map raw QNU method labels (e.g. '1,2,3,4') to standard method codes."""
    if not raw_methods:
        return ""
    parts = [p.strip() for p in raw_methods.split(",") if p.strip()]
    mapped = [METHOD_MAP.get(p, p) for p in parts]
    return ", ".join(mapped)


def parse_score(val: Any) -> str:
    """Parse Vietnamese numeric string (e.g. '25,1') to standard float string ('25.1')."""
    if val is None:
        return ""
    val_str = str(val).strip()
    if not val_str:
        return ""
    val_str = val_str.replace(",", ".")
    try:
        f = float(val_str)
        if f.is_integer():
            return str(int(f))
        return f"{f:.4f}".rstrip("0").rstrip(".")
    except ValueError:
        return ""


@register_crawler("DQN")
class QNUCrawler(BaseCrawler):
    """Crawler adapter for Quy Nhon University (DQN)."""

    def parse_2026_offers(
        self,
        html_content: str,
        source_url: str,
        retrieved_at: str
    ) -> List[Dict[str, Any]]:
        """Parse QNU 2026 admission offers table."""
        doc = html.fromstring(html_content)
        tables = doc.xpath("//table")
        if not tables:
            raise ValueError("No table found in 2026 HTML page.")

        table = tables[0]
        rows = table.xpath(".//tr")
        if not rows:
            raise ValueError("No tr elements found in 2026 admissions table.")

        parsed_rows = []
        header_skipped = False

        for r in rows:
            cells = [c.text_content().strip() for c in r.xpath(".//th|.//td")]
            if not cells:
                continue

            if not header_skipped and ("STT" in cells[0] or "Mã xét tuyển" in "".join(cells)):
                header_skipped = True
                continue

            if len(cells) < 6:
                continue

            qnu_table_row = cells[0]
            program_code = cells[1]
            program_name = cells[2]
            source_method_labels = cells[3]
            raw_quota = cells[4]
            combo_cell_text = cells[5]

            method_codes = map_method_codes(source_method_labels)
            matches = re.findall(r"\((.*?)\)", combo_cell_text)
            if not matches and combo_cell_text:
                matches = [combo_cell_text]

            for combo_inner in matches:
                combo_inner = combo_inner.strip()
                if not combo_inner:
                    continue

                raw_parts = [p.strip() for p in combo_inner.split(",") if p.strip()]
                subject_items: List[SubjectItem] = []
                subject_codes: List[str] = []

                for p in raw_parts:
                    item, err = normalize_subject_label(
                        p, university_code=self.university_code, source_url=source_url
                    )
                    if err:
                        print(f"[WARNING] {err}", file=sys.stderr)
                    if item:
                        subject_items.append(item)
                        subject_codes.append(item.subject_code)

                subjects_json_str = json.dumps(
                    [s.to_dict() for s in subject_items], ensure_ascii=False
                )
                canonical_combo_id = generate_canonical_combination_id(subject_codes)
                display_str = format_combination_display(subject_items)

                # Raw combo ID from source
                raw_combo_id = "_".join(subject_codes)

                try:
                    quota_val = int(raw_quota) if raw_quota and raw_quota.isdigit() else None
                except ValueError:
                    quota_val = None

                parsed_rows.append({
                    "university_code": self.university_code,
                    "admission_year": 2026,
                    "program_code": program_code,
                    "program_name": program_name,
                    "method_codes": method_codes,
                    "quota": quota_val if quota_val is not None else raw_quota,
                    "combination_id": raw_combo_id,
                    "combination_display": display_str,
                    "subjects_json": subjects_json_str,
                    "source_url": source_url,
                    "retrieved_at": retrieved_at,
                })

        return parsed_rows

    def parse_2025_cutoffs(
        self,
        html_content: str,
        source_url: str,
        retrieved_at: str
    ) -> List[Dict[str, Any]]:
        """Parse QNU 2025 cutoff scores table."""
        doc = html.fromstring(html_content)
        tables = doc.xpath("//table")
        if not tables:
            raise ValueError("No table found in 2025 HTML page.")

        table = tables[0]
        rows = table.xpath(".//tr")
        if not rows:
            raise ValueError("No tr elements found in 2025 cutoffs table.")

        parsed_rows = []
        header_skipped = False

        for r in rows:
            cells = [c.text_content().strip() for c in r.xpath(".//th|.//td")]
            if not cells:
                continue

            if not header_skipped and ("STT" in cells[0] or "Điểm chuẩn" in "".join(cells)):
                header_skipped = True
                continue

            if len(cells) < 5:
                continue

            program_name = cells[1]
            program_code = cells[2]
            raw_thpt = cells[3]
            raw_sat = cells[4]

            thpt_score = parse_score(raw_thpt)
            sat_score = parse_score(raw_sat)

            parsed_rows.append({
                "university_code": self.university_code,
                "admission_year": 2025,
                "program_code": program_code,
                "program_name": program_name,
                "method_code": "100",
                "score_type": "OFFICIAL_CUTOFF",
                "cutoff_score_thpt": thpt_score,
                "cutoff_score_sat": sat_score,
                "source_url": source_url,
                "retrieved_at": retrieved_at,
            })

        return parsed_rows

    def run(
        self,
        admission_year: int = 2026,
        cutoff_year: Optional[int] = 2025
    ) -> Dict[str, Any]:
        """Execute QNU crawler flow."""
        os.makedirs(self.raw_dir, exist_ok=True)
        manifest_sources = []
        result_files = {}

        # 1. Admission offers 2026
        retrieved_at_2026 = get_iso_now()
        html_2026 = self.fetch_url(URL_2026)

        raw_2026_rel = os.path.join("raw", f"qnu_admissions_{admission_year}.html")
        raw_2026_path = os.path.join(self.univ_data_dir, raw_2026_rel)
        with open(raw_2026_path, "w", encoding="utf-8") as f:
            f.write(html_2026)

        sha256_2026 = hashlib.sha256(html_2026.encode("utf-8")).hexdigest()
        manifest_sources.append({
            "url": URL_2026,
            "raw_html_path": raw_2026_rel.replace("\\", "/"),
            "sha256": sha256_2026,
            "retrieved_at": retrieved_at_2026,
        })

        rows_2026 = self.parse_2026_offers(html_2026, URL_2026, retrieved_at_2026)
        univ_lower = self.university_code.lower()
        csv_offers_name = f"{univ_lower}_admission_offers_{admission_year}.csv"
        csv_offers_path = os.path.join(self.univ_data_dir, csv_offers_name)

        offers_fields = [
            "university_code",
            "admission_year",
            "program_code",
            "program_name",
            "method_codes",
            "quota",
            "combination_id",
            "combination_display",
            "subjects_json",
            "source_url",
            "retrieved_at",
        ]
        self.write_csv(csv_offers_path, rows_2026, offers_fields)
        result_files["offers_csv"] = csv_offers_path

        # 2. Cutoff scores 2025
        if cutoff_year:
            retrieved_at_2025 = get_iso_now()
            html_2025 = self.fetch_url(URL_2025)

            raw_2025_rel = os.path.join("raw", f"qnu_cutoff_{cutoff_year}.html")
            raw_2025_path = os.path.join(self.univ_data_dir, raw_2025_rel)
            with open(raw_2025_path, "w", encoding="utf-8") as f:
                f.write(html_2025)

            sha256_2025 = hashlib.sha256(html_2025.encode("utf-8")).hexdigest()
            manifest_sources.append({
                "url": URL_2025,
                "raw_html_path": raw_2025_rel.replace("\\", "/"),
                "sha256": sha256_2025,
                "retrieved_at": retrieved_at_2025,
            })

            rows_2025 = self.parse_2025_cutoffs(html_2025, URL_2025, retrieved_at_2025)
            csv_cutoffs_name = f"{univ_lower}_cutoff_scores_{cutoff_year}.csv"
            csv_cutoffs_path = os.path.join(self.univ_data_dir, csv_cutoffs_name)

            cutoffs_fields = [
                "university_code",
                "admission_year",
                "program_code",
                "program_name",
                "method_code",
                "score_type",
                "cutoff_score_thpt",
                "cutoff_score_sat",
                "source_url",
                "retrieved_at",
            ]
            self.write_csv(csv_cutoffs_path, rows_2025, cutoffs_fields)
            result_files["cutoffs_csv"] = csv_cutoffs_path

        # Save manifest
        manifest_data = {
            "crawled_at": get_iso_now(),
            "sources": manifest_sources,
            "generated_files": list(result_files.values())
        }
        manifest_path = os.path.join(self.univ_data_dir, "source_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, ensure_ascii=False, indent=2)

        return result_files

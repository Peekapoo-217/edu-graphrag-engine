#!/usr/bin/env python3
"""Collect the two catalogue columns: Tổ hợp and Môn chi tiết."""

import argparse
import csv
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from datetime import datetime, timezone
import unicodedata
from urllib.request import Request, urlopen


SOURCE_URL = "https://diemthi.tuyensinh247.com/to-hop-mon.html"
CSV_COLUMNS = ("combination_code", "subject_details")
CODE_LIST = re.compile(r"[A-Za-z0-9]+(?:\s*[,;]\s*[A-Za-z0-9]+)*\Z")
WHITESPACE = re.compile(r"\s+")


def clean_text(value: str) -> str:
    return WHITESPACE.sub(" ", unicodedata.normalize("NFC", value)).strip()


class TableReader(HTMLParser):
    """Read visible text in table cells, including text nested inside links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_depth = 0
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.table_depth += 1
        elif self.table_depth and tag == "tr":
            self.row = []
        elif self.row is not None and tag in ("td", "th"):
            self.cell = []
        elif self.cell is not None and tag == "br":
            self.cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(clean_text("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_depth:
            self.table_depth -= 1


def split_codes(raw_code: str) -> list[str]:
    """Expand clear code lists; preserve malformed cells for later review."""
    raw_code = clean_text(raw_code)
    if not CODE_LIST.fullmatch(raw_code):
        return [raw_code]
    return list(dict.fromkeys(part.strip().upper() for part in re.split(r"[,;]", raw_code)))


def parse_labels(html_text: str) -> tuple[list[dict[str, str]], int]:
    parser = TableReader()
    parser.feed(html_text)
    labels: list[dict[str, str]] = []
    source_rows = 0
    column_positions: tuple[int, int] | None = None

    for cells in parser.rows:
        normalized = [cell.casefold() for cell in cells]
        if "tổ hợp" in normalized and "môn chi tiết" in normalized:
            column_positions = (normalized.index("tổ hợp"), normalized.index("môn chi tiết"))
            continue
        if column_positions is None or len(cells) <= max(column_positions):
            continue
        raw_code, details = (cells[index] for index in column_positions)
        if not raw_code or not details:
            continue
        source_rows += 1
        labels.extend(
            {"combination_code": code, "subject_details": details}
            for code in split_codes(raw_code)
        )

    if column_positions is None or not labels:
        raise ValueError("Cannot find non-empty 'Tổ hợp' and 'Môn chi tiết' columns")
    return labels, source_rows


def crawl(url: str, input_html: Path | None = None) -> bytes:
    if input_html is not None:
        return input_html.read_bytes()
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SubjectPathway/1.0)"})
    with urlopen(request, timeout=20) as response:
        return response.read()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=SOURCE_URL)
    parser.add_argument("--period", default="2026-2027")
    parser.add_argument("--output-dir", type=Path, default=Path("data/combination_labels"))
    parser.add_argument("--input-html", type=Path, help="Parse a saved HTML page instead of fetching")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing files")
    args = parser.parse_args()

    raw = crawl(args.url, args.input_html)
    labels, source_rows = parse_labels(raw.decode("utf-8-sig", errors="replace"))
    print(f"Source rows: {source_rows}; expanded labels: {len(labels)}")
    if args.dry_run:
        return

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "combination_labels.csv"
    manifest_path = args.output_dir / "source_manifest.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(labels)
    manifest = {
        "source_url": args.url,
        "source_period": args.period,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "csv_path": csv_path.name,
        "csv_sha256": hashlib.sha256(csv_path.read_bytes()).hexdigest(),
        "source_row_count": source_rows,
        "label_count": len(labels),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {csv_path} and {manifest_path}")


if __name__ == "__main__":
    main()

"""Private R2 PDF extraction worker. It only creates review drafts; it never publishes groups."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg
import requests
from pypdf import PdfReader
from psycopg.rows import dict_row

from rag_api.utils.config import settings


def _storage_client():
    import boto3
    endpoint = os.getenv("R2_ENDPOINT", "")
    access_key = os.getenv("R2_ACCESS_KEY_ID", "")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "")
    if not all((endpoint, access_key, secret_key, os.getenv("R2_BUCKET", ""))):
        raise RuntimeError("R2_ENDPOINT, R2_BUCKET and R2 credentials are required")
    return boto3.client("s3", endpoint_url=endpoint,
                        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                        region_name=os.getenv("R2_REGION", "auto"))


def _extract_text(path: Path) -> tuple[list[str], bool]:
    reader = PdfReader(str(path), strict=True)
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise ValueError("ENCRYPTED_PDF")
        except Exception as exc:
            raise ValueError("ENCRYPTED_PDF") from exc
    maximum = int(os.getenv("PDF_MAX_PAGES", "300"))
    if len(reader.pages) == 0 or len(reader.pages) > maximum:
        raise ValueError("PAGE_LIMIT")
    page_text = [(page.extract_text() or "").strip() for page in reader.pages]
    if all(page_text):
        return page_text, False
    with tempfile.TemporaryDirectory(prefix="thpt-pdf-ocr-") as folder:
        output = Path(folder) / "searchable.pdf"
        subprocess.run(["ocrmypdf", "--skip-text", "--language", "vie+eng", str(path), str(output)],
                       check=True, capture_output=True, timeout=900)
        ocr_reader = PdfReader(str(output), strict=True)
        ocr_text = [(page.extract_text() or "").strip() for page in ocr_reader.pages]
        return [original or (ocr_text[index] if index < len(ocr_text) else "")
                for index, original in enumerate(page_text)], True


def _llm_extract(page_text: list[str]) -> list[dict[str, Any]]:
    api_key, api_base, model = settings.LLM_API_KEY, settings.LLM_API_BASE, settings.LLM_MODEL
    if settings.INTERNAL_SERVICE_TOKEN:
        profile_response = requests.get(
            f"{settings.SUBJECT_PATHWAY_INTERNAL_API_URL}/api/v1/internal/llm/EXTRACTION",
            headers={"X-Internal-Service-Token": settings.INTERNAL_SERVICE_TOKEN},
            timeout=settings.LLM_TIMEOUT)
        profile_response.raise_for_status()
        profile = profile_response.json()
        api_key, api_base, model = profile.get("apiKey"), profile.get("endpoint"), profile.get("model")
    if not api_key or not api_base or not model:
        raise RuntimeError("LLM extraction configuration is missing")
    catalog = []
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
        catalog = connection.execute("SELECT code, name FROM subjects WHERE subject_type='ACADEMIC' ORDER BY code").fetchall()
    allowed = {row["code"] for row in catalog}
    prompt = {
        "instruction": "Extract THPT elective subject groups from the PDF text. Return only JSON with groups[]. "
        "Each group has group_code, group_name, subject_codes (choose only from allowed codes), and page_numbers (1-based). "
        "Do not infer missing subjects. Ignore admission combinations and university majors.",
        "allowed_subjects": catalog,
        "pages": [{"page": index + 1, "text": text[:12000]} for index, text in enumerate(page_text) if text],
        "schema": {"groups": [{"group_code": "string", "group_name": "string", "subject_codes": ["four codes"], "page_numbers": [1]}]},
    }
    response = requests.post(api_base.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "temperature": 0, "response_format": {"type": "json_object"},
              "messages": [{"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]},
        timeout=settings.LLM_TIMEOUT)
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    value = json.loads(content)
    groups = value.get("groups") if isinstance(value, dict) else None
    if not isinstance(groups, list) or len(groups) > 200:
        raise ValueError("INVALID_LLM_SCHEMA")
    return validate_groups(groups, allowed, len(page_text))


def validate_groups(groups: Any, allowed_subjects: set[str], page_count: int) -> list[dict[str, Any]]:
    """Validate untrusted model output against the fixed subject catalog and PDF page range."""
    if not isinstance(groups, list) or len(groups) > 200:
        raise ValueError("INVALID_LLM_SCHEMA")
    valid: list[dict[str, Any]] = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        code, name, subjects, pages = item.get("group_code"), item.get("group_name"), item.get("subject_codes"), item.get("page_numbers")
        if not isinstance(code, str) or not code.strip() or not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(subjects, list) or len(subjects) != 4 or not all(isinstance(subject, str) for subject in subjects) or len(set(subjects)) != 4:
            continue
        if not all(isinstance(subject, str) and subject in allowed_subjects for subject in subjects):
            continue
        if not isinstance(pages, list) or not pages or not all(type(page) is int and 1 <= page <= page_count for page in pages):
            continue
        valid.append({"code": code.strip()[:30], "name": name.strip()[:100], "subjects": subjects,
                      "pages": sorted(set(pages))})
    if not valid:
        raise ValueError("NO_VALID_GROUPS")
    return valid


def _claim_job() -> dict[str, Any] | None:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
        with connection.transaction():
            row = connection.execute("""SELECT j.id::text AS job_id, j.document_version_id::text AS version_id,
                v.object_key, v.document_id::text AS document_id
                FROM admin.extraction_jobs j JOIN admin.school_document_versions v ON v.id=j.document_version_id
                WHERE j.status IN ('PENDING','RETRY') AND j.available_at <= now()
                ORDER BY j.available_at, j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1""").fetchone()
            if not row:
                return None
            connection.execute("UPDATE admin.extraction_jobs SET status='RUNNING', attempt_count=attempt_count+1, locked_at=now(), updated_at=now() WHERE id=%s::uuid", (row["job_id"],))
            connection.execute("UPDATE admin.school_documents SET status='PROCESSING', updated_at=now() WHERE id=%s::uuid", (row["document_id"],))
            return row


def _process(job: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="thpt-pdf-") as folder:
        pdf_path = Path(folder) / "source.pdf"
        _storage_client().download_file(os.environ["R2_BUCKET"], job["object_key"], str(pdf_path))
        if pdf_path.stat().st_size > int(os.getenv("PDF_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024))):
            raise ValueError("FILE_LIMIT")
        pages, _used_ocr = _extract_text(pdf_path)
        groups = _llm_extract(pages)
    with psycopg.connect(settings.DATABASE_URL) as connection:
        with connection.transaction():
            for group in groups:
                connection.execute("""INSERT INTO admin.subject_group_drafts(document_version_id, group_code, group_name,
                    subject_codes, source_pages, status) VALUES (%s::uuid,%s,%s,%s::jsonb,%s,'DRAFT')
                    ON CONFLICT (document_version_id, group_code) DO UPDATE SET group_name=excluded.group_name,
                    subject_codes=excluded.subject_codes, source_pages=excluded.source_pages, status='DRAFT'""",
                    (job["version_id"], group["code"], group["name"], json.dumps(group["subjects"]), group["pages"]))
            connection.execute("UPDATE admin.extraction_jobs SET status='SUCCEEDED', locked_at=NULL, updated_at=now() WHERE id=%s::uuid", (job["job_id"],))
            connection.execute("UPDATE admin.school_documents SET status='DRAFT_READY', updated_at=now() WHERE id=%s::uuid", (job["document_id"],))


def run_once() -> bool:
    job = _claim_job()
    if not job:
        return False
    try:
        _process(job)
    except Exception as exc:
        max_attempts = int(os.getenv("PDF_WORKER_MAX_ATTEMPTS", "3"))
        safe_code = str(exc).splitlines()[0][:80] or "EXTRACTION_FAILED"
        with psycopg.connect(settings.DATABASE_URL) as connection:
            with connection.transaction():
                row = connection.execute("UPDATE admin.extraction_jobs SET status=CASE WHEN attempt_count >= %s THEN 'FAILED' ELSE 'RETRY' END, "
                    "available_at=now() + interval '2 minutes', locked_at=NULL, error_code=%s, updated_at=now() "
                    "WHERE id=%s::uuid RETURNING status", (max_attempts, safe_code, job["job_id"])).fetchone()
                connection.execute("UPDATE admin.school_documents SET status=CASE WHEN %s='FAILED' THEN 'FAILED' ELSE 'UPLOADED' END, updated_at=now() WHERE id=%s::uuid",
                    (row[0], job["document_id"]))
        return True
    return True


def main() -> None:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    while True:
        if not run_once():
            time.sleep(max(1, int(os.getenv("PDF_WORKER_POLL_SECONDS", "5"))))


if __name__ == "__main__":
    main()

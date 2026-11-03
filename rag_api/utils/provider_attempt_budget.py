"""Hard outbound completion-attempt cap for diagnostic runs.

The cap is enforced in the process immediately before every provider POST. The
JSONL ledger is append-only and contains identifiers and stages only; prompts,
answers, headers, API keys and endpoint query strings are never serialized.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import threading
from typing import Iterator
import uuid
from urllib.parse import urlparse

import requests


class ProviderAttemptLimitReached(requests.exceptions.RequestException):
    """Raised before network access when the configured diagnostic cap is spent."""


_THREAD_LOCK = threading.Lock()
_UUID4 = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
_STAGES = {"intent", "generation"}


def _configured_limit() -> int | None:
    raw = os.getenv("RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_LIMIT", "").strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ProviderAttemptLimitReached("Diagnostic provider attempt limit is invalid") from exc
    if not 0 <= limit <= 1000:
        raise ProviderAttemptLimitReached("Diagnostic provider attempt limit is outside the allowed range")
    return limit


@contextmanager
def _locked(stream) -> Iterator[None]:
    """Lock one ledger byte across threads and OS processes."""
    with _THREAD_LOCK:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0\n")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_rows(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    with path.open("rb") as stream:
        content = stream.read()
    if not content:
        return []
    rows: list[dict[str, object]] = []
    for raw in content.splitlines():
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderAttemptLimitReached("Diagnostic provider attempt ledger is invalid") from exc
        if not isinstance(row, dict) or row.get("schema_version") != 1 or row.get("stage") not in _STAGES:
            raise ProviderAttemptLimitReached("Diagnostic provider attempt ledger is invalid")
        rows.append(row)
    return rows


def provider_attempt_count(path: str | Path) -> int:
    """Count durable outbound attempts; malformed files fail closed."""
    file_path = Path(path)
    try:
        lock_path = file_path.with_suffix(file_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_stream, _locked(lock_stream):
            return len(_read_rows(file_path))
    except OSError as exc:
        raise ProviderAttemptLimitReached("Diagnostic provider attempt ledger is unavailable") from exc


def record_provider_attempt(*, stage: str, request_id: str | None, endpoint: str | None = None,
                             request_headers: dict[str, str] | None = None) -> int | None:
    """Durably reserve an outbound attempt and return its 1-based sequence.

    This function must be called immediately before requests.post. When the
    diagnostic cap is disabled it is inert, preserving normal runtime behavior.
    """
    limit = _configured_limit()
    _validate_run_headers(stage=stage, request_id=request_id, endpoint=endpoint,
                          request_headers=request_headers)
    if limit is None:
        return None
    ledger_value = os.getenv("RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_FILE", "").strip()
    if not ledger_value:
        raise ProviderAttemptLimitReached("Diagnostic provider attempt ledger path is not configured")
    if stage not in _STAGES or not isinstance(request_id, str) or not _UUID4.fullmatch(request_id):
        raise ProviderAttemptLimitReached("Diagnostic provider attempt metadata is invalid")
    ledger = Path(ledger_value)
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        lock_path = ledger.with_suffix(ledger.suffix + ".lock")
        with lock_path.open("a+b") as lock_stream, _locked(lock_stream):
            rows = _read_rows(ledger)
            if len(rows) >= limit:
                raise ProviderAttemptLimitReached("Diagnostic provider attempt limit reached")
            sequence = len(rows) + 1
            row = {
                "schema_version": 1,
                "attempt": sequence,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "stage": stage,
                "request_id": request_id,
            }
            with ledger.open("ab") as stream:
                stream.write((json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            return sequence
    except ProviderAttemptLimitReached:
        raise
    except OSError as exc:
        raise ProviderAttemptLimitReached("Diagnostic provider attempt ledger is unavailable") from exc


def provider_run_headers(*, endpoint: str, stage: str, request_id: str) -> dict[str, str]:
    """Return trusted internal attribution headers for strict evaluation calls.

    Isolation is opt-in. In strict mode the configured run and active request
    trace must agree, and the provider endpoint must be the configured proxy.
    These internal headers are stripped by the proxy before upstream forwarding.
    """
    if os.getenv("RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED", "false").strip().casefold() not in {
        "1", "true", "yes", "on",
    }:
        return {}
    from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace, trace_enabled

    run_id = os.getenv("RAG_DIAGNOSTIC_RUN_ID", "").strip()
    proxy_token = os.getenv("RAG_DIAGNOSTIC_PROXY_TOKEN", "")
    expected_base = os.getenv("RAG_DIAGNOSTIC_PROXY_BASE_URL", "").strip().rstrip("/")
    if (not _UUID4.fullmatch(run_id) or not _UUID4.fullmatch(request_id)
            or stage not in _STAGES):
        raise ProviderAttemptLimitReached("Diagnostic provider run attribution is invalid")
    if not proxy_token or not expected_base or not trace_enabled():
        raise ProviderAttemptLimitReached("Diagnostic provider isolation is not configured")
    trace = DiagnosticStageTrace.current()
    if trace is None or not trace.enabled or trace.run_id != run_id or trace.request_id != request_id:
        raise ProviderAttemptLimitReached("Diagnostic provider call is outside its trusted run context")
    try:
        expected = urlparse(expected_base)
        actual = urlparse(endpoint)
        if (expected.scheme != "http" or expected.hostname not in {"budget-proxy", "phase3-budget-proxy"}
                or expected.port != 20129
                or expected.path != "/v1" or expected.username or expected.password
                or expected.query or expected.fragment or actual.scheme != expected.scheme
                or actual.netloc != expected.netloc or actual.path != "/v1/chat/completions"
                or actual.query or actual.fragment or actual.username or actual.password):
            raise ValueError("route mismatch")
    except (TypeError, ValueError):
        raise ProviderAttemptLimitReached(
            "Diagnostic provider route is not the approved internal proxy"
        ) from None
    return {
        "X-Evaluator-Run-ID": run_id,
        "X-Diagnostic-Proxy-Token": proxy_token,
        "X-Evaluator-Stage": stage,
        "X-Evaluator-Request-ID": request_id,
    }


def _validate_run_headers(*, stage: str, request_id: str | None,
                          endpoint: str | None, request_headers: dict[str, str] | None) -> None:
    if os.getenv("RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED", "false").strip().casefold() not in {
        "1", "true", "yes", "on",
    }:
        return
    if not isinstance(endpoint, str) or not isinstance(request_headers, dict):
        raise ProviderAttemptLimitReached("Diagnostic provider route attribution is required")
    expected = provider_run_headers(endpoint=endpoint, stage=stage, request_id=request_id or "")
    for key, value in expected.items():
        supplied = request_headers.get(key, "")
        if not hmac.compare_digest(supplied, value):
            raise ProviderAttemptLimitReached("Diagnostic provider run attribution does not match")

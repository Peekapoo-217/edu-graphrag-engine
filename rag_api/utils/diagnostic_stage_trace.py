"""Append-only, sanitized request-stage trace for explicitly enabled diagnostics."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Iterator
import uuid


_STAGES = {
    "request", "intent", "entity_resolution", "routing", "backend_lookup",
    "retrieval", "context_formatting", "generation", "response",
}
_EVENTS = {"start", "complete", "error", "input"}
_ROUTE_KINDS = {"backend_lookup", "graph_retrieval_only", "graph_retrieval_generation",
                "hybrid", "none", "unknown"}
_DECISION_BRANCHES = {"greeting", "unsupported", "knowledge", "combination_lookup",
                      "school_group_lookup", "subject_group_advice",
                      "subject_group_comparison", "general_pathway", "unknown"}
_DISPOSITIONS = {"answer", "clarification", "unsupported", "no_data", "error", "unknown"}
_FILTER_KEYS = frozenset({"high_school_code", "academic_year", "subject_group_code", "program_code",
                          "university_code", "answer_scope", "requested_tasks", "intent"})
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_WRITE_LOCK = threading.Lock()
_CURRENT_TRACE: ContextVar["DiagnosticStageTrace | None"] = ContextVar(
    "rag_diagnostic_stage_trace", default=None
)


def trace_enabled() -> bool:
    return os.getenv("RAG_DIAGNOSTIC_TRACE_ENABLED", "false").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def validate_route_event_configuration(path_value: str, run_id: str) -> bool:
    """Require an existing writable trace directory and a filename bound to this run."""
    if not _valid_uuid(run_id) or not isinstance(path_value, str) or not path_value.strip():
        return False
    path = Path(path_value)
    return path.name == f"route_events_{run_id}.jsonl" and path.parent.is_dir() and os.access(path.parent, os.W_OK)


def _valid_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.fullmatch(value))


def append_stage_event(event: dict[str, Any]) -> None:
    """Write only allowlisted metadata; never serialize arbitrary input fields."""
    if not trace_enabled():
        return
    if not isinstance(event, dict):
        raise TypeError("Diagnostic trace event must be an object")
    stage = event.get("stage")
    name = event.get("event")
    run_id = event.get("run_id")
    correlation_id = event.get("correlation_id")
    request_id = event.get("request_id")
    if stage not in _STAGES or name not in _EVENTS:
        raise ValueError("Diagnostic trace stage or event is not allowed")
    if name == "input" and stage not in {"generation", "intent"}:
        raise ValueError("Input metadata is not allowed for this stage")
    if not all(_valid_uuid(value) for value in (run_id, correlation_id, request_id)):
        raise ValueError("Diagnostic trace identifiers must be UUIDv4 values")
    row: dict[str, Any] = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "correlation_id": correlation_id,
        "request_id": request_id,
        "stage": stage,
        "event": name,
    }
    duration = event.get("duration_ms")
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
        row["duration_ms"] = round(float(duration), 3)
    error_class = event.get("error_class")
    if name == "error" and isinstance(error_class, str) and re.fullmatch(
        r"[A-Za-z][A-Za-z0-9_]{0,79}", error_class,
    ):
        row["error_class"] = error_class
    if stage == "generation" and name == "input":
        digest = event.get("context_sha256")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            row["context_sha256"] = digest
        for field in ("context_chars", "context_utf8_bytes", "prompt_chars",
                      "prompt_utf8_bytes", "message_count", "history_turn_count"):
            value = event.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row[field] = value
        timeout = event.get("provider_timeout_seconds")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 600:
            row["provider_timeout_seconds"] = float(timeout)
    if stage == "intent" and name == "input":
        for field in ("message_count", "history_turn_count", "prompt_utf8_bytes", "request_body_utf8_bytes"):
            value = event.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                row[field] = value
        timeout = event.get("configured_timeout_seconds")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 0 < timeout <= 600:
            row["configured_timeout_seconds"] = float(timeout)
    if stage == "intent" and name in {"complete", "error"}:
        outcome = event.get("outcome")
        if outcome in {"response", "provider_error", "timeout"}:
            row["outcome"] = outcome
        error_code = event.get("transport_error_code")
        if error_code in {"connect_timeout", "read_timeout", "timeout", "provider_failure", "error"}:
            row["transport_error_code"] = error_code

    trace_path = os.getenv("RAG_DIAGNOSTIC_TRACE_FILE", "").strip()
    if not trace_path:
        raise RuntimeError("Diagnostic trace is enabled but its output file is not configured")
    destination = Path(trace_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        with destination.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())


def cleanup_expired_route_events(root: str | Path, *, now: datetime | None = None) -> list[str]:
    """Remove only run-scoped v2 route-event files after their embedded 30-day expiry."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("Cleanup clock must be timezone-aware")
    removed: list[str] = []
    for path in Path(root).glob("route_events_*.jsonl"):
        match = re.fullmatch(r"route_events_([0-9a-fA-F-]{36})\.jsonl", path.name)
        if not match or not _valid_uuid(match.group(1)):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
            rows = [json.loads(line) for line in lines]
            if not rows or any(
                not isinstance(row, dict) or row.get("schema_version") != 2
                or row.get("run_id", "").casefold() != match.group(1).casefold()
                or row.get("event") not in {"route_decision", "retrieval_result"}
                for row in rows
            ):
                continue
            expiry_values = [datetime.fromisoformat(row["expires_at_utc"].replace("Z", "+00:00"))
                             for row in rows]
            if all(expiry.tzinfo is not None and expiry <= current for expiry in expiry_values):
                path.unlink()
                removed.append(path.name)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return removed


def append_route_observation(data: dict[str, Any]) -> None:
    """Append one server-observed retrieval result without free-text payloads."""
    if not trace_enabled():
        return
    if not isinstance(data, dict):
        raise TypeError("Diagnostic route observation must be an object")
    run_id, correlation_id, request_id = (data.get("run_id"), data.get("correlation_id"),
                                           data.get("request_id"))
    if not all(_valid_uuid(value) for value in (run_id, correlation_id, request_id)):
        raise ValueError("Diagnostic route identifiers must be UUIDv4 values")
    path_value = os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip()
    if not path_value:
        raise RuntimeError("Diagnostic route event file is not configured")
    destination = Path(path_value)
    if destination.name != f"route_events_{run_id}.jsonl":
        raise ValueError("Diagnostic route event file must be bound to the active run ID")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cleanup_expired_route_events(destination.parent)

    source = data.get("evidence_source")
    backend_executed = data.get("backend_retrieval_executed")
    if not isinstance(backend_executed, bool):
        backend_executed = True if isinstance(source, str) and source.startswith("backend_") else None
    graph_executed = data.get("graph_retrieval_executed")
    if not isinstance(graph_executed, bool):
        graph_executed = True if source == "neo4j_graph" else False if backend_executed is False else None
    retrieval_executed = data.get("retrieval_executed")
    if not isinstance(retrieval_executed, bool):
        retrieval_executed = True if backend_executed is True or graph_executed is True else None
    generation_executed = data.get("generation_executed")
    if not isinstance(generation_executed, bool):
        generation_executed = None
    composition = data.get("hybrid_composition_verified")
    if not isinstance(composition, bool):
        composition = None
    if backend_executed is True and graph_executed is True and composition is True:
        execution_kind = "hybrid"
    elif backend_executed is True and graph_executed is False:
        execution_kind = "backend_lookup"
    elif backend_executed is False and graph_executed is True:
        execution_kind = ("graph_retrieval_generation" if generation_executed is True else
                          "graph_retrieval_only" if generation_executed is False else "unknown")
    elif retrieval_executed is False:
        execution_kind = "none"
    else:
        execution_kind = "unknown"
    disposition = data.get("response_mode")
    if disposition == "abstention":
        disposition = "no_data"
    disposition = disposition if disposition in _DISPOSITIONS else "unknown"
    raw_keys = data.get("filter_keys")
    if raw_keys is None:
        filters = data.get("filters")
        raw_keys = [key for key in filters if key in _FILTER_KEYS] if isinstance(filters, dict) else []
    if not isinstance(raw_keys, list) or any(not isinstance(key, str) or len(key) > 64 for key in raw_keys):
        raise ValueError("Diagnostic route filter keys are outside the allowlist")
    raw_keys = [key for key in raw_keys if key in _FILTER_KEYS]
    evidence_count = data.get("evidence_count")
    if evidence_count is not None and (isinstance(evidence_count, bool)
                                       or not isinstance(evidence_count, int) or evidence_count < 0):
        raise ValueError("Diagnostic route evidence count must be a non-negative integer or null")
    timestamp = datetime.now(timezone.utc)
    expires = timestamp + timedelta(days=30)
    base = {
        "schema_version": 2, "timestamp_utc": timestamp.isoformat(), "expires_at_utc": expires.isoformat(),
        "run_id": run_id, "correlation_id": correlation_id, "request_id": request_id,
        "route_origin": "server_observed", "execution_kind": execution_kind,
        "disposition": disposition, "retrieval_executed": retrieval_executed,
        "backend_retrieval_executed": backend_executed, "graph_retrieval_executed": graph_executed,
        "hybrid_composition_verified": composition,
        "generation_executed": generation_executed,
        "filter_keys": sorted(set(raw_keys)), "evidence_count": evidence_count, "final_route": True,
    }
    row = {**base, "event": "retrieval_result"}
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        with destination.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())


def append_route_decision(data: dict[str, Any]) -> None:
    """Record the selected server routing branch before retrieval begins."""
    if not trace_enabled():
        return
    if not isinstance(data, dict):
        raise TypeError("Diagnostic route decision must be an object")
    run_id, correlation_id, request_id = (data.get("run_id"), data.get("correlation_id"),
                                           data.get("request_id"))
    if not all(_valid_uuid(value) for value in (run_id, correlation_id, request_id)):
        raise ValueError("Diagnostic route identifiers must be UUIDv4 values")
    path_value = os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip()
    if not path_value:
        raise RuntimeError("Diagnostic route event file is not configured")
    destination = Path(path_value)
    if destination.name != f"route_events_{run_id}.jsonl":
        raise ValueError("Diagnostic route event file must be bound to the active run ID")
    branch = data.get("decision_branch")
    if branch not in _DECISION_BRANCHES:
        branch = "unknown"
    decision_status = data.get("decision_status", "selected")
    if decision_status not in {"selected", "not_reached"}:
        raise ValueError("Diagnostic route decision status is invalid")
    raw_keys = data.get("filter_keys", [])
    if not isinstance(raw_keys, list) or any(not isinstance(key, str) or len(key) > 64 for key in raw_keys):
        raise ValueError("Diagnostic route filter keys are outside the allowlist")
    row = {
        "schema_version": 2,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "expires_at_utc": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        "run_id": run_id, "correlation_id": correlation_id, "request_id": request_id,
        "event": "route_decision", "route_origin": "server_decision",
        "decision_branch": branch, "decision_status": decision_status,
        "execution_kind": "unknown", "disposition": "unknown",
        "retrieval_executed": None, "backend_retrieval_executed": None,
        "graph_retrieval_executed": None, "generation_executed": None,
        "filter_keys": sorted(set(key for key in raw_keys if key in _FILTER_KEYS)),
        "evidence_count": None,
    }
    with _WRITE_LOCK:
        with destination.open("ab") as stream:
            stream.write((json.dumps(row, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    trace = DiagnosticStageTrace.current()
    if trace is not None:
        trace.route_decision_recorded = True


class DiagnosticStageTrace:
    """Request scoped event writer; the trace is inert outside diagnostic mode."""

    def __init__(self, run_id: str | None, correlation_id: str, request_id: str) -> None:
        self.enabled = trace_enabled()
        self.run_id = run_id if _valid_uuid(run_id) else None
        self.correlation_id = correlation_id
        self.request_id = request_id
        self._token: Token[DiagnosticStageTrace | None] | None = None
        self._request_started: float | None = None
        self.route_decision_recorded = False
        self._pending_final_route: dict[str, Any] | None = None
        self._final_route_written = False
        self._retrieval_operations: dict[str, tuple[str, set[str]]] = {}
        self._retrieval_attempted = False

    def set_final_route(self, data: dict[str, Any]) -> None:
        """Keep the outermost response candidate until the request boundary closes."""
        if self.enabled and not self._final_route_written:
            self._pending_final_route = dict(data)

    @contextmanager
    def retrieval_operation(self, retrieval_system: str, *, parent_operation_id: str | None = None):
        """Trace one actual retrieval boundary with a stable operation ID."""
        operation = _RetrievalOperation(self, retrieval_system, parent_operation_id)
        self._retrieval_attempted = True
        operation.emit("operation_started", "attempted")
        try:
            yield operation
        except BaseException:
            operation.emit("operation_failed", "failed")
            raise
        else:
            operation.emit("operation_completed", "completed")

    def register_retrieval_result(self, operation_id: str, retrieval_system: str,
                                  evidence_reference_ids: list[str]) -> None:
        self._retrieval_operations[operation_id] = (retrieval_system, set(evidence_reference_ids))

    def compose_evidence(self, evidence_by_system: dict[str, list[str]], *, composition_id: str) -> set[str]:
        """Mark evidence consumed only when its references came from completed operations."""
        consumed: set[str] = set()
        for operation_id, (system, produced) in self._retrieval_operations.items():
            requested = set(evidence_by_system.get(system) or [])
            if requested and requested.issubset(produced):
                operation = _RetrievalOperation(self, system, self.request_id, operation_id=operation_id)
                operation.evidence_reference_ids = sorted(requested)
                operation.emit("evidence_composed", "completed", composition_status="verified",
                               composition_id=composition_id)
                consumed.add(system)
        return consumed

    def __enter__(self) -> "DiagnosticStageTrace":
        if self.enabled:
            if not self.run_id or not _valid_uuid(self.correlation_id) or not _valid_uuid(self.request_id):
                raise ValueError("Diagnostic request identifiers must be UUIDv4 values")
            self._request_started = time.perf_counter()
            self._token = _CURRENT_TRACE.set(self)
            append_stage_event(self._event("request", "start"))
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self.enabled:
            try:
                should_write_final = self._pending_final_route is not None or (
                    exc_type is not None and bool(os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip())
                )
                if should_write_final and (exc_type is not None or self._pending_final_route is None):
                    final = {
                        "run_id": self.run_id, "correlation_id": self.correlation_id,
                        "request_id": self.request_id, "execution_kind": "unknown",
                        "response_mode": "error" if exc_type is not None else "unknown",
                        "retrieval_executed": None, "backend_retrieval_executed": None,
                        "graph_retrieval_executed": None, "generation_executed": None,
                    }
                elif should_write_final:
                    final = self._pending_final_route
                if should_write_final and not self._final_route_written:
                    try:
                        append_route_observation(final)
                    except Exception:
                        # Trace persistence is diagnostic and must not alter chatbot delivery.
                        pass
                    self._final_route_written = True
                event = self._event("request", "error" if exc_type else "complete")
                if exc_type:
                    event["error_class"] = exc_type.__name__
                event["duration_ms"] = self._elapsed()
                try:
                    append_stage_event(event)
                except Exception:
                    # Diagnostics must not replace the chatbot's already computed response.
                    pass
            finally:
                if self._token is not None:
                    _CURRENT_TRACE.reset(self._token)

    def _event(self, stage: str, name: str, **fields: Any) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "correlation_id": self.correlation_id,
            "request_id": self.request_id,
            "stage": stage,
            "event": name,
            **fields,
        }

    @staticmethod
    def current() -> "DiagnosticStageTrace | None":
        return _CURRENT_TRACE.get()

    def _elapsed(self) -> float:
        return max(0.0, (time.perf_counter() - self._request_started) * 1000) if self._request_started else 0.0

    def generation_input(self, **metadata: Any) -> None:
        if self.enabled:
            append_stage_event(self._event("generation", "input", **metadata))

    def intent_input(self, *, message_count: int, history_turn_count: int,
                     prompt_utf8_bytes: int, request_body_utf8_bytes: int,
                     configured_timeout_seconds: float) -> None:
        if not self.enabled:
            return
        values = (message_count, history_turn_count, prompt_utf8_bytes, request_body_utf8_bytes)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise ValueError("Intent diagnostic counts and sizes must be non-negative integers")
        if (isinstance(configured_timeout_seconds, bool)
                or not isinstance(configured_timeout_seconds, (int, float))
                or configured_timeout_seconds <= 0):
            raise ValueError("Intent diagnostic timeout must be positive")
        append_stage_event(self._event("intent", "input", message_count=message_count,
            history_turn_count=history_turn_count, prompt_utf8_bytes=prompt_utf8_bytes,
            request_body_utf8_bytes=request_body_utf8_bytes,
            configured_timeout_seconds=float(configured_timeout_seconds)))

    def intent_terminal(self, *, outcome: str, duration_ms: float,
                        transport_error_code: str | None = None) -> None:
        if not self.enabled:
            return
        if outcome not in {"response", "provider_error", "timeout"}:
            raise ValueError("Intent diagnostic outcome is not allowed")
        if transport_error_code not in {None, "connect_timeout", "read_timeout", "timeout", "provider_failure", "error"}:
            raise ValueError("Intent diagnostic error code is not allowed")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)) or duration_ms < 0:
            raise ValueError("Intent diagnostic duration must be non-negative")
        fields: dict[str, Any] = {"outcome": outcome, "duration_ms": float(duration_ms)}
        if transport_error_code is not None:
            fields["transport_error_code"] = transport_error_code
        append_stage_event(self._event("intent", "complete" if outcome == "response" else "error", **fields))

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        if name not in _STAGES or name == "request":
            raise ValueError("Diagnostic stage is not allowed")
        started = time.perf_counter()
        append_stage_event(self._event(name, "start"))
        try:
            yield
        except Exception as exc:
            append_stage_event(self._event(name, "error", duration_ms=(time.perf_counter() - started) * 1000,
                                           error_class=type(exc).__name__))
            raise
        else:
            append_stage_event(self._event(name, "complete", duration_ms=(time.perf_counter() - started) * 1000))


class _RetrievalOperation:
    def __init__(self, trace: DiagnosticStageTrace, retrieval_system: str,
                 parent_operation_id: str | None, operation_id: str | None = None) -> None:
        if retrieval_system not in {"backend_api", "fastapi_graph"}:
            raise ValueError("Retrieval system is not an approved route label")
        self.trace = trace
        self.retrieval_system = retrieval_system
        self.operation_id = operation_id or str(uuid.uuid4())
        self.parent_operation_id = parent_operation_id or trace.request_id
        self.evidence_reference_ids: list[str] = []

    def emit(self, event: str, status: str, *, composition_status: str = "not_applicable",
             composition_id: str | None = None) -> None:
        if not self.trace.enabled:
            return
        path_value = os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip()
        if not path_value:
            return
        try:
            append_operation_event({
                "run_id": self.trace.run_id, "correlation_id": self.trace.correlation_id,
                "request_id": self.trace.request_id, "operation_id": self.operation_id,
                "parent_operation_id": self.parent_operation_id, "retrieval_system": self.retrieval_system,
                "event": event, "execution_status": status,
                "evidence_reference_ids": list(self.evidence_reference_ids),
                "composition_status": composition_status, "composition_id": composition_id,
            })
        except Exception:
            pass

    def evidence_produced(self, reference_ids: list[str]) -> None:
        self.evidence_reference_ids = sorted({str(value) for value in reference_ids if value})
        self.trace.register_retrieval_result(self.operation_id, self.retrieval_system,
                                             self.evidence_reference_ids)
        self.emit("evidence_produced", "completed")

    def evidence_composed(self, reference_ids: list[str], *, composition_id: str) -> None:
        refs = {str(value) for value in reference_ids if value}
        self.emit("evidence_composed", "completed" if refs and refs.issubset(self.evidence_reference_ids)
                  else "unknown", composition_status="verified" if refs and refs.issubset(
                      self.evidence_reference_ids) else "unverified", composition_id=composition_id)


def append_operation_event(data: dict[str, Any]) -> None:
    """Persist allowlisted operation facts; never include request text or retrieved content."""
    if not trace_enabled():
        return
    run_id = data.get("run_id")
    request_id = data.get("request_id")
    operation_id = data.get("operation_id")
    parent_id = data.get("parent_operation_id")
    if not all(_valid_uuid(value) for value in (run_id, request_id, operation_id, parent_id)):
        raise ValueError("Retrieval operation identifiers must be UUIDv4 values")
    path_value = os.getenv("RAG_DIAGNOSTIC_ROUTE_EVENT_FILE", "").strip()
    destination = Path(path_value)
    if destination.name != f"route_events_{run_id}.jsonl":
        raise ValueError("Diagnostic operation file must be bound to the active run ID")
    if data.get("retrieval_system") not in {"backend_api", "fastapi_graph"}:
        raise ValueError("Retrieval system is not an approved route label")
    if data.get("event") not in {"operation_started", "operation_completed", "operation_failed",
                                  "evidence_produced", "evidence_composed"}:
        raise ValueError("Retrieval operation event is invalid")
    references = data.get("evidence_reference_ids")
    if not isinstance(references, list) or any(not isinstance(item, str) or len(item) > 160 for item in references):
        raise ValueError("Evidence reference IDs are invalid")
    row = {
        "schema_version": 3, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "expires_at_utc": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        **{key: data[key] for key in ("run_id", "correlation_id", "request_id", "operation_id",
                                      "parent_operation_id", "retrieval_system", "event",
                                      "execution_status", "evidence_reference_ids", "composition_status",
                                      "composition_id")},
    }
    encoded = (json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    with _WRITE_LOCK:
        with destination.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

def record_response_stage() -> None:
    trace = _CURRENT_TRACE.get()
    if trace is not None and trace.enabled:
        append_stage_event(trace._event("response", "complete"))

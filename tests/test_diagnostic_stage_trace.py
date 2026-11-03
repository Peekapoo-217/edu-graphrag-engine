import json
import hashlib
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import requests

from rag_api.utils import diagnostic_stage_trace
from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace, append_stage_event
from rag_api.llm.llm_client import RAGGenerator
from rag_api.llm.provider_errors import LLMServiceUnavailable


class DiagnosticStageTraceTests(unittest.TestCase):
    def test_retrieval_operation_records_lifecycle_and_evidence_composition(self):
        run_id, correlation_id, request_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
            "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
        }):
            with DiagnosticStageTrace(run_id, correlation_id, request_id) as trace:
                with trace.retrieval_operation("fastapi_graph") as operation:
                    operation.evidence_produced(["graph:evidence-1"])
                    operation.evidence_composed(["graph:evidence-1"], composition_id="answer-1")
            rows = [json.loads(line) for line in (Path(directory) / f"route_events_{run_id}.jsonl").read_text(encoding="utf-8").splitlines()]
        events = [row for row in rows if row.get("event", "").startswith("operation_") or row.get("event") in {"evidence_produced", "evidence_composed"}]
        self.assertEqual(["operation_started", "evidence_produced", "evidence_composed", "operation_completed"],
                         [row["event"] for row in events])
        self.assertTrue(all(row["request_id"] == request_id for row in events))
        self.assertEqual(events[0]["operation_id"], events[-1]["operation_id"])

    def test_trace_writer_failure_does_not_escape_request_exit(self):
        run_id = str(uuid.uuid4())
        correlation_id = str(uuid.uuid4())
        request_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / f"stage_events_{run_id}.jsonl"),
            "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(Path(directory) / f"route_events_{run_id}.jsonl"),
        }):
            trace = DiagnosticStageTrace(run_id, correlation_id, request_id)
            with patch.object(diagnostic_stage_trace, "append_stage_event", side_effect=[None, OSError("disk" )]), \
                    patch.object(diagnostic_stage_trace, "append_route_observation", side_effect=OSError("disk")):
                with trace:
                    trace.set_final_route({
                        "run_id": run_id, "correlation_id": correlation_id, "request_id": request_id,
                        "response_mode": "answer", "retrieval_executed": False,
                    })

    def test_route_retention_cleanup_removes_only_expired_route_event_files(self):
        cleanup = getattr(diagnostic_stage_trace, "cleanup_expired_route_events", None)
        self.assertTrue(callable(cleanup), "run-scoped 30-day route event cleanup is missing")
        with tempfile.TemporaryDirectory() as directory:
            run_id = str(uuid.uuid4())
            route_file = Path(directory) / f"route_events_{run_id}.jsonl"
            expiry = (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=30)).isoformat()
            row = {"schema_version": 2, "run_id": run_id, "event": "route_decision",
                   "expires_at_utc": expiry}
            route_file.write_text(json.dumps(row) + "\n", encoding="utf-8")
            historical = Path(directory) / "stage_events.jsonl"
            historical.write_text("historical-evidence\n", encoding="utf-8")

            removed = cleanup(directory, now=datetime(2026, 3, 1, tzinfo=timezone.utc))

            self.assertEqual([route_file.name], removed)
            self.assertFalse(route_file.exists())
            self.assertTrue(historical.exists())

    def test_route_observation_is_versioned_allowlisted_and_run_scoped(self):
        append_route_observation = getattr(diagnostic_stage_trace, "append_route_observation", None)
        self.assertTrue(callable(append_route_observation), "sanitized route event writer is missing")
        with tempfile.TemporaryDirectory() as directory:
            run_id, correlation, request_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            path = Path(directory) / f"route_events_{run_id}.jsonl"
            with patch.dict(os.environ, {"RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                                         "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(path),
                                         "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "stage.jsonl")}):
                append_route_observation({
                    "run_id": run_id, "correlation_id": correlation, "request_id": request_id,
                    "execution_kind": "graph_retrieval_generation", "response_mode": "answer",
                    "retrieval_executed": True, "graph_retrieval_executed": True,
                    "backend_retrieval_executed": False, "generation_executed": True,
                    "filter_keys": ["academic_year", "subject_group_code"], "evidence_count": 2,
                    "user_message": "private question", "token": "synthetic-secret",
                })
                rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["retrieval_result"], [row["event"] for row in rows])
            self.assertTrue(all(row["schema_version"] == 2 for row in rows))
            self.assertTrue(all(row["route_origin"] == "server_observed" for row in rows))
            self.assertEqual(["academic_year", "subject_group_code"], rows[0]["filter_keys"])
            self.assertNotIn("private question", json.dumps(rows))
            self.assertNotIn("synthetic-secret", json.dumps(rows))

    def test_hybrid_route_requires_both_retrieval_paths(self):
        append_route_observation = getattr(diagnostic_stage_trace, "append_route_observation", None)
        self.assertTrue(callable(append_route_observation), "sanitized route event writer is missing")
        with tempfile.TemporaryDirectory() as directory:
            run_id = str(uuid.uuid4())
            path = Path(directory) / f"route_events_{run_id}.jsonl"
            with patch.dict(os.environ, {"RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                                         "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(path),
                                         "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "stage.jsonl")}):
                append_route_observation({
                    "run_id": run_id, "correlation_id": str(uuid.uuid4()),
                    "request_id": str(uuid.uuid4()), "execution_kind": "hybrid",
                    "response_mode": "answer", "evidence_source": "backend_subject_group_comparison_api",
                    "retrieval_executed": True, "graph_retrieval_executed": None,
                    "backend_retrieval_executed": True, "generation_executed": False,
                })
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(["unknown"], [row["execution_kind"] for row in rows])

    def test_route_decision_and_retrieval_result_are_distinct_observations(self):
        decision_writer = getattr(diagnostic_stage_trace, "append_route_decision", None)
        result_writer = getattr(diagnostic_stage_trace, "append_route_observation", None)
        self.assertTrue(callable(decision_writer))
        self.assertTrue(callable(result_writer))
        with tempfile.TemporaryDirectory() as directory:
            run_id, correlation, request_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            path = Path(directory) / f"route_events_{run_id}.jsonl"
            with patch.dict(os.environ, {"RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                                         "RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(path),
                                         "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(directory) / "stage.jsonl")}):
                trace = DiagnosticStageTrace(run_id, correlation, request_id)
                with trace:
                    decision_writer({"run_id": run_id, "correlation_id": correlation,
                                     "request_id": request_id, "decision_branch": "general_pathway",
                                     "filter_keys": ["academic_year", "private_filter"]})
                    result_writer({"run_id": run_id, "correlation_id": correlation,
                                   "request_id": request_id, "execution_kind": "graph_retrieval_generation",
                                   "response_mode": "answer", "retrieval_executed": True,
                                   "backend_retrieval_executed": False, "graph_retrieval_executed": True,
                                   "generation_executed": True, "evidence_count": 1})
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["route_decision", "retrieval_result"], [row["event"] for row in rows])
        self.assertEqual("server_decision", rows[0]["route_origin"])
        self.assertEqual("general_pathway", rows[0]["decision_branch"])
        self.assertIsNone(rows[0]["retrieval_executed"])
        self.assertEqual("server_observed", rows[1]["route_origin"])
        self.assertEqual("graph_retrieval_generation", rows[1]["execution_kind"])
        self.assertTrue(rows[1]["retrieval_executed"])
        self.assertEqual(["academic_year"], rows[0]["filter_keys"])

    def test_route_observation_classifies_only_observed_routes_and_dispositions(self):
        append_route_observation = getattr(diagnostic_stage_trace, "append_route_observation", None)
        self.assertTrue(callable(append_route_observation))
        cases = [
            ("neo4j_graph", True, False, "answer", "graph_retrieval_only"),
            ("backend_subjects", False, True, "answer", "backend_lookup"),
            ("hybrid", True, True, "answer", "hybrid"),
            (None, False, False, "clarification", "none"),
            (None, None, None, "unsupported", "unknown"),
            ("neo4j_graph", True, False, "abstention", "graph_retrieval_only"),
            (None, None, None, "error", "unknown"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            observed = []
            with patch.dict(os.environ, {"RAG_DIAGNOSTIC_TRACE_ENABLED": "true"}):
                for source, graph, backend, disposition, expected in cases:
                    run_id = str(uuid.uuid4())
                    path = Path(directory) / f"route_events_{run_id}.jsonl"
                    with patch.dict(os.environ, {"RAG_DIAGNOSTIC_ROUTE_EVENT_FILE": str(path)}):
                        append_route_observation({
                            "run_id": run_id, "correlation_id": str(uuid.uuid4()),
                            "request_id": str(uuid.uuid4()), "evidence_source": source,
                            "execution_kind": "hybrid" if source == "hybrid" else None,
                            "response_mode": disposition,
                            "retrieval_executed": False if disposition == "clarification" else None,
                            "graph_retrieval_executed": graph,
                            "backend_retrieval_executed": backend,
                            "hybrid_composition_verified": source == "hybrid",
                            "generation_executed": False if disposition == "clarification" or graph is True else None,
                        })
                        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                    observed.append((rows[0]["execution_kind"], rows[0]["disposition"]))
        self.assertEqual([case[4] for case in cases], [item[0] for item in observed])
        self.assertEqual(["answer", "answer", "answer", "clarification", "unsupported",
                          "no_data", "error"], [item[1] for item in observed])

    def test_generation_input_metadata_survives_provider_timeout_without_prompt_or_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(path),
            }), patch("rag_api.llm.llm_client.record_provider_attempt"), patch(
                "rag_api.llm.llm_client.requests.post", side_effect=requests.ReadTimeout("private provider body")
            ):
                trace = DiagnosticStageTrace(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
                generator = RAGGenerator(api_base="http://router.test/v1", api_key="private-key",
                                         model="test-model", timeout=2.5)
                with trace:
                    with trace.stage("generation"):
                        with self.assertRaises(LLMServiceUnavailable):
                            generator.generate_answer("private question", "  evidence secret  ", request_id=trace.request_id)
            serialized = path.read_text(encoding="utf-8")
            rows = [json.loads(line) for line in serialized.splitlines()]
        inputs = [row for row in rows if row["stage"] == "generation" and row["event"] == "input"]
        self.assertEqual(1, len(inputs))
        self.assertEqual(len("evidence secret"), inputs[0]["context_chars"])
        self.assertEqual(hashlib.sha256("evidence secret".encode()).hexdigest(), inputs[0]["context_sha256"])
        self.assertEqual(2.5, inputs[0]["provider_timeout_seconds"])
        self.assertEqual(2, inputs[0]["message_count"])
        self.assertNotIn("private", serialized)
        self.assertNotIn("evidence secret", serialized)

    def test_stage_events_are_allowlisted_and_written_as_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage_events.jsonl"
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(path),
            }):
                trace = DiagnosticStageTrace(str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4()))
                with trace:
                    with trace.stage("generation"):
                        pass
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["start", "start", "complete", "complete"], [row["event"] for row in rows])
        self.assertEqual({"request", "generation"}, {row["stage"] for row in rows})
        self.assertEqual(1, len({row["run_id"] for row in rows}))
        self.assertEqual(1, len({row["correlation_id"] for row in rows}))
        self.assertTrue(all(set(row).issubset({
            "schema_version", "timestamp_utc", "run_id", "correlation_id", "request_id",
            "stage", "event", "duration_ms", "error_class",
        }) for row in rows))

    def test_untrusted_fields_are_dropped_and_invalid_stage_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(path),
            }):
                append_stage_event({
                    "run_id": str(uuid.uuid4()), "correlation_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
                    "stage": "intent", "event": "error", "error_class": "TimeoutError",
                    "message": "secret api-key=do-not-log", "prompt": "private",
                })
                with self.assertRaises(ValueError):
                    append_stage_event({"stage": "arbitrary", "event": "start"})
            text = path.read_text(encoding="utf-8")
        self.assertNotIn("secret", text)
        self.assertNotIn("private", text)

    def test_disabled_trace_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trace.jsonl"
            with patch.dict(os.environ, {
                "RAG_DIAGNOSTIC_TRACE_ENABLED": "false",
                "RAG_DIAGNOSTIC_TRACE_FILE": str(path),
            }):
                trace = DiagnosticStageTrace(None, "corr", "req")
                with trace:
                    with trace.stage("intent"):
                        pass
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

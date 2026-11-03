"""Tests for the diagnostic outbound provider-attempt cap."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import requests

from rag_api.utils.provider_attempt_budget import (
    ProviderAttemptLimitReached,
    provider_run_headers,
    _configured_limit,
    record_provider_attempt,
    provider_attempt_count,
)
from rag_api.llm.intent_parser import IntentParser
from rag_api.llm.llm_client import RAGGenerator


class ProviderAttemptBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "attempts.jsonl"
        self.env = patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_LIMIT": "2",
            "RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_FILE": str(self.ledger),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_each_outbound_call_is_counted_and_limit_blocks_before_next_post(self):
        record_provider_attempt(stage="intent", request_id="12345678-1234-4234-8234-123456789abc")
        record_provider_attempt(stage="generation", request_id="12345678-1234-4234-8234-123456789abc")
        with self.assertRaises(ProviderAttemptLimitReached):
            record_provider_attempt(stage="intent", request_id="12345678-1234-4234-8234-123456789abc")
        self.assertEqual(2, provider_attempt_count(self.ledger))

    def test_invalid_ledger_fails_closed_and_records_no_secrets_or_prompt(self):
        self.ledger.write_text("not-json\n", encoding="utf-8")
        with self.assertRaises(ProviderAttemptLimitReached):
            record_provider_attempt(stage="intent", request_id="not-a-uuid")
        self.assertNotIn("secret", self.ledger.read_text(encoding="utf-8"))
        self.assertNotIn("private prompt", self.ledger.read_text(encoding="utf-8"))

    def test_unconfigured_profile_does_not_write_attempt_ledger(self):
        with patch.dict(os.environ, {"RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_LIMIT": "", "RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_FILE": ""}):
            self.assertIsNone(record_provider_attempt(stage="intent", request_id="12345678-1234-4234-8234-123456789abc"))
        self.assertFalse(self.ledger.exists())

    def test_intent_and_generation_transport_failures_remain_counted_as_post_attempts(self):
        request_id = "12345678-1234-4234-8234-123456789abc"
        with patch("rag_api.llm.intent_parser.requests.post",
                   side_effect=requests.ReadTimeout("synthetic timeout")) as intent_post:
            with self.assertRaises(Exception):
                IntentParser(api_base="http://proxy.test/v1", api_key="test-key",
                             model="test-model").parse_intent("question", request_id=request_id)
        self.assertEqual(1, intent_post.call_count)
        self.assertEqual(1, provider_attempt_count(self.ledger))

        with patch("rag_api.llm.llm_client.requests.post",
                   side_effect=requests.ConnectTimeout("synthetic timeout")) as generation_post:
            with self.assertRaises(Exception):
                RAGGenerator(api_base="http://proxy.test/v1", api_key="test-key",
                             model="test-model").generate_answer("question", "evidence", request_id=request_id)
        self.assertEqual(1, generation_post.call_count)
        self.assertEqual(2, provider_attempt_count(self.ledger))

    def test_provider_run_headers_require_active_trusted_run_and_proxy_route(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace
        run_id = "12345678-1234-4234-8234-123456789abc"
        request_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        token = "synthetic-internal-token"
        with patch.dict(os.environ, {
            "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
            "RAG_DIAGNOSTIC_RUN_ID": run_id,
            "RAG_DIAGNOSTIC_PROXY_TOKEN": token,
            "RAG_DIAGNOSTIC_PROXY_BASE_URL": "http://budget-proxy:20129/v1",
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(self.temp.name) / "trace.jsonl"),
        }):
            with DiagnosticStageTrace(run_id, request_id, request_id):
                headers = provider_run_headers(
                    endpoint="http://budget-proxy:20129/v1/chat/completions",
                    stage="intent", request_id=request_id,
                )
        self.assertEqual(run_id, headers["X-Evaluator-Run-ID"])
        self.assertEqual(token, headers["X-Diagnostic-Proxy-Token"])
        self.assertEqual("intent", headers["X-Evaluator-Stage"])
        self.assertEqual(request_id, headers["X-Evaluator-Request-ID"])

    def test_provider_run_headers_reject_untrusted_id_missing_token_and_direct_route(self):
        from rag_api.utils.diagnostic_stage_trace import DiagnosticStageTrace
        run_id = "12345678-1234-4234-8234-123456789abc"
        other_run_id = "abcdefab-cdef-4abc-8def-abcdefabcdef"
        request_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        base = {
            "RAG_DIAGNOSTIC_PROVIDER_RUN_ISOLATION_REQUIRED": "true",
            "RAG_DIAGNOSTIC_RUN_ID": run_id,
            "RAG_DIAGNOSTIC_PROXY_TOKEN": "synthetic-internal-token",
            "RAG_DIAGNOSTIC_PROXY_BASE_URL": "http://budget-proxy:20129/v1",
            "RAG_DIAGNOSTIC_TRACE_ENABLED": "true",
            "RAG_DIAGNOSTIC_TRACE_FILE": str(Path(self.temp.name) / "trace.jsonl"),
        }
        with patch.dict(os.environ, base):
            with DiagnosticStageTrace(other_run_id, request_id, request_id):
                with self.assertRaises(ProviderAttemptLimitReached):
                    provider_run_headers(endpoint="http://budget-proxy:20129/v1/chat/completions",
                                         stage="intent", request_id=request_id)
            with DiagnosticStageTrace(run_id, request_id, request_id):
                with patch.dict(os.environ, {"RAG_DIAGNOSTIC_PROXY_TOKEN": ""}):
                    with self.assertRaises(ProviderAttemptLimitReached):
                        provider_run_headers(endpoint="http://budget-proxy:20129/v1/chat/completions",
                                             stage="intent", request_id=request_id)
                with self.assertRaises(ProviderAttemptLimitReached):
                    provider_run_headers(endpoint="https://api.provider.example/v1/chat/completions",
                                         stage="intent", request_id=request_id)

    def test_parser_and_generator_block_before_requests_post_when_cap_is_spent(self):
        for stage in ("intent", "generation"):
            record_provider_attempt(stage=stage, request_id="12345678-1234-4234-8234-123456789abc")
        request_id = "12345678-1234-4234-8234-123456789abc"
        with patch("rag_api.llm.intent_parser.requests.post") as parser_post:
            with self.assertRaises(Exception):
                IntentParser(api_base="http://127.0.0.1:20128/v1", api_key="test-key",
                             model="test-model").parse_intent("question", request_id=request_id)
            parser_post.assert_not_called()
        with patch("rag_api.llm.llm_client.requests.post") as generator_post:
            with self.assertRaises(Exception):
                RAGGenerator(api_base="http://127.0.0.1:20128/v1", api_key="test-key",
                             model="test-model").generate_answer("question", "evidence", request_id=request_id)
            generator_post.assert_not_called()

    def test_zero_cap_blocks_parser_and_generator_before_any_post(self):
        with patch.dict(os.environ, {"RAG_DIAGNOSTIC_PROVIDER_ATTEMPT_LIMIT": "0"}):
            self.assertEqual(0, _configured_limit())
            request_id = "12345678-1234-4234-8234-123456789abc"
            with patch("rag_api.llm.intent_parser.requests.post") as parser_post:
                with self.assertRaises(Exception):
                    IntentParser(api_base="http://127.0.0.1:20128/v1", api_key="test-key",
                                 model="test-model").parse_intent("question", request_id=request_id)
                parser_post.assert_not_called()
            with patch("rag_api.llm.llm_client.requests.post") as generator_post:
                with self.assertRaises(Exception):
                    RAGGenerator(api_base="http://127.0.0.1:20128/v1", api_key="test-key",
                                 model="test-model").generate_answer("question", "evidence", request_id=request_id)
                generator_post.assert_not_called()
        self.assertEqual(0, provider_attempt_count(self.ledger))


if __name__ == "__main__":
    unittest.main()

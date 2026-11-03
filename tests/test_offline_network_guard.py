import threading
import os
import importlib.util
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests

from test_support.offline_network_guard import OfflineNetworkGuard, force_offline_llm_configuration
from rag_api.llm.intent_parser import IntentParser
from rag_api.llm.llm_client import RAGGenerator


class _OKHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args):
        pass


class OfflineNetworkGuardTests(unittest.TestCase):
    def test_offline_configuration_overrides_inherited_provider_endpoints(self):
        keys = ("LLM_API_BASE", "OPENAI_BASE_URL", "OPENAI_API_BASE", "EVALUATOR_BASE_URL",
                "PROVIDER_BASE_URL", "JUDGE_BASE_URL", "RAG_OFFLINE_TEST_MODE")
        previous = {key: os.environ.get(key) for key in keys}
        try:
            os.environ["LLM_API_BASE"] = "https://configured-provider.invalid/v1"
            force_offline_llm_configuration()
            for key in keys[:-1]:
                self.assertEqual("http://127.0.0.1:1/v1", os.environ[key])
            self.assertEqual("1", os.environ["RAG_OFFLINE_TEST_MODE"])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_configured_local_provider_endpoint_is_blocked_at_requests_boundary(self):
        endpoint = "http://127.0.0.1:1/v1"
        guard = OfflineNetworkGuard([endpoint])
        with guard:
            with self.assertRaisesRegex(RuntimeError, "configured provider endpoint"):
                requests.get(endpoint + "/chat/completions", timeout=0.1)
        self.assertEqual(1, guard.provider_attempts)
        self.assertEqual(0, guard.external_attempts)

    def test_offline_guard_blocks_intent_and_generation_client_requests(self):
        endpoint = "http://127.0.0.1:1/v1"
        guard = OfflineNetworkGuard([endpoint])
        with guard:
            with self.assertRaisesRegex(RuntimeError, "configured provider endpoint"):
                IntentParser(api_base=endpoint, api_key="test-only", model="test").parse_intent("question")
            with self.assertRaisesRegex(RuntimeError, "configured provider endpoint"):
                RAGGenerator(api_base=endpoint, api_key="test-only", model="test").generate_answer(
                    "question", "evidence"
                )
        self.assertEqual(2, guard.provider_attempts)

    @unittest.skipUnless(importlib.util.find_spec("httpx"), "HTTPX is not a Data Scrawler runtime dependency")
    def test_configured_provider_endpoint_is_blocked_at_httpx_boundary(self):
        import httpx

        endpoint = "http://127.0.0.1:1/v1"
        guard = OfflineNetworkGuard([endpoint])
        with guard:
            with self.assertRaisesRegex(RuntimeError, "configured provider endpoint"):
                httpx.get(endpoint + "/chat/completions", timeout=0.1)
        self.assertEqual(1, guard.provider_attempts)

    def test_local_fake_http_service_remains_available_when_explicitly_allowed(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _OKHandler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        endpoint = f"http://127.0.0.1:{server.server_port}"
        guard = OfflineNetworkGuard(["http://provider.example/v1"])
        guard.allow_local_endpoint(endpoint)
        try:
            with guard:
                response = requests.get(endpoint, timeout=1)
            self.assertEqual(200, response.status_code)
            self.assertEqual(b"ok", response.content)
            self.assertGreaterEqual(guard.local_requests, 1)
            guard.assert_clean()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

    def test_raw_socket_external_egress_is_blocked_and_counted(self):
        guard = OfflineNetworkGuard([])
        with guard:
            with self.assertRaisesRegex(OSError, "non-loopback"):
                import socket
                socket.socket.connect(None, ("203.0.113.10", 443))
        self.assertEqual(1, guard.external_attempts)

    def test_raw_udp_sendto_external_egress_is_blocked_and_counted(self):
        import socket

        guard = OfflineNetworkGuard([])
        with guard:
            with self.assertRaisesRegex(OSError, "non-loopback"):
                socket.socket.sendto(object(), b"test", ("203.0.113.10", 53))
        self.assertEqual(1, guard.external_attempts)

    def test_non_loopback_dns_resolution_is_blocked_and_counted(self):
        import socket

        guard = OfflineNetworkGuard([])
        with guard:
            with self.assertRaisesRegex(socket.gaierror, "name resolution"):
                socket.getaddrinfo("provider.example", 443)
        self.assertEqual(1, guard.external_attempts)

    def test_alternate_forward_and_reverse_resolvers_are_blocked(self):
        import socket

        guard = OfflineNetworkGuard([])
        with guard:
            for resolver_call in (
                lambda: socket.gethostbyname("provider.example"),
                lambda: socket.gethostbyname_ex("provider.example"),
                lambda: socket.gethostbyaddr("203.0.113.10"),
                lambda: socket.getnameinfo(("203.0.113.10", 443), 0),
            ):
                with self.assertRaisesRegex(socket.gaierror, "name resolution"):
                    resolver_call()
        self.assertEqual(4, guard.external_attempts)

    def test_loopback_reverse_lookup_is_blocked_and_nameinfo_stays_numeric(self):
        import socket

        guard = OfflineNetworkGuard([])
        with guard:
            with self.assertRaisesRegex(socket.gaierror, "name resolution"):
                socket.gethostbyaddr("127.0.0.1")
            host, _service = socket.getnameinfo(("127.0.0.1", 8000), 0)
        self.assertEqual("127.0.0.1", host)
        self.assertEqual(1, guard.external_attempts)

    def test_loopback_getfqdn_uses_numeric_name_without_reverse_dns(self):
        import socket

        guard = OfflineNetworkGuard([])
        with guard:
            self.assertEqual("127.0.0.1", socket.getfqdn("127.0.0.1"))
        self.assertEqual(0, guard.external_attempts)


if __name__ == "__main__":
    unittest.main()

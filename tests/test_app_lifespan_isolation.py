import unittest
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from rag_api import main_api


class AppLifespanIsolationTests(unittest.TestCase):
    def test_lifespan_preserves_explicitly_injected_services(self):
        injected_parser = Mock(name="injected-parser")
        injected_parser.parse_intent.return_value = {
            "intent": "unsupported", "status": "unsupported", "extracted_filters": {},
            "high_schools": [], "reply_to_user": "Unsupported fixture response",
        }
        injected_retriever = Mock(name="injected-retriever")
        injected_generator = Mock(name="injected-generator")

        with patch.object(main_api, "parser", injected_parser), \
                patch.object(main_api, "retriever", injected_retriever), \
                patch.object(main_api, "generator", injected_generator), \
                patch.object(main_api, "IntentParser") as parser_factory, \
                patch.object(main_api, "Neo4jRetriever") as retriever_factory, \
                patch.object(main_api, "RAGGenerator") as generator_factory:
            with TestClient(main_api.app) as client:
                self.assertIs(main_api.parser, injected_parser)
                self.assertIs(main_api.retriever, injected_retriever)
                self.assertIs(main_api.generator, injected_generator)
                response = client.post(
                    "/api/chat", json={"user_message": "unrelated request", "context": []},
                    headers={"X-Diagnostic-Run-ID": str(uuid.uuid4())},
                )
                self.assertEqual(200, response.status_code, response.text)

            parser_factory.assert_not_called()
            retriever_factory.assert_not_called()
            generator_factory.assert_not_called()
            injected_retriever.get_driver.assert_not_called()
            injected_retriever.close.assert_not_called()
            injected_parser.parse_intent.assert_called_once()

    def test_lifespan_creates_and_closes_only_services_it_owns(self):
        owned_parser = Mock(name="owned-parser")
        owned_retriever = Mock(name="owned-retriever")
        owned_generator = Mock(name="owned-generator")
        with patch.object(main_api, "parser", None), \
                patch.object(main_api, "retriever", None), \
                patch.object(main_api, "generator", None), \
                patch.object(main_api, "IntentParser", return_value=owned_parser) as parser_factory, \
                patch.object(main_api, "Neo4jRetriever", return_value=owned_retriever) as retriever_factory, \
                patch.object(main_api, "RAGGenerator", return_value=owned_generator) as generator_factory:
            with TestClient(main_api.app):
                self.assertIs(main_api.parser, owned_parser)
                self.assertIs(main_api.retriever, owned_retriever)
                self.assertIs(main_api.generator, owned_generator)

            parser_factory.assert_called_once_with()
            retriever_factory.assert_called_once_with()
            generator_factory.assert_called_once_with()
            owned_retriever.get_driver.assert_called_once_with()
            owned_retriever.close.assert_called_once_with()
            self.assertIsNone(main_api.parser)
            self.assertIsNone(main_api.retriever)
            self.assertIsNone(main_api.generator)

    def test_concurrent_requests_keep_parser_inputs_isolated_during_lifespan(self):
        barrier = threading.Barrier(2)
        seen = []
        lock = threading.Lock()

        class ConcurrentParser:
            def parse_intent(self, user_message, context, **_kwargs):
                barrier.wait(timeout=3)
                with lock:
                    seen.append((user_message, list(context)))
                return {"intent": "unsupported", "status": "unsupported",
                        "extracted_filters": {}, "high_schools": []}

            @staticmethod
            def is_ready_for_db(_intent):
                return False

        injected_parser = ConcurrentParser()
        with patch.object(main_api, "parser", injected_parser), \
                patch.object(main_api, "retriever", Mock()), \
                patch.object(main_api, "generator", Mock()), \
                patch.object(main_api, "IntentParser") as parser_factory, \
                patch.object(main_api, "Neo4jRetriever") as retriever_factory, \
                patch.object(main_api, "RAGGenerator") as generator_factory:
            with TestClient(main_api.app) as client:
                def send(question, prior):
                    return client.post("/api/chat", json={
                        "user_message": question,
                        "context": [{"role": "user", "content": prior}],
                    })

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(send, "question-a", "history-a"),
                               pool.submit(send, "question-b", "history-b")]
                    responses = [future.result(timeout=5) for future in futures]

        self.assertEqual([200, 200], [response.status_code for response in responses])
        self.assertEqual({("question-a", (("user", "history-a"),)),
                          ("question-b", (("user", "history-b"),))}, set(
            (question, tuple((item["role"], item["content"]) for item in history))
            for question, history in seen
        ))
        parser_factory.assert_not_called()
        retriever_factory.assert_not_called()
        generator_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

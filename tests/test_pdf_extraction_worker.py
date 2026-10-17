import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests
from botocore.exceptions import ClientError
from PIL import Image

from init_object_storage import ensure_bucket
from pdf_extraction_worker import (
    EXTRACTION_SYSTEM_PROMPT,
    ExtractionFailure,
    _extract_image_text,
    _llm_extract,
    _llm_profile,
    _request_groups,
    _document_status_after_extraction,
    _download_object,
    _storage_client,
    validate_groups,
)


class PdfExtractionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.allowed = {"MATH", "LITERATURE", "PHYSICS", "CHEMISTRY", "BIOLOGY"}

    def test_extraction_preserves_document_delete_states(self):
        self.assertIsNone(_document_status_after_extraction("DELETE_REQUESTED", "DRAFT_READY"))
        self.assertIsNone(_document_status_after_extraction("DELETED", "FAILED"))
        self.assertEqual(_document_status_after_extraction("PROCESSING", "DRAFT_READY"), "DRAFT_READY")

    @patch.dict("os.environ", {
        "OBJECT_STORAGE_PROVIDER": "MINIO",
        "OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
        "OBJECT_STORAGE_BUCKET": "documents",
        "OBJECT_STORAGE_REGION": "us-east-1",
        "OBJECT_STORAGE_ACCESS_KEY_ID": "access",
        "OBJECT_STORAGE_SECRET_ACCESS_KEY": "secret",
    }, clear=True)
    @patch("boto3.client")
    def test_storage_client_uses_generic_s3_settings_and_path_addressing(self, client):
        _storage_client()

        kwargs = client.call_args.kwargs
        self.assertEqual(kwargs["endpoint_url"], "http://minio:9000")
        self.assertEqual(kwargs["region_name"], "us-east-1")
        self.assertEqual(kwargs["config"].s3["addressing_style"], "path")

    @patch.dict("os.environ", {
        "R2_ENDPOINT": "https://old-r2.example",
        "R2_BUCKET": "old-bucket",
        "R2_ACCESS_KEY_ID": "old-key",
        "R2_SECRET_ACCESS_KEY": "old-secret",
    }, clear=True)
    def test_storage_client_does_not_fall_back_to_legacy_r2_variables(self):
        with self.assertRaisesRegex(RuntimeError, "OBJECT_STORAGE_ENDPOINT"):
            _storage_client()

    def test_missing_object_is_reported_as_terminal_storage_failure(self):
        client = Mock()
        client.download_file.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "GetObject",
        )

        with self.assertRaises(ExtractionFailure) as result:
            _download_object(client, "documents", "missing.pdf", "missing.pdf")

        self.assertEqual(result.exception.code, "STORAGE_OBJECT_MISSING")
        self.assertFalse(result.exception.retryable)

    def test_bucket_initialization_is_idempotent_for_existing_bucket(self):
        client = Mock()

        ensure_bucket(client, "documents")

        client.head_bucket.assert_called_once_with(Bucket="documents")
        client.create_bucket.assert_not_called()

    def test_bucket_initialization_creates_bucket_when_missing(self):
        client = Mock()
        client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadBucket",
        )

        ensure_bucket(client, "documents")

        client.create_bucket.assert_called_once_with(Bucket="documents")

    @patch("pdf_extraction_worker.settings")
    @patch("pdf_extraction_worker.psycopg.connect")
    def test_job_claim_skips_document_that_entered_delete_request(self, connect, settings):
        settings.DATABASE_URL = "postgresql://test"
        connection = MagicMock()
        connect.return_value.__enter__.return_value = connection
        connection.transaction.return_value.__enter__.return_value = None
        candidate = Mock()
        candidate.fetchone.return_value = {"job_id": "job", "version_id": "version", "object_key": "key",
                                           "content_type": "application/pdf", "document_id": "document"}
        locked_document = Mock()
        locked_document.fetchone.return_value = {"status": "DELETE_REQUESTED"}
        connection.execute.side_effect = [candidate, locked_document]

        from pdf_extraction_worker import _claim_job
        self.assertIsNone(_claim_job())
        self.assertEqual(connection.execute.call_count, 2)

    @patch("pdf_extraction_worker.settings")
    @patch("pdf_extraction_worker.psycopg.connect")
    def test_processing_stops_when_document_is_deleted(self, connect, settings):
        settings.DATABASE_URL = "postgresql://test"
        connection = MagicMock()
        connect.return_value.__enter__.return_value = connection
        connection.transaction.return_value.__enter__.return_value = None
        document = Mock()
        document.fetchone.return_value = ("DELETED",)
        current_job = Mock()
        current_job.fetchone.return_value = ("CANCELLED",)
        connection.execute.side_effect = [document, current_job]

        from pdf_extraction_worker import _process
        _process({"job_id": "job", "version_id": "version", "document_id": "document"})

        self.assertEqual(connection.execute.call_count, 2)

    @patch("pdf_extraction_worker.requests.get")
    @patch("pdf_extraction_worker.settings")
    def test_llm_profile_uses_only_environment_configuration(self, settings, get):
        settings.LLM_API_KEY = "environment-key"
        settings.LLM_API_BASE = "http://host.docker.internal:20128/v1"
        settings.LLM_MODEL = "text-model"

        self.assertEqual(_llm_profile(), (
            "environment-key", "http://host.docker.internal:20128/v1", "text-model"
        ))
        get.assert_not_called()

    @patch("pdf_extraction_worker.settings")
    def test_llm_profile_fails_clearly_when_environment_is_incomplete(self, settings):
        settings.LLM_API_KEY = "environment-key"
        settings.LLM_API_BASE = ""
        settings.LLM_MODEL = "text-model"
        with self.assertRaises(ExtractionFailure) as raised:
            _llm_profile()
        self.assertEqual(raised.exception.code, "LLM_CONFIG_MISSING")

    def test_accepts_subject_groups_with_one_to_four_catalog_subjects(self):
        groups = [
            {"group_code": "A01", "group_name": "Nhóm A01", "subject_codes": ["MATH"], "page_numbers": [1]},
            {"group_code": "A02", "group_name": "Nhóm A02", "subject_codes": ["MATH", "LITERATURE"], "page_numbers": [1]},
            {"group_code": "A03", "group_name": "Nhóm A03", "subject_codes": ["MATH", "LITERATURE", "PHYSICS"], "page_numbers": [2]},
            {"group_code": "A04", "group_name": "Nhóm A04",
             "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "CHEMISTRY"], "page_numbers": [2, 1, 2]},
        ]

        result = validate_groups(groups, self.allowed, 3)

        self.assertEqual([len(group["subjects"]) for group in result], [1, 2, 3, 4])
        self.assertEqual(result[-1]["pages"], [1, 2])

    def test_rejects_empty_five_subject_and_duplicate_subject_groups(self):
        invalid = [
            {"group_code": "EMPTY", "group_name": "Empty", "subject_codes": [], "page_numbers": [1]},
            {"group_code": "FIVE", "group_name": "Five",
             "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "CHEMISTRY", "BIOLOGY"], "page_numbers": [1]},
            {"group_code": "DUP", "group_name": "Duplicate", "subject_codes": ["MATH", "MATH"], "page_numbers": [1]},
        ]

        with self.assertRaisesRegex(ValueError, "NO_VALID_GROUPS"):
            validate_groups(invalid, self.allowed, 1)

    def test_rejects_unknown_subject_instead_of_trusting_model(self):
        groups = [{"group_code": "A", "group_name": "Nhóm A",
                   "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "UNKNOWN"],
                   "page_numbers": [1]}]

        with self.assertRaisesRegex(ValueError, "NO_VALID_GROUPS"):
            validate_groups(groups, self.allowed, 1)

    def test_rejects_duplicate_subjects_and_out_of_range_page(self):
        duplicate = {"group_code": "A", "group_name": "Nhóm A",
                     "subject_codes": ["MATH", "MATH", "PHYSICS", "CHEMISTRY"],
                     "page_numbers": [1]}
        bad_page = {"group_code": "B", "group_name": "Nhóm B",
                    "subject_codes": ["MATH", "LITERATURE", "PHYSICS", "CHEMISTRY"],
                    "page_numbers": [4]}

        with self.assertRaisesRegex(ValueError, "NO_VALID_GROUPS"):
            validate_groups([duplicate, bad_page], self.allowed, 3)

    def test_system_prompt_separates_elective_and_specialized_subject_columns(self):
        self.assertIn("Tổ hợp môn lựa chọn", EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("Bỏ qua hoàn toàn cột “Chuyên đề học tập”", EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("Không tự điền môn bị thiếu", EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("từ một đến bốn môn học khác nhau", EXTRACTION_SYSTEM_PROMPT)
        self.assertIn("JSON object", EXTRACTION_SYSTEM_PROMPT)

    @patch("pdf_extraction_worker.subprocess.run")
    def test_image_is_normalized_and_ocr_uses_vietnamese_and_english(self, run):
        run.return_value.stdout = "01 | Lý, Hóa, Sinh, Tin học"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.webp"
            Image.new("RGB", (32, 24), "white").save(path, format="WEBP")

            self.assertEqual(_extract_image_text(path), "01 | Lý, Hóa, Sinh, Tin học")

        command = run.call_args.args[0]
        self.assertEqual(command[0], "tesseract")
        self.assertIn("vie+eng", command)
        self.assertEqual(command[command.index("--psm") + 1], "3")
        self.assertIn("preserve_interword_spaces=1", command)
        self.assertTrue(run.call_args.kwargs["check"])
        self.assertGreater(run.call_args.kwargs["timeout"], 0)

    @patch("pdf_extraction_worker.subprocess.run")
    def test_image_ocr_empty_result_is_terminal(self, run):
        run.return_value.stdout = "  \n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.png"
            Image.new("RGB", (16, 16), "white").save(path)

            with self.assertRaises(ExtractionFailure) as result:
                _extract_image_text(path)

        self.assertEqual(result.exception.code, "OCR_EMPTY")
        self.assertFalse(result.exception.retryable)

    @patch("pdf_extraction_worker.subprocess.run", side_effect=subprocess.TimeoutExpired("tesseract", 60))
    def test_image_ocr_timeout_is_retryable(self, _run):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.png"
            Image.new("RGB", (16, 16), "white").save(path)

            with self.assertRaises(ExtractionFailure) as result:
                _extract_image_text(path)

        self.assertEqual(result.exception.code, "OCR_TIMEOUT")
        self.assertTrue(result.exception.retryable)

    @patch("pdf_extraction_worker.subprocess.run", side_effect=FileNotFoundError("tesseract"))
    def test_image_ocr_engine_failure_is_terminal(self, _run):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.png"
            Image.new("RGB", (16, 16), "white").save(path)

            with self.assertRaises(ExtractionFailure) as result:
                _extract_image_text(path)

        self.assertEqual(result.exception.code, "OCR_ENGINE_ERROR")
        self.assertFalse(result.exception.retryable)

    def test_empty_text_does_not_send_a_request_to_llm(self):
        with patch("pdf_extraction_worker._allowed_subjects") as catalog, \
             patch("pdf_extraction_worker._request_groups") as request:
            with self.assertRaisesRegex(ExtractionFailure, "OCR_EMPTY"):
                _llm_extract(["", "  "])

        catalog.assert_not_called()
        request.assert_not_called()

    @patch("pdf_extraction_worker._llm_profile", return_value=("test-key", "https://example.test/v1", "claude"))
    @patch("pdf_extraction_worker.requests.post")
    def test_text_request_uses_system_prompt_and_validates_groups(self, post, _profile):
        post.return_value.json.return_value = {"choices": [{"message": {"content":
            '{"groups":[{"group_code":"01","group_name":"Nhóm 01","subject_codes":'
            '["MATH","LITERATURE","PHYSICS","CHEMISTRY"],"page_numbers":[1]}]}'}}]}
        result = _request_groups({"pages": [{"page": 1, "text": "OCR text"}]}, self.allowed, 1)

        request = post.call_args.kwargs["json"]
        self.assertIs(request["stream"], False)
        self.assertEqual(request["messages"][0]["role"], "system")
        self.assertIn("Chuyên đề học tập", request["messages"][0]["content"])
        self.assertEqual(request["messages"][1]["role"], "user")
        self.assertEqual(json.loads(request["messages"][1]["content"])["pages"][0]["text"], "OCR text")
        self.assertEqual(result[0]["code"], "01")
        self.assertNotIn("image_url", str(request))

    @patch("pdf_extraction_worker._llm_profile", return_value=("test-key", "https://example.test/v1", "claude"))
    @patch("pdf_extraction_worker.requests.post")
    def test_llm_timeout_is_retryable_and_http_400_is_terminal(self, post, _profile):
        post.side_effect = requests.Timeout()
        with self.assertRaises(ExtractionFailure) as timeout:
            _request_groups({}, self.allowed, 1)
        self.assertEqual(timeout.exception.code, "LLM_TIMEOUT")
        self.assertTrue(timeout.exception.retryable)

        response = Mock(status_code=400)
        post.side_effect = requests.HTTPError(response=response)
        with self.assertRaises(ExtractionFailure) as invalid_request:
            _request_groups({}, self.allowed, 1)
        self.assertEqual(invalid_request.exception.code, "LLM_HTTP_400")
        self.assertFalse(invalid_request.exception.retryable)

    @patch("pdf_extraction_worker._llm_profile", return_value=("test-key", "https://example.test/v1", "claude"))
    @patch("pdf_extraction_worker.requests.post")
    def test_invalid_json_does_not_create_a_group(self, post, _profile):
        post.return_value.json.return_value = {"choices": [{"message": {"content": "not json"}}]}
        with self.assertRaisesRegex(ExtractionFailure, "INVALID_LLM_SCHEMA"):
            _request_groups({}, self.allowed, 1)

    @patch("pdf_extraction_worker._llm_profile", return_value=("test-key", "https://example.test/v1", "claude"))
    @patch("pdf_extraction_worker.requests.post")
    def test_no_valid_groups_is_terminal(self, post, _profile):
        post.return_value.json.return_value = {"choices": [{"message": {"content": '{"groups":[]}'}}]}
        with self.assertRaises(ExtractionFailure) as result:
            _request_groups({}, self.allowed, 1)
        self.assertEqual(result.exception.code, "NO_VALID_GROUPS")
        self.assertFalse(result.exception.retryable)


if __name__ == "__main__":
    unittest.main()

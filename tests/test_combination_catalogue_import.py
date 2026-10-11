"""Exercise the importer transaction order without a live database write."""

from contextlib import nullcontext
import unittest
from unittest.mock import patch

from import_combination_labels import REQUIRED_COLUMNS, import_snapshot


class FakeCursor:
    def __init__(self):
        self.statements = []
        self.result = []
        self.single = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.statements.append(query)
        if "information_schema.columns" in query:
            self.result = [(table, column) for table, columns in REQUIRED_COLUMNS.items()
                           for column in columns]
        elif "SELECT MAX(retrieved_at)" in query:
            self.single = (None,)
        elif "FROM admission_combinations c" in query:
            self.result = []
        elif "SELECT combination_id FROM admission_combinations" in query:
            self.result = []
        elif "SELECT code, name FROM subjects" in query:
            self.result = [
                ("MATH", "Toán"), ("LITERATURE", "Ngữ văn"),
                ("PHYSICS", "Vật lí"), ("CHEMISTRY", "Hóa học"),
                ("BIOLOGY", "Sinh học"), ("INFORMATICS", "Tin học"),
            ]
        elif "RETURNING combination_id" in query:
            self.single = (params[0],)

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.single


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.read_only = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def transaction(self):
        return nullcontext()

    def cursor(self):
        return self.cursor_instance


class CatalogueImportTests(unittest.TestCase):
    def test_dry_run_does_not_write_and_write_path_upserts_concrete_rows(self):
        rows = [{"combination_code": "K01", "subject_details": "Toán, Văn, Lý/Hóa/Sinh/Tin"}]
        manifest = {
            "source_url": "https://example.edu/combination",
            "source_period": "2026-2027",
            "retrieved_at": "2026-10-11T00:00:00+00:00",
        }
        dry_connection = FakeConnection()
        with patch("psycopg.connect", return_value=dry_connection):
            reasons, combinations, row_count, linked_count, subjects = import_snapshot(
                rows, manifest, "unused", dry_run=True, normalize_catalogue=True,
            )
        self.assertFalse(reasons)
        self.assertEqual((combinations, row_count, linked_count, subjects), (4, 5, 4, 0))
        self.assertTrue(dry_connection.read_only)
        self.assertFalse(any("INSERT INTO" in sql for sql in dry_connection.cursor_instance.statements))

        write_connection = FakeConnection()
        with patch("psycopg.connect", return_value=write_connection):
            import_snapshot(rows, manifest, "unused", normalize_catalogue=True)
        inserts = [sql for sql in write_connection.cursor_instance.statements if "INSERT INTO" in sql]
        self.assertEqual(sum("INSERT INTO admission_combinations" in sql for sql in inserts), 4)
        self.assertEqual(sum("INSERT INTO admission_combination_components" in sql for sql in inserts), 12)
        self.assertEqual(sum("INSERT INTO admission_combination_labels" in sql for sql in inserts), 5)


if __name__ == "__main__":
    unittest.main()

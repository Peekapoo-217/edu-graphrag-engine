#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tests/test_importer.py

Unit tests for importer safety, DDL absence, and config lookup.
"""

import os
import unittest
from import_to_postgres import load_university_config


class TestImporter(unittest.TestCase):

    def test_no_ddl_in_importer(self):
        """Test importer source code does NOT contain DDL or destructive statements."""
        importer_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "import_to_postgres.py"
        )
        with open(importer_path, "r", encoding="utf-8") as f:
            code = f.read().upper()

        forbidden_tokens = [
            "CREATE TABLE",
            "DROP TABLE",
            "TRUNCATE",
            "DELETE FROM",
            "ALTER TABLE",
            "INIT_SCHEMA",
            "SCHEMA_POSTGRES.SQL",
        ]

        for token in forbidden_tokens:
            self.assertNotIn(
                token,
                code,
                f"Forbidden DDL/Destructive token '{token}' found in import_to_postgres.py"
            )

    def test_university_config_lookup(self):
        """Test university config is loaded dynamically from universities.json."""
        config = load_university_config("DQN")
        self.assertEqual(config["university_code"], "DQN")
        self.assertEqual(config["university_name"], "Trường Đại học Quy Nhơn")
        self.assertEqual(config["crawler"], "qnu")

    def test_deleted_schema_sql_file(self):
        """Test schema_postgres.sql does not exist in workspace."""
        schema_file = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "schema_postgres.sql"
        )
        self.assertFalse(os.path.exists(schema_file), "schema_postgres.sql must be deleted!")


if __name__ == "__main__":
    unittest.main()

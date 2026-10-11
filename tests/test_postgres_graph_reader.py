"""Checks the deployed V1 PostgreSQL contract used by the graph projection."""

import unittest
from unittest.mock import MagicMock

from graph_sync.postgres_reader import REQUIRED_POSTGRES_SCHEMA, fetch_all_graph_data_from_postgres


class PostgresGraphReaderTests(unittest.TestCase):
    def test_offer_combination_join_and_cutoff_mapping(self):
        queries = []
        cursor = MagicMock()

        def execute(query):
            queries.append(query)
            if "FROM subjects ORDER" in query:
                rows = [("MATH", "Toán", "ACADEMIC", True, None)]
            elif "FROM high_schools ORDER" in query:
                rows = []
            elif "FROM subject_groups ORDER" in query:
                rows = []
            elif "FROM universities ORDER" in query:
                rows = [("DQN", "Đại học Quy Nhơn", None)]
            elif "FROM admission_offers o" in query:
                rows = [
                    ("DQN", 2026, "101", "Ngành 101", "100", 50, "MATH_SET", "https://example.edu/offer"),
                    ("DQN", 2026, "101", "Ngành 101", "100", 50, "MATH_SET_2", "https://example.edu/offer"),
                ]
            elif "FROM admission_combinations ORDER" in query:
                rows = [("MATH_SET", "Toán", 1), ("MATH_SET_2", "Toán khác", 1)]
            elif "FROM admission_combination_components" in query:
                rows = [
                    ("MATH_SET", "MATH", "Toán", "SUBJECT", "MATH", 1),
                    ("MATH_SET_2", "MATH", "Toán", "SUBJECT", "MATH", 1),
                ]
            elif "FROM cutoff_scores c" in query:
                rows = [("DQN", 2025, "101", "Ngành 101", "100", 25, None, "https://example.edu/cutoff")]
            else:
                raise AssertionError(f"Unexpected query: {query}")
            cursor.fetchall.return_value = rows

        cursor.execute.side_effect = execute
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        data = fetch_all_graph_data_from_postgres(connection)

        self.assertIn("majors", REQUIRED_POSTGRES_SCHEMA)
        self.assertIn("offer_combinations", REQUIRED_POSTGRES_SCHEMA)
        self.assertEqual(len(data["nodes"]["TrainingProgram"]), 1)
        self.assertEqual(len(data["relationships"]["ACCEPTS_COMBINATION"]), 2)
        self.assertEqual(data["nodes"]["TrainingProgram"][0]["name"], "Ngành 101")
        self.assertEqual(data["nodes"]["CutoffScore"][0]["uid"], "DQN|2025|101|100|OFFICIAL_CUTOFF")
        self.assertTrue(any("JOIN offer_combinations oc ON oc.offer_id = o.id" in query for query in queries))
        self.assertTrue(any("JOIN majors m ON m.program_code = c.program_code" in query for query in queries))


if __name__ == "__main__":
    unittest.main()

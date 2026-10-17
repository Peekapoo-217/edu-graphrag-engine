import unittest
from unittest.mock import MagicMock, patch

from graph_sync.projection_lock import GRAPH_PROJECTION_LOCK_KEY, graph_projection_lock


class ProjectionLockTests(unittest.TestCase):
    @patch("graph_sync.projection_lock.psycopg.connect")
    def test_acquires_and_releases_the_same_postgres_advisory_lock(self, connect):
        connection = MagicMock()
        connect.return_value.__enter__.return_value = connection

        with graph_projection_lock("postgresql://test"):
            self.assertEqual(connection.execute.call_count, 1)

        self.assertEqual(connection.execute.call_count, 2)
        acquire_sql, acquire_params = connection.execute.call_args_list[0].args
        release_sql, release_params = connection.execute.call_args_list[1].args
        self.assertIn("pg_advisory_lock", acquire_sql)
        self.assertIn("pg_advisory_unlock", release_sql)
        self.assertEqual(acquire_params, (GRAPH_PROJECTION_LOCK_KEY,))
        self.assertEqual(release_params, acquire_params)
        self.assertEqual(connection.commit.call_count, 2)


if __name__ == "__main__":
    unittest.main()

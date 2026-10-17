import unittest
from contextlib import nullcontext
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import MagicMock, patch

from graph_projection_worker import build_group_projection, delete_school_projection, run_once


def subject_row(group_code, subject_code):
    return {
        "school_name": "Trường Tôn Đức Thắng",
        "location": None,
        "group_code": group_code,
        "group_name": group_code,
        "subject_code": subject_code,
        "subject_name": subject_code,
        "subject_type": "ACADEMIC",
        "is_mandatory": False,
        "canonical_subject_code": None,
    }


class GraphProjectionWorkerTests(unittest.TestCase):
    def test_builds_projection_for_groups_with_one_and_three_subjects(self):
        rows = [
            subject_row("A01", "MATH"),
            subject_row("A02", "MATH"),
            subject_row("A02", "LITERATURE"),
            subject_row("A02", "PHYSICS"),
        ]

        graph = build_group_projection(rows, "SCHOOL", "2026-2027")

        self.assertEqual(len(graph["nodes"]["SubjectGroup"]), 2)
        self.assertEqual(len(graph["relationships"]["INCLUDES_SUBJECT"]), 4)
        self.assertEqual(graph["nodes"]["SubjectGroup"][0]["uid"], "SCHOOL|2026-2027|A01")

    def test_projection_preserves_publication_state_and_revision(self):
        row = subject_row("A01", "MATH")
        row["is_published"] = True
        row["publication_revision"] = 7

        graph = build_group_projection([row], "SCHOOL", "2026-2027")

        group = graph["nodes"]["SubjectGroup"][0]
        self.assertTrue(group["is_published"])
        self.assertEqual(group["publication_revision"], 7)
        offers_group = graph["relationships"]["OFFERS_GROUP"][0]
        self.assertTrue(offers_group["is_published"])
        self.assertEqual(offers_group["publication_revision"], 7)

    def test_unpublished_group_is_emitted_as_delete_tombstone_without_nodes_or_edges(self):
        row = subject_row("A01", "MATH")
        row["is_published"] = False
        row["publication_revision"] = 8

        graph = build_group_projection([row], "SCHOOL", "2026-2027")

        self.assertEqual(graph["nodes"]["SubjectGroup"], [])
        self.assertEqual(graph["nodes"]["Subject"], [])
        self.assertEqual(graph["relationships"]["OFFERS_GROUP"], [])
        self.assertEqual(graph["relationships"]["INCLUDES_SUBJECT"], [])
        self.assertEqual(graph["unpublished_subject_groups"], [{
            "uid": "SCHOOL|2026-2027|A01", "school_code": "SCHOOL",
            "academic_year": "2026-2027", "group_code": "A01", "publication_revision": 8,
        }])

    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    def test_active_school_missing_expected_group_is_not_treated_as_a_success(self, settings, connect):
        from graph_projection_worker import _load_projection_data

        settings.DATABASE_URL = "postgresql://test"
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(fetchall=MagicMock(return_value=[])),
            MagicMock(fetchone=MagicMock(return_value={"deleted_at": None})),
        ]
        connect.return_value.__enter__.return_value = connection

        with self.assertRaisesRegex(ValueError, "PUBLISHED_GROUPS_MISSING"):
            _load_projection_data({"school_code": "SCHOOL", "academic_year": "2026",
                                   "group_codes": ["A01"]})

    def test_rejects_empty_graph_result(self):
        with self.assertRaisesRegex(ValueError, "PUBLISHED_GROUPS_MISSING"):
            build_group_projection([], "SCHOOL", "2026-2027")

    def test_rejects_a_published_group_with_more_than_four_subjects(self):
        rows = [subject_row("A01", code) for code in ("MATH", "LITERATURE", "PHYSICS", "CHEMISTRY", "BIOLOGY")]
        with self.assertRaisesRegex(ValueError, "PUBLISHED_GROUP_SUBJECT_COUNT_INVALID"):
            build_group_projection(rows, "SCHOOL", "2026-2027")

    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    def test_school_projection_merges_all_active_academic_years(self, settings, connect):
        from graph_projection_worker import _load_school_projection_data

        settings.DATABASE_URL = "postgresql://test"
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value={"code": "SCHOOL", "name": "School", "location": "Province"})),
            MagicMock(fetchall=MagicMock(return_value=[
                {"school_name": "School", "location": "Province", "academic_year": "2025-2026",
                 "group_code": "A01", "group_name": "A01", "subject_code": "MATH",
                 "subject_name": "Math", "subject_type": "ACADEMIC", "is_mandatory": False,
                 "canonical_subject_code": None},
                {"school_name": "School", "location": "Province", "academic_year": "2026-2027",
                 "group_code": "A01", "group_name": "A01", "subject_code": "LITERATURE",
                 "subject_name": "Literature", "subject_type": "ACADEMIC", "is_mandatory": False,
                 "canonical_subject_code": None},
            ])),
        ]
        connect.return_value.__enter__.return_value = connection

        data = _load_school_projection_data({"school_code": "SCHOOL"})

        self.assertEqual(len(data["nodes"]["HighSchool"]), 1)
        self.assertEqual({node["academic_year"] for node in data["nodes"]["SubjectGroup"]},
                         {"2025-2026", "2026-2027"})
        self.assertEqual(len(data["relationships"]["OFFERS_GROUP"]), 2)

    @patch("graph_projection_worker.graph_projection_lock", return_value=nullcontext())
    @patch("graph_projection_worker.sync_nodes_and_relationships")
    @patch("graph_projection_worker._load_projection_data", return_value={"nodes": {}, "relationships": {}})
    @patch("graph_projection_worker._claim_job")
    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    @patch("graph_projection_worker._claim_school_job", return_value=None)
    def test_marks_projection_job_synced_after_success(
            self, _school_claim, settings, connect, claim, load, sync, projection_lock):
        settings.DATABASE_URL = "postgresql://test"
        settings.NEO4J_DATABASE = "edugraph"
        claim.return_value = {"id": "job-id", "school_code": "SCHOOL", "academic_year": "2026", "group_codes": ["A01"], "attempt_count": 1}
        connection = MagicMock()
        connect.return_value.__enter__.return_value = connection

        output = StringIO()
        with redirect_stdout(output):
            self.assertTrue(run_once(MagicMock()))

        self.assertIn("status='SYNCED'", connection.execute.call_args.args[0])
        sync.assert_called_once()
        self.assertIn("[graph-sync] claimed job=job-id", output.getvalue())
        self.assertIn("[graph-sync] synced job=job-id", output.getvalue())
        projection_lock.assert_called_once_with("postgresql://test")

    @patch("graph_projection_worker.graph_projection_lock", return_value=nullcontext())
    @patch("graph_projection_worker._load_projection_data", side_effect=RuntimeError("connection secret must not leak"))
    @patch("graph_projection_worker._claim_job")
    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    @patch("graph_projection_worker._claim_school_job", return_value=None)
    def test_retries_projection_failure_without_storing_provider_error(
            self, _school_claim, settings, connect, claim, _load, _projection_lock):
        settings.DATABASE_URL = "postgresql://test"
        claim.return_value = {"id": "job-id", "attempt_count": 1}
        connection = MagicMock()
        connect.return_value.__enter__.return_value = connection

        self.assertTrue(run_once(MagicMock()))

        sql, params = connection.execute.call_args.args
        self.assertIn("status='RETRY'", sql)
        self.assertEqual(params, (5, "job-id"))
        self.assertNotIn("connection secret", str(connection.execute.call_args))

    @patch("graph_projection_worker.graph_projection_lock", return_value=nullcontext())
    @patch("graph_projection_worker.sync_nodes_and_relationships")
    @patch("graph_projection_worker._load_school_projection_data", return_value={
        "nodes": {"HighSchool": [{"code": "NEW"}]}, "relationships": {}
    })
    @patch("graph_projection_worker.delete_school_projection")
    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    def test_school_code_change_deletes_old_code_then_upserts_current_projection(
            self, settings, connect, delete, _load, sync, _projection_lock):
        from graph_projection_worker import _run_school_job

        settings.DATABASE_URL = "postgresql://test"
        settings.NEO4J_DATABASE = "edugraph"
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value={"code": "NEW", "deleted_at": None})),
            MagicMock(),
        ]
        connect.return_value.__enter__.return_value = connection
        driver = MagicMock()
        job = {"id": "job-id", "school_code": "NEW", "previous_code": "OLD",
               "operation": "UPSERT", "attempt_count": 1}

        self.assertTrue(_run_school_job(driver, job))

        delete.assert_called_once_with(driver, "edugraph", ["OLD"])
        sync.assert_called_once_with(driver, "edugraph", {"nodes": {"HighSchool": [{"code": "NEW"}]}, "relationships": {}})
        self.assertIn("status='SYNCED'", connection.execute.call_args.args[0])

    @patch("graph_projection_worker.graph_projection_lock", return_value=nullcontext())
    @patch("graph_projection_worker.sync_nodes_and_relationships")
    @patch("graph_projection_worker._load_school_projection_data", return_value={
        "nodes": {"HighSchool": [{"code": "SCHOOL"}]}, "relationships": {}
    })
    @patch("graph_projection_worker.delete_school_projection")
    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    def test_stale_delete_job_reconciles_active_school_as_upsert(
            self, settings, connect, delete, _load, sync, _projection_lock):
        from graph_projection_worker import _run_school_job

        settings.DATABASE_URL = "postgresql://test"
        settings.NEO4J_DATABASE = "edugraph"
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value={"code": "SCHOOL", "deleted_at": None})),
            MagicMock(),
        ]
        connect.return_value.__enter__.return_value = connection
        driver = MagicMock()
        job = {"id": "old-delete-job", "school_code": "SCHOOL", "previous_code": None,
               "operation": "DELETE", "attempt_count": 1}

        self.assertTrue(_run_school_job(driver, job))

        delete.assert_called_once_with(driver, "edugraph", [None])
        sync.assert_called_once_with(driver, "edugraph", {"nodes": {"HighSchool": [{"code": "SCHOOL"}]},
                                                              "relationships": {}})
        self.assertIn("FOR UPDATE", connection.execute.call_args_list[0].args[0])
        self.assertIn("status='SYNCED'", connection.execute.call_args_list[1].args[0])

    def test_delete_projection_uses_parameterized_queries_and_unique_codes(self):
        driver = MagicMock()
        session = MagicMock()
        driver.session.return_value.__enter__.return_value = session

        delete_school_projection(driver, "edugraph", ["OLD", "OLD", None])

        self.assertEqual(session.run.call_count, 2)
        self.assertIn("DETACH DELETE sg", session.run.call_args_list[0].args[0])
        self.assertEqual(session.run.call_args_list[0].kwargs, {"codes": ["OLD"]})

    @patch("graph_projection_worker.graph_projection_lock", return_value=nullcontext())
    @patch("graph_projection_worker.delete_school_projection")
    @patch("graph_projection_worker.sync_nodes_and_relationships")
    @patch("graph_projection_worker._load_school_projection_data", return_value=None)
    @patch("graph_projection_worker.psycopg.connect")
    @patch("graph_projection_worker.settings")
    def test_school_upsert_for_deleted_school_only_removes_old_and_current_graph_nodes(
            self, settings, connect, _load, sync, delete, _projection_lock):
        from graph_projection_worker import _run_school_job

        settings.DATABASE_URL = "postgresql://test"
        settings.NEO4J_DATABASE = "edugraph"
        connection = MagicMock()
        connection.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value={"code": "NEW", "deleted_at": "deleted"})),
            MagicMock(),
        ]
        connect.return_value.__enter__.return_value = connection
        driver = MagicMock()
        job = {"id": "job-id", "school_code": "NEW", "previous_code": "OLD",
               "operation": "UPSERT", "attempt_count": 1}

        self.assertTrue(_run_school_job(driver, job))

        delete.assert_called_once_with(driver, "edugraph", ["NEW", "OLD"])
        sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()

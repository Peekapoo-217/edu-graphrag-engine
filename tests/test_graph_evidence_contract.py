import unittest

from rag_api.main_api import _chat_result, _graph_path_validation_payload
from rag_api.retrieval.graph_retriever import (
    MANDATORY_SUBJECTS_QUERY,
    SCHOOL_GROUPS_QUERY,
    Neo4jRetriever,
    validate_evidence_dependencies,
)


class _QueryRecord:
    def __init__(self, data):
        self._data = data

    def data(self):
        return dict(self._data)

    def get(self, key, default=None):
        return self._data.get(key, default)


class _QueryResult(list):
    def single(self):
        return self[0] if self else None


class _ControlledNeo4jSession:
    def __init__(self, program_rows, group_rows, global_mandatory=None):
        self.program_rows = program_rows
        self.group_rows = group_rows
        self.global_mandatory = global_mandatory or []
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, query, **_params):
        self.queries.append(query)
        if query == SCHOOL_GROUPS_QUERY:
            return _QueryResult([_QueryRecord(row) for row in self.group_rows])
        if query == MANDATORY_SUBJECTS_QUERY:
            return _QueryResult([_QueryRecord({"subject_codes": self.global_mandatory})])
        return _QueryResult([_QueryRecord(row) for row in self.program_rows])


class _ControlledNeo4jDriver:
    def __init__(self, session):
        self._session = session

    def session(self, **_kwargs):
        return self._session


def _query_pipeline_retriever(group_subjects, *, global_mandatory=None, required_subject_codes=None):
    required_subject_codes = required_subject_codes or ["BIOLOGY", "MATH"]
    raw_program = {
        "program_name": "Program P1", "program_code": "P1",
        "university_name": "University U1", "university_code": "U1",
        "combination_id": "C1", "combination_name": "Combination C1",
        "required_subjects": required_subject_codes,
        "required_subject_codes": required_subject_codes,
        "required_aptitude_codes": [], "required_aptitude": [],
        "acceptance_records": [{"admission_year": 2026, "method_codes": "100",
                                 "source_url": "fixture://source"}],
        "cutoff_scores": [], "score": 1.0,
    }
    group = {
        "high_school_code": "TVQN", "high_school_name": "THPT A",
        "academic_year": "2026", "subject_group_code": "G01",
        "subject_group_name": "Group G01", "subjects": group_subjects,
    }
    session = _ControlledNeo4jSession([raw_program], [group], global_mandatory)
    retriever = Neo4jRetriever.__new__(Neo4jRetriever)
    retriever.database = "neo4j"
    retriever._driver = _ControlledNeo4jDriver(session)
    retriever._get_embedding = lambda *_args, **_kwargs: []
    return retriever, session


def _record(*, group_subjects=None, school="TVQN", group_code="G01", year="2026", university="DQN",
            program="P1", combination_id="C1", required_subjects=None,
            mandatory_subjects=None):
    group = {
        "high_school_code": school,
        "high_school_name": "THPT Trưng Vương",
        "academic_year": year,
        "subject_group_code": group_code,
        "subject_group_name": group_code,
        "subjects": group_subjects if group_subjects is not None else [
            {"code": "BIOLOGY", "canonical_code": "BIOLOGY", "name": "Sinh học"},
            {"code": "MATH", "canonical_code": "MATH", "name": "Toán"},
        ],
        "mandatory_subject_codes": mandatory_subjects or [],
    }
    return {
        "university_code": university,
        "program_code": program,
        "program_name": "Nông học",
        "combinations": [{
            "id": combination_id,
            "required_subject_codes": required_subjects or ["BIOLOGY", "MATH"],
            "required_aptitude_codes": [],
            "acceptance_records": [{"admission_year": 2026, "method_codes": "100"}],
            "matching_subject_groups": [group],
        }],
    }


def _attach(record, filters=None):
    retriever = Neo4jRetriever.__new__(Neo4jRetriever)
    return retriever.attach_evidence_provenance([record], filters=filters or {"high_school_code": "TVQN", "academic_year": "2026"})[0]


class GraphEvidenceDependencyTests(unittest.TestCase):
    @staticmethod
    def _actual_query_pipeline(group_subjects, *, answer_scope=None, global_mandatory=None,
                               required_subject_codes=None):
        retriever, session = _query_pipeline_retriever(
            group_subjects, global_mandatory=global_mandatory,
            required_subject_codes=required_subject_codes,
        )
        filters = {
            "program_names": ["Program P1"], "high_school_code": "TVQN",
            "academic_year": "2026", "admission_year": 2026,
        }
        if answer_scope:
            filters["answer_scope"] = answer_scope
        records = retriever.retrieve_context(filters=filters, top_k=5)
        enriched = retriever.attach_evidence_provenance(records, filters=filters)
        payload = _graph_path_validation_payload(enriched, filters)
        response = _chat_result("success" if enriched else "no_data", "fixture answer",
                                "fixture-request", "pathway_lookup", {
                                    "evidence": enriched, "evidence_source": "neo4j_graph",
                                    "retrieval_executed": True, "graph_path_validation": payload,
                                })
        return response, session

    def test_real_query_adapter_path_and_v2_pipeline_accepts_controlled_complete_record(self):
        response, session = self._actual_query_pipeline([
            {"code": "BIOLOGY", "canonical_code": "BIOLOGY", "name": "Biology"},
            {"code": "MATH", "canonical_code": "MATH", "name": "Math"},
        ])
        self.assertEqual(["Program P1"], [record["program_name"] for record in response.data["evidence"]])
        self.assertTrue(response.data["graph_path_validation"]["complete"])
        self.assertEqual("complete", response.data["evidence"][0]["evidence_dependency_validation"]["status"])
        self.assertIn(SCHOOL_GROUPS_QUERY, session.queries)
        self.assertIn(MANDATORY_SUBJECTS_QUERY, session.queries)

    def test_real_query_adapter_retains_partial_overlap_without_certifying_full_path(self):
        response, _ = self._actual_query_pipeline([
            {"code": "BIOLOGY", "canonical_code": "BIOLOGY", "name": "Biology"},
        ], answer_scope="subject_overlap")
        self.assertEqual(1, len(response.data["evidence"]))
        self.assertEqual("subject_overlap", response.data["evidence"][0]["answer_scope"])
        self.assertFalse(response.data["graph_path_validation"]["complete"])
        self.assertEqual("incomplete", response.data["evidence"][0]["evidence_dependency_validation"]["status"])

    def test_tvqn_g01_chemistry_overlap_is_partial_for_chemistry_geography_math(self):
        subjects = [{"code": "CHEMISTRY", "canonical_code": "CHEMISTRY", "name": "Chemistry"}]
        overlap, _ = self._actual_query_pipeline(
            subjects, answer_scope="subject_overlap",
            required_subject_codes=["CHEMISTRY", "GEOGRAPHY", "MATH"],
        )
        full_path, _ = self._actual_query_pipeline(
            subjects, answer_scope="full_path",
            required_subject_codes=["CHEMISTRY", "GEOGRAPHY", "MATH"],
        )

        self.assertEqual(1, len(overlap.data["evidence"]))
        self.assertEqual(["CHEMISTRY"], [
            item["subject_code"] for item in overlap.data["evidence"][0]["verified_subject_overlaps"]
        ])
        self.assertEqual("incomplete", overlap.data["evidence"][0]["evidence_dependency_validation"]["status"])
        self.assertFalse(overlap.data["graph_path_validation"]["complete"])
        self.assertEqual([], full_path.data["evidence"])

    def test_subject_overlap_with_no_intersection_returns_no_evidence(self):
        response, _ = self._actual_query_pipeline(
            [], answer_scope="subject_overlap",
            required_subject_codes=["CHEMISTRY", "GEOGRAPHY", "MATH"],
        )

        self.assertEqual([], response.data["evidence"])
        self.assertFalse(response.data["graph_path_validation"]["complete"])

    def test_explicit_full_path_scope_rejects_missing_scoped_membership(self):
        response, _ = self._actual_query_pipeline([
            {"code": "CHEMISTRY", "canonical_code": "CHEMISTRY", "name": "Chemistry"},
        ], answer_scope="full_path")

        self.assertEqual([], response.data["evidence"])
        self.assertFalse(response.data["graph_path_validation"]["complete"])

    def test_real_query_adapter_does_not_use_global_mandatory_hint_as_scoped_membership(self):
        response, _ = self._actual_query_pipeline([
            {"code": "BIOLOGY", "canonical_code": "BIOLOGY", "name": "Biology"},
        ], global_mandatory=["MATH"])
        self.assertEqual([], response.data["evidence"])
        self.assertFalse(response.data["graph_path_validation"]["complete"])

    def test_query_fixture_to_v2_serializer_preserves_complete_path(self):
        raw_query_record = _record()
        adapter_records = [raw_query_record]
        assembled = _attach(adapter_records[0])
        payload = _graph_path_validation_payload([assembled], {
            "high_school_code": "TVQN", "academic_year": "2026", "admission_year": 2026,
        })
        response = _chat_result("success", "evidence-backed", "fixture-request", "pathway_lookup", {
            "evidence": [assembled], "evidence_source": "neo4j_graph", "retrieval_executed": True,
            "graph_path_validation": payload,
        })
        serialized = response.data["graph_path_validation"]
        self.assertEqual(1, len(adapter_records))
        self.assertEqual(assembled["evidence_path"], serialized["records"][0]["evidence_path"])
        self.assertEqual("complete", assembled["evidence_dependency_validation"]["status"])
        self.assertTrue(serialized["complete"])

    def test_query_fixture_dropping_edge_before_serialization_is_rejected(self):
        assembled = _attach(_record())
        edge = next(edge for edge in assembled["evidence_path"]
                    if edge["relationship_id"].startswith("includes-subject:"))
        assembled["evidence_path"].remove(edge)
        payload = _graph_path_validation_payload([assembled], {
            "high_school_code": "TVQN", "academic_year": "2026", "admission_year": 2026,
        })
        self.assertFalse(payload["complete"])
        self.assertIn(edge["relationship_id"], payload["records"][0]["missing_relationship_ids"])

    def test_query_fixture_with_wrong_school_year_scope_is_incomplete(self):
        assembled = _attach(_record(year="2025"))
        payload = _graph_path_validation_payload([assembled], {
            "high_school_code": "TVQN", "academic_year": "2026", "admission_year": 2026,
        })
        self.assertFalse(payload["complete"])
        self.assertTrue(payload["records"][0]["missing_scope_dependencies"])

    def test_school_group_query_observes_alias_relationship_before_canonicalizing_membership(self):
        from rag_api.retrieval.graph_retriever import SCHOOL_GROUPS_QUERY

        self.assertIn("IS_VARIANT_OF", SCHOOL_GROUPS_QUERY)
        self.assertIn("variant_relationship_observed", SCHOOL_GROUPS_QUERY)

    def test_shared_offer_edge_is_a_valid_edge_set_member_across_combination_dependencies(self):
        record = _record()
        record["combinations"][0]["matching_subject_groups"][0]["subjects"].append(
            {"code": "CHEMISTRY", "canonical_code": "CHEMISTRY", "name": "Hóa học"})
        second = dict(record["combinations"][0])
        second["id"] = "C2"
        second["acceptance_records"] = [{"admission_year": 2026, "method_codes": "100"}]
        record["combinations"].append(second)

        result = _attach(record)
        offer_edges = [edge for edge in result["evidence_path"] if edge["relationship_id"].startswith("offers-group:")]
        included_subjects = {edge["to"] for edge in result["evidence_path"]
                             if edge["relationship_id"].startswith("includes-subject:")}

        self.assertEqual(1, len(offer_edges), "shared edges are represented once in the directed edge set")
        self.assertEqual({"Subject:BIOLOGY", "Subject:MATH"}, included_subjects,
                         "path dependencies include only required subjects observed in the group")
        self.assertEqual("complete", result["evidence_dependency_validation"]["status"])
        self.assertTrue(result["evidence_dependency_validation"]["shared_relationship_ids"])

    def test_same_relationship_id_with_conflicting_endpoints_is_incomplete(self):
        result = _attach(_record())
        edge = next(edge for edge in result["evidence_path"] if edge["relationship_id"].startswith("offers-group:"))
        result["evidence_path"].append({**edge, "to": "SubjectGroup:OTHER|2026|G99"})

        validation = validate_evidence_dependencies(result, {"high_school_code": "TVQN", "academic_year": "2026"})

        self.assertEqual("incomplete", validation["status"])
        self.assertIn(edge["relationship_id"], validation["conflicting_relationship_ids"])

    def test_missing_required_edge_and_wrong_direction_remain_incomplete(self):
        missing = _attach(_record())
        edge = next(edge for edge in missing["evidence_path"] if edge["relationship_id"].startswith("includes-subject:"))
        missing["evidence_path"].remove(edge)
        missing_validation = validate_evidence_dependencies(
            missing, {"high_school_code": "TVQN", "academic_year": "2026"})
        self.assertEqual("incomplete", missing_validation["status"])
        self.assertIn(edge["relationship_id"], missing_validation["missing_relationship_ids"])

        wrong_direction = _attach(_record())
        edge = next(edge for edge in wrong_direction["evidence_path"]
                    if edge["relationship_id"].startswith("includes-subject:"))
        edge["from"], edge["to"] = edge["to"], edge["from"]
        direction_validation = validate_evidence_dependencies(
            wrong_direction, {"high_school_code": "TVQN", "academic_year": "2026"})
        self.assertEqual("incomplete", direction_validation["status"])
        self.assertIn(edge["relationship_id"], direction_validation["missing_relationship_ids"])

    def test_school_group_year_and_program_university_scopes_are_required(self):
        for missing_scope in (_record(school=None), _record(group_code=None), _record(year=None)):
            with self.subTest(scope=missing_scope["combinations"][0]["matching_subject_groups"][0]):
                validation = _attach(missing_scope)["evidence_dependency_validation"]
                self.assertEqual("incomplete", validation["status"])
                self.assertTrue(validation["missing_scope_dependencies"])

        mismatched = _attach(_record())
        edge = next(edge for edge in mismatched["evidence_path"] if edge["relationship_id"].startswith("offers-program:"))
        edge["from"] = "University:OTHER"
        mismatch_validation = validate_evidence_dependencies(mismatched, {"high_school_code": "TVQN", "academic_year": "2026"})
        self.assertEqual("incomplete", mismatch_validation["status"])
        self.assertIn(edge["relationship_id"], mismatch_validation["missing_relationship_ids"])

    def test_dependency_is_incomplete_when_requested_admission_scope_has_no_offer(self):
        record = _record()
        record["combinations"][0]["acceptance_records"] = []
        enriched = _attach(record)
        validation = enriched["evidence_dependency_validation"]

        self.assertEqual("incomplete", validation["status"])
        self.assertTrue(validation["missing_scope_dependencies"])
        self.assertTrue(validation["dependencies"])
        self.assertTrue(all(item["status"] == "incomplete" for item in validation["dependencies"]))

    def test_program_without_accepts_combination_edge_cannot_support_a_path(self):
        record = {
            "evidence_id": "graph:program-without-acceptance-edge",
            "university_code": "DQN", "program_code": "7340302",
            "combinations": [], "evidence_path": [],
        }

        validation = validate_evidence_dependencies(record, {
            "university": "DQN", "high_school_code": "TCV", "academic_year": "2026",
            "subject_group_code": "G03", "admission_year": "2026",
            "answer_scope": "subject_overlap",
        })

        self.assertEqual("incomplete", validation["status"])
        self.assertEqual([], validation["observed_relationship_ids"])

    def test_includes_subject_is_emitted_only_for_query_observed_membership(self):
        observed = _attach(_record())
        include_edges = [edge for edge in observed["evidence_path"]
                         if edge["relationship_id"].startswith("includes-subject:")]
        self.assertEqual({"BIOLOGY", "MATH"}, {edge["to"].split(":", 1)[1] for edge in include_edges})

        unsupported = _attach(_record(group_subjects=[{"code": "BIOLOGY", "canonical_code": "BIOLOGY"}],
                                      mandatory_subjects=[]))
        self.assertEqual("incomplete", unsupported["evidence_dependency_validation"]["status"])
        self.assertFalse(any(edge["relationship_id"].startswith("includes-subject:")
                             and edge["to"] == "Subject:MATH" for edge in unsupported["evidence_path"]))

    def test_variant_membership_keeps_actual_include_edge_and_canonicalization_edge(self):
        record = _record(
            group_subjects=[{"code": "TECH_BIOLOGY", "canonical_code": "TECHNOLOGY",
                             "variant_relationship_observed": True,
                             "name": "Công nghệ Sinh"}],
            required_subjects=["TECHNOLOGY"],
            mandatory_subjects=[],
        )

        attached = _attach(record)

        include_edges = [edge for edge in attached["evidence_path"]
                         if edge["relationship_id"].startswith("includes-subject:")]
        alias_edges = [edge for edge in attached["evidence_path"]
                       if edge["relationship_id"].startswith("is-variant-of:")]
        self.assertIn({"from": "SubjectGroup:TVQN|2026|G01", "to": "Subject:TECH_BIOLOGY",
                       "relationship_id": "includes-subject:" + Neo4jRetriever._stable_digest(
                           ["TVQN|2026|G01", "TECH_BIOLOGY"])}, include_edges)
        self.assertIn({"from": "Subject:TECH_BIOLOGY", "to": "Subject:TECHNOLOGY",
                       "relationship_id": "is-variant-of:" + Neo4jRetriever._stable_digest(
                           ["TECH_BIOLOGY", "TECHNOLOGY"])}, alias_edges)
        self.assertFalse(any(edge["to"] == "Subject:TECHNOLOGY" for edge in include_edges),
                         "canonical membership must not be fabricated as a direct INCLUDE edge")
        self.assertEqual("complete", attached["evidence_dependency_validation"]["status"])

        without_alias = dict(attached)
        without_alias["evidence_path"] = [edge for edge in attached["evidence_path"] if edge not in alias_edges]
        validation = validate_evidence_dependencies(
            without_alias, {"high_school_code": "TVQN", "academic_year": "2026"})
        self.assertEqual("incomplete", validation["status"])
        self.assertEqual([alias_edges[0]["relationship_id"]], validation["missing_relationship_ids"])

    def test_canonical_property_without_observed_alias_relationship_is_not_evidence(self):
        record = _record(
            group_subjects=[{"code": "TECH_BIOLOGY", "canonical_code": "TECHNOLOGY",
                             "variant_relationship_observed": False,
                             "name": "Công nghệ Sinh"}],
            required_subjects=["TECHNOLOGY"],
            mandatory_subjects=[],
        )

        attached = _attach(record)

        self.assertEqual("incomplete", attached["evidence_dependency_validation"]["status"])
        self.assertFalse(any(edge["relationship_id"].startswith("is-variant-of:")
                             for edge in attached["evidence_path"]))
        coverage = attached["evidence_dependency_validation"]["dependencies"][0]["subject_coverage"]
        self.assertEqual("canonical_alias_relationship_unobserved",
                         next(item for item in coverage if item["subject_code"] == "TECHNOLOGY")["reason"])

    def test_global_mandatory_flag_without_scoped_provenance_is_unverified(self):
        record = _record(
            group_subjects=[{"code": "BIOLOGY", "canonical_code": "BIOLOGY", "name": "Sinh học"}],
            required_subjects=["BIOLOGY", "MATH"],
            mandatory_subjects=["MATH"],
        )

        attached = _attach(record)
        validation = attached["evidence_dependency_validation"]

        self.assertEqual("incomplete", validation["status"])
        math_coverage = validation["dependencies"][0]["subject_coverage"]
        self.assertEqual({"subject_code": "MATH", "status": "unverified",
                          "reason": "mandatory_scope_or_provenance_unavailable"},
                         next(item for item in math_coverage if item["subject_code"] == "MATH"))
        self.assertTrue(any("mandatory_subject_provenance:MATH" in item
                            for item in validation["missing_scope_dependencies"]))
        self.assertFalse(any(edge["relationship_id"].startswith("includes-subject:")
                             and edge["to"] == "Subject:MATH" for edge in attached["evidence_path"]))

    def test_versioned_incomplete_contract_survives_chat_response_serialization(self):
        result = _attach(_record(group_subjects=[{"code": "BIOLOGY", "canonical_code": "BIOLOGY"}],
                                 mandatory_subjects=[]))
        contract = _graph_path_validation_payload([result], {"high_school_code": "TVQN", "academic_year": "2026"})

        self.assertEqual(2, contract["schema_version"])
        self.assertEqual("directed_edge_set", contract["path_semantics"])
        self.assertFalse(contract["complete"])
        response = _chat_result("success", "partial answer", "req-1", "pathway_lookup", {
            "evidence_source": "neo4j_graph", "evidence": [result], "graph_path_validation": contract,
        })
        serialized = response.model_dump()
        self.assertEqual(contract, serialized["data"]["graph_path_validation"])

    def test_one_incomplete_record_keeps_aggregate_of_twenty_records_incomplete(self):
        records = [_attach(_record(program=f"P{index}")) for index in range(19)]
        incomplete = _record(program="P19")
        incomplete["combinations"][0]["acceptance_records"] = []
        records.append(_attach(incomplete))

        payload = _graph_path_validation_payload(records, {"high_school_code": "TVQN", "academic_year": "2026"})

        self.assertEqual(20, len(payload["records"]))
        self.assertEqual(19, sum(row["status"] == "complete" for row in payload["records"]))
        self.assertEqual(1, sum(row["status"] == "incomplete" for row in payload["records"]))
        self.assertFalse(payload["complete"], "an aggregate cannot hide an incomplete record")
        self.assertTrue(payload["records"][-1]["missing_scope_dependencies"])


if __name__ == "__main__":
    unittest.main()

"""
Neo4j Writer module handling driver connections, constraint audits, batch MERGE execution, and verification queries.
"""

from typing import Any, Dict, List, Tuple
import neo4j

REQUIRED_CONSTRAINTS = [
    ("HighSchool", "code"),
    ("SubjectGroup", "uid"),
    ("Subject", "code"),
    ("University", "code"),
    ("TrainingProgram", "uid"),
    ("AdmissionCombination", "id"),
    ("AptitudeTest", "code"),
    ("CutoffScore", "uid"),
]

NODE_CYPHER_STATEMENTS: Dict[str, str] = {
    "Subject": """
        UNWIND $rows AS row
        MERGE (s:Subject {code: row.code})
        SET s.name = row.name,
            s.subject_type = row.subject_type,
            s.is_mandatory = row.is_mandatory,
            s.canonical_subject_code = row.canonical_subject_code
    """,
    "HighSchool": """
        UNWIND $rows AS row
        MERGE (h:HighSchool {code: row.code})
        SET h.name = row.name,
            h.location = row.location
    """,
    "SubjectGroup": """
        UNWIND $rows AS row
        MERGE (sg:SubjectGroup {uid: row.uid})
        SET sg.school_code = row.school_code,
            sg.academic_year = row.academic_year,
            sg.group_code = row.group_code,
            sg.name = row.name
    """,
    "University": """
        UNWIND $rows AS row
        MERGE (u:University {code: row.code})
        SET u.name = row.name,
            u.website = row.website
    """,
    "TrainingProgram": """
        UNWIND $rows AS row
        MERGE (tp:TrainingProgram {uid: row.uid})
        SET tp.university_code = row.university_code,
            tp.program_code = row.program_code,
            tp.name = row.name
    """,
    "AdmissionCombination": """
        UNWIND $rows AS row
        MERGE (c:AdmissionCombination {id: row.id})
        SET c.display_name = row.display_name,
            c.component_count = row.component_count
    """,
    "AptitudeTest": """
        UNWIND $rows AS row
        MERGE (a:AptitudeTest {code: row.code})
        SET a.name = row.name
    """,
    "CutoffScore": """
        UNWIND $rows AS row
        MERGE (cs:CutoffScore {uid: row.uid})
        SET cs.university_code = row.university_code,
            cs.admission_year = row.admission_year,
            cs.program_code = row.program_code,
            cs.method_code = row.method_code,
            cs.score_type = row.score_type,
            cs.cutoff_score_thpt = row.cutoff_score_thpt,
            cs.cutoff_score_sat = row.cutoff_score_sat,
            cs.source_url = row.source_url
    """,
}

RELATIONSHIP_CYPHER_STATEMENTS: Dict[str, str] = {
    "OFFERS_GROUP": """
        UNWIND $rows AS row
        MATCH (h:HighSchool {code: row.high_school_code})
        MATCH (sg:SubjectGroup {uid: row.subject_group_uid})
        MERGE (h)-[r:OFFERS_GROUP]->(sg)
    """,
    "INCLUDES_SUBJECT": """
        UNWIND $rows AS row
        MATCH (sg:SubjectGroup {uid: row.subject_group_uid})
        MATCH (s:Subject {code: row.subject_code})
        MERGE (sg)-[r:INCLUDES_SUBJECT]->(s)
    """,
    "IS_VARIANT_OF": """
        UNWIND $rows AS row
        MATCH (var:Subject {code: row.variant_subject_code})
        MATCH (base:Subject {code: row.canonical_subject_code})
        MERGE (var)-[r:IS_VARIANT_OF]->(base)
    """,
    "OFFERS_PROGRAM": """
        UNWIND $rows AS row
        MATCH (u:University {code: row.university_code})
        MATCH (tp:TrainingProgram {uid: row.training_program_uid})
        MERGE (u)-[r:OFFERS_PROGRAM]->(tp)
    """,
    "ACCEPTS_COMBINATION": """
        UNWIND $rows AS row
        MATCH (p:TrainingProgram {uid: row.training_program_uid})
        MATCH (c:AdmissionCombination {id: row.combination_id})
        MERGE (p)-[r:ACCEPTS_COMBINATION {admission_year: row.admission_year}]->(c)
        SET r.method_codes = row.method_codes,
            r.quota = row.quota,
            r.source_url = row.source_url
    """,
    "REQUIRES_SUBJECT": """
        UNWIND $rows AS row
        MATCH (c:AdmissionCombination {id: row.combination_id})
        MATCH (s:Subject {code: row.subject_code})
        MERGE (c)-[r:REQUIRES_SUBJECT]->(s)
        SET r.weight = row.weight
    """,
    "REQUIRES_APTITUDE": """
        UNWIND $rows AS row
        MATCH (c:AdmissionCombination {id: row.combination_id})
        MATCH (a:AptitudeTest {code: row.aptitude_code})
        MERGE (c)-[r:REQUIRES_APTITUDE]->(a)
        SET r.weight = row.weight
    """,
    "HAS_CUTOFF": """
        UNWIND $rows AS row
        MATCH (tp:TrainingProgram {uid: row.training_program_uid})
        MATCH (cs:CutoffScore {uid: row.cutoff_score_uid})
        MERGE (tp)-[r:HAS_CUTOFF]->(cs)
    """,
}


def get_neo4j_driver(uri: str, auth: Tuple[str, str]) -> neo4j.Driver:
    """Create Neo4j driver instance."""
    return neo4j.GraphDatabase.driver(uri, auth=auth)


def verify_neo4j_constraints(driver: neo4j.Driver, database: str) -> Tuple[bool, List[str]]:
    """
    Execute SHOW CONSTRAINTS in Neo4j to verify the existence of required unique constraints.
    Returns (is_valid, list_of_missing_constraint_descriptions).
    """
    existing_pairs = set()
    with driver.session(database=database) as session:
        result = session.run("SHOW CONSTRAINTS;")
        for record in result:
            rec_dict = record.data()
            labels_or_types = rec_dict.get("labelsOrTypes")
            properties = rec_dict.get("properties")
            
            if labels_or_types and properties:
                label = labels_or_types[0] if isinstance(labels_or_types, list) else labels_or_types
                prop = properties[0] if isinstance(properties, list) else properties
                existing_pairs.add((label, prop))

    missing: List[str] = []
    for label, prop in REQUIRED_CONSTRAINTS:
        if (label, prop) not in existing_pairs:
            missing.append(f"Constraint missing for label '{label}' on property '{prop}'")

    return len(missing) == 0, missing


def chunk_list(items: List[Any], batch_size: int) -> List[List[Any]]:
    """Partition a list into sublists of size batch_size."""
    if batch_size <= 0:
        batch_size = 500
    return [items[i : i + batch_size] for i in range(0, len(items), batch_size)]


def sync_nodes_and_relationships(
    driver: neo4j.Driver,
    database: str,
    graph_data: Dict[str, Any],
    batch_size: int = 500,
) -> None:
    """
    Synchronize nodes and relationships in strict sequential order.
    Order:
    1. Subject
    2. HighSchool
    3. SubjectGroup
    4. University
    5. TrainingProgram
    6. AdmissionCombination
    7. AptitudeTest
    8. CutoffScore
    9. Relationships (OFFERS_GROUP, INCLUDES_SUBJECT, IS_VARIANT_OF, OFFERS_PROGRAM, ACCEPTS_COMBINATION, REQUIRES_SUBJECT, REQUIRES_APTITUDE, HAS_CUTOFF)
    """
    node_order = [
        "Subject",
        "HighSchool",
        "SubjectGroup",
        "University",
        "TrainingProgram",
        "AdmissionCombination",
        "AptitudeTest",
        "CutoffScore",
    ]

    rel_order = [
        "OFFERS_GROUP",
        "INCLUDES_SUBJECT",
        "IS_VARIANT_OF",
        "OFFERS_PROGRAM",
        "ACCEPTS_COMBINATION",
        "REQUIRES_SUBJECT",
        "REQUIRES_APTITUDE",
        "HAS_CUTOFF",
    ]

    nodes_data = graph_data["nodes"]
    rels_data = graph_data["relationships"]

    with driver.session(database=database) as session:
        # Sync Nodes
        for label in node_order:
            rows = nodes_data.get(label, [])
            if not rows:
                continue
            cypher = NODE_CYPHER_STATEMENTS[label]
            chunks = chunk_list(rows, batch_size)
            try:
                for chunk in chunks:
                    session.run(cypher, rows=chunk)
            except Exception as e:
                raise RuntimeError(f"Error during Node Sync stage '{label}': {e}") from e

        # Sync Relationships
        for rel_type in rel_order:
            rows = rels_data.get(rel_type, [])
            if not rows:
                continue
            cypher = RELATIONSHIP_CYPHER_STATEMENTS[rel_type]
            chunks = chunk_list(rows, batch_size)
            try:
                for chunk in chunks:
                    session.run(cypher, rows=chunk)
            except Exception as e:
                raise RuntimeError(f"Error during Relationship Sync stage '{rel_type}': {e}") from e


def verify_graph_statistics(driver: neo4j.Driver, database: str) -> Dict[str, Any]:
    """
    Execute statistical and orphan checks in Neo4j and return verification results.
    """
    node_counts = {}
    rel_counts = {}
    orphans = {}

    with driver.session(database=database) as session:
        # Node statistics query
        res_nodes = session.run("MATCH (n) RETURN labels(n)[0] AS label, count(*) AS total ORDER BY label;")
        for record in res_nodes:
            node_counts[record["label"]] = record["total"]

        # Relationship statistics query
        res_rels = session.run("MATCH ()-[r]->() RETURN type(r) AS relationship, count(*) AS total ORDER BY relationship;")
        for record in res_rels:
            rel_counts[record["relationship"]] = record["total"]

        # Orphan node checks
        # SubjectGroup without HighSchool
        res_sg = session.run("MATCH (sg:SubjectGroup) WHERE NOT ()-[:OFFERS_GROUP]->(sg) RETURN count(sg) AS count;")
        orphans["SubjectGroup_without_HighSchool"] = res_sg.single()["count"]

        # TrainingProgram without University
        res_tp = session.run("MATCH (tp:TrainingProgram) WHERE NOT ()-[:OFFERS_PROGRAM]->(tp) RETURN count(tp) AS count;")
        orphans["TrainingProgram_without_University"] = res_tp.single()["count"]

        # AdmissionCombination without components
        res_ac = session.run("MATCH (c:AdmissionCombination) WHERE NOT (c)-[:REQUIRES_SUBJECT]->() AND NOT (c)-[:REQUIRES_APTITUDE]->() RETURN count(c) AS count;")
        orphans["AdmissionCombination_without_components"] = res_ac.single()["count"]

        # CutoffScore not connected to TrainingProgram
        res_cs = session.run("MATCH (cs:CutoffScore) WHERE NOT ()-[:HAS_CUTOFF]->(cs) RETURN count(cs) AS count;")
        orphans["CutoffScore_without_TrainingProgram"] = res_cs.single()["count"]

    return {
        "nodes": node_counts,
        "relationships": rel_counts,
        "orphans": orphans,
    }

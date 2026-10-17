"""
PostgreSQL Reader module with read-only transaction enforcement and information_schema verification.
"""

from typing import Any, Dict, List, Tuple
import psycopg

from .models import (
    get_canonical_subject_code,
    make_cutoff_score_uid,
    make_subject_group_uid,
    make_training_program_uid,
)

REQUIRED_POSTGRES_SCHEMA: Dict[str, List[str]] = {
    "subjects": ["code", "name", "subject_type", "is_mandatory", "canonical_subject_code"],
    "high_schools": ["code", "name", "location", "deleted_at"],
    "subject_groups": ["school_code", "academic_year", "group_code", "group_name", "subject_code", "deleted_at"],
    "universities": ["university_code", "university_name", "website"],
    "majors": ["program_code", "program_name"],
    "admission_offers": [
        "id", "university_code", "admission_year", "program_code",
        "method_codes", "quota", "source_url"
    ],
    "offer_combinations": ["offer_id", "canonical_combination_id"],
    "admission_combinations": ["combination_id", "combination_display", "component_count"],
    "admission_combination_components": [
        "combination_id", "component_code", "component_name",
        "component_type", "subject_code", "weight"
    ],
    "cutoff_scores": [
        "university_code", "admission_year", "program_code",
        "method_code", "cutoff_score_thpt", "cutoff_score_sat", "source_url"
    ],
}


def get_postgres_connection(database_url: str) -> psycopg.Connection:
    """Connect to PostgreSQL and enforce read-only transaction mode."""
    conn = psycopg.connect(database_url)
    conn.read_only = True
    return conn


def verify_postgres_schema(conn: psycopg.Connection) -> Tuple[bool, List[str]]:
    """Verify presence of all required tables and columns using information_schema."""
    missing_errors: List[str] = []
    with conn.cursor() as cur:
        for table_name, req_cols in REQUIRED_POSTGRES_SCHEMA.items():
            cur.execute(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = %s);",
                (table_name,)
            )
            table_exists = cur.fetchone()[0]
            if not table_exists:
                missing_errors.append(f"Missing required DB table: '{table_name}'")
                continue

            for col_name in req_cols:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s AND column_name = %s);",
                    (table_name, col_name)
                )
                col_exists = cur.fetchone()[0]
                if not col_exists:
                    missing_errors.append(f"Missing required column '{col_name}' in table '{table_name}'")

    return len(missing_errors) == 0, missing_errors


def fetch_all_graph_data_from_postgres(conn: psycopg.Connection) -> Dict[str, Any]:
    """
    Extract and normalize all node and relationship data from PostgreSQL.
    """
    with conn.cursor() as cur:
        # 1. Subjects & Variant relations
        cur.execute("SELECT code, name, subject_type, is_mandatory, canonical_subject_code FROM subjects ORDER BY code;")
        subjects_rows = cur.fetchall()
        
        subjects: List[Dict[str, Any]] = []
        is_variant_of_relations: List[Dict[str, Any]] = []
        
        for code, name, subject_type, is_mandatory, canonical_code in subjects_rows:
            canon = get_canonical_subject_code(code, canonical_code)
            subjects.append({
                "code": code,
                "name": name,
                "subject_type": subject_type,
                "is_mandatory": bool(is_mandatory),
                "canonical_subject_code": canon,
            })
            if canon and canon != code:
                is_variant_of_relations.append({
                    "variant_subject_code": code,
                    "canonical_subject_code": canon,
                })

        # 2. High Schools
        cur.execute("SELECT code, name, location FROM high_schools WHERE deleted_at IS NULL ORDER BY code;")
        high_schools = [
            {"code": r[0], "name": r[1], "location": r[2]}
            for r in cur.fetchall()
        ]

        # 3. Subject Groups & Relations
        cur.execute("SELECT sg.school_code, sg.academic_year, sg.group_code, sg.group_name, sg.subject_code, "
                    "coalesce(state.is_published, true), coalesce(state.publication_revision, 1) "
                    "FROM subject_groups sg LEFT JOIN admin.subject_group_publication_state state "
                    "ON state.school_code=sg.school_code AND state.academic_year=sg.academic_year "
                    "AND state.group_code=upper(sg.group_code) "
                    "WHERE sg.deleted_at IS NULL ORDER BY sg.school_code, sg.academic_year, sg.group_code;")
        sg_rows = cur.fetchall()
        
        subject_groups_map: Dict[str, Dict[str, Any]] = {}
        includes_subject_relations: List[Dict[str, Any]] = []
        offers_group_relations_by_key = {}
        unpublished_subject_groups_by_uid: Dict[str, Dict[str, Any]] = {}

        for school_code, academic_year, group_code, group_name, subject_code, is_published, publication_revision in sg_rows:
            uid = make_subject_group_uid(school_code, str(academic_year), group_code)
            if not bool(is_published):
                unpublished_subject_groups_by_uid[uid] = {
                    "uid": uid,
                    "school_code": school_code,
                    "academic_year": str(academic_year),
                    "group_code": group_code,
                    "publication_revision": int(publication_revision),
                }
                continue

            if uid not in subject_groups_map:
                subject_groups_map[uid] = {
                    "uid": uid,
                    "school_code": school_code,
                    "academic_year": str(academic_year),
                    "group_code": group_code,
                    "name": group_name,
                    "is_published": bool(is_published),
                    "publication_revision": int(publication_revision),
                }
            offers_group_relations_by_key[(school_code, uid)] = {
                "high_school_code": school_code,
                "subject_group_uid": uid,
                "school_code": school_code,
                "academic_year": str(academic_year),
                "group_code": group_code,
                "group_name": group_name,
                "is_published": bool(is_published),
                "publication_revision": int(publication_revision),
            }
            includes_subject_relations.append({
                "subject_group_uid": uid,
                "subject_code": subject_code,
            })

        # Publication-state rows are durable PostgreSQL tombstones. Read these
        # independently so a removed source row cannot leave a stale graph node.
        cur.execute(
            "SELECT school_code, academic_year, group_code, publication_revision "
            "FROM admin.subject_group_publication_state WHERE is_published=false "
            "ORDER BY school_code, academic_year, group_code;"
        )
        for school_code, academic_year, group_code, publication_revision in cur.fetchall():
            uid = make_subject_group_uid(school_code, str(academic_year), group_code)
            unpublished_subject_groups_by_uid[uid] = {
                "uid": uid,
                "school_code": school_code,
                "academic_year": str(academic_year),
                "group_code": group_code,
                "publication_revision": int(publication_revision),
            }

        subject_groups = list(subject_groups_map.values())
        offers_group_relations = [offers_group_relations_by_key[key]
                                  for key in sorted(offers_group_relations_by_key)]

        # 4. Universities
        cur.execute("SELECT university_code, university_name, website FROM universities ORDER BY university_code;")
        universities = [
            {"code": r[0], "name": r[1], "website": r[2]}
            for r in cur.fetchall()
        ]

        # 5. Training Programs, OFFERS_PROGRAM, ACCEPTS_COMBINATION
        cur.execute("""
            SELECT o.university_code, o.admission_year, o.program_code, m.program_name,
                   o.method_codes, o.quota, oc.canonical_combination_id, o.source_url
            FROM admission_offers o
            JOIN majors m ON m.program_code = o.program_code
            LEFT JOIN offer_combinations oc ON oc.offer_id = o.id
            ORDER BY o.university_code, o.program_code, o.admission_year, oc.canonical_combination_id;
        """)
        offer_rows = cur.fetchall()

        training_programs_map: Dict[str, Dict[str, Any]] = {}
        offers_program_set = set()
        accepts_combination_relations: List[Dict[str, Any]] = []

        for univ_code, adm_year, prog_code, prog_name, method_codes, quota, comb_id, source_url in offer_rows:
            tp_uid = make_training_program_uid(univ_code, prog_code)
            if tp_uid not in training_programs_map:
                training_programs_map[tp_uid] = {
                    "uid": tp_uid,
                    "university_code": univ_code,
                    "program_code": prog_code,
                    "name": prog_name,
                }
            offers_program_set.add((univ_code, tp_uid))
            if comb_id is not None:
                accepts_combination_relations.append({
                    "training_program_uid": tp_uid,
                    "combination_id": comb_id,
                    "admission_year": int(adm_year),
                    "method_codes": method_codes,
                    "quota": int(quota) if quota is not None else None,
                    "source_url": source_url,
                })

        # 6. Admission Combinations & Components (Aptitude, REQUIRES_SUBJECT, REQUIRES_APTITUDE)
        cur.execute("SELECT combination_id, combination_display, component_count FROM admission_combinations ORDER BY combination_id;")
        admission_combinations = [
            {"id": r[0], "display_name": r[1], "component_count": int(r[2])}
            for r in cur.fetchall()
        ]

        cur.execute("""
            SELECT combination_id, component_code, component_name, component_type, subject_code, weight
            FROM admission_combination_components
            ORDER BY combination_id, component_code;
        """)
        comp_rows = cur.fetchall()

        aptitude_tests_map: Dict[str, Dict[str, Any]] = {}
        requires_subject_relations: List[Dict[str, Any]] = []
        requires_aptitude_relations: List[Dict[str, Any]] = []

        for comb_id, comp_code, comp_name, comp_type, subj_code, weight in comp_rows:
            weight_val = float(weight) if weight is not None else 1.0
            if comp_type == "SUBJECT":
                requires_subject_relations.append({
                    "combination_id": comb_id,
                    "subject_code": subj_code,
                    "weight": weight_val,
                })
            elif comp_type == "APTITUDE":
                if comp_code not in aptitude_tests_map:
                    aptitude_tests_map[comp_code] = {
                        "code": comp_code,
                        "name": comp_name,
                    }
                requires_aptitude_relations.append({
                    "combination_id": comb_id,
                    "aptitude_code": comp_code,
                    "weight": weight_val,
                })

        aptitude_tests = list(aptitude_tests_map.values())

        # 7. Cutoff Scores & HAS_CUTOFF
        cur.execute("""
            SELECT c.university_code, c.admission_year, c.program_code, m.program_name,
                   c.method_code, c.cutoff_score_thpt, c.cutoff_score_sat, c.source_url
            FROM cutoff_scores c
            JOIN majors m ON m.program_code = c.program_code
            ORDER BY c.university_code, c.admission_year, c.program_code, c.method_code;
        """)
        cutoff_rows = cur.fetchall()

        cutoff_scores: List[Dict[str, Any]] = []
        has_cutoff_relations: List[Dict[str, Any]] = []

        for univ_code, adm_year, prog_code, prog_name, method_code, score_thpt, score_sat, source_url in cutoff_rows:
            score_type = "OFFICIAL_CUTOFF"
            uid = make_cutoff_score_uid(univ_code, int(adm_year), prog_code, method_code, score_type)
            tp_uid = make_training_program_uid(univ_code, prog_code)
            
            if tp_uid not in training_programs_map:
                training_programs_map[tp_uid] = {
                    "uid": tp_uid,
                    "university_code": univ_code,
                    "program_code": prog_code,
                    "name": prog_name,
                }
                offers_program_set.add((univ_code, tp_uid))

            cutoff_scores.append({
                "uid": uid,
                "university_code": univ_code,
                "admission_year": int(adm_year),
                "program_code": prog_code,
                "method_code": method_code,
                "score_type": score_type,
                "cutoff_score_thpt": float(score_thpt) if score_thpt is not None else None,
                "cutoff_score_sat": float(score_sat) if score_sat is not None else None,
                "source_url": source_url,
            })
            has_cutoff_relations.append({
                "training_program_uid": tp_uid,
                "cutoff_score_uid": uid,
            })

        training_programs = list(training_programs_map.values())
        offers_program_relations = [
            {"university_code": u_code, "training_program_uid": tp_uid}
            for u_code, tp_uid in sorted(offers_program_set)
        ]

    return {
        "nodes": {
            "Subject": subjects,
            "HighSchool": high_schools,
            "SubjectGroup": subject_groups,
            "University": universities,
            "TrainingProgram": training_programs,
            "AdmissionCombination": admission_combinations,
            "AptitudeTest": aptitude_tests,
            "CutoffScore": cutoff_scores,
        },
        "relationships": {
            "OFFERS_GROUP": offers_group_relations,
            "INCLUDES_SUBJECT": includes_subject_relations,
            "IS_VARIANT_OF": is_variant_of_relations,
            "OFFERS_PROGRAM": offers_program_relations,
            "ACCEPTS_COMBINATION": accepts_combination_relations,
            "REQUIRES_SUBJECT": requires_subject_relations,
            "REQUIRES_APTITUDE": requires_aptitude_relations,
            "HAS_CUTOFF": has_cutoff_relations,
        },
        "unpublished_subject_groups": list(unpublished_subject_groups_by_uid.values()),
        "raw_components": comp_rows,
    }

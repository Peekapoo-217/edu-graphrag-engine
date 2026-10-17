"""Retryable PostgreSQL-to-Neo4j projection worker for reviewed subject groups."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Any

import psycopg
from psycopg.rows import dict_row

from graph_sync.models import get_canonical_subject_code, make_subject_group_uid
from graph_sync.neo4j_writer import get_neo4j_driver, sync_nodes_and_relationships
from graph_sync.projection_lock import graph_projection_lock
from rag_api.utils.config import settings


def build_group_projection(rows: list[dict[str, Any]], school_code: str,
                           academic_year: str) -> dict[str, Any]:
    """Build a small idempotent graph projection from normalized PostgreSQL rows."""
    if not rows:
        raise ValueError("PUBLISHED_GROUPS_MISSING")

    groups: dict[str, dict[str, Any]] = {}
    unpublished_groups: dict[str, dict[str, Any]] = {}
    subjects: dict[str, dict[str, Any]] = {}
    offers_group: dict[tuple[str, str], dict[str, Any]] = {}
    includes_subject: set[tuple[str, str]] = set()
    variants: set[tuple[str, str]] = set()
    school = {
        "code": school_code,
        "name": rows[0]["school_name"],
        "location": rows[0]["location"],
    }

    for row in rows:
        code = row["group_code"]
        uid = make_subject_group_uid(school_code, academic_year, code)
        groups.setdefault(uid, {
            "uid": uid,
            "school_code": school_code,
            "academic_year": academic_year,
            "group_code": code,
            "name": row["group_name"],
            "is_published": bool(row.get("is_published", True)),
            "publication_revision": int(row.get("publication_revision", 1)),
        })
        group = groups[uid]
        if not group["is_published"]:
            unpublished_groups[uid] = {
                "uid": uid,
                "school_code": school_code,
                "academic_year": academic_year,
                "group_code": code,
                "publication_revision": group["publication_revision"],
            }
            continue

        subject_code = row["subject_code"]
        canonical = get_canonical_subject_code(subject_code, row["canonical_subject_code"])
        subjects[subject_code] = {
            "code": subject_code,
            "name": row["subject_name"],
            "subject_type": row["subject_type"],
            "is_mandatory": bool(row["is_mandatory"]),
            "canonical_subject_code": canonical,
        }
        offers_group[(school_code, uid)] = {
            "high_school_code": school_code,
            "subject_group_uid": uid,
            "school_code": school_code,
            "academic_year": academic_year,
            "group_code": code,
            "group_name": group["name"],
            "is_published": group["is_published"],
            "publication_revision": group["publication_revision"],
        }
        includes_subject.add((uid, subject_code))
        if canonical and canonical != subject_code:
            variants.add((subject_code, canonical))

    group_subject_counts: dict[str, int] = defaultdict(int)
    for uid, _subject_code in includes_subject:
        group_subject_counts[uid] += 1
    if any(count < 1 or count > 4 for count in group_subject_counts.values()):
        raise ValueError("PUBLISHED_GROUP_SUBJECT_COUNT_INVALID")

    return {
        "nodes": {
            "Subject": list(subjects.values()),
            "HighSchool": [school],
            "SubjectGroup": [group for group in groups.values() if group["is_published"]],
        },
        "relationships": {
            "OFFERS_GROUP": [
                offers_group[key]
                for key in sorted(offers_group)
            ],
            "INCLUDES_SUBJECT": [
                {"subject_group_uid": uid, "subject_code": code}
                for uid, code in sorted(includes_subject)
            ],
            "IS_VARIANT_OF": [
                {"variant_subject_code": variant, "canonical_subject_code": canonical}
                for variant, canonical in sorted(variants)
            ],
        },
        "unpublished_subject_groups": list(unpublished_groups.values()),
    }


def delete_school_projection(driver: Any, database: str, school_codes: list[str | None]) -> None:
    """Remove a school's graph nodes and its groups using parameterized Cypher."""
    codes = sorted({code for code in school_codes if code})
    if not codes:
        return
    with driver.session(database=database) as session:
        session.run(
            "UNWIND $codes AS code MATCH (sg:SubjectGroup {school_code: code}) DETACH DELETE sg",
            codes=codes,
        )
        session.run(
            "UNWIND $codes AS code MATCH (h:HighSchool {code: code}) DETACH DELETE h",
            codes=codes,
        )


def _claim_school_job() -> dict[str, Any] | None:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute(
                "UPDATE admin.high_school_graph_sync_jobs SET status='RETRY', available_at=now(), "
                "locked_at=NULL, updated_at=now() WHERE status='RUNNING' "
                "AND locked_at < now() - interval '10 minutes'"
            )
            return connection.execute(
                "WITH candidate AS ("
                "SELECT id FROM admin.high_school_graph_sync_jobs "
                "WHERE status IN ('PENDING','RETRY') AND available_at <= now() "
                "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE admin.high_school_graph_sync_jobs j SET status='RUNNING', "
                "attempt_count=j.attempt_count+1, locked_at=now(), updated_at=now() "
                "FROM candidate WHERE j.id=candidate.id "
                "RETURNING j.id::text AS id, j.school_code, j.previous_code, j.operation, j.attempt_count",
            ).fetchone()


def _load_school_projection_data(job: dict[str, Any]) -> dict[str, Any] | None:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
        school = connection.execute(
            "SELECT code, name, location FROM public.high_schools "
            "WHERE code=%s AND deleted_at IS NULL",
            (job["school_code"],),
        ).fetchone()
        if not school:
            return None
        rows = connection.execute(
            "SELECT hs.name AS school_name, hs.location, sg.academic_year, sg.group_code, sg.group_name, "
            "sg.subject_code, s.name AS subject_name, s.subject_type, s.is_mandatory, s.canonical_subject_code, "
            "coalesce(state.is_published, true) AS is_published, coalesce(state.publication_revision, 1) AS publication_revision "
            "FROM public.high_schools hs JOIN public.subject_groups sg ON sg.school_code=hs.code "
            "JOIN public.subjects s ON s.code=sg.subject_code "
            "LEFT JOIN admin.subject_group_publication_state state ON state.school_code=sg.school_code "
            "AND state.academic_year=sg.academic_year AND state.group_code=upper(sg.group_code) "
            "WHERE hs.code=%s AND hs.deleted_at IS NULL AND sg.deleted_at IS NULL "
            "ORDER BY sg.academic_year, sg.group_code, sg.subject_code",
            (job["school_code"],),
        ).fetchall()
    if not rows:
        return {
            "nodes": {"HighSchool": [dict(school)], "Subject": [], "SubjectGroup": []},
            "relationships": {"OFFERS_GROUP": [], "INCLUDES_SUBJECT": [], "IS_VARIANT_OF": []},
        }
    rows_by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_year[str(row["academic_year"])].append(row)
    projections = [build_group_projection(rows_by_year[year], job["school_code"], year)
                   for year in sorted(rows_by_year)]
    nodes: dict[str, dict[str, dict[str, Any]]] = {
        "Subject": {}, "HighSchool": {}, "SubjectGroup": {},
    }
    unpublished_groups: dict[str, dict[str, Any]] = {}
    relationships: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {
        "OFFERS_GROUP": {}, "INCLUDES_SUBJECT": {}, "IS_VARIANT_OF": {},
    }
    node_keys = {"Subject": "code", "HighSchool": "code", "SubjectGroup": "uid"}
    relationship_keys = {
        "OFFERS_GROUP": ("high_school_code", "subject_group_uid"),
        "INCLUDES_SUBJECT": ("subject_group_uid", "subject_code"),
        "IS_VARIANT_OF": ("variant_subject_code", "canonical_subject_code"),
    }
    for projection in projections:
        for label, values in projection["nodes"].items():
            for value in values:
                nodes[label][str(value[node_keys[label]])] = value
        for rel_type, values in projection["relationships"].items():
            for value in values:
                relationships[rel_type][tuple(value[key] for key in relationship_keys[rel_type])] = value
        for group in projection.get("unpublished_subject_groups", []):
            unpublished_groups[group["uid"]] = group
    return {
        "nodes": {label: list(values.values()) for label, values in nodes.items()},
        "relationships": {rel_type: list(values.values()) for rel_type, values in relationships.items()},
        "unpublished_subject_groups": list(unpublished_groups.values()),
    }


def _claim_job() -> dict[str, Any] | None:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required")
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute(
                "UPDATE admin.subject_group_graph_sync_jobs SET status='RETRY', available_at=now(), "
                "locked_at=NULL, updated_at=now() WHERE status='RUNNING' "
                "AND locked_at < now() - interval '10 minutes'"
            )
            return connection.execute(
                "WITH candidate AS ("
                "SELECT id FROM admin.subject_group_graph_sync_jobs "
                "WHERE status IN ('PENDING','RETRY') AND available_at <= now() "
                "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
                "UPDATE admin.subject_group_graph_sync_jobs j SET status='RUNNING', "
                "attempt_count=j.attempt_count+1, locked_at=now(), updated_at=now() "
                "FROM candidate WHERE j.id=candidate.id "
                "RETURNING j.id::text AS id, j.school_code, j.academic_year, j.group_codes, j.document_id::text AS document_id, j.attempt_count",
            ).fetchone()


def _load_projection_data(job: dict[str, Any]) -> dict[str, Any] | None:
    with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
        rows = connection.execute(
            "SELECT hs.name AS school_name, hs.location, sg.group_code, sg.group_name, "
            "sg.subject_code, s.name AS subject_name, s.subject_type, s.is_mandatory, s.canonical_subject_code, "
            "coalesce(state.is_published, true) AS is_published, coalesce(state.publication_revision, 1) AS publication_revision "
            "FROM public.high_schools hs JOIN public.subject_groups sg ON sg.school_code=hs.code "
            "JOIN public.subjects s ON s.code=sg.subject_code "
            "LEFT JOIN admin.subject_group_publication_state state ON state.school_code=sg.school_code "
            "AND state.academic_year=sg.academic_year AND state.group_code=upper(sg.group_code) "
            "WHERE hs.code=%s AND hs.deleted_at IS NULL AND sg.deleted_at IS NULL "
            "AND sg.academic_year=%s AND upper(sg.group_code)=ANY(%s::varchar[]) "
            "ORDER BY sg.group_code, sg.subject_code",
            (job["school_code"], job["academic_year"], [code.upper() for code in job["group_codes"]]),
        ).fetchall()
        if not rows:
            school = connection.execute(
                "SELECT deleted_at FROM public.high_schools WHERE code=%s",
                (job["school_code"],),
            ).fetchone()
            if school is not None and school["deleted_at"] is None:
                raise ValueError("PUBLISHED_GROUPS_MISSING")
            return None
    projected = build_group_projection(rows, job["school_code"], job["academic_year"])
    expected = {code.upper() for code in job["group_codes"]}
    actual = {node["group_code"].upper() for node in projected["nodes"]["SubjectGroup"]}
    actual.update(group["group_code"].upper()
                  for group in projected.get("unpublished_subject_groups", []))
    if actual != expected:
        raise ValueError("SUBJECT_GROUP_PROJECTION_INCOMPLETE")
    return projected


def run_once(driver: Any) -> bool:
    school_job = _claim_school_job()
    if school_job:
        print(f"[graph-sync] claimed school-job={school_job['id']} school={school_job['school_code']} "
              f"operation={school_job['operation']}", flush=True)
        return _run_school_job(driver, school_job)

    job = _claim_job()
    if not job:
        return False
    print(f"[graph-sync] claimed job={job['id']} document={job.get('document_id') or '-'} "
          f"school={job.get('school_code') or '-'} year={job.get('academic_year') or '-'}", flush=True)
    try:
        with graph_projection_lock(settings.DATABASE_URL):
            # Read source state only after the shared lock is acquired. A job is
            # an invalidation signal; PostgreSQL remains the source of truth.
            data = _load_projection_data(job)
            if data is not None:
                sync_nodes_and_relationships(driver, settings.NEO4J_DATABASE or "edugraph", data)
            with psycopg.connect(settings.DATABASE_URL) as connection:
                connection.execute(
                    "UPDATE admin.subject_group_graph_sync_jobs SET status='SYNCED', locked_at=NULL, "
                    "error_code=NULL, updated_at=now() WHERE id=%s::uuid AND status='RUNNING'",
                    (job["id"],),
                )
    except Exception as error:
        delay_seconds = min(1800, 5 * (2 ** min(job["attempt_count"] - 1, 9)))
        with psycopg.connect(settings.DATABASE_URL) as connection:
            connection.execute(
                "UPDATE admin.subject_group_graph_sync_jobs SET status='RETRY', locked_at=NULL, "
                "available_at=now() + (%s * interval '1 second'), error_code='GRAPH_SYNC_FAILED', updated_at=now() "
                "WHERE id=%s::uuid AND status='RUNNING'",
                (delay_seconds, job["id"]),
            )
        print(f"[graph-sync] retry job={job['id']} document={job.get('document_id') or '-'} "
              f"error={type(error).__name__} delay_seconds={delay_seconds}", flush=True)
        return True

    print(f"[graph-sync] synced job={job['id']} document={job.get('document_id') or '-'}", flush=True)
    return True


def _run_school_job(driver: Any, job: dict[str, Any]) -> bool:
    database = settings.NEO4J_DATABASE or "edugraph"
    try:
        # Use the same lock as document projection jobs and full sync. Re-read
        # school state only after obtaining it so stale requests reconcile to PG.
        with graph_projection_lock(settings.DATABASE_URL):
            with psycopg.connect(settings.DATABASE_URL, row_factory=dict_row) as connection:
                with connection.transaction():
                    school = connection.execute(
                        "SELECT code, deleted_at FROM public.high_schools WHERE code=%s FOR UPDATE",
                        (job["school_code"],),
                    ).fetchone()
                    if not school:
                        raise ValueError("SCHOOL_PROJECTION_SOURCE_MISSING")
                    if school["deleted_at"] is not None:
                        delete_school_projection(driver, database,
                                                 [job["school_code"], job.get("previous_code")])
                    else:
                        delete_school_projection(driver, database, [job.get("previous_code")])
                        data = _load_school_projection_data(job)
                        if data is None:
                            raise ValueError("ACTIVE_SCHOOL_PROJECTION_MISSING")
                        sync_nodes_and_relationships(driver, database, data)
                    connection.execute(
                        "UPDATE admin.high_school_graph_sync_jobs SET status='SYNCED', locked_at=NULL, "
                        "error_code=NULL, updated_at=now() WHERE id=%s::uuid AND status='RUNNING'",
                        (job["id"],),
                    )
    except Exception as error:
        delay_seconds = min(1800, 5 * (2 ** min(job["attempt_count"] - 1, 9)))
        with psycopg.connect(settings.DATABASE_URL) as connection:
            connection.execute(
                "UPDATE admin.high_school_graph_sync_jobs SET status='RETRY', locked_at=NULL, "
                "available_at=now() + (%s * interval '1 second'), error_code='GRAPH_SYNC_FAILED', updated_at=now() "
                "WHERE id=%s::uuid AND status='RUNNING'",
                (delay_seconds, job["id"]),
            )
        print(f"[graph-sync] retry school-job={job['id']} school={job['school_code']} "
              f"error={type(error).__name__} delay_seconds={delay_seconds}", flush=True)
        return True

    print(f"[graph-sync] synced school-job={job['id']} school={job['school_code']}", flush=True)
    return True


def main() -> None:
    required = (settings.DATABASE_URL, settings.NEO4J_URI, settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD)
    if not all(required):
        raise RuntimeError("DATABASE_URL and Neo4j connection settings are required")
    driver = get_neo4j_driver(settings.NEO4J_URI, (settings.NEO4J_USERNAME, settings.NEO4J_PASSWORD))
    try:
        while True:
            if not run_once(driver):
                time.sleep(max(1, int(os.getenv("GRAPH_SYNC_POLL_SECONDS", "5"))))
    finally:
        driver.close()


if __name__ == "__main__":
    main()

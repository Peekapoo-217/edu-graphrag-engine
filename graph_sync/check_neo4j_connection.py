"""
Read-only Neo4j Connection & Database Selection Diagnostic Tool.
Inspects configured URI, Database name, active session database, and constraint status across databases.
"""

import os
import sys
from typing import Any, Dict, List, Set, Tuple, Optional
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


def load_env_file(env_path: str = ".env") -> Dict[str, str]:
    """Inspect .env file and return key-value pairs."""
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"\'')
    return env_vars


def run_diagnostic():
    # 1. Inspect Source of Environment Variables
    env_file_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
    env_file_vars = load_env_file(env_file_path)

    # Determine origins
    uri_origin = "PowerShell/Process Env" if "NEO4J_URI" in os.environ else (".env file" if "NEO4J_URI" in env_file_vars else "Default Fallback")
    user_origin = "PowerShell/Process Env" if "NEO4J_USERNAME" in os.environ else (".env file" if "NEO4J_USERNAME" in env_file_vars else "Default Fallback")
    db_origin = "PowerShell/Process Env" if "NEO4J_DATABASE" in os.environ else (".env file" if "NEO4J_DATABASE" in env_file_vars else "Default Fallback")
    pw_origin = "PowerShell/Process Env" if "NEO4J_PASSWORD" in os.environ else (".env file" if "NEO4J_PASSWORD" in env_file_vars else "Not Set")

    # Load from dotenv if available
    try:
        from dotenv import load_dotenv
        load_dotenv(env_file_path)
    except ImportError:
        for k, v in env_file_vars.items():
            os.environ.setdefault(k, v)

    neo4j_uri = os.environ.get("NEO4J_URI", "neo4j://127.0.0.1:7687")
    neo4j_user = os.environ.get("NEO4J_USERNAME", "neo4j")
    neo4j_password = os.environ.get("NEO4J_PASSWORD")
    configured_db = os.environ.get("NEO4J_DATABASE", "edugraph")

    if not neo4j_password:
        print("[ERROR] NEO4J_PASSWORD is not set in environment or .env file.", file=sys.stderr)
        sys.exit(1)

    try:
        driver = neo4j.GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
        driver.verify_connectivity()
    except Exception as e:
        print(f"[ERROR] Failed to connect to Neo4j at {neo4j_uri}: {e}", file=sys.stderr)
        sys.exit(1)

    actual_db_name = "Unknown"
    # Query actual database name via Cypher
    try:
        with driver.session(database=configured_db) as session:
            try:
                res = session.run("CALL db.info() YIELD name RETURN name;")
                rec = res.single()
                if rec and rec["name"]:
                    actual_db_name = rec["name"]
            except Exception:
                # Fallback check
                actual_db_name = configured_db
    except Exception as e:
        actual_db_name = f"Error connecting to '{configured_db}': {e}"

    # Check available databases in Neo4j instance
    all_databases = []
    try:
        with driver.session() as session:
            res_dbs = session.run("SHOW DATABASES YIELD name, currentStatus, default RETURN name, currentStatus, default;")
            for r in res_dbs:
                all_databases.append(dict(r))
    except Exception:
        pass

    # Helper function to get constraints for a database
    def get_db_constraints(db_name: str) -> Tuple[Set[Tuple[str, str]], List[Dict[str, Any]]]:
        found_pairs = set()
        raw_list = []
        try:
            with driver.session(database=db_name) as session:
                res = session.run("SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties RETURN name, labelsOrTypes, properties;")
                for rec in res:
                    r_dict = rec.data()
                    raw_list.append(r_dict)
                    labels = r_dict.get("labelsOrTypes")
                    props = r_dict.get("properties")
                    if labels and props:
                        l_val = labels[0] if isinstance(labels, list) else labels
                        p_val = props[0] if isinstance(props, list) else props
                        found_pairs.add((l_val, p_val))
        except Exception:
            pass
        return found_pairs, raw_list

    # Get constraints on configured database
    config_db_pairs, config_db_raw = get_db_constraints(configured_db)

    # Check if constraints exist in other databases (e.g. 'neo4j', 'system')
    db_constraint_map = {}
    for db_info in all_databases:
        dname = db_info.get("name")
        if dname:
            pairs, _ = get_db_constraints(dname)
            db_constraint_map[dname] = pairs

    driver.close()

    # Format Output
    print("Neo4j connection diagnostic")
    print(f"- URI: {neo4j_uri} (Origin: {uri_origin})")
    print(f"- Configured database: {configured_db} (Origin: {db_origin})")
    print(f"- Actual database from CALL db.info(): {actual_db_name}")
    print(f"- Constraint count in '{configured_db}': {len(config_db_raw)}")
    print("- Constraints:")

    matched_count = 0
    for label, prop in REQUIRED_CONSTRAINTS:
        status = "PRESENT" if (label, prop) in config_db_pairs else "MISSING"
        if status == "PRESENT":
            matched_count += 1
        print(f"  - {label}({prop}): {status}")

    # Check if constraints were created in a DIFFERENT database
    misplaced_db = None
    for dname, pairs in db_constraint_map.items():
        if dname != configured_db:
            matched_other = sum(1 for label, prop in REQUIRED_CONSTRAINTS if (label, prop) in pairs)
            if matched_other > 0:
                misplaced_db = (dname, matched_other)

    print("\n--- Diagnostic Analysis ---")
    if matched_count == 8:
        print(f"RESULT: PASS - All 8 required unique constraints are present in database '{configured_db}'.")
    else:
        print(f"RESULT: FAIL - Only {matched_count}/8 required unique constraints are present in database '{configured_db}'.")
        if misplaced_db:
            other_db_name, cnt = misplaced_db
            print(f"  [DISCOVERY] Found {cnt}/8 constraints located in database '{other_db_name}' instead of '{configured_db}'!")
            print(f"  [ROOT CAUSE] The constraints were created in database '{other_db_name}', but sync is configured to connect to '{configured_db}'.")
            print(f"  [RECOMMENDED FIX] Set NEO4J_DATABASE={other_db_name} in .env OR create constraints inside database '{configured_db}'.")
        elif len(all_databases) > 0 and not any(configured_db == d.get("name") for d in all_databases):
            print(f"  [DISCOVERY] Configured database '{configured_db}' does not exist in Neo4j instance.")
            print(f"  Available databases: {[d.get('name') for d in all_databases]}")
        else:
            print(f"  [ROOT CAUSE] Database '{configured_db}' is accessible, but constraints have not been created yet in this database.")

    return matched_count == 8


if __name__ == "__main__":
    success = run_diagnostic()
    sys.exit(0 if success else 1)

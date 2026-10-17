"""
Data integrity validation module for --dry-run and pre-sync verification.
"""

from typing import Any, Dict, List, Set, Tuple


def validate_graph_data(graph_data: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Perform rigorous validation of extracted nodes and relationships.
    Returns (is_valid, list_of_error_strings).
    """
    errors: List[str] = []
    nodes = graph_data["nodes"]
    rels = graph_data["relationships"]
    raw_components = graph_data.get("raw_components", [])

    # 1. Collect UIDs and check for duplicates
    subject_codes: Set[str] = set()
    high_school_codes: Set[str] = set()
    subject_group_uids: Set[str] = set()
    university_codes: Set[str] = set()
    training_program_uids: Set[str] = set()
    combination_ids: Set[str] = set()
    aptitude_codes: Set[str] = set()
    cutoff_score_uids: Set[str] = set()

    # Subject duplicates
    for s in nodes["Subject"]:
        code = s["code"]
        if code in subject_codes:
            errors.append(f"Duplicate Subject code: '{code}'")
        subject_codes.add(code)

    # HighSchool duplicates
    for hs in nodes["HighSchool"]:
        code = hs["code"]
        if code in high_school_codes:
            errors.append(f"Duplicate HighSchool code: '{code}'")
        high_school_codes.add(code)

    # SubjectGroup duplicates
    for sg in nodes["SubjectGroup"]:
        uid = sg["uid"]
        if uid in subject_group_uids:
            errors.append(f"Duplicate SubjectGroup uid: '{uid}'")
        subject_group_uids.add(uid)

    unpublished_group_uids: Set[str] = set()
    for group in graph_data.get("unpublished_subject_groups", []):
        uid = group.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            errors.append("Unpublished SubjectGroup is missing a valid uid")
            continue
        if uid in unpublished_group_uids:
            errors.append(f"Duplicate unpublished SubjectGroup uid: '{uid}'")
        if uid in subject_group_uids:
            errors.append(f"SubjectGroup cannot be both published and deleted: '{uid}'")
        unpublished_group_uids.add(uid)
        if not group.get("school_code") or not group.get("academic_year") or not group.get("group_code"):
            errors.append(f"Unpublished SubjectGroup '{uid}' is missing its school/year/group scope")
        elif uid != f"{group['school_code']}|{group['academic_year']}|{group['group_code']}":
            errors.append(f"Unpublished SubjectGroup '{uid}' does not match its school/year/group scope")
        revision = group.get("publication_revision")
        if not isinstance(revision, int) or revision < 1:
            errors.append(f"Unpublished SubjectGroup '{uid}' has an invalid publication revision")

    # University duplicates
    for u in nodes["University"]:
        code = u["code"]
        if code in university_codes:
            errors.append(f"Duplicate University code: '{code}'")
        university_codes.add(code)

    # TrainingProgram duplicates
    for tp in nodes["TrainingProgram"]:
        uid = tp["uid"]
        if uid in training_program_uids:
            errors.append(f"Duplicate TrainingProgram uid: '{uid}'")
        training_program_uids.add(uid)

    # AdmissionCombination duplicates
    for c in nodes["AdmissionCombination"]:
        cid = c["id"]
        if cid in combination_ids:
            errors.append(f"Duplicate AdmissionCombination id: '{cid}'")
        combination_ids.add(cid)

    # AptitudeTest duplicates
    for a in nodes["AptitudeTest"]:
        code = a["code"]
        if code in aptitude_codes:
            errors.append(f"Duplicate AptitudeTest code: '{code}'")
        aptitude_codes.add(code)

    # CutoffScore duplicates
    for cs in nodes["CutoffScore"]:
        uid = cs["uid"]
        if uid in cutoff_score_uids:
            errors.append(f"Duplicate CutoffScore uid: '{uid}'")
        cutoff_score_uids.add(uid)

    # 2. Check relationship references to non-existent nodes
    for og in rels["OFFERS_GROUP"]:
        if og["high_school_code"] not in high_school_codes:
            errors.append(f"OFFERS_GROUP references missing HighSchool: '{og['high_school_code']}'")
        if og["subject_group_uid"] not in subject_group_uids:
            errors.append(f"OFFERS_GROUP references missing SubjectGroup: '{og['subject_group_uid']}'")

    for inc in rels["INCLUDES_SUBJECT"]:
        if inc["subject_group_uid"] not in subject_group_uids:
            errors.append(f"INCLUDES_SUBJECT references missing SubjectGroup: '{inc['subject_group_uid']}'")
        if inc["subject_code"] not in subject_codes:
            errors.append(f"INCLUDES_SUBJECT references missing Subject: '{inc['subject_code']}'")

    for var in rels["IS_VARIANT_OF"]:
        if var["variant_subject_code"] not in subject_codes:
            errors.append(f"IS_VARIANT_OF references missing variant Subject: '{var['variant_subject_code']}'")
        if var["canonical_subject_code"] not in subject_codes:
            errors.append(f"IS_VARIANT_OF references missing canonical Subject: '{var['canonical_subject_code']}'")

    for op in rels["OFFERS_PROGRAM"]:
        if op["university_code"] not in university_codes:
            errors.append(f"OFFERS_PROGRAM references missing University: '{op['university_code']}'")
        if op["training_program_uid"] not in training_program_uids:
            errors.append(f"OFFERS_PROGRAM references missing TrainingProgram: '{op['training_program_uid']}'")

    for ac in rels["ACCEPTS_COMBINATION"]:
        if ac["training_program_uid"] not in training_program_uids:
            errors.append(f"ACCEPTS_COMBINATION references missing TrainingProgram: '{ac['training_program_uid']}'")
        if ac["combination_id"] not in combination_ids:
            errors.append(f"ACCEPTS_COMBINATION references missing AdmissionCombination: '{ac['combination_id']}'")

    for rs in rels["REQUIRES_SUBJECT"]:
        if rs["combination_id"] not in combination_ids:
            errors.append(f"REQUIRES_SUBJECT references missing AdmissionCombination: '{rs['combination_id']}'")
        if rs["subject_code"] not in subject_codes:
            errors.append(f"REQUIRES_SUBJECT references missing Subject: '{rs['subject_code']}'")

    for ra in rels["REQUIRES_APTITUDE"]:
        if ra["combination_id"] not in combination_ids:
            errors.append(f"REQUIRES_APTITUDE references missing AdmissionCombination: '{ra['combination_id']}'")
        if ra["aptitude_code"] not in aptitude_codes:
            errors.append(f"REQUIRES_APTITUDE references missing AptitudeTest: '{ra['aptitude_code']}'")

    for hc in rels["HAS_CUTOFF"]:
        if hc["training_program_uid"] not in training_program_uids:
            errors.append(f"HAS_CUTOFF references missing TrainingProgram: '{hc['training_program_uid']}'")
        if hc["cutoff_score_uid"] not in cutoff_score_uids:
            errors.append(f"HAS_CUTOFF references missing CutoffScore: '{hc['cutoff_score_uid']}'")

    # 3. Check Component integrity
    # raw_components items: (comb_id, comp_code, comp_name, comp_type, subj_code, weight)
    comb_component_counts: Dict[str, int] = {}
    for comb_id, comp_code, comp_name, comp_type, subj_code, weight in raw_components:
        comb_component_counts[comb_id] = comb_component_counts.get(comb_id, 0) + 1
        
        if comp_type == "SUBJECT":
            if not subj_code:
                errors.append(f"Component '{comp_code}' in combination '{comb_id}' is type SUBJECT but missing subject_code")
        elif comp_type == "APTITUDE":
            if subj_code is not None:
                errors.append(f"Component '{comp_code}' in combination '{comb_id}' is type APTITUDE but has subject_code '{subj_code}'")

    # Verify combination component count match
    for c in nodes["AdmissionCombination"]:
        cid = c["id"]
        expected_cnt = c["component_count"]
        actual_cnt = comb_component_counts.get(cid, 0)
        if actual_cnt != expected_cnt:
            errors.append(
                f"AdmissionCombination '{cid}' component count mismatch: expected {expected_cnt}, actual {actual_cnt}"
            )

    return len(errors) == 0, errors

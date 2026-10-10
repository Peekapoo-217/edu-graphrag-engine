"""
Data models and UID generator utilities for graph node & relationship entities.
"""

from dataclasses import dataclass
from typing import Dict, Optional

# Variant subject mapping dictionary
CANONICAL_SUBJECT_MAP: Dict[str, str] = {
    "TECH_INDUSTRY": "TECHNOLOGY",
    "TECH_AGRI": "TECHNOLOGY",
    "TECH_PHYSICS": "TECHNOLOGY",
    "TECH_BIOLOGY": "TECHNOLOGY",
}


def get_canonical_subject_code(code: str, existing_canonical: Optional[str] = None) -> Optional[str]:
    """
    Determine canonical_subject_code for a subject code.
    Returns existing_canonical if provided, otherwise checks CANONICAL_SUBJECT_MAP.
    Returns None if the subject is itself canonical.
    """
    if existing_canonical and existing_canonical != code:
        return existing_canonical
    mapped = CANONICAL_SUBJECT_MAP.get(code)
    if mapped and mapped != code:
        return mapped
    return existing_canonical


def make_subject_group_uid(school_code: str, academic_year: str, group_code: str) -> str:
    """Generate UID for SubjectGroup: school_code|academic_year|group_code"""
    return f"{school_code}|{academic_year}|{group_code}"


def make_training_program_uid(university_code: str, program_code: str) -> str:
    """Generate UID for TrainingProgram: university_code|program_code"""
    return f"{university_code}|{program_code}"


def make_cutoff_score_uid(
    university_code: str,
    admission_year: int,
    program_code: str,
    method_code: str,
    score_type: str,
) -> str:
    """Generate UID for CutoffScore: university_code|admission_year|program_code|method_code|score_type"""
    return f"{university_code}|{admission_year}|{program_code}|{method_code}|{score_type}"


@dataclass
class SubjectNode:
    code: str
    name: str
    subject_type: str
    is_mandatory: bool
    canonical_subject_code: Optional[str] = None


@dataclass
class HighSchoolNode:
    code: str
    name: str
    location: Optional[str] = None


@dataclass
class SubjectGroupNode:
    uid: str
    school_code: str
    academic_year: str
    group_code: str
    name: str


@dataclass
class UniversityNode:
    code: str
    name: str
    website: Optional[str] = None


@dataclass
class TrainingProgramNode:
    uid: str
    university_code: str
    program_code: str
    name: str


@dataclass
class AdmissionCombinationNode:
    id: str
    display_name: str
    component_count: int


@dataclass
class AptitudeTestNode:
    code: str
    name: str


@dataclass
class CutoffScoreNode:
    uid: str
    university_code: str
    admission_year: int
    program_code: str
    method_code: str
    score_type: str
    cutoff_score_thpt: Optional[float] = None
    cutoff_score_sat: Optional[float] = None
    source_url: Optional[str] = None

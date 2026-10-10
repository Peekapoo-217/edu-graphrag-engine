#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/common/models.py

Data structures and type definitions for multi-university admissions crawler pipeline.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any


@dataclass
class SubjectItem:
    subject_code: str
    subject_name: str
    component_type: str  # 'SUBJECT' or 'APTITUDE'
    weight: float = 1.0
    source_label: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "subject_code": self.subject_code,
            "subject_name": self.subject_name,
            "component_type": self.component_type,
            "weight": self.weight,
            "source_label": self.source_label,
        }


@dataclass
class AdmissionOfferRecord:
    university_code: str
    admission_year: int
    program_code: str
    program_name: str
    method_codes: str
    quota: Optional[int]
    combination_id: str
    combination_display: str
    canonical_combination_id: str
    subjects_json: str
    source_url: str
    retrieved_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CutoffScoreRecord:
    university_code: str
    admission_year: int
    program_code: str
    program_name: str
    method_code: str
    score_type: str
    cutoff_score_thpt: Optional[float]
    cutoff_score_sat: Optional[float]
    source_url: str
    retrieved_at: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class UniversityConfig:
    university_code: str
    university_name: str
    website: str
    crawler: str

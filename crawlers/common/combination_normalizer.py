#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/common/combination_normalizer.py

Combination normalization module.
Generates canonical combination IDs and ordered display strings.
"""

from typing import List, Tuple
from crawlers.common.models import SubjectItem


def generate_canonical_combination_id(subject_codes: List[str]) -> str:
    """
    Generate canonical combination ID by sorting component subject codes alphabetically
    and joining them with '_'.
    Example: ['MATH', 'PHYSICS', 'CHEMISTRY'] -> 'CHEMISTRY_MATH_PHYSICS'
    """
    cleaned_codes = [c.strip() for c in subject_codes if c and c.strip()]
    sorted_codes = sorted(cleaned_codes)
    return "_".join(sorted_codes)


def format_combination_display(subjects: List[SubjectItem]) -> str:
    """
    Format combination display string.
    Rule: SUBJECT components first, APTITUDE components after.
    Example: (Ngữ văn, Toán, NK GDMN)
    """
    subject_comps = [s for s in subjects if s.component_type == "SUBJECT"]
    aptitude_comps = [s for s in subjects if s.component_type == "APTITUDE"]

    ordered_subjects = subject_comps + aptitude_comps
    names = [s.subject_name for s in ordered_subjects]
    return f"({', '.join(names)})"

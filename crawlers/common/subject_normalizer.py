#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawlers/common/subject_normalizer.py

Subject mapping and normalization module.
Standardizes Vietnamese subject labels to standard subject codes and component types.
"""

from typing import Dict, Tuple, Optional
from crawlers.common.models import SubjectItem

# Standard Subjects Map
# Normalized lowercase string -> (subject_code, canonical_name, component_type)
STANDARD_SUBJECTS: Dict[str, Tuple[str, str, str]] = {
    "toán": ("MATH", "Toán", "SUBJECT"),
    "văn": ("LITERATURE", "Văn", "SUBJECT"),
    "ngữ văn": ("LITERATURE", "Văn", "SUBJECT"),
    "anh": ("ENGLISH", "Anh", "SUBJECT"),
    "tiếng anh": ("ENGLISH", "Anh", "SUBJECT"),
    "lý": ("PHYSICS", "Lý", "SUBJECT"),
    "vật lý": ("PHYSICS", "Lý", "SUBJECT"),
    "vật lí": ("PHYSICS", "Lý", "SUBJECT"),
    "hóa": ("CHEMISTRY", "Hóa", "SUBJECT"),
    "hóa học": ("CHEMISTRY", "Hóa", "SUBJECT"),
    "sinh": ("BIOLOGY", "Sinh", "SUBJECT"),
    "sinh học": ("BIOLOGY", "Sinh", "SUBJECT"),
    "sử": ("HISTORY", "Sử", "SUBJECT"),
    "lịch sử": ("HISTORY", "Sử", "SUBJECT"),
    "địa": ("GEOGRAPHY", "Địa", "SUBJECT"),
    "địa lý": ("GEOGRAPHY", "Địa", "SUBJECT"),
    "địa lí": ("GEOGRAPHY", "Địa", "SUBJECT"),
    "tin": ("INFORMATICS", "Tin", "SUBJECT"),
    "tin học": ("INFORMATICS", "Tin", "SUBJECT"),
    "công nghệ": ("TECHNOLOGY", "Công nghệ", "SUBJECT"),
    "giáo dục kt và pl": ("ECONOMIC_LAW", "Giáo dục KT và PL", "SUBJECT"),
    "gdkt & pl": ("ECONOMIC_LAW", "Giáo dục KT và PL", "SUBJECT"),
    "gdkt và pl": ("ECONOMIC_LAW", "Giáo dục KT và PL", "SUBJECT"),
    "gdktpl": ("ECONOMIC_LAW", "Giáo dục KT và PL", "SUBJECT"),
    "giáo dục kinh tế và pháp luật": ("ECONOMIC_LAW", "Giáo dục KT và PL", "SUBJECT"),
    "tiếng trung": ("CHINESE", "Tiếng Trung", "SUBJECT"),
    # Aptitude tests
    "nk gdmn": ("APTITUDE_EARLY_CHILDHOOD", "NK GDMN", "APTITUDE"),
    "năng khiếu mầm non": ("APTITUDE_EARLY_CHILDHOOD", "NK GDMN", "APTITUDE"),
    "nk tdtt": ("APTITUDE_SPORTS", "NK TDTT", "APTITUDE"),
    "năng khiếu thể dục thể thao": ("APTITUDE_SPORTS", "NK TDTT", "APTITUDE"),
    "nk âm nhạc": ("APTITUDE_MUSIC", "Năng khiếu Âm nhạc", "APTITUDE"),
    "nk mỹ thuật": ("APTITUDE_FINE_ARTS", "Năng khiếu Mỹ thuật", "APTITUDE"),
}

# Standard known subject codes set for quick validation
VALID_STANDARD_SUBJECT_CODES = {
    "MATH", "LITERATURE", "ENGLISH", "PHYSICS", "CHEMISTRY",
    "BIOLOGY", "HISTORY", "GEOGRAPHY", "INFORMATICS", "TECHNOLOGY",
    "ECONOMIC_LAW", "CHINESE"
}


def normalize_subject_label(
    raw_label: str,
    university_code: str = "",
    source_url: str = ""
) -> Tuple[Optional[SubjectItem], Optional[str]]:
    """
    Normalize a raw subject label string to a SubjectItem.
    Returns (SubjectItem, error_message).
    If mapping is found, error_message is None.
    If mapping is not found, returns (None, error_message).
    """
    cleaned = raw_label.strip()
    norm = cleaned.lower()

    if norm in STANDARD_SUBJECTS:
        code, name, comp_type = STANDARD_SUBJECTS[norm]
        return SubjectItem(
            subject_code=code,
            subject_name=name,
            component_type=comp_type,
            weight=1.0,
            source_label=cleaned
        ), None

    # Handle legacy codes or direct code inputs
    if cleaned in VALID_STANDARD_SUBJECT_CODES:
        return SubjectItem(
            subject_code=cleaned,
            subject_name=cleaned,
            component_type="SUBJECT",
            weight=1.0,
            source_label=cleaned
        ), None

    if cleaned == "ECONOMIC_LAW_ED":
        return SubjectItem(
            subject_code="ECONOMIC_LAW",
            subject_name="Giáo dục KT và PL",
            component_type="SUBJECT",
            weight=1.0,
            source_label=cleaned
        ), None

    if cleaned.startswith("APTITUDE_"):
        return SubjectItem(
            subject_code=cleaned,
            subject_name=cleaned,
            component_type="APTITUDE",
            weight=1.0,
            source_label=cleaned
        ), None

    # Unmapped subject label -> return validation error
    err = (
        f"[UNMAPPED SUBJECT] Unknown subject label '{cleaned}' encountered. "
        f"University: '{university_code}', Source URL: '{source_url}'"
    )
    return None, err

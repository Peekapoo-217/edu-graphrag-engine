"""Expand explicit catalogue alternatives without inventing missing subjects."""

from dataclasses import dataclass
import hashlib
from itertools import product

from crawl_combination_labels import clean_text
from crawlers.common.models import SubjectItem
from crawlers.common.subject_normalizer import normalize_subject_label


# Exact catalogue terms; technology variants already exist in PostgreSQL.
# Foreign languages are explicit subjects, not aliases for English.
CATALOGUE_SUBJECTS: dict[str, tuple[str, str]] = {
    "công nghệ công nghiệp": ("TECH_INDUSTRY", "Công nghệ công nghiệp"),
    "công nghệ nông nghiệp": ("TECH_AGRI", "Công nghệ nông nghiệp"),
    "tiếng pháp": ("FRENCH", "Tiếng Pháp"),
    "tiếng nga": ("RUSSIAN", "Tiếng Nga"),
    "tiếng đức": ("GERMAN", "Tiếng Đức"),
    "tiếng hàn": ("KOREAN", "Tiếng Hàn"),
    "tiếng nhật": ("JAPANESE", "Tiếng Nhật"),
    "hoá": ("CHEMISTRY", "Hóa học"),
    "gdkt&pl": ("ECONOMIC_LAW", "Giáo dục kinh tế và pháp luật"),
    "ngoại ngữ": ("FOREIGN_LANGUAGE_UNSPECIFIED", "Ngoại ngữ (chưa xác định)"),
}
NEW_SUBJECTS: dict[str, str] = {
    "FRENCH": "Tiếng Pháp",
    "RUSSIAN": "Tiếng Nga",
    "GERMAN": "Tiếng Đức",
    "KOREAN": "Tiếng Hàn",
    "JAPANESE": "Tiếng Nhật",
    "FOREIGN_LANGUAGE_UNSPECIFIED": "Ngoại ngữ (chưa xác định)",
}

# These are entrance tests or performance tasks, never school subjects.
CATALOGUE_APTITUDE_ALIASES: dict[str, tuple[str, str]] = {
    "năng khiếu tdtt": ("APTITUDE_SPORTS", "Năng khiếu TDTT"),
    "năng khiếu giáo dục mầm non": ("APTITUDE_EARLY_CHILDHOOD", "Năng khiếu Giáo dục mầm non"),
    "vẽ mỹ thuật": ("APTITUDE_FINE_ARTS", "Vẽ mỹ thuật"),
}
NAMED_ASSESSMENTS = {
    "vẽ hình họa mỹ thuật", "năng khiếu nghệ thuật", "năng khiếu 1", "năng khiếu 2",
    "năng khiếu báo chí", "năng khiếu vẽ nghệ thuật 1", "năng khiếu vẽ nghệ thuật 2",
    "năng khiếu âm nhạc 1", "năng khiếu âm nhạc 2", "đọc diễn cảm", "hát",
    "vẽ trang trí màu", "vẽ năng khiếu", "nk mầm non 1( kể chuyện, đọc, diễn cảm)",
    "nk mầm non 2 (hát)", "xướng âm", "biểu diễn nghệ thuật", "năng khiếu skđa 1",
    "năng khiếu skđa 2", "hình họa", "trang trí", "năng kiếu báo chí",
    "xây dựng kịch bản sự kiện", "đọc kể diễn cảm", "hát - múa", "ký xướng âm",
    "hát hoặc biểu diễn nhạc cụ", "năng khiếu âm nhạc", "năng khiếu thuyết trình",
    "đọc hiểu", "tư duy khoa học giải quyết vấn đề", "tư duy định lượng",
    "tư duy định tính", "biểu diễn nghệ thuật", "năng khiếu văn hóa nghệ thuật",
    "năng khiếu đọc diễn cảm và hát",
    # Literal, source-scoped placeholders: no specific test or science subject is inferred.
    "năng khiếu", "nk1", "khoa học", "khoa học tự nhiên", "khoa học xã hội",
}


def synthetic_source_code(raw_code: str, details: str) -> str:
    """Identify a source row whose public code is malformed; SRC is not official."""
    digest = hashlib.sha256(
        f"{clean_text(raw_code)}\x1f{clean_text(details)}".encode("utf-8")
    ).hexdigest()[:7].upper()
    return f"SRC{digest}"


@dataclass(frozen=True)
class CatalogueComponent:
    code: str
    name: str
    component_type: str
    subject_code: str | None


def normalize_catalogue_component(raw_label: str, public_code: str) -> CatalogueComponent | None:
    """Classify a known subject or a named test, scoped to its public code."""
    item = normalize_catalogue_subject(raw_label)
    if item is not None:
        return CatalogueComponent(
            item.subject_code, item.subject_name, item.component_type,
            item.subject_code if item.component_type == "SUBJECT" else None,
        )
    cleaned = clean_text(raw_label)
    key = cleaned.casefold()
    if key in CATALOGUE_APTITUDE_ALIASES:
        code, name = CATALOGUE_APTITUDE_ALIASES[key]
        return CatalogueComponent(code, name, "APTITUDE", None)
    if key in NAMED_ASSESSMENTS:
        # Generic test names such as "Năng khiếu 1" can mean different tests
        # under different public codes; scope their identity to that code.
        digest = hashlib.sha256(f"{public_code}|{key}".encode("utf-8")).hexdigest()[:12]
        return CatalogueComponent(f"APTITUDE_CATALOGUE_{digest}", cleaned, "APTITUDE", None)
    return None


def normalize_catalogue_subject(raw_label: str) -> SubjectItem | None:
    """Use the shared normalizer, then exact aliases from this catalogue."""
    item, _ = normalize_subject_label(raw_label)
    if item is not None:
        return item
    entry = CATALOGUE_SUBJECTS.get(clean_text(raw_label).casefold())
    if entry is None:
        return None
    code, name = entry
    return SubjectItem(code, name, "SUBJECT", source_label=clean_text(raw_label))


def split_components(details: str) -> list[str]:
    """Split commas between components, keeping commas inside parentheses."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for character in details:
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise ValueError("Unbalanced parentheses in subject details")
        if character == "," and depth == 0:
            parts.append(clean_text("".join(current)))
            current = []
        else:
            current.append(character)
    if depth:
        raise ValueError("Unbalanced parentheses in subject details")
    parts.append(clean_text("".join(current)))
    if any(not part for part in parts):
        raise ValueError("Empty component in subject details")
    return parts


def expand_details(details: str) -> list[str]:
    """Turn an explicit slash choice into one row for each concrete option."""
    options = []
    for part in split_components(details):
        choices = [clean_text(choice) for choice in part.split("/")]
        if any(not choice for choice in choices):
            raise ValueError("Empty slash choice in subject details")
        options.append(choices)
    total = 1
    for choices in options:
        total *= len(choices)
    if total > 32:
        raise ValueError("Too many combinations in subject details")
    if total == 1:
        return [clean_text(details)]
    return list(dict.fromkeys(", ".join(parts) for parts in product(*options)))

# Module `crawlers/common/` - Tiện ích chung cho Crawlers

## 1. Mục đích

Package chứa các tiện ích chung được sử dụng bởi tất cả crawler adapter: mô hình dữ liệu, chuẩn hóa môn học, chuẩn hóa tổ hợp môn, và kiểm tra hợp lệ CSV.

## 2. Cấu trúc

```
crawlers/common/
├── models.py                  # Dataclass models
├── subject_normalizer.py      # Chuẩn hóa nhãn môn học
├── combination_normalizer.py  # Chuẩn hóa tổ hợp môn
├── validators.py              # Kiểm tra hợp lệ CSV
└── __init__.py
```

## 3. `models.py` - Mô hình dữ liệu

### 3.1 `SubjectItem`

```python
@dataclass
class SubjectItem:
    subject_code: str       # Mã môn chuẩn: "MATH", "PHYSICS", "APTITUDE_EARLY_CHILDHOOD"
    subject_name: str       # Tên hiển thị: "Toán", "Lý", "NK GDMN"
    component_type: str     # "SUBJECT" hoặc "APTITUDE"
    weight: float = 1.0     # Trọng số (mặc định 1.0)
    source_label: str = ""  # Nhãn gốc từ nguồn dữ liệu

    def to_dict() -> Dict[str, Any]
```

### 3.2 `AdmissionOfferRecord`

```python
@dataclass
class AdmissionOfferRecord:
    university_code: str
    admission_year: int
    program_code: str
    program_name: str
    method_codes: str           # Phân cách bằng dấu phẩy
    quota: Optional[int]
    combination_id: str         # Mã tổ hợp thô
    combination_display: str    # Hiển thị: "(Toán, Lý, Hóa)"
    canonical_combination_id: str  # Mã chuẩn: "CHEMISTRY_MATH_PHYSICS"
    subjects_json: str          # JSON string của List[SubjectItem]
    source_url: str
    retrieved_at: str           # ISO 8601
```

### 3.3 `CutoffScoreRecord`

```python
@dataclass
class CutoffScoreRecord:
    university_code: str
    admission_year: int
    program_code: str
    program_name: str
    method_code: str
    score_type: str             # "OFFICIAL_CUTOFF"
    cutoff_score_thpt: Optional[float]
    cutoff_score_sat: Optional[float]
    source_url: str
    retrieved_at: str
```

### 3.4 `UniversityConfig`

```python
@dataclass
class UniversityConfig:
    university_code: str
    university_name: str
    website: str
    crawler: str                # Tên crawler adapter
```

## 4. `subject_normalizer.py` - Chuẩn hóa môn học

### 4.1 `STANDARD_SUBJECTS` - Bảng ánh xạ

Dictionary mapping lowercase label → `(subject_code, canonical_name, component_type)`.

**Môn học chuẩn (SUBJECT):**

| Label (lowercase) | Code | Tên chuẩn | Component Type |
|--------------------|------|-----------|----------------|
| `toán` | `MATH` | Toán | SUBJECT |
| `ngữ văn` | `LITERATURE` | Văn | SUBJECT |
| `tiếng anh` | `ENGLISH` | Anh | SUBJECT |
| `vật lý` / `vật lí` | `PHYSICS` | Lý | SUBJECT |
| `hóa học` | `CHEMISTRY` | Hóa | SUBJECT |
| `sinh học` | `BIOLOGY` | Sinh | SUBJECT |
| `lịch sử` | `HISTORY` | Sử | SUBJECT |
| `địa lý` / `địa lí` | `GEOGRAPHY` | Địa | SUBJECT |
| `tin học` | `INFORMATICS` | Tin | SUBJECT |
| `công nghệ` | `TECHNOLOGY` | Công nghệ | SUBJECT |
| `giáo dục kt và pl` / `gdkt & pl` | `ECONOMIC_LAW` | Giáo dục KT và PL | SUBJECT |
| `tiếng trung` | `CHINESE` | Tiếng Trung | SUBJECT |

**Bài kiểm tra năng khiếu (APTITUDE):**

| Label | Code | Tên chuẩn |
|-------|------|-----------|
| `nk gdmn` | `APTITUDE_EARLY_CHILDHOOD` | NK GDMN |
| `nk tdtt` | `APTITUDE_SPORTS` | NK TDTT |
| `nk âm nhạc` | `APTITUDE_MUSIC` | Năng khiếu Âm nhạc |
| `nk mỹ thuật` | `APTITUDE_FINE_ARTS` | Năng khiếu Mỹ thuật |

### 4.2 `VALID_STANDARD_SUBJECT_CODES`

Set chứa 12 mã môn chuẩn hợp lệ: `MATH`, `LITERATURE`, `ENGLISH`, `PHYSICS`, `CHEMISTRY`, `BIOLOGY`, `HISTORY`, `GEOGRAPHY`, `INFORMATICS`, `TECHNOLOGY`, `ECONOMIC_LAW`, `CHINESE`

### 4.3 `normalize_subject_label()`

```python
def normalize_subject_label(
    raw_label: str,
    university_code: str = "",
    source_url: str = ""
) -> Tuple[Optional[SubjectItem], Optional[str]]
```

**Logic:**
1. Tìm trong `STANDARD_SUBJECTS` theo lowercase label
2. Nếu không tìm thấy, kiểm tra mã trực tiếp trong `VALID_STANDARD_SUBJECT_CODES`
3. Xử lý đặc biệt: `ECONOMIC_LAW_ED` → `ECONOMIC_LAW`
4. Xử lý `APTITUDE_*` prefix → component_type = `APTITUDE`
5. Nếu không khớp → trả về `(None, error_message)`

## 5. `combination_normalizer.py` - Chuẩn hóa tổ hợp môn

### 5.1 `generate_canonical_combination_id()`

```python
def generate_canonical_combination_id(subject_codes: List[str]) -> str
```

- Sắp xếp các mã môn theo alphabetical
- Nối bằng `_`
- VD: `['MATH', 'PHYSICS', 'CHEMISTRY']` → `CHEMISTRY_MATH_PHYSICS`

### 5.2 `format_combination_display()`

```python
def format_combination_display(subjects: List[SubjectItem]) -> str
```

-组件类型 `SUBJECT` hiển thị trước, `APTITUDE` hiển thị sau
- Đóng trong ngoặc tròn
- VD: `[SubjectItem(APTITUDE_EARLY_CHILDHOOD), SubjectItem(LITERATURE), SubjectItem(MATH)]` → `(Ngữ văn, Toán, NK GDMN)`

## 6. `validators.py` - Kiểm tra hợp lệ CSV

### 6.1 `validate_admissions_csv()`

```python
def validate_admissions_csv(
    filepath: str,
    db_subject_codes: Optional[Set[str]] = None
) -> Tuple[List[Dict], List[str], Dict[str, Any]]
```

**Kiểm tra:**

| # | Kiểm tra | Mô tả |
|---|----------|-------|
| 1 | Required fields | `university_code`, `admission_year`, `program_code`, `program_name`, `combination_id`, `combination_display`, `subjects_json` |
| 2 | Kiểu dữ liệu | `admission_year` (int), `quota` (int hoặc rỗng) |
| 3 | JSON parsing | `subjects_json` phải là JSON array hợp lệ |
| 4 | Component type | Nếu thiếu → tự suy luận từ mã: `APTITUDE_*` → APTITUDE, mã chuẩn → SUBJECT |
| 5 | Subject validation | Mã môn phải nằm trong `VALID_STANDARD_SUBJECT_CODES` |
| 6 | Aptitude validation | Mã phải bắt đầu bằng `APTITUDE_` |
| 7 | Duplicate key | `university_code + year + program_code + combination_id` phải duy nhất |
| 8 | ISO timestamp | `retrieved_at` phải là ISO 8601 hợp lệ |
| 9 | University match | `university_code` trong CSV phải khớp với CLI parameter |

**Trả về `(valid_records, errors, summary_stats)`:**

```python
summary_stats = {
    "valid_rows_count": int,
    "error_rows_count": int,
    "unique_canonical_ids_count": int,
    "total_subject_components": int,
    "total_aptitude_components": int,
    "unmapped_subject_labels": List[str],   # Các label không ánh xạ được
    "unclassified_components": List[str],   # Các component không phân loại được
}
```

### 6.2 `validate_cutoffs_csv()`

```python
def validate_cutoffs_csv(filepath: str) -> Tuple[List[Dict], List[str]]
```

**Kiểm tra:**
- Required fields: `university_code`, `admission_year`, `program_code`, `program_name`, `method_code`, `score_type`
- `admission_year` (int), `cutoff_score_thpt` (float), `cutoff_score_sat` (float)
- ISO timestamp validation
- Duplicate key: `university_code + year + program_code + method_code + score_type`

## 7. Lookup tự động component_type (Legacy Compatibility)

Cho các CSV cũ không có `component_type` trong `subjects_json`, validator sẽ tự suy luận:

```python
if not comp_type:
    if code.startswith("APTITUDE_"):
        comp_type = "APTITUDE"    # APTITUDE_* prefix → APTITUDE
    elif code in valid_subject_codes:
        comp_type = "SUBJECT"     # Mã chuẩn → SUBJECT
    else:
        # Lỗi: không phân loại được
```

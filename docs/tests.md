# Module `tests/` - Unit Test Suite

## 1. Mục đích

Bộ test unit cho toàn bộ pipeline data-scrawler: crawler adapters, normalizers, validators, importer, và graph mapping. Sử dụng framework `unittest` tiêu chuẩn Python.

## 2. Cách chạy test

```bash
# Chạy toàn bộ test suite
python -m unittest discover -s tests

# Chạy cụ thể 1 file test
python -m unittest tests.test_crawlers
python -m unittest tests.test_normalizers
python -m unittest tests.test_validators
python -m unittest tests.test_importer
python -m unittest tests.test_graph_mapping
```

## 3. Tổng quan coverage

| File test | Số test | Module kiểm tra |
|-----------|---------|-----------------|
| `test_crawlers.py` | 2 | `crawlers.base`, `crawlers.registry` |
| `test_normalizers.py` | 6 | `crawlers.common.subject_normalizer`, `crawlers.common.combination_normalizer` |
| `test_validators.py` | 2 | `crawlers.common.validators` |
| `test_importer.py` | 3 | `import_to_postgres` |
| `test_graph_mapping.py` | 6 | `graph_sync.models`, `graph_sync.neo4j_writer`, `graph_sync.validators` |
| **Tổng** | **19** | |

## 4. Chi tiết từng file test

### 4.1 `test_crawlers.py` - Crawler Registry Tests

| Test | Mô tả | Assert |
|------|-------|--------|
| `test_qnu_crawler_registration` | QNU crawler (DQN) được đăng ký đúng | `assertIn("DQN", list_registered_crawlers())`, `assertEqual(crawler.university_code, "DQN")` |
| `test_mock_adapter_registration` | Đăng ký adapter mock cho trường thứ 2 | Tạo `HUSTCrawler`, verify `assertIn("HUST", ...)`, `assertEqual(crawler.university_code, "HUST")`, `assertEqual(crawler.run()["offers_csv"], "hust_offers.csv")` |

**Không cần database hay network.**

### 4.2 `test_normalizers.py` - Subject & Combination Normalizer Tests

| Test | Mô tả | Assert |
|------|-------|--------|
| `test_canonical_combination_id_sorting` | 2 tổ hợp khác thứ tự tạo ra cùng canonical ID | `['MATH','PHYSICS','CHEMISTRY']` và `['MATH','CHEMISTRY','PHYSICS']` đều → `CHEMISTRY_MATH_PHYSICS` |
| `test_economic_law_mapping` | Label `gdkt & pl` và mã `ECONOMIC_LAW_ED` đều map về `ECONOMIC_LAW` | `assertEqual(item.subject_code, "ECONOMIC_LAW")` |
| `test_aptitude_identification` | Label `NK GDMN` và mã `APTITUDE_MUSIC` có component_type = `APTITUDE` | `assertEqual(item.component_type, "APTITUDE")` |
| `test_subject_identification` | Label `Toán` map về `MATH` với component_type = `SUBJECT` | `assertEqual(item.subject_code, "MATH")`, `assertEqual(item.component_type, "SUBJECT")` |
| `test_unknown_subject_validation_error` | Label không xác định trả về error | `assertIsNone(item)`, `assertIn("UNMAPPED SUBJECT", err)` |
| `test_combination_display_ordering` | SUBJECT hiển thị trước, APTITUDE sau | `[APTITUDE_EARLY_CHILDHOOD, LITERATURE, MATH]` → `(Ngữ văn, Toán, NK GDMN)` |

### 4.3 `test_validators.py` - CSV Validator Tests

| Test | Mô tả | Assert |
|------|-------|--------|
| `test_legacy_csv_without_component_type` | Legacy CSV không có `component_type` trong subjects_json được parse đúng | Component `MATH` → `component_type="SUBJECT"`, component `APTITUDE_EARLY_CHILDHOOD` → `component_type="APTITUDE"` |
| `test_unknown_component_validation_error` | Mã component không xác định tạo validation error | `assertIn("Unclassified component", errors[0])` |

**Sử dụng `tempfile.TemporaryDirectory()` để tạo CSV test.**

### 4.4 `test_importer.py` - Importer Safety Tests

| Test | Mô tả | Assert |
|------|-------|--------|
| `test_no_ddl_in_importer` | `import_to_postgres.py` **không chứa** DDL hoặc destructive statements | `assertNotIn("CREATE TABLE", ...)`, `assertNotIn("DROP TABLE", ...)`, `assertNotIn("TRUNCATE", ...)`, `assertNotIn("DELETE FROM", ...)`, `assertNotIn("ALTER TABLE", ...)` |
| `test_university_config_lookup` | Load config từ `universities.json` đúng cho DQN | `assertEqual(config["university_code"], "DQN")`, `assertEqual(config["crawler"], "qnu")` |
| `test_deleted_schema_sql_file` | `schema_postgres.sql` phải đã bị xóa khỏi workspace | `assertFalse(os.path.exists(schema_file))` |

**Đảm bảo importer an toàn: không tạo/xóa schema, không destructive operations.**

### 4.5 `test_graph_mapping.py` - Graph Mapping & Validation Tests

| Test | Mô tả | Assert |
|------|-------|--------|
| `test_uid_generation` | Kiểm tra UID creation functions | `make_subject_group_uid("TCV","2026","G01")` = `"TCV\|2026\|G01"`, `make_training_program_uid("DQN","7480201")` = `"DQN\|7480201"`, `make_cutoff_score_uid(...)` = `"DQN\|2025\|7480201\|100\|THPT"` |
| `test_technology_variant_mapping` | 4 TECH variants map về TECHNOLOGY | `get_canonical_subject_code("TECH_INDUSTRY")` = `"TECHNOLOGY"`, `get_canonical_subject_code("MATH")` = `None` |
| `test_subject_and_aptitude_separation_and_validation` | Graph data hợp lệ passes validation | `validate_graph_data(valid_data)` → `is_valid=True` |
| `test_detection_of_non_existent_references` | Phát hiện dangling references | Graph data với missing nodes → `is_valid=False`, error chứa `"MISSING_HS"` |
| `test_chunking_logic` | Batch partitioning đúng | `chunk_list(range(1050), 500)` → 3 chunks: [500, 500, 50] |
| `test_cypher_queries_use_merge_no_raw_create_or_detach_delete` | Audit Cypher安全 | Tất cả statements chứa `MERGE`, không chứa `CREATE` hay `DETACH DELETE` |

**Không cần database - test offline.**

## 5. Assertions an toàn quan trọng

### 5.1 DDL Absence Test

```python
# test_importer.py - Đảm bảo importer không destructive
forbidden_tokens = [
    "CREATE TABLE", "DROP TABLE", "TRUNCATE",
    "DELETE FROM", "ALTER TABLE", "INIT_SCHEMA",
    "SCHEMA_POSTGRES.SQL",
]
for token in forbidden_tokens:
    self.assertNotIn(token, code)
```

### 5.2 Cypher Safety Audit

```python
# test_graph_mapping.py - Đảm bảo Cypher dùng MERGE, không CREATE/DELETE
for cypher in all_statements:
    self.assertIn("MERGE", cypher_upper)
    self.assertNotIn("DETACH DELETE", cypher_upper)
    self.assertNotIn("CREATE ", cypher_upper)
```

## 6. Test data

### 6.1 Legacy CSV test data

```python
# test_validators.py - Legacy CSV không có component_type
legacy_subjects_json = '[{"subject_code": "MATH", "subject_name": "Toán", "weight": 1, "source_label": "Toán"}, {"subject_code": "APTITUDE_EARLY_CHILDHOOD", "subject_name": "NK GDMN", "weight": 1, "source_label": "NK GDMN"}]'
```

### 6.2 Valid graph data

```python
# test_graph_mapping.py - Graph data mẫu hợp lệ
valid_graph_data = {
    "nodes": {
        "Subject": [{"code": "MATH", "name": "Toán", ...}],
        "HighSchool": [{"code": "TCV", ...}],
        "SubjectGroup": [{"uid": "TCV|2026|G01", ...}],
        "University": [{"code": "DQN", ...}],
        "TrainingProgram": [{"uid": "DQN|7480201", ...}],
        "AdmissionCombination": [{"id": "A00", "component_count": 1, ...}],
        "AptitudeTest": [{"code": "NK01", ...}],
        "CutoffScore": [{"uid": "DQN|2025|7480201|100|THPT", ...}],
    },
    "relationships": {
        "OFFERS_GROUP": [{"high_school_code": "TCV", "subject_group_uid": "TCV|2026|G01"}],
        "INCLUDES_SUBJECT": [{"subject_group_uid": "TCV|2026|G01", "subject_code": "MATH"}],
        ...
    },
    "raw_components": [("A00", "MATH", "Toán", "SUBJECT", "MATH", 1.0)],
}
```

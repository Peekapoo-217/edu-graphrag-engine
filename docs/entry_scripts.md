# Script gốc - Entry Points CLI

## 1. Mục đích

Các script Python nằm trực tiếp trong thư mục `data-scrawler/`, cung cấp giao diện dòng lệnh (CLI) để thực thi các pipeline: crawl dữ liệu, import PostgreSQL, đồng bộ Neo4j.

## 2. Tổng quan luồng dữ liệu

```
┌─────────────────────┐
│  crawl_admissions.py │  ──→ CSV files + source_manifest.json
└─────────┬───────────┘
          │
          ▼
┌──────────────────────┐
│ import_to_postgres.py│  ──→ PostgreSQL (edu_guidance)
└─────────┬────────────┘
          │
          ▼
┌────────────────────────────┐
│ sync_postgres_to_neo4j.py  │  ──→ Neo4j (edugraph)
└────────────────────────────┘
```

## 3. `crawl_admissions.py` - Universal Crawler CLI

### 3.1 Mục đích

Entry point chính để crawl dữ liệu tuyển sinh từ bất kỳ trường đại học nào đã đăng ký trong `crawlers/registry`.

### 3.2 Cách sử dụng

```bash
python crawl_admissions.py --university DQN [--admission-year 2026] [--cutoff-year 2025] [--output-dir data]
```

| Tham số | Bắt buộc | Mặc định | Mô tả |
|---------|----------|----------|-------|
| `--university` | Có | - | Mã trường (VD: DQN, HUST) |
| `--admission-year` | Không | 2026 | Năm tuyển sinh |
| `--cutoff-year` | Không | 2025 | Năm điểm chuẩn (0 để bỏ qua) |
| `--output-dir` | Không | `data` | Thư mục output |

### 3.3 Flow thực thi

1. Parse CLI arguments
2. Tìm crawler đã đăng ký qua `CrawlerRegistry`
3. Gọi crawler.run() với các tham số
4. Output: CSV files + source_manifest.json

### 3.4 Output

```
data/{UNIV_CODE}/
├── raw/                          # HTML thô
│   ├── {univ}_admissions_{year}.html
│   └── {univ}_cutoff_{year}.html
├── {univ}_admission_offers_{year}.csv
├── {univ}_cutoff_scores_{year}.csv
└── source_manifest.json
```

---

## 4. `crawl_qnu_admissions.py` - Legacy QNU Wrapper

### 4.1 Mục đích

Wrapper tương thích ngược cho việc crawl dữ liệu DQN (Đại học Quy Nhơn). Delegates sang `QNUCrawler` qua registry.

### 4.2 Cách sử dụng

```bash
python crawl_qnu_admissions.py [--output-dir qnu_data]
```

| Tham số | Mặc định | Mô tả |
|---------|----------|-------|
| `--output-dir` | `qnu_data` | Thư mục output legacy |

### 4.3 Output

Output đến thư mục `qnu_data/` (legacy compatibility) thay vì `data/DQN/`.

---

## 5. `import_to_postgres.py` - Universal PostgreSQL Importer

### 5.1 Mục đích

Validate CSV và thực hiện upsert idempotent vào PostgreSQL (`edu_guidance` database).

### 5.2 Cách sử dụng

```bash
python import_to_postgres.py --university DQN --offers path/to/offers.csv [--cutoffs path/to/cutoffs.csv] [--dry-run]
```

| Tham số | Bắt buộc | Mô tả |
|---------|----------|-------|
| `--university` | Có | Mã trường (VD: DQN) |
| `--offers` | Có | Đường dẫn file CSV đề án tuyển sinh |
| `--cutoffs` | Không | Đường dẫn file CSV điểm chuẩn |
| `--dry-run` | Không | Chỉ validate, không ghi DB |

### 5.3 Flow thực thi

```
1. Load university config từ config/universities.json
2. Validate offers CSV (validate_admissions_csv)
3. Validate cutoffs CSV (nếu có) (validate_cutoffs_csv)
4. Verify university_code trong CSV khớp CLI parameter
5. Nếu --dry-run → in summary và thoát
6. Kết nối PostgreSQL
7. Verify DB schema qua information_schema
8. Kiểm tra subject FK tồn tại trong bảng subjects
9. Transaction upsert:
   a. upsert universities
   b. upsert admission_combinations
   c. upsert admission_combination_components
   d. upsert admission_offers
   e. upsert cutoff_scores (nếu có)
10. In summary
```

### 5.4 Database Schema yêu cầu

| Bảng | Cột bắt buộc |
|------|--------------|
| `universities` | `university_code`, `university_name`, `website` |
| `admission_offers` | `university_code`, `admission_year`, `program_code`, `program_name`, `method_codes`, `quota`, `combination_id`, `combination_display`, `canonical_combination_id`, `subjects_json`, `source_url`, `retrieved_at` |
| `cutoff_scores` | `university_code`, `admission_year`, `program_code`, `program_name`, `method_code`, `score_type`, `cutoff_score_thpt`, `cutoff_score_sat` |
| `subjects` | `code` |
| `admission_combinations` | `combination_id`, `combination_display`, `component_count` |
| `admission_combination_components` | `combination_id`, `component_code`, `component_name`, `component_type`, `subject_code`, `weight` |

### 5.5 Upsert Order

```
universities → admission_combinations → admission_combination_components → admission_offers → cutoff_scores
```

### 5.6 Dry-run Output

```
=== DRY-RUN VALIDATION SUMMARY ===
University Code:              DQN
Admission offers CSV:         53 valid rows, 0 errors.
Unique canonical combo IDs:   50
Total SUBJECT components:     143
Total APTITUDE components:    7
Unmapped subject labels:      []
Unclassified components:      []
```

---

## 6. `import_qnu_to_postgres.py` - Legacy QNU Importer Wrapper

### 6.1 Mục đích

Wrapper tương thích ngược cho import dữ liệu DQN vào PostgreSQL.

### 6.2 Cách sử dụng

```bash
python import_qnu_to_postgres.py [--admissions-csv path] [--cutoffs-csv path] [--dry-run]
```

### 6.3 Flow

- Resolve đường dẫn legacy từ `qnu_data/`
- Forward sang `import_to_postgres.main()`

---

## 7. `sync_postgres_to_neo4j.py` - PostgreSQL → Neo4j Synchronizer

### 7.1 Mục đích

Đọc dữ liệu từ PostgreSQL và đồng bộ sang Neo4j Knowledge Graph (`edugraph`) bằng Cypher MERGE idempotent.

### 7.2 Cách sử dụng

```bash
python sync_postgres_to_neo4j.py [--dry-run] [--verify] [--batch-size 500]
```

| Tham số | Mặc định | Mô tả |
|---------|----------|-------|
| `--dry-run` | - | Extract + validate, không ghi Neo4j |
| `--verify` | - | Chạy verification queries và in graph stats |
| `--batch-size` | 500 | Kích thước batch UNWIND MERGE |

### 7.3 Flow thực thi

```
[1/5] Kết nối PostgreSQL (Read-Only mode)
[2/5] Verify PostgreSQL schema qua information_schema
[3/5] Extract và normalize dữ liệu PostgreSQL
[4/5] Graph data validation & integrity checks
    ├── Duplicate UID detection (8 node types)
    ├── Referential integrity (8 relationship types)
    └── Component integrity (SUBJECT vs APTITUDE)
[5/5] (nếu không --dry-run) Sync to Neo4j:
    ├── Verify Neo4j constraints (8 unique constraints)
    ├── Sync Nodes (8 loại, theo thứ tự)
    ├── Sync Relationships (8 loại, theo thứ tự)
    └── Verify graph statistics + orphan detection
```

### 7.4 Environment Variables yêu cầu

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `DATABASE_URL` | - | PostgreSQL connection string |
| `NEO4J_URI` | `neo4j://127.0.0.1:7687` | Neo4j Bolt URI |
| `NEO4J_USERNAME` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | - | Neo4j password (bắt buộc) |
| `NEO4J_DATABASE` | `edugraph` | Tên database Neo4j |

### 7.5 Verification Report

```
================ NEO4J GRAPH VERIFICATION REPORT ================
Node Labels & Totals:
  Subject                :   20
  HighSchool             :    2
  SubjectGroup           :    8
  University             :    1
  TrainingProgram        :   53
  AdmissionCombination   :   50
  AptitudeTest           :    2
  CutoffScore            :   52

Relationship Types & Totals:
  OFFERS_GROUP           :    8
  INCLUDES_SUBJECT       :   32
  IS_VARIANT_OF          :    4
  OFFERS_PROGRAM         :   53
  ACCEPTS_COMBINATION    :  316
  REQUIRES_SUBJECT       :  143
  REQUIRES_APTITUDE      :    7
  HAS_CUTOFF             :   52

Orphan Node Diagnostics:
  [OK] SubjectGroup_without_HighSchool        :    0
  [OK] TrainingProgram_without_University     :    0
  [OK] AdmissionCombination_without_components:    0
  [OK] CutoffScore_without_TrainingProgram    :    0
  --> No orphan nodes detected in graph!
=================================================================
```

### 7.6 Reference Counts

Script chứa `REFERENCE_COUNTS` dictionary để so sánh với kết quả thực tế sau sync. Nếu khác biệt sẽ in WARNING.

---

## 8. Cấu hình chung

### 8.1 `.env` file

```bash
DATABASE_URL="postgresql://user:password@localhost:5432/edu_guidance"
NEO4J_URI="neo4j://127.0.0.1:7687"
NEO4J_USERNAME="neo4j"
NEO4J_PASSWORD="your_password"
NEO4J_DATABASE="edugraph"
```

### 8.2 `config/universities.json`

```json
{
  "DQN": {
    "university_code": "DQN",
    "university_name": "Trường Đại học Quy Nhơn",
    "website": "https://qnu.edu.vn",
    "crawler": "qnu"
  }
}
```

## 9. Chạy thử (Recommended)

Luôn chạy `--dry-run` trước khi thực hiện ghi database:

```bash
# Step 1: Crawl dữ liệu
python crawl_admissions.py --university DQN

# Step 2: Validate trước khi import
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv --dry-run

# Step 3: Import thực tế
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv

# Step 4: Validate trước khi sync
python sync_postgres_to_neo4j.py --dry-run

# Step 5: Sync thực tế
python sync_postgres_to_neo4j.py

# Step 6: Verify
python sync_postgres_to_neo4j.py --verify
```

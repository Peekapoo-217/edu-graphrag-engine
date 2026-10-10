# BÁO CÁO TOÀN DIỆN KIẾN TRÚC VÀ NGHIỆP VỤ HỆ THỐNG DATA-SCRAWLER

> **Hệ thống:** Thu thập (Crawler), Chuẩn hóa (Normalizer), Nạp Dữ liệu Relational (PostgreSQL Importer) và Đồng bộ Đồ thị Tri thức (Neo4j Graph Sync Pipeline)  
> **Cơ sở dữ liệu Relational (Source of Truth):** PostgreSQL (`edu_guidance`)  
> **Cơ sở dữ liệu Graph Projection (Knowledge Graph):** Neo4j (`edugraph`)  
> **Kiến trúc chính:** Adapter Architecture, Read-Only Extraction, Idempotent Transaction Upserts, Parameterized Batch UNWIND MERGE  
> **Ngày cập nhật:** 23/08/2026  

---

## 1. TỔNG QUAN HỆ THỐNG & MỤC TIÊU KIẾN TRÚC

Dự án **`data-scrawler`** là phân hệ xử lý dữ liệu trung tâm trong hệ thống tư vấn đường lối học tập tuyển sinh. Phân hệ được thiết kế và tái cấu trúc hoàn chỉnh theo đường lối **Multi-University ETL Pipeline** phục vụ 3 mục tiêu chiến lược:

1. **Thu thập Dữ liệu Tuyển sinh Đa Trường (Multi-University Scraper):** Áp dụng *Adapter Pattern* cho phép dễ dàng mở rộng thu thập dữ liệu tuyển sinh từ nhiều trường đại học mà không ảnh hưởng tới lõi xử lý dữ liệu.
2. **Lưu trữ CSDL Quan hệ Chuẩn hóa (PostgreSQL - Structured Source of Truth):** Quản lý toàn vẹn dữ liệu tuyển sinh (chỉ tiêu, phương thức, tổ hợp môn, điểm chuẩn) bằng giao dịch an toàn (Transaction Upsert) và kiểm soát schema qua `information_schema`.
3. **Đồng bộ Đồ thị Tri thức (Neo4j Graph Projection):** Chiếu dữ liệu từ PostgreSQL sang Neo4j Knowledge Graph phục vụ các truy vấn duyệt đồ thị (Graph Traversal) và thuật toán gợi ý đường lối học tập.

```mermaid
flowchart LR
    subgraph Phase1["1. Data Scraping & Normalization"]
        Web["University Websites / HTML"] --> Adapter["Crawler Adapter (e.g. QNU)"]
        Adapter --> Normalizer["Subject & Combination Normalizers"]
        Normalizer --> CSV["Raw & Normalized CSV / Manifest"]
    end

    subgraph Phase2["2. Relational Persistence (Source of Truth)"]
        CSV --> DryRunPG["--dry-run Validation"]
        DryRunPG --> SchemaCheck["information_schema Inspection"]
        SchemaCheck --> PGUpsert["PostgreSQL Transaction (edu_guidance)"]
    end

    subgraph Phase3["3. Graph Projection Sync"]
        PGUpsert --> PGRead["Read-Only PG Extraction"]
        PGRead --> GraphValidate["Graph Data Integrity Validator"]
        GraphValidate --> Neo4jWriter["Neo4j Writer (UNWIND MERGE)"]
        Neo4jWriter --> Neo4jDB[("Neo4j Knowledge Graph (edugraph)")]
    end
```

---

## 2. CẤU TRÚC MÃ NGUỒN VÀ MÔ-ĐỦN DỰ ÁN

```
c:\workspace\thesis-project\data-scrawler\
├── config/
│   └── universities.json               # Cấu hình thông tin các trường đại học (Code, Name, Website, Crawler)
├── crawlers/
│   ├── __init__.py
│   ├── base.py                         # Interface BaseCrawler (HTTP fetch, retry, CSV writer)
│   ├── registry.py                     # CrawlerRegistry (Đăng ký & tra cứu adapter theo mã trường)
│   ├── qnu.py                          # Adapter bóc tách dữ liệu ĐH Quy Nhơn (DQN)
│   └── common/
│       ├── __init__.py
│       ├── subject_normalizer.py       # Chuẩn hóa mã môn & phân loại môn văn hóa (SUBJECT) / năng khiếu (APTITUDE)
│       ├── combination_normalizer.py   # Tạo canonical_combination_id & sắp xếp hiển thị combination_display
│       ├── validators.py               # Kiểm tra tính hợp lệ dữ liệu CSV tuyển sinh & điểm chuẩn
│       └── models.py                   # Dataclass đại diện cho bản ghi tuyển sinh, điểm chuẩn, môn học
├── graph_sync/
│   ├── __init__.py
│   ├── models.py                       # Sinh UID duy nhất (SubjectGroup, TrainingProgram, CutoffScore) & Canonical mapping
│   ├── postgres_reader.py              # Đọc dữ liệu từ PostgreSQL ở chế độ Read-Only & kiểm tra schema
│   ├── neo4j_writer.py                 # Thực thi Cypher UNWIND MERGE theo Batch 500 & audit constraint
│   ├── validators.py               # Kiểm tra tham chiếu ngoại & tính hợp lệ của dữ liệu trước khi nạp Neo4j
│   └── check_neo4j_connection.py       # Công cụ chẩn đoán kết nối và kiểm tra constraint Neo4j
├── data/
│   └── DQN/                            # Thư mục lưu dữ liệu thu thập theo từng mã trường
│       ├── raw/                        # Lưu các trang HTML thô đã cào
│       ├── source_manifest.json        # Nguồn gốc dữ liệu & timestamp thu thập
│       ├── dqn_admission_offers_2026.csv
│       └── dqn_cutoff_scores_2025.csv
├── tests/
│   ├── test_crawlers.py               # Test kiến trúc Adapter & Mock Crawler
│   ├── test_importer.py               # Test Importer (No DDL, dry-run, schema check)
│   ├── test_normalizers.py             # Test sorting canonical ID, subject map, aptitude separation
│   ├── test_validators.py              # Test CSV validation rules & error handling
│   └── test_graph_mapping.py           # Test UID generation, graph validation, chunking, Cypher safety
├── crawl_admissions.py                  # CLI Crawler chính cho đa trường
├── import_to_postgres.py                # CLI PostgreSQL Importer chính
├── sync_postgres_to_neo4j.py            # CLI Đồng bộ PostgreSQL -> Neo4j
├── crawl_qnu_admissions.py              # Script wrapper tương thích ngược cho QNU
├── import_qnu_to_postgres.py            # Script wrapper tương thích ngược cho QNU
├── README.md                            # Hướng dẫn vận hành dự án
├── README_IMPORT.md                     # Hướng dẫn chi tiết nạp dữ liệu PostgreSQL
└── BAO_CAO_DU_AN.md                     # Báo cáo toàn diện kiến trúc hệ thống (File này)
```

---

## 3. PHÂN HỆ 1: CRAWLER & CHUẨN HÓA DỮ LIỆU (CRAWLER ADAPTER SUBSYSTEM)

### 3.1. Thiết kế Adapter Architecture
Phân hệ thu thập áp dụng **Adapter Pattern** giúp tách biệt hoàn toàn logic bóc tách HTML của từng trường đại học với bộ xử lý dữ liệu chung.

- **`BaseCrawler` ([`crawlers/base.py`](file:///c:/workspace/thesis-project/data-scrawler/crawlers/base.py)):** Lớp cơ sở trừu tượng cung cấp tính năng fetch URL kèm cơ chế tự động thử lại (Retry logic 3 lần, delay lũy thừa), đặt `User-Agent` tiêu chuẩn, ghi file CSV mã hóa UTF-8 BOM (`utf-8-sig`) và quản lý cấu trúc thư mục đầu ra `data/<UNIVERSITY_CODE>/raw/`.
- **`CrawlerRegistry` ([`crawlers/registry.py`](file:///c:/workspace/thesis-project/data-scrawler/crawlers/registry.py)):** Bộ đăng ký động sử dụng decorator `@register_crawler("CODE")`. CLI có thể khởi tạo adapter tương ứng thông qua mã trường truyền vào từ câu lệnh.
- **`QNUCrawler` ([`crawlers/qnu.py`](file:///c:/workspace/thesis-project/data-scrawler/crawlers/qnu.py)):** Adapter triển khai bóc tách dữ liệu thông tin tuyển sinh 2026 và điểm chuẩn 2025 của Trường Đại học Quy Nhơn (`DQN`).

### 3.2. Chuẩn hóa Mã môn & Phân tách Môn Văn Hóa vs Năng Khiếu
- **Môn văn hóa chuẩn (`SUBJECT`):** Định danh theo bộ mã chuẩn (`MATH`, `LITERATURE`, `ENGLISH`, `PHYSICS`, `CHEMISTRY`, `BIOLOGY`, `HISTORY`, `GEOGRAPHY`, `INFORMATICS`, `TECHNOLOGY`, `ECONOMIC_LAW`, `CHINESE`).
- **Mã ngành Luật Kinh tế:** Được chuẩn hóa duy nhất thành `ECONOMIC_LAW` (thay thế mã cũ `ECONOMIC_LAW_ED`).
- **Phân tách Môn Năng Khiếu (`APTITUDE`):** Tuyệt đối KHÔNG đưa môn năng khiếu vào bảng danh mục môn văn hóa `subjects`. Các môn năng khiếu được gắn `component_type = "APTITUDE"` và gắn mã dạng `APTITUDE_EARLY_CHILDHOOD`, `APTITUDE_SPORTS`...
- **`canonical_combination_id`:** Tạo mã tổ hợp chuẩn bất biến bằng cách sắp xếp danh sách mã môn theo thứ tự bảng chữ cái (Ví dụ: Tổ hợp gồm Toán, Lý, Hóa luôn tạo ra ID chuẩn là `CHEMISTRY_MATH_PHYSICS`).
- **`combination_display`:** Sắp xếp tên hiển thị ưu tiên các môn văn hóa (`SUBJECT`) lên trước, các môn năng khiếu (`APTITUDE`) xuống sau.

---

## 4. PHÂN HỆ 2: NẠP DỮ LIỆU LƯU TRƯ CHUẨN HOÁ (POSTGRESQL IMPORTER SUBSYSTEM)

### 4.1. Nguyên tắc An toàn & Kiểm soát Schema
> [!IMPORTANT]
> - **Tuyệt đối KHÔNG chứa câu lệnh DDL:** Source code không bao giờ chứa `CREATE TABLE`, `DROP TABLE`, `DELETE`, `TRUNCATE`, `ALTER TABLE` hay khởi tạo schema. Schema CSDL PostgreSQL được quản lý thủ công bên ngoài.
> - **Kiểm tra Schema tự động qua `information_schema`:** Trước khi thực hiện bất kỳ giao dịch ghi nào, hàm [`verify_db_schema()`](file:///c:/workspace/thesis-project/data-scrawler/import_to_postgres.py#L42-L91) tự động kiểm tra sự tồn tại của 6 bảng CSDL và toàn bộ các cột bắt buộc. Nếu thiếu, Importer sẽ báo lỗi chi tiết và hủy thao tác.
> - **Kiểm tra sự tồn tại của Môn văn hóa:** Hàm [`fetch_existing_db_subjects()`](file:///c:/workspace/thesis-project/data-scrawler/import_to_postgres.py#L94-L99) đối chiếu toàn bộ các môn `SUBJECT` trong file dữ liệu với bảng `subjects` trong CSDL. Nếu phát hiện môn chưa có trong DB, hệ thống dừng lại yêu cầu bổ sung dữ liệu danh mục trước.

### 4.2. Thứ tự Giao dịch Upsert An toàn (Transaction Order)
Importer thực hiện toàn bộ thao tác nạp dữ liệu trong **duy nhất 1 PostgreSQL Transaction** ([`import_to_postgres.py: L147-L297`](file:///c:/workspace/thesis-project/data-scrawler/import_to_postgres.py#L147-L297)) theo đúng thứ tự ràng buộc khóa ngoại:

```mermaid
flowchart TD
    Config["config/universities.json"] --> ValidateConfig["Load University Config"]
    ValidateConfig --> SchemaCheck["verify_db_schema() via information_schema"]
    SchemaCheck --> CheckSubjects["Check SUBJECT codes against DB subjects table"]
    CheckSubjects --> TxStart["Begin Transaction: with conn.transaction()"]
    TxStart --> Step1["1. Upsert Table: universities (ON CONFLICT DO UPDATE)"]
    Step1 --> Step2["2. Upsert Table: admission_combinations (ON CONFLICT DO UPDATE)"]
    Step2 --> Step3["3. Upsert Table: admission_combination_components (ON CONFLICT DO UPDATE)"]
    Step3 --> Step4["4. Upsert Table: admission_offers (ON CONFLICT DO UPDATE with Jsonb)"]
    Step4 --> Step5["5. Upsert Table: cutoff_scores (ON CONFLICT DO UPDATE)"]
    Step5 --> TxCommit["Commit Transaction"]
```

---

## 5. PHÂN HỆ 3: ĐỒNG BỘ ĐỒ THỊ TRI THỨC (NEO4J GRAPH SYNC SUBSYSTEM)

Phân hệ [`sync_postgres_to_neo4j.py`](file:///c:/workspace/thesis-project/data-scrawler/sync_postgres_to_neo4j.py) đóng vai trò chuyển đổi dữ liệu quan hệ từ PostgreSQL thành mô hình đồ thị tri thức (Knowledge Graph Projection) trên Neo4j (`edugraph`).

### 5.1. Nguyên tắc Đọc An toàn từ PostgreSQL
Khi trích xuất dữ liệu từ PostgreSQL, hệ thống kết nối với cờ `conn.read_only = True` ([`postgres_reader.py: L36-40`](file:///c:/workspace/thesis-project/data-scrawler/graph_sync/postgres_reader.py#L36-L40)), bảo đảm quá trình đồng bộ sang Neo4j tuyệt đối không gây biến đổi hoặc ảnh hưởng tới dữ liệu PostgreSQL gốc.

### 5.2. Mô hình Đồ thị Tri thức Neo4j Schema
Đồ thị tri thức được thiết kế gồm **8 loại Node Labels** và **8 loại Relationships**:

```mermaid
erDiagram
    HighSchool ||--o{ SubjectGroup : OFFERS_GROUP
    SubjectGroup ||--o{ Subject : INCLUDES_SUBJECT
    Subject ||--o| Subject : IS_VARIANT_OF
    University ||--o{ TrainingProgram : OFFERS_PROGRAM
    TrainingProgram ||--o{ AdmissionCombination : ACCEPTS_COMBINATION
    AdmissionCombination ||--o{ Subject : REQUIRES_SUBJECT
    AdmissionCombination ||--o{ AptitudeTest : REQUIRES_APTITUDE
    TrainingProgram ||--o{ CutoffScore : HAS_CUTOFF
```

#### Chi tiết 8 Node Labels:
1. `Subject`: Nút môn học (`code`, `name`, `subject_type`, `is_mandatory`, `canonical_subject_code`).
2. `HighSchool`: Nút trường THPT (`code`, `name`, `location`).
3. `SubjectGroup`: Nút tổ hợp môn học sinh THPT chọn (`uid`: `SchoolCode|Year|GroupCode`).
4. `University`: Nút trường đại học (`code`, `name`, `website`).
5. `TrainingProgram`: Nút ngành/chương trình đào tạo (`uid`: `UnivCode|ProgramCode`).
6. `AdmissionCombination`: Nút tổ hợp môn xét tuyển (`id`: `canonical_combination_id`).
7. `AptitudeTest`: Nút môn thi năng khiếu (`code`, `name`).
8. `CutoffScore`: Nút điểm chuẩn xét tuyển (`uid`: `UnivCode|Year|ProgramCode|MethodCode|ScoreType`).

#### Chi tiết 8 Relationship Types:
1. `(:HighSchool)-[:OFFERS_GROUP]->(:SubjectGroup)`
2. `(:SubjectGroup)-[:INCLUDES_SUBJECT]->(:Subject)`
3. `(:Subject)-[:IS_VARIANT_OF]->(:Subject)` (Ánh xạ môn biến thể về môn chuẩn, ví dụ: môn Công nghệ ngành về môn Công nghệ chuẩn)
4. `(:University)-[:OFFERS_PROGRAM]->(:TrainingProgram)`
5. `(:TrainingProgram)-[:ACCEPTS_COMBINATION {admission_year, method_codes, quota, source_url}]->(:AdmissionCombination)`
6. `(:AdmissionCombination)-[:REQUIRES_SUBJECT {weight}]->(:Subject)`
7. `(:AdmissionCombination)-[:REQUIRES_APTITUDE {weight}]->(:AptitudeTest)`
8. `(:TrainingProgram)-[:HAS_CUTOFF]->(:CutoffScore)`

### 5.3. Cơ chế Bất Biến & Nạp Batch Tốc độ cao (Parameterized UNWIND MERGE)
- **Không dùng `CREATE` hay `DETACH DELETE`:** Toàn bộ câu lệnh Cypher đều sử dụng cú pháp parameterized `UNWIND $rows AS row MERGE (...) SET ...` ([`neo4j_writer.py: L19-L134`](file:///c:/workspace/thesis-project/data-scrawler/graph_sync/neo4j_writer.py#L19-L134)). Điều này giúp việc đồng bộ có tính chất bất biến (Idempotent), chạy nhiều lần không gây trùng lặp dữ liệu.
- **Nạp theo Batch (500 bản ghi/lượt):** Dữ liệu được chia nhỏ qua hàm `chunk_list` giúp tối ưu bộ nhớ và tốc độ thực thi trên Neo4j.
- **Thứ tự Nạp Tuần tự:** Đảm bảo nạp toàn bộ 8 loại Nodes trước, sau đó mới nạp 8 loại Relationships để tránh lỗi rỗng tham chiếu.

### 5.4. Kiểm soát Constraint & Chẩn đoán Node Mồ Côi (Orphan Diagnostics)
- **Audit Constraint trước khi Sync:** Hàm [`verify_neo4j_constraints()`](file:///c:/workspace/thesis-project/data-scrawler/graph_sync/neo4j_writer.py#L142-L165) kiểm tra 8 Unique Constraints trên Neo4j (`SHOW CONSTRAINTS`). Nếu thiếu constraint, tiến trình dừng lại để bảo vệ toàn vẹn dữ liệu.
- **Kiểm tra Thống kê & Node Mồ côi sau Sync:** Hàm [`verify_graph_statistics()`](file:///c:/workspace/thesis-project/data-scrawler/graph_sync/neo4j_writer.py#L247-L287) thực thi các câu truy vấn thống kê tổng số Node, Relationship và phát hiện các trường hợp Node bị mồ côi (Ví dụ: `SubjectGroup` không gắn liền với `HighSchool`, `CutoffScore` không kết nối tới `TrainingProgram`).

---

## 6. BỘ KIỂM THỬ TỰ ĐỘNG (AUTOMATED TEST SUITE)

Hệ thống tích hợp bộ unit test toàn diện có thể chạy hoàn toàn **Offline (không cần kết nối CSDL thật)** qua câu lệnh `python -m unittest discover -s tests`:

```
tests/
├── test_crawlers.py         # Kiểm thử BaseCrawler, CrawlerRegistry, QNUCrawler adapter & Mock adapter
├── test_importer.py         # Kiểm thử Importer: Không chứa DDL, kiểm tra schema, dry-run mode, config verification
├── test_normalizers.py       # Kiểm thử chuẩn hóa môn học, Luật Kinh tế, sắp xếp canonical ID, phân loại Aptitude
├── test_validators.py        # Kiểm thử các quy tắc validation file CSV tuyển sinh & điểm chuẩn
└── test_graph_mapping.py     # Kiểm thử sinh UID đồ thị, ánh xạ môn biến thể, chunking batch, kiểm tra an toàn Cypher (MERGE)
```

> **Kết quả kiểm thử:** 100% các bài test (Unit Tests) đều vượt qua (`PASSED`), bảo đảm hệ thống vận hành ổn định và an toàn.

---

## 7. QUY TRÌNH VẬN HÀNH HỆ THỐNG END-TO-END

### BƯỚC 1: Cài đặt Phụ thuộc & Môi trường
```powershell
# Cài đặt các gói phụ thuộc Python
pip install -r requirements.txt

# Khai báo biến môi trường PostgreSQL & Neo4j
$env:DATABASE_URL="postgresql://postgres:MAT_KHAU@127.0.0.1:5432/edu_guidance"
$env:NEO4J_URI="neo4j://127.0.0.1:7687"
$env:NEO4J_USERNAME="neo4j"
$env:NEO4J_PASSWORD="MAT_KHAU_NEO4J"
$env:NEO4J_DATABASE="edugraph"
```

### BƯỚC 2: Thu thập Dữ liệu (Scraping)
```powershell
# Thu thập dữ liệu trường ĐH Quy Nhơn (DQN)
python crawl_admissions.py --university DQN --admission-year 2026 --cutoff-year 2025 --output-dir data
```

### BƯỚC 3: Kiểm tra & Nạp vào PostgreSQL (Relational Source of Truth)
```powershell
# 1. Kiểm tra dữ liệu mà không ghi DB (--dry-run)
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv --dry-run

# 2. Thực hiện Nạp dữ liệu vào PostgreSQL (Upsert Transaction)
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv
```

### BƯỚC 4: Đồng bộ dữ liệu PostgreSQL sang Neo4j (Knowledge Graph)
```powershell
# 1. Chẩn đoán kết nối và kiểm tra constraint Neo4j
python graph_sync/check_neo4j_connection.py

# 2. Kiểm tra dữ liệu trích xuất & validate trước khi đồng bộ (--dry-run)
python sync_postgres_to_neo4j.py --dry-run

# 3. Thực hiện đồng bộ chính thức vào Neo4j
python sync_postgres_to_neo4j.py

# 4. Kiểm tra báo cáo thống kê đồ thị và node mồ côi sau đồng bộ (--verify)
python sync_postgres_to_neo4j.py --verify
```

---

## 8. HƯỚNG DẪN MỞ RỘNG TRƯỜNG ĐẠI HỌC MỚI (MULTI-TENANT EXTENSION)

Để bổ sung một trường đại học mới (Ví dụ mã trường: `HUST` - Đại học Bách khoa Hà Nội):

1. **Khai báo cấu hình trường** vào file [`config/universities.json`](file:///c:/workspace/thesis-project/data-scrawler/config/universities.json):
   ```json
   {
     "HUST": {
       "university_code": "HUST",
       "university_name": "Đại học Bách khoa Hà Nội",
       "website": "https://hust.edu.vn",
       "crawler": "hust"
     }
   }
   ```

2. **Tạo Crawler Adapter mới** tại `crawlers/hust.py`:
   ```python
   from crawlers.base import BaseCrawler
   from crawlers.registry import register_crawler

   @register_crawler("HUST")
   class HUSTCrawler(BaseCrawler):
       def run(self, admission_year=2026, cutoff_year=2025):
           # Thực hiện bóc tách HTML tuyển sinh HUST
           # Lưu kết quả xuất ra CSV tại data/HUST/
           return {"offers_csv": "data/HUST/hust_admission_offers_2026.csv"}
   ```

3. **Chạy Pipeline thu thập & nạp dữ liệu** theo các câu lệnh CLI tiêu chuẩn. Bộ Importer và Neo4j Sync sẽ tự động xử lý dữ liệu của trường mới mà không cần viết lại bất kỳ dòng code ghi CSDL nào.

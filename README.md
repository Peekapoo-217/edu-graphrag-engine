# Multi-University Admissions Data Crawler & Importer Pipeline

Hệ thống thu thập (Scraper), kiểm soát chất lượng (Validator) và nạp dữ liệu tuyển sinh (PostgreSQL Importer) mở rộng hỗ trợ nhiều trường đại học (`edu_guidance`).

---

## 1. Kiến trúc Hệ thống (Adapter Pattern)

Dự án áp dụng thiết kế **Adapter Architecture** giúp dễ dàng thêm mới trường đại học bằng cách tạo Crawler Adapter riêng mà không cần sửa đổi bộ chuẩn hóa, validation hay importer CSDL:

```
crawlers/
├── base.py                         # Interface BaseCrawler
├── registry.py                     # CrawlerRegistry (Quản lý adapter theo mã trường)
├── qnu.py                          # Adapter crawl Trường ĐH Quy Nhơn (DQN)
└── common/
    ├── subject_normalizer.py       # Chuẩn hóa mã môn (ECONOMIC_LAW, APTITUDE...)
    ├── combination_normalizer.py   # Xử lý canonical_combination_id & thứ tự hiển thị
    ├── validators.py               # Kiểm tra hợp lệ CSV & JSON
    └── models.py                   # Cấu trúc dữ liệu chuẩn Dataclass
```

---

## 2. Quy Định Tuyệt Đối Về Cơ Sở Dữ Liệu (PostgreSQL Constraints)

> [!IMPORTANT]
> - Database và cấu trúc bảng CSDL (`edu_guidance`) được quản lý thủ công bên ngoài source code.
> - **Source code KHÔNG thực thi DDL:** Tuyệt đối không chứa `CREATE TABLE`, `DROP TABLE`, `DELETE`, `TRUNCATE`, `ALTER TABLE`, hoặc tự động thay đổi schema.
> - Importer chỉ sử dụng các câu lệnh `SELECT`, `INSERT`, `UPDATE` (Upsert).
> - Trước khi nạp dữ liệu, Importer tự động kiểm tra sự tồn tại của các bảng và cột bắt buộc bằng `information_schema`. Nếu thiếu, chương trình dừng và thông báo lỗi rõ ràng.

### Danh mục các bảng bắt buộc kiểm tra:
- `universities`
- `admission_offers`
- `cutoff_scores`
- `subjects`
- `admission_combinations`
- `admission_combination_components`

---

## 3. Hướng Dẫn Vận Hành

### BƯỚC 1: Cài đặt phụ thuộc & Cấu hình môi trường

Cài đặt thư viện Python (3.10+):
```powershell
pip install -r requirements.txt
```

Khai báo chuỗi kết nối PostgreSQL (Ví dụ trên PowerShell):
```powershell
$env:DATABASE_URL="postgresql://postgres:MAT_KHAU@localhost:5432/edu_guidance"
```

---

### BƯỚC 2: Chạy Crawler thu thập dữ liệu

Sử dụng CLI crawler chung:
```powershell
python crawl_admissions.py --university DQN --admission-year 2026 --cutoff-year 2025 --output-dir data
```

Dữ liệu đầu ra được tự động lưu theo cấu trúc:
```
data/
└── DQN/
    ├── raw/
    │   ├── qnu_admissions_2026.html
    │   └── qnu_cutoff_2025.html
    ├── source_manifest.json
    ├── dqn_admission_offers_2026.csv
    └── dqn_cutoff_scores_2025.csv
```

*(Lưu ý: Script tương thích ngược `python crawl_qnu_admissions.py` vẫn hoạt động bình thường).*

---

### BƯỚC 3: Kiểm tra hợp lệ dữ liệu (Chế độ `--dry-run`)

Kiểm tra lỗi dữ liệu mà không cần mở giao dịch ghi vào CSDL:
```powershell
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv --dry-run
```

*(Lưu ý: Script tương thích ngược `python import_qnu_to_postgres.py --dry-run` vẫn hoạt động bình thường).*

---

### BƯỚC 4: Nạp dữ liệu vào PostgreSQL (Upsert)

Chạy Nạp dữ liệu thực tế:
```powershell
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv
```

Thứ tự nạp dữ liệu an toàn theo khóa ngoại:
1. `universities` (Upsert metadata trường)
2. `admission_combinations` (Upsert tổ hợp chuẩn `canonical_combination_id`)
3. `admission_combination_components` (Upsert thành phần môn học `SUBJECT` & năng khiếu `APTITUDE`)
4. `admission_offers` (Upsert chỉ tiêu tuyển sinh kèm `canonical_combination_id`)
5. `cutoff_scores` (Upsert điểm chuẩn các năm)

---

## 4. Hướng Dẫn Thêm Trường Đại Học Mới

1. Khai báo thông tin trường vào file [`config/universities.json`](file:///c:/workspace/thesis-project/data-scrawler/config/universities.json):
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
2. Tạo crawler adapter mới trong thư mục `crawlers/` (Ví dụ: `crawlers/hust.py`):
   ```python
   from crawlers.base import BaseCrawler
   from crawlers.registry import register_crawler

   @register_crawler("HUST")
   class HUSTCrawler(BaseCrawler):
       def run(self, admission_year=2026, cutoff_year=2025):
           # Viết logic bóc tách dữ liệu HUST tại đây
           pass
   ```
3. Chạy lệnh crawl và import bình thường:
   ```powershell
   python crawl_admissions.py --university HUST
   python import_to_postgres.py --university HUST --offers data/HUST/hust_admission_offers_2026.csv
   ```

---

## 5. Quy Chuẩn Môn Học & Năng Khiếu

- Mã môn văn hóa chuẩn: `MATH`, `LITERATURE`, `ENGLISH`, `PHYSICS`, `CHEMISTRY`, `BIOLOGY`, `HISTORY`, `GEOGRAPHY`, `INFORMATICS`, `TECHNOLOGY`, `ECONOMIC_LAW`, `CHINESE`.
- Mã ngành Luật Kinh tế chuẩn là `ECONOMIC_LAW` (Không dùng `ECONOMIC_LAW_ED`).
- Năng khiếu: Dùng `component_type = "APTITUDE"`, mã có dạng `APTITUDE_*` (ví dụ `APTITUDE_EARLY_CHILDHOOD`, `APTITUDE_SPORTS`). Năng khiếu KHÔNG lưu vào bảng `subjects`.
- `canonical_combination_id`: Tạo bằng cách sắp xếp danh sách mã môn chuẩn theo thứ tự alphabet (Ví dụ: `CHEMISTRY_MATH_PHYSICS`).
- `combination_display`: Đưa các môn `SUBJECT` lên trước, `APTITUDE` xuống sau (Ví dụ: `(Ngữ văn, Toán, Năng khiếu Mầm non)`).

---

## 6. Đồng Bộ Dữ Liệu PostgreSQL Sang Neo4j (Knowledge Graph POC)

Module [`sync_postgres_to_neo4j.py`](file:///c:/workspace/thesis-project/data-scrawler/sync_postgres_to_neo4j.py) có nhiệm vụ đọc dữ liệu từ PostgreSQL (`edu_guidance`) và tạo/cập nhật Knowledge Graph trên Neo4j (`edugraph`) một cách bất biến (Idempotent) bằng Cypher `MERGE`.

### Cấu hình biến môi trường kết nối:

```powershell
$env:DATABASE_URL="postgresql://postgres:MAT_KHAU@127.0.0.1:5432/edu_guidance"
$env:NEO4J_URI="neo4j://127.0.0.1:7687"
$env:NEO4J_USERNAME="neo4j"
$env:NEO4J_PASSWORD="YOUR_NEO4J_PASSWORD"
$env:NEO4J_DATABASE="edugraph"
```

### Các lệnh vận hành:

1. **Chạy kiểm tra tính hợp lệ dữ liệu và đếm dự kiến (`--dry-run`):**
   ```powershell
   python sync_postgres_to_neo4j.py --dry-run
   ```

2. **Thực hiện đồng bộ dữ liệu sang Neo4j:**
   ```powershell
   python sync_postgres_to_neo4j.py
   ```

3. **Kiểm tra thống kê node, quan hệ và phát hiện node mồ côi (`--verify`):**
   ```powershell
   python sync_postgres_to_neo4j.py --verify
   ```

4. **Chạy bộ Unit Test (Offline, không cần kết nối CSDL):**
   ```powershell
   python -m unittest tests/test_graph_mapping.py
   ```

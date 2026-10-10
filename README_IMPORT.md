# QNU Admissions Data Importer & Pipeline Wrapper

Hướng dẫn sử dụng công cụ import dữ liệu tuyển sinh Trường Đại học Quy Nhơn (QNU - `DQN`) và các trường Đại học vào CSDL PostgreSQL (`edu_guidance`).

---

## 1. Yêu cầu hệ thống

- **Python 3.10+**
- **PostgreSQL 14+** (Cơ sở dữ liệu `edu_guidance` được quản lý thủ công bên ngoài)
- Thư viện Python: `psycopg` v3 (`psycopg[binary]`), `lxml`

Cài đặt phụ thuộc:
```powershell
pip install -r requirements.txt
```

---

## 2. Hướng dẫn sử dụng

### BƯỚC 1: Thiết lập biến môi trường `DATABASE_URL`

Trên **Windows PowerShell**:
```powershell
$env:DATABASE_URL="postgresql://postgres:MAT_KHAU@localhost:5432/edu_guidance"
```

---

### BƯỚC 2: Chạy kiểm tra hợp lệ dữ liệu (`--dry-run`)

Chạy chế độ `--dry-run` để kiểm tra lỗi dữ liệu mà không ghi vào CSDL:

```powershell
python import_qnu_to_postgres.py --dry-run
```

Hoặc qua CLI importer tổng quát:
```powershell
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv --dry-run
```

---

### BƯỚC 3: Import dữ liệu thực tế (Upsert)

```powershell
python import_qnu_to_postgres.py
```

Hoặc qua CLI importer tổng quát:
```powershell
python import_to_postgres.py --university DQN --offers data/DQN/dqn_admission_offers_2026.csv --cutoffs data/DQN/dqn_cutoff_scores_2025.csv
```

---

## 3. Xem chi tiết tài liệu kiến trúc toàn diện

Vui lòng tham khảo tài liệu chính thức tại [`README.md`](file:///c:/workspace/thesis-project/data-scrawler/README.md) và báo cáo kiến trúc tại [`report.md`](file:///c:/workspace/thesis-project/data-scrawler/report.md).

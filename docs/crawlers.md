# Module `crawlers/` - Adapter Pattern Crawler Framework

## 1. Mục đích

Framework crawler dữ liệu tuyển sinh đại học sử dụng **Adapter Pattern**, cho phép mở rộng dễ dàng cho nhiều trường đại học. Mỗi trường đại học sẽ có một adapter riêng (kế thừa `BaseCrawler`) và được đăng ký động trong `Registry`.

## 2. Kiến trúc tổng quan

```
crawlers/
├── base.py              # Abstract base class cho tất cả crawlers
├── registry.py          # Registry động (decorator pattern)
├── qnu.py               # Adapter cho Đại học Quy Nhơn (DQN)
├── common/              # Tiện ích chung (xem common-utilities.md)
│   ├── models.py
│   ├── subject_normalizer.py
│   ├── combination_normalizer.py
│   └── validators.py
└── __init__.py
```

## 3. Thành phần chi tiết

### 3.1 `base.py` - Abstract Base Crawler

Lớp trừu tượng định nghĩa interface chung cho tất cả crawler adapter.

**Hằng số cấu hình:**

| Hằng số | Giá trị | Mô tả |
|----------|---------|-------|
| `USER_AGENT` | Chrome 120 | User-Agent giả lập trình duyệt |
| `MAX_RETRIES` | 3 | Số lần thử lại tối đa khi fetch URL |
| `RETRY_DELAY` | 2.0s | Thời gian chờ giữa các lần retry |
| `REQUEST_TIMEOUT` | 15.0s | Timeout mỗi request HTTP |

**Lớp `BaseCrawler(ABC)`:**

```python
class BaseCrawler(ABC):
    def __init__(self, university_code: str, output_dir: str = "data")
    def fetch_url(self, url, retries=3, timeout=15.0) -> str          # HTTP GET với retry
    def write_csv(self, filepath, rows, fieldnames) -> None            # Ghi CSV UTF-8 BOM
    @abstractmethod
    def run(self, admission_year=2026, cutoff_year=2025) -> Dict       # Pipeline chính
```

**Thuộc tính instance:**
- `self.university_code` - Mã trường (viết hoa)
- `self.output_dir` - Thư mục gốc output (absolute path)
- `self.univ_data_dir` - Thư mục data theo trường (`{output_dir}/{UNIV_CODE}`)
- `self.raw_dir` - Thư mục lưu HTML thô (`{univ_data_dir}/raw`)

**Hàm tiện ích `get_iso_now()`:**
- Trả về chuỗi ISO 8601 với timezone offset cục bộ

### 3.2 `registry.py` - Crawler Registry

Registry động sử dụng decorator pattern để đăng ký và tìm kiếm crawler theo mã trường.

**Biến module:**
- `_CRAWLER_REGISTRY: Dict[str, Type[BaseCrawler]]` - Dictionary lưu mapping mã trường → lớp crawler

**Các hàm:**

```python
def register_crawler(university_code: str):
    """Decorator để đăng ký crawler adapter."""
    # Usage: @register_crawler("DQN")

def get_crawler(university_code: str, output_dir: str = "data") -> BaseCrawler:
    """Tạo và trả về instance crawler theo mã trường."""
    # Raises ValueError nếu mã trường chưa đăng ký

def list_registered_crawlers() -> List[str]:
    """Trả về danh sách mã trường đã đăng ký."""
```

### 3.3 `qnu.py` - Adapter Đại học Quy Nhơn (DQN)

Adapter crawler cụ thể cho Trường Đại học Quy Nhơn, đăng ký với mã `DQN`.

**Đăng ký:** `@register_crawler("DQN")`

**URL nguồn dữ liệu:**

| Năm | Loại | URL |
|------|------|-----|
| 2026 | Đề án tuyển sinh | `tuyensinh.qnu.edu.vn/.../tb-1906-thong-tin-tuyen-sinh-dai-hoc-nam-2026...` |
| 2025 | Điểm chuẩn | `tuyensinh.qnu.edu.vn/.../diem-chuan-trung-tuyen-dai-hoc-he-chinh-quy-nam-2025` |

**Ánh xạ mã phương thức `METHOD_MAP`:**

| Mã gốc | Mã chuẩn |
|---------|----------|
| `1` | `100` |
| `2` | `200` |
| `3` | `402A` |
| `4` | `402B` |
| `5` | `405` |

**Các hàm tiện ích:**

```python
def map_method_codes(raw_methods: str) -> str:
    """Chuyển đổi mã phương thức gốc sang mã chuẩn. VD: '1,2,3' → '100, 200, 402A'"""

def parse_score(val: Any) -> str:
    """Phân tích điểm từ chuỗi tiếng Việt. VD: '25,1' → '25.1'"""
```

**Lớp `QNUCrawler(BaseCrawler)`:**

```python
def parse_2026_offers(self, html_content, source_url, retrieved_at) -> List[Dict]:
    """Parse bảng đề án tuyển sinh 2026 từ HTML."""
    # Trích xuất: STT, Mã xét tuyển, Tên ngành, Phương thức, Chỉ tiêu, Tổ hợp môn
    # Trả về list dict với các trường: university_code, admission_year, program_code,
    #   program_name, method_codes, quota, combination_id, combination_display,
    #   subjects_json, source_url, retrieved_at

def parse_2025_cutoffs(self, html_content, source_url, retrieved_at) -> List[Dict]:
    """Parse bảng điểm chuẩn 2025 từ HTML."""
    # Trích xuất: Tên ngành, Mã ngành, Điểm THPT, Điểm SAT
    # method_code luôn là "100", score_type luôn là "OFFICIAL_CUTOFF"

def run(self, admission_year=2026, cutoff_year=2025) -> Dict[str, Any]:
    """Thực hiện pipeline crawler đầy đủ."""
    # Flow: fetch HTML 2026 → parse offers → ghi CSV → fetch HTML 2025 → parse cutoffs → ghi CSV → tạo manifest
```

**Output files:**

```
data/DQN/
├── raw/
│   ├── qnu_admissions_2026.html     # HTML thô
│   └── qnu_cutoff_2025.html         # HTML thô
├── dqn_admission_offers_2026.csv    # CSV đề án tuyển sinh
├── dqn_cutoff_scores_2025.csv       # CSV điểm chuẩn
└── source_manifest.json             # Metadata crawl
```

**Cấu trúc `source_manifest.json`:**

```json
{
  "crawled_at": "2026-08-26T09:00:00+07:00",
  "sources": [
    {
      "url": "https://tuyensinh.qnu.edu.vn/...",
      "raw_html_path": "raw/qnu_admissions_2026.html",
      "sha256": "abc123...",
      "retrieved_at": "2026-08-26T09:00:00+07:00"
    }
  ],
  "generated_files": [
    "data/DQN/dqn_admission_offers_2026.csv",
    "data/DQN/dqn_cutoff_scores_2025.csv"
  ]
}
```

## 4. Cách thêm crawler mới

1. Tạo file mới trong `crawlers/`, ví dụ `hust.py`
2. Kế thừa `BaseCrawler` và implement `run()`
3. Sử dụng decorator `@register_crawler("HUST")`
4. Import file trong `crawlers/__init__.py`
5. Thêm cấu hình trường vào `config/universities.json`

```python
from crawlers.base import BaseCrawler
from crawlers.registry import register_crawler

@register_crawler("HUST")
class HUSTCrawler(BaseCrawler):
    def run(self, admission_year=2026, cutoff_year=2025):
        # Triển khai crawler cụ thể
        return {"offers_csv": "...", "cutoffs_csv": "..."}
```

## 5. Phân quyền thay đổi

| File | Quyền |
|------|-------|
| `base.py` | Chỉ sửa khi thay đổi interface chung |
| `registry.py` | Không cần sửa khi thêm adapter mới |
| `qnu.py` | Chỉ sửa khi thay đổi logic crawl DQN |
| Adapter mới | Thêm file mới, không sửa file hiện có |

## 6. Phụ thuộc

- `lxml.html` - Parse HTML (cho QNU crawler)
- `csv`, `json`, `hashlib` - Tiêu chuẩn Python
- `urllib.request` - HTTP requests
- `crawlers.common.*` - Tiện ích chung

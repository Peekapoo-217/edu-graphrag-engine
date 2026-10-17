# Module `graph_sync/` - PostgreSQL → Neo4j Sync Pipeline

## 1. Mục đích

Pipeline đồng bộ dữ liệu từ PostgreSQL (source of truth) sang Neo4j Knowledge Graph (graph projection). Sử dụng idempotent MERGE Cypher để đảm bảo có thể chạy lại nhiều lần mà không tạo duplicate data.

## 2. Cấu trúc

```
graph_sync/
├── models.py                 # Graph data models & UID generation
├── postgres_reader.py        # Đọc dữ liệu từ PostgreSQL (read-only)
├── validators.py             # Kiểm tra tính toàn vẹn graph data
├── neo4j_writer.py           # Ghi dữ liệu vào Neo4j (MERGE batch)
├── check_neo4j_connection.py # Script kiểm tra kết nối Neo4j
└── __init__.py
```

## 3. `models.py` - Mô hình dữ liệu đồ thị

### 3.1 Variant Mapping

```python
CANONICAL_SUBJECT_MAP = {
    "TECH_INDUSTRY": "TECHNOLOGY",
    "TECH_AGRI": "TECHNOLOGY",
    "TECH_PHYSICS": "TECHNOLOGY",
    "TECH_BIOLOGY": "TECHNOLOGY",
}
```

Ánh xạ các biến thể môn Công nghệ về mã chuẩn `TECHNOLOGY`.

### 3.2 UID Generators

| Hàm | Format | VD |
|------|--------|-----|
| `make_subject_group_uid(school_code, academic_year, group_code)` | `{school}\|{year}\|{group}` | `TCV\|2026\|G01` |
| `make_training_program_uid(university_code, program_code)` | `{univ}\|{program}` | `DQN\|7480201` |
| `make_cutoff_score_uid(univ_code, year, program_code, method_code, score_type)` | `{univ}\|{year}\|{program}\|{method}\|{type}` | `DQN\|2025\|7480201\|100\|OFFICIAL_CUTOFF` |

### 3.3 Node Dataclasses

| Dataclass | Thuộc tính chính |
|-----------|------------------|
| `SubjectNode` | code, name, subject_type, is_mandatory, canonical_subject_code |
| `HighSchoolNode` | code, name, location |
| `SubjectGroupNode` | uid, school_code, academic_year, group_code, name |
| `UniversityNode` | code, name, website |
| `TrainingProgramNode` | uid, university_code, program_code, name |
| `AdmissionCombinationNode` | id, display_name, component_count |
| `AptitudeTestNode` | code, name |
| `CutoffScoreNode` | uid, university_code, admission_year, program_code, method_code, score_type, cutoff_score_thpt, cutoff_score_sat, source_url |

## 4. `postgres_reader.py` - Đọc PostgreSQL

### 4.1 `REQUIRED_POSTGRES_SCHEMA`

Dictionary định nghĩa 10 bảng và các cột bắt buộc:

| Bảng | Cột bắt buộc |
|------|--------------|
| `subjects` | code, name, subject_type, is_mandatory, canonical_subject_code |
| `high_schools` | code, name, location |
| `subject_groups` | school_code, academic_year, group_code, group_name, subject_code |
| `universities` | university_code, university_name, website |
| `majors` | program_code, program_name |
| `admission_offers` | id, university_code, admission_year, program_code, method_codes, quota, source_url |
| `offer_combinations` | offer_id, canonical_combination_id |
| `admission_combinations` | combination_id, combination_display, component_count |
| `admission_combination_components` | combination_id, component_code, component_name, component_type, subject_code, weight |
| `cutoff_scores` | university_code, admission_year, program_code, method_code, cutoff_score_thpt, cutoff_score_sat, source_url |

Reader lấy tên ngành từ `majors`, lấy tổ hợp của từng offer qua `offer_combinations` và đặt `CutoffScore.score_type = OFFICIAL_CUTOFF` trong graph vì PostgreSQL không có cột `score_type`. UID điểm chuẩn hiện có được giữ nguyên. Nhãn tổ hợp chưa liên kết không được chiếu sang Neo4j; tổ hợp chuẩn mới có node và các cạnh `REQUIRES_SUBJECT`, nhưng chỉ có `ACCEPTS_COMBINATION` khi có offer thực tế.

### 4.2 Các hàm

```python
def get_postgres_connection(database_url: str) -> psycopg.Connection:
    """Kết nối PostgreSQL và enforced read_only=True."""

def verify_postgres_schema(conn) -> Tuple[bool, List[str]]:
    """Kiểm tra tất cả bảng và cột yêu cầu tồn tại qua information_schema."""

def fetch_all_graph_data_from_postgres(conn) -> Dict[str, Any]:
    """Trích xuất toàn bộ dữ liệu graph từ PostgreSQL."""
```

### 4.3 Cấu trúc dữ liệu trả về từ `fetch_all_graph_data_from_postgres()`

```python
{
    "nodes": {
        "Subject": [...],
        "HighSchool": [...],
        "SubjectGroup": [...],
        "University": [...],
        "TrainingProgram": [...],
        "AdmissionCombination": [...],
        "AptitudeTest": [...],
        "CutoffScore": [...],
    },
    "relationships": {
        "OFFERS_GROUP": [...],        # HighSchool → SubjectGroup
        "INCLUDES_SUBJECT": [...],    # SubjectGroup → Subject
        "IS_VARIANT_OF": [...],       # Subject (variant) → Subject (canonical)
        "OFFERS_PROGRAM": [...],      # University → TrainingProgram
        "ACCEPTS_COMBINATION": [...], # TrainingProgram → AdmissionCombination
        "REQUIRES_SUBJECT": [...],    # AdmissionCombination → Subject
        "REQUIRES_APTITUDE": [...],   # AdmissionCombination → AptitudeTest
        "HAS_CUTOFF": [...],          # TrainingProgram → CutoffScore
    },
    "unpublished_subject_groups": [...],  # UID nhóm có trạng thái công bố false, cần xóa node
    "raw_components": [...]  # Dữ liệu thô cho validation
}
```

Nhóm đang công bố được chiếu thành node `SubjectGroup` và các cạnh tương ứng. Nhóm có `is_published=false` không đi vào node/relationship projection; nó được đưa vào `unpublished_subject_groups` từ `admin.subject_group_publication_state` làm tombstone bền vững. Writer xóa bằng UID tham số hóa và kiểm tra thêm trường/năm/mã nhóm cùng `publication_revision`, rồi `DETACH DELETE` đúng node. Khóa duy nhất của SubjectGroup là UID `{school_code}|{academic_year}|{group_code}`, nên nhóm cùng mã ở trường khác không bị đụng tới.

## 5. `validators.py` - Kiểm tra tính toàn vẹn

### 5.1 `validate_graph_data()`

```python
def validate_graph_data(graph_data: Dict[str, Any]) -> Tuple[bool, List[str]]
```

**3 nhóm kiểm tra:**

#### Nhóm 1: Duplicate UID Detection

Kiểm tra trùng lặp UID cho tất cả 8 loại node:
- Subject: code
- HighSchool: code
- SubjectGroup: uid
- University: code
- TrainingProgram: uid
- AdmissionCombination: id
- AptitudeTest: code
- CutoffScore: uid

#### Nhóm 2: Referential Integrity

Kiểm tra mỗi relationship tham chiếu đến node tồn tại:

| Relationship | Kiểm tra |
|--------------|----------|
| `OFFERS_GROUP` | high_school_code ∈ HighSchool, subject_group_uid ∈ SubjectGroup |
| `INCLUDES_SUBJECT` | subject_group_uid ∈ SubjectGroup, subject_code ∈ Subject |
| `IS_VARIANT_OF` | variant_subject_code ∈ Subject, canonical_subject_code ∈ Subject |
| `OFFERS_PROGRAM` | university_code ∈ University, training_program_uid ∈ TrainingProgram |
| `ACCEPTS_COMBINATION` | training_program_uid ∈ TrainingProgram, combination_id ∈ AdmissionCombination |
| `REQUIRES_SUBJECT` | combination_id ∈ AdmissionCombination, subject_code ∈ Subject |
| `REQUIRES_APTITUDE` | combination_id ∈ AdmissionCombination, aptitude_code ∈ AptitudeTest |
| `HAS_CUTOFF` | training_program_uid ∈ TrainingProgram, cutoff_score_uid ∈ CutoffScore |

#### Nhóm 3: Component Integrity

- Component type `SUBJECT` phải có `subject_code`
- Component type `APTITUDE` phải **không** có `subject_code`
- `component_count` trong `AdmissionCombination` phải khớp với số lượng component thực tế

## 6. `neo4j_writer.py` - Ghi Neo4j

### 6.1 `REQUIRED_CONSTRAINTS`

8 unique constraints cần có trên Neo4j:

| Label | Property |
|-------|----------|
| HighSchool | code |
| SubjectGroup | uid |
| Subject | code |
| University | code |
| TrainingProgram | uid |
| AdmissionCombination | id |
| AptitudeTest | code |
| CutoffScore | uid |

### 6.2 Node Cypher Statements

Tất cả sử dụng `MERGE` (idempotent) + `UNWIND` (batch):

```cypher
-- VD: Subject node
UNWIND $rows AS row
MERGE (s:Subject {code: row.code})
SET s.name = row.name,
    s.subject_type = row.subject_type,
    s.is_mandatory = row.is_mandatory,
    s.canonical_subject_code = row.canonical_subject_code
```

### 6.3 Relationship Cypher Statements

Tất cả sử dụng `MATCH` + `MERGE`:

```cypher
-- VD: OFFERS_GROUP relationship
UNWIND $rows AS row
MATCH (h:HighSchool {code: row.high_school_code})
MATCH (sg:SubjectGroup {uid: row.subject_group_uid})
MERGE (h)-[r:OFFERS_GROUP]->(sg)
```

### 6.4 Thứ tự đồng bộ

**Nodes (theo thứ tự):**
1. Subject
2. HighSchool
3. SubjectGroup
4. University
5. TrainingProgram
6. AdmissionCombination
7. AptitudeTest
8. CutoffScore

**Relationships (theo thứ tự):**
1. OFFERS_GROUP
2. INCLUDES_SUBJECT
3. IS_VARIANT_OF
4. OFFERS_PROGRAM
5. ACCEPTS_COMBINATION
6. REQUIRES_SUBJECT
7. REQUIRES_APTITUDE
8. HAS_CUTOFF

### 6.5 Các hàm

```python
def get_neo4j_driver(uri, auth) -> neo4j.Driver

def verify_neo4j_constraints(driver, database) -> Tuple[bool, List[str]]:
    """Kiểm tra 8 unique constraints tồn tại qua SHOW CONSTRAINTS."""

def chunk_list(items, batch_size) -> List[List]:
    """Chia list thành các batch con."""

def sync_nodes_and_relationships(driver, database, graph_data, batch_size=500):
    """Đồng bộ nodes và relationships theo thứ tự, dùng UNWIND MERGE batch."""

def verify_graph_statistics(driver, database) -> Dict[str, Any]:
    """Kiểm tra thống kê graph và orphan nodes."""
```

### 6.6 Orphan Detection

Kiểm tra 4 loại orphan node:
- `SubjectGroup` không có `HighSchool` nào `OFFERS_GROUP`
- `TrainingProgram` không có `University` nào `OFFERS_PROGRAM`
- `AdmissionCombination` không có `REQUIRES_SUBJECT` hay `REQUIRES_APTITUDE`
- `CutoffScore` không có `TrainingProgram` nào `HAS_CUTOFF`

## 7. Neo4j Graph Schema tổng quan

```
(:HighSchool)-[:OFFERS_GROUP]->(:SubjectGroup)-[:INCLUDES_SUBJECT]->(:Subject)
(:Subject)-[:IS_VARIANT_OF]->(:Subject)  [variant → canonical]
(:University)-[:OFFERS_PROGRAM]->(:TrainingProgram)
(:TrainingProgram)-[:ACCEPTS_COMBINATION {admission_year, method_codes, quota, source_url}]->(:AdmissionCombination)
(:AdmissionCombination)-[:REQUIRES_SUBJECT {weight}]->(:Subject)
(:AdmissionCombination)-[:REQUIRES_APTITUDE {weight}]->(:AptitudeTest)
(:TrainingProgram)-[:HAS_CUTOFF]->(:CutoffScore)
```

## 8. Projection nhóm môn được SUPER_ADMIN công bố

`graph_projection_worker.py` đồng bộ tăng dần các nhóm môn được công bố và thao tác CRUD trường THPT. Backend ghi nhóm môn và `admin.subject_group_graph_sync_jobs`, hoặc school change và `admin.high_school_graph_sync_jobs`, trong cùng PostgreSQL transaction. Worker claim hai hàng đợi bằng `FOR UPDATE SKIP LOCKED`. Mọi graph writer, gồm worker trường/tài liệu và lệnh full-sync, lấy cùng PostgreSQL advisory lock trước khi đọc trạng thái nguồn và giữ khóa đến khi ghi Neo4j hoàn tất. Một job chỉ là tín hiệu cần đối chiếu; trạng thái PostgreSQL hiện tại quyết định nhóm nào được MERGE và nhóm nào bị xóa. Vì vậy full-sync snapshot cũ không thể tạo lại node đã bị lifecycle job xóa. Writer thực hiện cập nhật nhóm trong một transaction Neo4j; job chỉ chuyển `SYNCED` sau khi graph transaction commit.

Worker có thể chạy lại an toàn. Job lỗi được chuyển `RETRY` với exponential backoff tối đa 30 phút; lease `RUNNING` quá 10 phút được đưa lại hàng đợi sau khi worker khởi động lại. Error detail nội bộ không được lưu vào bảng, chỉ lưu `GRAPH_SYNC_FAILED`; stdout ghi `claimed`, `retry`, và `synced` cùng ID job/trường/tài liệu. Các nhóm môn hợp lệ có từ một đến bốn môn khác nhau. Chạy trong root Compose bằng service `graph-projection-worker`; cần `DATABASE_URL`, `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` và migration V12 đã được áp dụng. Soft-delete tài liệu không xóa graph; chỉ thao tác **Ngừng công bố** mới xóa node nhóm khi trạng thái hiệu lực PostgreSQL cho nhóm không còn nguồn công bố.

## 9. Vòng đời công bố nhóm môn theo tài liệu

V11 tạo `admin.subject_group_publication_state` theo khóa trường/năm/mã nhóm, có `is_published` và `publication_revision`; `admin.document_subject_group_sources` ghi nguồn tài liệu. Backfill chỉ gán nguồn khi audit publication và nhóm PostgreSQL khớp. Nhóm không xác minh được đánh dấu `UNATTRIBUTED`; API ngừng công bố trả HTTP 409 và không thay đổi trạng thái graph. V12 chỉ sửa source rows có thể ánh xạ không xung đột từ mã trường cũ sang mã hiện tại qua `admin.high_school_code_history`; các xung đột không tự suy đoán. Khi nhiều tài liệu cùng là nguồn của một nhóm, nhóm vẫn được công bố nếu còn ít nhất một nguồn đang công bố.

Soft-delete chỉ đánh dấu tài liệu/bản nháp trong PostgreSQL và giữ object key trong MinIO. Nó không cập nhật trạng thái graph, nên nhóm vẫn tra cứu được. ADMIN tạo yêu cầu `DELETE_REQUESTED`; SUPER_ADMIN duyệt mới hoàn tất soft-delete, còn SUPER_ADMIN có thể xóa trực tiếp. `SUPER_ADMIN` có thể gọi API ngừng công bố với tài liệu đã soft-delete; PostgreSQL tính trạng thái hiệu lực rồi ghi outbox cùng transaction. Worker xóa node `SubjectGroup` và mọi cạnh nối vào node đó khi không còn nguồn tài liệu đang công bố. Nhóm còn nguồn công bố khác được giữ nguyên. Đồng bộ toàn bộ PostgreSQL → Neo4j áp dụng cùng trạng thái và xóa lại các node có tombstone. Thiếu provenance hoặc thiếu một nhóm đã công bố trong mapping sẽ trả HTTP 409 trước khi sửa trạng thái. Khôi phục kiểm tra tồn tại mọi object version, bật lại nguồn tài liệu, rồi dựng lại node/quan hệ bằng `MERGE` từ PostgreSQL; các node môn và trường dùng chung không bị xóa. PostgreSQL advisory lock được dùng chung giữa worker và full-sync để job cũ không tạo lại node sau khi trạng thái mới hơn đã được áp dụng. Lặp lại restore dùng lại job đã ghi audit.

Worker ghi tiến độ ra stdout để xem ngay khi chạy foreground hoặc theo dõi service Compose:

```text
[graph-sync] claimed job=<id> document=<id> school=<code> year=<year>
[graph-sync] retry job=<id> document=<id> error=<type> delay_seconds=<seconds>
[graph-sync] synced job=<id> document=<id>
[graph-sync] claimed school-job=<id> school=<code> operation=<operation>
[graph-sync] retry school-job=<id> school=<code> error=<type> delay_seconds=<seconds>
[graph-sync] synced school-job=<id> school=<code>
```

```powershell
docker compose logs -f graph-projection-worker
```

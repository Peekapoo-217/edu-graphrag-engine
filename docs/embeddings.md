# Module `embeddings/` - Vector Embedding Pipeline

## 1. Mục đích

Script ETL (Extract-Transform-Load) tự động hóa việc tạo vector embedding cho các `TrainingProgram` nodes trong Neo4j, sử dụng Ollama REST API với model `bge-m3` (1024-dimensional vectors). Các vector này được lưu trực tiếp lên nodes Neo4j và được sử dụng bởi `rag_api/retrieval/graph_retriever.py` cho Vector Similarity Search.

## 2. Cấu trúc

```
embeddings/
├── embedder.py      # ETL pipeline chính
└── __init__.py
```

## 3. `embedder.py` - Vectorization ETL

### 3.1 Pipeline Flow

```
┌─────────────────────────────────────────┐
│  1. Kết nối Neo4j                       │
└─────────────┬───────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────┐
│  2. Query TrainingProgram nodes         │
│     WHERE embedding IS NULL             │
└─────────────┬───────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────┐
│  3. Với mỗi node:                       │
│     a. construct_embedding_text()       │
│     b. get_ollama_embedding()           │
│     c. update_node_vector_property()    │
└─────────────┬───────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────┐
│  4. Lặp lại cho đến khi không còn node  │
│     nào thiếu embedding                 │
└─────────────────────────────────────────┘
```

### 3.2 Các hàm

#### `get_neo4j_driver()`

```python
def get_neo4j_driver() -> neo4j.Driver:
    """Tạo và xác minh kết nối Neo4j."""
    # Kiểm tra NEO4J_PASSWORD tồn tại
    # Tạo driver với NEO4J_URI và auth
    # Verify connectivity
    # Exit(1) nếu kết nối thất bại
```

#### `get_ollama_embedding()`

```python
def get_ollama_embedding(
    text: str,
    model: Optional[str] = None,     # Mặc định: settings.OLLAMA_EMBED_MODEL
    ollama_url: Optional[str] = None, # Mặc định: settings.OLLAMA_HOST
    timeout: Optional[float] = None   # Mặc định: settings.OLLAMA_TIMEOUT
) -> List[float]:
    """
    Tạo embedding vector từ text qua Ollama REST API.
    
    Endpoint: POST {OLLAMA_HOST}/api/embeddings
    Body: {"model": "bge-m3", "prompt": text}
    Returns: List[float] (1024 dimensions)
    
    Raises: RuntimeError nếu HTTP request thất bại
    """
```

#### `fetch_unembedded_training_programs()`

```python
def fetch_unembedded_training_programs(
    driver: neo4j.Driver,
    database: Optional[str] = None,
    limit: Optional[int] = None   # Mặc định: settings.EMBEDDER_BATCH_SIZE
) -> List[Dict[str, Any]]:
    """
    Query Neo4j tìm TrainingProgram nodes chưa có embedding.
    
    Cypher:
    MATCH (tp:TrainingProgram)
    WHERE tp.embedding IS NULL
    OPTIONAL MATCH (u:University)-[:OFFERS_PROGRAM]->(tp)
    RETURN tp.uid, tp.program_code, tp.name, u.code, u.name
    LIMIT $limit
    """
```

#### `construct_embedding_text()`

```python
def construct_embedding_text(node_data: Dict[str, Any]) -> str:
    """
    Tạo text enriched để vectorization.
    
    Format: "Ngành đào tạo: {name}. Mã ngành: {code}. Trường: {univ_name} ({univ_code})."
    
    VD: "Ngành đào tạo: Kỹ thuật phần mềm. Mã ngành: 7480103. Trường: Đại học Quy Nhơn (DQN)."
    """
```

#### `update_node_vector_property()`

```python
def update_node_vector_property(
    driver: neo4j.Driver,
    database: Optional[str],
    uid: str,
    embedding: List[float]
) -> None:
    """
    Lưu embedding vector lên node Neo4j.
    
    Cypher:
    MATCH (tp:TrainingProgram {uid: $uid})
    CALL db.create.setNodeVectorProperty(tp, 'embedding', $embedding)
    """
```

#### `run_embedding_pipeline()`

```python
def run_embedding_pipeline() -> None:
    """
    Thực hiện ETL pipeline đầy đủ:
    1. Kết nối Neo4j
    2. Query nodes thiếu embedding (theo batch)
    3. Với mỗi node: tạo text → gọi Ollama → lưu vector
    4. Lặp lại cho đến khi hoàn tất
    """
```

### 3.3 Batch Processing

Pipeline xử lý theo batch với `EMBEDDER_BATCH_SIZE` (mặc định 50):
- Query tối đa 50 nodes thiếu embedding mỗi lần
- Xử lý từng node trong batch
- Lặp lại cho đến khi không còn node nào thiếu embedding

### 3.4 Error Handling

- Nếu Ollama API thất bại cho 1 node → log lỗi, ngủ 1s, tiếp tục node tiếp theo
- Nếu Neo4j connection thất bại → exit(1)
- Nếu NEO4J_PASSWORD thiếu → exit(1)

## 4. Cấu hình

### 4.1 Environment Variables

| Biến | Mặc định | Mô tả |
|------|----------|-------|
| `NEO4J_URI` | - | Neo4j connection URI |
| `NEO4J_USERNAME` | `neo4j` | Neo4j username |
| `NEO4J_PASSWORD` | - | Neo4j password (bắt buộc) |
| `NEO4J_DATABASE` | `edugraph` | Tên database |
| `OLLAMA_HOST` | - | Ollama service URL |
| `OLLAMA_EMBED_MODEL` | - | Tên model embedding (VD: `bge-m3`) |
| `OLLAMA_TIMEOUT` | `30.0` | Timeout mỗi request (giây) |
| `EMBEDDER_BATCH_SIZE` | `50` | Số node xử lý mỗi batch |

### 4.2 Yêu cầu hệ thống

- Neo4j đã chạy và có `TrainingProgram` nodes (từ `sync_postgres_to_neo4j.py`)
- Ollama đã chạy với model `bge-m3` đã pull
- Vector index `program_vector_index` đã tạo trên Neo4j

## 5. Chạy script

```bash
# Từ data-scrawler/
python embeddings/embedder.py

# Output mẫu:
# === STARTING TRAININGPROGRAM VECTORIZATION ETL PIPELINE ===
# Ollama Model:    bge-m3
# Ollama Host:     http://localhost:11434
# Target Database: edugraph
# Batch Size:      50
# Connected to Neo4j database 'edugraph' at neo4j://127.0.0.1:7687
# Processing batch of 53 TrainingProgram nodes...
# [1] Processed UID 'DQN|7480101' | Dim: 1024 | Text: 'Ngành đào tạo: CNTT. Mã ngành:...'
# ...
# === ETL PIPELINE FINISHED: Total 53 TrainingProgram nodes embedded. ===
```

## 6. Mối quan hệ với các module khác

```
embeddings/embedder.py
    ├── Đọc từ: Neo4j (TrainingProgram nodes từ sync_postgres_to_neo4j.py)
    ├── Ghi vào: Neo4j (trường `embedding` trên TrainingProgram nodes)
    ├── Gọi: Ollama REST API (bge-m3 model)
    └── Config từ: rag_api/utils/config.py
```

## 7. lưu ý

- Script này phải chạy **SAU** khi đã sync dữ liệu từ PostgreSQL sang Neo4j (`sync_postgres_to_neo4j.py`)
- Vector index `program_vector_index` phải được tạo trước khi chạy RAG API
- Model `bge-m3` phải được pull trong Ollama trước khi chạy

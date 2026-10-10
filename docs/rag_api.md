# Module `rag_api/` - Hybrid GraphRAG Admissions Advisor API

> Lưu ý cho hướng tư vấn học sinh lớp 9: ngoài hội thoại RAG hiện hữu, `/api/chat` có nhánh `subject_group_recommendation`. Nhánh này xác thực nhãn trường/môn với catalog của Spring API rồi ủy quyền xếp hạng cho `POST /api/v1/high-schools/{schoolCode}/subject-groups/recommendations`; LLM không tự tính điểm. Cấu hình kết nối bằng `SUBJECT_PATHWAY_API_URL` và `SUBJECT_PATHWAY_API_TIMEOUT`. Xem công thức và ví dụ request tại [`doc/subject-group-recommendations.md`](../../doc/subject-group-recommendations.md).

## 1. Mục đích

API FastAPI phục vụ hệ thống RAG (Retrieval-Augmented Generation) tư vấn tuyển sinh đại học. Kết hợp Vector Similarity Search (bge-m3), Neo4j Knowledge Graph Traversal, và LLM (Claude Sonnet) để trả lời câu hỏi người dùng.

## 2. Cấu trúc

```
rag_api/
├── main_api.py              # FastAPI app entry point & orchestrator
├── utils/
│   ├── config.py            # Centralized configuration
│   └── logger.py            # Standardized logger
├── retrieval/
│   └── graph_retriever.py   # Hybrid Neo4j Vector + Graph Traversal
├── llm/
│   ├── intent_parser.py     # Query Router & Intent Parser
│   └── llm_client.py        # RAG Answer Generator
├── prompts/
│   └── prompt_templates.py  # Prompt templates
└── __init__.py
```

## 3. `main_api.py` - FastAPI Application

### 3.1 Khởi tạo

```python
app = FastAPI(
    title="Hybrid GraphRAG Admissions Advisor API",
    version="1.0.0",
    lifespan=lifespan
)
```

**Lifespan Manager:**
- Startup: Khởi tạo `IntentParser`, `Neo4jRetriever`, `RAGGenerator` singletons
- Pre-verify Neo4j connectivity
- Shutdown: Đóng Neo4j driver

### 3.2 CORS

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 3.3 Pydantic Models

#### `ChatRequest`

```python
class ChatRequest(BaseModel):
    user_message: str          # Tin nhắn câu hỏi mới
    context: List[Dict]        # Lịch sử hội thoại [{"role": "user"|"assistant", "content": "..."}]
    chat_history: Optional[List[Dict]]  # Legacy alias

    def get_conversation_context() -> List[Dict]
```

#### `ChatResponse`

```python
class ChatResponse(BaseModel):
    status: str    # "ready", "incomplete", hoặc "success"
    message: str   # Nội dung phản hồi
    data: Optional[Dict]  # Dữ liệu chi tiết (filters, records_count, context_summary)
```

### 3.4 API Endpoints

#### `POST /api/chat`

**Luồng xử lý 5 bước:**

```
Step 1: Parse Intent
    └── parser.parse_intent(user_message, context) → {status, extracted_filters, reply_to_user}

Step 2: Check Completeness
    ├── status == "incomplete" → Return reply_to_user (không query DB)
    └── status == "ready" → Extract filters, tiếp tục

Step 3: Retrieve Context from Neo4j
    ├── retriever.retrieve_context(filters, top_k=3) → records
    └── retriever.format_for_llm(records) → formatted_context (Markdown)

Step 4: Generate LLM Answer
    └── generator.generate_answer(user_query, graph_context, context) → answer_text

Step 5: Return ChatResponse
    └── {status: "success", message: answer_text, data: {filters, retrieved_count, context_summary}}
```

#### `GET /health`

```json
{
  "status": "healthy",
  "service": "Hybrid GraphRAG Admissions Advisor API",
  "neo4j_status": "connected",
  "ollama_host": "http://localhost:11434",
  "llm_model": "claude-3-5-sonnet"
}
```

## 4. `utils/config.py` - Centralized Configuration

### 4.1 Settings Dataclass

```python
@dataclass
class Settings:
    # Database
    DATABASE_URL: Optional[str]
    
    # Neo4j
    NEO4J_URI: str
    NEO4J_USERNAME: str
    NEO4J_PASSWORD: Optional[str]
    NEO4J_DATABASE: str
    
    # LLM (9router Gateway)
    LLM_API_KEY: Optional[str]     # Hoặc GEMINI_API_KEY
    LLM_API_BASE: str
    LLM_MODEL: str
    LLM_TIMEOUT: float             # Default: 30.0s
    
    # API Server
    API_HOST: str                   # Default: 0.0.0.0
    API_PORT: int                   # Default: 8000
    
    # Ollama Embedding
    OLLAMA_HOST: str
    OLLAMA_EMBED_MODEL: str         # bge-m3
    OLLAMA_TIMEOUT: float           # Default: 30.0s
    EMBEDDER_BATCH_SIZE: int        # Default: 50
```

Tự động load `.env` file khi import module.

### 4.2 Singleton

```python
settings = Settings()  # Được import bởi tất cả sub-modules
```

## 5. `retrieval/graph_retriever.py` - Hybrid Retriever

### 5.1 `Neo4jRetriever` Class

```python
class Neo4jRetriever:
    def __init__(self, uri=None, auth=None, database=None)
    def get_driver() -> neo4j.Driver      # Lazy initialization
    def close() -> None                    # Đóng driver
    def _get_embedding(text) -> List[float]  # Ollama bge-m3 1024-dim
    def retrieve_context(filters, top_k=3) -> List[Dict]
    def format_for_llm(records) -> str     # Markdown format
```

### 5.2 Vector Search

Gọi Ollama REST API (`/api/embeddings`) với model `bge-m3` để tạo vector 1024-dim từ text query.

```python
def _get_embedding(self, text: str) -> List[float]:
    # POST {OLLAMA_HOST}/api/embeddings
    # Body: {"model": "bge-m3", "prompt": text}
    # Returns: List[float] (1024 dimensions)
```

### 5.3 `retrieve_context()` - Hybrid Retrieval

**2 chiến lược query:**

#### Khi có keyword (vector search):

```cypher
CALL db.index.vector.queryNodes('program_vector_index', $top_k, $vector)
YIELD node AS p, score AS similarity_score
MATCH (u:University)-[:OFFERS_PROGRAM]->(p)
-- ... filters ...
ORDER BY score DESC
LIMIT $top_k
```

#### Khi không có keyword (graph traversal):

```cypher
MATCH (u:University)-[:OFFERS_PROGRAM]->(p:TrainingProgram)
WHERE ($university IS NULL OR toLower(u.name) CONTAINS toLower($university))
  AND ($keyword IS NULL OR toLower(p.name) CONTAINS toLower($keyword))
-- ... OPTIONAL MATCHES ...
LIMIT $top_k
```

**Filters hỗ trợ:**

| Filter | Mô tả |
|--------|-------|
| `keyword` | Từ khóa ngành học (trigger vector search) |
| `university` | Tên hoặc mã trường |
| `score_max` | Điểm tối đa (cutoff filter) |
| `combination` | Khối/tổ hợp môn |
| `high_school` | Tên hoặc mã trường THPT |

### 5.4 `format_for_llm()` - Markdown Output

```markdown
### NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC (NEO4J KNOWLEDGE GRAPH)

#### 1. Ngành: Kỹ thuật phần mềm (Mã ngành: 7480103) (Độ tương đồng Vector: 0.8542)
- **Trường Đại học:** Đại học Quy Nhơn (DQN)
- **Tổ hợp môn xét tuyển:** CHEMISTRY_MATH_PHYSICS (Toán, Lý, Hóa)
  -> Các môn bắt buộc: Toán, Lý, Hóa
- **Khuyên chọn Nhóm môn THPT:** Trường TCV - Nhóm môn: **Nhóm 1**
  -> Các môn học thuộc nhóm môn THPT này: **Toán, Lý, Hóa**
- **Lịch sử điểm chuẩn:**
  - Năm 2025 (Phương thức 100): Điểm THPT: **24.5**
```

## 6. `llm/intent_parser.py` - Query Router

### 6.1 `IntentParser` Class

```python
class IntentParser:
    def __init__(self, api_base=None, api_key=None, model=None, timeout=30.0)
    def parse_intent(self, user_message, context=None) -> Dict
    def is_ready_for_db(self, parsed_json) -> bool
```

### 6.2 System Prompt

```
Bạn là Chuyên gia Điều phối Thông tin (Query Router) cho Hệ thống RAG Tư vấn Tuyển sinh.

Nhiệm vụ:
1. Đọc tin nhắn mới + lịch sử trò chuyện
2. Trích xuất filters: keyword, university, high_school, score_max, combination
3. Đánh giá status:
   - "ready" nếu có ÍT NHẤT 1 trong: keyword, university, high_school
   - "incomplete" nếu thiếu cả 3

Output JSON:
{
  "status": "ready"|"incomplete",
  "extracted_filters": {...},
  "reply_to_user": "câu hỏi gợi mở" | null
}
```

### 6.3 `is_ready_for_db()`

Kiểm tra `status == "ready"` và至少 1 filter chính (`keyword`, `university`, hoặc `high_school`) có giá trị.

### 6.4 Fallback Heuristic

Khi không có LLM API key hợp lệ, sử dụng rule-based parser với predefined keywords/universities/high_schools.

## 7. `llm/llm_client.py` - RAG Generator

### 7.1 `RAGGenerator` Class

```python
class RAGGenerator:
    def __init__(self, api_base=None, api_key=None, model=None, timeout=None)
    def generate_answer(self, user_query, graph_context, context=None) -> str
```

### 7.2 System Prompt

```
Bạn là Chuyên gia Kỹ sư Tư vấn Tuyển sinh Đại học.

NGUYÊN TẮC TỐI THƯỢNG: CHỈ trả lời dựa vào [NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC].
Nếu không có dữ liệu → Xin lỗi và thông báo.
TUYỆT ĐỐI KHÔNG tự bịa đặt (No Hallucination).
```

### 7.3 Fallback

Khi không có LLM API key hợp lệ, trả về mock response chứa graph context trực tiếp.

## 8. Chạy API Server

```bash
# Từ data-scrawler/
python -m rag_api.main_api

# Hoặc
uvicorn rag_api.main_api:app --host 0.0.0.0 --port 8000 --reload
```

## 9. Ví dụ sử dụng

```bash
# Gửi câu hỏi
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "user_message": "Em muốn học ngành CNTT ở Quy Nhơn điểm bao nhiêu?",
    "context": []
  }'

# Response
{
  "status": "success",
  "message": "Dựa trên dữ liệu đồ thị tri thức, ngành CNTT tại ĐH Quy Nhơn...",
  "data": {
    "filters": {"keyword": "cntt", "university": "quy nhơn", ...},
    "retrieved_count": 3,
    "context_summary": "### NGỮ CẢNH DỮ LIỆU..."
  }
}
```

## 10. Prompt Templates

`prompts/prompt_templates.py` chứa:
- `SYSTEM_PROMPT` - System prompt chính
- `QA_CONTEXT_PROMPT` - Template cho QA context (legacy/unused)

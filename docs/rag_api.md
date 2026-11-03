# Module `rag_api/` - Hybrid GraphRAG Admissions Advisor API

Sơ đồ PlantUML mức tổng quan về luồng chuẩn bị dữ liệu và xử lý chat: [`graph-rag-chatbot-pipeline.puml`](graph-rag-chatbot-pipeline.puml).

> Luồng `subject_group_recommendation` và endpoint `/recommendations` là hợp đồng cũ còn được giữ để tương thích. Chat hiện tại dùng intent `subject_group_comparison`, đối chiếu từng tiêu chí và không xếp hạng tổng hợp; xem mục 12. Tài liệu request cũ vẫn có tại [`doc/subject-group-recommendations.md`](../../doc/subject-group-recommendations.md).

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
    status: str    # "success", "incomplete", "no_data", hoặc "unsupported"
    message: str   # Nội dung phản hồi
    data: Optional[Dict]  # Dữ liệu chi tiết (filters, retrieved_context, evidence)
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
    ├── retriever.retrieve_context(filters, top_k=21) → tối đa 20 records trong response
    └── retriever.format_for_llm(records) → formatted_context (Markdown)

Step 4: Generate LLM Answer
    └── generator.generate_answer(user_query, graph_context, context) → answer_text

Step 5: Return ChatResponse
    └── {status: "success", message: answer_text, data: {filters, retrieved_count, retrieved_context, evidence, evidence_truncated}}
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

Khi có filter `high_school`, retriever lấy các nhóm môn của năm học mới nhất từ graph và chỉ giữ offer/tổ hợp có đầy đủ mọi môn yêu cầu trong nhóm đó hoặc trong danh sách môn bắt buộc. So sánh dùng `canonical_subject_code` khi có, nếu không dùng `code`. Các nhóm phù hợp được gắn trực tiếp vào từng tổ hợp tương ứng; chỉ trùng một môn không tạo kết quả phù hợp. Tổ hợp có yêu cầu bài thi năng khiếu không được xác nhận chỉ bằng nhóm môn THPT. Trường/ngành vẫn phải có cạnh `ACCEPTS_COMBINATION`; nhóm môn THPT không tự tạo admission offer. Kết quả này gợi ý tổ hợp môn, không khẳng định đủ điều kiện trúng tuyển.

### 5.4 `format_for_llm()` - Markdown Output

```markdown
### NGỮ CẢNH DỮ LIỆU ĐỒ THỊ TRI THỨC (NEO4J KNOWLEDGE GRAPH)

#### 1. Ngành: Kỹ thuật phần mềm (Mã ngành: 7480103) (Độ tương đồng Vector: 0.8542)
- **Trường Đại học:** Đại học Quy Nhơn (DQN)
- **Tổ hợp môn xét tuyển:** CHEMISTRY_MATH_PHYSICS (Toán, Lý, Hóa) -> Môn yêu cầu: Toán, Lý, Hóa
- **Nhóm môn THPT phù hợp:** Trường TCV - Nhóm 1 (Mã: A01, năm học 2026-2027) -> Môn trong nhóm: Toán, Lý, Hóa
  - Phạm vi khớp chỉ là độ phủ môn học; không xác nhận thí sinh đủ điều kiện tuyển sinh.
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

### 6.4 Provider error handling

The historical heuristic parser fallback has been removed. A missing key, provider error, invalid JSON, or unusable response produces an HTTP 503 response; the service does not fabricate a successful parse.

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

### 7.3 Provider error handling

The historical mock answer fallback has been removed. A missing key, provider error, or empty/unusable response produces an HTTP 503 response. The chat UI can then show a retry message without exposing deployment commands.

Parser, retrieval, and generation HTTP errors include additive `X-Request-ID`, `X-Error-Stage`,
`X-Error-Type`, and `X-Error-Code` response headers. The stage is `intent`, `retrieval`, or
`generation`; the code is a sanitized `timeout`, `connect_timeout`, `read_timeout`,
`provider_failure`, or `error`. Intent connection timeouts mean the connection was not
established; read timeouts mean no response data arrived within the configured Requests read
timeout. A read timeout does not identify whether the provider or an intermediary caused the
delay. These headers
contain no provider message, prompt, credentials, or conversation history.
When a provider returns an HTTP 4xx/5xx response, the Chatbot also returns a numeric
`X-Upstream-Status` header (for example `429`). This is the upstream status only; provider body
text and credentials are not forwarded. Each parser/generation stage sends one completion POST,
does not retry client-side, and disables redirects.

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
    "retrieved_context": "### NGỮ CẢNH DỮ LIỆU...",
    "evidence": [{"program_name": "...", "combination_name": "..."}],
    "evidence_truncated": false
  }
}
```

## 10. Prompt Templates

`prompts/prompt_templates.py` chứa:
- `SYSTEM_PROMPT` - System prompt chính
- `QA_CONTEXT_PROMPT` - Template cho QA context (legacy/unused)

## 11. Environment-only model configuration

The RAG API and PDF extraction worker read `LLM_API_KEY`, `LLM_API_BASE`, and `LLM_MODEL` from their environment. Intent classification, chat answer generation, and document extraction remain distinct requests/flows, while using the configured model and provider. There is no internal endpoint for retrieving or overriding model configuration, and the backend admin UI cannot read or change these values. Keep the key in the local `.env` file and out of source control.

## 12. Current chat contract — 2026-10-18

This section is the current contract and supersedes the earlier five-step example above.

- `POST /api/chat` accepts `user_message` and up to 40 prior turns in `context`. The current message is sent separately and is not duplicated in history. `chat_history` remains a compatibility alias.
- The router selects a bounded intent: greeting, unsupported, knowledge, school group lookup, combination lookup, pathway lookup, subject-group advice, or subject-group comparison. Only explicit user evidence may become a preference, strength, confidence value, or grade. An assessment scale is accepted only when the user states it.
- Subject-group comparison resolves school and subject names against the Backend catalog. Ambiguous or unknown labels are returned for clarification. It sends the profile to `POST /api/v1/high-schools/{schoolCode}/subject-groups/comparisons`; the Backend returns criterion-by-criterion matches and real admission offers. It does not calculate a percentage or aggregate ranking.
- The comparison response keeps the legacy `subjects` field as the effective set (group subjects plus mandatory subjects) and adds `groupSubjects` and `mandatorySubjects` so the chat formatter can state group membership without conflating the two. Group-composition replies use `groupSubjects`; mandatory/common subjects are shown separately when present. Matching admission combinations are only listed when the user asks about combinations/pathways or a verified requested-program offer exists.
- Combination lookup by public code uses `GET /api/v1/admissions/combinations/{code}`; exact 2-4 subject sets use `POST /api/v1/admissions/combinations/resolve-by-subjects`. The existing `search-by-subjects` capability-search semantics are unchanged. A school lookup uses the Backend's high-school and subject-group endpoints.
- Current response statuses are `success`, `incomplete`, `no_data`, and `unsupported`. General RAG responses return `data.retrieved_context`, structured `data.evidence` (at most 20 records), `data.retrieved_count`, `data.context_evidence_ids`, `data.omitted_evidence_ids`, and `data.evidence_truncated`. Empty retrieval is `no_data`; Neo4j, Backend, or LLM unavailability returns HTTP 503.
- Both router and answer generation read `settings.LLM_MODEL` from `data-scrawler/.env`; the configured value remains `ag/claude-sonnet-4-6(high)`. No provider/model fallback or mock answer is returned on failure.


## 13. Relevance evaluation response contract - 2026-10-19

- `data.response_mode` is additive and uses `answer`, `clarification`, `abstention`, or `unsupported`. Existing API status and fields remain compatible.
- `data.retrieval_units` contains ordered evidence with `id`, `source`, `record_type`, and `text`. The evaluator uses these units; `data.retrieved_context` remains available to older clients.
- `data.actual_execution_route` separates `intent_handler`/`handler` from the retrieval route. It includes `retrieval_executed` (`true`, `false`, or `null`), `retrieval_status` (`executed`, `not_executed`, or `unknown`), `retrieval_route`, and the observed `evidence_source`. It also retains `execution_kind`, `graph_retrieval_executed`, `generation_executed`, request/trace reference, and available evidence entity/relationship IDs. `response_disposition` mirrors `data.response_mode`; `disposition_reason` is populated only when a handler provides a clarification, abstention, or unsupported reason. When route evidence is insufficient, `execution_kind` stays `unknown` and `unknown_route_reason` identifies the missing evidence; it is never inferred from answer text. This field is additive and contains no question text or credentials.
- A two-school comparison keeps the school/group pair stated in the current user message. A group may be written as a numbered label (for example, “Nhóm 02”) or a public code (“G02”); aliases are paired only inside a clause that identifies one school, then resolved against that school's academic-year catalog. If a clause names multiple schools or the pair cannot be established, the response asks for clarification. For a verified pair of single groups, the response lists their shared and distinct group subjects by catalog code; it does not infer admission eligibility or list completeness beyond those groups.

## 14. Exact context and Backend resolver contract — 2026-10-20

- A successful Graph generation response includes `data.generation_context_exact=true` and `data.generation_context_sha256`. The digest is SHA-256 over the UTF-8 bytes of the exact `graph_context` string passed to `generator.generate_answer`. `data.retrieved_context` carries that same string; the response does not duplicate it in a `generation_context` field. This attests to the Graph evidence argument, not to the full system prompt or conversation history.
- The evaluator accepts `retrieved_context` as exact generation evidence only when the explicit flag is true and its digest matches. It never infers exactness from `retrieval_units`, an oracle context, or a plain legacy response. Other response paths do not claim this Graph context contract.
- `POST /api/v1/admissions/combinations/resolve-by-subjects` must return an object with `matches` as a list and a non-empty `resultScope`. Match, component, and label records are type-validated against the Backend response shape. An empty `matches: []` is valid `no_data`; missing/null/wrong-typed fields or malformed JSON produce HTTP 502 from the chatbot adapter. The error contains no raw upstream body.
- School-group lookup can return only explicitly requested group codes. Subject-catalog lookup limits evidence to the requested subject or classification category.
- If a student has not named a high school, the advice route acknowledges their stated preferences/strengths and asks for the school before comparing actual groups.
- The router and answer model continue to read `settings.LLM_MODEL` from `.env`, currently `ag/claude-sonnet-4-6(high)`. Unit tests do not call the model or database.


## Evaluation diagnostic addendum - combination lookup

For an explicit admission-combination code, the resolver reads the combination-label endpoint and preserves all returned labels. It avoids interpreting a clearly named `G01`-style school-group code as an admission-combination code. For an explicit set of subject names, it resolves exact catalog names/aliases, requires 2-4 unique subjects, and calls the additive exact resolver endpoint `/api/v1/admissions/combinations/resolve-by-subjects`. Ambiguous names and insufficient subject sets produce a clarification. A zero-row search is reported as no matching data in the current dataset, not as proof that the combination does not exist.

A code returned by the intent parser is used only when the code also appears in the current user message or a prior user turn. Assistant text is not code provenance. If the current turn names a different code than a stale parser filter, the current user-provided code takes precedence; multiple codes in the current turn trigger clarification.

The evaluator diagnostic suite contains 20 deterministic fixtures for code lookup, name/subject resolution, ambiguity, errors, multiple candidates/labels, and conversation-history routing. The follow-up history case mocks the parser output; it verifies that prior turns reach the parser and does not establish live model parsing accuracy. Aptitude-only phrasing is not resolved by the regular-subject catalog yet and currently reaches subject-count clarification (fixture PR13); this remains a recorded diagnostic limitation.

## Current R1a-R3c chat/retrieval contract - 2026-10-20

- Admission combination lookup by a public code still uses `GET /api/v1/admissions/combinations/{code}` and preserves all latest labels, including unlinked labels. Lookup by an exact 2-4 subject set resolves catalog names/aliases, then calls the additive endpoint `POST /api/v1/admissions/combinations/resolve-by-subjects` with `{"subjectCodes":[...]}`. This endpoint only resolves exact subject-only compositions; `POST /api/v1/admissions/search-by-subjects` retains its existing capability-search semantics.
- Comparison requests keep `academicYear` for the THPT school-year scope and send `admissionYear` and `methodCode` separately for admission evidence. Objective comparison does not require preferences/grades; personalized advice continues to require relevant user profile evidence. Missing requested groups/labels do not erase known facts; response data declares `answer_completeness` and missing codes.
- Graph retrieval applies canonical combination, admission-year and method filters; high-school subject coverage honors explicit `academic_year`. Aptitude components are retained as separate requirements and clearly remain unverified by subject-group coverage.
- Graph context records include stable `evidence_id`, business-key `relationship_ids` and `evidence_path`; `/api/chat` returns `context_evidence_ids`, `omitted_evidence_ids`, `retrieved_context`, and `evidence_truncated` for traceable context assembly.
- Graph records include `evidence_dependency_validation` with complete/incomplete path status, required/missing relationship IDs, missing requested scope dependencies (for admission year/method or school/year), and explicit `aptitude_coverage: unverified` when applicable. A graph response becomes `answer_completeness: partial` when a retrieved record has an incomplete path; this does not claim that an aptitude component is satisfied by a THPT subject group.
- Offline verification: 128 Data Scrawler tests and 12 focused Backend tests passed. Live service/database and paired benchmark validation have not been run.

## Mixed school-group facts and advice

When a request asks both which subject groups a named school offers and for tentative advice, the chatbot returns both tasks in one response. If the user has stated preferences or strengths, the existing comparison endpoint supplies group facts and preference matches. Advice is limited to those stated signals and verified `groupSubjects`, and the response retains the limitation that it does not establish admissions eligibility or outcome. If preferences are missing, the chatbot returns the verified group facts and asks for the missing preference information; it marks `answer_completeness` as `partial`.

The response may include additive `requested_tasks`, `answered_tasks`, and `unanswered_tasks` arrays. Existing `status`, `message`, evidence, response mode, and execution-route fields keep their meanings. A deterministic school-group lookup without an advice request remains a concise lookup.

Chat logs include parse, retrieval, and generation durations. Stage failures identify the stage and exception class without logging exception text, prompts, secrets, or conversation contents. These log fields do not change the response contract.

## Graph pathway school identity resolution

School mentions for graph, school-group, advice and comparison routes are resolved through the same Backend `GET /api/v1/high-schools` catalog matcher. A unique catalog match replaces the raw phrase with the catalog name and code; graph `SCHOOL_GROUPS_QUERY` uses that code as an exact identity filter, while Backend lookup handlers use it as the school business key. The optional `data.school_resolution` field records the resolved code/name and catalog provenance for school-group responses. A school name in the catalog may include a location qualifier; location metadata is used only when the catalog actually supplies it. If a name remains unknown or ambiguous, the chatbot asks the user to confirm the school and does not pass the raw phrase to a fuzzy graph filter. If the catalog is unavailable or malformed, the request fails closed with HTTP 503. Academic-year and subject-coverage matching remain unchanged. A program is returned as a school-to-program path only when verified school groups cover its required subjects or the missing subjects are marked mandatory; aptitude requirements remain separately unverified. No graph relationship is inferred from name similarity. University-only wording is not treated as an explicit THPT identity.

For an explicit school, academic year, and group scope, the Graph route may return a `subject_overlap` partial result only when the selected group and program satisfy the requested school/year scope and each reported subject overlap is supported by the directed relationship IDs present in that record's evidence path. When the current question also names an exact admission combination, the response may separately report its acceptance only if the canonical combination, university/program, admission year, official HTTPS source domain, and corresponding directed `ACCEPTS_COMBINATION` edge all match. Admission-combination acceptance does not establish that the selected school group satisfies the full path. This partial result reports verified subject overlap; it does not establish complete admission eligibility. The Graph Evidence Contract V2 dependency status remains incomplete whenever required mandatory-subject scope or provenance is unavailable. If no overlap can be verified, the handler returns an incomplete response without asking generation to infer a negative result. Explicit group aliases are resolved against the authoritative school/year catalog; ambiguous aliases remain unresolved.

### Approved response-mode decisions (2026-10-26)

- For an unqualified school name that matches exactly one entry in the authoritative high-school catalog, the school-group lookup answers and discloses the canonical school, code, and catalog location as an explicit assumption. The user can correct that assumption. If the catalog returns multiple candidates, the chatbot asks for location or school code; it never selects a candidate by list order. Unknown or unavailable catalog results remain non-answers.
- A year-specific eligibility question in the pathway route asks for any missing high-school identity, university, program, or admission year before making an eligibility conclusion. `academic_year` is not a substitute for `admission_year`. Known slots are not requested again. A narrow question asking whether one shared subject by itself guarantees eligibility or admission receives a general no-guarantee answer without asserting year-specific eligibility; a concrete eligibility comparison still requires the admission year. This gate does not change ordinary pathway information lookups.
- These two behavior decisions are recorded as human-reviewed `behavior_gold` annotations in the separately fingerprinted response-mode approval sidecar. They do not approve reference answers, factual claims, graph paths, or any other scenarios.

Cross-school group comparison resolves and queries each school independently using the same requested academic year. Results carry only evidence returned for that school; a missing requested group is reported in `unavailable_group_requests`, and a school with no available source result is reported separately. A response may be marked partial when at least one school's facts are verified. It does not infer shared/different subjects for a school whose requested evidence is unavailable. Singular history references such as “nhóm đó” ask for clarification when the prior user turn named multiple distinct group codes.

Graph generation trims leading and trailing whitespace from the formatted retriever context before passing it to the generator. `retrieved_context` and `generation_context_sha256` attest to that same normalized string.

In explicitly enabled diagnostics, the append-only stage trace records a `generation` / `input` event immediately before the provider POST. It contains the trimmed graph-context character and UTF-8 byte counts, its SHA-256 digest, full message-content size, message/history counts, and the configured provider timeout. It never stores prompt text, answers, API credentials, or provider response bodies. The event remains available if the provider request times out; it does not establish why the provider was slow or supply a token count without tokenizer data.

For subject-group advice, the response names candidate groups only when their explicit `groupSubjects` contains a subject the student stated they like. Multiple matching groups are presented as unranked tentative candidates. If no candidate is supported by the available evidence, the response gives the verified group facts and asks for more preference information; the advice task is marked unanswered and the result partial. Candidate wording is explicitly tentative and does not imply guaranteed fit or admissions eligibility.

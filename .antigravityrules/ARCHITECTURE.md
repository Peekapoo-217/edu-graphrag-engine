# KIẾN TRÚC HỆ THỐNG (SYSTEM ARCHITECTURE)
**Dự án:** Hệ thống Tư vấn Hướng nghiệp Hybrid GraphRAG

## 1. TỔNG QUAN KIẾN TRÚC (HIGH-LEVEL ARCHITECTURE)
Hệ thống sử dụng luồng kiến trúc Agentic RAG phân tách rõ ràng giữa khâu Điều phối/Phân giải (Routing/Intent Parsing), Truy xuất dữ liệu đa phương thức (Hybrid Retrieval), và Sinh văn bản (Generation).

## 2. STACK CÔNG NGHỆ BẮT BUỘC (TECH STACK)
- **Ngôn ngữ cốt lõi:** Python 3.10+
- **Cơ sở dữ liệu Đồ thị & Vector:** Neo4j. 
  - *Lưu ý:* Bắt buộc sử dụng thư viện `neo4j` (raw driver python) để tối ưu hiệu suất truy vấn. Không sử dụng các wrapper cồng kềnh như Langchain Graph nếu không thực sự cần thiết.
- **Mô hình Nhúng (Embedding Model):** `bge-m3` (Không gian vector: 1024 dimensions).
  - *Triển khai:* Chạy local thông qua Ollama REST API. Chuyên đảm nhận việc vector hóa câu hỏi và dữ liệu đầu vào.
- **Mô hình Sinh & Điều phối (LLM / Generation):** `ag/claude-sonnet-4-6(high)`.
  - *Triển khai:* Gọi qua API Gateway của 9router. Đảm nhận nhiệm vụ bóc tách ý định người dùng (Intent Parsing) và sinh câu trả lời cuối cùng.

## 3. NGUYÊN LÝ THIẾT KẾ (DESIGN PRINCIPLES)
- **Single Responsibility (Đơn trách nhiệm):** Mỗi module/file chỉ đảm nhận một vai trò duy nhất trong Pipeline. 
  - File điều phối (Intent Parser) KHÔNG chứa logic gọi database. 
  - File truy xuất (Retriever) KHÔNG chứa logic format câu trả lời cho người dùng cuối.
- **Centralized Configuration:** Mọi cấu hình kết nối, API keys, endpoints phải được quản lý tập trung từ file `.env` thông qua module `rag_api.utils.config`.
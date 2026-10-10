# KỶ LUẬT VIẾT MÃ (CODING GUIDELINES)

## 1. QUY TẮC TỐI THƯỢNG (STRICT RULES)
- **KHÔNG HARD-CODE:** Tuyệt đối không hard-code API Key, Database URI, User, Password, Port hay bất kỳ URL nào trực tiếp trong file mã nguồn. Bắt buộc load từ module `config`.
- **DRY (Don't Repeat Yourself):** Dọn dẹp sạch sẽ code. Loại bỏ ngay lập tức các thư viện (imports) không sử dụng, các biến khai báo nhưng không dùng, và các đoạn code bị comment (dead code).
- **Type Hinting Bắt buộc:** Tất cả các hàm và phương thức trong Python phải khai báo Type Hinting rõ ràng cho cả tham số đầu vào và giá trị trả về (ví dụ: `def search(query: str) -> list[dict]:`).

## 2. QUY CHUẨN ĐẶT TÊN (NAMING CONVENTIONS)
- **Biến và Hàm (Variables & Functions):** Sử dụng `snake_case` (ví dụ: `get_embedding_vector`, `user_input`).
- **Lớp (Classes):** Sử dụng `PascalCase` (ví dụ: `Neo4jRetriever`, `IntentParser`).
- **Hằng số (Constants):** Sử dụng `UPPER_SNAKE_CASE` (ví dụ: `MAX_RETRIES = 3`, `DEFAULT_TIMEOUT`).

## 3. KỶ LUẬT TRUY VẤN ĐỒ THỊ (CYPHER & NEO4J)
- **Parameterized Queries BẮT BUỘC:** MỌI tham số động truyền vào lệnh Cypher phải sử dụng syntax biến của Neo4j (ví dụ: `$keyword`, `$vector`, `$score`). **Tuyệt đối không dùng f-string của Python để nối chuỗi giá trị vào lệnh Cypher** (để tránh Cypher Injection và tối ưu hóa Neo4j query cache).
- **Quản lý Session Database:** Luôn mở và đóng kết nối database an toàn. Bắt buộc dùng Context Manager (block `with driver.session() as session:`) để tự động giải phóng tài nguyên mạng.

## 4. XỬ LÝ LỖI & LOGGING (ERROR HANDLING & LOGGING)
- **Không dùng `print()`:** Trong môi trường thực thi chuẩn, lỗi phát sinh không được dùng `print()` đơn thuần. Phải sử dụng hệ thống Logging (ví dụ: `from rag_api.utils.logger import logger`).
- **An toàn thực thi (Fail-safe):** Mọi thao tác I/O (gọi Database, gọi HTTP API) đều phải bọc trong `try...except`. Nếu xảy ra lỗi, hàm phải ghi log và trả về giá trị fallback an toàn (như rỗng `[]`, `None`, hoặc `dict` mặc định) thay vì làm crash toàn bộ tiến trình ứng dụng.
# REPORT - data-scrawler

## Đồng bộ worker với xóa tài liệu — 2026-10-14

- [x] T1. Worker không claim job của tài liệu DELETE_REQUESTED/DELETED; khóa tài liệu trước khi claim và trước khi ghi kết quả để đồng bộ thứ tự khóa với backend.
- [x] T2. Sau OCR/LLM, kiểm tra lại trạng thái tài liệu và job; bỏ kết quả nếu tài liệu đã xóa hoặc job đã bị CANCELLED. Khi chỉ có yêu cầu xóa, job đang chạy có thể ghi draft nhưng không đổi trạng thái DELETE_REQUESTED.
- [x] T3. Giữ nguyên hành vi retry/failure khi tài liệu còn hoạt động; lỗi muộn không làm sống lại tài liệu đã xóa.
- [x] T4. Thêm unit test cho trạng thái claim, stop khi đã xóa, và giữ trạng thái xóa yêu cầu. `python -m unittest discover -s tests`: 57 test đạt.
- [ ] Chưa restart worker triển khai; backend và worker cần được cập nhật cùng nhau để dùng trạng thái job CANCELLED từ Flyway V7.

## OCR–ETL ảnh tài liệu THPT — 2026-10-13

- [x] Worker đọc `content_type`, giữ OCR PDF và bổ sung nhánh ảnh PNG/JPEG/WebP. Ảnh được giải mã, giới hạn 20 megapixel, OCR cục bộ bằng Tesseract rồi gửi văn bản tới model `EXTRACTION`; JSON và mã môn được kiểm tra trước khi tạo bản nháp nguồn `[1]`.
- [x] Phân loại lỗi model/timeout an toàn, không để job phiên bản cũ ghi đè trạng thái phiên bản mới. Request đặt `stream=false` để gateway trả JSON thay vì SSE mặc định.
- [x] `python -m unittest discover -s tests`: 54 test đạt. Đã build và recreate `pdf-worker` với Tesseract OCR ảnh và request văn bản.
- [x] Chẩn đoán route vision 9Router trả HTTP 200 nhưng không đọc ảnh; chuyển sang OCR cục bộ và sửa Tesseract dùng `--psm 3` sau khi `--psm 6` bỏ mất các hàng bảng.
- [x] Retry tài liệu mẫu qua API: job `SUCCEEDED`, document `DRAFT_READY`, 3 draft nhóm môn hợp lệ được lưu ở trang 1.

## Admin THPT và trích xuất PDF — 2026-10-12

Checklist dùng chung cho phần data-scrawler:

- [x] Worker nhận job trích xuất từ PostgreSQL và lấy PDF riêng tư từ R2.
- [x] Đọc text PDF, OCR trang scan tiếng Việt/Anh, giới hạn kích thước/trang và dọn file tạm.
- [x] Trích xuất draft có trường nguồn trang; kiểm tra dữ liệu LLM như đầu vào không tin cậy.
- [x] Không tự công bố dữ liệu; backend quản lý phê duyệt và ghi nhóm môn chuẩn hóa.
- [x] Kiểm thử worker và cập nhật tài liệu contract.

Ghi nhận ban đầu: `.env` chưa có R2 vars, requirements chưa có thư viện PDF và chưa có worker/job. Cấu hình key được giữ kín; chưa thử kết nối R2.

Cập nhật triển khai: worker đã được thêm và truy vấn các bảng workflow qua schema `admin` (`admin.extraction_jobs`, `admin.school_document_versions`, `admin.school_documents`, `admin.subject_group_drafts`); danh mục môn vẫn nằm trong `public.subjects`. Cần backend áp dụng Flyway V6 trước khi chạy worker. Contract và hướng dẫn cấu hình ở `docs/pdf_worker.md`. `python -m unittest discover -s tests` đã chạy: 42 test đạt; chưa kiểm tra kết nối R2 hoặc PostgreSQL thực.

## Report: cào danh mục tổ hợp môn

Ngày triển khai: 2026-10-10. Nguồn: https://diemthi.tuyensinh247.com/to-hop-mon.html

### Thay đổi

- `crawl_combination_labels.py` chỉ đọc hai cột **Tổ hợp** và **Môn chi tiết**. Ô nhiều mã được tách thành các dòng riêng. Output gồm CSV chỉ có hai trường dữ liệu và manifest ghi URL, niên khóa, thời điểm, SHA-256, số dòng.
- `import_combination_labels.py` xác thực snapshot và schema Flyway V3, rồi upsert vào PostgreSQL. Chỉ nối `canonical_combination_id` khi ba môn phổ thông khớp duy nhất. Dòng xung đột, môn năng khiếu, thiếu mapping hoặc không có tổ hợp chuẩn vẫn được lưu với liên kết null.
- Importer tự nạp `data-scrawler/.env`; biến `DATABASE_URL` đã export trong terminal vẫn được ưu tiên. Vì vậy khi đang ở thư mục `data-scrawler`, có thể chạy trực tiếp mà không cần tự export biến môi trường.
- Bộ chuẩn hóa môn bổ sung alias `GDKTPL` và `Giáo dục kinh tế và pháp luật` để liên kết đúng các tổ hợp X tương ứng.
- Không đồng bộ Neo4j vì nhãn chỉ phục vụ hiển thị, không thay đổi quan hệ hay đề xuất.

### Snapshot và cách chạy

Snapshot 2026-10-10: 261 dòng nguồn, 293 dòng sau tách mã; 11 mã có mô tả xung đột.

```powershell
python crawl_combination_labels.py --dry-run
python crawl_combination_labels.py
python import_combination_labels.py --dry-run
## Sau khi backend áp dụng Flyway V3 và cấu hình DATABASE_URL an toàn:
python import_combination_labels.py
```

Importer không tạo bảng, không xóa dữ liệu và từ chối snapshot cũ hơn lần nhập gần nhất. PostgreSQL chưa được ghi trong phiên này: kết nối local `127.0.0.1:5432` bị từ chối xác thực với thông tin trong `.env`.

### Kiểm tra

`python -m unittest discover -s tests` và `python import_combination_labels.py --dry-run` đã chạy thành công ngày 2026-10-10.

### Cập nhật triển khai 2026-10-11: tạo tổ hợp chuẩn từ nhãn

- Giữ `admission_combination_labels` làm dữ liệu nguồn theo mã, mô tả, URL, kỳ và thời điểm cào. Một bộ ba môn có thể có nhiều mã như B08 và D08; bộ ba đã chuẩn hóa có một `admission_combinations.combination_id` ổn định (mã môn sắp xếp rồi ghép bằng `_`). K01 hiện có hai mô tả khác nhau trong snapshot nên cả hai nhãn K01 vẫn chưa được liên kết. Không thay 50 tổ hợp cũ và không xóa 316 liên kết `offer_combinations`.
- `import_combination_labels.py --promote-normalized` đối chiếu mã với tổ hợp đã có, rồi tạo tổ hợp và ba component `SUBJECT` cho mỗi bộ ba môn mới có đủ trong `subjects`. Tên hiển thị và component lấy từ bảng `subjects`; `weight = 1`. Mã có mô tả xung đột, năng khiếu, môn chưa ánh xạ hoặc bộ môn không hợp lệ vẫn có nhãn nhưng không được tạo tổ hợp. Mã tổ hợp trùng một hàng hiện có nhưng khác thành phần không được ghi đè. Mọi ghi của một lần import nằm trong một transaction; import lại dùng cùng khóa, không thêm bản ghi trùng.
- `--dry-run --promote-normalized` đọc PostgreSQL với transaction read-only để tính số lượng dự kiến và không ghi. Với snapshot 293 dòng hiện tại và PostgreSQL lúc kiểm tra: 36 tổ hợp chuẩn mới, 108 component, tổng 88 nhãn liên kết (48 đã có và 40 mới). 205 nhãn còn lại chưa liên kết: 22 mô tả xung đột, 1 mã không hợp lệ, 180 chưa ánh xạ hoặc năng khiếu, 2 không đủ ba thành phần.
- `graph_sync/postgres_reader.py` nay đọc đúng schema V1: `admission_offers` nối `majors` lấy tên ngành, nối `offer_combinations` lấy tổ hợp; `cutoff_scores` nối `majors` lấy tên ngành. `score_type` graph được gán `OFFICIAL_CUTOFF` để giữ UID cũ. Neo4j lấy projection từ PostgreSQL; tổ hợp mới sẽ có node và cạnh môn, nhưng không tự có cạnh ngành–tổ hợp nếu chưa có offer. Bộ kiểm tra số lượng graph so với projection thay cho con số cố định.
- Không cần migration Flyway: V1 đã có `admission_combinations`, `admission_combination_components`, `subjects`, `offer_combinations`; V3 đã có `admission_combination_labels`. Không sửa crawler nguồn và không cập nhật schema API.

#### Trình tự vận hành

```powershell
cd C:\workspace\thesis-project\data-scrawler
python import_combination_labels.py --dry-run --promote-normalized
python import_combination_labels.py --promote-normalized
python sync_postgres_to_neo4j.py --dry-run
python sync_postgres_to_neo4j.py
```

Chỉ chạy hai lệnh ghi sau khi đã xem kết quả dry run và đã cho phép ghi vào PostgreSQL, Neo4j của môi trường đích. Trong phiên triển khai này chỉ chạy dry run, không ghi dữ liệu thật. `python sync_postgres_to_neo4j.py --dry-run` đã xác nhận projection hiện tại có 50 tổ hợp, 316 cạnh `ACCEPTS_COMBINATION` và 52 điểm chuẩn, validation thành công.

### Bổ sung ngày 2026-10-11: chuẩn hóa từng biến thể của một mã

Lệnh `python import_combination_labels.py --normalize-catalogue` mở rộng nhãn nguồn có lựa chọn dấu `/` trước khi gắn `canonical_combination_id`. K01 tạo bốn dòng `Toán, Văn, Lý`, `Toán, Văn, Hóa`, `Toán, Văn, Sinh`, `Toán, Văn, Tin`, bên cạnh dòng K01 `Toán, Tiếng Anh, Tin học`. Q00 tạo hai dòng theo lựa chọn thứ ba. Dòng nguồn có dấu `/` vẫn được giữ nguyên trong PostgreSQL với canonical null để truy vết; mỗi dòng biến thể rõ nghĩa có canonical riêng. Nhiều mô tả cùng một mã được xét độc lập, không còn loại toàn bộ mã vì xung đột mô tả.

Bộ chuẩn hóa dùng mã môn sẵn có cho Công nghệ công nghiệp/nông nghiệp, thêm năm ngoại ngữ ghi rõ tên vào `subjects`, và giữ các bài vẽ/năng khiếu cụ thể thành `APTITUDE` component. ID tổ hợp tính từ mã thành phần đã sắp xếp; trước khi tạo mới, importer tìm bộ thành phần có sẵn. Tất cả thao tác ghi trong cùng transaction. Không tạo schema hoặc xóa dữ liệu.

Dry run đối với PostgreSQL trước khi nhập (86 tổ hợp, 293 nhãn) dự kiến xử lý 299 dòng nhãn từ 293 dòng nguồn, trong đó 283 biến thể có thể gắn canonical, tạo 158 bộ thành phần mới và 5 môn ngoại ngữ. Hai dòng gốc có dấu `/` được giữ null để truy vết. Còn 13 biến thể có tên thành phần chung/chưa xác định (`Ngoại ngữ`, `Năng khiếu`, `NK1`, `Khoa học...`) và 1 dòng có mã không hợp lệ. Đây là giới hạn của snapshot, không phải lỗi khóa ngoại; không được tự gán mã môn cụ thể cho các dòng này.

#### Kết quả chạy thực tế

Sau khi chạy `python import_combination_labels.py --normalize-catalogue` và `python sync_postgres_to_neo4j.py`, PostgreSQL có 299 nhãn (283 gắn canonical, 16 null), 244 tổ hợp và 27 môn. K01 có năm dòng cụ thể đã gắn canonical và một dòng nguồn có dấu `/` còn null. Dry run importer lần hai cho thấy 0 tổ hợp và 0 môn cần tạo thêm. Neo4j có 244 `AdmissionCombination`, 27 `Subject`, 46 `AptitudeTest`, 668 `REQUIRES_SUBJECT`, 65 `REQUIRES_APTITUDE`; 316 `ACCEPTS_COMBINATION` giữ nguyên. Báo cáo graph không có orphan và số lượng khớp projection PostgreSQL. `python -m unittest discover -s tests` chạy 37 test, tất cả đạt.

**Đánh giá:** Phần crawl hai cột và nhập các biến thể rõ nghĩa đáp ứng hợp đồng hiện tại. Chuẩn hóa toàn bộ dòng nguồn vẫn **cần quyết định nghiệp vụ** cho 13 biến thể tên chung, 1 mã không hợp lệ và 2 dòng gốc có lựa chọn. Không thể xác minh môn cụ thể của các tên chung chỉ từ snapshot. Không thay đổi schema; các khóa, FK và upsert hiện có đáp ứng phần đã triển khai.

### Bổ sung ngày 2026-10-11: tên nội bộ cho thành phần chung

Người dùng đã giao tự đặt tên. Importer hiện dùng `FOREIGN_LANGUAGE_UNSPECIFIED` cho “Ngoại ngữ” chưa xác định, mã `APTITUDE_CATALOGUE_<hash>` theo mã tổ hợp nguồn và tên cho các cụm đánh giá chung, và `SRC107B55D` làm mã nội bộ cho dòng nguồn có ô mã sai. Các mã này không xác nhận một môn/bài thi/mã công bố cụ thể. Dòng nguồn sai mã và hai dòng gốc có dấu `/` vẫn được giữ với canonical null để truy vết.

Sau import: PostgreSQL có 300 nhãn (297 gắn canonical, 3 dòng gốc null), 257 tổ hợp, 772 thành phần, 28 môn. Neo4j có 257 `AdmissionCombination`, 28 `Subject`, 62 `AptitudeTest` và giữ nguyên 316 `ACCEPTS_COMBINATION`. Dry run import lặp lại dự kiến thêm 0 tổ hợp và 0 môn; 39 test đạt. Không thay đổi các bản ghi offer, major hoặc cutoff. Xem `docs/REPORT.md` để biết phân tích bảng, quy tắc mã và giới hạn ngữ nghĩa.

---

## Báo cáo: chuẩn hóa nhãn tổ hợp và ảnh hưởng tới các bảng tuyển sinh

Ngày kiểm tra: 2026-10-11. Phạm vi: snapshot hai cột `Tổ hợp` và `Môn chi tiết` từ https://diemthi.tuyensinh247.com/to-hop-mon.html. Phân tích theo các skill `database-schema-engineering` và `python-crawl-database-integration`.

### Quy tắc đặt mã

- `FOREIGN_LANGUAGE_UNSPECIFIED` lưu đúng nghĩa “Ngoại ngữ (chưa xác định)”; không suy ra Tiếng Anh hay ngôn ngữ cụ thể.
- “Năng khiếu”, “NK1”, “Khoa học”, “Khoa học tự nhiên”, “Khoa học xã hội” được giữ nguyên tên nguồn, có mã `APTITUDE_CATALOGUE_<hash>` xác định theo mã tổ hợp nguồn và tên thành phần. Đây là thành phần đánh giá/tên chung chưa xác định, không phải một môn học phổ thông cụ thể. Giới hạn của schema V1 là loại component chỉ có `SUBJECT` và `APTITUDE`, nên các tên đánh giá chung đi qua nhánh `APTITUDE` và được chiếu thành node `AptitudeTest` trong Neo4j. Không được hiểu các node này là bài thi năng khiếu đã được xác minh.
- Dòng nguồn có ô mã `(Ngữ văn, Vật lí, Tiếng Hàn)` được giữ nguyên với canonical null. Thêm nhãn nội bộ ổn định `SRC107B55D` cùng mô tả để trỏ tới `KOREAN_LITERATURE_PHYSICS`. Tiền tố `SRC` là mã do hệ thống sinh, không phải mã tổ hợp chính thức.
- Hai dòng nguồn K01 và Q00 chứa dấu `/` giữ canonical null để truy vết. Các biến thể đã tách có canonical riêng.

### Ảnh hưởng bảng PostgreSQL

| Bảng | Kết luận |
| --- | --- |
| `admission_combination_labels` | Upsert 300 nhãn từ 293 dòng snapshot, 297 nhãn liên kết canonical và 3 dòng gốc null. |
| `admission_combinations` | Thêm 13 bộ thành phần mới, tổng 257. ID canonical dựa trên thành phần, không dựa trên mã công bố. |
| `admission_combination_components` | Thêm 39 thành phần, tổng 772. Thành phần tên chung giữ mã riêng, không gán thành môn cụ thể. |
| `subjects` | Thêm `FOREIGN_LANGUAGE_UNSPECIFIED`, tổng 28. |
| `admission_offers`, `offer_combinations`, `majors`, `cutoff_scores` | Không cập nhật. Số bản ghi vẫn lần lượt là 53, 316, 54, 52. Snapshot không chứa trường, ngành, năm, phương thức hoặc điểm chuẩn để tạo quan hệ tuyển sinh. |

Không cần migration Flyway cho phép nạp bổ sung hiện tại: V1 đã có các bảng canonical, component, subject và khóa ngoại từ `offer_combinations`; V3 đã có nhãn nguồn với khóa ngoại canonical nullable. Kiểm tra sau ghi: 0 nhãn trỏ tới canonical không tồn tại và 0 quan hệ offer trỏ tới canonical không tồn tại. Không xóa hay đổi ID của 316 quan hệ offer hiện có.

Các tổ hợp mới chỉ trở thành tổ hợp được một trường/ngành chấp nhận khi có dữ liệu tuyển sinh của trường đó và một `offer_combinations` tương ứng. Tự thêm quan hệ từ danh mục chung sẽ tạo khẳng định sai. API tra cứu trường/ngành dựa trên `offer_combinations` nên không tự xuất hiện trường hoặc ngành mới; truy vấn tìm tổ hợp theo môn chỉ xét component `SUBJECT`, vì vậy các tổ hợp chứa component tên chung kiểu `APTITUDE` không được khớp như bộ môn phổ thông.

### Neo4j và kiểm chứng

Đã đồng bộ Neo4j từ PostgreSQL. Graph có 257 `AdmissionCombination`, 28 `Subject`, 62 `AptitudeTest`, 689 `REQUIRES_SUBJECT`, 83 `REQUIRES_APTITUDE`; `ACCEPTS_COMBINATION` vẫn 316. Công cụ báo số lượng khớp projection và không có orphan.

Đã chạy `python -m unittest discover -s tests`: 39 test đạt. Đã chạy importer dry run trước ghi (13 tổ hợp và 1 subject dự kiến), import thực tế, dry run lần hai (0 tổ hợp và 0 subject mới), Neo4j dry run, sync và verify. Kiểm tra SQL sau ghi xác nhận 300 nhãn, 297 liên kết, 3 dòng gốc null, 0 FK lệch.

### Giới hạn cần nhớ ở phiên sau

Các mã `APTITUDE_CATALOGUE_...` chỉ lưu chính xác cụm chữ nguồn. Khi có nguồn chính thức mô tả bản chất từng bài/nhóm đánh giá, nên thiết kế loại component riêng qua migration được duyệt và chuyển graph contract tương ứng; không gộp chúng vào một môn hoặc một bài năng khiếu cụ thể bằng suy đoán. Mã `SRC107B55D` cũng chỉ là định danh nội bộ để liên kết dòng sai mã.

---

## Theo dõi chuẩn hóa mã tổ hợp và tách môn lựa chọn

Ngày bắt đầu: 2026-10-11. Phạm vi: `data-scrawler`; cập nhật phần khác chỉ khi hợp đồng dữ liệu/API thực sự thay đổi.

### Checklist

- [x] 1. Kiểm kê 293 nhãn nguồn, các mẫu dấu `/`, mã trùng, môn chưa ánh xạ và dữ liệu PostgreSQL hiện tại. **Report:** Snapshot có 293 dòng/282 mã; PostgreSQL có 86 tổ hợp, 88 nhãn liên kết và 205 nhãn null. Có 11 mã với nhiều mô tả; 3 dòng chứa dấu `/` hoặc ngoặc cần phân tích cú pháp; 65 cách ghi thành phần chưa có trong normalizer hiện tại (233 lần xuất hiện). Các nhóm lớn: biến thể Công nghệ, ngoại ngữ, năng khiếu/mỹ thuật. Hai dòng có dấu phẩy bên trong mô tả năng khiếu hoặc hơn ba thành phần.
- [x] 2. Xác định quy tắc tách lựa chọn, mã canonical ổn định, giữ nguồn và xử lý nhãn mơ hồ. **Report:** Người dùng giao tự đặt tên. Đã đặt mã tường minh cho Ngoại ngữ chưa xác định, giữ các tên đánh giá chung theo mã nguồn và thêm mã nội bộ SRC cho dòng nguồn sai mã. Hai dòng gốc có dấu / vẫn để canonical null, các biến thể có liên kết riêng. Chi tiết tại docs/REPORT.md.
- [x] 3. Triển khai chuẩn hóa/import idempotent; không xóa hàng hoặc sửa liên kết tuyển sinh hiện có. **Report:** Đã thêm `combination_label_normalizer.py` và chế độ `--normalize-catalogue`. K01 tách thành bốn biến thể, Q00 thành hai; mã trùng được xét theo từng mô tả. Importer thêm môn ngoại ngữ theo danh sách cho phép, tạo tổ hợp mới và upsert nhãn trong một transaction; không sửa `offer_combinations`. Đã chạy import thực tế thành công.
- [x] 4. Bổ sung kiểm thử cho K01, mã trùng, môn đặc biệt, chạy lặp và tình huống không thể suy diễn an toàn. **Report:** Đã có test tách K01/Q00, dấu phẩy trong ngoặc, ngoại ngữ/Công nghệ, môn năng khiếu riêng, mã trùng, chạy lại planner, tên chung chưa thể ánh xạ và đường ghi SQL bằng connection giả. `python -m unittest discover -s tests`: 37/37 đạt.
- [x] 5. Chạy unit test, dry run PostgreSQL và Neo4j; ghi số lượng trước/sau dự kiến. **Report:** PostgreSQL import thành công: 299 nhãn, 283 liên kết canonical, 244 tổ hợp, 27 môn. Dry run lần hai dự kiến thêm 0 tổ hợp và 0 môn. Neo4j dry run và sync thực tế thành công: 244 tổ hợp, 27 môn, 46 AptitudeTest, 316 `ACCEPTS_COMBINATION`, 668 `REQUIRES_SUBJECT`, 65 `REQUIRES_APTITUDE`; graph khớp projection và không có orphan trong báo cáo.
- [x] 6. Cập nhật tài liệu hợp đồng dữ liệu và báo cáo vận hành. **Report:** Đã bổ sung lệnh, quy tắc tách và số liệu dry run trong `docs/entry_scripts.md` và `docs/REPORT.md`.

### Giới hạn ghi dữ liệu

Các lệnh kiểm tra là read-only. Không xóa nhãn nguồn, tổ hợp, component hoặc quan hệ Neo4j. Mọi nhãn không thể diễn giải chắc chắn sẽ được liệt kê để rà soát, thay vì gán một `canonical_combination_id` tùy đoán.

### Các trường hợp cần quyết định

- `M01`, `M13`, `N05`, `N04`, `H09`, `H10`, `H11`, `H06`, `M05`: có thành phần “Năng khiếu” chung, không rõ bài thi nào.
- `H10`, `H11`, `H06`: “Ngoại ngữ” chung, không rõ là Anh, Pháp, Đức... (đồng thời có “Năng khiếu” chung).
- `M10`: `NK1` không có diễn giải.
- `D73`, `D96`, `M05`, `Q00`: “Khoa học tự nhiên/xã hội/Khoa học” không phải tên môn đơn lẻ trong `subjects`.
- Một dòng nguồn có cột mã là `(Ngữ văn, Vật lí, Tiếng Hàn)` thay vì mã tổ hợp; trang nguồn hiện tại cũng ghi như vậy (dòng 156 của https://diemthi.tuyensinh247.com/to-hop-mon.html), nên không tự tạo mã từ dữ liệu bị thiếu.

Các mã ở trên có thể lặp vì một dòng có nhiều thành phần chung. Hai dòng gốc K01/Q00 có dấu `/` được giữ riêng để truy vết và không nhận một canonical ID duy nhất; các biến thể của chúng được xử lý độc lập.


### Bổ sung theo yêu cầu ngày 2026-10-11

- [x] 7. Tự đặt mã cho 13 nhãn thành phần chung và một dòng mã nguồn không hợp lệ. **Report:** Dùng FOREIGN_LANGUAGE_UNSPECIFIED, APTITUDE_CATALOGUE_<hash> theo mã nguồn/tên, và nhãn nội bộ SRC107B55D; không suy ra môn hoặc mã công bố không có trong nguồn.
- [x] 8. Phân tích ảnh hưởng tới toàn bộ bảng admission bằng skill schema và crawler. **Report:** Chỉ bốn bảng danh mục/nhãn/component/subject cần thêm hoặc cập nhật bản ghi. admission_offers, offer_combinations, majors, cutoff_scores không đủ dữ liệu để thay đổi, giữ 53/316/54/52 bản ghi. Không cần migration Flyway cho thao tác hiện tại; xem docs/REPORT.md.
- [x] 9. Chạy test, dry run, ghi PostgreSQL, đồng bộ và kiểm tra Neo4j. **Report:** 39 test đạt; PostgreSQL hiện có 300 nhãn, 297 gắn canonical, 3 dòng gốc null, 257 tổ hợp, 772 component và 28 subject. Dry run lặp lại: 0 tổ hợp/subject mới. Neo4j khớp projection, 316 ACCEPTS_COMBINATION, không orphan.
- [x] 10. Ghi báo cáo ngữ nghĩa mã nội bộ, phạm vi thay đổi bảng và giới hạn hiện tại. **Report:** docs/REPORT.md và phần bổ sung trong tài liệu vận hành. Chỉ data-scrawler có thay đổi mã/tài liệu trong đợt này.
# OCR worker 9Router connectivity

- [x] Configure Docker `pdf-worker` to reach the host 9Router at `host.docker.internal:20128`; keep `localhost:20128` valid for direct Windows execution from `.env`.
- [x] Add unit coverage for environment profile fallback and for an environment API key combined with an active admin endpoint/model override.
- [x] Document the host-versus-container endpoint distinction in `docs/pdf_worker.md`.
- [x] Recreate the worker and confirm it reaches the configured 9Router `/v1/models` endpoint (HTTP 200).
- [x] Recreate the worker with the text OCR pipeline and retry the existing failed extraction job; verify the resulting review draft against the source image.

## OCR ảnh thành văn bản trước khi gọi 9Router — 2026-10-13

- [x] Chuẩn hóa ảnh theo EXIF và chạy Tesseract với `vie+eng`, tự phân tích bố cục (`--psm 3`) và giữ khoảng cách cột; gửi OCR text cho Claude qua request chat completions dạng JSON.
- [x] Dùng prompt tiếng Việt chỉ lấy cột “Tổ hợp môn lựa chọn”/“Nhóm môn lựa chọn”, bỏ qua “Chuyên đề học tập” và các cột khác; yêu cầu đúng bốn môn có trong danh mục và không tự đoán.
- [x] Phân biệt `OCR_EMPTY`, `OCR_ENGINE_ERROR`, `OCR_TIMEOUT`; không gọi LLM khi OCR rỗng; giữ validation JSON, mã môn và số trang trước khi tạo draft.
- [x] Rebuild/recreate worker rồi retry tài liệu mẫu; job `SUCCEEDED`, document `DRAFT_READY`, lưu ba nhóm `01`/`02`/`03` khớp môn lựa chọn trong nguồn.

## Công bố nhóm môn và đồng bộ Neo4j tự động — 2026-10-14

- [x] Cập nhật PDF/image extraction contract sang 1–4 mã môn khác nhau; worker chỉ ghi draft vào `admin` và dùng bảng mới `admin.extraction_log`.
- [x] Thêm `graph_projection_worker.py`: claim outbox bằng `SKIP LOCKED`, đọc các nhóm đã công bố từ PostgreSQL, MERGE vào Neo4j, retry exponential backoff và không lưu thông tin lỗi nội bộ.
- [x] Thêm Dockerfile/service Compose riêng; ép worker dùng datasource đã cấu hình cho container thay vì `DATABASE_URL` local từ `.env`.
- [x] Bổ sung unit test cho nhóm 1–4 môn, dữ liệu rỗng/sai số lượng, Neo4j projection thành công và retry khi lỗi.
- [x] Cập nhật `docs/pdf_worker.md` và `docs/graph_sync.md`; `python -m unittest discover -s tests`: 63 test đạt.

Worker projection cần backend áp dụng Flyway V8 và database/Neo4j đang hoạt động. Trong phiên này chỉ chạy test offline và xác thực cú pháp Compose; chưa khởi động worker hoặc ghi dữ liệu thật lên Neo4j.

## Ràng buộc tổ hợp chatbot với nhóm môn THPT — 2026-10-14

- [x] Tách truy vấn nhóm trường khỏi truy vấn admission; không còn nối `Subject` của tổ hợp với nhóm THPT qua một cạnh môn bất kỳ.
- [x] Chỉ giữ tổ hợp khi mọi môn yêu cầu đều có trong nhóm mới nhất của trường hoặc là môn bắt buộc; dùng mã canonical khi có, không xác nhận tổ hợp cần thi năng khiếu chỉ bằng nhóm môn THPT.
- [x] Giữ `ACCEPTS_COMBINATION` làm điều kiện offer thực; không coi nhóm THPT là dữ liệu admission hay kết luận đủ điều kiện trúng tuyển.
- [x] Bổ sung test trường hợp chỉ trùng một môn bị loại, môn bắt buộc được tính vào coverage, và chỉ xét năm học mới nhất.
- [x] Kiểm tra: `python -m unittest discover -s tests` (65 test đạt); `EXPLAIN` thành công cho truy vấn admission, nhóm trường và môn bắt buộc trên Neo4j.
- [x] Rebuild/recreate service `rag-api`; `/health` báo Neo4j `connected`. Không ghi hay đồng bộ thêm dữ liệu graph trong bước này.

## Đồng bộ CRUD trường THPT và cấu hình model qua environment — 2026-10-16

- [x] Mở rộng `graph_projection_worker.py` để ưu tiên outbox `admin.high_school_graph_sync_jobs`, đồng bộ trường/nhóm môn qua mọi năm học và xóa node trường/nhóm khi đổi mã hoặc xóa mềm.
- [x] Lọc trường/nhóm đã xóa trong projection nhóm incremental và trong full PostgreSQL reader; xử lý job nhóm cũ như no-op khi trường/nhóm không còn hoạt động.
- [x] Giữ truy vấn Neo4j tham số hóa, retry/backoff và mã lỗi an toàn. Cập nhật docs về schema V10 và xóa graph bất đồng bộ.
- [x] Gỡ trạng thái cấu hình LLM qua endpoint nội bộ; RAG API và extraction worker dùng `LLM_API_KEY`, `LLM_API_BASE`, `LLM_MODEL` từ `.env`.
- [x] Kiểm thử: `python -m unittest discover -s tests` (69 test đạt); test bổ sung trường có nhiều năm, xóa node có tham số và đổi mã trường.
- [ ] Chưa chạy worker với PostgreSQL/Neo4j thật; cần áp dụng backend migration V10 và rebuild/recreate `graph-projection-worker` sau khi cập nhật container.

## S3-compatible storage for document extraction - 2026-10-16

- [x] Point the PDF/image worker at shared `OBJECT_STORAGE_*` settings for MinIO or R2, use path-style S3 addressing, and remove fallback to legacy `R2_*` variables. OCR, LLM prompting, validation and draft writes are unchanged.
- [x] Report missing objects as terminal `STORAGE_OBJECT_MISSING` jobs and add an idempotent bucket initializer for the Docker MinIO profile.
- [x] Verification: focused extraction/storage tests 24/24; full `python -m unittest discover -s tests` passed 74 tests.
- [ ] No worker container was started. OCR against a real MinIO object and end-to-end draft creation remain to be smoke-tested after services are started.

## Xóa nhóm môn đã ngừng công bố khỏi Neo4j và dựng lại khi restore — 2026-10-17

- [x] Full reader và incremental worker tách nhóm đang công bố khỏi danh sách tombstone lấy từ `admin.subject_group_publication_state`; nhóm hết nguồn công bố không còn được gửi vào tập node/cạnh cần tạo.
- [x] Neo4j writer xóa node bằng `DETACH DELETE` theo UID, đồng thời kiểm tra trường/năm/mã nhóm và revision. Ghi nhóm môn trong transaction; các node `HighSchool`/`Subject` dùng chung vẫn còn.
- [x] Worker tài liệu, worker trường và full-sync dùng chung PostgreSQL advisory lock, đọc trạng thái nguồn mới nhất trước khi ghi để tránh job cũ dựng lại node vừa xóa.
- [x] Full-sync dry-run báo số nhóm sẽ xóa; kiểm tra graph cảnh báo nếu node ngừng công bố còn sót.
- [x] Kiểm tra: `python -m unittest discover -s tests` — 84 test đạt; `compileall` và `git diff --check` đạt.
- [ ] Chưa chạy full-sync hoặc worker trên PostgreSQL/Neo4j thật. Sau khi build lại worker, cần `--dry-run`, chạy full-sync để dọn node cũ, rồi kiểm tra query Neo4j trước/sau restore.
# Khôi phục trường và đồng bộ graph theo trạng thái PostgreSQL — 2026-10-16

- [x] Worker khóa bản ghi trường trong khi áp dụng Neo4j projection và quyết định UPSERT/cleanup theo trạng thái hiện tại, xử lý an toàn job DELETE cũ sau khi restore.
- [x] Không báo SYNCED cho document projection thiếu nhóm dự kiến ở một trường còn hoạt động; ghi log terminal cho school jobs và giữ retry/backoff.
- [x] Kiểm thử: `python -m unittest discover -s tests` (78 test đạt).
- [ ] Chưa chạy worker với PostgreSQL/Neo4j thật; migration backend V12 cần được áp dụng trước khi cập nhật service.

## Đồng bộ trạng thái công bố tài liệu lên Neo4j — 2026-10-17

> Trạng thái tại thời điểm ghi report này đã được thay thế: bản cập nhật cùng ngày bên dưới xóa node `SubjectGroup` và các cạnh của nhóm đã ngừng công bố; restore dựng lại node từ PostgreSQL.

- [x] Projection reader/worker truyền trạng thái công bố và revision trên quan hệ trường–nhóm môn.
- [x] Neo4j writer chỉ áp dụng revision mới nhất: ngừng công bố thì xóa cạnh `OFFERS_GROUP`; khôi phục thì tạo lại cạnh. Node trường/nhóm và cạnh `INCLUDES_SUBJECT` được giữ lại.
- [x] Bỏ nhóm đã ngừng công bố khỏi chẩn đoán orphan; full sync lấy trạng thái hiện tại từ PostgreSQL để hội tụ lại graph.
- [x] Kiểm thử: `python -m unittest discover -s tests` (80 test đạt).
- [ ] Chưa xác minh truy vấn trên Neo4j thật; cần chạy worker sau khi các service và migration lifecycle đã sẵn sàng.

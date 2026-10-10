# Report: cào danh mục tổ hợp môn

Ngày triển khai: 2026-10-10. Nguồn: https://diemthi.tuyensinh247.com/to-hop-mon.html

## Thay đổi

- `crawl_combination_labels.py` chỉ đọc hai cột **Tổ hợp** và **Môn chi tiết**. Ô nhiều mã được tách thành các dòng riêng. Output gồm CSV chỉ có hai trường dữ liệu và manifest ghi URL, niên khóa, thời điểm, SHA-256, số dòng.
- `import_combination_labels.py` xác thực snapshot và schema Flyway V3, rồi upsert vào PostgreSQL. Chỉ nối `canonical_combination_id` khi ba môn phổ thông khớp duy nhất. Dòng xung đột, môn năng khiếu, thiếu mapping hoặc không có tổ hợp chuẩn vẫn được lưu với liên kết null.
- Importer tự nạp `data-scrawler/.env`; biến `DATABASE_URL` đã export trong terminal vẫn được ưu tiên. Vì vậy khi đang ở thư mục `data-scrawler`, có thể chạy trực tiếp mà không cần tự export biến môi trường.
- Bộ chuẩn hóa môn bổ sung alias `GDKTPL` và `Giáo dục kinh tế và pháp luật` để liên kết đúng các tổ hợp X tương ứng.
- Không đồng bộ Neo4j vì nhãn chỉ phục vụ hiển thị, không thay đổi quan hệ hay đề xuất.

## Snapshot và cách chạy

Snapshot 2026-10-10: 261 dòng nguồn, 293 dòng sau tách mã; 11 mã có mô tả xung đột.

```powershell
python crawl_combination_labels.py --dry-run
python crawl_combination_labels.py
python import_combination_labels.py --dry-run
# Sau khi backend áp dụng Flyway V3 và cấu hình DATABASE_URL an toàn:
python import_combination_labels.py
```

Importer không tạo bảng, không xóa dữ liệu và từ chối snapshot cũ hơn lần nhập gần nhất. PostgreSQL chưa được ghi trong phiên này: kết nối local `127.0.0.1:5432` bị từ chối xác thực với thông tin trong `.env`.

## Kiểm tra

`python -m unittest discover -s tests` và `python import_combination_labels.py --dry-run` đã chạy thành công ngày 2026-10-10.

# Job giảm giá tùy chọn

Mỗi page trong `config.json` có `discount_followup`. Hiện `enabled: false`, chưa có
giá và chưa gửi tin giảm giá. Không thay đổi giá gốc trong `sales-script.json`.

```json
"discount_followup": {
  "enabled": false,
  "incomplete_after_seconds": 7200,
  "hesitation_after_seconds": 300,
  "max_customer_age_seconds": 82800,
  "discount_price": "",
  "hesitation_keywords": ["chưa muốn lấy", "chưa mua", "để suy nghĩ", "để chị suy nghĩ", "giá cao", "đắt quá", "mắc quá"],
  "hesitation_reply": "Dạ chị cứ cân nhắc thêm nhé, khi cần chị nhắn shop hỗ trợ ạ.",
  "message": "Chị {name} ơi, shop có giá ưu đãi {discount_price} cho mẫu chị đang quan tâm. Nếu chị muốn lấy, chị gửi thêm {missing_fields} để shop tư vấn tiếp nhé ạ."
}
```

## Bật và chỉnh

1. Điền `discount_price`, ví dụ nội dung giá do bạn quyết định, có thể kèm đơn vị/bộ.
2. Sửa `message` theo ưu đãi thực tế, ví dụ phí ship/điều kiện nếu có.
3. Đổi `enabled` thành `true`, giữ `sales_script` của page.
4. Khởi động lại `python bot.py poll` để thử. Bật gửi thật bằng `python bot.py poll --live`.

Các biến trong message: `{name}`, `{discount_price}`, `{missing_fields}`. Giá được
lưu dạng chữ để bạn tự ghi rõ điều kiện; tool không tính tổng đơn, không tự thay giá
báo giá gốc ở những lần khách hỏi sau. Bật khi chưa nhập giá sẽ báo lỗi cấu hình.

## Hai điều kiện

- **Chưa đủ thông tin**: sau 7200 giây tính từ tin nhắn khách gần nhất đã được bot xử
  lý thành công, nếu thiếu size/SĐT/địa chỉ thì gửi ưu đãi. Khi khách trả lời thêm mà
  vẫn thiếu, mốc 2 giờ bắt đầu lại từ tin đó. Đã đủ ba trường thì không giảm giá tự động,
  kể cả khách chưa xác nhận tổng hợp.
- **Đang cân nhắc**: khớp một cụm trong `hesitation_keywords` (không phân biệt dấu/hoa
  thường), bot gửi `hesitation_reply`, sau 300 giây gửi ưu đãi nếu vẫn đủ điều kiện.
  Đặt `hesitation_after_seconds: 0` để gửi ở lượt polling tiếp theo.

Một hội thoại chỉ có **một lần gửi ưu đãi** trong DB hiện tại. Không lặp lại ưu đãi
khi khách tiếp tục im lặng. Tin hoàn tất, opt-out hoặc yêu cầu nhân viên có ưu tiên
cao hơn. “Dừng nhắn”, “không mua”, “không cần”, “hủy đơn” vẫn dừng bot; không phải
từ khóa để gửi giảm giá. Comment không được lên lịch giảm giá.

Lịch chỉ tạo từ tin mới được xử lý khi tính năng đang bật, không truy ngược khách cũ.
Tool dùng SQLite `followups`, không dùng Windows Task Scheduler hay dịch vụ ngoài;
tiến trình `poll` phải chạy. Lịch được giữ sau khởi động lại. Máy tắt quá lâu sẽ không
gửi ngay mọi lịch cũ: `max_customer_age_seconds` mặc định giới hạn 23 giờ kể từ tin
khách. Đây là giới hạn của tool; API vẫn có thể từ chối gửi theo quyền/trạng thái nền tảng.
Chế độ webhook `serve` không chạy scheduler này.

Trước khi gửi tool đọc lại conversation/messages: bỏ qua khi có tin khách mới chưa
xử lý, page/nhân viên đã trả lời sau lúc lên lịch, bị chặn gửi, tag dừng, hoặc đã gán
nhân viên khi `skip_assigned` bật. Tin mới ngoài cửa sổ polling có thể khiến lịch bị
hủy thay vì gửi ưu đãi sai thời điểm. Hai lần đọc/gửi không phải giao dịch nguyên tử;
vẫn có thể có tin mới tới ngay giữa lần kiểm tra và lúc gửi.

## Theo dõi

```powershell
python bot.py followups
python bot.py followups --live
```

`due` là Unix timestamp; `reason` là `incomplete` hoặc `hesitation`.
`pending`: đang chờ; `sent`: đã gửi; `dry_run`: đã mô phỏng; `cancelled`: không còn
phù hợp; `expired`: quá tuổi tin; `failed`/`unknown`: cần xem log, không tự gửi lại.
Thông tin nội dung dự kiến/đã gửi nằm trong `result`; log có `pancake.discounts`.
Dry-run và live dùng DB riêng. `reset-state` không xóa lịch sử ưu đãi, nên không làm
khách được gửi nhiều lần. Giá và nội dung dùng theo cấu hình được tải lúc khởi động;
sửa file cần khởi động lại, kể cả với lịch đang chờ.

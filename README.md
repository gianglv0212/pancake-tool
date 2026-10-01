# Pancake Auto Reply — Python

Inbox live với `llm.enabled: true` dùng bộ xử lý hội thoại có ngữ cảnh trong
`dialogue.py`: đọc lịch sử, hiểu ý khách và viết lời tư vấn/hỏi lại tự nhiên.
Code kiểm tra dữ liệu, xác nhận đơn và mua lại; cấu hình và giới hạn nằm trong
[SALES-FLOW.md](SALES-FLOW.md). Khi tắt LLM hoặc chạy dry-run, bot dùng quy tắc cũ.

Job giảm giá tùy chọn: [DISCOUNT-FOLLOWUP.md](DISCOUNT-FOLLOWUP.md).
Sửa `discount_followup` của từng page trong `config.json` để bật/tắt, đặt giá và nội dung.

## Log để debug lỗi ảnh/API

Khởi động lại tool để áp dụng log mới. Mặc định log vừa hiện trên terminal vừa lưu vào
`logs/pancake.log` (UTF-8, xoay vòng 5 MB, giữ 5 bản cũ).

```powershell
python bot.py poll --live --log-level DEBUG
```

Đọc log đang chạy từ cửa sổ PowerShell khác:

```powershell
Get-Content .\logs\pancake.log -Tail 100 -Wait
```

Tìm `job=...`, `step=...`, `photo=...` để theo dõi chuỗi. Các mốc gồm `download_start`,
`download_ok` (MIME, số byte, thời gian), `upload_start`, `upload_response`, `media_ready`,
`send_start`, `send_response`. Lỗi HTTP ghi mã và tối đa 6000 ký tự response; lỗi mạng/
Python có traceback. Log gửi Pancake chỉ ghi độ dài và content IDs của request.
Riêng log chẩn đoán LLM ghi plain text context/request, response, kết quả kiểm tra
và câu bot định trả lời ở mức INFO để đối chiếu hội thoại; có thể chứa SĐT/địa chỉ.
Xem mục “Log plain text để đối chiếu hội thoại” trong [SALES-FLOW.md](SALES-FLOW.md).
Response API lỗi có thể chứa dữ liệu do API trả về; token đã được che cả trong traceback.
`--log-file logs/custom.log` đổi đường dẫn. Thư mục `logs/` được bỏ qua bởi Git.

Lỗi cũ không thể khôi phục response nếu phiên bản trước chưa ghi log. Lỗi kỹ thuật dùng
trạng thái `error`: dừng job hiện tại, không tự gửi lại; tin khách mới có ID khác vẫn
được xử lý, dù nội dung giống nhau. `human` chỉ dành cho dừng để nhân viên hỗ trợ.
Khi khởi động, tool tự chuyển trạng thái human do job lỗi của phiên bản cũ sang error;
không chạy lại job đã ignored/sent/unknown. Tin “xin giá” mới sẽ gửi lại nhóm báo giá,
nên phần chữ từng gửi thành công trước lỗi ảnh có thể xuất hiện lại theo yêu cầu mới.
Không cần bật live để kiểm tra format log, nhưng dry-run sẽ
không thực hiện download/upload/send nên không tái hiện lỗi mạng ảnh.

Kịch bản bán hàng từ `reply-quicks.md` đã cấu hình: xem [SALES-FLOW.md](SALES-FLOW.md)
để biết thứ tự báo giá/ảnh, nhận size–SĐT–địa chỉ và lệnh `python bot.py leads`.

Tool đọc API định kỳ (polling) của nhiều Facebook page và trả lời bằng kịch bản JSON.
Python 3.11 trở lên, không cần cài thư viện bên ngoài. Chưa kết nối tài khoản thật.

## Chạy API polling — không cần webhook

Giữ nguyên `config.json` đang có. Nếu chưa có, sao chép `config.example.json` rồi thay
ID page. Lấy Page Access Token ở Pancake → page → Cài đặt → Công cụ. Dán token vào
`page_access_token` của từng page trong `config.json`, chỉ cần lưu một lần:

```json
"page_access_token": "TOKEN_THAT_CUA_PAGE"
```

Sau đó chạy:

```powershell
python bot.py poll
```

Lệnh này đọc API thật, chỉ mô phỏng phản hồi. Xem kết quả bằng `python bot.py status`.
Sau khi sửa kịch bản đúng nhu cầu, gửi thật bằng:

```powershell
python bot.py poll --live
```

Dừng bằng Ctrl+C. Không cần webhook secret, tên miền hoặc mở cổng. Máy cần luôn bật,
có mạng. Dừng tiến trình `serve` cũ nếu có; chỉ chạy một bot trên cùng DB.

Token trong config được ưu tiên. Nếu để trống, tool vẫn hỗ trợ lấy từ biến môi trường
được chỉ định bởi `token_env`. Khi dùng token trong config có thể bỏ `token_env`.
`config.json` đã được bỏ qua trong Git; giữ riêng file này vì có chứa token.

- Nghỉ mặc định 10 giây sau mỗi lượt quét. Thêm `"poll_interval_seconds": 5` vào cấp
  ngoài cùng config để chỉnh. Độ trễ còn phụ thuộc số page và lượng hội thoại.
- Lần đầu mỗi page ghi mốc thời gian vào SQLite, bỏ qua tin trước mốc. Dry-run/live
  dùng DB riêng: lần đầu chạy live tạo mốc mới. Không xóa DB nếu cần giữ mốc.
- Quét lại cửa sổ `max_event_age_seconds` (600 giây mặc định), phân trang và chống
  trùng bằng ID. Sau khởi động lại chỉ lấy bù tin còn trong cửa sổ; tin quá cũ bị bỏ qua.
- Tin trong từng hội thoại được xử lý từ cũ đến mới, không phụ thuộc trạng thái unread.
- GET lỗi mạng/429/5xx thử tối đa 3 lần; page lỗi không chặn page tiếp theo. GET và POST
  dùng chung giới hạn khoảng 4 request/giây/page. POST lỗi không tự gửi lại.
- `python bot.py poll --once` chạy một lượt đọc API. `python bot.py status --live`
  hiển thị kết quả gửi thật. Token cần cả khi chạy thử để đọc dữ liệu.
- Chưa thử tài khoản thật. Phân trang messages dùng `current_count` là vị trí trước
  batch vừa đọc, dựa trên `message_count` và đặc tả Pancake. Nếu API trả dữ liệu khác
  đặc tả, cần kiểm tra phản hồi thực tế. Hội thoại nhiều người/livestream chưa hỗ trợ.

Phần webhook bên dưới là tùy chọn, không cần thực hiện khi dùng polling.

## Webhook tùy chọn trên Windows

```powershell
Copy-Item config.example.json config.json
python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:PANCAKE_WEBHOOK_SECRET = 'DAN_CHUOI_NGAU_NHIEN_VUA_TAO'
python bot.py serve
```

Sửa `config.json`: thay `YOUR_FACEBOOK_PAGE_ID` bằng ID page thật. Lấy Page Access Token
tại Pancake → page → Cài đặt → Công cụ. Đây là token Pancake của từng page.
Thêm các page khác vào `pages`, mỗi page có `token_env` và `rules` riêng.
Biến môi trường chỉ tồn tại trong phiên PowerShell hiện tại; cấu hình lại khi khởi động lại.

Chạy thử mặc định không gửi API. Xem quyết định trong SQLite:

```powershell
python bot.py status
```

Để nhận webhook thật, triển khai tiến trình trên máy chủ luôn hoạt động, đặt reverse proxy
HTTPS trước `127.0.0.1:8000`. Cấu hình URL trong Pancake → page → Công cụ → Webhook:

`https://TEN-MIEN-CUA-BAN/webhooks/pancake/CHUOI_BI_MAT`

Chọn sự kiện `messaging` nếu giao diện cho phép chọn. Endpoint nhận POST và trả 200 sau
khi lưu hàng đợi; `GET /health` dùng kiểm tra máy chủ. Tài liệu không mô tả giao thức
challenge GET hay chữ ký webhook; tool không tự giả định cơ chế đó. URL bí mật là lớp
kiểm soát truy cập của tool, không phải xác thực chữ ký Pancake. Không ghi URL callback
vào access log của proxy. Chỉ mở HTTPS qua proxy, giới hạn kích thước/rate request.
Nếu thao tác Verify của tài khoản yêu cầu giao thức khác, cần đối chiếu payload thực tế.

Mỗi page bật webhook dùng thêm 1 connection slot theo tài liệu Pancake. Cấu hình webhook
là bước người quản trị thực hiện; tool chưa tự đăng ký webhook hay triển khai hosting.

Sau khi xem và sửa kịch bản, bật gửi thật:

```powershell
$env:PANCAKE_PAGE_1_TOKEN = 'PAGE_ACCESS_TOKEN_CUA_BAN'
python bot.py serve --live
python bot.py status --live
```

Dry-run và live dùng hai DB riêng để kết quả thử không chặn sự kiện thật. Chỉ chạy **một
tiến trình serve cho mỗi DB**. Dừng bản thử trước khi chạy bản thật. Không gửi token lên
chat hoặc đưa vào Git. `config.json`, DB và `.env` được bỏ qua bởi Git; tool không tự đọc `.env`.

## Viết kịch bản

Rules chạy từ trên xuống, chọn rule khớp đầu tiên. `keywords` khớp chuỗi con, không phân
biệt hoa/thường và dấu tiếng Việt; đây không phải phân loại bằng AI. `keywords: []` khớp
mọi nội dung, nên đặt sau các rule cụ thể. Không có rule khớp thì không trả lời.

| Trường | Ý nghĩa |
| --- | --- |
| `id` | Tên duy nhất trong page |
| `channels` | `INBOX`, `COMMENT`; mặc định cả hai |
| `state` | Trạng thái cần khớp; mặc định `*` |
| `keywords` | Chỉ cần khớp một từ/cụm |
| `reply` | Nội dung, hỗ trợ `{name}` |
| `next_state` | Trạng thái tiếp theo của conversation |
| `action` | `reply`, `private_reply`, `handoff` |

Ví dụ nhắn riêng từ bình luận (thêm vào rules trước rule trả lời bình luận):

```json
{"id":"comment-to-inbox","channels":["COMMENT"],"keywords":["tư vấn"],"action":"private_reply","reply":"Chào {name}, bạn muốn tìm hiểu sản phẩm nào?"}
```

Nhắn riêng cần `can_reply_privately` và post ID. Trả lời bình luận cần `can_comment`.
`handoff` hoặc `next_state: "human"` dừng bot ở conversation đó; **không tự gán nhân viên
hoặc gửi thông báo**. Nhân viên tiếp tục xử lý trong Pancake. Cho phép bot hoạt động lại:

```powershell
python bot.py reset-state --live --page PAGE_ID --conversation CONVERSATION_ID
```

Mặc định bỏ qua conversation đã được gán nhân viên (`skip_assigned: true`); đặt false
nếu page tự động gán tất cả conversation nhưng vẫn muốn bot trả lời. `stop_tags` nhận
danh sách ID tag dạng số để loại trừ. Trạng thái riêng theo page/conversation, không nối
tự động từ comment sang conversation inbox mới.

## Độ tin cậy và giới hạn

- SQLite lưu sự kiện trước khi trả 200, chống trùng theo page/conversation/message ID.
- Chỉ xử lý người gửi trùng với khách của conversation; bỏ qua page gửi, message bị xóa,
  có lịch sử sửa, thiếu ngày tạo hoặc quá 600 giây. Có thể chỉnh `max_event_age_seconds`.
  Conversation nhiều người chưa hỗ trợ đầy đủ. Sự kiện đến trễ/sai thứ tự có thể không
  khớp bước; bản này xử lý theo thứ tự nhận, chưa sắp xếp lại lịch sử.
- Worker cách nhau tối thiểu 0,25 giây giữa các job. Các ứng dụng khác dùng cùng page
  cũng có thể ảnh hưởng hạn mức; HTTP 429 được lưu `failed` để kiểm tra.
- Khi timeout, HTTP 5xx hoặc tiến trình dừng giữa lúc gửi: đánh dấu `unknown`, không tự
  gửi lại vì có thể Pancake đã gửi thành công. Kiểm tra hội thoại thật trước xử lý thủ công.
  Không đảm bảo exactly-once giữa SQLite và API bên ngoài. Bản này không có tự retry.
- Sự kiện cập nhật cùng ID không kích hoạt trả lời lần hai. Không đọc lại lịch sử lúc
  khởi động; chỉ nhận webhook mới. Sự kiện bị bỏ qua không có nhật ký riêng.
- DB lưu nội dung khách và câu trả lời: giới hạn quyền đọc, backup và chủ động đặt chính
  sách lưu trữ. Không có giao diện quản trị hay tự dọn DB ở bản này.
- Nội dung mẫu chưa phải kịch bản bán hàng của bạn. Không tự thêm giá hoặc cam kết giao hàng.

## Kiểm thử

```powershell
python -m unittest discover -s tests -v
```

Kiểm thử bằng API giả lập, gồm chống echo/trùng, phân page, nội dung tiếng Việt, chuyển
bước, handoff, private reply và cấu trúc request. Chưa kiểm chứng gửi trên page thật.
Có thể mô phỏng webhook JSON theo mẫu trong `docs/webhook.yaml`:

```powershell
python bot.py simulate --event event.json --config config.json
```

Đổi `inserted_at` trong mẫu thành UTC hiện tại để không bị bộ lọc sự kiện cũ bỏ qua.

## Tài liệu đã đối chiếu

- https://developer.pancake.biz/openapi/openapi.yaml
- https://developer.pancake.biz/openapi/webhook.yaml

Bản tải lưu trong `docs/`, ngày 28/09/2026. Gửi text bằng POST
`https://pages.fm/api/public_api/v1/pages/{page_id}/conversations/{conversation_id}/messages`,
query `page_access_token`; các action lần lượt `reply_inbox`, `reply_comment`,
`private_replies`. Tool không sinh lại token để tránh làm mất hiệu lực token hiện có.

# LLM hỗ trợ từng bước bán hàng

Luồng bán hàng vẫn nằm trong `sales.py` và `reorders.py`. LLM chỉ hỗ trợ khi parser
chưa giải quyết được bước đang chờ; không chọn FAQ, không tự xây luồng hội thoại mới.

## Cách xử lý

1. Parser chạy trước. Đọc được dữ liệu hợp lệ cho bước đang chờ thì lưu và tiếp tục
   kịch bản, không gọi LLM. Địa chỉ độc lập rõ ràng cũng được xử lý miễn phí bằng code.
2. Nếu cần hỗ trợ, LLM nhận bước hiện tại, các trường được phép trích, dữ liệu đã có,
   câu bot đã gửi và tối đa ba cặp lời thoại gần nhất của cùng page/hội thoại/phiên.
   Không dùng lời bot gửi thất bại làm bằng chứng đã hỏi/xác nhận.
3. Model đề xuất dữ liệu và bằng chứng, hoặc một câu hỏi làm rõ đúng bước đó.
4. `step_support.py` kiểm tra đúng trường và cấu trúc đầu ra. Riêng địa chỉ,
   code lưu từ nguyên văn khách gửi; không bắt LLM chia hoặc xác minh thôn/xã/tỉnh.
5. Dữ liệu hợp lệ được đưa về `sales.decide()` để tiếp tục kịch bản. Nếu chưa rõ,
   câu hỏi do model viết **thay thế** câu hỏi mẫu của bước đó, không gửi cả hai.

Parser thường vẫn có thể lưu thông tin khách cung cấp sớm ở bước khác. Phần LLM
chỉ được cập nhật trường trong phạm vi của bước hiện tại:

| Bước | Phạm vi LLM |
| --- | --- |
| `size`, `size_confirm` | Size, cân nặng, chiều cao. Có cân nặng trong bảng là đủ để parser chọn size và chuyển bước; không bắt buộc chiều cao. |
| `color` | Một hoặc nhiều màu có trong cấu hình sản phẩm, nối bằng dấu phẩy và khoảng trắng. |
| `phone` | Một số di động Việt Nam đầy đủ, không tự thêm chữ số. |
| `address` | Hiểu khách đang cung cấp địa chỉ hay cần hỏi thêm; chọn nguyên văn để lưu. |
| `confirm` | Hiểu sự đồng ý với bản tổng hợp đã gửi, hoặc hỏi lại cho rõ. |
| `contact_reuse_confirm`, `reorder_confirm` | Hiểu sự đồng ý với đề nghị dùng lại liên hệ/mở phiên mới đã gửi. |

Các trạng thái hoàn tất, dừng và nhân viên phụ trách tiếp tục được bảo vệ bằng code.
LLM không tự tạo/hủy đơn trên Pancake, đổi giá/chính sách hoặc gán nhân viên.

## Địa chỉ nhiều lượt

Ví dụ:

- Khách: “La Thạch”.
- Bot: “La Thạch thuộc xã/phường và tỉnh/thành nào chị nhỉ?”
- Khách: “Liên Minh Hà Nội”.
- Code có thể nhận địa chỉ “La Thạch, Liên Minh Hà Nội”, rồi gửi tổng hợp đơn.

Không còn `address_parts` trong kết quả LLM. Model chỉ xác định ý nghĩa câu trả lời
và liệu có cần hỏi thêm. Ví dụ “xóm đoàn kết, la thạch, liên minh, hà nội” được lưu
nguyên văn khi LLM nhận đây là địa chỉ đủ rõ; không yêu cầu phân loại từng địa danh.

Khi cần hỏi thêm, tin khách được giữ dưới dạng nguyên văn trong
`pending_address_texts`. Code tạo `address_options` từ tin hiện tại và các cách
nối với lời bổ sung trước đó. LLM chọn phương án phù hợp: dùng riêng tin mới khi
khách sửa/gửi lại toàn bộ, hoặc ghép khi khách đang bổ sung. Code không thêm địa
danh và không ghi đè cách viết của khách. Dữ liệu tạm từ phiên bản cũ vẫn được
đọc để tránh mất phần đã cung cấp. Không xác minh địa chỉ thực tế bằng bản đồ.

## Xác nhận

Quyết định có đề nghị xác nhận lưu `confirmation_snapshot`. Chỉ dùng phản hồi đã
gửi thành công làm ngữ cảnh; snapshot phải còn khớp dữ liệu phiên hiện tại.
“Ừ em” có thể được hiểu là đồng ý. Câu phủ định, câu hỏi hoặc đồng ý kèm sửa không
được dùng để tự hoàn tất đơn. Câu hỏi làm rõ của LLM không tự tạo một bản tổng hợp
mới; khi không có đề nghị hợp lệ, bot tiếp tục yêu cầu làm rõ bằng kịch bản.

## Lượt sửa và ngân sách

Mỗi tin có một lượt gọi thông thường. Nếu validator bác kết quả, có thể gọi thêm
**tối đa một lượt**, chỉ để viết câu hỏi làm rõ theo lỗi; lượt sửa không được lưu
dữ liệu hay xác nhận đơn. Cả hai lượt đều phải giữ chỗ ngân sách và tính vào
`max_calls_per_conversation`. Lượt sửa có hậu tố `:llm-clarification-repair` trong
ID kế toán tại bảng `llm_calls`, không phải một tin gửi Pancake.

Không tự gọi lại khi timeout/lỗi mạng, API từ chối hoặc phản hồi chưa hoàn tất.
Thiếu key/hết ngân sách/lỗi kỹ thuật dùng câu dự phòng của bước hiện tại; không
tính thêm một lần khách trả lời không rõ. Một lượt lỗi vẫn có thể bị tính phí;
thiếu usage thì giữ nguyên khoản chi phí dự phòng như trước.

Model vẫn là nano, dùng reasoning `low`, đầu ra JSON strict, giới hạn token/input
theo config hiện tại. Không tự tăng ngân sách hoặc giới hạn token của người dùng.
Reasoning cũng thuộc số token đầu ra; giới hạn quá thấp có thể khiến phản hồi
chưa hoàn tất và quay về câu dự phòng.

## Chạy và đọc log

Giữ `llm.enabled: true` và key hiện có trong config. Dừng tiến trình cũ rồi chạy:

```powershell
python bot.py poll --live --log-level INFO
```

Các mốc log:

- `llm_skip`: không cần hoặc không thể gọi API.
- `llm_call_reason`: lý do, bước, trường được phép, dữ liệu parser và số lượt gọi.
- `llm_request` / `llm_response`: request và response.
- `llm_interpretation`: JSON model trả.
- `llm_validation`: chấp nhận dữ liệu, chấp nhận câu hỏi, hoặc bác với lý do.
- `llm_final_decision`: câu trả lời và bước cuối cùng do code chọn.
- `send_status`: kết quả gửi Pancake thực tế.

Log có thể chứa dữ liệu khách; key/token được che. Khởi động lại không xóa dữ liệu.
Dry-run không gọi API và không mô phỏng câu hỏi tự nhiên của LLM.

## Kiểm tra đã thực hiện

Bộ kiểm tra tự động gồm giới hạn bước, dữ liệu sai/ngoài phạm vi, địa chỉ nhiều lượt,
xác nhận có ràng buộc, lịch sử riêng từng khách, timeout và hạn mức lượt sửa.
Bản đơn giản hóa địa chỉ đã qua kiểm tra cục bộ; chưa chạy lại API thật cho bản
này. Công cụ thử API dùng dữ liệu giả, DB tạm và không gửi Pancake.

Khi cần kiểm tra lại API thật, lệnh tùy chọn sau có phí và ngân sách riêng tối đa
0,02 USD mỗi lần:

```powershell
python tests/smoke_steps.py --live-llm
```

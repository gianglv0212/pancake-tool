"""Explicit new-purchase consent, separate from completed order information."""
import re
from bot import normalize


def wants_reorder(text):
    n = normalize(text)
    if re.search(r'\b(khong|ko|k|chua|dung|huy)\b', n):
        return False
    return bool(re.search(r'\b(?:mua|dat|lay) (?:(?:lai|them)\b|(?:mot |1 )?don moi\b|(?:mot|1) (?:bo|cai|ao) nua\b)', n))


def old_order_request(text):
    n = normalize(text)
    return bool(re.search(r'\b(?:don (?:cu|truoc|vua|cua|chi|em|hang)|doi dia chi|doi size|doi mau|huy don|tra hang|doi tra|giao (?:den|toi) dau|bao gio (?:giao|nhan))\b', n))


def lifecycle(script, event, previous):
    """Return (handled, decision); terminal stages can only reopen through consent."""
    from sales import decide, conversation_intent, is_affirmation
    stage = previous.get('stage')
    if stage not in ('complete', 'stopped', 'reorder_confirm'):
        return False, None
    message = event['data']['message']
    if message['type'] != 'INBOX':
        return True, None
    text = message['message']
    lead = dict(previous)

    def reply(key, default, new_stage):
        lead['stage'] = new_stage
        body = {'action': 'reply_inbox', 'message': script['prompts'].get(key, default)}
        return True, {'rule': 'sales:' + new_stage, 'body': body, 'bodies': [body],
                      'lead': lead, 'next_state': 'human' if new_stage == 'human' else new_stage}

    # An old-order question never opens a new purchase, even if mixed with a reorder.
    if (stage != 'stopped' and old_order_request(text)) or conversation_intent(text) == 'human':
        lead['handoff_reason'] = 'existing_order_support'
        return reply('old_order_human', 'Shop đã ghi nhận yêu cầu về đơn trước. Chị vui lòng chờ nhân viên kiểm tra và hỗ trợ nhé ạ.', 'human')
    if stage == 'reorder_confirm':
        if is_affirmation(text):
            fresh = {'stage': 'start', 'introduced': True, 'repeat_count': 0,
                     'session_id': 'reorder:' + str(message['id'])}
            contact = {k: previous[k] for k in ('phone', 'address') if previous.get(k)}
            if contact:
                fresh['previous_contact'] = contact
            result = decide(script, event, fresh)
            body = {'action': 'reply_inbox', 'message': script['prompts'].get(
                'reorder_start', 'Dạ shop bắt đầu phiên mua mới. Chị chọn size nào (S, M, L, XL, 2XL) cho lần này ạ?')}
            result['bodies'] = [body]
            result['body'] = body
            result['new_session'] = True
            return True, result
        if conversation_intent(text) == 'stop' or re.fullmatch(r'(?:da )?(?:khong|ko|chua|thoi)(?: a| nhe| nha)?[.! ]*', normalize(text)):
            origin = lead.pop('reorder_origin', 'complete')
            lead.pop('repeat_count', None)
            return reply('reorder_cancel', 'Dạ shop chưa mở phiên mua mới. Thông tin trước đó vẫn được giữ nguyên ạ.', origin)
        lead['repeat_count'] = lead.get('repeat_count', 0) + 1
        if lead['repeat_count'] >= 3:
            lead['handoff_reason'] = 'repeated_reorder_confirmation'
            return reply('human', 'Chị vui lòng chờ nhân viên hỗ trợ nhé ạ.', 'human')
        return reply('reorder_confirm', 'Chị muốn đặt thêm một đơn mới phải không ạ? Chị nhắn “đúng rồi” để bắt đầu, hoặc “không” nếu chưa muốn mua thêm nhé.', 'reorder_confirm')
    if wants_reorder(text):
        lead['reorder_origin'] = stage
        lead['repeat_count'] = 0
        return reply('reorder_confirm', 'Chị muốn đặt thêm một đơn mới phải không ạ? Chị nhắn “đúng rồi” để bắt đầu, hoặc “không” nếu chưa muốn mua thêm nhé.', 'reorder_confirm')
    if stage == 'complete' and conversation_intent(text) == 'stop':
        return reply('stop', 'Dạ shop ghi nhận, bot sẽ dừng nhắn tại cuộc trò chuyện này ạ.', 'stopped')
    # Completed customers may browse again without modifying the previous order.
    if stage == 'complete' and re.search(r'\b(gia|bao nhieu|xin anh|xem anh|mau nay)\b', normalize(text)):
        bodies = []
        name = (message.get('from') or {}).get('name') or 'chị'
        for part in script['groups'].get('2', []):
            if 'message' in part:
                bodies.append({'action': 'reply_inbox', 'message': part['message'].replace('#{FULL_NAME}', name)})
            else:
                bodies.append({'action': 'reply_inbox', 'photos': part['photos']})
        bodies.append({'action': 'reply_inbox', 'message': 'Nếu muốn đặt thêm đơn mới, chị nhắn “mua lại” giúp shop nhé ạ.'})
        return True, {'rule': 'sales:reorder_browse', 'body': bodies[0], 'bodies': bodies,
                      'lead': lead, 'next_state': 'complete'}
    return True, None

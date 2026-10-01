"""Quick-reply import and deterministic sales conversation state machine."""
import csv
import json
import re
from pathlib import Path
from discounts import hesitant

from bot import normalize


def select_script(document, page_id):
    """Select an explicit page script; continue supporting legacy single-page files."""
    from copy import deepcopy
    if 'pages' in document:
        if str(page_id) not in document['pages']:
            raise ValueError(f'sales_script missing page {page_id}')
        document = document['pages'][str(page_id)]
    script = deepcopy(document)
    if not isinstance(script.get('groups'), dict) or not isinstance(script.get('prompts'), dict):
        raise ValueError(f'Invalid sales_script for page {page_id}: groups and prompts required')
    colors = script.get('product', {}).get('colors', [])
    if not isinstance(colors, list) or any(not isinstance(c, str) or not c.strip() for c in colors):
        raise ValueError('product.colors must be a list of non-empty color names')
    if len({normalize(c) for c in colors}) != len(colors):
        raise ValueError('product.colors contains duplicate colors')
    script['prompts'].setdefault('color', 'Chị chọn màu nào ạ? Shop có các màu: {colors}.')
    return script


def extract_color(text, lead, colors):
    result = dict(lead)
    if not colors:
        return result
    n = normalize(text)
    if result.get('color') not in colors or re.search(r'\b(?:doi|sua|sai) mau\b', n):
        result.pop('color', None)
    # Only accept an explicit selection or a reply consisting of a color name.
    # Do not infer colors from addresses, questions, negations or alternatives.
    if '?' in text or re.search(r'\b(khong|ko|chua|hay|hoac|con|het)\b', n):
        return result
    matches = []
    for color in colors:
        label = re.escape(normalize(color))
        if (re.fullmatch(r'(?:da |vang )?(?:(?:lay|chon) )?(?:mau )?' + label + r'(?: nhe| nha| a| nhe a)?[.!]*', n)
                or re.search(r'\b(?:mau|chon|lay)\s+' + label + r'(?!\w)', n)):
            matches.append(color)
    if len(matches) == 1:
        result['color'] = matches[0]
    return result


def compile_script(source, output):
    with open(source, encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream, delimiter='\t'))
    groups = {}
    for row in sorted(rows, key=lambda row: int(row['quickReplyIndex'])):
        group = groups.setdefault(str(int(row['quickReplyIndex'])), [])
        if row.get('message', '').strip():
            group.append({'message': row['message'].strip()})
        if row.get('photos', '').strip():
            group.append({'photos': row['photos'].split()})
    script = {'groups': groups, 'prompts': {
        'comment': 'Dạ chào chị #{FULL_NAME}, chị nhắn Messenger cho shop để được báo giá và tư vấn size nhé ạ!',
        'phone': 'Chị cho shop xin số điện thoại liên hệ nhận hàng nhé ạ.',
        'address': 'Chị cho shop xin địa chỉ nhận hàng đầy đủ: số nhà/tên đường hoặc thôn/ấp, phường/xã và tỉnh/thành phố nhé ạ.',
        'size_confirm': 'Theo bảng cân nặng, shop gợi ý size {size}. Chị xác nhận chọn size {size} giúp shop nhé ạ; nếu muốn mặc rộng hơn chị cho shop biết để nhân viên tư vấn thêm.',
        'size_unclear': 'Shop chưa xác định chắc size phù hợp. Chị cho shop biết size muốn chọn (S, M, L, XL, 2XL), hoặc nhắn “nhân viên” để được tư vấn nhé ạ.',
        'confirm': 'Shop ghi nhận: size {size}; SĐT {phone}; địa chỉ {address}. Chị kiểm tra và nhắn “đúng rồi” nếu thông tin chính xác, hoặc gửi lại phần cần sửa nhé ạ.',
        'complete': 'Shop đã nhận đủ thông tin của chị và chuyển sang bước nhân viên kiểm tra sản phẩm, xác nhận đơn. Cảm ơn chị ạ!',
        'human': 'Shop đã ghi nhận yêu cầu tư vấn của chị. Chị vui lòng chờ nhân viên hỗ trợ nhé ạ.',
        'stop': 'Dạ shop ghi nhận, bot sẽ dừng nhắn tại cuộc trò chuyện này ạ.'}}
    Path(output).write_text(json.dumps(script, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return script


def conversation_intent(text):
    """Require an explicit request; a keyword mention alone is not an opt-out."""
    human = False
    for clause in re.split(r'[,;.!\n]+', normalize(text)):
        clause = clause.strip()
        # Negated requests must not become a handoff or cancellation.
        negated = re.search(r'\b(?:(?:khong|ko|k|chua)(?: (?:can|muon))?|dung)\s+(?:gap |noi chuyen voi )?(?:goi|nhan vien|khieu nai|tu van)', clause)
        stop = re.fullmatch(
            r'(?:(?:da|vang|thoi|chi|em|anh|minh|toi|shop|cho|xin) )*'
            r'(?:stop|(?:khong|ko|k) (?:mua|lay)(?: (?:nua|hang|ao|don nay))?'
            r'|(?:khong|ko) can(?: mua| lay)?(?: nua)?|huy don(?: hang| nay)?'
            r'|dung (?:nhan|gui)(?: tin| tin nhan)?(?: (?:nua|cho (?:chi|em|anh)))?)'
            r'(?: (?:giup|ho|cho|chi|em|anh|minh|toi|shop|nhe|nha|a|voi|di))*', clause)
        if stop:
            return 'stop'
        if not negated and re.search(
                r'\b(nhan vien|khieu nai|goi dien|goi cho|goi em|goi chi|tu van truc tiep)\b', clause):
            human = True
    return 'human' if human else None


def is_affirmation(text):
    n = normalize(text).strip(' .!,')
    if n in ('👍', '👍🏻', '👍🏼', '👍🏽', '👍🏾', '👍🏿'):
        return True
    return bool(re.fullmatch(
        r'(?:(?:da|vang|chi|em|anh|minh|toi) )*'
        r'(?:dung roi|dung|chinh xac|xac nhan|ok|oke|okay|dong y|chot)'
        r'(?: (?:roi|nhe|nha|a|shop|em|chi|anh))*', n))


def selected_size(text):
    """Read choices per clause, preserving ambiguity across the entire message."""
    n = normalize(text)
    label = r'(2xl|xxl|xl|s|m|l)(?!\w)'
    # Catch shorthand alternatives such as "size M hay L" as well.
    if re.search(r'(?:\b(?:hay|hoac)\s+|/)\s*(?:(?:size|sz|co)\s*)?' + label, n):
        return None
    choices = set()
    for clause in re.split(r'[,;.!\n]+', n):
        if re.search(r'\b(khong|ko|k|chua|dung|hay|hoac|co vua)\b|\?', clause):
            continue
        matches = re.findall(r'\b(?:size|sz|co|lay|chon|chot)\s*' + label, clause)
        bare = re.fullmatch(r'\s*' + label + r'(?: (?:nhe|nha|a|shop|em|chi))*\s*', clause)
        if bare:
            matches.append(bare[1])
        choices.update(s.upper().replace('XXL', '2XL') for s in matches)
    return choices.pop() if len(choices) == 1 else None


def extract(text, lead):
    """Only confirmed size labels and plausible VN mobile numbers become fields."""
    result = dict(lead)
    n = normalize(text)
    for field, pattern in [('phone', r'\b(?:sai|doi|sua) (?:so|sdt|so dien thoai)\b'),
                           ('address', r'\b(?:sai|doi|sua) dia chi\b'),
                           ('size', r'\b(?:sai|doi|sua) (?:size|sz)\b')]:
        if re.search(pattern, n):
            result.pop(field, None)
    size = selected_size(text)
    if size:
        result['size'] = size
        result.pop('suggested_size', None)
    phones = re.findall(r'(?<!\d)(?:\+84|84|0)(?:[ .-]*\d){9}(?![ .-]*\d)', text)
    valid = set()
    for raw in phones:
        number = re.sub(r'\D', '', raw)
        number = '0' + number[2:] if number.startswith('84') else number
        if re.fullmatch(r'0[35789]\d{8}', number):
            valid.add(number)
    if len(valid) == 1 and not re.search(r'\b(khong phai|so cu|sai so)\b', n):
        result['phone'] = valid.pop()
    weight = re.search(r'(?<!\d)(\d{2,3}(?:[.,]\d)?)\s*(?:kg|ky|can|kilogram)\b', n)
    if weight:
        result['weight_kg'] = float(weight[1].replace(',', '.'))
    height = re.search(r'(?<!\d)(1)[m.,](\d{2})(?!\d)', n)
    cm = re.search(r'(?<!\d)(1\d{2})\s*cm\b', n)
    if height:
        result['height_cm'] = 100 + int(height[2])
    elif cm:
        result['height_cm'] = int(cm[1])
    address_match = re.search(r'(?:địa chỉ|dia chi|đ/c|d/c|giao (?:đến|den|tới|toi)|gửi (?:về|ve))\s*[:：-]?\s*(.+)', text, re.I | re.S)
    address = address_match[1].strip() if address_match else ''
    if not address and lead.get('stage') == 'address':
        address = text.strip()
    # Reject generic acknowledgements and incomplete location-only answers.
    address_n = normalize(address)
    detail = re.search(r'\b(duong|thon|ap|hem|ngo|so nha|to|khu pho)\b|\d', address_n)
    locality = re.search(r'\b(phuong|xa|quan|huyen|p\.|q\.)', address_n)
    province = re.search(r'\b(tinh|thanh pho|tp|ha noi|hcm|ho chi minh|da nang|hai phong|can tho)\b', address_n)
    if len(address) >= 15 and detail and locality and province and not re.search(r'\b(khong|chua|doi sau)\b', address_n):
        # Remove explicitly labelled phone/size segments from the address.
        address = re.split(r'(?:[;\n]\s*(?:sđt|sdt|đt|phone|size|sz)\s*[:：])', address, flags=re.I)[0].strip(' ,;')
        result['address'] = address
    return result


def decide(script, event, previous, assistance=None):
    from reorders import lifecycle
    handled, decision = lifecycle(script, event, previous)
    if handled:
        return decision
    data = event['data']
    message = data['message']
    n = normalize(message['message'])
    name = (message.get('from') or {}).get('name') or 'chị'
    lead = dict(previous)
    lead.setdefault('session_id', 'purchase:' + str(message['id']) if not previous else 'legacy')
    colors = script.get('product', {}).get('colors', [])
    required = ('size', 'phone', 'address') + (('color',) if colors else ())
    if lead.get('stage') == 'human':
        return None
    output = []
    intent = conversation_intent(message['message'])
    assistance = assistance or {}

    def text(value):
        output.append({'action': 'reply_inbox', 'message': value.replace('#{FULL_NAME}', name)})

    def prompt(key, **kwargs):
        text(script['prompts'][key].format(**kwargs) if kwargs else script['prompts'][key])

    def group(index):
        for part in script['groups'].get(str(index), []):
            if 'message' in part:
                if index == 2 and 'chiều cao cân nặng' in part['message'] and (lead.get('size') or lead.get('weight_kg')):
                    continue
                text(part['message'])
            else:
                output.append({'action': 'reply_inbox', 'photos': part['photos']})

    if message['type'] == 'COMMENT':
        if not previous.get('comment_replied') and message.get('can_comment'):
            output.append({'action': 'reply_comment', 'message_id': message['id'],
                           'message': script['prompts']['comment'].replace('#{FULL_NAME}', name)})
            lead['comment_replied'] = True
        else:
            return None
    elif intent == 'stop':
        prompt('stop')
        lead['stage'] = 'stopped'
    elif intent == 'human':
        lead = extract_color(message['message'], extract(message['message'], lead), colors)
        prompt('human')
        lead['stage'] = 'human'
    elif script.get('discount_followup', {}).get('enabled') and hesitant(message['message'],script['discount_followup']):
        lead = extract_color(message['message'], extract(message['message'], lead), colors)
        text(script['discount_followup']['hesitation_reply'])
        lead['stage'] = 'hesitating'
    else:
        lead = extract_color(message['message'], extract(message['message'], lead), colors)
        lead.update(assistance.get('updates', {}))
        if assistance.get('updates', {}).get('size'):
            lead.pop('suggested_size', None)
        affirm = is_affirmation(message['message'])
        if previous.get('stage') == 'contact_reuse_confirm':
            if affirm:
                for key, value in previous.get('previous_contact', {}).items():
                    lead.setdefault(key, value)
                lead['contact_checked'] = True
                lead.pop('previous_contact', None)
            elif (re.search(r'\b(khong|ko|chua|doi|sua|moi)\b', n)
                  or any(lead.get(k) != previous.get(k) for k in ('phone', 'address'))):
                lead['contact_checked'] = True
                lead.pop('previous_contact', None)
        if previous.get('stage') == 'size_confirm' and affirm and lead.get('suggested_size'):
            lead['size'] = lead.pop('suggested_size')
        if previous.get('stage') == 'confirm' and affirm and all(lead.get(k) and lead.get(k) == previous.get(k) for k in required):
            prompt('complete')
            lead['stage'] = 'complete'
        else:
            price = not assistance.get('answers') and bool(re.search(r'\b(gia|bao nhieu|xin anh|xem anh|mau ao|chat lieu|vai gi)\b', n))
            if not lead.get('introduced') or price:
                group(2)
                lead['introduced'] = True
            if not lead.get('size'):
                weight = lead.get('weight_kg')
                suggestion = next((size for lo, hi, size in [(42,48,'S'), (49,55,'M'), (56,61,'L'), (62,69,'XL'), (70,80,'2XL')]
                                   if weight is not None and lo <= weight <= hi), None)
                if suggestion:
                    lead['suggested_size'] = suggestion
                    group(3)
                    prompt('size_confirm', size=suggestion)
                    lead['stage'] = 'size_confirm'
                elif weight is not None:
                    prompt('size_unclear')
                    lead['stage'] = 'size'
                elif re.search(r'\b(size|sz|bang size|bang sz)\b', n):
                    group(3)
                    group(4)
                    lead['stage'] = 'size'
                else:
                    if previous.get('introduced') and not price:
                        if assistance.get('keep_clarifying'):
                            prompt('size_unclear')
                        else:
                            group(5)
                    lead['stage'] = 'size'
            elif colors and not lead.get('color'):
                prompt('color', colors=', '.join(colors))
                lead['stage'] = 'color'
            elif lead.get('previous_contact') and not lead.get('contact_checked') and (not lead.get('phone') or not lead.get('address')):
                contact = lead['previous_contact']
                details = '; '.join(f'{label}: {contact[key]}' for key, label in [('phone', 'SĐT'), ('address', 'địa chỉ')] if contact.get(key) and not lead.get(key))
                text('Chị có muốn dùng lại thông tin nhận hàng của đơn trước (' + details + ')? Chị nhắn “đúng rồi” để dùng lại, hoặc “không” để nhập thông tin mới nhé ạ.')
                lead['stage'] = 'contact_reuse_confirm'
            elif not lead.get('phone'):
                prompt('phone')
                lead['stage'] = 'phone'
            elif not lead.get('address'):
                prompt('address')
                lead['stage'] = 'address'
            else:
                prompt('confirm', **{k: lead.get(k, '') for k in ('size', 'phone', 'address', 'color')})
                if colors and '{color}' not in script['prompts']['confirm']:
                    output[-1]['message'] = 'Màu: ' + lead['color'] + '. ' + output[-1]['message']
                lead['stage'] = 'confirm'
    if message['type'] == 'INBOX':
        waiting = {'size', 'size_confirm', 'color', 'phone', 'address', 'confirm', 'contact_reuse_confirm'}
        fields = (*required, 'weight_kg', 'height_cm', 'suggested_size')
        progressed = any(lead.get(k) != previous.get(k) for k in fields) or bool(assistance.get('answers'))
        repeated = lead.get('stage') in waiting and lead.get('stage') == previous.get('stage') and not progressed
        lead['repeat_count'] = previous.get('repeat_count', 0) + 1 if repeated else 0
        if lead['repeat_count'] >= 3 and not assistance.get('keep_clarifying'):
            output.clear()
            text(script['prompts'].get('repeated_handoff',
                 'Shop chưa hiểu rõ thông tin của chị sau vài lần trao đổi. Chị vui lòng chờ nhân viên hỗ trợ trực tiếp nhé ạ.'))
            lead['stage'] = 'human'
            lead['handoff_reason'] = 'repeated_unresolved_input'
        if lead.get('stage') not in ('human', 'stopped', 'complete'):
            prefix = assistance.get('answers', []) or ([assistance['clarification']] if assistance.get('clarification') else [])
            output[:0] = [{'action': 'reply_inbox', 'message': answer} for answer in prefix]
    return {'rule': 'sales:' + lead.get('stage', 'comment'), 'bodies': output,
            'body': output[0] if output else None, 'lead': lead,
            'next_state': lead['stage'] if lead.get('stage') in ('human', 'complete', 'stopped') else 'sales'}

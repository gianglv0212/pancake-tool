"""Persistent optional discount follow-ups, executed by the polling process."""
import json
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

LOG = logging.getLogger('pancake.discounts')

DEFAULTS = {
    'enabled': False,
    'incomplete_after_seconds': 7200,
    'hesitation_after_seconds': 300,
    'max_customer_age_seconds': 82800,
    'discount_price': '',
    'hesitation_keywords': ['chưa muốn lấy', 'chưa mua', 'để suy nghĩ', 'để chị suy nghĩ', 'giá cao', 'đắt quá', 'mắc quá'],
    'hesitation_reply': 'Dạ chị cứ cân nhắc thêm nhé, khi cần chị nhắn shop hỗ trợ ạ.',
    'message': 'Chị {name} ơi, shop có giá ưu đãi {discount_price} cho mẫu chị đang quan tâm. Nếu chị muốn lấy, chị gửi thêm {missing_fields} để shop tư vấn tiếp nhé ạ.'
}


def validate(options):
    if not options.get('enabled'):
        return
    if not str(options.get('discount_price', '')).strip():
        raise ValueError('discount_followup: fill discount_price before enabling')
    for key in ('incomplete_after_seconds', 'hesitation_after_seconds', 'max_customer_age_seconds'):
        if not isinstance(options.get(key), (int, float)) or options[key] < 0:
            raise ValueError('discount_followup: invalid ' + key)
    if not options.get('message'):
        raise ValueError('discount_followup: message required')
    options['message'].format(name='chị', discount_price=options['discount_price'], missing_fields='size')


def hesitant(text, options):
    from bot import normalize
    n = normalize(text)
    return any(re.search(r'(?<!\w)' + re.escape(normalize(k)) + r'(?!\w)', n)
               for k in options.get('hesitation_keywords', []) if k.strip())


def stamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def schedule(store, page, event, lead, options, now=None):
    if not options.get('enabled') or event['data']['message']['type'] != 'INBOX':
        return
    now = time.time() if now is None else now
    message = event['data']['message']
    conversation = str(event['data']['conversation']['id'])
    eligible = lead.get('stage') not in ('human','complete','stopped','error','reorder_confirm','contact_reuse_confirm') and not all(lead.get(k) for k in ('size','phone','address'))
    with store.connect() as db:
        if not eligible:
            db.execute("UPDATE followups SET status='cancelled',result='No longer eligible' WHERE page=? AND conversation=? AND status='pending'", (page,conversation))
            return
        reason = 'hesitation' if hesitant(message['message'], options) else 'incomplete'
        delay = options['hesitation_after_seconds'] if reason == 'hesitation' else options['incomplete_after_seconds']
        customer_at = stamp(message['inserted_at'])
        values = (page,conversation,str(message['id']),customer_at,now,customer_at+delay,reason,
                  (message.get('from') or {}).get('name') or 'chị',str((message.get('from') or {}).get('id')))
        db.execute("""INSERT INTO followups(page,conversation,message,customer_at,scheduled_at,due,reason,name,customer)
            VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(page,conversation) DO UPDATE SET
            message=excluded.message,customer_at=excluded.customer_at,scheduled_at=excluded.scheduled_at,
            due=excluded.due,reason=excluded.reason,name=excluded.name,customer=excluded.customer,status='pending',result=NULL
            WHERE followups.status IN ('pending','cancelled')""", values)
    LOG.info('page=%s conversation=%s followup_scheduled reason=%s delay=%ss',page,conversation,reason,delay)


def fresh_context(client, page, conversation):
    params, seen = {'order_by':'updated_at'}, set()
    for _ in range(1000):
        batch = client.get('v2',page,'/conversations',params).get('conversations')
        if not isinstance(batch,list):
            raise ValueError('Missing conversations')
        for c in batch:
            if str(c['id']) == conversation:
                return c, client.get('v1',page,'/conversations/'+quote(conversation,safe='')+'/messages',{})
        if len(batch)<60:
            return None, None
        cursor = str(batch[-1]['id'])
        if cursor in seen:
            raise ValueError('Conversation pagination stalled')
        seen.add(cursor)
        params = {'order_by':'updated_at','last_conversation_id':cursor}
    raise ValueError('Conversation pagination limit')


def run_due(store, config, client, live=False, now=None):
    from bot import page_token
    now = time.time() if now is None else now
    with store.connect() as db:
        rows = db.execute("SELECT * FROM followups WHERE status='pending' AND due<=? ORDER BY due",(now,)).fetchall()
    for row in rows:
        page, conv = row['page'], row['conversation']
        page_config = config['pages'].get(page,{})
        options = page_config.get('discount_followup',{})
        def finish(status, reason):
            with store.connect() as db:
                db.execute('UPDATE followups SET status=?,result=? WHERE page=? AND conversation=?',(status,reason,page,conv))
            LOG.info('page=%s conversation=%s followup=%s trigger=%s',page,conv,status,row['reason'])
        if not page_config.get('enabled',True) or not options.get('enabled'):
            finish('cancelled','Feature disabled')
            continue
        lead = store.lead(page,conv)
        if (store.state(page,conv) in ('human','error') or lead.get('stage') in ('human','complete','stopped','error','reorder_confirm','contact_reuse_confirm')
                or all(lead.get(k) for k in ('size','phone','address'))):
            finish('cancelled','Complete or paused')
            continue
        if now-row['customer_at'] > options['max_customer_age_seconds']:
            finish('expired','Customer message too old')
            continue
        try:
            c, response = fresh_context(client,page,conv)
            if not c or c.get('is_removed') or (page_config.get('skip_assigned',True) and c.get('assignee_ids')):
                finish('cancelled','Conversation removed/missing/assigned')
                continue
            tags = {t.get('id') if isinstance(t,dict) else t for t in c.get('tags',[]) if t is not None}
            if tags & set(page_config.get('stop_tags',[])) or response.get('can_inbox') is False or response.get('is_banned'):
                finish('cancelled','Cannot inbox or stop tag')
                continue
            messages = response.get('messages')
            if not isinstance(messages,list) or not messages:
                raise ValueError('Missing messages for follow-up validation')
            incoming = [m for m in messages if str((m.get('from') or {}).get('id'))==row['customer']]
            newest = max(incoming,key=lambda m:stamp(m['inserted_at']),default=None)
            if not newest or str(newest['id'])!=row['message']:
                finish('cancelled','New customer activity; wait for normal processing')
                continue
            if any(str((m.get('from') or {}).get('id'))==page and stamp(m['inserted_at'])>row['scheduled_at'] for m in messages):
                finish('cancelled','Page/staff has replied since scheduling')
                continue
            missing = ', '.join(label for key,label in [('size','size'),('phone','số điện thoại'),('address','địa chỉ nhận hàng')] if not lead.get(key))
            body = {'action':'reply_inbox','message':options['message'].format(name=row['name'],discount_price=options['discount_price'],missing_fields=missing)}
            with store.connect() as db:
                claimed = db.execute("UPDATE followups SET status='processing' WHERE page=? AND conversation=? AND status='pending'",(page,conv)).rowcount
            if not claimed:
                continue
            status = client.send(page,conv,page_token(page_config),body) if live else 'dry_run'
            finish(status,json.dumps({'body':body,'reason':row['reason']},ensure_ascii=False))
        except Exception:
            LOG.exception('page=%s conversation=%s discount_followup_failed',page,conv)
            with store.connect() as db:
                db.execute("UPDATE followups SET status='unknown',result='Interrupted send; inspect logs' WHERE page=? AND conversation=? AND status='processing'",(page,conv))

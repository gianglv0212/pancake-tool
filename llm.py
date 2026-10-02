"""Budgeted, step-scoped extraction and contextual clarification. No business tools."""
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from bot import normalize
from diagnostics import register_secret, redact, log_http_error

LOG = logging.getLogger('pancake.llm')


def trace(event, step, value):
    """Readable diagnostic blocks, correlated to one customer message."""
    identity = (f"page={event['page_id']} conversation={event['data']['conversation']['id']} "
                f"message={event['data']['message']['id']}")
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    # Redact before logging too: callers may install their own formatter.
    LOG.info('%s step=%s\n%s\n%s step=%s END', identity, step, redact(content), identity, step)
DEFAULTS = {
    'enabled': False,
    'model': 'gpt-5.4-nano',
    'api_key': '',
    'api_key_env': 'OPENAI_API_KEY',
    'daily_budget_usd': 1.0,
    'max_calls_per_conversation': 10,
    'timeout_seconds': 5,
    'max_output_tokens': 512,
    'max_input_bytes': 16000,
}
from step_support import (FIELDS, SCHEMA, SCOPES, INSTRUCTIONS, schema_for,
                          recent_context, validate_result, address_options)


def settings(value):
    # Accept legacy configuration without activating or sending its FAQ content.
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != 'faq'}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('llm: invalid settings or unknown keys')
    options = {**DEFAULTS, **value}
    if not isinstance(options['api_key'], str):
        raise ValueError('llm.api_key must be a string')
    options['api_key'] = options['api_key'].strip()
    register_secret(options['api_key'])
    if type(options['enabled']) is not bool:
        raise ValueError('llm.enabled must be boolean')
    # Restrict models so an unpriced model cannot bypass the budget calculation.
    if options['model'] not in ('gpt-5.4-nano', 'gpt-5.4-nano-2026-03-17'):
        raise ValueError('llm.model: only gpt-5.4-nano supported')
    if not isinstance(options['api_key_env'], str) or not options['api_key_env'].strip():
        raise ValueError('llm.api_key_env required')
    for key, lo, hi in [('daily_budget_usd', 0, 1000), ('timeout_seconds', 1, 15),
                        ('max_calls_per_conversation', 0, 100), ('max_output_tokens', 128, 2048),
                        ('max_input_bytes', 2000, 64000)]:
        number = options[key]
        if type(number) not in (int, float) or not math.isfinite(number) or not lo <= number <= hi:
            raise ValueError('llm: invalid ' + key)
        if key in ('max_calls_per_conversation', 'max_output_tokens', 'max_input_bytes') and type(number) is not int:
            raise ValueError('llm: integer required for ' + key)
    return options


def reserve(store, event, options, amount, session=None):
    """Reserve before network I/O; concurrent workers share the same SQLite budget."""
    page = str(event['page_id'])
    conv = str(event['data']['conversation']['id'])
    mid = str(event['data']['message']['id'])
    day = datetime.now(timezone.utc).date().isoformat()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM llm_calls WHERE page=? AND conversation=? AND message=?', (page, conv, mid)).fetchone():
            return False
        lead_row = db.execute('SELECT data FROM leads WHERE page=? AND conversation=?', (page, conv)).fetchone()
        if session is None:
            session = json.loads(lead_row[0]).get('session_id', 'legacy') if lead_row else 'legacy'
        calls = db.execute('SELECT count(*) FROM llm_calls WHERE page=? AND conversation=? AND session=?', (page, conv, session)).fetchone()[0]
        used = db.execute('SELECT coalesce(sum(charged_microusd),0) FROM llm_calls WHERE page=? AND day=?', (page, day)).fetchone()[0]
        if calls >= options['max_calls_per_conversation'] or used + amount > int(options['daily_budget_usd'] * 1_000_000):
            return False
        db.execute('INSERT INTO llm_calls(page,conversation,message,day,status,reserved_microusd,charged_microusd,input_tokens,output_tokens,result,session) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (page, conv, mid, day, 'reserved', amount, amount, None, None, None, session))
    return True


def finish(store, event, status, response=None, result=None):
    usage = (response or {}).get('usage') or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens, output_tokens = usage.get('input_tokens'), usage.get('output_tokens')
    values = [status, json.dumps(result, ensure_ascii=False) if result is not None else None]
    sql = 'UPDATE llm_calls SET status=?,result=?'
    if all(type(t) is int and t >= 0 for t in (input_tokens, output_tokens)):
        # USD per million tokens == microUSD per token; cached tokens charged conservatively.
        charge = math.ceil(input_tokens * .20 + output_tokens * 1.25)
        sql += ',input_tokens=?,output_tokens=?,charged_microusd=?'
        values += [input_tokens, output_tokens, charge]
    # Missing usage, timeout or interrupted process retains the full reservation.
    values += [str(event['page_id']), str(event['data']['conversation']['id']), str(event['data']['message']['id'])]
    with store.connect() as db:
        db.execute(sql + ' WHERE page=? AND conversation=? AND message=?', values)


def request_model(body, key, timeout):
    request = Request('https://api.openai.com/v1/responses', data=body,
                      headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}, method='POST')
    with urlopen(request, timeout=timeout) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError('Oversized LLM response')
    return json.loads(raw)


def parse_response(response):
    if not isinstance(response, dict) or response.get('status') != 'completed':
        raise ValueError('LLM response incomplete')
    texts = []
    for item in response.get('output', []):
        if item.get('type') == 'message':
            for part in item.get('content', []):
                if part.get('type') == 'refusal':
                    raise ValueError('LLM refusal')
                if part.get('type') == 'output_text':
                    texts.append(part['text'])
    if len(texts) != 1:
        raise ValueError('Missing structured output')
    return json.loads(texts[0])


def assist(store, page, event, previous, decision, live=False):
    from copy import deepcopy
    from sales import address_only, conversation_intent, is_affirmation
    options = page.get('llm', {})
    text = event['data']['message']['message']
    stage = previous.get('stage')

    def skip(reason):
        trace(event, 'llm_skip', reason)
        return None

    if not options.get('enabled') or event['data']['message']['type'] != 'INBOX' or not live:
        return skip('Disabled, non-INBOX or dry-run')
    if stage not in SCOPES or conversation_intent(text) or is_affirmation(text):
        return skip('No active step to interpret, or explicit lifecycle/confirmation handled by rules')
    if decision['lead'].get('stage') == 'hesitating':
        return skip('Hesitation handled by rules')
    allowed = SCOPES[stage]
    changed_fields = [k for k in FIELDS if decision['lead'].get(k) != previous.get(k)]
    expected = {'size_confirm': 'size', 'confirm': 'confirmation',
                'contact_reuse_confirm': 'confirmation', 'reorder_confirm': 'confirmation'}.get(stage, stage)
    resolved = bool(set(changed_fields) & set(allowed))
    # A parser-recognized correction to an order needs a fresh summary, not model consent.
    if stage in ('confirm', 'contact_reuse_confirm') and changed_fields:
        resolved = True
    address = address_only(text, stage)
    if address and decision['lead'].get('address') == address:
        return skip('Standalone address handled by parser; continue collecting missing fields')
    n = normalize(text)
    simple = re.fullmatch(r'(?:xin )?(?:gia|anh|bang size)|(?:chao|hi|hello|alo)(?: shop)?[.! ]*', n)
    if resolved or simple:
        return skip({'resolved': resolved, 'simple': bool(simple), 'changed_fields': changed_fields})
    key = options.get('api_key', '').strip() or os.environ.get(options['api_key_env'], '').strip()
    if not key:
        return skip('Missing API key: set llm.api_key or the configured environment variable')
    register_secret(key)
    recent = recent_context(store, event, previous)
    context = {
        'current_message': text, 'stage': stage, 'expected_field': expected,
        'allowed_fields': list(allowed),
        'known': {k: previous[k] for k in (*FIELDS, 'suggested_size') if k in previous},
        'parser_updates': {k: decision['lead'][k] for k in changed_fields if k in decision['lead']},
        'colors': page['_sales_script'].get('product', {}).get('colors', []),
        'constraints': {'sizes': ['S','M','L','XL','2XL'], 'weight_kg': [20,250],
                        'height_cm': [100,230]},
        **recent,
    }
    if stage == 'address':
        context['address_options'] = address_options(text, previous)
    session = previous.get('session_id', decision['lead'].get('session_id', 'legacy'))
    for attempt in range(2):
        repair = attempt == 1
        account_event = deepcopy(event)
        if repair:
            account_event['data']['message']['id'] = str(event['data']['message']['id']) + ':llm-clarification-repair'
        payload = {'model': options['model'], 'store': False, 'instructions': INSTRUCTIONS,
                   'reasoning': {'effort': 'low'}, 'max_output_tokens': options['max_output_tokens'],
                   'text': {'format': {'type': 'json_schema', 'name': 'sales_step', 'strict': True,
                                       'schema': schema_for(stage, repair, text, previous)}}}
        if repair:
            payload['instructions'] += '\nLƯỢT NÀY CHỈ VIẾT reply hỏi lại theo validation_error. Không trích dữ liệu, không xác nhận. reply không được rỗng.'
        while True:
            state = {k:v for k,v in context.items() if k not in ('recent_turns','current_message')}
            payload['input'] = ([{'role':'developer', 'content':json.dumps(state,ensure_ascii=False)}]
                                + context['recent_turns'] + [{'role':'user','content':text}])
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            if len(body) <= options['max_input_bytes']:
                break
            if not context['recent_turns']:
                return skip('Input exceeds max_input_bytes')
            context['recent_turns'].pop(0)
        amount = math.ceil((len(body) + 4096) * .20 + options['max_output_tokens'] * 1.25)
        if not reserve(store, account_event, options, amount, session):
            return skip('Budget/call limit or duplicate message; no further request')
        trace(event, 'llm_call_reason', {
            'attempt': attempt + 1, 'repair': repair,
            'reasons': [context['validation_error'] if repair else
                        'Parser chưa giải quyết được bước đang chờ; chỉ diễn giải bước này'],
            'previous_stage': stage, 'rule_stage': decision['lead'].get('stage'),
            'expected_field': expected, 'allowed_fields': list(allowed),
            'changed_fields': changed_fields, 'parser_updates': context['parser_updates'],
            'resolved_by_rules': resolved, 'standalone_address_detected': bool(address),
            'budget_reserved_microusd': amount, 'next_action': 'Call LLM API now'})
        trace(event, 'llm_request', {**payload, 'input': context})
        response = None
        started = time.monotonic()
        try:
            response = request_model(body, key, options['timeout_seconds'])
        except Exception as error:
            if isinstance(error, HTTPError):
                log_http_error(LOG, f"message={event['data']['message']['id']} llm_http_error", error)
            trace(event, 'llm_failure', {'error_type': type(error).__name__, 'error': str(error),
                  'fallback': 'Current-step prompt; no automatic network retry'})
            finish(store, account_event, 'failed')
            return None
        trace(event, 'llm_response', response)
        try:
            parsed = parse_response(response)
            trace(event, 'llm_interpretation', parsed)
            if repair and (not parsed.get('needs_clarification') or parsed.get('updates')):
                raise ValueError('Repair may only ask a clarification, not mutate data')
            result = validate_result(parsed, text, previous, page['_sales_script'], options, context)
        except (ValueError, TypeError, KeyError) as error:
            trace(event, 'llm_validation', {'status': 'rejected', 'reason': str(error), 'attempt': attempt+1})
            finish(store, account_event, 'rejected', response if isinstance(response, dict) else None)
            # Refusals/incomplete responses are technical failures, not a request to spend again.
            if repair or not isinstance(response, dict) or response.get('status') != 'completed' or str(error) == 'LLM refusal':
                return None
            context['validation_error'] = str(error)
            continue
        trace(event, 'llm_validation', {'status': 'clarification' if result.get('clarification') else 'accepted',
              'accepted_assistance': result, 'elapsed_seconds': round(time.monotonic()-started,3)})
        finish(store, account_event, 'accepted', response, result)
        return result
    return None


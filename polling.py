"""Pancake REST polling, without a webhook server."""
import json
import logging
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen
from bot import plan, process_one, send, page_token
from diagnostics import log_http_error

LOG = logging.getLogger('pancake')


def timestamp(value):
    d = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


class ApiError(Exception):
    pass


class Client:
    def __init__(self, pages):
        self.pages, self.last = pages, {}
        self.locks = {page: threading.Lock() for page in pages}

    def throttle(self, page):
        with self.locks[page]:
            time.sleep(max(0, .25 - (time.monotonic() - self.last.get(page, 0))))
            self.last[page] = time.monotonic()

    def get(self, version, page, suffix, params):
        query = dict(params, page_access_token=page_token(self.pages[page]))
        url = f'https://pages.fm/api/public_api/{version}/pages/{quote(page, safe="")}{suffix}?' + urlencode(query)
        for attempt in range(3):
            self.throttle(page)
            LOG.debug('page=%s GET version=%s path=%s attempt=%s params=%s', page, version, suffix, attempt+1, params)
            try:
                with urlopen(url, timeout=20) as response:
                    data = json.load(response)
                if not isinstance(data, dict) or data.get('success') is False:
                    raise ApiError('Unsuccessful API response')
                return data
            except HTTPError as error:
                log_http_error(LOG, f'page={page} GET {suffix} attempt={attempt+1}', error)
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise ApiError(f'HTTP {error.code}') from None
            except (URLError, OSError, ValueError):
                LOG.exception('page=%s GET path=%s attempt=%s failed', page, suffix, attempt+1)
                if attempt == 2:
                    raise ApiError('Network or JSON error') from None
            time.sleep(2 ** attempt)

    def send(self, page, conversation, token, body):
        self.throttle(page)
        return send(page, conversation, token, body)


def read_messages(client, page, conversation, cutoff):
    suffix = '/conversations/' + quote(str(conversation['id']), safe='') + '/messages'
    remaining = conversation.get('message_count')
    params, seen, result, post = {}, set(), [], None
    for _ in range(1000):
        response = client.get('v1', page, suffix, params)
        batch = response.get('messages')
        if not isinstance(batch, list):
            raise ApiError('Missing messages array')
        post = post or response.get('post')
        if not batch:
            break
        new = [m for m in batch if str(m['id']) not in seen]
        if not new:
            raise ApiError('Message pagination stalled')
        seen.update(str(m['id']) for m in new)
        result.extend(m for m in new if timestamp(m['inserted_at']) >= cutoff)
        if min(timestamp(m['inserted_at']) for m in batch) < cutoff or len(batch) < 30:
            break
        if not isinstance(remaining, int):
            raise ApiError('Missing message_count for pagination')
        remaining -= len(batch)
        if remaining <= 0:
            break
        params = {'current_count': remaining}
    else:
        raise ApiError('Message pagination limit exceeded')
    return sorted(result, key=lambda m: (timestamp(m['inserted_at']), str(m['id']))), post


def poll_page(store, config, client, page, now=None):
    now = time.time() if now is None else now
    with store.connect() as db:
        db.execute('INSERT OR IGNORE INTO poll_starts VALUES(?,?)', (page, now))
        started = db.execute('SELECT started FROM poll_starts WHERE page=?', (page,)).fetchone()[0]
    cutoff = max(started, now - config.get('max_event_age_seconds', 600))
    params, cursors, conversations = {'order_by': 'updated_at'}, set(), {}
    for _ in range(1000):
        batch = client.get('v2', page, '/conversations', params).get('conversations')
        if not isinstance(batch, list):
            raise ApiError('Missing conversations array')
        if not batch:
            break
        for c in batch:
            if timestamp(c['updated_at']) >= cutoff:
                conversations[str(c['id'])] = c
        if len(batch) < 60 or min(timestamp(c['updated_at']) for c in batch) < cutoff:
            break
        cursor = str(batch[-1]['id'])
        if cursor in cursors:
            raise ApiError('Conversation pagination stalled')
        cursors.add(cursor)
        params = {'order_by': 'updated_at', 'last_conversation_id': cursor}
    else:
        raise ApiError('Conversation pagination limit exceeded')
    for conversation in conversations.values():
        if conversation.get('type') not in ('INBOX', 'COMMENT'):
            continue
        try:
            messages, post = read_messages(client, page, conversation, cutoff)
            for message in messages:
                message = dict(message)
                if isinstance(message.get('original_message'), str):
                    message['message'] = message['original_message']
                message.setdefault('type', conversation['type'])
                event = {'page_id': page, 'event_type': 'messaging', 'data': {
                    'conversation': conversation, 'message': message,
                    'post': post or {'id': conversation.get('post_id')}}}
                if plan(config, event, now=now, validate_only=True):
                    store.enqueue(event)
        except (ApiError, ValueError, KeyError, TypeError, AttributeError) as error:
            LOG.error('page=%s conversation=%s read failed: %s; retry next cycle',
                      page, conversation.get('id'), type(error).__name__)


def run(config, store, live=False, once=False):
    client = Client(config['pages'])
    interval = config.get('poll_interval_seconds', 10)
    if not isinstance(interval, (int, float)) or interval < 1:
        raise ValueError('poll_interval_seconds must be at least 1')
    for page in config['pages'].values():
        if page.get('enabled', True):
            page_token(page)
    store.recover()
    LOG.info('Polling %s; interval=%ss; no webhook required', 'LIVE' if live else 'DRY RUN', interval)
    def drain():
        while process_one(store, config, live, sender=client.send):
            pass

    try:
        while True:
            for page, options in config['pages'].items():
                if not options.get('enabled', True):
                    continue
                try:
                    poll_page(store, config, client, page)
                except (ApiError, ValueError, KeyError, TypeError, AttributeError) as error:
                    LOG.error('page=%s poll failed: %s; retry next cycle', page,
                              str(error) if isinstance(error, ApiError) else type(error).__name__)
            with ThreadPoolExecutor(max_workers=config.get('worker_count', 1)) as workers:
                futures = [workers.submit(drain) for _ in range(config.get('worker_count', 1))]
                for future in futures:
                    future.result()
            from discounts import run_due
            run_due(store,config,client,live)
            if once:
                return
            time.sleep(interval)
    except KeyboardInterrupt:
        LOG.info('Polling stopped')

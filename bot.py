"""Pancake scripted replies. Python 3.11+, standard library only."""
import argparse
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timezone
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from diagnostics import register_secret, response_text, log_http_error, redact, setup

LOG = logging.getLogger("pancake")


def normalize(text):
    text = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def page_token(page):
    """Prefer the token saved in config; keep ENV compatibility."""
    token = page.get("page_access_token")
    if isinstance(token, str) and token.strip():
        register_secret(token.strip())
        return token.strip()
    token = os.environ.get(page.get("token_env") or "", "").strip()
    if not token:
        raise ValueError("Missing page_access_token in config.json (or token_env)")
    register_secret(token)
    return token


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not config.get("pages"):
        raise ValueError("pages must not be empty")
    for page_id, page in config["pages"].items():
        if page.get('sales_script'):
            script_path = Path(path).resolve().parent / page['sales_script']
            from sales import select_script
            page['_sales_script'] = select_script(json.loads(script_path.read_text(encoding='utf-8-sig')), page_id)
        from discounts import DEFAULTS, validate
        page['discount_followup'] = {**DEFAULTS, **page.get('discount_followup', {})}
        validate(page['discount_followup'])
        if page['discount_followup']['enabled'] and not page.get('_sales_script'):
            raise ValueError('discount_followup requires sales_script')
        if page.get('_sales_script'):
            page['_sales_script']['discount_followup'] = page['discount_followup']
        from llm import settings
        page['llm'] = settings(page.get('llm', {}))
        if page['llm']['enabled'] and not page.get('_sales_script'):
            raise ValueError('llm requires sales_script')
        names = set()
        for rule in page.get("rules", []):
            if rule["id"] in names:
                raise ValueError("Duplicate rule id")
            names.add(rule["id"])
            if rule.get("action", "reply") not in ("reply", "private_reply", "handoff"):
                raise ValueError("Invalid action")
            if rule.get("action") != "handoff" and not rule.get("reply"):
                raise ValueError("Reply text required")
    return config


def plan(config, event, state="start", now=None, validate_only=False, lead=None):
    """Return one matching action; ignore non-customer, old, removed events."""
    if not isinstance(event, dict) or event.get("event_type") != "messaging":
        return None
    page_id = str(event.get("page_id", ""))
    page = config["pages"].get(page_id)
    if not page or not page.get("enabled", True):
        return None
    data = event.get("data") or {}
    message, conversation = data.get("message") or {}, data.get("conversation") or {}
    sender = message.get("from") or {}
    customer = conversation.get("from") or {}
    if (not sender.get("id") or str(sender["id"]) == page_id
            or str(sender["id"]) != str(customer.get("id"))):
        return None
    if message.get("is_removed") or conversation.get("is_removed") or message.get("edit_history"):
        return None
    if not message.get("id") or not conversation.get("id") or not message.get("message"):
        return None
    if str(message.get("page_id", page_id)) != page_id:
        return None
    if str(message.get("conversation_id", conversation["id"])) != str(conversation["id"]):
        return None
    try:
        created = datetime.fromisoformat(message["inserted_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now or time.time()) - created.timestamp()
        if age < -60 or age > config.get("max_event_age_seconds", 600):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    if page.get("skip_assigned", True) and conversation.get("assignee_ids"):
        return None
    tags = {t.get('id') if isinstance(t, dict) else t for t in (conversation.get('tags') or []) if t is not None}
    if set(page.get("stop_tags", [])) & tags:
        return None
    # Older sales versions encoded completion/opt-out as human. Only those
    # explicit lead stages may re-enter the consent flow; true handoffs stay paused.
    if state == "human" and not (page.get('_sales_script') and (lead or {}).get('stage') in ('complete', 'stopped')):
        return None
    channel = message.get("type")
    if channel not in ("INBOX", "COMMENT"):
        return None
    if validate_only:
        return True
    text = normalize(message["message"])
    for rule in page.get("rules", []):
        if channel not in rule.get("channels", ["INBOX", "COMMENT"]):
            continue
        if rule.get("state", "*") not in ("*", state):
            continue
        keywords = rule.get("keywords", [])
        if keywords and not any(normalize(k) in text for k in keywords):
            continue
        action = rule.get("action", "reply")
        body = {"message": rule.get("reply", "").replace("{name}", sender.get("name") or "bạn")}
        if action == "handoff":
            body = None
        elif action == "private_reply":
            post_id = (data.get("post") or {}).get("id")
            if channel != "COMMENT" or not post_id or not message.get("can_reply_privately"):
                continue
            body.update(action="private_replies", post_id=post_id,
                        message_id=message["id"], from_id=sender["id"])
        elif channel == "COMMENT":
            if not message.get("can_comment"):
                continue
            body.update(action="reply_comment", message_id=message["id"])
        else:
            body.update(action="reply_inbox")
        return {"rule": rule["id"], "body": body,
                "next_state": "human" if action == "handoff" else rule.get("next_state", state)}
    return None


class Store:
    def __init__(self, path):
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                  id INTEGER PRIMARY KEY, page TEXT, conversation TEXT, message TEXT,
                  payload TEXT, status TEXT DEFAULT 'pending', result TEXT,
                  UNIQUE(page, conversation, message));
                CREATE TABLE IF NOT EXISTS states (
                  page TEXT, conversation TEXT, state TEXT, PRIMARY KEY(page, conversation));
                CREATE TABLE IF NOT EXISTS poll_starts (page TEXT PRIMARY KEY, started REAL);
                CREATE TABLE IF NOT EXISTS leads (
                  page TEXT, conversation TEXT, data TEXT, PRIMARY KEY(page, conversation));
                CREATE TABLE IF NOT EXISTS deliveries (
                  job INTEGER, step INTEGER, status TEXT, PRIMARY KEY(job, step));
                CREATE TABLE IF NOT EXISTS media (
                  page TEXT, url TEXT, content_id TEXT, PRIMARY KEY(page, url));
                CREATE TABLE IF NOT EXISTS followups (
                  page TEXT, conversation TEXT, message TEXT, customer_at REAL, scheduled_at REAL,
                  due REAL, reason TEXT, name TEXT, customer TEXT, status TEXT DEFAULT 'pending',
                  result TEXT, PRIMARY KEY(page,conversation));
                CREATE TABLE IF NOT EXISTS llm_calls (
                  page TEXT, conversation TEXT, message TEXT, day TEXT, status TEXT,
                  reserved_microusd INTEGER, charged_microusd INTEGER,
                  input_tokens INTEGER, output_tokens INTEGER, result TEXT,
                  PRIMARY KEY(page,conversation,message));
                CREATE TABLE IF NOT EXISTS sales_history (
                  page TEXT, conversation TEXT, session TEXT, stage TEXT,
                  data TEXT, archived_at TEXT, job INTEGER,
                  PRIMARY KEY(page,conversation,session));
            """)
            if 'session' not in {row[1] for row in db.execute('PRAGMA table_info(llm_calls)')}:
                db.execute("ALTER TABLE llm_calls ADD COLUMN session TEXT NOT NULL DEFAULT 'legacy'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, event):
        data = event["data"]
        with self.connect() as db:
            cursor = db.execute("INSERT OR IGNORE INTO jobs(page,conversation,message,payload) VALUES(?,?,?,?)",
                       (str(event["page_id"]), str(data["conversation"]["id"]),
                        str(data["message"]["id"]), json.dumps(event, ensure_ascii=False)))
            LOG.debug('page=%s conversation=%s message=%s queue=%s', event['page_id'],
                      data['conversation']['id'], data['message']['id'],
                      'new' if cursor.rowcount else 'duplicate_message_id')

    def state(self, page, conversation):
        with self.connect() as db:
            row = db.execute("SELECT state FROM states WHERE page=? AND conversation=?", (page, conversation)).fetchone()
            return row[0] if row else "start"

    def lead(self, page, conversation):
        with self.connect() as db:
            row = db.execute('SELECT data FROM leads WHERE page=? AND conversation=?', (page, conversation)).fetchone()
            return json.loads(row[0]) if row else {}

    def recover(self):
        with self.connect() as db:
            db.execute("INSERT INTO states SELECT page,conversation,'error' FROM jobs WHERE status='processing' ON CONFLICT(page,conversation) DO UPDATE SET state='error'")
            db.execute("UPDATE deliveries SET status='unknown' WHERE status='processing'")
            db.execute("UPDATE jobs SET status='unknown',result='Interrupted; inspect before retry' WHERE status='processing'")
            db.execute("UPDATE followups SET status='unknown',result='Interrupted; inspect before retry' WHERE status='processing'")
            # Migrate old technical pauses only when the latest non-ignored job failed.
            # Explicit handoff/opt-out/completion jobs are never reopened.
            rows = db.execute("""SELECT s.page,s.conversation,j.result FROM states s
                JOIN jobs j ON j.id=(SELECT max(id) FROM jobs
                    WHERE page=s.page AND conversation=s.conversation AND status!='ignored')
                WHERE s.state='human' AND j.status IN ('unknown','failed')""").fetchall()
            for row in rows:
                lead_row = db.execute('SELECT data FROM leads WHERE page=? AND conversation=?', (row['page'],row['conversation'])).fetchone()
                lead = json.loads(lead_row[0]) if lead_row else {}
                if lead.get('stage') in ('complete','stopped'):
                    continue
                db.execute("UPDATE states SET state='error' WHERE page=? AND conversation=?", (row['page'],row['conversation']))
                if lead.get('stage') == 'human':
                    lead['stage'] = 'error'
                    db.execute('UPDATE leads SET data=? WHERE page=? AND conversation=?',
                               (json.dumps(lead,ensure_ascii=False),row['page'],row['conversation']))
                LOG.info('page=%s conversation=%s migrated technical pause human->error; waiting for NEW message, no job replay', row['page'],row['conversation'])

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='processing' WHERE id=?", (row["id"],))
            return row

    def finish(self, job, status, result, state=None, lead=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,result=? WHERE id=?", (status, result, job["id"]))
            if state is not None:
                db.execute("INSERT INTO states VALUES(?,?,?) ON CONFLICT(page,conversation) DO UPDATE SET state=excluded.state",
                           (job["page"], job["conversation"], state))
            if lead is not None:
                old = db.execute('SELECT data FROM leads WHERE page=? AND conversation=?', (job['page'], job['conversation'])).fetchone()
                # Archive before replacing, including pre-upgrade completed leads.
                for snapshot in ([json.loads(old[0])] if old else []) + [lead]:
                    if snapshot.get('stage') in ('complete', 'stopped'):
                        db.execute('INSERT OR IGNORE INTO sales_history VALUES(?,?,?,?,?,?,?)',
                                   (job['page'], job['conversation'], snapshot.get('session_id', 'legacy'),
                                    snapshot['stage'], json.dumps(snapshot, ensure_ascii=False),
                                    datetime.now(timezone.utc).isoformat(), job['id']))
                db.execute('INSERT INTO leads VALUES(?,?,?) ON CONFLICT(page,conversation) DO UPDATE SET data=excluded.data',
                           (job['page'], job['conversation'], json.dumps(lead, ensure_ascii=False)))


def send(page, conversation, token, body):
    register_secret(token)
    started = time.monotonic()
    context = f'page={page} conversation={conversation} action={body.get("action")}'
    LOG.info('%s send_start fields=%s text_length=%s content_ids=%s', context, list(body), len(body.get('message', '')), body.get('content_ids', []))
    url = ("https://pages.fm/api/public_api/v1/pages/" + quote(page, safe="")
           + "/conversations/" + quote(conversation, safe="") + "/messages?"
           + urlencode({"page_access_token": token}))
    request = Request(url, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            LOG.error('%s send_invalid_json response=%s', context, response_text(raw.decode('utf-8', errors='replace')))
            raise
        LOG.info('%s send_response elapsed=%.2fs response=%s', context, time.monotonic()-started, response_text(data))
        if data.get("success") is True:
            return "sent"
        return "failed"
    except HTTPError as error:
        log_http_error(LOG, context + ' send', error)
        return "unknown" if error.code >= 500 else "failed"
    except (URLError, TimeoutError, OSError, ValueError):
        LOG.exception('%s send_failed elapsed=%.2fs (will not retry automatically)', context, time.monotonic()-started)
        # A timeout can mean the message was sent. Never retry automatically.
        return "unknown"


def process_one(store, config, live=False, sender=send):
    job = store.claim()
    if not job:
        return False
    index = None
    LOG.info('job=%s page=%s conversation=%s message=%s process_start', job['id'], job['page'], job['conversation'], job['message'])
    try:
        event = json.loads(job['payload'])
        state = store.state(job['page'], job['conversation'])
        page = config['pages'][job['page']]
        if state == 'error':
            LOG.info('job=%s new_message_after_error; previous jobs will not be retried', job['id'])
        if page.get('_sales_script'):
            from sales import decide
            previous = store.lead(job['page'], job['conversation'])
            decision = (decide(page['_sales_script'], event, previous)
                        if plan(config, event, state, validate_only=True, lead=previous) else None)
            if decision and page.get('llm', {}).get('enabled'):
                from llm import assist, trace
                previous = store.lead(job['page'], job['conversation'])
                assistance = assist(store, page, event, previous, decision, live=live)
                decision = decide(page['_sales_script'], event, previous,
                                  {**(assistance or {}), 'keep_clarifying': True})
                trace(event, 'llm_final_decision', {
                    'previous_stage': previous.get('stage', 'start'),
                    'stage': decision['lead'].get('stage') if decision else None,
                    'assistance_used': assistance,
                    'replies': decision.get('bodies', []) if decision else [],
                    'delivery': 'planned; inspect send_status for delivery' if live else 'dry_run; not sent'})
        else:
            decision = plan(config, event, state)
        if not decision:
            conversation = event.get('data', {}).get('conversation', {})
            lead_stage = store.lead(job['page'], job['conversation']).get('stage')
            reason = (f'No reply: state={state}, lead_stage={lead_stage}, '
                      f'assigned={bool(conversation.get("assignee_ids"))}, '
                      f'skip_assigned={page.get("skip_assigned", True)}; '
                      'check age/sender/channel/stop_tags and matching rule')
            store.finish(job, "ignored", reason)
            LOG.info('job=%s ignored reason=%s', job['id'], reason)
            return True
        status = "handoff" if decision["body"] is None else "dry_run"
        if live and decision["body"] is not None:
            token = page_token(config["pages"][job["page"]])
            from media import prepare
            for index, body in enumerate(decision.get('bodies', [decision['body']])):
                LOG.info('job=%s step=%s/%s action=%s photos=%s prepare_start', job['id'], index+1,
                         len(decision.get('bodies', [decision['body']])), body.get('action'), len(body.get('photos', [])))
                with store.connect() as db:
                    db.execute('INSERT INTO deliveries VALUES(?,?,?)', (job['id'], index, 'processing'))
                prepared = prepare(store, job['page'], token, body)
                status = sender(job["page"], job["conversation"], token, prepared)
                LOG.info('job=%s step=%s send_status=%s', job['id'], index+1, status)
                with store.connect() as db:
                    db.execute('UPDATE deliveries SET status=? WHERE job=? AND step=?', (status, job['id'], index))
                if status != 'sent':
                    break
                if len(decision.get('bodies', [])) > 1:
                    time.sleep(.3)
        accepted = status in ('sent', 'handoff', 'dry_run')
        failed_lead = dict(decision['lead'], stage='error') if decision.get('lead') else None
        if page.get('_sales_script') and (previous.get('stage') in ('complete', 'stopped', 'reorder_confirm', 'contact_reuse_confirm')
                                          or decision.get('new_session') or decision.get('lead', {}).get('stage') == 'complete'):
            # A failed send must not consume consent or replace the old purchase.
            failed_lead = previous
        store.finish(job, status, json.dumps(decision, ensure_ascii=False),
                     decision['next_state'] if accepted else ('error' if page.get('_sales_script') else None),
                     decision.get('lead') if accepted else failed_lead)
        if accepted and decision.get('new_session'):
            with store.connect() as db:
                db.execute("UPDATE followups SET status='cancelled',result='New purchase session' WHERE page=? AND conversation=?", (job['page'], job['conversation']))
        if accepted and decision.get('lead'):
            from discounts import schedule
            schedule(store,job['page'],event,decision['lead'],page.get('discount_followup',{}))
        LOG.info("job=%s status=%s rule=%s", job["id"], status, decision["rule"])
    except Exception as error:
        LOG.exception('job=%s page=%s conversation=%s step=%s processing_failed; waiting for new customer message',
                      job['id'], job['page'], job['conversation'], None if index is None else index+1)
        if index is not None:
            with store.connect() as db:
                db.execute("UPDATE deliveries SET status='unknown' WHERE job=? AND step=?", (job['id'], index))
        store.finish(job, 'unknown', redact(f'{type(error).__name__}: {error}')[:2000], 'error')
    return True


def serve(config, store, host, port, live):
    secret = os.environ.get("PANCAKE_WEBHOOK_SECRET", "")
    register_secret(secret)
    if len(secret) < 32:
        raise ValueError("Set PANCAKE_WEBHOOK_SECRET to a random secret of at least 32 characters")
    if live:
        for page in config["pages"].values():
            if page.get("enabled", True):
                page_token(page)
    store.recover()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # The callback path contains a secret.

        def respond(self, code):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}' if code == 200 else b'{"ok":false}')

        def do_GET(self):
            self.respond(200 if self.path == "/health" else 404)

        def do_POST(self):
            if not hmac.compare_digest(self.path, "/webhooks/pancake/" + secret):
                self.respond(404)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1_000_000:
                    self.respond(413)
                    return
                event = json.loads(self.rfile.read(length))
                # Validate before persisting; plan is evaluated again by worker.
                if plan(config, event, validate_only=True):
                    store.enqueue(event)
                self.respond(200)
            except (ValueError, TypeError, KeyError, AttributeError):
                self.respond(400)
            except sqlite3.Error:
                self.respond(503)

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                worked = process_one(store, config, live)
            except sqlite3.Error:
                LOG.error("Database unavailable")
                worked = False
            stop.wait(0.25 if worked else 0.5)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((host, port), Handler)
    LOG.info("Listening on %s:%s mode=%s", host, port, "LIVE" if live else "DRY RUN")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        stop.set()
        thread.join(timeout=25)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["poll", "serve", "simulate", "status", "leads", "orders", "followups", "llm-usage", "reset-state"])
    parser.add_argument("--once", action="store_true")
    parser.add_argument('--log-level', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'], default='INFO')
    parser.add_argument('--log-file', default='logs/pancake.log')
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--db")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--event")
    parser.add_argument("--page")
    parser.add_argument("--conversation")
    args = parser.parse_args()
    setup(args.log_level, args.log_file)
    if args.command == "simulate":
        if not args.event:
            parser.error("--event required")
        config = load_config(args.config)
        event = json.loads(Path(args.event).read_text(encoding='utf-8-sig'))
        page = config['pages'].get(str(event.get('page_id')), {})
        if page.get('_sales_script') and plan(config, event, validate_only=True):
            from sales import decide
            decision = decide(page['_sales_script'], event, {})
        else:
            decision = plan(config, event)
        print(json.dumps(decision, ensure_ascii=False, indent=2))
        return
    store = Store(args.db or ("live.sqlite3" if args.live else "dry-run.sqlite3"))
    if args.command == "status":
        with store.connect() as db:
            for row in db.execute("SELECT id,page,conversation,message,status,result FROM jobs ORDER BY id DESC LIMIT 30"):
                print(json.dumps(dict(row), ensure_ascii=False))
    elif args.command == 'orders':
        with store.connect() as db:
            for row in db.execute('SELECT * FROM sales_history ORDER BY archived_at DESC'):
                print(json.dumps(dict(row), ensure_ascii=False))
    elif args.command == 'llm-usage':
        with store.connect() as db:
            for row in db.execute('SELECT page,day,status,count(*) AS calls, sum(charged_microusd)/1000000.0 AS budget_used_usd, sum(input_tokens) AS input_tokens, sum(output_tokens) AS output_tokens FROM llm_calls GROUP BY page,day,status ORDER BY day DESC,page,status'):
                print(json.dumps(dict(row), ensure_ascii=False))
    elif args.command == 'followups':
        with store.connect() as db:
            for row in db.execute('SELECT page,conversation,due,reason,status,result FROM followups ORDER BY due'):
                print(json.dumps(dict(row),ensure_ascii=False))
    elif args.command == 'leads':
        with store.connect() as db:
            for row in db.execute('SELECT * FROM leads'):
                print(json.dumps(dict(row), ensure_ascii=False))
    elif args.command == "reset-state":
        if not args.page or not args.conversation:
            parser.error("--page and --conversation required")
        with store.connect() as db:
            db.execute("DELETE FROM states WHERE page=? AND conversation=?", (args.page, args.conversation))
            db.execute('DELETE FROM leads WHERE page=? AND conversation=?', (args.page, args.conversation))
    else:
        if args.command == "poll":
            from polling import run
            run(load_config(args.config), store, args.live, args.once)
        else:
            serve(load_config(args.config), store, args.host, args.port, args.live)


if __name__ == "__main__":
    main()

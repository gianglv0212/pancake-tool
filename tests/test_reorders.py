import json
import sqlite3
from pathlib import Path
import unittest
from unittest.mock import patch

import test_bot
from test_bot import event
from bot import Store, process_one
from sales import decide
from llm import reserve, settings
from discounts import schedule


class ReorderTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown

    def script(self):
        document = json.loads(Path('sales-script.json').read_text(encoding='utf-8-sig'))
        return next(iter(document['pages'].values()))

    def seed(self, stage='complete', state='human'):
        self.config['pages']['p1'].update(_sales_script=self.script(), page_access_token='fake')
        lead = dict(stage=stage, introduced=True, size='M', phone='0912345678',
                    address='12 đường A phường B thành phố Hà Nội', weight_kg=55, repeat_count=2)
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO leads VALUES(?,?,?)', ('p1', 'c1', json.dumps(lead)))
            db.execute('INSERT OR REPLACE INTO states VALUES(?,?,?)', ('p1', 'c1', state))
        return lead

    def incoming(self, text, mid, **overrides):
        payload = event(text, message_id=mid)
        payload['data']['conversation'].update(overrides)
        self.store.enqueue(payload)
        process_one(self.store, self.config)
        return self.store.lead('p1', 'c1')

    def test_legacy_completion_reorder_contact_consent_and_two_snapshots(self):
        original = self.seed()
        lead = self.incoming('chị muốn mua lại', 'buy')
        self.assertEqual(lead['stage'], 'reorder_confirm')
        self.assertEqual(lead['size'], 'M')
        with self.store.connect() as db:
            snapshot = db.execute('SELECT data FROM sales_history').fetchone()[0]
            self.assertEqual(json.loads(snapshot), original)
        lead = self.incoming('đúng rồi', 'new')
        self.assertEqual(lead['stage'], 'size')
        self.assertEqual(lead['session_id'], 'reorder:new')
        self.assertEqual(lead['repeat_count'], 0)
        for field in ('size', 'phone', 'address', 'weight_kg', 'color'):
            self.assertNotIn(field, lead)
        lead = self.incoming('size L', 'size')
        self.assertEqual(lead['stage'], 'contact_reuse_confirm')
        self.assertNotIn('phone', lead)
        lead = self.incoming('ok', 'reuse')
        self.assertEqual(lead['stage'], 'confirm')
        self.assertEqual(lead['phone'], original['phone'])
        lead = self.incoming('đúng rồi', 'finish')
        self.assertEqual(lead['stage'], 'complete')
        self.assertEqual(self.store.state('p1', 'c1'), 'complete')
        self.incoming('đúng rồi', 'finish')  # duplicate message
        with self.store.connect() as db:
            rows = db.execute('SELECT session,data FROM sales_history ORDER BY session').fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(json.loads(rows[0]['data']), original)
            self.assertEqual(json.loads(rows[1]['data'])['size'], 'L')

    def test_declining_new_order_preserves_original_and_deduplicates_archive(self):
        original = self.seed()
        self.incoming('mua thêm', 'request')
        lead = self.incoming('không', 'decline')
        self.assertEqual(lead['stage'], 'complete')
        self.assertEqual(lead['address'], original['address'])
        self.assertNotIn('session_id', lead)
        self.incoming('mua lại', 'request2')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sales_history').fetchone()[0], 1)

    def test_manual_new_contact_and_color_selection(self):
        self.seed()
        self.config['pages']['p1']['_sales_script']['product']['colors'] = ['Đỏ', 'Xanh']
        self.incoming('đặt lại mẫu này', 'request')
        self.incoming('ok', 'new')
        self.assertEqual(self.incoming('L', 'size')['stage'], 'color')
        self.assertEqual(self.incoming('màu xanh', 'color')['stage'], 'contact_reuse_confirm')
        lead = self.incoming('không', 'no-reuse')
        self.assertEqual(lead['stage'], 'phone')
        self.assertNotIn('previous_contact', lead)
        self.incoming('0987654321', 'phone')
        lead = self.incoming('15 đường C phường D tỉnh E', 'address')
        self.assertEqual(lead['stage'], 'confirm')
        self.assertEqual(lead['phone'], '0987654321')

    def test_old_order_support_and_no_accidental_reopen(self):
        for text in ['đơn chị đến đâu rồi?', 'đổi địa chỉ đơn vừa rồi', 'hủy đơn',
                     'mua thêm nhưng đổi size đơn cũ giúp chị']:
            with self.subTest(text=text):
                result = decide(self.script(), event(text), {'stage': 'complete', 'size': 'M'})
                self.assertEqual(result['lead']['stage'], 'human')
                self.assertEqual(result['lead']['size'], 'M')
        for text in ['ok', '👍', 'chị không mua lại đâu']:
            self.assertIsNone(decide(self.script(), event(text), {'stage': 'complete'}))

    def test_price_browsing_keeps_previous_order(self):
        original = self.seed()
        lead = self.incoming('xin giá', 'browse')
        self.assertEqual(lead, original)
        self.assertEqual(self.store.state('p1', 'c1'), 'complete')

    def test_stopped_requires_explicit_purchase_request(self):
        self.seed(stage='stopped')
        self.assertEqual(self.incoming('xin giá', 'ignored')['stage'], 'stopped')
        self.assertEqual(self.incoming('mua lại', 'request')['stage'], 'reorder_confirm')
        self.assertEqual(self.incoming('không', 'no')['stage'], 'stopped')
        self.incoming('chị mua 1 bộ nữa', 'request2')
        self.assertEqual(self.incoming('ok', 'yes')['stage'], 'size')

    def test_staff_handoff_assignment_and_stop_tags_still_block(self):
        original = self.seed(stage='human')
        self.assertEqual(self.incoming('mua lại', 'human'), original)
        original = self.seed()
        self.assertEqual(self.incoming('mua lại', 'assigned', assignee_ids=['staff']), original)
        self.config['pages']['p1']['stop_tags'] = [123]
        self.assertEqual(self.incoming('mua lại', 'tagged', tags=[123]), original)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sales_history').fetchone()[0], 0)

    def test_send_failure_does_not_consume_reorder_consent(self):
        self.seed()
        self.incoming('mua lại', 'request')
        before = self.store.lead('p1', 'c1')
        self.store.enqueue(event('ok', message_id='yes-fails'))
        with patch('media.prepare', side_effect=lambda s,p,t,b: b):
            process_one(self.store, self.config, live=True, sender=lambda *a: 'failed')
        self.assertEqual(self.store.lead('p1', 'c1'), before)
        self.assertEqual(self.incoming('ok', 'retry-new-id')['stage'], 'size')

    def test_pending_reorder_and_contact_confirmation_skip_llm(self):
        self.seed()
        self.config['pages']['p1']['llm']['enabled'] = True
        self.incoming('mua lại', 'request')
        with patch('llm.request_model') as model:
            self.incoming('ờ để xem', 'unsure')
            self.incoming('ok', 'new')
            self.incoming('L', 'size')
            self.incoming('ừm', 'unsure-contact')
            model.assert_not_called()

    def test_repeat_limit_and_no_discount_while_awaiting_consent(self):
        self.seed()
        self.incoming('mua lại', 'request')
        for i in range(3):
            lead = self.incoming('???', str(i))
        self.assertEqual(lead['stage'], 'human')
        options = {'enabled': True}
        for stage in ('reorder_confirm', 'contact_reuse_confirm'):
            schedule(self.store, 'p1', event(), {'stage': stage}, options)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM followups').fetchone()[0], 0)

    def test_llm_session_allowance_resets_but_daily_budget_does_not(self):
        self.seed()
        options = settings({'daily_budget_usd': .003, 'max_calls_per_conversation': 1})
        self.assertTrue(reserve(self.store, event(message_id='old-call'), options, 1000))
        self.assertFalse(reserve(self.store, event(message_id='old-limit'), options, 1000))
        self.incoming('mua lại', 'request')
        self.incoming('ok', 'new')
        self.assertTrue(reserve(self.store, event(message_id='new-call'), options, 1000))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT sum(charged_microusd) FROM llm_calls').fetchone()[0], 2000)
        options['max_calls_per_conversation'] = 10
        self.assertFalse(reserve(self.store, event(message_id='budget'), options, 1001))

    def test_existing_llm_database_migrates_without_losing_usage(self):
        path = str(Path(self.tmp.name) / 'legacy.db')
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE llm_calls(page TEXT,conversation TEXT,message TEXT,day TEXT,status TEXT,reserved_microusd INTEGER,charged_microusd INTEGER,input_tokens INTEGER,output_tokens INTEGER,result TEXT,PRIMARY KEY(page,conversation,message))')
            db.execute("INSERT INTO llm_calls VALUES('p1','c1','old','2026-01-01','failed',1000,1000,NULL,NULL,NULL)")
        db.close()
        migrated = Store(path)
        with migrated.connect() as db:
            row = db.execute('SELECT session,charged_microusd FROM llm_calls').fetchone()
            self.assertEqual(tuple(row), ('legacy', 1000))


if __name__ == '__main__':
    unittest.main()

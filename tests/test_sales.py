import json
import unittest
from unittest.mock import patch
import test_bot
from test_bot import event
from bot import process_one
from sales import decide, extract
from media import prepare
from email.message import Message


class SalesTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown

    def script(self):
        with open('sales-script.json', encoding='utf-8') as file:
            return json.load(file)['pages']['122526837503326']

    def test_colors_and_correction(self):
        script = self.script()
        script['product']['colors'] = ['Đỏ', 'Xanh', 'Trắng']
        lead = {}
        for message, stage in [('size M', 'color'), ('đỏ hay xanh?', 'color'),
                               ('không lấy đỏ', 'color'),
                               ('đỏ', 'phone'), ('0912345678', 'address'),
                               ('12 đường A, phường B, tỉnh C', 'confirm'),
                               ('đổi màu xanh', 'confirm'), ('đúng rồi', 'complete')]:
            result = decide(script, event(message), lead)
            lead = result['lead']
            self.assertEqual(lead['stage'], stage, message)
            if stage == 'confirm':
                self.assertIn(lead['color'], result['body']['message'])
        self.assertEqual(lead['color'], 'Xanh')

    def test_color_early_and_address_not_color(self):
        from sales import extract_color
        script = self.script()
        script['product']['colors'] = ['Đỏ', 'Trắng']
        result = decide(script, event('size L; màu trắng; SĐT 0912345678'), {})
        self.assertEqual(result['lead']['color'], 'Trắng')
        self.assertEqual(result['lead']['stage'], 'address')
        self.assertNotIn('color', extract_color('12 đường Đỏ, phường B, tỉnh C', {}, ['Đỏ']))
        changed = extract_color('đổi màu tím', {'color': 'Đỏ'}, ['Đỏ'])
        self.assertNotIn('color', changed)

    def test_load_page_scripts_and_missing_page(self):
        from pathlib import Path
        from bot import load_config
        scripts = {'pages': {'p1': self.script(), 'p2': self.script()}}
        scripts['pages']['p1']['product']['colors'] = ['Đỏ']
        scripts['pages']['p2']['prompts']['phone'] = 'Page 2 phone'
        root = Path(self.tmp.name)
        (root / 'scripts.json').write_text(json.dumps(scripts), encoding='utf-8')
        cfg = {'pages': {p: {'sales_script': 'scripts.json'} for p in ('p1', 'p2')}}
        (root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        loaded = load_config(root / 'config.json')['pages']
        self.assertEqual(loaded['p1']['_sales_script']['product']['colors'], ['Đỏ'])
        self.assertEqual(loaded['p2']['_sales_script']['product']['colors'], [])
        self.assertEqual(loaded['p2']['_sales_script']['prompts']['phone'], 'Page 2 phone')
        cfg['pages']['p3'] = {'sales_script': 'scripts.json'}
        (root / 'config.json').write_text(json.dumps(cfg), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'missing page p3'):
            load_config(root / 'config.json')

    def test_import_order_and_photos(self):
        groups = self.script()['groups']
        self.assertEqual(list(groups), ['0','1','2','3','4','5'])
        self.assertEqual(len(groups['2']), 4)
        self.assertEqual(len(groups['2'][1]['photos']), 3)
        self.assertIn('429k', groups['2'][0]['message'])

    def test_full_conversation(self):
        lead = {}
        for text, stage in [('bao nhiêu tiền', 'size'), ('chị 55kg cao 1m60', 'size_confirm'),
                            ('ok', 'phone'), ('0912 345 678', 'address'),
                            ('12 đường A, phường B, thành phố Hà Nội', 'confirm'), ('đúng rồi', 'complete')]:
            decision = decide(self.script(), event(text), lead)
            lead = decision['lead']
            self.assertEqual(lead['stage'], stage)
        self.assertEqual(lead['size'], 'M')
        self.assertEqual(lead['phone'], '0912345678')
        self.assertEqual(lead['height_cm'], 160)

    def test_all_fields_and_correction(self):
        first = decide(self.script(), event('size XL; SĐT 0912345678; địa chỉ: 12 đường A, phường B, tỉnh C'), {})
        self.assertEqual(first['lead']['stage'], 'confirm')
        changed = decide(self.script(), event('đổi size M'), first['lead'])
        self.assertEqual(changed['lead']['size'], 'M')
        self.assertEqual(changed['lead']['stage'], 'confirm')
        self.assertFalse(any('xin chiều cao' in b.get('message','') for b in first['bodies']))

    def test_ambiguous_invalid_and_missing_address(self):
        self.assertNotIn('size', extract('size M có vừa không?', {}))
        self.assertNotIn('size', extract('không lấy size M', {}))
        self.assertNotIn('size', extract('size M hay size L', {}))
        self.assertNotIn('phone', extract('0123456789', {}))
        self.assertNotIn('address', extract('Hà Nội', {'stage': 'address'}))
        self.assertNotIn('address', extract('12 đường A phường B', {'stage': 'address'}))
        self.assertEqual(extract('+84 912 345 678', {})['phone'], '0912345678')

    def test_comment_does_not_claim_private_message_sent(self):
        result = decide(self.script(), event('giá', 'COMMENT'), {})
        self.assertNotIn('đã gửi', result['body']['message'])
        self.assertNotIn('phone', result['lead'])
        self.assertIsNone(decide(self.script(), event('giá', 'COMMENT'), result['lead']))

    def test_handoff_and_optout(self):
        for text, stage in [('gọi cho chị', 'human'), ('không mua nữa', 'stopped')]:
            result = decide(self.script(), event(text), {})
            self.assertEqual(result['lead']['stage'], stage)
            self.assertEqual(len(result['bodies']), 1)

    def test_intent_negation_and_explicit_requests(self):
        for message in ['không cần gọi đâu, nhắn ở đây giúp chị',
                        'chị không muốn hủy đơn', 'không cần nhân viên đâu',
                        'chị không muốn gặp nhân viên',
                        'đừng gọi cho chị', 'không khiếu nại đâu',
                        'hủy đơn có mất phí không?', 'shop có cho hủy đơn không?']:
            with self.subTest(message=message):
                result = decide(self.script(), event(message), {})
                self.assertNotIn(result['lead']['stage'], ('human', 'stopped'))
        for message in ['ko mua nữa', 'chị không mua nữa nhé', 'hủy đơn giúp chị',
                        'dừng nhắn tin cho chị', 'không cần', 'nhân viên, hủy đơn giúp chị']:
            with self.subTest(message=message):
                self.assertEqual(decide(self.script(), event(message), {})['lead']['stage'], 'stopped')
        for message in ['gọi cho chị được không?', 'nhân viên', 'chị muốn khiếu nại']:
            self.assertEqual(decide(self.script(), event(message), {})['lead']['stage'], 'human')

    def test_natural_size_and_ambiguous_choices(self):
        for message, size in [('chị lấy M nhé', 'M'), ('M nhé!', 'M'),
                              ('cho chị size XL nha!', 'XL'), ('em chọn XXL ạ', '2XL'),
                              ('size M, ship bao lâu?', 'M')]:
            self.assertEqual(extract(message, {}).get('size'), size, message)
        for message in ['size M hay L', 'size M hoặc XL', 'size M hay size L', 'size M/L',
                        'size M có vừa không?', 'không lấy size M', 'size M; size L',
                        'chưa muốn lấy M', 'size M có vừa']:
            self.assertNotIn('size', extract(message, {}), message)

    def test_natural_confirmation_is_contextual(self):
        lead = dict(introduced=True, stage='confirm', size='M', phone='0912345678',
                    address='12 đường A, phường B, tỉnh C')
        for message in ['ok shop', 'đúng rồi em', 'dạ chính xác ạ!', '👍', 'chốt nhé shop']:
            self.assertEqual(decide(self.script(), event(message), lead)['lead']['stage'], 'complete', message)
            suggestion = dict(introduced=True, stage='size_confirm', suggested_size='M')
            self.assertEqual(decide(self.script(), event(message), suggestion)['lead']['size'], 'M')
            self.assertNotEqual(decide(self.script(), event(message), {})['lead']['stage'], 'complete')
        for message in ['ok nhưng đổi size L', 'chưa đúng rồi em', 'đúng rồi?', 'ok shop, đổi địa chỉ']:
            self.assertNotEqual(decide(self.script(), event(message), lead)['lead']['stage'], 'complete', message)

    def test_repeated_unresolved_input_handoff_and_reset(self):
        lead = dict(introduced=True, stage='phone', size='M')
        for count in range(1, 4):
            result = decide(self.script(), event('???'), lead)
            lead = result['lead']
            self.assertEqual(lead['repeat_count'], count)
            self.assertEqual(lead['stage'], 'phone' if count < 3 else 'human')
        self.assertEqual(result['next_state'], 'human')
        self.assertEqual(len(result['bodies']), 1)
        self.assertEqual(lead['handoff_reason'], 'repeated_unresolved_input')
        self.assertIsNone(decide(self.script(), event('xin giá'), lead))
        # Useful early information or a correction resets the counter even at the same step.
        previous = dict(introduced=True, stage='size', repeat_count=2)
        result = decide(self.script(), event('0912345678'), previous)
        self.assertEqual(result['lead']['stage'], 'size')
        self.assertEqual(result['lead']['repeat_count'], 0)
        previous = dict(introduced=True, stage='phone', size='M', repeat_count=2)
        result = decide(self.script(), event('0912345678'), previous)
        self.assertEqual(result['lead']['stage'], 'address')
        self.assertEqual(result['lead']['repeat_count'], 0)

    def test_repeat_counter_persists_and_duplicate_does_not_count(self):
        self.config['pages']['p1']['_sales_script'] = self.script()
        for mid, message in [('first', 'size M'), ('retry1', '???'), ('retry1', '???'),
                             ('retry2', '???'), ('retry3', '???')]:
            self.store.enqueue(event(message, message_id=mid))
            process_one(self.store, self.config)
        self.assertEqual(self.store.lead('p1', 'c1')['repeat_count'], 3)
        self.assertEqual(self.store.state('p1', 'c1'), 'human')

    def test_persisted_lead_and_dry_run_no_upload(self):
        self.config['pages']['p1']['_sales_script'] = self.script()
        self.store.enqueue(event('size L'))
        with patch('media.prepare', side_effect=AssertionError('No upload in dry run')):
            process_one(self.store, self.config)
        self.assertEqual(self.store.lead('p1', 'c1')['size'], 'L')
        self.assertEqual(self.store.lead('p1', 'c1')['stage'], 'phone')

    def test_sequence_failure_stops_and_hands_off(self):
        self.config['pages']['p1'].update(_sales_script=self.script(), page_access_token='test')
        self.store.enqueue(event('giá'))
        calls = []
        def sender(*args):
            calls.append(args[-1])
            return 'sent' if len(calls) == 1 else 'unknown'
        with patch('media.prepare', side_effect=lambda store,page,token,body: body), patch('bot.time.sleep'):
            process_one(self.store, self.config, True, sender)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.store.state('p1', 'c1'), 'error')
        self.assertFalse(process_one(self.store, self.config, True, sender))

    def test_media_upload_cache_and_payload(self):
        class Response:
            def __init__(self, payload, mime='application/json'):
                self.payload = payload
                self.headers = Message()
                self.headers['Content-Type'] = mime
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return self.payload
        for field in ('attachment_type', 'type'):
            with self.subTest(field=field):
                body = {'action': 'reply_inbox', 'photos': [f'https://content.pancake.vn/{field}.jpg']}
                uploaded = {'success': True, 'id': 'photo1', field: 'PHOTO', 'page_id': 'p1'}
                responses = [Response(b'image', 'image/jpeg'), Response(json.dumps(uploaded).encode())]
                with patch('media.urlopen', side_effect=responses) as request, patch('media.time.sleep'):
                    prepared = prepare(self.store, 'p1', 'test-token', body)
                    self.assertEqual(prepared, {'action': 'reply_inbox', 'content_ids': ['photo1']})
                    upload = request.call_args.args[0]
                    self.assertIn(b'name="file"', upload.data)
                    self.assertEqual(prepare(self.store, 'p1', 'test-token', body), prepared)
                    self.assertEqual(request.call_count, 2)
        for uploaded in ({'success': True, 'id': 'x', 'type': 'VIDEO'},
                         {'success': False, 'id': 'x', 'type': 'PHOTO'},
                         {'success': True, 'id': '', 'type': 'PHOTO'}, []):
            with self.subTest(invalid=uploaded):
                body = {'action': 'reply_inbox', 'photos': ['https://content.pancake.vn/invalid.jpg']}
                responses = [Response(b'image', 'image/jpeg'), Response(json.dumps(uploaded).encode())]
                with patch('media.urlopen', side_effect=responses), patch('media.time.sleep'):
                    with self.assertRaises(ValueError):
                        prepare(self.store, 'p1', 'test-token', body)

    def test_interrupted_sequence_blocks_conversation(self):
        self.store.enqueue(event('giá'))
        job = self.store.claim()
        with self.store.connect() as db:
            db.execute('INSERT INTO deliveries VALUES(?,?,?)', (job['id'], 0, 'processing'))
        self.store.recover()
        self.assertEqual(self.store.state('p1', 'c1'), 'error')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM deliveries').fetchone()[0], 'unknown')

    def test_identical_text_with_new_ids_replies_twice(self):
        self.config['pages']['p1']['_sales_script'] = self.script()
        for mid in ['price1', 'price2']:
            self.store.enqueue(event('xin giá', message_id=mid))
            process_one(self.store, self.config)
        self.store.enqueue(event('xin giá', message_id='price2'))
        self.assertFalse(process_one(self.store, self.config))
        with self.store.connect() as db:
            jobs = db.execute('SELECT status,result FROM jobs ORDER BY id').fetchall()
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertEqual(job['status'], 'dry_run')
            self.assertIn('429k', json.loads(job['result'])['bodies'][0]['message'])

    def test_new_price_message_after_media_exception(self):
        self.config['pages']['p1'].update(_sales_script=self.script(), page_access_token='test')
        self.store.enqueue(event('xin giá', message_id='before'))
        with patch('media.prepare', side_effect=ValueError('upload failed')):
            process_one(self.store, self.config, True)
        self.store.enqueue(event('xin giá', message_id='after'))
        process_one(self.store, self.config)
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT status FROM jobs ORDER BY id')], ['unknown','dry_run'])

    def test_legacy_error_migrates_but_handoff_stays(self):
        self.store.enqueue(event('xin giá'))
        job = self.store.claim()
        self.store.finish(job, 'unknown', 'Old processing error', 'human', {'stage':'human'})
        self.store.recover()
        self.assertEqual(self.store.state('p1','c1'), 'error')
        self.assertEqual(self.store.lead('p1','c1')['stage'], 'error')
        self.store.enqueue(event('nhân viên', message_id='handoff'))
        job = self.store.claim()
        self.store.finish(job, 'sent', 'Explicit handoff', 'human', {'stage':'human'})
        self.store.recover()
        self.assertEqual(self.store.state('p1','c1'), 'human')


if __name__ == '__main__':
    unittest.main()

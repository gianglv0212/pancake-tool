import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from bot import Store, load_config, process_one
import test_bot
from test_bot import event
from sales import decide, select_script
from llm import assist, settings, reserve, validate_result, request_model


def interpretation(updates=None, intent='inform', unclear=False, reply=None, evidence=''):
    return dict(intent=intent, updates=updates or [], needs_clarification=unclear,
                evidence=evidence,
                reply=reply if reply is not None else ('Chị đang phân vân giữa những size nào ạ?' if unclear else ''))


def update(field, value, evidence):
    return dict(field=field, value=value, evidence=evidence)


def response(result):
    return {'status': 'completed', 'usage': {'input_tokens': 1000, 'output_tokens': 100},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}


class LLMTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown

    def page(self, **options):
        document = json.loads(Path('sales-script.json').read_text(encoding='utf-8-sig'))
        script = select_script(document, next(iter(document['pages'])))
        return {'_sales_script': script, 'llm': settings({'enabled': True, **options})}

    def call(self, text='55', previous=None, page=None, live=True, mid='m1'):
        page = page or self.page()
        previous = previous or {'stage': 'size', 'introduced': True}
        payload = event(text, message_id=mid)
        return assist(self.store, page, payload, previous, decide(page['_sales_script'], payload, previous), live)

    def test_disabled_dry_run_missing_key_and_clear_fields_never_call(self):
        with patch('llm.request_model') as model, patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.call(page=self.page(enabled=False)))
            self.assertIsNone(self.call(live=False))
            self.assertIsNone(self.call())
            model.assert_not_called()
        with patch('llm.request_model') as model, patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}):
            for text in ['size M', '0912345678', 'ok shop', 'nhân viên', 'không mua', 'xin giá']:
                previous = dict(stage='phone', size='M', introduced=True) if text == '0912345678' else None
                self.assertIsNone(self.call(text, previous))
            model.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM llm_calls').fetchone()[0], 0)

    def test_successful_weight_fallback_and_request_contract(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model', return_value=response(
                interpretation([update('weight_kg', '55', '55')]))) as model:
            result = self.call()
            self.assertEqual(result['updates'], {'weight_kg': 55.0})
            body = json.loads(model.call_args.args[0])
            self.assertFalse(body['store'])
            self.assertEqual(body['text']['format']['type'], 'json_schema')
            self.assertTrue(body['text']['format']['strict'])
            self.assertNotIn('tools', body)
            self.assertNotIn('fake', model.call_args.args[0].decode())
            self.assertIsNone(self.call())
            self.assertEqual(model.call_count, 1)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM llm_calls').fetchone()
            self.assertEqual(row['status'], 'accepted')
            self.assertEqual(row['charged_microusd'], 325)

    def test_config_key_works_without_env_and_takes_priority(self):
        from diagnostics import redact
        for index, environment in enumerate(({}, {'OPENAI_API_KEY': 'unused-env-key'})):
            with patch.dict(os.environ, environment, clear=True), patch('llm.request_model', return_value=response(
                    interpretation([update('weight_kg', '55', '55')]))) as model:
                result = self.call(page=self.page(api_key='  config-test-secret  '), mid='config-' + str(index))
                self.assertEqual(result['updates']['weight_kg'], 55)
                self.assertEqual(model.call_args.args[1], 'config-test-secret')
                self.assertNotIn('config-test-secret', model.call_args.args[0].decode())
        self.assertNotIn('config-test-secret', redact('key=config-test-secret'))
        with self.store.connect() as db:
            for row in db.execute('SELECT result FROM llm_calls'):
                self.assertNotIn('config-test-secret', row[0])

    def test_blank_config_key_falls_back_to_env(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'env-test-secret'}), patch('llm.request_model', return_value=response(interpretation())) as model:
            self.call(page=self.page(api_key='  '))
            self.assertEqual(model.call_args.args[1], 'env-test-secret')

    def test_budget_and_call_limit_survive_reopen(self):
        options = settings({'daily_budget_usd': .001, 'max_calls_per_conversation': 1})
        self.assertTrue(reserve(self.store, event(), options, 1000))
        self.assertFalse(reserve(Store(self.store.path), event(message_id='m2'), options, 1))
        other = event(message_id='m3')
        other['data']['conversation']['id'] = 'c2'
        self.assertFalse(reserve(self.store, other, options, 1))

    def test_budget_day_and_page_are_independent(self):
        options = settings({'daily_budget_usd': .001})
        self.assertTrue(reserve(self.store, event(), options, 1000))
        other = event(message_id='other')
        other['page_id'] = 'p2'
        self.assertTrue(reserve(self.store, other, options, 1000))
        with self.store.connect() as db:
            db.execute("UPDATE llm_calls SET day='2000-01-01' WHERE page='p1'")
        self.assertTrue(reserve(self.store, event(message_id='new-day'), options, 1000))

    def test_concurrent_reservations_cannot_overspend(self):
        options = settings({'daily_budget_usd': .001})
        def attempt(mid):
            return reserve(self.store, event(message_id=str(mid)), options, 1000)
        with ThreadPoolExecutor(max_workers=4) as workers:
            self.assertEqual(sum(workers.map(attempt, range(4))), 1)

    def test_timeout_retains_reservation_and_falls_back(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model', side_effect=TimeoutError) as model:
            self.assertIsNone(self.call())
            self.assertIsNone(self.call())
            self.assertEqual(model.call_count, 1)
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM llm_calls').fetchone()
            self.assertEqual(row['status'], 'failed')
            self.assertEqual(row['charged_microusd'], row['reserved_microusd'])

    def test_malformed_incomplete_and_refusal_fall_back(self):
        variants = [[], {}, {'status': 'incomplete'}, response({'reply': 'free shipping'}),
                    {'status': 'completed', 'usage': 'bad', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]}]
        for i, value in enumerate(variants):
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model', return_value=value):
                self.assertIsNone(self.call(mid=str(i)))

    def test_legacy_faq_is_ignored_and_not_sent_to_model(self):
        page = self.page(api_key='fake', faq=[dict(id='old', keywords=['55'], answer='OLD_FAQ_CONTENT')])
        with patch('llm.request_model', return_value=response(interpretation(
                [update('weight_kg', '55', '55')]))) as model:
            result = self.call('55', page=page)
        self.assertEqual(result['updates'], {'weight_kg': 55.0})
        self.assertNotIn('answers', result)
        self.assertNotIn('faq', page['llm'])
        payload = model.call_args.args[0].decode()
        self.assertNotIn('faq', payload.lower())
        self.assertNotIn('OLD_FAQ_CONTENT', payload)
        with self.assertRaises(ValueError):
            validate_result(dict(interpretation(), faq_ids=['old']), '55', {}, page['_sales_script'], page['llm'])

    def test_validator_rejects_fabrication_negation_and_alternatives(self):
        page = self.page()
        for text, change in [('tôi cần tư vấn', update('size', 'M', 'size M')),
                             ('không lấy M', update('size', 'M', 'M')),
                             ('M hay L', update('size', 'M', 'M')),
                             ('M/L', update('size', 'M', 'M')),
                             ('M?', update('size', 'M', 'M')),
                             ('Hà Nội', update('address', '12 đường A phường B Hà Nội', 'Hà Nội')),
                             ('0912345678 0987654321', update('phone', '0912345678', '0912345678')),
                             ('55', update('weight_kg', '55', '55'))]:
            with self.subTest(text=text):
                stage = 'size' if change['field']=='size' else change['field']
                with self.assertRaises(ValueError):
                    validate_result(interpretation([change]), text, {'stage':stage}, page['_sales_script'], page['llm'])
        result = validate_result(interpretation([update('size', 'L', 'L')]), 'L cho chị nha', {'stage':'size'}, page['_sales_script'], page['llm'])
        self.assertEqual(result['updates'], {'size': 'L'})

    def test_model_cannot_complete_stop_or_handoff(self):
        page = self.page()
        previous = dict(stage='confirm', introduced=True, size='M', phone='0912345678', address='12 đường A phường B Hà Nội')
        for intent in ('stop', 'human', 'confirm'):
            with self.assertRaises(ValueError):
                validate_result(interpretation(intent=intent, evidence='ừ em'), 'ừ em', previous, page['_sales_script'], page['llm'])
            result = decide(page['_sales_script'], event('ừ em'), previous)
            self.assertEqual(result['lead']['stage'], 'confirm')
            self.assertEqual(result['next_state'], 'sales')

    def test_live_worker_integration_and_disabled_compatibility(self):
        page = self.page()
        self.config['pages']['p1'].update(page, page_access_token='fake-pancake')
        self.store.enqueue(event('chị 55', message_id='setup'))
        process_one(self.store, self.config)  # initial size prompt, no paid request
        self.store.enqueue(event('55', message_id='weight'))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model', return_value=response(
                interpretation([update('weight_kg', '55', '55')]))), patch('media.prepare', side_effect=lambda s,p,t,b: b), patch('bot.time.sleep'):
            process_one(self.store, self.config, live=True, sender=lambda *a: 'sent')
        self.assertEqual(self.store.lead('p1', 'c1')['stage'], 'size_confirm')
        self.assertEqual(self.store.lead('p1', 'c1')['suggested_size'], 'M')

    def test_input_limit_and_zero_budget_prevent_network(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model') as model:
            self.assertIsNone(self.call(page=self.page(daily_budget_usd=0)))
            self.assertIsNone(self.call('x' * 17000))
            model.assert_not_called()

    def test_partial_parse_still_calls_model_for_pending_size(self):
        previous = dict(stage='size', introduced=True)
        with patch('llm.request_model', return_value=response(interpretation(
                [update('size', 'L', 'L')]))) as model:
            assistance = self.call('L cho chị nha, 0912345678', previous,
                                   self.page(api_key='fake'))
        self.assertEqual(assistance['updates'], {'size': 'L'})
        context = json.loads(json.loads(model.call_args.args[0])['input'][0]['content'])
        self.assertEqual(context['expected_field'], 'size')
        self.assertEqual(context['parser_updates']['phone'], '0912345678')

    def test_standalone_address_at_every_active_stage_avoids_model(self):
        address = 'thôn la thạch, xã liên minh, hà nội'
        for stage in ('size', 'size_confirm', 'color', 'phone', 'address', 'confirm'):
            with self.subTest(stage=stage):
                previous = dict(stage=stage, introduced=True)
                if stage in ('phone','address','confirm'):
                    previous['size'] = 'M'
                page = self.page(api_key='fake')
                decision = decide(page['_sales_script'], event(address), previous)
                self.assertEqual(decision['lead']['address'], address)
                self.assertEqual(decision['lead']['stage'], 'phone' if previous.get('size') else 'size')
                with patch('llm.request_model') as model:
                    self.assertIsNone(self.call(address, previous, page, mid=stage))
                    model.assert_not_called()
                # Repeating an already saved address is also a free deterministic path.
                with patch('llm.request_model') as model:
                    self.assertIsNone(self.call(address, decision['lead'], page, mid=stage+'-repeat'))
                    model.assert_not_called()

    def test_unlabelled_location_when_waiting_for_address_is_saved_without_llm(self):
        from sales import extract
        page = self.page(api_key='fake')
        previous = dict(stage='address',size='M',phone='0912345678',introduced=True,repeat_count=2)
        for text in ['La thạch, xã liên minh, hà nội',
                     'La Thạch, xã Liên Minh, thành phố Hà Nội']:
            with self.subTest(text=text):
                decision = decide(page['_sales_script'],event(text),previous)
                self.assertEqual(decision['lead']['address'],text)
                self.assertEqual(decision['lead']['stage'],'confirm')
                self.assertEqual(decision['lead']['repeat_count'],0)
                with patch('llm.request_model') as model:
                    self.assertIsNone(self.call(text,previous,page))
                    model.assert_not_called()
                self.assertNotIn('address',extract(text,{'stage':'size'}))
        for text in ['La thạch, xã liên minh', 'xã liên minh, hà nội',
                     'La thạch, xã liên minh, hà nội, ship bao lâu?',
                     'La thạch hay chỗ khác, xã liên minh, hà nội']:
            with self.subTest(rejected=text):
                self.assertNotIn('address',extract(text,previous))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM llm_calls').fetchone()[0],0)

    def test_address_questions_and_incomplete_locations_still_use_model(self):
        samples = ['xã liên minh, hà nội',
                   'thôn la thạch, xã liên minh, hà nội, ship bao lâu?',
                   'thôn la thạch hay thôn khác, xã liên minh, hà nội',
                   'thôn la thạch, xã liên minh, hà nội, tư vấn thêm cho chị']
        from sales import address_only
        for i, text in enumerate(samples):
            with self.subTest(text=text):
                self.assertIsNone(address_only(text))
                with patch('llm.request_model', return_value=response(interpretation(unclear=True))) as model:
                    self.call(text, page=self.page(api_key='fake'), mid='address-question-'+str(i))
                    model.assert_called_once()
                previous = dict(stage='address',size='M',phone='0912345678',introduced=True)
                page = self.page(api_key='fake')
                decision = decide(page['_sales_script'],event(text),previous)
                self.assertNotIn('address',decision['lead'])
                with patch('llm.request_model', return_value=response(interpretation(unclear=True, reply='Chị cho em rõ phần địa chỉ nhận hàng nhé?'))) as model:
                    self.call(text, previous, page, mid='waiting-address-'+str(i))
                    model.assert_called_once()

    def test_plain_text_trace_shows_request_response_and_rejected_updates(self):
        secret = 'llm-trace-test-secret'
        with self.assertLogs('pancake.llm', level='INFO') as captured, \
                patch('llm.request_model', return_value=response(interpretation(
                    [update('size', 'M', 'M')], unclear=True))):
            self.call('M hay L ' + secret, page=self.page(api_key=secret))
        log = '\n'.join(captured.output)
        for value in ('step=llm_call_reason', 'expected_field', 'changed_fields',
                      'standalone_address_detected', 'step=llm_request', 'step=llm_response', 'step=llm_interpretation',
                      'step=llm_validation', 'Uncertain response cannot mutate data',
                      'page=p1 conversation=c1 message=m1', 'M hay L'):
            self.assertIn(value, log)
        self.assertNotIn(secret, log)
        self.assertIn('[REDACTED]', log)
        self.assertLess(log.index('step=llm_call_reason'), log.index('step=llm_request'))

    def test_timeout_trace_identifies_message_and_fallback(self):
        with self.assertLogs('pancake.llm', level='INFO') as captured, \
                patch('llm.request_model', side_effect=TimeoutError('request timed out')):
            self.call(page=self.page(api_key='trace-timeout-key'))
        log = '\n'.join(captured.output)
        self.assertIn('step=llm_failure', log)
        self.assertIn('TimeoutError', log)
        self.assertIn('page=p1 conversation=c1 message=m1', log)

    def test_enabled_worker_keeps_asking_when_model_uncertain_or_unavailable(self):
        page = self.page(api_key='fake')
        self.config['pages']['p1'].update(page, page_access_token='fake-pancake')
        self.store.enqueue(event('chào', message_id='setup'))
        process_one(self.store, self.config)
        for index in range(5):
            self.store.enqueue(event('chưa rõ lắm', message_id='unclear-' + str(index)))
            with patch('llm.request_model', return_value=response(interpretation(unclear=True)),
                    side_effect=TimeoutError if index == 4 else None), \
                    patch('media.prepare', side_effect=lambda s,p,t,b: b), patch('bot.time.sleep'):
                sent = []
                process_one(self.store, self.config, live=True,
                            sender=lambda *args: sent.append(args[-1]) or 'sent')
            lead = self.store.lead('p1', 'c1')
            self.assertEqual(lead['stage'], 'size')
            self.assertNotIn('size', lead)
            expected = interpretation(unclear=True)['reply'] if index < 4 else page['_sales_script']['prompts']['size_unclear']
            self.assertEqual(sent, [{'action':'reply_inbox','message':expected}])

    def test_config_validation(self):
        for invalid in [{'api_key': 123}, {'enabled': 'false'}, {'daily_budget_usd': -1}, {'daily_budget_usd': float('nan')},
                        {'model': 'unpriced-model'}, {'max_calls_per_conversation': 1.5},
                        {'enable': True}]:
            with self.assertRaises(ValueError):
                settings(invalid)
        path = Path(self.tmp.name) / 'config.json'
        path.write_text(json.dumps({'pages': {'p': {'llm': {'enabled': True}}}}))
        with self.assertRaisesRegex(ValueError, 'sales_script'):
            load_config(path)

    def test_http_transport_has_fixed_endpoint_and_timeout(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit): return b'{"status":"completed"}'
        with patch('llm.urlopen', return_value=Response()) as network:
            request_model(b'{}', 'test-key', 5)
            req = network.call_args.args[0]
            self.assertEqual(req.full_url, 'https://api.openai.com/v1/responses')
            self.assertEqual(req.get_header('Authorization'), 'Bearer test-key')
            self.assertEqual(network.call_args.kwargs['timeout'], 5)


if __name__ == '__main__':
    unittest.main()

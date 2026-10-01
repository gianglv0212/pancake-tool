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


def interpretation(updates=None, faq_ids=None, intent='inform', unclear=False):
    return dict(intent=intent, updates=updates or [], faq_ids=faq_ids or [], needs_clarification=unclear)


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

    def test_faq_is_fixed_free_and_resets_repeat_counter(self):
        page = self.page(faq=[dict(id='size_help', keywords=['bảng size'], answer='Câu trả lời shop đã duyệt.')])
        previous = dict(stage='size', introduced=True, repeat_count=2)
        with patch('llm.request_model') as model:
            assistance = self.call('bảng size', previous, page, live=False)
            model.assert_not_called()
        result = decide(page['_sales_script'], event('bảng size'), previous, assistance)
        self.assertEqual(result['lead']['stage'], 'size')
        self.assertEqual(result['lead']['repeat_count'], 0)
        self.assertEqual(result['body']['message'], 'Câu trả lời shop đã duyệt.')

    def test_model_faq_uses_only_configured_answer(self):
        page = self.page(faq=[dict(id='size_help', keywords=['bảng size'], answer='Fixed answer')])
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model', return_value=response(interpretation(faq_ids=['size_help']))):
            self.assertEqual(self.call('tư vấn cỡ áo với', page=page)['answers'], ['Fixed answer'])
        with self.assertRaises(ValueError):
            validate_result(interpretation(faq_ids=['invented']), 'hi', {}, page['_sales_script'], page['llm'])

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
                result = validate_result(interpretation([change]), text, {}, page['_sales_script'], page['llm'])
                self.assertIsNone(result)
        result = validate_result(interpretation([update('size', 'L', 'L')]), 'L cho chị nha', {}, page['_sales_script'], page['llm'])
        self.assertEqual(result['updates'], {'size': 'L'})

    def test_model_cannot_complete_stop_or_handoff(self):
        page = self.page()
        previous = dict(stage='confirm', introduced=True, size='M', phone='0912345678', address='12 đường A phường B Hà Nội')
        for intent in ('stop', 'human', 'confirm'):
            assistance = validate_result(interpretation(intent=intent), 'ừ em', previous, page['_sales_script'], page['llm'])
            result = decide(page['_sales_script'], event('ừ em'), previous, assistance)
            self.assertEqual(result['lead']['stage'], 'confirm')
            self.assertEqual(result['next_state'], 'sales')

    def test_live_worker_integration_and_disabled_compatibility(self):
        page = self.page()
        self.config['pages']['p1'].update(page, page_access_token='fake-pancake')
        self.store.enqueue(event('chị 55', message_id='setup'))
        process_one(self.store, self.config)  # initial size prompt, no paid request
        self.store.enqueue(event('55', message_id='weight'))
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model', return_value=response(
                {'reply': 'Dạ chị muốn chọn M cho lần này không ạ?', 'action': 'reply', 'evidence': '',
                 'question': 'size', 'updates': [dict(update('weight_kg', '55', '55'), source='current')],
                 'fact_ids': [], 'reuse_fields': [], 'send_photos': False})), patch('media.prepare', side_effect=lambda s,p,t,b: b), patch('bot.time.sleep'):
            process_one(self.store, self.config, live=True, sender=lambda *a: 'sent')
        self.assertEqual(self.store.lead('p1', 'c1')['stage'], 'size')
        self.assertEqual(self.store.lead('p1', 'c1')['pending']['suggested_size'], 'M')

    def test_input_limit_and_zero_budget_prevent_network(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake'}), patch('llm.request_model') as model:
            self.assertIsNone(self.call(page=self.page(daily_budget_usd=0)))
            self.assertIsNone(self.call('x' * 65000))
            model.assert_not_called()

    def test_partial_parse_still_calls_model_for_pending_size(self):
        previous = dict(stage='size', introduced=True)
        with patch('llm.request_model', return_value=response(interpretation(
                [update('size', 'L', 'L')]))) as model:
            assistance = self.call('L cho chị nha, 0912345678', previous,
                                   self.page(api_key='fake'))
        self.assertEqual(assistance['updates'], {'size': 'L'})
        context = json.loads(json.loads(model.call_args.args[0])['input'])
        self.assertEqual(context['expected_field'], 'size')
        self.assertEqual(context['parser_updates']['phone'], '0912345678')

    def test_plain_text_trace_shows_request_response_and_rejected_updates(self):
        secret = 'llm-trace-test-secret'
        with self.assertLogs('pancake.llm', level='INFO') as captured, \
                patch('llm.request_model', return_value=response(interpretation(
                    [update('size', 'M', 'M')], unclear=True))):
            self.call('M hay L ' + secret, page=self.page(api_key=secret))
        log = '\n'.join(captured.output)
        for value in ('step=llm_request', 'step=llm_response', 'step=llm_interpretation',
                      'step=llm_validation', 'needs_clarification=true',
                      'page=p1 conversation=c1 message=m1', 'M hay L'):
            self.assertIn(value, log)
        self.assertNotIn(secret, log)
        self.assertIn('[REDACTED]', log)

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
            with patch('llm.request_model', return_value=response(
                    {'reply': 'Chị đang phân vân về độ rộng hay chiều dài của áo ạ?', 'action': 'reply',
                     'evidence': '', 'question': 'clarify', 'updates': [],
                     'fact_ids': [], 'reuse_fields': [], 'send_photos': False}),
                    side_effect=TimeoutError if index == 4 else None), \
                    patch('media.prepare', side_effect=lambda s,p,t,b: b), patch('bot.time.sleep'):
                sent = []
                process_one(self.store, self.config, live=True,
                            sender=lambda *args: sent.append(args[-1]) or 'sent')
            lead = self.store.lead('p1', 'c1')
            self.assertEqual(lead['stage'], 'dialogue')
            self.assertNotIn('size', lead)
            self.assertTrue(any('độ rộng hay chiều dài' in body.get('message', '') for body in sent) if index < 4
                            else any('trục trặc' in body.get('message', '') for body in sent))

    def test_config_validation(self):
        for invalid in [{'api_key': 123}, {'enabled': 'false'}, {'daily_budget_usd': -1}, {'daily_budget_usd': float('nan')},
                        {'model': 'unpriced-model'}, {'max_calls_per_conversation': 1.5},
                        {'faq': [{'id': 'x', 'keywords': [], 'answer': ''}]}, {'enable': True}]:
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

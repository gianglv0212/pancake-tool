import json
import unittest
from pathlib import Path
from unittest.mock import patch

import test_bot
from test_bot import event
from bot import process_one
from llm import settings
from dialogue import apply_result, facts_for, recent_turns, respond, schema_for


def output(reply='Dạ chị đang muốn tìm mẫu mặc đi làm hay đi chơi ạ?', action='reply', question='clarify', **kwargs):
    return dict(reply=reply, action=action, question=question, evidence=kwargs.get('evidence', ''),
                updates=kwargs.get('updates', []), fact_ids=kwargs.get('fact_ids', []),
                reuse_fields=kwargs.get('reuse_fields', []), send_photos=kwargs.get('send_photos', False))


def update(field, value, evidence, source='current'):
    return dict(field=field, value=value, evidence=evidence, source=source)


def api(result):
    return {'status': 'completed', 'usage': {'input_tokens': 1000, 'output_tokens': 200},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(result)}]}]}


class DialogueTests(unittest.TestCase):
    def setUp(self):
        test_bot.BotTests.setUp(self)
        doc = json.loads(Path('sales-script.json').read_text(encoding='utf-8-sig'))
        self.script = next(iter(doc['pages'].values()))
        self.page = self.config['pages']['p1']
        self.page.update(_sales_script=self.script, page_access_token='fake',
                         llm=settings({'enabled': True, 'api_key': 'fake-dialogue-key'}))
        self.facts = facts_for(self.script, self.page['llm'])

    tearDown = test_bot.BotTests.tearDown

    def send(self, text, mid, result, status='sent'):
        self.store.enqueue(event(text, message_id=mid))
        with patch('llm.request_model', return_value=api(result)) as model, \
                patch('media.prepare', side_effect=lambda s,p,t,b: b), patch('bot.time.sleep'):
            process_one(self.store, self.config, live=True, sender=lambda *args: status)
        return self.store.lead('p1', 'c1'), model

    def apply(self, result, text='ừ em', previous=None, old=None):
        return apply_result(result, event(text), previous or {}, self.script, self.facts, old or {})

    def test_every_live_inbox_turn_gets_context_and_model_reply(self):
        lead, model = self.send('chào shop', 'hello', output('Chị muốn xem mẫu nào ạ?'))
        self.assertEqual(model.call_count, 1)
        lead, model = self.send('mẫu vừa gửi đó em', 'next', output('Chị đang nói đến mẫu áo dài phải không ạ?'))
        messages = json.loads(model.call_args.args[0])['input']
        self.assertEqual([t['role'] for t in messages], ['developer', 'user', 'assistant', 'user'])
        self.assertEqual(messages[2]['content'], 'Chị muốn xem mẫu nào ạ?')
        self.assertEqual(messages[-1]['content'], 'mẫu vừa gửi đó em')
        self.assertNotIn('fake-dialogue-key', model.call_args.args[0].decode())
        with self.store.connect() as db:
            decision = json.loads(db.execute('SELECT result FROM jobs ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(len(decision['bodies']), 1)
        self.assertEqual(decision['body']['message'], 'Chị đang nói đến mẫu áo dài phải không ạ?')

    def test_weight_is_not_size_choice_and_pending_choice_uses_context(self):
        lead = self.apply(output('Chị chọn M nhé?', question='size', updates=[update('weight_kg', '55', '55')]),
                          '55', {'stage': 'size', 'pending': {'kind': 'weight_kg'}})['lead']
        self.assertNotIn('size', lead)
        self.assertEqual(lead['pending']['suggested_size'], 'M')
        choice = self.apply(output('Dạ chị cho em số liên hệ nhận hàng nhé.', question='phone',
                                  updates=[update('size', 'M', 'ừ em', 'pending')]), previous=lead)
        self.assertEqual(choice['lead']['size'], 'M')
        with self.assertRaises(ValueError):
            self.apply(output(updates=[update('size', 'M', '55')]), '55')

    def test_natural_confirmation_bound_to_summary_not_any_ok(self):
        ready = dict(size='M', phone='0912345678', address='12 Nguyễn Trãi, phường B, Hà Nội', quantity=1, stage='dialogue')
        offer = self.apply(output('Em tổng hợp lại thông tin của chị nhé.', 'offer_order', 'none'), previous=ready)
        self.assertIn(ready['address'], offer['body']['message'])
        confirmed = self.apply(output('Dạ em đã ghi nhận để nhân viên kiểm tra sản phẩm cho chị.', 'confirm_order', 'none', evidence='ừ em'), previous=offer['lead'])
        self.assertEqual(confirmed['lead']['stage'], 'complete')
        self.assertNotIn('nhắn “đúng rồi”', confirmed['body']['message'])
        with self.assertRaises(ValueError):
            self.apply(output(action='confirm_order', evidence='ừ em'), previous=ready)
        altered = dict(offer['lead'], size='L')
        with self.assertRaises(ValueError):
            self.apply(output(action='confirm_order', evidence='ừ em'), previous=altered)

    def test_mixed_confirmation_and_correction_cannot_complete(self):
        pending = {'kind': 'order', 'values': {'size': 'M', 'phone': '0912345678', 'address': '12 đường A phường B Hà Nội', 'quantity': 1}}
        previous = dict(pending['values'], pending=pending, stage='confirm')
        with self.assertRaises(ValueError):
            self.apply(output(action='confirm_order', evidence='ok đổi L', updates=[update('size', 'L', 'L')]), 'ok đổi L', previous)
        result = self.apply(output('Em đổi sang L và gửi lại thông tin nhé.', 'offer_order', 'none',
                                   updates=[update('size', 'L', 'L')]), 'ok đổi L', previous)
        self.assertEqual(result['lead']['pending']['values']['size'], 'L')

    def test_reorder_and_reuse_need_separate_contextual_consent(self):
        old = {'size': 'M', 'phone': '0912345678', 'address': '12 đường A phường B Hà Nội', 'quantity': 1}
        offer = self.apply(output('Dạ chị muốn lấy thêm ạ.', 'offer_reorder', 'none', evidence='lấy như lần trước'),
                           'lấy như lần trước', dict(old, stage='complete'), old)['lead']
        fresh = self.apply(output('Chị muốn dùng size cũ hay chọn size khác ạ?', 'confirm_reorder', 'clarify', evidence='ừ em'), previous=offer, old=old)['lead']
        self.assertNotIn('phone', fresh)
        reuse = self.apply(output('Em nhắc lại thông tin lần trước nhé.', 'offer_reuse', 'none', evidence='dùng lại hết',
                                  reuse_fields=['size','phone','address']), 'dùng lại hết', fresh, old)['lead']
        accepted = self.apply(output('Em đã ghi nhận các thông tin chị vừa đồng ý.', 'confirm_reuse', 'none', evidence='ừ em'), previous=reuse, old=old)['lead']
        self.assertEqual(accepted['phone'], old['phone'])
        self.assertNotEqual(accepted['stage'], 'complete')
        declined = self.apply(output('Dạ khi nào chị muốn mua thêm cứ nhắn em nhé.', 'decline', 'none', evidence='chưa em'), 'chưa em', offer, old)['lead']
        self.assertEqual(declined['stage'], 'complete')

    def test_unknown_fields_facts_and_invented_values_rejected(self):
        cases = [output(fact_ids=['fake_policy']), output(updates=[update('size','XL','M')]),
                 output(updates=[update('address','12 invented road','Hà Nội')]),
                 output('Giá chỉ 100k thôi chị.'), output('Shop đã tạo đơn cho chị rồi.'),
                 output(updates=[update('quantity','9','2')])]
        for result in cases:
            with self.subTest(result=result):
                with self.assertRaises(ValueError):
                    self.apply(result, 'M Hà Nội 2')

    def test_approved_facts_and_configured_photos(self):
        key = next(k for k,v in self.facts.items() if '429k' in v)
        result = self.apply(output('Em gửi chị thông tin mẫu này nhé.', fact_ids=[key], send_photos=True))
        self.assertIn('429k', result['body']['message'])
        self.assertEqual(result['bodies'][1]['photos'], self.script['groups']['2'][1]['photos'])

    def test_failed_delivery_does_not_commit_pending_or_enter_history_as_sent(self):
        self.send('chào', 'first', output('Chị cao bao nhiêu ạ?', question='height_cm'))
        before = self.store.lead('p1','c1')
        self.send('160cm', 'failed', output('Chị nặng bao nhiêu ạ?', question='weight_kg',
                                          updates=[update('height_cm','160','160cm')]), status='failed')
        self.assertEqual(self.store.lead('p1','c1'), before)
        turns = recent_turns(self.store, event('tiếp', message_id='next'), 8)
        self.assertIn({'role':'user','content':'160cm'}, turns)
        self.assertNotIn({'role':'assistant','content':'Chị nặng bao nhiêu ạ?'}, turns)

    def test_history_isolated_between_customers_and_pages(self):
        self.send('khách một', 'one', output('Câu riêng của khách một.'))
        another = event('khách hai', message_id='two')
        another['data']['conversation']['id'] = 'c2'
        self.assertEqual(recent_turns(self.store, another, 8), [])
        another['data']['conversation']['id'] = 'c1'
        another['page_id'] = 'p2'
        self.assertEqual(recent_turns(self.store, another, 8), [])

    def test_budget_and_timeout_keep_state_without_regex_clarification(self):
        previous = dict(stage='dialogue', phone='0912345678', repeat_count=9)
        self.page['llm']['daily_budget_usd'] = 0
        with patch('llm.request_model') as model:
            result = respond(self.store, self.page, event('chưa rõ'), previous)
            model.assert_not_called()
        self.assertEqual(result['lead'], previous)
        self.assertIn('trục trặc', result['body']['message'])
        self.assertNotIn('đúng rồi', result['body']['message'])

    def test_assigned_human_and_dry_run_never_call_model(self):
        payload = event('chào')
        payload['data']['conversation']['assignee_ids'] = ['staff']
        self.store.enqueue(payload)
        with patch('llm.request_model') as model:
            process_one(self.store, self.config, live=True)
            self.store.enqueue(event('chào', message_id='dry'))
            process_one(self.store, self.config)
            model.assert_not_called()

    def test_repeated_questions_do_not_force_handoff(self):
        previous = {'stage':'dialogue','repeat_count':50}
        result = self.apply(output('Chị muốn xem ảnh màu nào để dễ chọn hơn ạ?'), previous=previous)
        self.assertEqual(result['lead']['stage'],'dialogue')
        self.assertEqual(result['lead']['repeat_count'],0)

    def test_ambiguous_size_question_does_not_bind_a_choice(self):
        previous = self.apply(output('Chị muốn M hay L ạ?', question='size'),
                              previous={'weight_kg':55})['lead']
        self.assertIsNone(previous['pending']['suggested_size'])
        with self.assertRaises(ValueError):
            self.apply(output(updates=[update('size','M','ừ em','pending')]), previous=previous)

    def test_complete_data_always_gets_a_bound_summary(self):
        previous = dict(size='M', phone='0912345678', quantity=1)
        address = '12 Nguyễn Trãi, phường Thanh Xuân Trung, Hà Nội'
        result = self.apply(output('Em ghi địa chỉ rồi nhé.', question='none',
                                   updates=[update('address',address,address)]), address, previous)
        self.assertEqual(result['rule'], 'dialogue:offer_order')
        self.assertEqual(result['lead']['pending']['values']['address'], address)

    def test_incomplete_offer_with_valid_question_keeps_grounded_data(self):
        result = self.apply(output('Chị cho em xin SĐT nhé?', 'offer_order', 'phone',
                                   updates=[update('size','M','M')]), 'M')
        self.assertEqual(result['lead']['size'],'M')
        self.assertEqual(result['rule'],'dialogue:reply')
        self.assertEqual(result['lead']['pending']['kind'],'phone')

    def test_history_confirmation_is_scoped_and_requires_pending_field(self):
        address = '12 Nguyễn Trãi, phường Thanh Xuân Trung, Hà Nội'
        result = output('Em ghi nhận địa chỉ nhé.', question='phone', evidence='ừ em',
                        updates=[update('address',address,address,'history')])
        history = [{'role':'user','content':address}]
        accepted = apply_result(result,event('ừ em'),{'pending':{'kind':'address'}},
                                self.script,self.facts,{},history)
        self.assertEqual(accepted['lead']['address'],address)
        for pending, turns in [({},history), ({'pending':{'kind':'address'}},[])]:
            with self.assertRaises(ValueError):
                apply_result(result,event('ừ em'),pending,self.script,self.facts,{},turns)

    def test_schema_restricts_evidence_and_terminal_mutations(self):
        schema = schema_for({'stage':'complete'},self.facts,{},[],'mua thêm')
        self.assertEqual(schema['properties']['evidence']['enum'],['mua thêm'])
        self.assertEqual(schema['properties']['updates']['maxItems'],0)
        self.assertNotIn('confirm_order',schema['properties']['action']['enum'])
        self.assertNotIn('color',schema['properties']['question']['enum'])

    def test_answering_a_question_preserves_pending_order(self):
        values = dict(size='M', phone='0912345678', address='12 Nguyễn Trãi, Hà Nội', quantity=1)
        previous = dict(values, stage='confirm',pending={'kind':'order','values':values})
        result = self.apply(output('Em gửi thông tin chất liệu cho chị.', question='none'), previous=previous)
        self.assertEqual(result['lead']['pending'],previous['pending'])


if __name__ == '__main__':
    unittest.main()

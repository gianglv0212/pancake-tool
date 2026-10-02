import json
import unittest
from unittest.mock import patch
from test_bot import event
import test_llm
from test_llm import interpretation, update, response
from bot import process_one
from sales import decide, extract
from step_support import validate_result, confirmation_snapshot, recent_context


class StepSupportTests(unittest.TestCase):
    setUp = test_llm.LLMTests.setUp
    tearDown = test_llm.LLMTests.tearDown
    page = test_llm.LLMTests.page
    call = test_llm.LLMTests.call

    def previous(self):
        return dict(stage='address',size='M',phone='0912345678',introduced=True,session_id='session-a')

    def seed(self, previous, status='sent', text='size M'):
        payload = event(text, message_id='seed')
        self.store.enqueue(payload)
        job = self.store.claim()
        snapshot = confirmation_snapshot(previous,self.page()['_sales_script'])
        result = dict(lead=previous, bodies=[{'action':'reply_inbox','message':'Chị kiểm tra thông tin giúp em nhé?'}],
                      confirmation_snapshot=snapshot)
        self.store.finish(job,status,json.dumps(result), 'sales',previous)

    def test_address_validator_accepts_evidence_without_parser_recognition(self):
        text = 'xóm đoàn kết, la thạch, liên minh, hà nội'
        previous = self.previous()
        self.assertNotIn('address',extract(text,previous))
        page = self.page(api_key='fake')
        parsed = interpretation([update('address',text.title(),text)])
        with patch('llm.request_model',return_value=response(parsed)) as model:
            assistance = self.call(text,previous,page)
        model.assert_called_once()
        decision = decide(page['_sales_script'],event(text),previous,assistance)
        self.assertEqual(decision['lead']['address'],text)
        self.assertEqual(decision['lead']['stage'],'confirm')

    def test_clarification_replaces_template_and_partial_address_survives(self):
        previous = self.previous()
        page = self.page(api_key='fake')
        self.config['pages']['p1'].update(page,page_access_token='fake')
        self.seed(previous)
        parsed = interpretation(unclear=True, reply='La Thạch thuộc xã/phường và tỉnh/thành nào chị nhỉ?')
        self.store.enqueue(event('La Thạch',message_id='partial'))
        sent = []
        with patch('llm.request_model',return_value=response(parsed)), patch('media.prepare',side_effect=lambda s,p,t,b:b):
            process_one(self.store,self.config,live=True,sender=lambda *args:sent.append(args[-1]) or 'sent')
        lead = self.store.lead('p1','c1')
        self.assertEqual(lead['stage'],'address')
        self.assertNotIn('address',lead)
        self.assertEqual(lead['pending_address_texts'],['La Thạch'])
        self.assertEqual(sent,[{'action':'reply_inbox','message':parsed['reply']}])
        text = 'Liên Minh Hà Nội'
        full = 'La Thạch, Liên Minh Hà Nội'
        parsed = interpretation([update('address',full,text)])
        with patch('llm.request_model',return_value=response(parsed)) as model:
            assistance = self.call(text,lead,page,mid='rest')
        context = json.loads(json.loads(model.call_args.args[0])['input'][0]['content'])
        self.assertEqual(context['last_question'],sent[0]['message'])
        decision = decide(page['_sales_script'],event(text),lead,assistance)
        self.assertEqual(decision['lead']['address'],full)
        self.assertNotIn('pending_address_texts',decision['lead'])

    def test_out_of_scope_extraction_gets_one_budgeted_question_repair(self):
        previous = dict(stage='phone',size='M',introduced=True)
        bad = interpretation([update('weight_kg','55','55kg')])
        question = interpretation(unclear=True,reply='Chị cho em xin số điện thoại liên hệ nhận hàng nhé?')
        page = self.page(api_key='fake')
        with patch('llm.request_model',side_effect=[response(bad),response(question)]) as model:
            assistance = self.call('55kg',previous,page)
        self.assertEqual(model.call_count,2)
        payload = json.loads(model.call_args.args[0])
        self.assertEqual(payload['text']['format']['schema']['properties']['updates']['maxItems'],0)
        self.assertIn('outside current step',json.loads(payload['input'][0]['content'])['validation_error'])
        decision = decide(page['_sales_script'],event('55kg'),previous,assistance)
        self.assertEqual(decision['lead']['weight_kg'],55)  # ordinary parser still retains other data
        self.assertEqual(decision['lead']['stage'],'phone')
        self.assertEqual(decision['bodies'],[{'action':'reply_inbox','message':question['reply']}])
        with self.store.connect() as db:
            rows = db.execute('SELECT status,charged_microusd FROM llm_calls ORDER BY rowid').fetchall()
        self.assertEqual([r[0] for r in rows],['rejected','accepted'])
        self.assertEqual(sum(r[1] for r in rows),650)

    def test_repair_respects_call_limit_and_never_loops(self):
        bad = response(interpretation([update('phone','0912345678','55')]))
        for limit, expected in [(1,1),(10,2)]:
            with patch('llm.request_model',return_value=bad) as model:
                self.assertIsNone(self.call(page=self.page(api_key='fake',max_calls_per_conversation=limit),mid=str(limit)))
            self.assertEqual(model.call_count,expected)

    def test_timeout_does_not_retry_or_count_as_customer_confusion(self):
        previous = dict(self.previous(),repeat_count=2)
        self.seed(previous)
        self.config['pages']['p1'].update(self.page(api_key='fake'),page_access_token='fake')
        self.store.enqueue(event('La Thạch',message_id='timeout'))
        with patch('llm.request_model',side_effect=TimeoutError) as model, patch('media.prepare',side_effect=lambda s,p,t,b:b):
            process_one(self.store,self.config,live=True,sender=lambda *args:'sent')
        model.assert_called_once()
        self.assertEqual(self.store.lead('p1','c1')['repeat_count'],2)
        self.assertEqual(self.store.lead('p1','c1')['stage'],'address')

    def test_natural_confirmation_requires_delivered_unchanged_summary(self):
        previous = dict(self.previous(),stage='confirm',address='La Thạch, Liên Minh, Hà Nội')
        self.seed(previous)
        page = self.page(api_key='fake')
        parsed = interpretation(intent='confirm',evidence='ừ em')
        with patch('llm.request_model',return_value=response(parsed)):
            assistance = self.call('ừ em',previous,page)
        decision = decide(page['_sales_script'],event('ừ em'),previous,assistance)
        self.assertEqual(decision['lead']['stage'],'complete')
        for text, lead in [('ừ em',dict(previous,size='L')),('ừ nhưng đổi L',previous),('chưa em',previous)]:
            with self.subTest(text=text,lead=lead):
                with self.assertRaises(ValueError):
                    validate_result(interpretation(intent='confirm',evidence=text),text,lead,page['_sales_script'],page['llm'],
                                    {'confirmation_offer':confirmation_snapshot(previous,page['_sales_script'])})

    def test_failed_delivery_and_other_customers_cannot_authorize_confirmation(self):
        previous = dict(self.previous(),stage='confirm',address='La Thạch, Liên Minh, Hà Nội')
        self.seed(previous,status='failed')
        self.assertIsNone(recent_context(self.store,event('ừ em',message_id='new'),previous)['confirmation_offer'])
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='sent'")
        other = event('ừ em',message_id='other')
        other['data']['conversation']['id'] = 'c2'
        self.assertEqual(recent_context(self.store,other,previous)['recent_turns'],[])
        self.assertEqual(recent_context(self.store,event('ừ em'),dict(previous,session_id='new-session'))['recent_turns'],[])

    def test_invented_address_and_off_step_question_are_rejected(self):
        page = self.page()
        text = 'La Thạch Hà Nội'
        with self.assertRaises(ValueError):
            validate_result(interpretation([update('address','La Thạch, Liên Minh, Hà Nội',text)]),
                            text,self.previous(),page['_sales_script'],page['llm'])
        with self.assertRaises(ValueError):
            validate_result(interpretation(unclear=True,reply='Địa chỉ chưa rõ, chị cho em xin số điện thoại nhé?'),
                            text,self.previous(),page['_sales_script'],page['llm'])

    def test_contextual_consent_reuses_existing_size_and_reorder_lifecycle(self):
        page = self.page()
        cases = [({'stage':'size_confirm','suggested_size':'M','introduced':True},'phone'),
                 ({'stage':'reorder_confirm','reorder_origin':'complete','session_id':'old','phone':'0912345678'},'size'),
                 ({'stage':'contact_reuse_confirm','size':'M','previous_contact':{'phone':'0912345678','address':'La Thạch, Liên Minh, Hà Nội'},'introduced':True},'confirm')]
        for previous, expected in cases:
            with self.subTest(stage=previous['stage']):
                assistance = validate_result(interpretation(intent='confirm',evidence='ừ em'), 'ừ em', previous,
                    page['_sales_script'],page['llm'],{'confirmation_offer':confirmation_snapshot(previous,page['_sales_script'])})
                result = decide(page['_sales_script'],event('ừ em'),previous,assistance)
                self.assertEqual(result['lead']['stage'],expected)
                if previous['stage']=='reorder_confirm':
                    self.assertTrue(result['new_session'])
                    self.assertNotIn('phone',result['lead'])

    def test_field_validators_are_scoped_and_check_values(self):
        page = self.page()
        page['_sales_script']['product']['colors'] = ['Đỏ','Xanh']
        cases = [('phone','số của chị: 0912 345 678','0912345678'),
                 ('color','đỏ cho chị nha','Đỏ'),
                 ('color','chị lấy đỏ và xanh','Đỏ, Xanh')]
        for field,text,value in cases:
            result = validate_result(interpretation([update(field,value,text)]),text,{'stage':field},page['_sales_script'],page['llm'])
            self.assertEqual(result['updates'][field],value)
        for text in ['đỏ hoặc xanh','không lấy đỏ','đỏ?']:
            with self.assertRaises(ValueError):
                validate_result(interpretation([update('color','Đỏ',text)]),text,{'stage':'color'},page['_sales_script'],page['llm'])
        for value in ('Đỏ', 'Đỏ, Tím', 'Đỏ, Đỏ'):
            with self.assertRaises(ValueError):
                validate_result(interpretation([update('color',value,'đỏ và xanh')]),'đỏ và xanh',
                                {'stage':'color'},page['_sales_script'],page['llm'])


if __name__ == '__main__':
    unittest.main()

import json
import unittest
from unittest.mock import Mock
from datetime import datetime, timezone
import test_bot
from test_bot import event
from discounts import DEFAULTS, schedule, run_due, validate
from sales import decide


class DiscountTests(unittest.TestCase):
    def setUp(self):
        test_bot.BotTests.setUp(self)
        self.options = {**DEFAULTS,'enabled':True,'discount_price':'399.000đ'}
        self.config['pages']['p1'].update(discount_followup=self.options,page_access_token='test')
        self.e = event('xin giá')
        self.e['data']['message']['inserted_at'] = datetime.fromtimestamp(1000,timezone.utc).isoformat()
        with self.store.connect() as db:
            db.execute('INSERT INTO leads VALUES(?,?,?)',('p1','c1',json.dumps({'stage':'size'})))
        self.client = Mock()
        self.client.get.side_effect = lambda version,*args: ({'conversations':[self.e['data']['conversation']]} if version=='v2' else {'messages':[self.e['data']['message']], 'can_inbox':True})
        self.client.send.return_value = 'sent'

    tearDown = test_bot.BotTests.tearDown

    def status(self):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM followups').fetchone()
            return dict(row) if row else None

    def test_disabled_and_requires_price(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},DEFAULTS,now=1001)
        self.assertIsNone(self.status())
        with self.assertRaises(ValueError): validate({**DEFAULTS,'enabled':True})

    def test_two_hours_and_once_across_restart(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=1001)
        self.assertEqual(self.status()['due'],8200)
        run_due(self.store,self.config,self.client,True,now=8199)
        self.client.send.assert_not_called()
        run_due(self.store,self.config,self.client,True,now=8200)
        self.assertEqual(self.status()['status'],'sent')
        self.assertIn('399.000đ',self.client.send.call_args.args[-1]['message'])
        self.store.recover()
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=8300)
        run_due(self.store,self.config,self.client,True,now=8400)
        self.assertEqual(self.client.send.call_count,1)

    def test_hesitation_but_not_optout(self):
        with open('sales-script.json',encoding='utf-8') as file: script=json.load(file)['pages']['122526837503326']
        script['discount_followup'] = self.options
        e=event('chưa muốn lấy')
        lead=decide(script,e,{})['lead']
        self.assertEqual(lead['stage'],'hesitating')
        e['data']['message']['inserted_at']=self.e['data']['message']['inserted_at']
        schedule(self.store,'p1',e,lead,self.options,now=1001)
        self.assertEqual(self.status()['due'],1300)
        stopped=decide(script,event('dừng nhắn'),lead)['lead']
        schedule(self.store,'p1',self.e,stopped,self.options,now=1002)
        self.assertEqual(self.status()['status'],'cancelled')

    def test_new_message_moves_timer(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=1001)
        self.e['data']['message']['inserted_at']=datetime.fromtimestamp(2000,timezone.utc).isoformat()
        self.e['data']['message']['id']='new'
        schedule(self.store,'p1',self.e,{'stage':'phone','size':'M'},self.options,now=2001)
        self.assertEqual(self.status()['due'],9200)

    def test_complete_cancels(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=1001)
        with self.store.connect() as db:
            db.execute('UPDATE leads SET data=?',(json.dumps({'size':'M','phone':'0912345678','address':'address'}),))
        run_due(self.store,self.config,self.client,True,now=8200)
        self.assertEqual(self.status()['status'],'cancelled')
        self.client.send.assert_not_called()

    def test_fresh_new_reply_cancels(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=1001)
        self.e['data']['message']['id']='new'
        run_due(self.store,self.config,self.client,True,now=8200)
        self.assertEqual(self.status()['status'],'cancelled')
        self.client.send.assert_not_called()

    def test_dry_run_and_expired(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=1001)
        run_due(self.store,self.config,self.client,False,now=8200)
        self.assertEqual(self.status()['status'],'dry_run')
        self.client.send.assert_not_called()
        with self.store.connect() as db: db.execute("UPDATE followups SET status='pending'")
        run_due(self.store,self.config,self.client,True,now=100000)
        self.assertEqual(self.status()['status'],'expired')

    def test_unknown_send_not_retried(self):
        schedule(self.store,'p1',self.e,{'stage':'size'},self.options,now=1001)
        self.client.send.return_value='unknown'
        run_due(self.store,self.config,self.client,True,now=8200)
        run_due(self.store,self.config,self.client,True,now=8300)
        self.assertEqual(self.client.send.call_count,1)

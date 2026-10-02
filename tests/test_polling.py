import unittest
import threading
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import test_bot
from test_bot import event
from polling import ApiError, poll_page, read_messages, run
from bot import Store


def date(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


class PollTests(unittest.TestCase):
    setUp = test_bot.BotTests.setUp
    tearDown = test_bot.BotTests.tearDown
    def payload(self, t, mid='new'):
        e = event(message_id=mid)
        c, m = e['data']['conversation'], e['data']['message']
        c.update(type='INBOX', updated_at=date(t), message_count=1)
        m['inserted_at'] = date(t)
        return c, m

    def count(self):
        with self.store.connect() as db:
            return db.execute('SELECT count(*) FROM jobs').fetchone()[0]

    def test_baseline_restart_and_duplicates(self):
        c, m = self.payload(100)
        client = Mock()
        client.get.side_effect = lambda version, *args: {'conversations': [c]} if version == 'v2' else {'messages': [m]}
        poll_page(self.store, self.config, client, 'p1', now=101)
        self.assertEqual(self.count(), 0)
        c['updated_at'] = m['inserted_at'] = date(102)
        poll_page(self.store, self.config, client, 'p1', now=103)
        poll_page(Store(self.store.path), self.config, client, 'p1', now=104)
        self.assertEqual(self.count(), 1)

    def test_conversation_pagination(self):
        with self.store.connect() as db:
            db.execute('INSERT INTO poll_starts VALUES(?,?)', ('p1', 99))
        c, m = self.payload(101)
        batch = [dict(c, id=str(i)) for i in range(60)]
        client = Mock()
        def get(version, page, suffix, params):
            if version == 'v1': return {'messages': []}
            return {'conversations': [] if params.get('last_conversation_id') else batch}
        client.get.side_effect = get
        poll_page(self.store, self.config, client, 'p1', now=102)
        self.assertTrue(any(call.args[-1].get('last_conversation_id') == '59' for call in client.get.call_args_list))

    def test_message_pagination_and_order(self):
        c, m = self.payload(101)
        c['message_count'] = 31
        batch = [dict(m, id=str(i), inserted_at=date(101 + i)) for i in range(30, 0, -1)]
        client = Mock()
        client.get.side_effect = [{'messages': batch}, {'messages': [dict(m, id='0')]}]
        messages, _ = read_messages(client, 'p1', c, 100)
        self.assertEqual(len(messages), 31)
        self.assertEqual(messages[0]['id'], '0')
        self.assertEqual(client.get.call_args.args[-1], {'current_count': 1})

    def test_failed_read_keeps_baseline(self):
        client = Mock()
        client.get.side_effect = ApiError('HTTP 503')
        with self.assertRaises(ApiError):
            poll_page(self.store, self.config, client, 'p1', now=100)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT started FROM poll_starts').fetchone()[0], 100)

    def test_tags_from_rest(self):
        from bot import plan
        e = event()
        e['data']['conversation']['tags'] = [None, {'id': 7}]
        self.config['pages']['p1']['stop_tags'] = [7]
        self.assertIsNone(plan(self.config, e))

    def test_multi_page_worker_pool(self):
        self.config['worker_count'] = 2
        self.config['pages'] = {page: {'enabled': True, 'page_access_token': 'test'}
                                for page in ('p1', 'p2')}
        barrier = threading.Barrier(2)
        def process(*args, **kwargs):
            barrier.wait(timeout=5)
            return False
        with patch('polling.Client'), patch('polling.poll_page') as poll, \
                patch('polling.process_one', side_effect=process) as worker, \
                patch('discounts.run_due'):
            run(self.config, self.store, once=True)
        self.assertEqual([call.args[3] for call in poll.call_args_list], ['p1', 'p2'])
        self.assertEqual(worker.call_count, 2)

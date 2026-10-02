import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from bot import Store, load_config, plan, process_one, send, page_token


def event(text="ship", channel="INBOX", message_id="m1"):
    return {"page_id": "p1", "event_type": "messaging", "data": {
        "conversation": {"id": "c1", "from": {"id": "u1"}},
        "message": {"id": message_id, "page_id": "p1", "conversation_id": "c1",
                    "from": {"id": "u1", "name": "Lan"}, "type": channel,
                    "message": text, "inserted_at": datetime.now(timezone.utc).isoformat(),
                    "can_comment": True, "can_reply_privately": True},
        "post": {"id": "post1"}}}


class BotTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config("config.example.json")
        self.config["pages"]["p1"] = self.config["pages"].pop("YOUR_FACEBOOK_PAGE_ID")
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "test.db"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_vietnamese_and_comment_target(self):
        result = plan(self.config, event("GIA BAO NHIEU", "COMMENT"))
        self.assertEqual(result["body"]["action"], "reply_comment")
        self.assertEqual(result["body"]["message_id"], "m1")
        self.assertIn("Lan", result["body"]["message"])

    def test_echo_unknown_page_and_old_events(self):
        for field, value in [("from", {"id": "p1"}), ("inserted_at", "2020-01-01"),
                             ("is_removed", True), ("page_id", "other"), ("edit_history", {"old": "a"})]:
            payload = event()
            payload["data"]["message"][field] = value
            self.assertIsNone(plan(self.config, payload))
        payload = event()
        payload["page_id"] = "other"
        self.assertIsNone(plan(self.config, payload))

    def test_assigned_and_tags(self):
        payload = event()
        payload["data"]["conversation"]["assignee_ids"] = ["staff"]
        self.assertIsNone(plan(self.config, payload))
        payload["data"]["conversation"]["assignee_ids"] = []
        payload["data"]["conversation"]["tags"] = [123]
        self.config["pages"]["p1"]["stop_tags"] = [123]
        self.assertIsNone(plan(self.config, payload))

    def test_persistent_dedup_and_dry_run(self):
        self.store.enqueue(event())
        self.store.enqueue(event())
        calls = []
        process_one(self.store, self.config, sender=lambda *a: calls.append(a))
        self.assertFalse(process_one(Store(self.store.path), self.config))
        self.assertEqual(calls, [])
        self.assertEqual(self.store.state("p1", "c1"), "awaiting_location")

    def test_claim_serializes_each_conversation(self):
        self.store.enqueue(event('giá', message_id='m1'))
        self.store.enqueue(event('xin ảnh', message_id='m2'))
        other = event('giá', message_id='m3')
        other['data']['conversation']['id'] = 'c2'
        other['data']['message']['conversation_id'] = 'c2'
        self.store.enqueue(other)
        first = self.store.claim()
        another_worker = Store(self.store.path)
        second = another_worker.claim()
        self.assertEqual(first['message'], 'm1')
        self.assertEqual(second['conversation'], 'c2')
        self.assertIsNone(another_worker.claim())
        self.store.finish(first, 'sent', '{}', lead={'introduced': True})
        third = another_worker.claim()
        self.assertEqual(third['message'], 'm2')
        self.assertTrue(another_worker.lead('p1', 'c1')['introduced'])

    def test_page_array_and_page_specific_scripts(self):
        path = os.path.join(self.tmp.name, 'config.json')
        script_path = os.path.join(self.tmp.name, 'scripts.json')
        scripts = {'pages': {page: {'groups': {'2': [{'message': page}]}, 'prompts': {}}
                             for page in ('p1', 'p2')}}
        with open(script_path, 'w', encoding='utf-8') as file:
            json.dump(scripts, file)
        config = {'worker_count': 2, 'pages': [
            {'page_id': page, 'sales_script': 'scripts.json', 'token_env': page + '_TOKEN'}
            for page in ('p1', 'p2')]}
        with open(path, 'w', encoding='utf-8') as file:
            json.dump(config, file)
        loaded = load_config(path)
        self.assertEqual(loaded['worker_count'], 2)
        for page in ('p1', 'p2'):
            self.assertEqual(loaded['pages'][page]['_sales_script']['groups']['2'][0]['message'], page)
            self.assertEqual(loaded['pages'][page]['token_env'], page + '_TOKEN')
        config['pages'].append(config['pages'][0])
        with open(path, 'w', encoding='utf-8') as file:
            json.dump(config, file)
        with self.assertRaisesRegex(ValueError, 'Duplicate page_id'):
            load_config(path)

    def test_multistep_queued_events(self):
        self.store.enqueue(event())
        followup = event("Hà Nội", message_id="m2")
        self.assertTrue(plan(self.config, followup, validate_only=True))
        self.store.enqueue(followup)
        process_one(self.store, self.config)
        process_one(self.store, self.config)
        self.assertEqual(self.store.state("p1", "c1"), "human")
        self.assertIsNone(plan(self.config, event(), state="human"))

    def test_private_reply(self):
        rule = {"id": "private", "action": "private_reply", "reply": "Xin chào"}
        self.config["pages"]["p1"]["rules"] = [rule]
        body = plan(self.config, event(channel="COMMENT"))["body"]
        self.assertEqual(body["action"], "private_replies")
        self.assertEqual(body["post_id"], "post1")
        self.assertEqual(body["from_id"], "u1")
        self.assertIsNone(plan(self.config, event()))

    def test_unknown_send_not_retried_or_advance_state(self):
        self.store.enqueue(event())
        with patch.dict(os.environ, {"PANCAKE_PAGE_1_TOKEN": "test"}):
            process_one(self.store, self.config, live=True, sender=lambda *a: "unknown")
        self.assertFalse(process_one(self.store, self.config))
        self.assertEqual(self.store.state("p1", "c1"), "start")

    def test_api_request_contract(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"success": true}'
        with patch("bot.urlopen", return_value=Response()) as request:
            self.assertEqual(send("p1", "c/1", "secret", {"action": "reply_inbox", "message": "Hi"}), "sent")
            req = request.call_args.args[0]
            self.assertIn("/api/public_api/v1/pages/p1/conversations/c%2F1/messages?", req.full_url)
            self.assertEqual(req.method, "POST")
            self.assertEqual(json.loads(req.data)["action"], "reply_inbox")

    def test_config_token_preferred_and_env_optional(self):
        with patch.dict(os.environ, {"TEST_TOKEN": "env-token"}):
            self.assertEqual(page_token({"page_access_token": " config-token ", "token_env": "TEST_TOKEN"}), "config-token")
            self.assertEqual(page_token({"token_env": "TEST_TOKEN"}), "env-token")
            self.assertEqual(page_token({"page_access_token": "direct-token"}), "direct-token")
        with self.assertRaisesRegex(ValueError, "Missing page_access_token"):
            page_token({})

    def test_live_send_with_config_token(self):
        page = self.config["pages"]["p1"]
        page.pop("token_env")
        page["page_access_token"] = "config-token"
        self.store.enqueue(event())
        calls = []
        def sender(*args):
            calls.append(args)
            return "sent"
        process_one(self.store, self.config, live=True, sender=sender)
        self.assertEqual(calls[0][2], "config-token")


if __name__ == "__main__":
    unittest.main()

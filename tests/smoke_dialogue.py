"""Synthetic API smoke test. Never sends Pancake messages or uses production DB."""
import argparse
import copy
import json
import logging
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bot import Store, load_config
from dialogue import respond


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-llm', action='store_true', help='Allow paid OpenAI calls, local budget at most USD 0.05')
    args = parser.parse_args()
    if not args.live_llm:
        parser.error('--live-llm required; this test uses paid API calls')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    handler = logging.StreamHandler()
    handler.addFilter(lambda record: 'step=dialogue_failure' in record.getMessage() or 'step=dialogue_interpretation' in record.getMessage())
    logging.getLogger('pancake.llm').addHandler(handler)
    logging.getLogger('pancake.llm').setLevel(logging.INFO)
    config = load_config('config.json')
    page = copy.deepcopy(next(p for p in config['pages'].values() if p.get('_sales_script')))
    page['llm'].update(enabled=True, daily_budget_usd=.05, max_calls_per_conversation=10)
    samples = ['chị 55kg, thích mặc rộng chút', 'vậy lấy M nha em',
               '0912345678, địa chỉ: 12 Nguyễn Trãi, phường Thanh Xuân Trung, Hà Nội', 'ừ em', 'chị muốn mua thêm',
               'ừ mở đơn mới đi em', 'dùng lại size, số điện thoại và địa chỉ như lần trước nhé', 'ừ dùng lại nhé em']
    expected = ['reply', 'reply', 'offer_order', 'confirm_order', 'offer_reorder', 'confirm_reorder', 'offer_reuse', 'confirm_reuse']
    failures = []
    with tempfile.TemporaryDirectory() as root:
        store = Store(str(Path(root) / 'synthetic.sqlite3'))
        for i, text in enumerate(samples):
            event = {'page_id': 'synthetic', 'event_type': 'messaging', 'data': {
                'conversation': {'id': 'synthetic', 'from': {'id': 'synthetic-user'}},
                'message': {'id': str(i), 'type': 'INBOX', 'message': text,
                            'from': {'id': 'synthetic-user'}, 'inserted_at': datetime.now(timezone.utc).isoformat()}}}
            store.enqueue(event)
            job = store.claim()
            previous = store.lead('synthetic', 'synthetic')
            decision = respond(store, page, event, previous)
            print('CUSTOMER:', text)
            print('BOT:', decision['body']['message'] if decision else '(no reply)')
            print('RESULT:', decision['rule'] if decision else 'ignored')
            if not decision or decision['rule'] != 'dialogue:' + expected[i]:
                failures.append((i, expected[i], decision['rule'] if decision else 'ignored'))
            store.finish(job, 'dry_run' if decision else 'ignored', json.dumps(decision),
                         decision['next_state'] if decision else None, decision['lead'] if decision else None)
        with store.connect() as db:
            print('API_USAGE:', [dict(r) for r in db.execute('SELECT status,input_tokens,output_tokens,charged_microusd FROM llm_calls')])
        if failures:
            raise AssertionError(f'Unexpected dialogue actions: {failures}')
        print('PASS: purchase confirmation, new session and consented reuse')


if __name__ == '__main__':
    main()

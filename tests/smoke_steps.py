"""Optional paid step-support smoke test; temporary DB, no Pancake messages."""
import argparse
import copy
import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot import Store, load_config
from sales import decide
from llm import assist


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live-llm',action='store_true')
    if not parser.parse_args().live_llm:
        parser.error('--live-llm required; API is paid, budget USD 0.02 maximum per run')
    sys.stdout.reconfigure(encoding='utf-8')
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(lambda record: 'step=llm_validation' in record.getMessage() or 'step=llm_failure' in record.getMessage())
    logging.getLogger('pancake.llm').addHandler(handler)
    logging.getLogger('pancake.llm').setLevel(logging.INFO)
    page = copy.deepcopy(next(p for p in load_config('config.json')['pages'].values() if p.get('_sales_script')))
    page['llm'].update(enabled=True,daily_budget_usd=.02,max_calls_per_conversation=6)
    with tempfile.TemporaryDirectory() as root:
        store = Store(str(Path(root)/'steps.sqlite3'))
        previous = dict(stage='address',size='M',phone='0912345678',introduced=True,session_id='synthetic')
        for index,text in enumerate(['La Thạch','Liên Minh Hà Nội','ừ em']):
            event = {'page_id':'synthetic','event_type':'messaging','data':{
                'conversation':{'id':'synthetic','from':{'id':'customer'}},
                'message':{'id':str(index),'type':'INBOX','message':text,'from':{'id':'customer'},
                           'inserted_at':datetime.now(timezone.utc).isoformat()}}}
            store.enqueue(event)
            job = store.claim()
            draft = decide(page['_sales_script'],event,previous)
            assistance = assist(store,page,event,previous,draft,live=True)
            result = decide(page['_sales_script'],event,previous,{**(assistance or {}),'keep_clarifying':True})
            print(json.dumps({'customer':text,'assistance':assistance,'stage':result['lead']['stage'],
                              'replies':result['bodies']},ensure_ascii=False))
            store.finish(job,'sent',json.dumps(result),result['next_state'],result['lead'])
            previous = result['lead']
            assert assistance is not None, 'No validated model assistance'
            assert previous['stage'] == ['address','confirm','complete'][index], 'Unexpected stage'
        with store.connect() as db:
            print('USAGE', [dict(r) for r in db.execute('SELECT status,charged_microusd FROM llm_calls')])
        print('PASS: contextual address clarification, composition, confirmation')


if __name__ == '__main__':
    main()

import copy
import json
import tempfile
import time
import unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace

import test_catalog as fixtures
from analysis_queue import drain,pending_repos,pause_reason
from radar import analysis_pack,input_hash,config,salvage_batch,normalize_line_numbers,analyze
from github_client import GitHub
from unittest.mock import patch


class QueueTests(unittest.TestCase):
    setUp=fixtures.CatalogTests.setUp
    tearDown=fixtures.CatalogTests.tearDown
    add=fixtures.CatalogTests.add

    def prepare(self,count):
        bodies={}
        for rid in range(1,count+1):bodies[rid]=self.add(rid)
        self.store.db.execute('DELETE FROM analyses');self.store.db.commit()
        events=[]
        def evidence(repo,**kwargs):
            events.append(('evidence',repo['id']))
            return {'repo_id':repo['id'],'pieces':[{'id':'readme','url':repo['html_url'],'lines':['A document toolkit.']}]}
        def analyze(store,packs,*args):
            events.append(('analyze',len(packs)))
            return {p['repo_id']:copy.deepcopy(bodies[p['repo_id']]) for p in packs}
        return SimpleNamespace(evidence=evidence),analyze,events,bodies

    def run_queue(self,api,analyze,salvage=lambda *a:{}):
        return drain(self.store,dict(config(),analysis_batch_size=4),api,self.root,time.monotonic()+600,analyze,analysis_pack,input_hash,
                     lambda:datetime.now(timezone.utc),salvage)

    def test_all_25_analyzed_streaming_without_old_caps(self):
        api,analyze,events,_=self.prepare(25)
        result=self.run_queue(api,analyze)
        self.assertEqual(result['analyzed'],25)
        self.assertEqual(result['remaining'],0)
        self.assertEqual(events[4],('analyze',4))
        self.assertTrue(all(c.get('analysis') for c in result['contexts']))
        self.assertEqual(pending_repos(self.store),[])

    def test_bad_item_isolated_valid_others_saved_no_blind_retry(self):
        api,_,_,bodies=self.prepare(4)
        def bad(*args):raise ValueError('invalid source anchor')
        result=self.run_queue(api,bad,lambda *args:{i:bodies[i] for i in (1,2,3)})
        self.assertEqual(result['analyzed'],3)
        self.assertEqual(result['remaining'],1)
        self.assertTrue(self.store.get('analysis-blocked:4'))
        def forbidden(*a):raise AssertionError('must not retry rejected item')
        self.run_queue(SimpleNamespace(evidence=forbidden),forbidden)

    def test_budget_failure_stops_batch_keeps_pending(self):
        api,_,events,_=self.prepare(9)
        def budget(*args):raise ValueError('今日DS预算不足')
        result=self.run_queue(api,budget)
        self.assertEqual(result['analyzed'],0)
        self.assertEqual(result['remaining'],9)
        self.assertEqual(len(events),4)
        self.assertTrue(result['errors'])
        self.assertIsNone(self.store.get('analysis-blocked:1'))

    def test_exact_quote_salvage_does_not_fabricate_bad_quotes(self):
        _,_,_,bodies=self.prepare(2)
        packs=[{'repo_id':i,'pieces':[{'id':'readme','url':'https://github.com/a/b','lines':['A document toolkit.']}]} for i in (1,2)]
        for a in bodies.values():a['repo_id']=1
        bodies[1]['claims'][0]['line']=99
        bodies[2]['repo_id']=2;bodies[2]['claims'][0]['quote']='never present quotation'
        (self.root/'batch-0.output.json').write_text(json.dumps({'projects':list(bodies.values())}),encoding='utf-8')
        result=salvage_batch(self.root,0,packs)
        self.assertEqual(set(result),{1})
        self.assertEqual(result[1]['claims'][0]['line'],1)

    def test_transient_readonly_model_request_has_two_bounded_retries(self):
        api,_,_,bodies=self.prepare(4)
        calls=[]
        def transient(store,packs,cfg,folder,index,*args):
            calls.append(str(folder))
            if len(calls)<3:
                (Path(folder)/f'batch-{index}.process.log').write_text('WinError 10054',encoding='utf-8')
                raise ValueError('DS调用失败')
            return bodies
        result=self.run_queue(api,transient)
        self.assertEqual(result['analyzed'],4)
        self.assertEqual(len(set(calls)),3)

    def test_parallel_fetch_uses_separate_sqlite_connections(self):
        self.prepare(6)
        api=GitHub(self.store,time.monotonic()+60,credentials=(None,'test'))
        def evidence(client,repo,**kwargs):
            client.store.db.execute('SELECT COUNT(*) FROM repos').fetchone()
            return {'repo_id':repo['id'],'pieces':[]}
        with patch('github_client.GitHub.evidence',evidence):
            result=api.evidence_many(self.store.repos())
        self.assertEqual(len(result),6)
        self.assertTrue(all(not isinstance(v,Exception) for v in result.values()))

    def test_network_failure_budget_explanation_does_not_misclassify_pause(self):
        api,_,_,_=self.prepare(4)
        def failure(*args):raise ValueError('DS调用失败或超时；预算及原始结果已保留')
        self.run_queue(api,failure)
        self.assertEqual(self.store.get('analysis-pause')['kind'],'network')

    def test_unknown_maturity_downgrades_to_reference_and_keeps_original(self):
        body,changes=normalize_line_numbers({'projects':[{'repo_id':1,'kind':'成熟潜力'}]})
        self.assertEqual(body['projects'][0]['kind'],'值得借鉴')
        self.assertEqual(body['projects'][0]['_original_kind'],'成熟潜力')
        self.assertTrue(changes)

    def test_budget_pause_resumes_next_beijing_day(self):
        self.store.put('analysis-pause',{'kind':'budget','day':'2026-10-15','reason':'预算不足'})
        self.assertIsNone(pause_reason(self.store,config(),self.today))
        self.assertIsNone(self.store.get('analysis-pause'))

    def test_json_syntax_gets_one_separate_budgeted_format_pass(self):
        api,_,_,bodies=self.prepare(1)
        bodies[1]['repo_id']=1
        pack=api.evidence(self.store.repos()[0])
        calls=[]
        traces=[]
        def process(argv,**kwargs):
            calls.append(argv)
            traces.append(kwargs['env']['DEEPSEEK_TRACE_ID'])
            target=Path(argv[argv.index('--out')+1])
            target.write_text('{bad JSON' if len(calls)==1 else json.dumps({'projects':[bodies[1]]}),encoding='utf-8')
            return SimpleNamespace(returncode=0,stdout=b'',stderr=b'')
        with patch('radar.subprocess.run',process):
            accepted=analyze(self.store,[pack],config(),self.root,0,time.monotonic()+120)
        self.assertEqual(set(accepted),{1})
        self.assertEqual(len(calls),2)
        self.assertEqual(len(set(traces)),2)
        self.assertTrue((self.root/'batch-0-format-repair/batch-0.output.json').exists())
        self.assertNotEqual(calls[0][calls[0].index('--in')+1],calls[1][calls[1].index('--in')+1])

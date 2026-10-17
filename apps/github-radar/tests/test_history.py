"""Historical coverage, independent discovery sharing and offline deduplication."""
import json
from datetime import timedelta
from unittest.mock import patch

import unittest
import test_catalog as fixtures
from catalog import latest_rows, validate_sharing
from history import query_plan, current_page, advance_page, library_records, search_library, story, write_history, mark_sharing_published
from notification import eligible, notify_discoveries
from radar import normalize_line_numbers


class HistoryTests(unittest.TestCase):
    setUp = fixtures.CatalogTests.setUp
    tearDown = fixtures.CatalogTests.tearDown
    add = fixtures.CatalogTests.add
    def test_forced_classic_and_playful_with_rotation(self):
        cfg={'searches_per_scan':3,'queries':[{'category':c,'q':str(i)} for i,c in enumerate(['科研','效率','经典回溯','趣味探索','探索'])]}
        first=query_plan(self.store,cfg)
        self.assertEqual({q['category'] for q in first},{'科研','经典回溯','趣味探索'})
        self.assertNotEqual(first,query_plan(self.store,cfg))
        cfg['searches_per_scan']=1
        self.assertEqual(len(query_plan(self.store,cfg)),1)
        self.assertEqual(len(query_plan(self.store,{'searches_per_scan':1,'queries':[{'category':'科研'}]})),1)

    def test_pagination_caps_and_survives_rolling_dates(self):
        query='topic:x created:<2024-10-16'
        self.assertEqual(current_page(self.store,query,'stars',20),1)
        advance_page(self.store,query,'stars',20,99999)
        self.assertEqual(current_page(self.store,'topic:x created:<2024-10-17','stars',20),2)
        for _ in range(49): advance_page(self.store,query,'stars',20,99999)
        self.assertEqual(current_page(self.store,query,'stars',20),1)
        self.assertEqual(current_page(self.store,'topic:y','stars',20),1)
        advance_page(self.store,query,'stars',20,0)
        self.assertEqual(current_page(self.store,query,'stars',20),1)

    def test_library_includes_pending_ignored_low_relevance_and_aliases(self):
        self.add(1,score=1,codex=False)
        self.add(2)
        self.store.set_feedback(2,'ignore','irrelevant','now')
        self.store.upsert_repo({'id':3,'full_name':'old/classic','html_url':'https://github.com/old/classic'},'2020-01-01')
        self.store.upsert_repo({'id':3,'full_name':'new/classic','html_url':'https://github.com/new/classic'},'2026-10-16')
        records=library_records(self.store)
        self.assertEqual([r['status'] for r in records],['已分析','已忽略','待分析'])
        self.assertEqual(latest_rows(self.store),[])
        found=search_library(self.store,'old/classic')
        self.assertEqual(found['total_matches'],1)
        self.assertEqual(found['items'][0]['first_seen'],'2020-01-01')
        self.assertEqual(search_library(self.store,status='已忽略')['total_matches'],1)
        write_history(self.store,self.root)
        data=json.loads((self.root/'reports/library/index.json').read_text(encoding='utf-8'))
        self.assertEqual(data['count'],3)
        self.assertTrue(data['projects'][0]['analysis']['claims'])

    def test_fun_sharing_does_not_require_utility_or_codex_and_stable_id(self):
        self.add(1,score=1,codex=False)
        repo=self.store.repos()[0];repo.update(description='creative game',created_at='2010-01-01T00:00:00Z')
        self.store.upsert_repo(repo,'now')
        row=latest_rows(self.store,include_exploration=True)[0]
        one,_=story(row)
        self.assertTrue(one['classic'])
        self.assertEqual(one['kind'],'有趣发现')
        row['repo']['stargazers_count']+=100
        row['reviewed_at']='2030-01-01'
        two,_=story(row)
        self.assertEqual(one['share_id'],two['share_id'])
        write_history(self.store,self.root)
        packet=json.loads((self.root/'reports/sharing.json').read_text(encoding='utf-8'))
        self.assertEqual(len(packet['items']),1)
        self.assertIsNone(self.store.get('sharing-published:1'))
        mark_sharing_published(self.store,self.root)
        self.assertEqual(self.store.get('sharing-published:1')['share_id'],one['share_id'])

    def test_notification_dedup_cooldown_and_failure_no_false_delivery(self):
        items=[{'repo_id':1,'share_id':'new'}]
        old={'1':{'share_id':'old','at':self.today.isoformat()}}
        self.assertEqual(eligible(items,old,self.today+timedelta(days=1)),[])
        self.assertEqual(eligible(items,old,self.today+timedelta(days=15)),items)
        old['1']['share_id']='new'
        self.assertEqual(eligible(items,old,self.today+timedelta(days=100)),[])
        self.add(1);write_history(self.store,self.root)
        with patch('notification.sys.platform','win32'),patch('notification.subprocess.run',side_effect=OSError):
            self.assertEqual(notify_discoveries(self.store,self.root,{})['status'],'failed')
        self.assertIsNone(self.store.get('notification-delivered'))

    def test_sharing_rejects_invalid_model_values(self):
        for value in ({'why':'x','try_idea':'y','curiosity':True},{'why':'x','try_idea':'y','curiosity':float('nan')},{'why':'x'}):
            with self.assertRaises(ValueError): validate_sharing(value)

    def test_line_normalization_only_canonical_numeric_strings(self):
        body={'projects':[{'repo_id':1,'claims':[{'line':v} for v in ['3','003','3.0',True,-1,3,'unknown']]}]}
        repaired,changes=normalize_line_numbers(body)
        self.assertEqual([x['line'] for x in repaired['projects'][0]['claims']],[3,'003','3.0',True,-1,3,'unknown'])
        self.assertEqual(len(changes),1)
        self.assertEqual(body['projects'][0]['claims'][0]['line'],'3')

"""Catalog ranking, evidence integrity and advisory-only deployment policy."""
import copy
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from radar_store import Store
from catalog import latest_rows, recommendation, validate_codex, write_catalog


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state.db')
        self.today = datetime(2026,10,16,tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, rid, score=4, at='2026-10-15T00:00:00+00:00', codex=True):
        repo = {'id': rid, 'full_name': f'owner/project-{rid}', 'html_url': f'https://github.com/owner/project-{rid}',
                'stargazers_count': 1000, 'archived': False}
        self.store.upsert_repo(repo, at)
        analysis = {'summary': '科研文档', 'fit': '适合科研流程', 'difference': '尚需比较', 'adoption': 'Windows未实测',
                    'kind': '新兴潜力', 'scores': {k: score for k in ('relevance','value','adoption','engineering','community')},
                    'unknowns': ['未运行'], 'claims': [{'text': '文档工具', 'source': 'readme', 'line': 1, 'quote': 'document toolkit'}],
                    '_sources': {'readme': {'id': 'readme', 'url': repo['html_url'], 'lines': ['A document toolkit.']}}}
        if codex:
            analysis['codex'] = {'integration': 'skill', 'priority': '高', 'reason': '科研文档工作流', 'overlap': '需与已有工具对照',
                                 'requirements': '待检查依赖', 'risk': '脚本未审查', 'next_step': '确认具体SKILL.md'}
        self.store.save_analysis(rid, 'hash-'+at, analysis, at)
        return analysis

    def test_top50_is_lifetime_deduplicated_and_not_filled(self):
        self.add(1)
        self.add(1, at='2026-10-16T00:00:00+00:00')
        self.add(2)
        result = write_catalog(self.store, self.root, {}, self.today)
        self.assertEqual(result['top_count'], 2)
        data = json.loads((self.root/'reports/catalog.json').read_text(encoding='utf-8'))
        self.assertEqual(data['capacity'], 50)
        self.assertEqual(len({x['repo_id'] for x in data['projects']}), 2)

    def test_latest_lower_score_does_not_revive_old_peak(self):
        self.add(1, score=5)
        self.add(1, score=1, at='2026-10-16T00:00:00+00:00')
        self.assertEqual(latest_rows(self.store, self.today), [])

    def test_top50_capacity_and_detail_links(self):
        for rid in range(1,55):
            self.add(rid)
        result = write_catalog(self.store, self.root, {}, self.today)
        self.assertEqual(result['qualified_pool'], 54)
        self.assertEqual(result['top_count'], 50)
        self.assertEqual(len(list((self.root/'reports/projects').glob('*.md'))), 50)
        self.assertIn('只提示高价值', (self.root/'reports/codex-deployment.md').read_text(encoding='utf-8'))

    def test_excluded_feedback_and_known_remains_in_history(self):
        for rid in range(1,5):
            self.add(rid)
        self.store.set_feedback(1,'ignore','not useful','now')
        self.store.set_feedback(2,'later','not now','now')
        self.store.set_feedback(3,'tried_failed','failed','now')
        self.store.set_feedback(4,'known','already known','now')
        self.assertEqual([r['repo_id'] for r in latest_rows(self.store,self.today)],[4])

    def test_expired_unknown_application_and_archived_never_high_candidate(self):
        self.add(1, at='2026-08-01T00:00:00+00:00')
        self.add(2, codex=False)
        self.add(3)
        rows = {r['repo_id']:r for r in latest_rows(self.store,self.today)}
        self.assertNotIn('高价值', recommendation(rows[1]))
        self.assertNotIn('高价值', recommendation(rows[2]))
        for kind in ('application','reference','unknown'):
            row = copy.deepcopy(rows[3]); row['codex']['integration'] = kind
            self.assertNotIn('高价值', recommendation(row))
        row = copy.deepcopy(rows[3]); row['repo']['archived'] = True
        self.assertNotIn('高价值', recommendation(row))

    def test_rejects_fake_deployment_approval_and_wrong_type(self):
        analysis = self.add(1)
        value = dict(analysis['codex'], installed=True)
        with self.assertRaises(ValueError): validate_codex(value)
        value = dict(analysis['codex'], integration='run_arbitrary_script')
        with self.assertRaises(ValueError): validate_codex(value)

    def test_stored_bad_anchor_is_not_ranked(self):
        analysis = self.add(1)
        analysis['claims'][0]['quote'] = 'This quotation never existed'
        self.store.save_analysis(1,'bad',analysis,'2026-10-16T00:00:00+00:00')
        self.assertEqual(latest_rows(self.store,self.today),[])

    def test_codex_only_update_does_not_refresh_old_general_analysis(self):
        analysis = self.add(1, at='2026-08-01T00:00:00+00:00')
        analysis['_base_reviewed_at'] = '2026-08-01T00:00:00+00:00'
        analysis['_codex_reviewed_at'] = '2026-10-16T00:00:00+00:00'
        self.store.save_analysis(1,'codex-update',analysis,'2026-10-16T00:00:00+00:00')
        row = latest_rows(self.store,self.today)[0]
        self.assertTrue(row['stale'])
        self.assertEqual(row['reviewed_at'],'2026-08-01T00:00:00+00:00')
        self.assertNotIn('高价值',recommendation(row))

    def test_expiry_boundary_uses_real_elapsed_time_not_rounded_days(self):
        self.add(1, at='2026-09-15T12:00:00+00:00')
        row=latest_rows(self.store,self.today)[0]
        self.assertEqual(row['age_days'],30)
        self.assertTrue(row['stale'])

    def test_fixed_pages_escape_remote_html_and_preserve_existing_backup(self):
        analysis = self.add(1)
        analysis['codex']['reason'] = '<script>alert(1)</script>'
        self.store.save_analysis(1,'new',analysis,'2026-10-16T00:00:00+00:00')
        write_catalog(self.store,self.root,{},self.today)
        text = (self.root/'reports/projects/1.md').read_text(encoding='utf-8')
        self.assertNotIn('<script>',text)
        self.add(2)
        write_catalog(self.store,self.root,{},self.today)
        self.assertTrue(list((self.root/'backups/catalog').glob('*.bak')))

    def test_catalog_rebuild_needs_no_network_or_model(self):
        self.add(1)
        from unittest.mock import patch
        with patch('subprocess.run', side_effect=AssertionError('must be local')):
            result = write_catalog(self.store,self.root,{},self.today)
        self.assertEqual(result['top_count'],1)

    def test_main_review_correction_is_bound_to_evidence_version(self):
        analysis = self.add(1)
        row = latest_rows(self.store,self.today)[0]
        self.store.put('codex-review:1',{'input_hash':row['input_hash'],'at':'2026-10-16T00:00:00+00:00',
                                        'overrides':{'overlap':'人工复核：有重叠，尚需对照'}})
        self.assertIn('人工复核',latest_rows(self.store,self.today)[0]['codex']['overlap'])
        self.store.save_analysis(1,'new-material',analysis,'2026-10-16T01:00:00+00:00')
        self.assertNotIn('人工复核',latest_rows(self.store,self.today)[0]['codex']['overlap'])


if __name__ == '__main__': unittest.main()

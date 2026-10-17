"""Behavior tests with isolated temp folders; no real API calls or credentials."""
import concurrent.futures
import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import radar
from radar_store import FileLock, Store
from reports import cleanup, render, write_report
from publisher import PublishError, publish_reports


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.store = Store(self.path / 'state.db')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_unknown_cost_remains_reserved_and_duplicates_rejected(self):
        self.assertTrue(self.store.reserve('a', '2026-10-16', 60, 100))
        self.store.settle('a', None)
        self.assertFalse(self.store.reserve('b', '2026-10-16', 41, 100))
        self.assertEqual(self.store.budget('2026-10-16')['unknown_count'], 1)
        with self.assertRaises(ValueError):
            self.store.reserve('a', '2026-10-16', 1, 100)
        with self.assertRaises(ValueError):
            self.store.settle('a', 0)

    def test_actual_cost_and_overrun(self):
        self.store.reserve('a', '2026-10-16', 60, 100)
        self.store.settle('a', 30)
        self.assertTrue(self.store.reserve('b', '2026-10-16', 70, 100))
        self.store.settle('b', 80)
        self.assertEqual(self.store.budget('2026-10-16')['charged_micro'], 110)
        self.assertFalse(self.store.reserve('c', '2026-10-16', 1, 100))
        self.assertTrue(self.store.reserve('d', '2026-10-17', 100, 100))

    def test_money_rejects_bool_negative_and_noninteger(self):
        for value in (True, -1, 1.5, float('nan')):
            with self.assertRaises(ValueError):
                self.store.reserve('x', '2026-10-16', value, 100)

    def test_two_connections_cannot_double_spend(self):
        def reserve(trace):
            s = Store(self.path / 'state.db')
            try:
                return s.reserve(trace, '2026-10-16', 60, 100)
            finally:
                s.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ('first', 'second')))
        self.assertEqual(sum(results), 1)

    def test_growth_uses_real_interval_and_oldest_observation(self):
        self.assertIsNone(self.store.growth(1, '2026-10-16T00:00:00+00:00', 90))
        self.store.snapshot(1, '2026-10-13T00:00:00+00:00', 80)
        self.store.snapshot(1, '2026-10-15T00:00:00+00:00', 85)
        self.assertEqual(self.store.growth(1, '2026-10-16T00:00:00+00:00', 90), {'days': 3.0, 'net_stars': 10})
        self.assertIsNone(self.store.growth(2, '2026-10-16T00:00:00+00:00', 10))

    def test_repo_identity_survives_rename(self):
        self.store.upsert_repo({'id': 123, 'full_name': 'old/name'}, '2026-10-15')
        self.store.upsert_repo({'id': 123, 'full_name': 'new/name'}, '2026-10-16')
        self.assertEqual(self.store.repos(), [{'id': 123, 'full_name': 'new/name', '_known_names': ['old/name']}])

    def test_lock_excludes_another_process_and_recovers(self):
        lock = self.path / 'scan.lock'
        code = "import sys;sys.path.insert(0,sys.argv[1]);from radar_store import FileLock;FileLock(__import__('pathlib').Path(sys.argv[2])).__enter__()"
        with FileLock(lock):
            result = subprocess.run([sys.executable, '-I', '-c', code, str(SCRIPTS), str(lock)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
        with FileLock(lock):
            pass


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.packs = [{'repo_id': 1, 'pieces': [{'id': 'readme', 'url': 'https://github.com/a/b',
                                               'lines': ['A scientific research toolkit for documents.']}]}]
        self.item = {'repo_id': 1, 'summary': '工具', 'fit': '处理文档', 'difference': '尚未比较', 'adoption': 'Windows未验证',
                     'kind': '新兴潜力', 'scores': {k: 3 for k in radar.WEIGHTS}, 'unknowns': ['未实测'],
                     'claims': [{'text': '科研工具', 'source': 'readme', 'line': 1, 'quote': 'scientific research toolkit'}]}
        self.item['codex'] = {'integration': 'skill', 'priority': '高', 'reason': '文档工作流',
                              'overlap': '已有文档技能，需对照增量', 'requirements': '依赖未验证',
                              'risk': '需检查脚本', 'next_step': '确认具体SKILL.md及依赖'}

    def test_accepts_exact_supported_anchor(self):
        self.assertIn(1, radar.validate_analysis({'projects': [copy.deepcopy(self.item)]}, self.packs))

    def test_codex_analysis_must_not_be_omitted_or_assert_installation(self):
        item = copy.deepcopy(self.item)
        del item['codex']
        with self.assertRaises(ValueError):
            radar.validate_analysis({'projects': [item]}, self.packs)

    def test_codex_only_batch_requires_exact_ids_and_no_rewritten_facts(self):
        valid = {'projects': [{'repo_id': 1, 'codex': copy.deepcopy(self.item['codex'])}]}
        self.assertIn(1, radar.validate_codex_batch(valid, self.packs))
        for item in ({'repo_id': 2, 'codex': self.item['codex']},
                     {'repo_id': True, 'codex': self.item['codex']},
                     {'repo_id': 1, 'codex': self.item['codex'], 'summary': 'rewritten'}):
            with self.assertRaises(ValueError):
                radar.validate_codex_batch({'projects':[item]},self.packs)
        item = copy.deepcopy(self.item)
        item['codex']['installed'] = True
        with self.assertRaises(ValueError):
            radar.validate_analysis({'projects': [item]}, self.packs)

    def test_rejects_missing_duplicate_wrong_id_and_nan(self):
        for items in ([], [self.item, self.item], [dict(self.item, repo_id=2)]):
            with self.assertRaises(ValueError):
                radar.validate_analysis({'projects': items}, self.packs)
        item = copy.deepcopy(self.item)
        item['scores']['adoption'] = float('nan')
        with self.assertRaises(ValueError):
            radar.validate_analysis({'projects': [item]}, self.packs)

    def test_rejects_fabricated_quote_bool_line_and_outside_anchor(self):
        for change in ({'quote': 'This quote does not exist'}, {'line': True}, {'line': 2}, {'source': 'missing'}):
            item = copy.deepcopy(self.item)
            item['claims'][0].update(change)
            with self.assertRaises(ValueError):
                radar.validate_analysis({'projects': [item]}, self.packs)

    def test_cache_ignores_star_and_push_but_not_readme(self):
        pack = {'repo_id': 1, 'pieces': [{'id': 'metadata', 'lines': [json.dumps({'id': 1, 'stargazers_count': 3, 'pushed_at': 'old'})]},
                                        {'id': 'readme', 'lines': ['functional text']}], 'warnings': []}
        other = copy.deepcopy(pack)
        other['pieces'][0]['lines'] = [json.dumps({'id': 1, 'stargazers_count': 10, 'pushed_at': 'new'})]
        cfg = {'profile': 'research'}
        first = radar.input_hash(radar.analysis_pack(pack), cfg)
        self.assertEqual(first, radar.input_hash(radar.analysis_pack(other), cfg))
        other['pieces'][1]['lines'][0] = 'new function'
        self.assertNotEqual(first, radar.input_hash(radar.analysis_pack(other), cfg))

    def test_budget_blocks_before_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'state.db')
            cfg = radar.config()
            cfg['daily_budget_cny'] = 0.000001
            with patch('radar.subprocess.run') as process:
                with self.assertRaisesRegex(ValueError, '预算不足'):
                    radar.analyze(store, self.packs, cfg, Path(folder), 0, __import__('time').monotonic()+100)
                process.assert_not_called()
            store.close()

    def test_duplicate_trace_blocks_before_paid_dispatch(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder)/'state.db')
            day = radar.now().astimezone(radar.BJ).date().isoformat()
            store.reserve('radar-fixed', day, 10, 20000000)
            with patch('radar.uuid.uuid4', return_value=SimpleNamespace(hex='fixed')), \
                 patch('radar.subprocess.run') as process:
                with self.assertRaisesRegex(ValueError, 'duplicate'):
                    radar.analyze(store, self.packs, radar.config(), Path(folder), 0, __import__('time').monotonic()+100)
                process.assert_not_called()
            self.assertEqual(store.budget(day)['charged_micro'], 10)
            store.close()


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state.db')
        self.folder = self.root/'reports'
        self.folder.mkdir()
        self.today = datetime(2026, 10, 16, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def register(self, i, age=40):
        rid = '20260901-090000-%08x' % i
        name = 'github-report-' + rid + '.md'
        target = self.folder / name
        target.write_text('generated', encoding='utf-8')
        self.store.save_report(rid, (self.today-timedelta(days=age, minutes=i)).isoformat(), str(self.folder),
                               [{'name': name, 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}])
        return target

    def test_keep_at_least_ten_and_unknown_files_untouched(self):
        paths = [self.register(i) for i in range(12)]
        unknown = self.folder/'my-notes.md'
        unknown.write_text('personal')
        preview = cleanup(self.store, self.folder, now=self.today)
        self.assertEqual(len(preview['eligible']), 2)
        self.assertTrue(all(p.exists() for p in paths))
        result = cleanup(self.store, self.folder, apply=True, now=self.today)
        self.assertEqual(len(result['deleted']), 2)
        self.assertEqual(sum(p.exists() for p in paths), 10)
        self.assertTrue(unknown.exists())

    def test_edited_report_protected(self):
        paths = [self.register(i) for i in range(11)]
        paths[-1].write_text('user edited')
        result = cleanup(self.store, self.folder, apply=True, now=self.today)
        self.assertEqual(len(result['deleted']), 0)
        self.assertEqual(len(result['protected']), 1)

    def test_recent_report_retained_even_beyond_count(self):
        paths = [self.register(i, age=10) for i in range(12)]
        cleanup(self.store, self.folder, apply=True, now=self.today)
        self.assertTrue(all(p.exists() for p in paths))

    def test_manifest_cannot_escape_folder(self):
        for i in range(10):
            self.register(i, age=5)
        outside = self.root/'private.txt'
        outside.write_text('keep')
        self.store.save_report('evil', (self.today-timedelta(days=40)).isoformat(), str(self.folder),
                               [{'name': '../private.txt', 'sha256': hashlib.sha256(outside.read_bytes()).hexdigest()}])
        result = cleanup(self.store, self.folder, apply=True, now=self.today)
        self.assertEqual(len(result['protected']), 1)
        self.assertTrue(outside.exists())

    def test_html_escapes_remote_content(self):
        run = {'local_time': 'now', 'status': 'completed', 'discovered': 1, 'evidence_count': 1,
               'analyzed': 0, 'cache_hits': 0, 'daily_budget': 20, 'errors': []}
        card = {'repo': {'id': 1, 'full_name': '<script>alert(1)</script>', 'html_url': 'https://github.com/a/b',
                         'description': '<img src=x onerror=alert(1)>'}, 'category': 'test', 'change': 'new'}
        _, page = render(run, [card], {'charged_micro': 0, 'unknown_count': 0})
        self.assertNotIn('<script>', page)
        self.assertNotIn('<img ', page)
        self.assertIn('Content-Security-Policy', page)


class ScheduleAndPublishTests(unittest.TestCase):
    def test_failed_publish_reuses_analysis_without_paying_again(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root/'state.db')
            cfg = radar.config()
            cfg.update(queries=[{'category': '科研', 'q': 'research', 'sort': 'stars'}], searches_per_scan=1)
            repo = {'id': 100, 'full_name': 'a/research', 'html_url': 'https://github.com/a/research',
                    'description': 'scientific research', 'stargazers_count': 100, 'topics': []}
            pack = {'repo_id': 100, 'full_name': 'a/research', 'pieces': [
                {'id': 'readme', 'url': repo['html_url'], 'lines': ['scientific research toolkit']}], 'warnings': []}
            analysis = {'summary': '科研工具', 'fit': '科研', 'difference': '未对照', 'adoption': '未实测',
                        'kind': '新兴潜力', 'scores': {k: 4 for k in radar.WEIGHTS}, 'unknowns': ['未实测'],
                        'claims': [{'text': '科研工具', 'source': 'readme', 'line': 1, 'quote': 'scientific research'}],
                        '_sources': {'readme': pack['pieces'][0]}}
            class API:
                def __init__(self, *args, **kwargs):
                    self.requests, self.auth_source, self.rate = 0, 'mock', {}
                def get(self, *args, **kwargs):
                    return {'items': [dict(repo)], 'total_count': 1}, 'https://api.github.com/search/repositories'
                def evidence(self, *args, **kwargs):
                    return copy.deepcopy(pack)
            args = Namespace(scheduled=False, limit=1, no_model=False, no_publish=False)
            with patch('radar.APP', root), patch('radar.GitHub', API), \
                 patch('radar.prepare', side_effect=PublishError('offline')), \
                 patch('radar.analyze', return_value={100: copy.deepcopy(analysis)}) as model:
                first = radar.scan(store, cfg, args)
                second = radar.scan(store, cfg, args)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(first['analyzed'], 1)
            self.assertEqual(second['cache_hits'], 1)
            self.assertFalse(second['published'])
            store.close()

    def test_schedule_boot_plus_30_then_two_hour_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            s = Store(Path(folder)/'test.db')
            boot = datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)
            cfg = {'boot_delay_minutes': 30, 'scan_interval_minutes': 120}
            with patch('radar.now', return_value=boot+timedelta(minutes=29)):
                self.assertIsNone(radar.scheduled_slot(s, cfg, boot.isoformat()))
            with patch('radar.now', return_value=boot+timedelta(minutes=30)):
                self.assertIsNotNone(radar.scheduled_slot(s, cfg, boot.isoformat()))
                self.assertIsNone(radar.scheduled_slot(s, cfg, boot.isoformat()))
            with patch('radar.now', return_value=boot+timedelta(minutes=149)):
                self.assertIsNone(radar.scheduled_slot(s, cfg, boot.isoformat()))
            with patch('radar.now', return_value=boot+timedelta(minutes=150)):
                self.assertIsNotNone(radar.scheduled_slot(s, cfg, boot.isoformat()))
            s.close()

    def test_scheduled_commit_rejects_staged_code(self):
        with patch('publisher.verify_remote'), patch('publisher.git', return_value='scripts/radar.py') as git:
            with self.assertRaises(PublishError):
                publish_reports(Path('.'), 'whoaha77/github-radar-reports', 'x')
            self.assertEqual(git.call_count, 1)

    def test_real_git_report_commit_leaves_unstaged_code_untouched(self):
        import publisher
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root, remote = base/'checkout', base/'remote.git'
            root.mkdir()
            def command(*args):
                return subprocess.run(['git', *args], check=True, capture_output=True)
            command('init', '--bare', str(remote))
            command('-C', str(root), 'init', '-b', 'main')
            command('-C', str(root), 'config', 'user.name', 'Test')
            command('-C', str(root), 'config', 'user.email', 'test@example.invalid')
            command('-C', str(root), 'remote', 'add', 'origin', str(remote))
            (root/'scripts').mkdir()
            (root/'reports').mkdir()
            (root/'scripts/code.py').write_text('original')
            (root/'reports/report.md').write_text('first')
            command('-C', str(root), 'add', '.')
            command('-C', str(root), 'commit', '-m', 'initial')
            command('-C', str(root), 'push', '-u', 'origin', 'main')
            (root/'scripts/code.py').write_text('unfinished code iteration')
            (root/'reports/report.md').write_text('new report')
            with patch('publisher.verify_remote'):
                publisher.publish_reports(root, 'whoaha77/github-radar-reports', 'test')
            paths = command('-C', str(root), 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD').stdout.decode().splitlines()
            self.assertEqual(paths, ['reports/report.md'])
            self.assertIn('scripts/code.py', command('-C', str(root), 'status', '--porcelain').stdout.decode())


if __name__ == '__main__':
    unittest.main()

"""Local synthetic regression checks: no sessions, configs, network or model calls."""
import contextlib
import io
import json
import runpy
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
COST = runpy.run_path(str(ROOT / 'bin' / 'cost_report.py'), run_name='reporting_test')
REVIEW = runpy.run_path(str(ROOT / 'bin' / 'review_text.py'), run_name='review_test')


class ReportingTests(unittest.TestCase):
    def test_legacy_failed_zero_is_ambiguous(self):
        legacy=dict(type='worker',status='timeout',input_tokens=0,output_tokens=0,cached_input_tokens=0,cost_estimated_cny=None)
        groups,_=COST['summarize']([legacy])
        self.assertIsNone(groups['worker']['in'])
        legacy['status']='unknown'
        groups,_=COST['summarize']([legacy])
        self.assertIsNone(groups['worker']['in'])
        legacy['usage_status']='reported'
        groups,_=COST['summarize']([legacy])
        self.assertEqual(groups['worker']['in'],0)
    def test_all_missing_stays_unknown(self):
        groups, total = COST['summarize']([{'type': 'worker', 'status': 'timeout'}])
        row = groups['worker']
        self.assertIsNone(total)
        for key in ('cost', 'in', 'out', 'cache'):
            self.assertIsNone(row[key])
            self.assertEqual(row['unknown_' + key], 1)
        self.assertEqual(COST['format_known'](row['in']), '未知')

    def test_mixed_coverage_preserves_known_zero(self):
        groups, total = COST['summarize']([
            {'type': 'ask', 'cost_estimated_cny': 0, 'input_tokens': 0,
             'output_tokens': 2, 'cached_input_tokens': 0}, {'type': 'ask'}])
        row = groups['ask']
        self.assertEqual(total, 0)
        self.assertEqual(row['in'], 0)
        self.assertEqual(row['out'], 2)
        self.assertEqual(COST['format_known'](row['in'], row['unknown_in']), '0（1条未知）')

    def test_cache_not_added_to_input(self):
        groups, total = COST['summarize']([{'type': 'ask', 'cost_estimated_cny': 0.1,
                                          'input_tokens': 100, 'cached_input_tokens': 80,
                                          'output_tokens': 10}])
        self.assertEqual(groups['ask']['in'], 100)
        self.assertEqual(groups['ask']['cache'], 80)
        self.assertEqual(total, 0.1)
        self.assertEqual(COST['summarize']([])[1], 0)

    def test_days_filter_handles_aware_and_naive(self):
        now = datetime.now(timezone.utc)
        records = [{'ts': (now - timedelta(days=30)).isoformat()},
                   {'ts': now.isoformat()},
                   {'ts': (datetime.now() - timedelta(days=30)).isoformat()},
                   {'ts': datetime.now().isoformat()}]
        with patch.object(Path, 'exists', return_value=True), patch.object(
                Path, 'read_text', return_value='\n'.join(json.dumps(row) for row in records)):
            self.assertEqual(len(COST['load'](7)), 2)

    def test_report_distinguishes_missing_usage_and_cost(self):
        rows = [{'type': 'worker', 'status': 'timeout'}]
        with patch.object(sys, 'argv', ['cost_report.py']), patch.object(
                Path, 'exists', return_value=True), patch.object(
                Path, 'read_text', return_value=json.dumps(rows[0])), contextlib.redirect_stdout(io.StringIO()) as out:
            COST['main']()
        self.assertIn('| worker | 1 | 未知 | 0.0 | 未知 | 未知 | 未知 |', out.getvalue())
        self.assertIn('1 条调用费用未知', out.getvalue())


class DiffEvidenceTests(unittest.TestCase):
    def preflight(self, metadata, content=b'diff --git a/app.py b/app.py\n+print(1)\n'):
        with tempfile.TemporaryDirectory() as folder:
            argv = ['review_text.py', '--workspace', folder, '--what', 'fake regression',
                    '--git-diff', '--dry-run']
            with patch.object(sys, 'argv', argv), patch.object(
                    subprocess, 'run', side_effect=[subprocess.CompletedProcess([], 0, stdout=metadata),
                                                    subprocess.CompletedProcess([], 0, stdout=content)]) as run:
                with contextlib.redirect_stdout(io.StringIO()):
                    try:
                        REVIEW['main']()
                    except ValueError as error:
                        return str(error), run.call_count
                return None, run.call_count

    def test_sensitive_modified_rejected_before_content_read(self):
        for name in (b'.env', b'auth.json', b'config.toml', b'config/.env.production', b'private.key'):
            with self.subTest(name=name):
                error, calls = self.preflight(b'M\0' + name + b'\0')
                self.assertIn('sensitive path', error)
                self.assertEqual(calls, 1)

    def test_sensitive_rename_old_and_new_rejected(self):
        for metadata in (b'R100\0.env\0readme.md\0', b'R100\0app.json\0auth.json\0'):
            error, calls = self.preflight(metadata)
            self.assertIn('sensitive path', error)
            self.assertEqual(calls, 1)

    def test_safe_paths_and_rename_pass(self):
        error, calls = self.preflight(b'R100\0old name.py\0new\nname.py\0M\0app.tsx\0')
        self.assertIsNone(error)
        self.assertEqual(calls, 2)

    def test_malformed_metadata_rejected_before_content(self):
        error, calls = self.preflight(b'R100\0only-one-path.py\0')
        self.assertIn('malformed', error)
        self.assertEqual(calls, 1)

    def test_explicit_sensitive_file_uses_same_rule(self):
        self.assertTrue(REVIEW['sensitive_review_path']('config/.env.local'))
        self.assertFalse(REVIEW['sensitive_review_path']('src/config.ts'))


if __name__ == '__main__':
    unittest.main()

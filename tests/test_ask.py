"""ask.py 的纯 mock 单元测试：不联网、不读密钥配置、不调用真实 API。

运行：
  python -m unittest discover -s work/tests -t work/tests -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ASK_PATH = Path(os.environ.get('ASK_PATH', str(ROOT / 'bin' / 'ask.py')))
_spec = importlib.util.spec_from_file_location('ask_under_test', ASK_PATH)
ask = importlib.util.module_from_spec(_spec)
sys.modules['ask_under_test'] = ask
_spec.loader.exec_module(ask)

USAGE = {'prompt_tokens': 100, 'prompt_cache_hit_tokens': 40, 'completion_tokens': 20}


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def payload(content, finish='stop', usage=USAGE):
    return {'choices': [{'finish_reason': finish, 'message': {'content': content}}], 'usage': usage}


def batch_payload(items, finish='stop', usage=USAGE):
    return payload(json.dumps({'results': items}), finish, usage)


def http_error(code, body=b'{"error":"x"}'):
    return urllib.error.HTTPError('https://api.deepseek.com/chat/completions', code, 'err', {}, io.BytesIO(body))


class Base(unittest.TestCase):
    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmp = Path(tmpdir.name)
        self.log = self.tmp / 'delegations.jsonl'
        self._patch(mock.patch.object(ask, 'load_key', return_value='test-key'))
        self._patch(mock.patch.object(ask, 'LOG', self.log))

    def _patch(self, patcher):
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, 'argv', ['ask.py'] + [str(x) for x in argv]), \
                mock.patch.object(sys, 'stdout', out), \
                mock.patch.object(sys, 'stderr', err):
            try:
                ask.main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
                if isinstance(e.code, str):
                    err.write(e.code + chr(10))
        return code, out.getvalue(), err.getvalue()

    def urlopen(self, fn):
        return mock.patch.object(ask.urllib.request, 'urlopen', side_effect=fn)

    def api(self, response):
        return mock.patch.object(ask.urllib.request, 'urlopen', return_value=FakeResponse(response))

    def api_must_not_be_called(self):
        def fake(req, timeout=None):
            raise AssertionError('不该发出 API 请求')
        return self.urlopen(fake)

    def records(self):
        if not self.log.exists():
            return []
        return [json.loads(x) for x in self.log.read_text(encoding='utf-8').splitlines() if x.strip()]

    def mk(self, rel, text):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
        return p


class ResponseValidationTests(Base):
    def test_truncated_length_refuses_write_and_logs_failure(self):
        dst = self.tmp / 'out.md'
        with self.api(payload('半截内容', finish='length')):
            code, out, err = self.run_cli(['--out', str(dst), '润色'])
        self.assertNotEqual(code, 0)
        self.assertFalse(dst.exists())
        self.assertNotIn('半截内容', out)
        self.assertIn('finish_reason', err)
        recs = self.records()
        self.assertEqual([r['status'] for r in recs], ['failed'])
        self.assertEqual(recs[0]['exit_code'], 1)
        self.assertEqual(recs[0]['input_tokens'], 100)
        self.assertGreater(recs[0]['cost_estimated_cny'], 0)
        self.assertEqual(recs[0]['files_changed'], [])

    def test_non_string_content_refuses(self):
        dst = self.tmp / 'out.md'
        with self.api(payload(None)):
            code, out, err = self.run_cli(['--out', str(dst), 'x'])
        self.assertNotEqual(code, 0)
        self.assertFalse(dst.exists())
        self.assertIn('content', err)
        self.assertEqual([r['status'] for r in self.records()], ['failed'])

    def test_empty_content_refuses(self):
        dst = self.tmp / 'out.md'
        with self.api(payload('   ')):
            code, out, err = self.run_cli(['--out', str(dst), 'x'])
        self.assertNotEqual(code, 0)
        self.assertFalse(dst.exists())

    def test_stdout_mode_prints_nothing_when_truncated(self):
        with self.api(payload('半截', finish='length')):
            code, out, err = self.run_cli(['x'])
        self.assertNotEqual(code, 0)
        self.assertEqual(out, '')

    def test_success_writes_file_and_logs_done(self):
        dst = self.tmp / 'out.md'
        with self.api(payload('结果')):
            code, out, err = self.run_cli(['--out', str(dst), 'x'])
        self.assertEqual(code, 0)
        self.assertEqual(dst.read_text(encoding='utf-8'), '结果')
        self.assertEqual([r['status'] for r in self.records()], ['done'])

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        out_dir = self.tmp / 'out'
        src = self.mk('in/a.md', 'A')
        with self.api(batch_payload([{'name': 'a.md', 'content': 'A!'}])):
            code, out, err = self.run_cli(['--batch', str(src), '--out-dir', str(out_dir), 'cmd'])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ['a.md'])


class BatchResultValidationTests(Base):
    def _run_batch(self, items, source_names=('a.md', 'b.md'), finish='stop', extra_args=()):
        src_dir = self.tmp / 'in'
        for n in source_names:
            self.mk(f'in/{n}', 'text of ' + n)
        out_dir = self.tmp / 'out'
        argv = ['--batch'] + [str(src_dir / n) for n in source_names] + \
               ['--out-dir', str(out_dir)] + list(extra_args) + ['cmd']
        with self.api(batch_payload(items, finish=finish)):
            code, out, err = self.run_cli(argv)
        return code, out, err, out_dir

    def test_missing_item_rejects_whole_batch(self):
        code, out, err, out_dir = self._run_batch([{'name': 'a.md', 'content': 'A!'}])
        self.assertNotEqual(code, 0)
        self.assertIn('缺项', err)
        self.assertFalse(out_dir.exists())

    def test_extra_item_rejected(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': 'a.md', 'content': 'A!'}, {'name': 'b.md', 'content': 'B!'}, {'name': 'c.md', 'content': 'C!'}])
        self.assertNotEqual(code, 0)
        self.assertIn('越界', err)
        self.assertFalse(out_dir.exists())

    def test_duplicate_name_rejected(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': 'a.md', 'content': '1'}, {'name': 'a.md', 'content': '2'}])
        self.assertNotEqual(code, 0)
        self.assertIn('重复', err)
        self.assertFalse(out_dir.exists())

    def test_parent_escape_name_rejected(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': '../a.md', 'content': 'x'}, {'name': 'b.md', 'content': 'y'}])
        self.assertNotEqual(code, 0)
        self.assertIn('非法', err)
        self.assertFalse(out_dir.exists())

    def test_absolute_path_name_rejected(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': str(self.tmp / 'evil.md'), 'content': 'x'}, {'name': 'b.md', 'content': 'y'}])
        self.assertNotEqual(code, 0)
        self.assertIn('非法', err)
        self.assertFalse(out_dir.exists())

    def test_new_name_rejected(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': 'zzz.md', 'content': 'x'}, {'name': 'b.md', 'content': 'y'}])
        self.assertNotEqual(code, 0)
        self.assertIn('越界', err)
        self.assertFalse(out_dir.exists())

    def test_non_string_content_rejected(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': 'a.md', 'content': 1}, {'name': 'b.md', 'content': 'y'}])
        self.assertNotEqual(code, 0)
        self.assertIn('content', err)
        self.assertFalse(out_dir.exists())

    def test_truncated_batch_writes_nothing(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': 'a.md', 'content': 'A!'}, {'name': 'b.md', 'content': 'B!'}], finish='length')
        self.assertNotEqual(code, 0)
        self.assertFalse(out_dir.exists())
        self.assertEqual([r['status'] for r in self.records()], ['failed'])

    def test_happy_path_all_names_once(self):
        code, out, err, out_dir = self._run_batch(
            [{'name': 'a.md', 'content': 'A!'}, {'name': 'b.md', 'content': 'B!'}])
        self.assertEqual(code, 0)
        self.assertEqual((out_dir / 'a.md').read_text(encoding='utf-8'), 'A!')
        self.assertEqual((out_dir / 'b.md').read_text(encoding='utf-8'), 'B!')


class BatchInputTests(Base):
    def test_same_basename_fails_before_request(self):
        a = self.mk('in1/x.md', '1')
        b = self.mk('in2/x.md', '2')
        with self.api_must_not_be_called():
            code, out, err = self.run_cli(['--batch', str(a), str(b), '--out-dir', str(self.tmp / 'out'), 'cmd'])
        self.assertNotEqual(code, 0)
        self.assertIn('同名', err)

    def test_same_file_twice_is_deduped(self):
        src = self.mk('in/a.md', 'A')
        captured = {}

        def fake(req, timeout=None):
            captured['body'] = json.loads(req.data.decode('utf-8'))
            return FakeResponse(batch_payload([{'name': 'a.md', 'content': 'A!'}]))

        out_dir = self.tmp / 'out'
        with self.urlopen(fake):
            code, out, err = self.run_cli(['--batch', str(src), str(src), '--out-dir', str(out_dir), 'cmd'])
        self.assertEqual(code, 0)
        prompt = captured['body']['messages'][1]['content']
        self.assertEqual(prompt.count('<<<FILE: a.md>>>'), 1)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ['a.md'])

    def test_absolute_glob_supported(self):
        self.mk('in/a.md', 'A')
        self.mk('in/b.md', 'B')
        pattern = str(self.tmp / 'in' / '*.md')
        out_dir = self.tmp / 'out'
        with self.api(batch_payload([{'name': 'a.md', 'content': 'A!'}, {'name': 'b.md', 'content': 'B!'}])):
            code, out, err = self.run_cli(['--batch', pattern, '--out-dir', str(out_dir), 'cmd'])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(p.name for p in out_dir.iterdir()), ['a.md', 'b.md'])


class OutputSafetyTests(Base):
    def test_out_equals_in_is_refused_before_request(self):
        f = self.mk('draft.md', 'orig')
        with self.api_must_not_be_called():
            code, out, err = self.run_cli(['--in', str(f), '--out', str(f), 'polish'])
        self.assertNotEqual(code, 0)
        self.assertIn('覆盖输入', err)
        self.assertEqual(f.read_text(encoding='utf-8'), 'orig')

    def test_out_dir_equal_input_dir_is_refused(self):
        src = self.mk('in/a.md', 'A')
        with self.api_must_not_be_called():
            code, out, err = self.run_cli(['--batch', str(src), '--out-dir', str(self.tmp / 'in'), 'cmd'])
        self.assertNotEqual(code, 0)
        self.assertIn('覆盖输入', err)
        self.assertEqual(src.read_text(encoding='utf-8'), 'A')

    def test_forbidden_zone_single_refused_before_request(self):
        for target in ('D:/protected-data/out.md', 'd:/protected-data/sub/out.md', 'D:\\research-raw\\deep\\out.md'):
            with self.api_must_not_be_called():
                code, out, err = self.run_cli(['--out', target, 'x'])
            self.assertNotEqual(code, 0, target)
            self.assertIn('禁区', err)

    def test_forbidden_zone_batch_dir_refused_before_request(self):
        src = self.mk('in/a.md', 'A')
        with self.api_must_not_be_called():
            code, out, err = self.run_cli(['--batch', str(src), '--out-dir', 'D:/research-raw/out', 'cmd'])
        self.assertNotEqual(code, 0)
        self.assertIn('禁区', err)

    def test_existing_output_refused_then_overwrite_backs_up(self):
        src = self.mk('in/a.md', 'A')
        out_dir = self.tmp / 'out'
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / 'a.md'
        dst.write_text('OLD', encoding='utf-8')
        response = batch_payload([{'name': 'a.md', 'content': 'NEW'}])

        with self.api(response):
            code, out, err = self.run_cli(['--batch', str(src), '--out-dir', str(out_dir), 'cmd'])
        self.assertNotEqual(code, 0)
        self.assertIn('--overwrite', err)
        self.assertEqual(dst.read_text(encoding='utf-8'), 'OLD')

        with self.api(response):
            code, out, err = self.run_cli(['--batch', str(src), '--out-dir', str(out_dir), 'cmd', '--overwrite'])
        self.assertEqual(code, 0)
        self.assertEqual(dst.read_text(encoding='utf-8'), 'NEW')
        baks = list(out_dir.glob('a.md.*.bak'))
        self.assertEqual(len(baks), 1)
        self.assertEqual(baks[0].read_text(encoding='utf-8'), 'OLD')

    def test_existing_single_out_refused_then_overwrite(self):
        dst = self.tmp / 'out.md'
        dst.write_text('OLD', encoding='utf-8')
        with self.api(payload('NEW')):
            code, out, err = self.run_cli(['--out', str(dst), 'x'])
        self.assertNotEqual(code, 0)
        self.assertEqual(dst.read_text(encoding='utf-8'), 'OLD')
        with self.api(payload('NEW')):
            code, out, err = self.run_cli(['--out', str(dst), 'x', '--overwrite'])
        self.assertEqual(code, 0)
        self.assertEqual(dst.read_text(encoding='utf-8'), 'NEW')
        self.assertEqual(len(list(self.tmp.glob('out.md.*.bak'))), 1)


class TimeoutRetryTests(Base):
    def test_http_400_is_not_retried(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            raise http_error(400)

        with self.urlopen(fake):
            code, out, err = self.run_cli(['--retries', '2', 'x'])
        self.assertNotEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('HTTP 400', err)

    def test_http_401_and_403_are_not_retried(self):
        for status in (401, 403):
            calls = []

            def fake(req, timeout=None, _s=status):
                calls.append(timeout)
                raise http_error(_s)

            with self.urlopen(fake):
                code, out, err = self.run_cli(['--retries', '2', 'x'])
            self.assertNotEqual(code, 0)
            self.assertEqual(len(calls), 1, f'HTTP {status} 不该重试')

    def test_http_429_then_success(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise http_error(429)
            return FakeResponse(payload('ok'))

        with mock.patch.object(ask.time, 'sleep', lambda s: None), self.urlopen(fake):
            code, out, err = self.run_cli(['x'])
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn('ok', out)

    def test_5xx_exhausted_keeps_failed_ledger_with_usage(self):
        body = json.dumps({'error': 'boom', 'usage': USAGE}).encode('utf-8')

        def fake(req, timeout=None):
            raise http_error(500, body)

        with mock.patch.object(ask.time, 'sleep', lambda s: None), self.urlopen(fake):
            code, out, err = self.run_cli(['--retries', '1', 'x'])
        self.assertNotEqual(code, 0)
        recs = self.records()
        self.assertEqual([r['status'] for r in recs], ['failed'])
        self.assertEqual(recs[0]['exit_code'], 1)
        self.assertEqual(recs[0]['input_tokens'], 100)
        self.assertGreater(recs[0]['cost_estimated_cny'], 0)
        self.assertNotIn('done', [r['status'] for r in recs])

    def test_retries_zero_means_single_attempt(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            raise urllib.error.URLError('down')

        with self.urlopen(fake):
            code, out, err = self.run_cli(['--retries', '0', 'x'])
        self.assertNotEqual(code, 0)
        self.assertEqual(len(calls), 1)

    def test_deadline_is_shared_across_retries(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            time.sleep(0.25)
            raise urllib.error.URLError('down')

        started = time.monotonic()
        with self.urlopen(fake):
            code, out, err = self.run_cli(['--timeout', '0.6', '--retries', '2', 'x'])
        elapsed = time.monotonic() - started
        self.assertNotEqual(code, 0)
        self.assertEqual(len(calls), 1, '预算耗尽后不得再发起新请求')
        self.assertLessEqual(calls[0], 0.6)
        self.assertLess(elapsed, 1.6)
        self.assertGreater(elapsed, 0.5)
        self.assertIn('超时', err)

    def test_timeout_budget_passed_to_urlopen(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            return FakeResponse(payload('ok'))

        with self.urlopen(fake):
            code, out, err = self.run_cli(['--timeout', '7', 'x'])
        self.assertEqual(code, 0)
        self.assertLessEqual(calls[0], 7.0)
        self.assertGreater(calls[0], 6.0)

    def test_retries_out_of_range_rejected(self):
        for bad in ('-1', '3'):
            code, out, err = self.run_cli(['--retries', bad, 'x'])
            self.assertNotEqual(code, 0, bad)

    def test_non_positive_timeout_rejected(self):
        code, out, err = self.run_cli(['--timeout', '0', 'x'])
        self.assertNotEqual(code, 0)


class CliCompatTests(Base):
    def test_existing_flags_still_parse(self):
        with self.api(payload('hello')):
            code, out, err = self.run_cli(['--thinking', 'off', '--max-tokens', '128', '--json', 'hi'])
        self.assertEqual(code, 0)
        self.assertIn('hello', out)

    def test_files_and_stdin_modes(self):
        f = self.mk('a.md', 'A')
        with self.api(payload('hello')):
            code, out, err = self.run_cli(['summarize', '--files', str(f)])
        self.assertEqual(code, 0)
        self.assertIn('hello', out)
        with mock.patch.object(sys, 'stdin', io.StringIO('stdin text')):
            with self.api(payload('hello')):
                code, out, err = self.run_cli(['--stdin', 'translate'])
        self.assertEqual(code, 0)

    def test_is_peak_uses_beijing_time(self):
        self.assertEqual(ask.BEIJING, timezone(timedelta(hours=8)))
        self.assertTrue(ask.is_peak(datetime(2026, 10, 15, 10, 0)))
        self.assertTrue(ask.is_peak(datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)))
        self.assertFalse(ask.is_peak(datetime(2026, 10, 15, 12, 30)))
        self.assertFalse(ask.is_peak(datetime(2026, 10, 17, 10, 0)))
        self.assertEqual(ask.FORBIDDEN_ROOTS, ('D:/protected-data', 'D:/research-raw'))


if __name__ == '__main__':
    unittest.main(verbosity=2)

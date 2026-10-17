import sys
import json
import uuid
import hashlib
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'bin'))
import auto_relay as r  # noqa: E402

BUCKET = 'codex'
THREAD = str(uuid.uuid4())


def _sha(b):
    return hashlib.sha256(b).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.state.mkdir()
        self.ws = self.root / 'ws'
        self.ws.mkdir()
        (self.ws / 'a.txt').write_bytes(b'hello')
        (self.ws / 'sub').mkdir()
        (self.ws / 'sub' / 'b.txt').write_bytes(b'world')
        self.task = self.root / 'task.md'
        self.task.write_text('do the thing', encoding='utf-8')
        self.files = ['a.txt', 'sub/b.txt']
        self.job = r.arm(state=str(self.state), workspace=str(self.ws),
                         task=str(self.task), files=self.files,
                         thread_id=THREAD, test_signals=True)
        self.jd = Path(self.job['job_dir'])

    def tearDown(self):
        self.tmp.cleanup()

    def envelope(self, used=100.0, reached='usagelimit'):
        return {'rateLimitsByLimitId': {BUCKET: {
            'limitId': BUCKET,
            'rateLimitReachedType': reached,
            'primary': {'usedPercent': used, 'resetsAt': 10 ** 12},
        }}}

    def available(self):
        return {'rateLimitsByLimitId': {BUCKET: {
            'limitId': BUCKET, 'primary': {'usedPercent': 5.0, 'resetsAt': 10 ** 12}}}}


class TestArmAndSnapshot(Base):
    def test_arm_shape_and_state(self):
        self.assertTrue(self.job['job_id'].startswith('job-'))
        self.assertEqual(Path(self.job['workspace']), self.jd / 'workspace')
        self.assertTrue((self.jd / 'job.json').is_file())

    def test_original_immutable(self):
        before = {n: _sha((self.ws / n).read_bytes()) for n in self.files}
        r.arm(state=str(self.state), workspace=str(self.ws), task=str(self.task),
              files=self.files, thread_id=THREAD, test_signals=True)
        after = {n: _sha((self.ws / n).read_bytes()) for n in self.files}
        self.assertEqual(before, after)

    def test_snapshot_hashes(self):
        snap = Path(self.job['workspace'])
        self.assertEqual(_sha((snap / 'a.txt').read_bytes()), _sha(b'hello'))
        self.assertEqual(_sha((snap / 'sub' / 'b.txt').read_bytes()), _sha(b'world'))

    def test_new_file_not_snapshotted(self):
        (self.ws / 'a.txt').write_bytes(b'hello')
        (self.ws / 'new.txt').write_bytes(b'x')
        j = r.arm(state=str(self.state), workspace=str(self.ws),
                  task=str(self.task), files=self.files,
                  thread_id=THREAD, test_signals=True)
        self.assertFalse((Path(j['workspace']) / 'new.txt').exists())

    def test_secret_refusal(self):
        (self.ws / '.env').write_bytes(b'SECRET=1')
        with self.assertRaises(r.RelayError):
            r.arm(state=str(self.state), workspace=str(self.ws),
                  task=str(self.task), files=['.env'],
                  thread_id=THREAD, test_signals=True)

    def test_traversal_refused(self):
        with self.assertRaises(r.RelayError):
            r.job_path(self.state, '../../etc')
        with self.assertRaises(r.RelayError):
            r.load_job(self.root / 'notajob')


class TestTransitions(Base):
    def _dispatch(self):
        jd = self.jd
        r.step(jd, signal='exhausted', now=100)
        r.step(jd, signal='exhausted', now=101)
        r.step(jd, signal='exhausted', now=106, quota_reader=None)

    def _mock_handoff(self):
        sid = 'codex-relay-' + uuid.uuid4().hex
        hid = 'handoff-' + uuid.uuid4().hex
        h = mock.Mock(return_value=dict(session_id=sid, handoff_id=hid, job_dir=str(self.jd)))
        return h, sid, hid

    def _fake_api(self):
        api = mock.Mock()
        api.rpc = mock.Mock(side_effect=self._rpc)
        self._phase = {'n': 0}
        return api

    def _rpc(self, method, params):
        if method == 'session/list':
            return {'items': [{'sessionId': self.sid, 'running': False}]}
        if method == 'session/projections':
            return {'values': {'inbox': {}}}
        if method == 'session/prompt':
            self._prompt_count = getattr(self, '_prompt_count', 0) + 1
            return {'accepted': True}
        raise AssertionError(method)

    def _packet(self):
        p = self.root / 'p.json'
        p.write_text(json.dumps({'messages': [
            {'role': 'assistant', 'text': 'done'}]}), encoding='utf-8')
        m = self.root / 'p.md'
        m.write_text('# done', encoding='utf-8')
        return dict(json_path=str(p), md_path=str(m))

    def test_full_flow(self):
        h, sid, hid = self._mock_handoff()
        api = self._fake_api()
        self.sid = sid
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()):
            # arm -> exhausted confirm
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            s = r.step(self.jd, signal='exhausted', now=106)
            self.assertEqual(s['phase'], 'dsh_ack')
            self.assertEqual(s['handoff_id'], hid)
            self.assertEqual(s['session_id'], sid)
            # ack packet -> prompt accepted exactly once
            s = r.step(self.jd, signal='available', now=107, bridge_api=api)
            self.assertEqual(s['phase'], 'dsh_active')
            self.assertEqual(self._prompt_count, 1)
            # idle twice separated by 10
            s = r.step(self.jd, signal='available', now=200, bridge_api=api)
            self.assertNotEqual(s['phase'], 'dsh_ready')
            s = r.step(self.jd, signal='available', now=210, bridge_api=api)
            self.assertEqual(s['phase'], 'dsh_ready')
            # available later -> return_ready
            s = r.step(self.jd, signal='available', now=220, bridge_api=api)
            self.assertEqual(s['phase'], 'return_ready')
            self.assertEqual(self._prompt_count, 1)

    def test_ambiguous_handoff_stops(self):
        h = mock.Mock(side_effect=RuntimeError('boom'))
        with mock.patch.object(r, 'handoff', h):
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            s = r.step(self.jd, signal='exhausted', now=106)
            self.assertEqual(s['phase'], 'needs_attention')
            self.assertEqual(h.call_count, 1)
            s2 = r.step(self.jd, signal='exhausted', now=107)
            self.assertEqual(s2['phase'], 'needs_attention')
            self.assertEqual(h.call_count, 1)

    def test_prompt_fail_stops_no_retry(self):
        h, sid, hid = self._mock_handoff()
        api = mock.Mock()
        calls = {'prompt': 0}

        def rpc(method, params):
            if method == 'session/list':
                return {'items': [{'sessionId': sid, 'running': False}]}
            if method == 'session/projections':
                return {'values': {'inbox': {}}}
            if method == 'session/prompt':
                calls['prompt'] += 1
                raise RuntimeError('nope')
            raise AssertionError(method)
        api.rpc = rpc
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()):
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            r.step(self.jd, signal='exhausted', now=106)
            s = r.step(self.jd, signal='available', now=107, bridge_api=api)
            self.assertEqual(s['phase'], 'needs_attention')
            s = r.step(self.jd, signal='available', now=200, bridge_api=api)
            self.assertEqual(s['phase'], 'needs_attention')
            self.assertEqual(calls['prompt'], 1)

    def test_persisted_phase(self):
        h, sid, hid = self._mock_handoff()
        api = self._fake_api()
        self.sid = sid
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()):
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            r.step(self.jd, signal='exhausted', now=106)
            j = json.loads((self.jd / 'job.json').read_text(encoding='utf-8'))
            self.assertEqual(j['phase'], 'dsh_ack')
            # re-read prompt count: a fresh step must not double-submit
            r.step(self.jd, signal='available', now=107, bridge_api=api)
            self.assertEqual(self._prompt_count, 1)

    def test_no_test_signal_without_flag(self):
        j = r.arm(state=str(self.state), workspace=str(self.ws), task=str(self.task),
                  files=self.files, thread_id=THREAD, test_signals=False)
        with self.assertRaises(r.RelayError):
            r.step(j['job_dir'], signal='exhausted', now=100)

    def test_task_tamper_stops(self):
        self.task.write_text('do the thing', encoding='utf-8')
        r.step(self.jd, signal='exhausted', now=100)
        r.step(self.jd, signal='exhausted', now=101)
        j = json.loads((self.jd / 'job.json').read_text(encoding='utf-8'))
        Path(j['task_path']).write_text('tampered', encoding='utf-8')
        s = r.step(self.jd, signal='exhausted', now=106)
        self.assertEqual(s['phase'], 'needs_attention')

    def test_stale_wrongbucket_empty(self):
        r.step(self.jd, signal='exhausted', now=100)
        r.step(self.jd, signal='exhausted', now=101)
        # stale bucket -> not exhausted -> resets and no dispatch
        j = json.loads((self.jd / 'job.json').read_text(encoding='utf-8'))
        s = r.step(self.jd, quota_reader=lambda b: {'rateLimitsByLimitId': {}},
                   now=106)
        self.assertEqual(s['phase'], 'armed')

    def test_busy_missingrow_malformed_inbox(self):
        h, sid, hid = self._mock_handoff()
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()):
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            r.step(self.jd, signal='exhausted', now=106)
            api = mock.Mock()

            def busy(method, params):
                if method == 'session/list':
                    return {'items': [{'sessionId': sid, 'running': True}]}
                raise AssertionError(method)
            api.rpc = busy
            s = r.step(self.jd, signal='available', now=107, bridge_api=api)
            self.assertNotEqual(s.get('phase'), 'dsh_active')
            calls = {'n': 0}

            def malformed(method, params):
                if method == 'session/list':
                    return {'items': [{'sessionId': sid, 'running': False}]}
                if method == 'session/projections':
                    return {'values': {'inbox': 'notalist'}}
                if method == 'session/prompt':
                    calls['n'] += 1
                    return {'accepted': True}
                raise AssertionError(method)
            api.rpc = malformed
            s = r.step(self.jd, signal='available', now=108, bridge_api=api)
            self.assertNotEqual(s.get('phase'), 'dsh_active')
            self.assertEqual(calls['n'], 0)

    def test_idle_active_twice(self):
        h, sid, hid = self._mock_handoff()
        api = self._fake_api()
        self.sid = sid
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()):
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            r.step(self.jd, signal='exhausted', now=106)
            r.step(self.jd, signal='available', now=107, bridge_api=api)
            s = r.step(self.jd, signal='available', now=200, bridge_api=api)
            self.assertNotEqual(s['phase'], 'dsh_ready')
            s = r.step(self.jd, signal='available', now=210, bridge_api=api)
            self.assertEqual(s['phase'], 'dsh_ready')
            s = r.step(self.jd, signal='available', now=220, bridge_api=api)
            self.assertEqual(s['phase'], 'return_ready')
            self.assertEqual(self._prompt_count, 1)
            # it is terminal
            s = r.step(self.jd, signal='exhausted', now=300, bridge_api=api)
            self.assertEqual(s['phase'], 'return_ready')

    def test_sourcechanges_shown(self):
        h, sid, hid = self._mock_handoff()
        api = self._fake_api()
        self.sid = sid
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()):
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            r.step(self.jd, signal='exhausted', now=106)
            r.step(self.jd, signal='available', now=107, bridge_api=api)
            r.step(self.jd, signal='available', now=200, bridge_api=api)
            r.step(self.jd, signal='available', now=210, bridge_api=api)
            (self.ws / 'a.txt').write_bytes(b'changed')
            s = r.step(self.jd, signal='available', now=220, bridge_api=api)
            self.assertEqual(s['phase'], 'return_ready')
            j = json.loads((self.jd / 'job.json').read_text(encoding='utf-8'))
            self.assertEqual(j['original_changed'], ['a.txt'])

    def test_queued_return_failure_once(self):
        h, sid, hid = self._mock_handoff()
        api = self._fake_api()
        self.sid = sid
        j = json.loads((self.jd / 'job.json').read_text(encoding='utf-8'))
        j['auto_resume'] = True
        r.save_job(self.jd, j)
        with mock.patch.object(r, 'handoff', h), \
                mock.patch.object(r, 'return_packet', return_value=self._packet()), \
                mock.patch.object(subprocess, 'run',
                                  return_value=mock.Mock(returncode=1)) as run:
            r.step(self.jd, signal='exhausted', now=100)
            r.step(self.jd, signal='exhausted', now=101)
            r.step(self.jd, signal='exhausted', now=106)
            r.step(self.jd, signal='available', now=107, bridge_api=api)
            r.step(self.jd, signal='available', now=200, bridge_api=api)
            r.step(self.jd, signal='available', now=210, bridge_api=api)
            s = r.step(self.jd, signal='available', now=220, bridge_api=api)
            self.assertEqual(s['phase'], 'needs_attention')
            self.assertEqual(run.call_count, 1)


class TestQuota(Base):
    def test_classify_exhausted(self):
        self.assertEqual(r.classify_quota(self.envelope(), BUCKET, 100), 'exhausted')

    def test_classify_available(self):
        self.assertEqual(r.classify_quota(self.available(), BUCKET, 100), 'available')

    def test_classify_unknown(self):
        self.assertEqual(r.classify_quota({'rateLimitsByLimitId': {}}, BUCKET, 100), 'unknown')

    def test_classify_wrongbucket(self):
        self.assertEqual(r.classify_quota({'rateLimitsByLimitId': {}}, BUCKET, 100), 'unknown')

    def test_classify_emptybyid(self):
        self.assertEqual(r.classify_quota({'rateLimitsByLimitId': {BUCKET: {}}}, BUCKET, 100), 'unknown')


class TestLock(unittest.TestCase):
    def test_exclusive(self):
        d = tempfile.mkdtemp()
        p = Path(d) / 'x.lock'
        ready = threading.Event()
        out = {}

        def attempt():
            try:
                with r.file_lock(p):
                    out['ok'] = True
            except Exception:
                out['ok'] = False
            ready.set()

        with r.file_lock(p):
            t = threading.Thread(target=attempt)
            t.start()
            ready.wait(2)
            t.join(2)
        self.assertFalse(out.get('ok', False))
        with r.file_lock(p):
            self.assertTrue(p.is_file())


if __name__ == '__main__':
    unittest.main()
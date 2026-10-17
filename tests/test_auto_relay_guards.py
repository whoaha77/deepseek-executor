import sys,json,time,uuid,tempfile,subprocess,unittest
from pathlib import Path
from unittest import mock
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bin'))
import auto_relay as r


class Guards(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.ws=self.root/'source';self.ws.mkdir()
        (self.ws/'file.txt').write_text('baseline',encoding='utf-8')
        self.task=self.root/'task.md';self.task.write_text('Only edit file.txt and verify.',encoding='utf-8')
        self.state=self.root/'state'

    def tearDown(self):self.temp.cleanup()

    def job(self,**kw):
        d=r.arm(self.state,self.ws,self.task,['file.txt'],str(uuid.uuid4()),**kw)
        return Path(d['job_dir'])

    def quota(self,pct=20):
        return {'rateLimits':{'limitId':'codex','primary':{'usedPercent':pct,'resetsAt':1000}}}

    def test_missing_new_output_keeps_none_baseline(self):
        j=r.arm(self.state,self.ws,self.task,['new.txt'],str(uuid.uuid4()))
        self.assertEqual(r.load_job(j['job_dir'])['baseline'],{'new.txt':None})

    def test_secret_contents_refused_before_job_creation(self):
        (self.ws/'file.txt').write_text('sk-'+'a'*24)
        with self.assertRaises(Exception):self.job()
        self.assertFalse(self.state.exists())

    def test_metadata_retry_limit_and_no_paid_retry(self):
        jd=self.job(test_signals=True);j=r.load_job(jd)
        j.update(phase='dsh_active',session_id='codex-relay-test',handoff_id='handoff-test',idle_since=1)
        r.save_job(jd,j)
        with mock.patch.object(r,'get_idle',return_value=True),mock.patch.object(r,'return_packet',side_effect=RuntimeError),mock.patch.object(r,'handoff') as h:
            self.assertEqual(r.step(jd,now=20,bridge_api=mock.Mock())['phase'],'dsh_active')
            self.assertEqual(r.step(jd,now=21,bridge_api=mock.Mock())['phase'],'dsh_active')
            self.assertEqual(r.step(jd,now=22,bridge_api=mock.Mock())['phase'],'needs_attention')
            h.assert_not_called()

    def test_empty_and_malformed_bucket_never_fallback(self):
        for buckets in ({},None,[],{'other':self.quota()['rateLimits']}):
            x=dict(self.quota(100),rateLimitsByLimitId=buckets)
            self.assertEqual(r.classify_quota(x,'codex',100),'unknown')

    def test_stale_secondary_and_nan_unknown(self):
        for part in ({'usedPercent':100,'resetsAt':99},{'usedPercent':float('nan'),'resetsAt':1000},{'usedPercent':True,'resetsAt':1000}):
            x=self.quota();x['rateLimits']['secondary']=part
            self.assertEqual(r.classify_quota(x,'codex',100),'unknown')

    def test_zero_extra_credits_does_not_mean_exhausted(self):
        x=self.quota();x['rateLimits']['credits']={'hasCredits':False,'unlimited':False,'balance':'0'}
        self.assertEqual(r.classify_quota(x,'codex',100),'available')

    def test_network_unknown_resets_confirmation(self):
        jd=self.job();x=self.quota(100)
        with mock.patch.object(r,'handoff') as h:
            r.step(jd,now=100,quota_reader=lambda _:x)
            r.step(jd,now=110,quota_reader=lambda _:None)
            self.assertIsNone(r.load_job(jd)['first_exhausted_at'])
            r.step(jd,now=120,quota_reader=lambda _:x)
            h.assert_not_called()

    def test_real_job_rejects_injected_signal_at_all_phases(self):
        jd=self.job()
        with self.assertRaises(r.RelayError):r.inject(self.state,jd.name,'exhausted')
        with self.assertRaises(r.RelayError):r.step(jd,signal='available')

    def test_rollout_only_fresh_matching_metadata(self):
        p=self.root/'rollout.jsonl';now=time.time()
        event=dict(type='event_msg',timestamp=datetime.fromtimestamp(now,timezone.utc).isoformat(),payload=dict(type='token_count',rate_limits=dict(limit_id='codex',primary=dict(used_percent=100,resets_at=now+1000))))
        p.write_text(json.dumps(event)+'\n',encoding='utf-8')
        self.assertEqual(r.rollout_quota(p,'codex',now-1,now),'exhausted')
        self.assertEqual(r.rollout_quota(p,'other',now-1,now),'unknown')
        self.assertEqual(r.rollout_quota(p,'codex',now+1,now),'unknown')
        self.assertEqual(r.rollout_quota(p,'codex',now-1,now+181),'unknown')
        event['payload']['type']='user_message'
        p.write_text(json.dumps(event)+'\n',encoding='utf-8')
        self.assertEqual(r.rollout_quota(p,'codex',now-1,now),'unknown')

    def test_rollout_bound_to_selected_thread(self):
        p=self.root/'rollout.jsonl';p.write_text(json.dumps(dict(type='session_meta',payload=dict(id=str(uuid.uuid4())))))
        with self.assertRaises(r.RelayError):
            r.arm(self.state,self.ws,self.task,['file.txt'],str(uuid.uuid4()),rollout=p)

    def test_interrupted_resume_is_unknown_never_retried(self):
        jd=self.job(auto_resume=True);j=r.load_job(jd)
        j.update(phase='return_ready',resume_attempted=True,resume_state='attempting');r.save_job(jd,j)
        with mock.patch.object(r.subprocess,'run') as run:
            self.assertEqual(r.step(jd)['phase'],'needs_attention');run.assert_not_called()

    def test_process_lock_byte_zero(self):
        lock=self.root/'process.lock'
        code="import sys;sys.path.insert(0,sys.argv[1]);import auto_relay as r;from pathlib import Path;exec('try:\\n with r.file_lock(Path(sys.argv[2])): pass\\nexcept OSError: sys.exit(7)')"
        with r.file_lock(lock):
            proc=subprocess.run([sys.executable,'-I','-c',code,str(ROOT/'bin'),str(lock)],capture_output=True,timeout=10)
        self.assertEqual(proc.returncode,7,proc.stderr.decode(errors='replace'))


if __name__=='__main__':unittest.main()

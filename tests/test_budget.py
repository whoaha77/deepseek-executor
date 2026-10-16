import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('team',ROOT/'bin/team.py');team=importlib.util.module_from_spec(s);s.loader.exec_module(team)
from budget import resolve


class Tests(unittest.TestCase):
    def test_profiles_and_custom(self):
        self.assertEqual(resolve({})['max_commands'],16)
        self.assertEqual(resolve({'budget_profile':'quick'})['implementation_sec'],60)
        b=resolve(dict(budget_profile='extended',budget_reason='cross-file invariant',budget={'verification_sec':300}))
        self.assertEqual(b['verification_sec'],300)
        self.assertEqual(b['max_output_files'],4)
    def test_invalid_budgets(self):
        cases=[{'budget_profile':'unknown'}, {'budget_profile':'extended'}, {'budget':{'verification_sec':300}},
               {'budget_reason':'specific','budget':{'max_commands':True}}, {'budget_reason':'specific','budget':{'unknown':1}},
               {'timeout_sec':480}, {'budget_reason':None}]
        for n in cases:
            with self.subTest(node=n),self.assertRaises(ValueError):resolve(n)
    def test_legacy_short_timeout(self):
        b=resolve({'timeout_sec':60})
        self.assertEqual(b['implementation_sec'],60);self.assertEqual(b['verification_sec'],90)
    def test_verifier_has_separate_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);clock=[0.0]
            node=dict(id='w',backend='worker',workspace=d,objective='one value',spec='write only value',inputs=[],outputs=['value.json'],
                timeout_sec=1,budget_reason='fixture simulated slow implementation',budget={'verification_sec':2},
                acceptance_argv=[sys.executable,'-I','-c','import time;time.sleep(0.3)'],checks=[dict(type='json',path='value.json')])
            local=dict(id='v',backend='local',workspace=d,depends_on=['w'],outputs=['v.json'],argv=[sys.executable,'-c',"from pathlib import Path;Path('v.json').write_text('{}')"],checks=[dict(type='json',path='v.json')])
            plan={'nodes':[node,local]};registry=team.validate(plan)
            real_popen=subprocess.Popen
            class Worker:
                def wait(self,timeout):
                    clock[0]=11.0
                    (root/'value.json').write_text('{}')
                    return 0
            def popen(argv,**kwargs):
                if argv[0]=='powershell':return Worker()
                return real_popen(argv,**kwargs)
            folder=root/'run';folder.mkdir()
            with patch.object(team,'time',SimpleNamespace(monotonic=lambda:clock[0])),patch.object(team.subprocess,'Popen',side_effect=popen):
                r=team.run_node(registry['w'],folder,100)
            self.assertEqual(r['status'],'done',r)
            self.assertEqual(r['acceptance_exit_code'],0)
            self.assertEqual(json.loads((folder/'w.phase.json').read_text())['phase'],'done')
    def test_three_independent_workers_require_reason(self):
        # Budget and wave validation tested without starting any worker.
        with tempfile.TemporaryDirectory() as d:
            def node(i):return dict(id=i,backend='worker',workspace=d,objective='one behavior',spec='one change',inputs=[],outputs=[i+'.txt'],checks=[dict(type='nonempty',path=i+'.txt')],acceptance_argv=[sys.executable,'-c','pass'])
            p=dict(nodes=[node(i) for i in 'abc'],max_worker_packets=3)
            with self.assertRaises(ValueError):team.validate(p)
            for bad in (None,False,[],{}):
                p['wave_reason']=bad
                with self.assertRaises(ValueError):team.validate(p)
            p['wave_reason']='three distinct file owners with separate checks'
            self.assertEqual(len(team.validate(p)),3)
    def test_expired_deadline_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            node=dict(id='w',backend='worker',workspace=d,objective='one',spec='one',inputs=[],outputs=['x.json'],
                      checks=[dict(type='json',path='x.json')],acceptance_argv=[sys.executable,'-c','pass'])
            folder=Path(d)/'run';folder.mkdir()
            registry=team.validate({'nodes':[node]})
            with patch.object(team.subprocess,'Popen',side_effect=AssertionError('must not launch')):
                result=team.run_node(registry['w'],folder,time.monotonic()-1)
            self.assertEqual(result['status'],'failed');self.assertIn('deadline',result['error'])


if __name__=='__main__':unittest.main()

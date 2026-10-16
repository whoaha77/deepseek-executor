import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('team',ROOT/'bin/team.py')
m=importlib.util.module_from_spec(s);s.loader.exec_module(m)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def worker(self,name='a'):
        return dict(id=name,backend='worker',workspace=str(self.root),objective='Fix one explicit value',
                    budget_profile='quick',inputs=[],outputs=[name+'.json'],spec='Change only the supplied value; stop on scope changes.',
                    timeout_sec=60,checks=[dict(type='json',path=name+'.json')],
                    acceptance_argv=[sys.executable,'-I','-c',f'import json;assert json.load(open({(name+".json")!r}))["ok"] is True'])
    def test_packet_scope(self):
        for change in ('files','timeout','objective','acceptance'):
            n=self.worker()
            if change=='files':
                n['outputs']=['a.json','b.json','c.json'];n['checks']=[dict(type='json',path=x) for x in n['outputs']]
            if change=='timeout':n['timeout_sec']=480
            if change=='objective':n.pop('objective')
            if change=='acceptance':n.pop('acceptance_argv')
            with self.subTest(change=change),self.assertRaises(ValueError):m.validate(dict(nodes=[n]))
    def test_wave_size(self):
        with self.assertRaises(ValueError):m.validate(dict(nodes=[self.worker(x) for x in 'abc']))
    def test_paid_dependency_gate(self):
        a,b=self.worker('a'),self.worker('b');b['depends_on']=['a']
        with self.assertRaises(ValueError):m.validate(dict(nodes=[a,b]))
        middle=dict(id='local',backend='local',workspace=str(self.root),depends_on=['a'],inputs=[],outputs=['local.json'],checks=[dict(type='json',path='local.json')],argv=[sys.executable,'-c','pass'])
        b['depends_on']=['local']
        with self.assertRaises(ValueError):m.validate(dict(nodes=[a,middle,b]))
    def test_independent_packets_allowed(self):
        self.assertEqual(len(m.validate(dict(nodes=[self.worker('a'),self.worker('b')]))),2)
    def execute(self,accept_ok):
        node=self.worker()
        if not accept_ok:node['acceptance_argv']=[sys.executable,'-I','-c','raise SystemExit(9)']
        local=dict(id='next',backend='local',workspace=str(self.root),depends_on=['a'],outputs=['next.json'],inputs=[],checks=[dict(type='json',path='next.json')],argv=[sys.executable,'-I','-c',"from pathlib import Path;Path('next.json').write_text('{}')"])
        plan=dict(nodes=[node,local],timeout_sec=20)
        real_popen=subprocess.Popen;seen=[]
        class Finished:
            def wait(self,timeout):return 0
        def fake(argv,**kwargs):
            if argv[0]=='powershell':
                seen.append(argv)
                (self.root/'a.json').write_text('{"ok":true}')
                return Finished()
            return real_popen(argv,**kwargs)
        with patch.object(m.subprocess,'Popen',side_effect=fake):
            code=m.execute(plan,m.validate(plan),self.root/'run')
        state=json.loads((self.root/'run/checkpoint.json').read_text())
        self.assertEqual(seen[0][seen[0].index('-MaxToolCommands')+1],'8')
        self.assertEqual(seen[0][seen[0].index('-MaxPatchFailures')+1],'1')
        self.assertEqual(state['main_agent_acceptance'],'pending')
        return code,state
    def test_real_local_verifier(self):
        code,state=self.execute(True)
        self.assertEqual(code,0);self.assertEqual(state['nodes']['a']['acceptance_exit_code'],0)
        self.assertEqual(state['nodes']['a']['main_agent_acceptance'],'pending')
    def test_local_failure_blocks_dependent(self):
        code,state=self.execute(False)
        self.assertEqual(code,1);self.assertEqual(state['nodes']['a']['acceptance_exit_code'],9)
        self.assertEqual(state['nodes']['next']['status'],'blocked_dependency')
        self.assertFalse((self.root/'next.json').exists())


if __name__=='__main__':unittest.main()

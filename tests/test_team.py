import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
import threading
from unittest.mock import patch

candidate=Path(__file__).resolve().parents[1]/'bin'/'team.py'
spec=importlib.util.spec_from_file_location('team',candidate if candidate.exists() else Path(__file__).with_name('team.py'))
team=importlib.util.module_from_spec(spec);spec.loader.exec_module(team)

class TeamTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='team-test-');self.addCleanup(self.tmp.cleanup);self.base=Path(self.tmp.name)
    def node(self,name='a',deps=None,output=None,code=None):
        output=output or name+'.json'
        return dict(id=name,backend='local',workspace=str(self.base),inputs=[],outputs=[output],depends_on=deps or [],
            argv=[sys.executable,'-c',code or f"from pathlib import Path;Path({output!r}).write_text('{{\"ok\":true}}')"],checks=[dict(type='json',path=output)],timeout_sec=10)
    def run_plan(self,nodes,parallel=2,timeout=30,label='run'):
        plan=dict(nodes=nodes,max_parallel=parallel,timeout_sec=timeout);registry=team.validate(plan);folder=self.base/label
        code=team.execute(plan,registry,folder);return code,json.loads((folder/'checkpoint.json').read_text(encoding='utf-8'))
    def test_parallel_then_join(self):
        a,b=self.node('a'),self.node('b')
        c=self.node('c',['a','b'],code="from pathlib import Path;import json;assert json.loads(Path('a.json').read_text(encoding='utf-8'))['ok'];assert json.loads(Path('b.json').read_text(encoding='utf-8'))['ok'];Path('c.json').write_text('{}')")
        c['inputs']=['a.json','b.json']
        code,state=self.run_plan([a,b,c]);self.assertEqual(code,0);self.assertEqual(len(state['nodes']),3);self.assertEqual(state['main_agent_acceptance'],'pending')
    def test_missing_dependency(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node(deps=['absent'])]))
    def test_cycle(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node('a',['b']),self.node('b',['a'])]))
    def test_duplicate_id(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node(),self.node()]))
    def test_unordered_write_conflict(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node(),self.node('b',output='a.json')]))
    def test_unordered_read_write_conflict(self):
        b=self.node('b');b['inputs']=['a.json']
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node(),b]))
    def test_escape(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node(output='../escape.json')]))
    def test_concurrency_bound(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node()],max_parallel=4))
    def test_missing_checks(self):
        node=self.node();node['checks']=[]
        with self.assertRaises(ValueError):team.validate(dict(nodes=[node]))
    def test_failed_verifier_blocks_dependent(self):
        a=self.node(code="from pathlib import Path;Path('a.json').write_text('invalid')")
        code,state=self.run_plan([a,self.node('b',['a'])]);self.assertEqual(code,1);self.assertEqual(state['nodes']['b']['status'],'blocked_dependency');self.assertFalse((self.base/'b.json').exists())
    def test_failed_exit_blocks_dependent(self):
        code,state=self.run_plan([self.node(code='raise SystemExit(7)'),self.node('b',['a'])]);self.assertEqual(code,1);self.assertEqual(state['nodes']['a']['exit_code'],7)
    def test_existing_output_backed_up(self):
        (self.base/'a.json').write_text('old');code,state=self.run_plan([self.node()]);self.assertEqual(code,0);self.assertEqual((self.base/'run'/'backups'/'a'/'a.json').read_text(encoding='utf-8'),'old')
    def test_missing_input_fails_before_command(self):
        node=self.node();node['inputs']=['absent.json'];code,state=self.run_plan([node]);self.assertEqual(code,1);self.assertFalse((self.base/'a.json').exists())
    def test_timeout_stops_process(self):
        node=self.node(code='import time;time.sleep(30)');node['timeout_sec']=1
        begin=time.monotonic();code,state=self.run_plan([node]);self.assertEqual(code,1);self.assertEqual(state['nodes']['a']['status'],'timeout');self.assertLess(time.monotonic()-begin,8)
    def test_every_output_must_be_checked(self):
        node=self.node();node['outputs'].append('extra.json')
        with self.assertRaises(ValueError):team.validate(dict(nodes=[node]))
    def test_self_dependency_rejected(self):
        with self.assertRaises(ValueError):team.validate(dict(nodes=[self.node('a',['a'])]))
    def test_global_deadline_precedes_node_budget(self):
        node=self.node(code='import time;time.sleep(30)');node['timeout_sec']=600
        begin=time.monotonic();code,state=self.run_plan([node],timeout=1)
        self.assertEqual(code,1);self.assertEqual(state['nodes']['a']['status'],'timeout');self.assertLess(time.monotonic()-begin,8)
    def test_actual_parallelism_does_not_exceed_cap(self):
        lock=threading.Lock();active=0;peak=0
        def fake(node,folder,deadline):
            nonlocal active,peak
            with lock:active+=1;peak=max(peak,active)
            time.sleep(0.08)
            with lock:active-=1
            return {'status':'done'}
        with patch.object(team,'run_node',side_effect=fake):
            code,state=self.run_plan([self.node(x) for x in ['a','b','c','d','e']],parallel=2)
        self.assertEqual(code,0);self.assertEqual(peak,2)
    def test_running_checkpoint_uses_memory_not_live_file(self):
        original=Path.read_text
        def read(path,*args,**kwargs):
            if path.name.endswith('.phase.json'):
                raise AssertionError('live phase file must not be opened by scheduler')
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',new=read):
            code,state=self.run_plan([self.node('a'),self.node('b')])
        self.assertEqual(code,0,state)

if __name__=='__main__':unittest.main()

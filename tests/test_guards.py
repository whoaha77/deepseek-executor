import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
def load(name):
    s=importlib.util.spec_from_file_location(name,ROOT/'bin'/(name+'.py'))
    m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
runner=load('process_runner')
review=load('review_text')


class Tests(unittest.TestCase):
    def execute(self, code, **limits):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'in').write_text('')
            request=dict(exe=sys.executable,args=['-I','-c',code],stdin=str(p/'in'),
                         stdout=str(p/'out'),stderr=str(p/'err'),timeout=5,**limits)
            result=runner.run(request)
            checkpoint=json.loads((p/'out.monitor.json').read_text())
            return result,checkpoint

    def test_completed(self):
        code,state=self.execute('print("{}",flush=True)')
        self.assertEqual(code,0);self.assertEqual(state['reason'],'completed')

    def test_command_budget_and_partial_preservation(self):
        event=json.dumps(dict(type='item.started',item=dict(id='one',type='command_execution')))
        with tempfile.TemporaryDirectory() as d:
            marker=Path(d)/'partial'
            script=f'import pathlib,time;pathlib.Path({str(marker)!r}).write_text("partial");print({event!r},flush=True);print({event.replace('one','two')!r},flush=True);time.sleep(30)'
            code,state=self.execute(script,max_commands=1)
            self.assertEqual(code,125);self.assertEqual(state['reason'],'command_budget')
            self.assertEqual(marker.read_text(),'partial')

    def test_patch_budget(self):
        event=json.dumps(dict(type='item.completed',item=dict(id='one',type='command_execution',exit_code=1,aggregated_output='Invalid patch: ending')))
        code,state=self.execute(f'import time;print({event!r},flush=True);time.sleep(30)',max_patch_errors=1)
        self.assertEqual(code,125);self.assertEqual(state['patch_errors'],1)

    def test_final_event_without_newline(self):
        event=json.dumps(dict(type='item.started',item=dict(id='one',type='command_execution')))
        code,state=self.execute(f'print({event!r},end="",flush=True)',max_commands=1)
        self.assertEqual(code,0);self.assertEqual(state['commands'],1)

    def test_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'in').write_text('')
            code=runner.run(dict(exe=sys.executable,args=['-I','-c','import time;time.sleep(30)'],stdin=str(p/'in'),stdout=str(p/'out'),stderr=str(p/'err'),timeout=1))
            self.assertEqual(code,124)

    def test_duplicate_and_malformed(self):
        a=runner.Activity()
        for t in ('item.started','item.completed'):
            a.consume(json.dumps(dict(type=t,item=dict(id='one',type='command_execution',exit_code=0))))
        a.consume('bad');a.consume('[]')
        self.assertEqual(len(a.commands),1);self.assertEqual(a.malformed,2)

    def test_evidence_id_and_repeat_line(self):
        sources={'ui.tsx':'same\ncontext\nsame\n'}
        text,anchors=review.indexed_evidence(sources)
        row=dict(issues=[dict(evidence_id='E000003',counterexample='input',impact='effect')],unverified=[])
        result=review.resolve_review(row,anchors,sources)
        self.assertEqual(result['issues'][0]['evidence_line'],3)
        self.assertEqual(result['issues'][0]['path'],'ui.tsx')
        self.assertEqual(result['issues'][0]['quote'],'same')
        row['issues'][0]['evidence_id']='E999999'
        with self.assertRaises(ValueError):review.resolve_review(row,anchors,sources)

    def test_preflight_no_network(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'ui.tsx').write_text('export const x=1;')
            args=['review','--workspace',d,'--what','inspect','--files','ui.tsx','--dry-run']
            with patch.object(sys,'argv',args),patch.object(review.subprocess,'run',side_effect=AssertionError('paid process')):
                self.assertEqual(review.main(),0)
        with self.assertRaises(ValueError):review.indexed_evidence({'x.ts':'x'*1601})


if __name__=='__main__':unittest.main()

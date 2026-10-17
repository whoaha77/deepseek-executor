"""Transport, local startup, and human-only export boundaries; offline."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch,MagicMock
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'bin'))
import dsh_bridge as bridge
import dsh_start as launch

class RuntimeTests(unittest.TestCase):
    def test_redirect_rejects_same_prefix_different_origin(self):
        handler=bridge._SameOriginRedirect('http://127.0.0.1:3088')
        request=urllib.request.Request('http://127.0.0.1:3088/')
        for target in ['http://127.0.0.1:30880/','http://example.com/','http://user@127.0.0.1:3088/']:
            with self.assertRaises(Exception):handler.redirect_request(request,None,302,'test',{},target)
    def test_required_camelcase_methods(self):
        for method in ['session/selectModel','session/modelCatalog']:
            self.assertEqual(bridge.rpc_envelope(method,{},'id')['method'],method)
    def test_duplicate_file_aliases_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'a.py';p.write_text('pass')
            with self.assertRaises(Exception):bridge.fingerprint(folder,['a.py','./a.py'])
    def test_runtime_messages_never_exported(self):
        records=[dict(type='event',event=dict(seq=1,type='user/message',data=dict(role='user',source={'kind':'future-internal-context'},content=[dict(type='text',text='internal')])))]
        self.assertEqual(bridge.public_messages(records),[])
    def test_uppercase_env_task_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'.ENV.md';p.write_text('not a credential')
            with self.assertRaises(Exception):bridge.read_task(p)
    def test_empty_or_duplicate_launch_tokens_refused(self):
        for url in ['http://127.0.0.1:3088/?token=','http://127.0.0.1:3088/?token=a&token=b']:
            with self.assertRaises(Exception):bridge.validate_launch_url(url,3088)
    def test_export_refuses_secret_text(self):
        record=dict(type='event',event=dict(seq=1,type='assistant/message',data={'message':{'role':'assistant','content':[{'type':'text','text':'sk-'+'A'*24}]}}))
        with self.assertRaises(Exception):bridge.public_messages([record])
    def test_rpc_id_mismatch_refused(self):
        api=bridge.API.__new__(bridge.API);api._base='http://127.0.0.1:3088/'
        api._opener=MagicMock();api._opener.open.return_value=io.BytesIO(json.dumps(dict(type='server-response',rpcId='wrong',result={'ok':True,'value':{}})).encode())
        with self.assertRaises(bridge.BridgeError):api.rpc('session/list',{'_request':{}})
    def test_existing_live_server_is_reused_without_key_or_spawn(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);(state/'process.json').write_text(json.dumps(dict(pid=123,home=str(state/'home'),port=3088)))
            fake=MagicMock();fake.base='http://127.0.0.1:3088'
            with patch.object(launch,'API',return_value=fake),patch.object(launch.subprocess,'Popen') as spawn:
                result=launch.start(state)
                self.assertTrue(result['reused']);spawn.assert_not_called()
    def test_start_refuses_another_home_for_active_server(self):
        with tempfile.TemporaryDirectory() as folder:
            state=Path(folder);(state/'process.json').write_text(json.dumps(dict(pid=123,home=str(state/'a'),port=3088)))
            with patch.object(launch,'API',return_value=MagicMock()):
                with self.assertRaises(Exception):launch.start(state,state/'b')

if __name__=='__main__':unittest.main()

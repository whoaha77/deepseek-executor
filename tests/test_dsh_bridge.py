import importlib.util
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "bin" / "dsh_bridge.py"

spec = importlib.util.spec_from_file_location("dsh_bridge", MOD_PATH)
dsh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dsh)


def make_event(seq, etype, data):
    return {"type": "event", "event": {"seq": seq, "type": etype, "data": data}}


def user_event(seq, text="hello"):
    return make_event(seq, "user/message", {
        "role": "user",
        "source": {"kind": "user"},
        "content": [{"type": "text", "text": text}],
    })


def assistant_event(seq, text="world", thinking="not public"):
    return make_event(seq, "assistant/message", {
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": text},
                {"type": "thinking", "thinking": thinking},
            ],
        }
    })


class FakeAPI:
    def __init__(self, state, script=None):
        self.state = state
        self.script = script or {}
        self.create_returns = {}
        self.calls = []
        self.base = "http://127.0.0.1:9999/"
        self.launch = "http://127.0.0.1:9999/?token=FAKE"

    def rpc(self, method, args):
        self.calls.append((method, args))
        handler = self.script.get(method)
        if callable(handler):
            return handler(args)
        if method in self.script:
            return self.script[method]
        if method == "session/create":
            sid = args["request"]["sessionId"]
            self.create_returns["sid"] = sid
            return {"sessionId": "codex-relay-" + sid}
        if method == "session/selectModel":
            return {"selected": {"provider": "deepseek-official", "model": "deepseek-flash"}}
        if method == "session/rename":
            return {}
        if method == "session/prompt":
            return {"accepted": True}
        raise AssertionError("unexpected rpc " + method)

    def count(self, method):
        return sum(1 for m, _ in self.calls if m == method)


def accepted_hb(api):
    sid = "codex-relay-" + "0" * 32
    api.script["session/list"] = {"items": [{"sessionId": sid, "running": False}]}
    api.script["session/projections"] = {
        "asOfSeq": 7,
        "values": {"inbox": {"next-turn": [], "next-step": []}, "tokenUsage": {}},
    }
    api.script["session/page"] = {
        "records": [user_event(1), assistant_event(2)],
        "hasMore": False,
    }
    return sid


class TestValidateWorkspace(unittest.TestCase):
    def test_rejects_protected_workspace(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            protected = p / "protected"
            protected.mkdir()
            orig = dsh.PROTECTED_ROOTS
            dsh.PROTECTED_ROOTS = [protected]
            try:
                with self.assertRaises(Exception):
                    dsh.validate_workspace(str(protected))
            finally:
                dsh.PROTECTED_ROOTS = orig
            self.assertTrue(p.exists())

    def test_rejects_missing(self):
        with self.assertRaises(Exception):
            dsh.validate_workspace(str(ROOT / "does-not-exist-xyz"))


class TestReadTask(unittest.TestCase):
    def test_reads_explicit_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "t.md"
            f.write_text("hello task", encoding="utf-8")
            self.assertIn("hello task", dsh.read_task(str(f)))

    def test_refuses_synthetic_key_no_echo(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "t.md"
            secret = "sk-" + "a" * 24
            f.write_text("key " + secret, encoding="utf-8")
            with self.assertRaises(Exception) as ctx:
                dsh.read_task(str(f))
            self.assertNotIn(secret, str(ctx.exception))

    def test_refuses_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "t.md"
            f.write_bytes(b"\xff\xfe\x00bad")
            with self.assertRaises(Exception):
                dsh.read_task(str(f))


class TestFingerprint(unittest.TestCase):
    def test_detects_changes_and_absent_none(self):
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "a.txt").write_text("one", encoding="utf-8")
            first = dsh.fingerprint(str(ws), ["a.txt", "missing.txt"])
            self.assertEqual(first["missing.txt"], None)
            self.assertTrue(first["a.txt"])
            (ws / "a.txt").write_text("two", encoding="utf-8")
            second = dsh.fingerprint(str(ws), ["a.txt", "missing.txt"])
            self.assertNotEqual(first["a.txt"], second["a.txt"])

    def test_rejects_traversal_drive_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            for bad in [["../x.txt"], ["C:/x.txt"], ["a.txt", "a.txt"]]:
                with self.assertRaises(Exception):
                    dsh.fingerprint(td, bad)


class TestPublicMessages(unittest.TestCase):
    def test_only_human_source_and_assistant_text(self):
        records = [
            user_event(1),
            assistant_event(2),
            make_event(3, "user/message", {
                "role": "user",
                "source": {"kind": "runtime"},
                "content": [{"type": "text", "text": "runtime"}],
            }),
            make_event(4, "user/message", {
                "role": "system",
                "source": {"kind": "user"},
                "content": [{"type": "text", "text": "sys"}],
            }),
        ]
        out = dsh.public_messages(records)
        texts = [m["text"] for m in out]
        self.assertIn("hello", texts)
        self.assertIn("world", texts)
        self.assertNotIn("runtime", texts)
        self.assertNotIn("sys", texts)
        self.assertNotIn("not public", texts)


class TestValidateLaunchUrl(unittest.TestCase):
    def test_rejects_bad_urls(self):
        port = 1234
        token = "sometokenvalue"
        good = "http://127.0.0.1:%d/?token=%s" % (port, token)
        self.assertTrue(dsh.validate_launch_url(good, port))
        bad = [
            "http://evil.com:%d/?token=%s" % (port, token),
            "http://127.0.0.1:9999/?token=%s" % token,
            "http://user:pass@127.0.0.1:%d/?token=%s" % (port, token),
            "http://127.0.0.1:%d/" % port,
        ]
        for u in bad:
            with self.assertRaises(Exception):
                dsh.validate_launch_url(u, port)


class TestRpcEnvelope(unittest.TestCase):
    def test_valid_camelcase_method(self):
        env = dsh.rpc_envelope("session/create", {"a": 1}, "r1")
        self.assertEqual(env["type"], "client-request")
        self.assertEqual(env["rpcId"], "r1")
        self.assertEqual(env["method"], "session/create")
        self.assertEqual(env["payload"]["args"], {"a": 1})

    def test_rejects_invalid_method(self):
        for bad in ["session create", "session;drop", "1bad", "session/create!"]:
            with self.assertRaises(Exception):
                dsh.rpc_envelope(bad, {}, "r1")


class TestHandoffFlow(unittest.TestCase):
    def _setup(self):
        td = tempfile.TemporaryDirectory()
        state = Path(td.name) / "state"
        ws = Path(td.name) / "ws"
        state.mkdir()
        ws.mkdir()
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        task = Path(td.name) / "task.md"
        task.write_text("do the work", encoding="utf-8")
        fake = FakeAPI(str(state))
        return td, state, ws, task, fake

    def test_manifest_submitted_only_after_accepted(self):
        td, state, ws, task, fake = self._setup()
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                result = dsh.handoff(str(state), str(ws), str(task), ["a.txt"], "T")
            manifest = json.loads(Path(result["packet_path"]).read_text(encoding="utf-8")) \
                if result["packet_path"].endswith(".json") else None
            self.assertEqual(fake.count("session/create"), 1)
            self.assertEqual(fake.count("session/prompt"), 1)
            self.assertTrue(result["handoff_id"].startswith("handoff-"))
            self.assertEqual(manifest['dispatch_mode'],'acknowledge')
            prompt=next(a['request']['content'][0]['text'] for m,a in fake.calls if m=='session/prompt')
            self.assertIn('只确认收到交接',prompt)
        finally:
            td.cleanup()

    def test_execute_submits_one_actionable_prompt(self):
        td,state,ws,task,fake=self._setup()
        try:
            with mock.patch.object(dsh,'API',lambda s:fake):
                result=dsh.handoff(state,ws,task,['a.txt'],'execute',execute=True)
            manifest=json.loads(Path(result['packet_path']).read_text(encoding='utf-8'))
            self.assertEqual(manifest['dispatch_mode'],'execute')
            self.assertEqual(manifest['status'],'submitted')
            self.assertEqual(fake.count('session/prompt'),1)
            prompt=next(a['request']['content'][0]['text'] for m,a in fake.calls if m=='session/prompt')
            self.assertIn('现在直接执行',prompt)
            self.assertNotIn('只确认收到交接',prompt)
        finally:td.cleanup()

    def test_execute_rejects_truthy_string_before_remote_mutation(self):
        td,state,ws,task,fake=self._setup()
        try:
            with mock.patch.object(dsh,'API',lambda s:fake):
                with self.assertRaises(dsh.BridgeError):
                    dsh.handoff(state,ws,task,['a.txt'],'bad',execute='yes')
            self.assertEqual(fake.calls,[])
        finally:td.cleanup()

    def test_rejected_prompt_preserves_unknown_or_failed(self):
        td, state, ws, task, fake = self._setup()
        created = {}

        def create(args):
            created["sid"] = "codex-relay-" + args["request"]["sessionId"]
            return {"sessionId": created["sid"]}

        def prompt(args):
            return {"accepted": False}

        fake.script["session/create"] = create
        fake.script["session/prompt"] = prompt
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                with self.assertRaises(Exception):
                    dsh.handoff(str(state), str(ws), str(task), ["a.txt"], "T")
            self.assertEqual(fake.count("session/prompt"), 1)
            hbdir = Path(str(state)) / "handoffs"
            manifests = list(hbdir.rglob("*.json"))
            found = False
            for m in manifests:
                data = json.loads(m.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "status" in data:
                    self.assertIn(data["status"], ("preparing", "unknown_or_failed"))
                    found = True
            self.assertTrue(found)
        finally:
            td.cleanup()

    def test_unexpected_model_stops_before_prompt(self):
        td, state, ws, task, fake = self._setup()
        fake.script["session/create"] = lambda a: {
            "sessionId": "codex-relay-" + a["request"]["sessionId"]
        }
        fake.script["session/selectModel"] = lambda a: {
            "selected": {"provider": "deepseek-official", "model": "some-other"}
        }
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                with self.assertRaises(Exception):
                    dsh.handoff(str(state), str(ws), str(task), ["a.txt"], "T")
            self.assertEqual(fake.count("session/prompt"), 0)
        finally:
            td.cleanup()


class TestReturnPacket(unittest.TestCase):
    def _handoff(self):
        td = tempfile.TemporaryDirectory()
        state = Path(td.name) / "state"
        ws = Path(td.name) / "ws"
        state.mkdir()
        ws.mkdir()
        (ws / "a.txt").write_text("hello", encoding="utf-8")
        task = Path(td.name) / "task.md"
        task.write_text("do the work", encoding="utf-8")
        fake = FakeAPI(str(state))
        with mock.patch.object(dsh, "API", lambda s: fake):
            res = dsh.handoff(str(state), str(ws), str(task), ["a.txt"], "T")
        return td, state, ws, fake, res

    def test_normal_handback(self):
        td, state, ws, fake, res = self._handoff()
        sid = res["session_id"]
        fake.calls.clear()
        fake.script["session/list"] = {"items": [{"sessionId": sid, "running": False}]}
        fake.script["session/projections"] = {
            "asOfSeq": 7,
            "values": {"inbox": {"next-turn": [], "next-step": []}, "tokenUsage": {"total": 3}},
        }
        fake.script["session/page"] = {
            "records": [user_event(1), assistant_event(2)],
            "hasMore": False,
        }
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                out = dsh.return_packet(str(state), res["handoff_id"])
            self.assertEqual(fake.count("session/prompt"), 0)
            self.assertEqual(fake.count("session/create"), 0)
            report = json.loads(Path(out["json_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["main_agent_acceptance"], "pending")
        finally:
            td.cleanup()

    def test_running_refused(self):
        td, state, ws, fake, res = self._handoff()
        sid = res["session_id"]
        fake.script["session/list"] = {"items": [{"sessionId": sid, "running": True}]}
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                with self.assertRaises(Exception):
                    dsh.return_packet(str(state), res["handoff_id"])
        finally:
            td.cleanup()

    def test_pending_queue_refused(self):
        td, state, ws, fake, res = self._handoff()
        sid = res["session_id"]
        fake.script["session/list"] = {"items": [{"sessionId": sid, "running": False}]}
        fake.script["session/projections"] = {
            "asOfSeq": 7,
            "values": {"inbox": {"next-turn": [{"x": 1}], "next-step": []}, "tokenUsage": {}},
        }
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                with self.assertRaises(Exception):
                    dsh.return_packet(str(state), res["handoff_id"])
        finally:
            td.cleanup()

    def test_pagination_no_progress_refused(self):
        td, state, ws, fake, res = self._handoff()
        sid = res["session_id"]
        fake.script["session/list"] = {"items": [{"sessionId": sid, "running": False}]}
        fake.script["session/projections"] = {
            "asOfSeq": 100,
            "values": {"inbox": {"next-turn": [], "next-step": []}, "tokenUsage": {}},
        }
        fake.script["session/page"] = {
            "records": [user_event(50)],
            "hasMore": True,
        }
        try:
            with mock.patch.object(dsh, "API", lambda s: fake):
                with self.assertRaises(Exception):
                    dsh.return_packet(str(state), res["handoff_id"])
        finally:
            td.cleanup()

    def test_malformed_or_conflicting_sequences_refused(self):
        for records in ([make_event("invalid", "user/message", {})], [user_event(1), assistant_event(1)]):
            td, state, ws, fake, res = self._handoff()
            try:
                fake.script["session/list"]={"items":[{"sessionId":res["session_id"],"running":False}]}
                fake.script["session/projections"]={"asOfSeq":100,"values":{"inbox":{"next-turn":[],"next-step":[]},"tokenUsage":{}}}
                fake.script["session/page"]={"records":records,"hasMore":False}
                with mock.patch.object(dsh,"API",lambda s:fake):
                    with self.assertRaises(dsh.BridgeError):dsh.return_packet(str(state),res["handoff_id"])
                self.assertFalse(list((state/'handoffs'/res['handoff_id']).rglob('return-*.json')))
            finally:td.cleanup()


if __name__ == "__main__":
    unittest.main()

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import check_bundle  # noqa: E402

PY = sys.executable


def _plan(workspace, checks):
    return {"workspace": str(workspace), "checks": checks}


def _chk(cid, code, timeout=10):
    return {"id": cid, "argv": [PY, "-c", code], "timeout_sec": timeout}


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.base = Path(self.td.name)
        self.ws = self.base / "ws"
        self.ws.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_pass_and_fail(self):
        out = self.base / "out1"
        plan = _plan(self.ws, [_chk("ok", "raise SystemExit(0)"),
                               _chk("bad", "raise SystemExit(3)")])
        summary = check_bundle.run_plan(plan, out)
        self.assertEqual(summary["result_count"], 2)
        self.assertEqual(summary["failed_ids"], ["bad"])
        self.assertEqual(summary["checks"][0]["status"], "passed")
        self.assertEqual(summary["checks"][1]["status"], "failed")
        self.assertEqual(summary["checks"][1]["exit_code"], 3)
        self.assertTrue((out / "summary.json").exists())
        self.assertEqual(summary["main_agent_acceptance"], "pending")

    def test_timeout(self):
        out = self.base / "out2"
        plan = _plan(self.ws, [_chk("slow", "import time; time.sleep(30)", timeout=1)])
        start = time.monotonic()
        summary = check_bundle.run_plan(plan, out)
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(summary["checks"][0]["status"], "timeout")
        self.assertEqual(summary["checks"][0]["exit_code"], 124)

    def test_timeout_kills_descendant(self):
        out = self.base / "out3"
        marker = self.ws / "marker.txt"
        child = f"import time;time.sleep(3);open({str(marker)!r},'w').write('x')"
        code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(30)"
        plan = _plan(self.ws, [_chk("tree", code, timeout=1)])
        summary = check_bundle.run_plan(plan, out)
        self.assertEqual(summary["checks"][0]["status"], "timeout")
        time.sleep(4)
        self.assertFalse(marker.exists())

    def test_invalid_before_sideeffects(self):
        out = self.base / "out4"
        dup = [_chk("a", "pass"), _chk("a", "pass")]
        summary = check_bundle.run_plan(_plan(self.ws, dup), out)
        self.assertEqual(summary["status"], "invalid")
        self.assertFalse(out.exists())
        bad_exe = [{"id": "x", "argv": ["no_such_exe_zzz"], "timeout_sec": 5}]
        summary = check_bundle.run_plan(_plan(self.ws, bad_exe), out)
        self.assertEqual(summary["status"], "invalid")
        self.assertFalse(out.exists())

    def test_no_overwrite(self):
        out = self.base / "out5"
        out.mkdir()
        summary = check_bundle.run_plan(_plan(self.ws, [_chk("a", "pass")]), out)
        self.assertEqual(summary["status"], "invalid")

    def test_stdout_suppressed_and_no_secret(self):
        out = self.base / "out6"
        token = "sec" + "retkey"
        code = "import sys; print(r'%s'); print('done')" % token
        plan = _plan(self.ws, [_chk("quiet", code)])
        summary = check_bundle.run_plan(plan, out)
        self.assertEqual(summary["status"], "ok")
        so = (out / "quiet.stdout.log").read_text()
        self.assertIn(token, so)
        pub = json.dumps({k: summary[k] for k in ("status", "result_count", "failed_ids")})
        self.assertNotIn(token, pub)
        self.assertLess(len(pub), 1500)

    def test_cli_json(self):
        out = self.base / "out7"
        planfile = self.base / "plan.json"
        plan = _plan(self.ws, [_chk("a", "pass")])
        planfile.write_text(json.dumps(plan), encoding="utf-8")
        proc = subprocess.run([PY, str(ROOT / "bin" / "check_bundle.py"),
                               "--plan", str(planfile), "--out", str(out)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout.strip())
        self.assertEqual(payload["status"], "ok")
        self.assertLessEqual(len(proc.stdout.strip()), 1500)

    def test_import_no_run(self):
        self.assertTrue(hasattr(check_bundle, "run_plan"))
        self.assertTrue(hasattr(check_bundle, "main"))


if __name__ == "__main__":
    unittest.main()

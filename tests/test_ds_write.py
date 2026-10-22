import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import ds_write as d  # noqa: E402


class DsWriteContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.ws = self.base / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)
        (self.ws / "mod.py").write_text("ORIGINAL = 1\n", encoding="utf-8")
        self.out = self.base / "out"
        self.write_task()

    def ns(self, ws=None, out=None, task=None, inputs=None, execute=True,
           check_plan=None, repair_rounds=1, timeout_sec=120, target="mod.py",
           dry_run=False):
        return d._build_parser().parse_args([
            "--workspace", str(ws if ws is not None else self.ws),
            "--target", target,
            "--task", str(task if task is not None else self.base / "task.txt"),
            "--inputs", *[str(i) for i in (inputs or [])],
            "--out", str(out if out is not None else self.out),
            "--repair-rounds", str(repair_rounds),
            "--timeout-sec", str(timeout_sec),
        ] + (["--check-plan", str(check_plan)] if check_plan else [])
          + (["--execute"] if execute and not dry_run else [])
          + (["--dry-run"] if dry_run else []))

    def write_task(self, text="task body\n"):
        task = self.base / "task.txt"
        task.write_text(text, encoding="utf-8")
        return task

    def write_plan(self, code, ws=None, timeout_sec=10, tdir=None):
        tdir = Path(tdir if tdir is not None else self.base)
        path = tdir / "plan.json"
        path.write_text(json.dumps({
            "workspace": str(ws if ws is not None else self.ws),
            "checks": [{
                "id": "check",
                "argv": [sys.executable, "-c", code],
                "timeout_sec": timeout_sec,
            }],
        }), encoding="utf-8")
        return path

    def fake_generator(self, writer):
        calls = {"n": 0}

        def gen(instruction, files, candidate, logprefix, remaining, trace_id):
            calls["n"] += 1
            writer(calls["n"], candidate)
            return True

        return gen, calls

    def write_candidate(self, path, content):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def run_generate(self, ns, gen):
        with mock.patch.object(d, "_generate", gen, create=True):
            return d.run(ns)

    def read_receipt(self, result):
        p = Path(result["receipt"])
        self.assertTrue(p.is_file(), "receipt file missing")
        return json.loads(p.read_text(encoding="utf-8"))

    def assertInvalidArg(self, ns, gen):
        gen.assert_not_called()
        with self.assertRaises((ValueError, d.b.BridgeError)):
            d.run(ns)

    def test_dryrun_no_outputdir_gen_write(self):
        gen = mock.Mock()
        ns = self.ns(execute=False, dry_run=True)
        before = sorted(p.name for p in self.ws.iterdir())
        result = self.run_generate(ns, gen)
        gen.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(before, sorted(p.name for p in self.ws.iterdir()))
        self.assertEqual(result["status"], "dry_run")
        self.assertIsNone(result["receipt"])
        self.assertTrue(result["pending"])

    def test_existing_file_backup_after_generation(self):
        task = self.write_task()

        def writer(n, candidate):
            self.assertFalse((self.out / "original.py").exists(),
                             "backup must not exist before apply")
            self.write_candidate(candidate, "NEW = 2\n")

        gen, calls = self.fake_generator(writer)
        code = "import mod; assert mod.NEW == 2"
        result = self.run_generate(
            self.ns(task=task, check_plan=self.write_plan(code)), gen)
        self.assertEqual(calls["n"], 1)
        backup = self.out / "original.py"
        self.assertTrue(backup.is_file(), "original.py backup missing")
        self.assertEqual(backup.read_text(encoding="utf-8"), "ORIGINAL = 1\n")
        self.assertEqual(result["status"], "verified_pending_review")
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["main_agent_acceptance"], "pending")

    def test_newfile_generation_without_checks_pending_review(self):
        (self.ws / "mod.py").unlink()
        task = self.write_task()
        gen, calls = self.fake_generator(
            lambda n, c: self.write_candidate(c, "X = 1\n"))
        result = self.run_generate(self.ns(task=task, check_plan=None), gen)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result["status"], "generated_pending_review")
        self.assertTrue(result["pending"])
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["status"], "generated_pending_review")
        self.assertEqual(receipt["main_agent_acceptance"], "pending")
        self.assertEqual((self.ws / "mod.py").read_text(encoding="utf-8"), "X = 1\n")
        self.assertFalse((self.out / "original.py").exists())

    def test_local_check_passes_verified_pending_review(self):
        task = self.write_task()
        code = "import mod; assert mod.NEW == 5"
        gen, calls = self.fake_generator(
            lambda n, c: self.write_candidate(c, "NEW = 5\n"))
        result = self.run_generate(
            self.ns(task=task, check_plan=self.write_plan(code)), gen)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result["status"], "verified_pending_review")
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["status"], "verified_pending_review")
        self.assertEqual(receipt["main_agent_acceptance"], "pending")
        self.assertEqual(receipt["task_sha"], d._task_sha(task))
        self.assertTrue(receipt["checks"])

    def test_failing_check_then_repaired_candidate_verified_backup_original(self):
        task = self.write_task()
        code = "from pathlib import Path; ns={}; exec(Path('mod.py').read_text(),ns); assert ns['NEW'] == 7"

        def writer(n, candidate):
            if n == 1:
                self.write_candidate(candidate, "NEW = 1\n")
            else:
                self.write_candidate(candidate, "NEW = 7\n")

        gen, calls = self.fake_generator(writer)
        plan = self.write_plan(code)
        result = self.run_generate(
            self.ns(task=task, check_plan=plan, repair_rounds=1), gen)
        self.assertEqual(calls["n"], 2)
        backup = self.out / "original.py"
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "ORIGINAL = 1\n")
        self.assertEqual(result["status"], "verified_pending_review")
        self.assertEqual(result["attempt_count"], 2)
        receipt = self.read_receipt(result)
        statuses = [a["status"] for a in receipt["attempts"]]
        self.assertEqual(statuses, ["applied", "applied"])
        self.assertEqual(receipt["checks"][0]["status"], "failed")

    def test_failing_check_rounds0_blocks(self):
        task = self.write_task()
        code = "import mod; assert mod.NEW == 9"
        gen, calls = self.fake_generator(
            lambda n, c: self.write_candidate(c, "NEW = 1\n"))
        result = self.run_generate(
            self.ns(task=task, check_plan=self.write_plan(code), repair_rounds=0), gen)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result["status"], "blocked")
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["status"], "blocked")
        self.assertNotEqual(receipt["checks"][0]["status"], "ok")

    def test_malformed_python_then_repair(self):
        task = self.write_task()
        code = "import mod; assert mod.NEW == 3"

        def writer(n, candidate):
            if n == 1:
                self.write_candidate(candidate, "def broken(:\n")
            else:
                self.write_candidate(candidate, "NEW = 3\n")

        gen, calls = self.fake_generator(writer)
        result = self.run_generate(
            self.ns(task=task, check_plan=self.write_plan(code), repair_rounds=1), gen)
        self.assertEqual(calls["n"], 2)
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["attempts"][0]["status"], "invalid_candidate")
        self.assertEqual(receipt["attempts"][1]["status"], "applied")
        self.assertEqual(result["status"], "verified_pending_review")

    def test_generator_runtime_error_blocks_attempt_count1(self):
        task = self.write_task()

        def gen(instruction, files, candidate, logprefix, remaining, trace_id):
            raise RuntimeError("boom")

        ns = self.ns(task=task)
        result = self.run_generate(ns, gen)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["attempt_count"], 1)
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["attempts"][0]["status"], "generation_failed")
        self.assertEqual((self.ws / "mod.py").read_text(encoding="utf-8"), "ORIGINAL = 1\n")

    def test_generator_writes_foreign_target_not_overwritten(self):
        task = self.write_task()

        def writer(n, candidate):
            self.write_candidate(candidate, "NEW = 4\n")
            self.write_candidate(self.ws / "mod.py", "FOREIGN = 99\n")

        gen, calls = self.fake_generator(writer)
        code = "import mod; assert mod.NEW == 4"
        result = self.run_generate(
            self.ns(task=task, check_plan=self.write_plan(code)), gen)
        self.assertNotEqual(result["status"], "verified_pending_review")
        self.assertEqual((self.ws / "mod.py").read_text(encoding="utf-8"), "FOREIGN = 99\n")
        self.assertEqual(result["status"], "blocked")

    def test_generator_changes_task_must_block(self):
        task = self.write_task()

        def writer(n, candidate):
            self.write_candidate(candidate, "NEW = 4\n")
            task.write_text("tampered task\n", encoding="utf-8")

        gen, calls = self.fake_generator(writer)
        result = self.run_generate(self.ns(task=task), gen)
        self.assertEqual(result["status"], "blocked")
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["status"], "blocked")

    def test_check_changes_target_never_verified(self):
        task = self.write_task()
        code = (
            "from pathlib import Path\n"
            "p = Path(%r)\n"
            "p.write_text('TAMPERED = 1\\n', encoding='utf-8')\n"
            "import mod\n"
            "assert mod.NEW == 6\n" % str(self.ws / "mod.py")
        )
        gen, calls = self.fake_generator(
            lambda n, c: self.write_candidate(c, "NEW = 6\n"))
        plan = self.write_plan(code)
        result = self.run_generate(
            self.ns(task=task, check_plan=plan, repair_rounds=0), gen)
        self.assertNotEqual(result["status"], "verified_pending_review")
        self.assertIn(result["status"], ("needs_attention", "blocked"))
        receipt = self.read_receipt(result)
        self.assertNotEqual(receipt["status"], "verified_pending_review")
        self.assertEqual((self.ws / "mod.py").read_text(encoding="utf-8"),
                         "TAMPERED = 1\n")

    def test_traversal_target_rejected_before_api(self):
        gen = mock.Mock()
        ns = self.ns(target="../escape.py")
        self.assertInvalidArg(ns, gen)

    def test_timeout_nan_rejected(self):
        gen = mock.Mock()
        ns = self.ns(timeout_sec=float("nan"))
        self.assertInvalidArg(ns, gen)

    def test_duplicate_input_alias_rejected(self):
        gen = mock.Mock()
        f = self.ws / "input.txt"
        f.write_text("x", encoding="utf-8")
        ns = self.ns(inputs=["input.txt", "input.txt"], target="mod.py",
                     task="task.txt")
        (self.base / "task.txt").write_text("t\n", encoding="utf-8")
        self.assertInvalidArg(ns, gen)

    def test_aggregate_existing_target_too_large_rejected(self):
        big = "A" * (8 * 1024 * 1024)
        (self.ws / "mod.py").write_text(big, encoding="utf-8")
        (self.base / "task.txt").write_text("t\n", encoding="utf-8")
        gen = mock.Mock()
        ns = self.ns(task="task.txt")
        self.assertInvalidArg(ns, gen)


if __name__ == "__main__":
    unittest.main()

class DsWriteBoundaryTests(unittest.TestCase):
    setUp = DsWriteContractTests.setUp
    ns = DsWriteContractTests.ns
    write_task = DsWriteContractTests.write_task
    write_plan = DsWriteContractTests.write_plan
    fake_generator = DsWriteContractTests.fake_generator
    write_candidate = DsWriteContractTests.write_candidate
    run_generate = DsWriteContractTests.run_generate
    read_receipt = DsWriteContractTests.read_receipt
    assertInvalidArg = DsWriteContractTests.assertInvalidArg

    def test_relative_task_path_workspace_task_included_in_inputs(self):
        task = self.ws / "task.txt"
        task.write_text("Fix the function", encoding="utf-8")

        def writer(n, candidate):
            self.write_candidate(candidate, "NEW = 2\n")

        gen, calls = self.fake_generator(writer)
        result = self.run_generate(
            self.ns(task="task.txt", check_plan=self.write_plan("import mod; assert mod.NEW == 2")), gen)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(result["status"], "verified_pending_review")
        receipt = self.read_receipt(result)
        self.assertEqual(receipt["task_sha"], d._task_sha(task))
        self.assertIn("mod.py", receipt["baseline"])

    def test_missing_explicit_input_rejected_before_gen(self):
        gen = mock.Mock()
        self.write_task()
        ns = self.ns(inputs=["missing.txt"], task=self.base / "task.txt")
        self.assertInvalidArg(ns, gen)
        gen.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_mismatched_plan_workspace_rejected(self):
        gen = mock.Mock()
        task = self.write_task()
        other = self.base / "otherws"
        other.mkdir(parents=True, exist_ok=True)
        plan = self.write_plan("import mod; assert mod.NEW == 2", ws=other)
        ns = self.ns(task=task, check_plan=plan)
        self.assertInvalidArg(ns, gen)
        gen.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_timeout_zero_with_check_plan_rejected_before_gen(self):
        gen = mock.Mock()
        task = self.write_task()
        plan = self.write_plan("import mod; assert mod.NEW == 2", timeout_sec=0)
        ns = self.ns(task=task, check_plan=plan)
        self.assertInvalidArg(ns, gen)
        gen.assert_not_called()
        self.assertFalse(self.out.exists())

    def test_newtarget_repaired_after_failed_check_has_no_backup(self):
        (self.ws / "mod.py").unlink()
        task = self.write_task()
        code = "from pathlib import Path; ns={}; exec(Path('mod.py').read_text(),ns); assert ns['NEW'] == 7"

        def writer(n, candidate):
            if n == 1:
                self.write_candidate(candidate, "NEW = 1\n")
            else:
                self.write_candidate(candidate, "NEW = 7\n")

        gen, calls = self.fake_generator(writer)
        plan = self.write_plan(code)
        result = self.run_generate(
            self.ns(task=task, check_plan=plan, repair_rounds=1), gen)
        self.assertEqual(calls["n"], 2)
        self.assertFalse((self.out / "original.py").exists(),
                         "new target must have no original.py backup")
        self.assertEqual(result["status"], "verified_pending_review")
        receipt = self.read_receipt(result)
        self.assertIsNone(receipt["baseline"].get("mod.py"))
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-c",
             "from pathlib import Path; ns={}; exec(Path('mod.py').read_text(),ns); print(ns['NEW'])"],
            cwd=str(self.ws), capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "7")

    def test_cli_main_dryrun_stdout_json_noout_gen(self):
        import io
        import contextlib
        gen = mock.Mock()
        argv = [
            "--workspace", str(self.ws),
            "--target", "mod.py",
            "--task", str(self.base / "task.txt"),
            "--out", str(self.out),
            "--dry-run",
        ]
        buf = io.StringIO()
        with mock.patch.object(d, "_generate", gen, create=True), contextlib.redirect_stdout(buf):
            rc = d.main(argv)
        gen.assert_not_called()
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["status"], "dry_run")
        self.assertIsNone(payload["receipt"])
        self.assertTrue(payload["pending"])
        self.assertEqual(payload["target"], "mod.py")
        self.assertFalse(self.out.exists())
"""Local subprocess regressions; no provider, credentials or network access."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_under_test', ROOT/'bin/process_runner.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def event(kind='item.completed', key='one', **fields):
    return json.dumps(dict(type=kind, item=dict(id=key, type='command_execution', **fields)))


def alive(pid):
    if os.name == 'nt':
        from ctypes import wintypes
        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(api.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    try:
        status = Path('/proc')/str(pid)/'stat'
        if status.exists() and status.read_text().split()[2] == 'Z':
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def request(self, code, **overrides):
        source = self.path/'stdin'
        source.write_text('')
        return dict(exe=sys.executable, args=['-I', '-c', code], stdin=str(source),
                    stdout=str(self.path/'stdout'), stderr=str(self.path/'stderr'), timeout=4, **overrides)

    def execute(self, code, **overrides):
        result = runner.run(self.request(code, **overrides))
        state = json.loads((self.path/'stdout.monitor.json').read_text())
        return result, state

    def test_last_allowed_command_can_finish(self):
        started = event('item.started')
        completed = event(exit_code=0)
        code = f'import time;print({started!r},flush=True);time.sleep(.2);print({completed!r},flush=True)'
        result, state = self.execute(code, max_commands=1)
        self.assertEqual(result, 0)
        self.assertEqual(state['commands'], 1)
        self.assertEqual(state['active_commands'], 0)
        self.assertEqual(state['reason'], 'completed')

    def test_command_overrun_stops(self):
        started = event('item.started')
        other = event('item.started', key='two')
        result, state = self.execute(f'import time;print({started!r},flush=True);print({other!r},flush=True);time.sleep(20)', max_commands=1)
        self.assertEqual(result, 125)
        self.assertEqual(state['reason'], 'command_budget')
        self.assertTrue(state['cleanup']['process_stopped'])

    def test_idle_without_tools_stops(self):
        result, state = self.execute('import time;time.sleep(20)', idle_sec=.3)
        self.assertEqual(result, 125)
        self.assertEqual(state['reason'], 'idle_budget')
        self.assertLess(state['elapsed_sec'], 3)

    def test_reasoning_does_not_reset_idle_budget(self):
        text = json.dumps(dict(type='item.completed', item=dict(type='agent_message', text='still working')))
        result, state = self.execute(f'import time\nfor i in range(20):\n print({text!r},flush=True)\n time.sleep(.05)', idle_sec=.3)
        self.assertEqual(result, 125)
        self.assertEqual(state['reason'], 'idle_budget')

    def test_active_command_ignores_idle_until_implementation_deadline(self):
        started, completed = event('item.started'), event(exit_code=0)
        code = f'import time;print({started!r},flush=True);time.sleep(.8);print({completed!r},flush=True)'
        result, state = self.execute(code, idle_sec=.3, max_commands=1)
        self.assertEqual(result, 0)
        self.assertGreater(state['elapsed_sec'], .7)

    def test_active_command_still_has_wall_clock_deadline(self):
        request = self.request(f'import time;print({event("item.started")!r},flush=True);time.sleep(20)', idle_sec=.3)
        request['timeout'] = 1
        self.assertEqual(runner.run(request), 124)

    def test_final_event_without_newline(self):
        result, state = self.execute(f'print({event(exit_code=0)!r},end="",flush=True)', max_commands=1)
        self.assertEqual(result, 0)
        self.assertEqual(state['commands'], 1)

    def test_file_hash_evidence_does_not_extend_idle(self):
        target = self.path/'output.txt'
        code = f'import pathlib,time\np=pathlib.Path({str(target)!r})\nfor i in range(20):\n p.write_text(str(i))\n time.sleep(.05)'
        result, state = self.execute(code, watch_paths=[str(target)], idle_sec=.3)
        self.assertEqual(result, 125)
        self.assertEqual(state['reason'], 'idle_budget')
        self.assertTrue(state['watched_files'][0]['observed_change'])
        self.assertIsNotNone(state['watched_files'][0]['sha256'])

    def test_monitor_error_cleans_live_process(self):
        marker = self.path/'started'
        request = self.request(f'import pathlib,time;pathlib.Path({str(marker)!r}).write_text("started");time.sleep(20)')
        real_popen = subprocess.Popen
        launched = []
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            if args[0][0] == sys.executable:
                launched.append(process)
            return process
        def failed_write(*args):
            until = time.monotonic()+2
            while not marker.exists() and time.monotonic() < until:
                time.sleep(.01)
            raise OSError('simulated monitor disk failure')
        with patch.object(runner.subprocess, 'Popen', side_effect=launch), patch.object(runner, 'write_checkpoint', side_effect=failed_write):
            self.assertEqual(runner.run(request), 126)
        self.assertTrue(marker.exists())
        self.assertIsNotNone(launched[0].poll())
        self.assertIn('supervisor_error', (self.path/'stderr').read_text())

    def test_timeout_cleans_descendant_process(self):
        child_pid = self.path/'child.pid'
        code = f'import subprocess,sys,pathlib,time\np=subprocess.Popen([sys.executable,"-I","-c","import time;time.sleep(20)"])\npathlib.Path({str(child_pid)!r}).write_text(str(p.pid))\ntime.sleep(20)'
        request = self.request(code)
        request['timeout'] = 1
        self.assertEqual(runner.run(request), 124)
        self.assertTrue(child_pid.exists())
        pid = int(child_pid.read_text())
        until = time.monotonic()+2
        while alive(pid) and time.monotonic() < until:
            time.sleep(.05)
        try:
            self.assertFalse(alive(pid), 'descendant survived cleanup')
        finally:
            if alive(pid):
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(pid), '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    os.kill(pid, signal.SIGKILL)

    def test_null_command_output_is_safe(self):
        activity = runner.Activity()
        activity.consume(event(exit_code=1, aggregated_output=None))
        self.assertEqual(len(activity.commands), 1)
        self.assertEqual(len(activity.patch_errors), 0)

    def test_windows_cleanup_failure_is_visible_and_falls_back(self):
        class LiveProcess:
            pid = 12345
            code = None
            def poll(self):
                return self.code
            def kill(self):
                self.code = -9
            def wait(self, timeout):
                return self.code
        process = LiveProcess()
        with patch.object(runner.os, 'name', 'nt'), patch.object(runner.subprocess, 'CREATE_NO_WINDOW', 0, create=True), patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stderr=b'access denied', stdout=b'')):
            cleanup = runner.stop(process)
            repeated = runner.stop(process)
        self.assertTrue(cleanup['process_stopped'])
        self.assertIn('taskkill failed', cleanup['warnings'][0])
        self.assertEqual(repeated['warnings'], [])

    def test_chunks_and_missing_final_newline(self):
        code = 'import json;print(json.dumps(dict(type="item.completed",item=dict(type="command_execution",id="one",exit_code=0,aggregated_output="x"*200000))),end="",flush=True)'
        result, state = self.execute(code, max_commands=1)
        self.assertEqual(result, 0)
        self.assertEqual(state['commands'], 1)
        self.assertEqual(state['malformed_lines'], 0)


if __name__ == '__main__':
    unittest.main()

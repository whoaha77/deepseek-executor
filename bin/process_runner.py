"""Supervise a fixed argv; record evidence without extending its deadline."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


class Activity:
    TOOL_TYPES = {'command_execution', 'file_change', 'mcp_tool_call', 'web_search',
                  'collab_tool_call', 'tool_call'}

    def __init__(self):
        self.commands = set()
        self.active_commands = set()
        self.patch_errors = set()
        self.malformed = 0
        self.tool_events = 0

    def consume(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            self.malformed += 1
            return
        if not isinstance(event, dict):
            self.malformed += 1
            return
        item = event.get('item', {})
        if not isinstance(item, dict):
            return
        item_type = item.get('type')
        if isinstance(item_type, str) and item_type in self.TOOL_TYPES:
            self.tool_events += 1
        if item.get('type') != 'command_execution':
            return
        key = item.get('id')
        if not isinstance(key, (str, int)):
            return
        self.commands.add(key)
        if event.get('type') == 'item.completed':
            self.active_commands.discard(key)
        elif event.get('type') == 'item.started':
            self.active_commands.add(key)
        output = item.get('aggregated_output', '')
        if not isinstance(output, str):
            output = ''
        if (event.get('type') == 'item.completed' and item.get('exit_code') not in (None, 0)
                and any(s in output for s in ('Invalid patch:', 'requires a UTF-8 PATCH argument'))):
            self.patch_errors.add(key)


class WatchedFiles:
    """Best-effort file evidence, not a write sandbox or a reason to add time."""
    def __init__(self, paths):
        self.paths = [Path(p).resolve() for p in paths]
        self.baseline = {}
        self.observed = set()
        self.rows = []
        self.refresh(initial=True)

    def refresh(self, initial=False):
        rows = []
        for path in self.paths:
            row = {'path': str(path), 'exists': path.exists(), 'sha256': None}
            try:
                if path.is_file():
                    digest = hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(65536), b''):
                            digest.update(chunk)
                    row['sha256'] = digest.hexdigest()
                elif row['exists']:
                    row['error'] = 'watch path is not a regular file'
            except OSError as error:
                row['error'] = str(error)
            signature = (row['exists'], row['sha256'], row.get('error'))
            if initial:
                self.baseline[str(path)] = signature
            changed = signature != self.baseline.get(str(path))
            if changed:
                self.observed.add(str(path))
            row.update(changed=changed, observed_change=str(path) in self.observed)
            rows.append(row)
        self.rows = rows
        return rows


def stop(process):
    """Idempotent best-effort tree cleanup; failures are returned for audit."""
    warnings = []
    if os.name == 'nt':
        if process.poll() is None:
            try:
                result = subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
                if result.returncode:
                    detail = (result.stderr or result.stdout or b'').decode('utf-8', errors='replace')[:500]
                    warnings.append('taskkill failed: exit=' + str(result.returncode) + ' ' + detail)
            except (OSError, subprocess.SubprocessError) as error:
                warnings.append('taskkill failed: ' + str(error))
    else:
        try:
            # Popen owns a new session, so this also reaches surviving descendants.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            warnings.append('process group cleanup failed: ' + str(error))
    if process.poll() is None:
        try:
            process.kill()
        except (OSError, subprocess.SubprocessError) as error:
            warnings.append('process kill failed: ' + str(error))
    try:
        process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError) as error:
        warnings.append('process wait failed: ' + str(error))
    return {'process_stopped': process.poll() is not None, 'warnings': warnings}


def write_checkpoint(path, state):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(state), encoding='utf-8')
    os.replace(temporary, path)


def read_events(trace, pending, activity, final=False):
    # Bound each live poll, while draining completed output in fixed-size chunks.
    for _ in range(64 if not final else 2**31):
        chunk = trace.read(65536)
        if not chunk:
            break
        pending += chunk
        lines = pending.split(b'\n')
        pending = lines.pop()
        for line in lines:
            if line.strip():
                activity.consume(line)
    if final and pending.strip():
        activity.consume(pending)
        pending = b''
    return pending


def run(request):
    timeout = int(request['timeout'])
    if timeout < 1 or timeout > 3600:
        raise ValueError('timeout must be between 1 and 3600 seconds')
    max_commands = request.get('max_commands', 0)
    max_patch_errors = request.get('max_patch_errors', 0)
    if any(type(v) is not int or not 0 <= v <= 1000 for v in (max_commands, max_patch_errors)):
        raise ValueError('activity limits must be integers in 0..1000; zero disables')
    idle_sec = request.get('idle_sec', 0)
    if type(idle_sec) not in (int, float) or not 0 <= idle_sec <= 3600:
        raise ValueError('idle_sec must be a number in 0..3600; zero disables')
    watch_paths = request.get('watch_paths', [])
    if not isinstance(watch_paths, list) or any(not isinstance(p, str) or not p for p in watch_paths):
        raise ValueError('watch_paths must be explicit file paths')
    watched = WatchedFiles(watch_paths)
    activity = Activity()
    reason, code = 'completed', None
    checkpoint = Path(request.get('monitor', request['stdout'] + '.monitor.json'))
    started = time.monotonic()
    last_tool_event = started
    last_write = started - 1
    fingerprint = None
    state = {}
    process = None
    with open(request['stdin'], 'rb') as src, open(request['stdout'], 'wb') as out, open(request['stderr'], 'wb') as err:
        try:
            process = subprocess.Popen([request['exe'], *request['args']], stdin=src, stdout=out, stderr=err,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                                       start_new_session=os.name != 'nt')
            pending = b''
            with open(request['stdout'], 'rb') as trace:
                while True:
                    previous_events = activity.tool_events
                    pending = read_events(trace, pending, activity)
                    code = process.poll()
                    if code is not None:
                        pending = read_events(trace, pending, activity, final=True)
                    now = time.monotonic()
                    if activity.tool_events != previous_events:
                        last_tool_event = now
                    if max_patch_errors and len(activity.patch_errors) >= max_patch_errors:
                        reason, code = 'patch_error_budget', 125
                    elif max_commands and len(activity.commands) > max_commands:
                        reason, code = 'command_budget', 125
                    elif code is None and now - started >= timeout:
                        reason, code = 'wall_clock_timeout', 124
                    elif code is None and idle_sec and not activity.active_commands and now-last_tool_event >= idle_sec:
                        reason, code = 'idle_budget', 125
                    current = (reason, code, len(activity.commands), len(activity.patch_errors),
                               len(activity.active_commands), activity.tool_events, activity.malformed)
                    if now-last_write >= 0.5 or current != fingerprint:
                        state = dict(reason=reason, commands=len(activity.commands), patch_errors=len(activity.patch_errors),
                                     active_commands=len(activity.active_commands), tool_events=activity.tool_events,
                                     idle_elapsed_sec=0 if activity.active_commands else round(now-last_tool_event, 2),
                                     malformed_lines=activity.malformed, elapsed_sec=round(now-started, 2),
                                     status='running' if code is None else 'stopped', exit_code=code,
                                     limits=dict(commands=max_commands, patch_errors=max_patch_errors, timeout_sec=timeout,
                                                 idle_sec=idle_sec), watched_files=watched.refresh(),
                                     main_agent_acceptance='pending')
                        write_checkpoint(checkpoint, state)
                        last_write, fingerprint = now, current
                    if code is not None:
                        break
                    time.sleep(0.05)
        except (Exception, KeyboardInterrupt) as error:
            reason, code = 'supervisor_error', 126
            state['supervisor_error'] = str(error)
            err.write(('\n[runner] supervisor_error: ' + str(error) + '\n').encode('utf-8', errors='replace'))
            err.flush()
        finally:
            cleanup = stop(process) if process is not None else {'process_stopped': True, 'warnings': []}
            for warning in cleanup['warnings']:
                err.write(('\n[runner] cleanup_warning: ' + warning + '\n').encode('utf-8', errors='replace'))
            state.update(reason=reason, exit_code=code, status='stopped', cleanup=cleanup,
                         elapsed_sec=round(time.monotonic()-started, 2), watched_files=watched.refresh(),
                         main_agent_acceptance='pending')
            try:
                write_checkpoint(checkpoint, state)
            except Exception as error:
                reason, code = 'supervisor_error', 126
                err.write(('\n[runner] supervisor_error: final monitor write failed: ' + str(error) + '\n').encode('utf-8', errors='replace'))
            if reason != 'completed':
                err.write(f'\n[runner] {reason}; partial files require inspection; not success.\n'.encode())
            err.flush()
    return code


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    options = parser.parse_args()
    sys.exit(run(json.loads(Path(options.request).read_text(encoding='utf-8-sig'))))

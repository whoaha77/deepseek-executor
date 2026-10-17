"""Opt-in quota monitoring and isolated official DSH task relay; no original writes."""
import argparse
import contextlib
import hashlib
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

_BIN = os.path.dirname(os.path.abspath(__file__))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from pathlib import Path  # noqa: E402
import uuid  # noqa: E402

import dsh_bridge  # noqa: E402

ROOT = os.path.dirname(_BIN)
DEFAULT_STATE = os.path.join(ROOT, "state", "auto-relay")
DSH_STATE = Path(ROOT) / "state" / "dsh"
JOB_RE = re.compile(r"^job-[0-9a-f]{32}$")
ROLLOUT_LIMIT = 128 * 1024
ROLLOUT_LINES = 200
ROLLOUT_FRESH = 180
EXHAUST_CONFIRM = 5.0
IDLE_CONFIRM = 10.0
LINE_LIMIT = 1024 * 1024
RATE_REACHED = {"usagelimit", "ratelimit", "workspacelimit", "creditsexhausted",
                "weekly", "hourly"}
VERSION = "0.3.0"


class RelayError(Exception):
    def __init__(self, kind, msg=""):
        super().__init__(msg or kind)
        self.kind = kind


def _sha_text(text):
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _real(p):
    return os.path.realpath(os.path.abspath(p))


def _within(child, root):
    c, r = _real(child), _real(root)
    if c == r:
        return True
    return c.startswith(r + os.sep)


def _protected(path):
    roots = getattr(dsh_bridge, "PROTECTED_ROOTS", None) or []
    for r in roots:
        try:
            if _within(path, r):
                return True
        except OSError:
            return True
    fn = getattr(dsh_bridge, "_is_within", None)
    if callable(fn):
        for r in roots:
            try:
                if fn(path, r):
                    return True
            except Exception:
                return True
    return False


class CodexRPC:
    def __init__(self, timeout=20.0):
        self.timeout = timeout
        self.proc = None
        self._id = 0
        self._pending = {}
        self._lock = threading.Lock()
        self._reader = None

    def __enter__(self):
        exe = shutil.which("codex") or "codex"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self.proc = subprocess.Popen(
                [exe, "app-server", "--stdio"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=flags,
            )
            self._reader = threading.Thread(target=self._read_loop, args=(self.proc,),
                                            daemon=True)
            self._reader.start()
            self._request("initialize", {
                "clientInfo": {"name": "deepseek_auto_relay", "version": VERSION},
                "capabilities": {"experimentalApi": True},
            })
            self._notify("initialized", {})
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _read_loop(self, proc):
        try:
            stdout = proc.stdout
            if stdout is None:
                return
            while True:
                line = stdout.readline(LINE_LIMIT)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, TypeError):
                    continue
                if not isinstance(obj, dict):
                    continue
                mid = obj.get("id")
                if mid is not None and ("result" in obj or "error" in obj):
                    with self._lock:
                        box = self._pending.get(mid)
                    if box is not None:
                        try:
                            box.put_nowait(obj)
                        except queue.Full:
                            pass
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                for box in list(self._pending.values()):
                    try:
                        box.put_nowait(None)
                    except queue.Full:
                        pass

    def _send(self, obj):
        p = self.proc
        if p is None or p.stdin is None:
            raise RelayError("rpc_closed")
        p.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        p.stdin.flush()

    def _notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method, params):
        with self._lock:
            self._id += 1
            mid = self._id
            box = queue.Queue(maxsize=1)
            self._pending[mid] = box
        try:
            self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
            try:
                resp = box.get(timeout=self.timeout)
            except queue.Empty:
                raise RelayError("rpc_timeout")
            if resp is None:
                raise RelayError("rpc_closed")
            if "error" in resp:
                raise RelayError("rpc_error")
            return resp.get("result")
        finally:
            with self._lock:
                self._pending.pop(mid, None)

    def rate_limits(self):
        return self._request("account/rateLimits/read", {})

    def close(self):
        p = self.proc
        self.proc = None
        if p is None:
            return
        try:
            p.terminate()
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
                p.wait(timeout=5)
            except OSError:
                pass
        try:
            if p.stdin:
                p.stdin.close()
        except OSError:
            pass
        try:
            if p.stdout:
                p.stdout.close()
        except OSError:
            pass


def probe_quota(bucket="codex"):
    try:
        with CodexRPC() as rpc:
            return rpc.rate_limits()
    except Exception:
        return None


def _camel(name):
    parts = re.split(r"[_\-]+", str(name))
    if not parts:
        return str(name)
    return parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


def _camel_deep(obj):
    if isinstance(obj, dict):
        return {_camel(k): _camel_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camel_deep(v) for v in obj]
    return obj


def _norm_rate_limits(raw):
    if not isinstance(raw, dict):
        return None
    if "rateLimitsByLimitId" in raw or "rateLimits" in raw:
        out = {}
        for key in ("rateLimitsByLimitId", "rateLimits"):
            if key in raw:
                val = raw[key]
                if key == "rateLimitsByLimitId":
                    if not isinstance(val, dict):
                        return None
                    out[key] = {str(k): _camel_deep(v) if isinstance(v, dict) else v
                                for k, v in val.items()}
                else:
                    if not isinstance(val, dict):
                        return None
                    out[key] = _camel_deep(val)
        return out
    return {"rateLimits": _camel_deep(raw)}


def _finite_percent(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    if not math.isfinite(f) or f < 0 or f > 100:
        return None
    return f


def _resets_ok(reset, now, require_fresh):
    if reset is None:
        return True
    if isinstance(reset, bool) or not isinstance(reset, (int, float)):
        return False
    f = float(reset)
    if not math.isfinite(f):
        return False
    if require_fresh and f <= now:
        return False
    return True


def _classify_limits(limits, now):
    if not isinstance(limits, dict):
        return "unknown"
    reached = limits.get("rateLimitReachedType")
    if reached is not None:
        if not isinstance(reached, str) or not reached.strip():
            return "unknown"
        if reached.strip().lower() not in RATE_REACHED:
            return "unknown"
        primary = limits.get("primary")
        if not isinstance(primary, dict):
            return "unknown"
        if _finite_percent(primary.get("usedPercent")) is None:
            return "unknown"
        for part in (primary, limits.get("secondary")):
            if not isinstance(part, dict):
                continue
            if not _resets_ok(part.get("resetsAt"), now, True):
                return "unknown"
        return "exhausted"
    primary = limits.get("primary")
    if not isinstance(primary, dict):
        return "unknown"
    pv = _finite_percent(primary.get("usedPercent"))
    if pv is None:
        return "unknown"
    if not _resets_ok(primary.get("resetsAt"), now, True):
        return "unknown"
    secondary = limits.get("secondary")
    sv = None
    if secondary is not None:
        if not isinstance(secondary, dict):
            return "unknown"
        sv = _finite_percent(secondary.get("usedPercent"))
        if sv is None:
            return "unknown"
        if not _resets_ok(secondary.get("resetsAt"), now, True):
            return "unknown"
    if pv >= 100 or (sv is not None and sv >= 100):
        return "exhausted"
    return "available"


def classify_quota(result, bucket, now):
    if not isinstance(result, dict):
        return "unknown"
    by_id = result.get("rateLimitsByLimitId")
    if "rateLimitsByLimitId" in result:
        if not isinstance(by_id, dict):
            return "unknown"
        named = by_id.get(bucket)
        if not isinstance(named, dict):
            return "unknown"
        lid = named.get("limitId")
        if lid is not None and lid != bucket:
            return "unknown"
        return _classify_limits(named, now)
    legacy = result.get("rateLimits")
    if not isinstance(legacy, dict):
        return "unknown"
    lid = legacy.get("limitId")
    if lid != bucket:
        return "unknown"
    return _classify_limits(legacy, now)


def _parse_ts(ts):
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        f = float(ts)
        return f if math.isfinite(f) else None
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip().replace("Z", "+00:00")
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.timestamp()


def rollout_quota(path, bucket, armed_at, now):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > ROLLOUT_LIMIT:
                f.seek(size - ROLLOUT_LIMIT)
            raw = f.read(ROLLOUT_LIMIT)
    except OSError:
        return "unknown"
    lines = raw.decode("utf-8", "replace").splitlines()
    lines = lines[-ROLLOUT_LINES:]
    best = None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") != "event_msg":
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "token_count":
            continue
        tsv = _parse_ts(obj.get("timestamp"))
        if tsv is None:
            continue
        if tsv > now + 10 or tsv < armed_at or (now - tsv) > ROLLOUT_FRESH:
            continue
        raw_rl = payload.get("rate_limits")
        if raw_rl is None:
            raw_rl = payload.get("rateLimits")
        norm = _norm_rate_limits(raw_rl)
        if not norm:
            continue
        if best is None or tsv >= best[0]:
            best = (tsv, norm)
    if best is None:
        return "unknown"
    return classify_quota(best[1], bucket, now)


API = dsh_bridge.API
handoff = dsh_bridge.handoff
return_packet = dsh_bridge.return_packet
TERMINAL = {'return_ready', 'needs_attention', 'stopped'}


def _safe_directory(path):
    path = Path(path).resolve()
    if _protected(path):
        raise RelayError('protected_state')
    return path


@contextlib.contextmanager
def file_lock(path):
    """Cooperative process lock, always byte zero on Windows."""
    f = Path(path).open('a+b')
    acquired = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(f.fileno()).st_size == 0:
                f.write(b'\0'); f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def job_path(state, job_id):
    if not isinstance(job_id, str) or not JOB_RE.fullmatch(job_id):
        raise RelayError('invalid_job')
    root = _safe_directory(state or DEFAULT_STATE)
    jd = (root/'jobs'/job_id).resolve()
    if not dsh_bridge._is_within(jd, root):
        raise RelayError('job_escape')
    return jd


def load_job(jobdir):
    jd = _safe_directory(jobdir)
    if not JOB_RE.fullmatch(jd.name):
        raise RelayError('invalid_job')
    j = json.loads((jd/'job.json').read_text(encoding='utf-8'))
    if j['job_id'] != jd.name or not JOB_RE.fullmatch(jd.name):
        raise RelayError('invalid_job')
    if str(uuid.UUID(j['thread_id'])) != j['thread_id']:
        raise RelayError('invalid_thread')
    if Path(j['workspace']).resolve() != (jd/'workspace').resolve():
        raise RelayError('invalid_snapshot')
    if Path(j['task_path']).resolve() != (jd/'task.md').resolve():
        raise RelayError('invalid_task_path')
    if set(j['filenames']) != set(j['baseline']):
        raise RelayError('invalid_baseline')
    return j


def save_job(jd, j):
    j['revision'] = j.get('revision', 0) + 1
    dsh_bridge.write_json(Path(jd)/'job.json', j, overwrite=True)


def _status(j):
    keys = ('job_id', 'phase', 'handoff_id', 'session_id', 'return_md',
            'original_changed', 'attention', 'resume_state', 'daemon_pid',
            'quota', 'test_signals', 'main_agent_acceptance', 'notification_path')
    return {k:j.get(k) for k in keys}


def _task(j):
    text = dsh_bridge.read_task(j['task_path'])
    if _sha_text(text) != j['task_sha']:
        raise RelayError('task_changed')
    return text


def arm(state=None, workspace=None, task=None, files=None, thread_id=None,
        rollout=None, bucket='codex', auto_resume=False, test_signals=False):
    if str(uuid.UUID(thread_id)) != thread_id:
        raise RelayError('invalid_thread')
    src = dsh_bridge.validate_workspace(workspace)
    root = _safe_directory(state or DEFAULT_STATE)
    if dsh_bridge._is_within(src, root):
        raise RelayError('source_inside_state')
    text = dsh_bridge.read_task(task)
    if not text.strip():
        raise RelayError('empty_task')
    try:
        baseline = dsh_bridge.fingerprint(src, files)
    except dsh_bridge.BridgeError:
        raise RelayError('invalid_declared_files') from None
    contents = {}
    for name, digest in baseline.items():
        p = src/name
        if dsh_bridge._is_within(p.resolve(), root):
            raise RelayError('recursive_snapshot')
        if digest is not None:
            with p.open('rb') as f:
                data = f.read(dsh_bridge.MAX_FILE_BYTES+1)
            if len(data) > dsh_bridge.MAX_FILE_BYTES:
                raise RelayError('snapshot_input_too_large')
            dsh_bridge._scan_secret(data.decode('utf-8'))
            if hashlib.sha256(data).hexdigest() != digest:
                raise RelayError('source_changed_during_snapshot')
            contents[name] = data
    if rollout is not None:
        rp = Path(rollout).resolve()
        if not rp.is_file() or rp.suffix.lower() != '.jsonl':
            raise RelayError('invalid_rollout')
        # Bind an explicitly selected rollout to the intended thread, without history export.
        with rp.open('rb') as f:
            first = json.loads(f.readline(65537))
        if first.get('type') != 'session_meta' or first.get('payload',{}).get('id') != thread_id:
            raise RelayError('rollout_thread_mismatch')
        rollout = str(rp)
    job_id = 'job-'+uuid.uuid4().hex
    jd = job_path(root, job_id)
    snap = jd/'workspace'; snap.mkdir(parents=True, exist_ok=False)
    for name, data in contents.items():
        p = snap/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    if dsh_bridge.fingerprint(snap, list(baseline)) != baseline:
        raise RelayError('snapshot_mismatch')
    taskpath = jd/'task.md'; taskpath.write_text(text, encoding='utf-8')
    j = dict(job_id=job_id, phase='armed', revision=0, source_root=str(src),
             workspace=str(snap), task_path=str(taskpath), task_sha=_sha_text(text),
             filenames=list(baseline), baseline=baseline, thread_id=thread_id,
             armed_at=time.time(), bucket=bucket, rollout=rollout,
             auto_resume=bool(auto_resume), test_signals=bool(test_signals),
             main_agent_acceptance='pending', first_exhausted_at=None,
             idle_since=None, resume_attempted=False)
    save_job(jd, j)
    return dict(job_id=job_id, job_dir=str(jd), workspace=str(snap), phase='armed')


def get_idle(api, sid):
    items = api.rpc('session/list', {'_request':{}})['items']
    row = next((x for x in items if x.get('sessionId') == sid), None)
    if row is None or row.get('running') is not False:
        return False
    inbox = api.rpc('session/projections', {'request':{'sessionId':sid}})['values']['inbox']
    if not isinstance(inbox, dict):
        raise RelayError('invalid_inbox')
    for k in ('next-turn', 'next-step'):
        if k in inbox and not isinstance(inbox[k], list):
            raise RelayError('invalid_inbox')
        if inbox.get(k):
            return False
    return True


def _quota(j, jd, now, signal, reader):
    if signal is not None and not j['test_signals']:
        raise RelayError('test_signal_refused')
    if j['test_signals']:
        p = Path(jd)/'test-signal.json'
        s = signal if signal is not None else (json.loads(p.read_text())['signal'] if p.exists() else 'unknown')
        if s not in ('exhausted', 'available', 'unknown'):
            raise RelayError('invalid_test_signal')
        return s
    result = (reader or probe_quota)(j['bucket'])
    state = classify_quota(result, j['bucket'], now)
    if state == 'unknown' and j.get('rollout'):
        state = rollout_quota(j['rollout'], j['bucket'], j['armed_at'], now)
    return state


def _attention(jd, j, reason):
    j.update(phase='needs_attention', attention=reason)
    save_job(jd, j)
    return _status(j)


def _resume(jd, j):
    """One attempt only: a queue acceptance is not proof that the UI resumed."""
    if not j['auto_resume'] or j['resume_attempted']:
        return
    j['resume_attempted'] = True; j['resume_state'] = 'attempting'; save_job(jd,j)
    try:
        r = subprocess.run([shutil.which('codex') or 'codex', 'queue', '--thread', j['thread_id'],
                            '--message', 'DSH隔离接力已交回，请先验收，不直接覆盖原文件。报告：'+j['return_md']+
                            '；自动接力清单：'+str(Path(jd)/'job.json')],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        j['resume_state'] = 'queue_accepted' if r.returncode == 0 else 'failed_or_unknown'
    except Exception:
        j['resume_state'] = 'failed_or_unknown'
    save_job(jd,j)


def step(jobdir, signal=None, now=None, quota_reader=None, bridge_api=None):
    jd = _safe_directory(jobdir)
    with file_lock(jd/'tick.lock'):
        j = load_job(jd); phase = j['phase']; now = time.time() if now is None else now
        if signal is not None and not j['test_signals']:
            raise RelayError('test_signal_refused')
        if phase == 'return_ready' and j['auto_resume'] and j.get('resume_state') not in ('queue_accepted','failed_or_unknown'):
            return _attention(jd,j,'resume_interrupted_before_confirmation')
        if phase in TERMINAL:
            return _status(j)
        if j.get('stop_requested'):
            j.update(phase='stopped', dsh_may_still_run=bool(j.get('session_id'))); save_job(jd,j)
            return _status(j)
        if phase in ('dispatching', 'submit_pending'):
            return _attention(jd,j,'ambiguous_remote_commit')
        try:
            # Every tick records a fresh signal. A failed query is never exhaustion.
            j['quota'] = _quota(j,jd,now,signal,quota_reader)
            if phase == 'armed':
                if j['quota'] != 'exhausted':
                    j['first_exhausted_at'] = None
                elif j['first_exhausted_at'] is None:
                    j['first_exhausted_at'] = now
                elif now-j['first_exhausted_at'] >= EXHAUST_CONFIRM:
                    _task(j)
                    if dsh_bridge.fingerprint(j['workspace'],j['filenames']) != j['baseline']:
                        return _attention(jd,j,'snapshot_changed_before_dispatch')
                    j.update(phase='dispatching', request_id=str(uuid.uuid4())); save_job(jd,j)
                    try:
                        h = handoff(DSH_STATE,j['workspace'],j['task_path'],j['filenames'],
                                    '自动接力：'+j['job_id'])
                        if not h['session_id'].startswith('codex-relay-') or not h['handoff_id'].startswith('handoff-'):
                            raise RelayError('invalid_handoff')
                    except Exception:
                        return _attention(jd,j,'handoff_failed_or_unknown')
                    j.update(phase='dsh_ack',session_id=h['session_id'],handoff_id=h['handoff_id'])
            elif phase in ('dsh_ack', 'dsh_active'):
                api = bridge_api or API(DSH_STATE)
                if not get_idle(api,j['session_id']):
                    j['idle_since'] = None
                elif phase == 'dsh_ack':
                    ack = return_packet(DSH_STATE,j['handoff_id'])
                    packet = json.loads(Path(ack['json_path']).read_text(encoding='utf-8'))
                    if not any(m.get('role') == 'assistant' and m.get('text','').strip() for m in packet['messages']):
                        save_job(jd,j); return _status(j)
                    text = _task(j)
                    prompt = ('现在执行已预授权的接力任务。仅在当前隔离副本中操作，范围为：'+
                              ', '.join(j['filenames'])+'。禁止访问或修改原目录、父目录、密钥、私人会话及科研原始数据。'+
                              '可为当前任务搜索公开资料、访问GitHub并下载必要源码/依赖至当前副本；明确离线任务仍离线。'
                              '必要临时材料留副本内，交付与追踪范围仍为声明文件。下载不自动授权安装或执行未知程序；'
                              '不提权或自行发布，按任务既有授权处理。此副本不是OS文件沙箱。'
                              '负责必要脚本、产物生成、本地检查与原范围内常规修复；实际渲染不可验证时明确说明。'
                              '不无限重试、续时或另派付费后继；完成后报告产物路径、验证及待裁决事项。\n'+text)
                    dsh_bridge._scan_secret(prompt)
                    j['phase'] = 'submit_pending'; save_job(jd,j)
                    try:
                        accepted = api.rpc('session/prompt', {'request':{
                            'sessionId':j['session_id'],'requestId':j['request_id'],'mode':'queue',
                            'content':[{'type':'text','text':prompt}],'clientTimeZone':'Asia/Shanghai'}})
                        if accepted.get('accepted') is not True:
                            raise RelayError('not_accepted')
                    except Exception:
                        return _attention(jd,j,'prompt_failed_or_unknown')
                    j.update(phase='dsh_active',idle_since=None)
                elif j.get('idle_since') is None:
                    j['idle_since'] = now
                elif now-j['idle_since'] >= IDLE_CONFIRM:
                    packet = return_packet(DSH_STATE,j['handoff_id'])
                    j.update(phase='dsh_ready',return_json=packet['json_path'],return_md=packet['md_path'])
            elif phase == 'dsh_ready':
                if j['quota'] == 'available':
                    current = dsh_bridge.fingerprint(j['source_root'],j['filenames'])
                    j['original_changed'] = [n for n in current if current[n] != j['baseline'][n]]
                    note = jd/'交回通知.md'
                    note.write_text('# DSH隔离接力结果待验收\n\n报告：'+j['return_md']+
                                    '\n\n原目录变化：'+json.dumps(j['original_changed'],ensure_ascii=False)+
                                    '\n\n未自动合并。DSH会话仍保留，导出是某时刻的快照，不撤销写权限。',encoding='utf-8')
                    j.update(phase='return_ready',notification_path=str(note)); save_job(jd,j)
                    _resume(jd,j)
                    if j.get('resume_state') in ('failed_or_unknown','attempting'):
                        return _attention(jd,j,'auto_resume_unavailable')
            else:
                return _attention(jd,j,'invalid_phase')
            j.pop('metadata_error',None)
            j['metadata_failures'] = 0
        except RelayError as e:
            return _attention(jd,j,e.kind)
        except Exception as e:
            # Remote writes are separately guarded above; read failures may safely wait.
            j['metadata_error'] = type(e).__name__
            j['metadata_failures'] = j.get('metadata_failures',0)+1
            if j['metadata_failures'] >= 3:
                return _attention(jd,j,'repeated_metadata_or_export_failure')
        save_job(jd,j)
        return _status(j)


def status(state=None, job=None):
    return _status(load_job(job_path(state,job)))


def stop(state=None, job=None):
    jd = job_path(state,job)
    with file_lock(jd/'tick.lock'):
        j = load_job(jd); j['stop_requested'] = True; save_job(jd,j)
    return step(jd)


def inject(state=None, job=None, signal=None):
    jd = job_path(state,job)
    with file_lock(jd/'tick.lock'):
        if not load_job(jd)['test_signals'] or signal not in ('exhausted','available','unknown'):
            raise RelayError('test_signal_refused')
        dsh_bridge.write_json(jd/'test-signal.json',dict(signal=signal),overwrite=True)
    return dict(job_id=job,test_signal=signal)


def run(state=None, job=None, interval=60, once=False):
    if not 5 <= interval <= 3600:
        raise RelayError('invalid_interval')
    jd = job_path(state,job)
    with file_lock(jd/'run.lock'):
        while True:
            result = step(jd)
            if once or result['phase'] in TERMINAL:
                return result
            # Wake quickly on stop; no model calls in this loop.
            for _ in range(interval):
                time.sleep(1)
                if load_job(jd).get('stop_requested'):
                    return step(jd)


def daemon(state=None, job=None, interval=60):
    jd = job_path(state,job); load_job(jd)
    cmd = [sys.executable,'-X','utf8','-I',str(Path(__file__).resolve()),
           '--state',str(Path(state or DEFAULT_STATE).resolve()),'run','--job',job,'--interval',str(interval)]
    flags = (subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    with (jd/'daemon.log').open('ab') as log:
        p = subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=log,stderr=log,
                             creationflags=flags,start_new_session=(os.name != 'nt'))
    with file_lock(jd/'tick.lock'):
        j = load_job(jd); j['daemon_pid'] = p.pid; save_job(jd,j)
    return dict(job_id=job,pid=p.pid,status='process_started')


def main(argv=None):
    p = argparse.ArgumentParser(description='Opt-in Codex quota relay into an isolated DSH workspace')
    p.add_argument('--state',default=DEFAULT_STATE)
    sub = p.add_subparsers(dest='command',required=True)
    a = sub.add_parser('arm')
    for name in ('workspace','task','thread-id'):
        a.add_argument('--'+name,required=True)
    a.add_argument('--files',nargs='+',required=True); a.add_argument('--rollout')
    a.add_argument('--bucket',default='codex')
    a.add_argument('--auto-resume',action='store_true'); a.add_argument('--test-signals',action='store_true')
    for name in ('run','daemon','status','stop','inject'):
        x = sub.add_parser(name); x.add_argument('--job',required=True)
        if name in ('run','daemon'): x.add_argument('--interval',type=int,default=60)
        if name == 'run': x.add_argument('--once',action='store_true')
        if name == 'inject': x.add_argument('--signal',choices=['exhausted','available','unknown'],required=True)
    a = vars(p.parse_args(argv)); command = a.pop('command')
    try:
        result = globals()[command](**a)
    except Exception as e:
        print(json.dumps(dict(error=type(e).__name__))); return 1
    print(json.dumps(result,ensure_ascii=False)); return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())



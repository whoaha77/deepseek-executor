#!/usr/bin/env python3
"""ask —— T1 层：直连 DeepSeek API 的纯文本通道（不经过 Codex 外壳）

为什么快又便宜（对比 delegate.ps1 走 codex exec）：
  - 没有 Codex 的 ~9k 系统提示与工具定义，没有 agent 往返
  - 一次 HTTP 调用（默认关思考）通常 2~8 秒；成本按 flash 峰谷价计（估价，见 price_cny）
  - 适合：改写/翻译/格式化/抽取/分类/起名/写正则/生成清单 等"纯文本进、纯文本出"的机械活
需要"能读写文件、能跑命令"的活，请用 delegate.ps1（T2 层）。

用法：
  python -I ask.py "把下面的中文润色成学术风格：<正文>"
  python -I ask.py --in draft.md --out polished.md "润色成学术风格，保持 Markdown 结构"
  python -I ask.py "每份写 3 句摘要" --files a.md b.md                # 单次调用，多文件
  python -I ask.py "统一标题格式为 '# 序号 标题'" --batch docs/*.md --out-dir out/
  echo "..." | python -I ask.py --stdin "翻译成英文"
（注意：--files / --batch 是 nargs='+'，指令要写在可选参数之前。）

超时与重试：
  --timeout 是**整次调用**的总预算（默认 60 秒，含重试与退避），用 monotonic deadline 计算，
  重试不会再拿到一份完整预算；--retries 默认 1，范围 0..2。
  只有 429/5xx 与网络错误才重试；400/401/403 等直接失败，不重试。
  finish_reason 非 stop（例如 length 截断）或 content 不是非空字符串时：报错退出，
  不写任何文件、不上报 done；能拿到 HTTP usage 时按失败账记 status=failed。

输出安全：
  先校验整批（result 的 basename 必须与输入 basename 集合完全一致、每个恰好一次、content 是字符串），
  再预检所有目标，最后才写文件；输出一律用"同目录临时文件 + os.replace"做单文件原子写入
  （不宣称整批原子性）。输出不得覆盖输入文件，不得落在只读禁区（D:/protected-data、D:/research-raw，大小写不敏感）；
  输出已存在时默认拒绝，加 --overwrite 才覆盖（覆盖前自动做时间戳备份）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import subprocess
import math
import uuid
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

API_URL = 'https://api.deepseek.com/chat/completions'
MODEL = 'deepseek-flash'
LOG = Path(__file__).resolve().parent.parent / 'logs' / 'delegations.jsonl'
BEIJING = timezone(timedelta(hours=8))            # 时间峰谷判断一律按北京时间（UTC+8）
FORBIDDEN_ROOTS = ('D:/protected-data', 'D:/research-raw')    # 只读禁区（大小写不敏感，按 resolve 后的路径判断）
BACKOFF_BASE_SEC = 2                              # 退避 = 2**已失败次数 * 2 秒，且受 deadline 约束


def load_key() -> str:
    """密钥单一来源：环境变量 DEEPSEEK_API_KEY > worker-home/config.toml（脚本内不存 key）。"""
    k = os.environ.get('DEEPSEEK_API_KEY')
    if k:
        return k
    cfg = Path(__file__).resolve().parent.parent / 'worker-home' / 'config.toml'
    if cfg.exists():
        m = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', cfg.read_text(encoding='utf-8'))
        if m:
            return m.group(1)
    raise SystemExit('[ask] 找不到 DeepSeek key：请设环境变量 DEEPSEEK_API_KEY，'
                     r'或在 worker-home/config.toml 里填 experimental_bearer_token')

# 固定系统前缀 = 命中 DeepSeek 上下文缓存（命中价 1/50 ~ 1/50）
SYSTEM = (
    '你是精确的执行器。只完成用户明确要求的那一件事：不寒暄、不解释、不加开场白与结尾客套。'
    '输出就是最终结果本身；除非用户要求，不要用代码围栏包裹，不要复述输入。'
)
BATCH_SYSTEM = (
    '你是精确的批量执行器。用户会给出同一套指令和用 <<<FILE: 名字 >>> 分隔的多份文本。'
    '对每一份分别执行指令，然后用**纯 JSON**回复：{"results":[{"name":"文件名","content":"处理后的全文"}]}，'
    '不要任何其它文字、不要代码围栏。'
)


class AskError(Exception):
    """调用/校验失败。usage 是能拿到的"失败账"（拿不到就是 None），绝不虚报成功。"""

    def __init__(self, message: str, usage=None, kind: str = 'error'):
        super().__init__(message)
        self.message = message
        self.usage = usage if isinstance(usage, dict) else None
        self.kind = kind


def is_peak(dt=None) -> bool:
    """北京时间（UTC+8）工作日 09:00-12:00 / 14:00-18:00（不含法定节假日，节假日请自行忽略峰值）。

    dt 为 None 时取当前北京时间；传入 naive datetime 视为北京时间墙上时间，传入 aware datetime 会先换算到 UTC+8。
    """
    dt = dt or datetime.now(BEIJING)
    if dt.tzinfo is not None:
        dt = dt.astimezone(BEIJING)
    if dt.weekday() >= 5:
        return False
    return (9 <= dt.hour < 12) or (14 <= dt.hour < 18)


def price_cny(in_tok, cached_tok, out_tok, peak: bool):
    """按下表峰/谷单价（元/百万 token）换算 —— **只是估计**，实际以官方账单为准。"""
    hit, miss, out = (0.04, 2.0, 8.0) if peak else (0.02, 1.0, 4.0)
    return round((cached_tok * hit + max(0, in_tok - cached_tok) * miss + out_tok * out) / 1e6, 4)


def _usage_from_body(body: str):
    """从（错误）响应体里尽力取出 usage，用于保留失败账。"""
    try:
        data = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict) and isinstance(data.get('usage'), dict):
        return data['usage']
    return None


def call(messages, thinking: str, max_tokens: int, json_mode: bool,
         timeout: float = 60.0, retries: int = 1):
    """单次 HTTP 调用。

    timeout 是**整次调用**的总预算：重试与退避共用同一条 monotonic deadline，
    每轮不会再获得完整预算。只有 429/5xx 与网络错误才重试；400/401/403 等不重试。
    """
    key = load_key()
    body = {
        'model': MODEL,
        'messages': messages,
        'max_tokens': max_tokens,
        'stream': False,
    }
    if thinking == 'off':
        body['thinking'] = {'type': 'disabled'}
    else:
        body['thinking'] = {'type': 'enabled', 'reasoning_effort': thinking}
    if json_mode:
        body['response_format'] = {'type': 'json_object'}

    deadline = time.monotonic() + float(timeout)
    last_err = None
    last_usage = None
    for attempt in range(retries + 1):
        remaining = min(float(timeout), deadline - time.monotonic())
        if remaining <= 0:
            raise AskError(f'超时：{timeout}s 预算已耗尽（已尝试 {attempt} 次）：{last_err or "请求未完成"}',
                           last_usage, kind='timeout')
        req = urllib.request.Request(
            API_URL, data=json.dumps(body).encode('utf-8'), method='POST',
            headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=remaining) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                raw = e.read().decode('utf-8', errors='replace')
            except Exception:  # noqa: BLE001
                raw = ''
            last_usage = _usage_from_body(raw) or last_usage
            last_err = f'HTTP {e.code}: {raw[:300]}'
            retryable = e.code == 429 or 500 <= e.code < 600
            if not retryable or attempt >= retries:
                raise AskError(last_err, last_usage, kind=f'http-{e.code}')
        except (urllib.error.URLError, TimeoutError, OSError) as e:  # Only transient network failures may be retried
            last_err = str(e)
            if attempt >= retries:
                raise AskError(last_err, last_usage, kind='network')
        backoff = 2 ** attempt * BACKOFF_BASE_SEC
        remaining = min(float(timeout), deadline - time.monotonic())
        if remaining <= 0:
            raise AskError(f'超时：{timeout}s 预算已耗尽（已尝试 {attempt + 1} 次）：{last_err}',
                           last_usage, kind='timeout')
        if backoff >= remaining:
            time.sleep(remaining)
            raise AskError(f'超时：{timeout}s 预算不足以重试：{last_err}', last_usage, kind='timeout')
        time.sleep(backoff)
    raise AskError(f'超时：{timeout}s 预算已耗尽：{last_err}', last_usage, kind='timeout')


def log_run(kind, prompt_chars, usage, duration, extra=None,
            status: str = 'done', exit_code: int = 0, blocked_reason: str = ''):
    """写一条台账。usage 拿不到就不写（返回 None）；失败时 status=failed，禁止虚报成功。"""
    if not usage:
        return None
    peak = is_peak()
    cost = price_cny(usage.get('prompt_tokens', 0), usage.get('prompt_cache_hit_tokens', 0),
                     usage.get('completion_tokens', 0), peak)
    rec = {
        'ts': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'), 'unit_id': 'ask-' + uuid.uuid4().hex[:12],
        'trace_id': os.environ.get('DEEPSEEK_TRACE_ID'), 'node_id': os.environ.get('DEEPSEEK_NODE_ID'),
        'type': kind, 'model': MODEL, 'effort': 'text', 'sandbox': 'none', 'workspace': str(Path.cwd()),
        'is_peak': peak, 'duration_sec': round(duration, 1), 'exit_code': exit_code, 'status': status,
        'files_changed': (extra or {}).get('files_changed', []), 'summary': (extra or {}).get('summary', ''),
        'unverified': [], 'blocked_reason': blocked_reason,
        'input_tokens': usage.get('prompt_tokens', 0), 'cached_input_tokens': usage.get('prompt_cache_hit_tokens', 0),
        'output_tokens': usage.get('completion_tokens', 0), 'reasoning_output_tokens': 0,
        'cost_estimated_cny': cost, 'artifacts': {'prompt_chars': prompt_chars},
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')
    return cost


def _extract_content(resp) -> str:
    """校验 finish_reason == stop 且 content 是非空字符串；不满足就报错（不写文件、不上报 done）。"""
    usage = resp.get('usage') if isinstance(resp, dict) else None
    try:
        choice = resp['choices'][0]
    except Exception:  # noqa: BLE001
        raise AskError('响应缺少 choices[0]', usage, kind='bad-response')
    finish = choice.get('finish_reason')
    if finish != 'stop':
        raise AskError(f'finish_reason={finish!r}（非 stop，响应可能被截断），拒绝写入/上报 done',
                       usage, kind='truncated')
    content = (choice.get('message') or {}).get('content')
    if not isinstance(content, str) or not content.strip():
        raise AskError(f'content 不是非空字符串（{type(content).__name__}），拒绝写入/上报 done',
                       usage, kind='bad-content')
    return content


def _fail(log_kind, prompt_chars, t0, message, usage=None, files_changed=None):
    """统一失败出口：能拿到用量就留失败账（status=failed / exit_code=1），然后非 0 退出。"""
    log_run(log_kind, prompt_chars, usage, time.time() - t0, status='failed', exit_code=1,
            blocked_reason=message, extra={'files_changed': files_changed or [], 'summary': ''})
    raise SystemExit(f'[ask] 失败：{message}')


def _norm(path) -> Path:
    """绝对 + 大小写不敏感的规范化路径（不要求路径已存在），用于判断"是否同一文件/是否在禁区"。"""
    return Path(os.path.normcase(os.path.realpath(str(path))))


def _in_forbidden(path) -> bool:
    target = _norm(path)
    for root in FORBIDDEN_ROOTS:
        root_n = _norm(root)
        if target == root_n or root_n in target.parents:
            return True
    return False


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace：单文件原子写入（不宣称整批原子性）。"""
    parent = path.parent if str(path.parent) else Path('.')
    fd, tmp = tempfile.mkstemp(dir=str(parent), prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup_existing(path: Path) -> Path:
    """覆盖前的时间戳备份（同秒冲突时自动加序号）。"""
    ts = datetime.now(BEIJING).strftime('%Y%m%d-%H%M%S')
    dst = path.with_name(f'{path.name}.{ts}.bak')
    seq = 1
    while dst.exists():
        dst = path.with_name(f'{path.name}.{ts}.{seq}.bak')
        seq += 1
    shutil.copy2(path, dst)
    return dst


def _preflight_output(dst: Path, inputs_norm: set, overwrite: bool):
    """写入前预检单个目标；返回需要先备份的路径（None = 新建）。任何问题都抛 AskError，绝不半写。"""
    if _in_forbidden(dst):
        raise AskError(f'输出落在只读禁区（{"、".join(FORBIDDEN_ROOTS)}），拒绝写入：{dst}')
    if _norm(dst) in inputs_norm:
        raise AskError(f'输出会覆盖输入文件，拒绝写入：{dst}')
    if dst.exists():
        if dst.is_dir():
            raise AskError(f'输出目标是目录，拒绝写入：{dst}')
        if not overwrite:
            raise AskError(f'输出已存在（如确实要覆盖请加 --overwrite）：{dst}')
        return dst
    parent = dst.parent
    if parent.exists() and not parent.is_dir():
        raise AskError(f'输出目录不可用（同名文件挡路）：{parent}')
    return None


def collect_batch_inputs(items):
    """批量输入：按绝对路径去重、支持绝对 glob，保持首次出现顺序。"""
    files = []
    seen = set()
    for item in items:
        if '*' in item or '?' in item or '[' in item:
            matches = sorted(glob.glob(item))
        else:
            pth = Path(item)
            matches = sorted(pth.glob('*')) if pth.is_dir() else [pth]
        for m in matches:
            p = Path(m)
            if not p.is_file():
                continue
            key = _norm(p)
            if key in seen:
                continue
            seen.add(key)
            files.append(p)
    return files


def check_unique_basenames(files):
    """不同输入同 basename 无法按名回写：请求前就报错。返回去重后的 basename 列表。"""
    by_name = {}
    for f in files:
        by_name.setdefault(f.name, []).append(f)
    dupes = {n: v for n, v in by_name.items() if len(v) > 1}
    if dupes:
        detail = '；'.join(f'{n} <- ' + ' | '.join(str(x) for x in sorted(v)) for n, v in sorted(dupes.items()))
        raise AskError(f'输入存在同名文件（basename 冲突），必须在请求前消除：{detail}')
    return sorted(by_name)


def parse_batch_results(msg: str):
    m = re.findall(r'(?s)\{.*\}', msg)
    try:
        data = json.loads(m[-1]) if m else json.loads(msg)
    except Exception as e:  # noqa: BLE001
        raise AskError(f'批量结果不是合法 JSON：{e}')
    if not isinstance(data, dict) or not isinstance(data.get('results'), list):
        raise AskError('批量结果缺少 results 数组')
    return data['results']


def validate_batch_results(results, expected_names):
    """整批先验证：basename 集合完全一致、每个恰好一次、content 是字符串。返回 {name: content}。"""
    expected = set(expected_names)
    out = {}
    for i, item in enumerate(results):
        if not isinstance(item, dict):
            raise AskError(f'results[{i}] 不是对象')
        name = item.get('name')
        content = item.get('content')
        if not isinstance(name, str) or not name.strip():
            raise AskError(f'results[{i}] 缺少合法 name')
        if name in ('.', '..') or '/' in name or '\\' in name or Path(name).name != name:
            raise AskError(f'results[{i}] name 非法（禁止路径/绝对路径/../）：{name!r}')
        if name not in expected:
            raise AskError(f'results[{i}] name 越界（不得新增输入以外的文件）：{name!r}')
        if name in out:
            raise AskError(f'results[{i}] name 重复：{name!r}')
        if not isinstance(content, str):
            raise AskError(f'results[{i}] content 不是字符串：{name!r}')
        out[name] = content
    missing = sorted(expected - set(out))
    if missing:
        raise AskError(f'批量结果缺项（模型漏了）：{missing}')
    return out


def run_batch(a, t0):
    st = {'prompt_chars': 0, 'usage': None, 'written': []}
    try:
        files = collect_batch_inputs(a.batch)
        if not files:
            raise AskError('批量模式没有找到文件')
        names = check_unique_basenames(files)

        out_dir = Path(a.out_dir) if a.out_dir else Path('out')
        if _in_forbidden(out_dir):
            raise AskError(f'输出目录落在只读禁区，拒绝写入：{out_dir}')
        if out_dir.exists() and not out_dir.is_dir():
            raise AskError(f'输出目录不可用（已存在同名文件）：{out_dir}')

        inputs_norm = {_norm(f) for f in files}
        for name in names:  # 请求前先按输入 basename 预检所有目标：能早失败就不花钱
            _preflight_output(out_dir / name, inputs_norm, a.overwrite)

        parts = []
        for f in files:
            parts.append(f'<<<FILE: {f.name}>>>\n' + f.read_text(encoding='utf-8', errors='replace'))
        user = a.instruction + '\n\n' + '\n\n'.join(parts)
        st['prompt_chars'] = len(user)
        resp = call([{'role': 'system', 'content': BATCH_SYSTEM}, {'role': 'user', 'content': user}],
                    a.thinking, a.max_tokens, True, a.timeout, a.retries)
        st['usage'] = resp.get('usage')
        results = validate_batch_results(parse_batch_results(_extract_content(resp)), names)

        backups = {}
        for name in sorted(results):  # 写任何文件之前，对真实目标再做一次权威预检
            bk = _preflight_output(out_dir / name, inputs_norm, a.overwrite)
            if bk is not None:
                backups[name] = bk
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            for name, content in sorted(results.items()):
                dst = out_dir / name
                if name in backups:
                    _backup_existing(dst)
                _atomic_write_text(dst, content)
                st['written'].append(str(dst))
        except OSError as e:
            raise AskError(f'写入失败（只完成 {len(st["written"])}/{len(results)}，非整批原子）：{e}')
    except AskError as e:
        _fail('ask-batch', st['prompt_chars'], t0, e.message, e.usage or st['usage'], st['written'])

    written = st['written']
    cost = log_run('ask-batch', st['prompt_chars'], st['usage'], time.time() - t0,
                   {'files_changed': written, 'summary': f'{len(written)} 个文件'})
    print(f'[ask] 批量完成 {len(written)}/{len(files)} 个 -> {out_dir}  用时 {time.time()-t0:.1f}s  成本 ¥{cost}')
    for w in written:
        print('  ', w)
    if backups:
        print(f'[ask] 覆盖前已时间戳备份 {len(backups)} 个旧文件（同名 .bak）')


def run_single(a, t0):
    inputs = []
    input_paths = []
    if a.infile:
        input_paths.append(Path(a.infile))
        inputs.append(Path(a.infile).read_text(encoding='utf-8', errors='replace'))
    if a.stdin:
        inputs.append(sys.stdin.read())
    if a.files:
        for f in a.files:
            input_paths.append(Path(f))
            inputs.append(f'<<<FILE: {Path(f).name}>>>\n' + Path(f).read_text(encoding='utf-8', errors='replace'))
    user = a.instruction + (('\n\n' + '\n\n'.join(inputs)) if inputs else '')

    dst = Path(a.out) if a.out else None
    inputs_norm = {_norm(p) for p in input_paths}
    bk = None
    try:
        if dst is not None:  # 请求前预检目标，避免"钱花了却写不出去"
            bk = _preflight_output(dst, inputs_norm, a.overwrite)
        resp = call([{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': user}],
                    a.thinking, a.max_tokens, a.json, a.timeout, a.retries)
        text = _extract_content(resp)
    except AskError as e:
        _fail('ask', len(user), t0, e.message, e.usage)

    if dst is None:
        cost = log_run('ask', len(user), resp.get('usage'), time.time() - t0, {'summary': a.instruction[:60]})
        sys.stdout.write(text if text.endswith('\n') else text + '\n')
        print(f'\n[ask] 用时 {time.time()-t0:.1f}s  成本 ¥{cost}', file=sys.stderr)
        return
    try:
        if not dst.parent.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
        if bk is not None:
            _backup_existing(dst)
        _atomic_write_text(dst, text)
    except OSError as e:
        _fail('ask', len(user), t0, f'写入失败：{e}', resp.get('usage'))
    cost = log_run('ask', len(user), resp.get('usage'), time.time() - t0, {'summary': a.instruction[:60], 'files_changed': [str(dst)]})
    print(f'[ask] -> {a.out}  用时 {time.time()-t0:.1f}s  成本 ¥{cost}')


def main():
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8')
        if not sys.stderr.isatty():
            sys.stderr.reconfigure(encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass

    p = argparse.ArgumentParser(prog='ask', description='T1 层：直连 DeepSeek 的纯文本通道')
    p.add_argument('instruction', help='要做什么（指令本身）')
    p.add_argument('--in', dest='infile', help='把该文件内容作为输入')
    p.add_argument('--stdin', action='store_true', help='从 stdin 读输入')
    p.add_argument('--files', nargs='+', help='多个输入文件（单次调用）')
    p.add_argument('--batch', nargs='+', metavar='GLOB_OR_FILE', help='批量模式：对每个文件套用同一指令，输出到 --out-dir')
    p.add_argument('--out-dir', help='批量模式的输出目录')
    p.add_argument('--out', help='单次模式：结果写到此文件（默认打印到 stdout）')
    p.add_argument('--thinking', choices=['off', 'low', 'high', 'max'], default='off')
    p.add_argument('--max-tokens', type=int, default=8192)
    p.add_argument('--json', action='store_true', help='要求纯 JSON 输出')
    p.add_argument('--timeout', type=float, default=60.0,
                   help='整次调用的总预算秒数（含重试与退避），默认 60')
    p.add_argument('--retries', type=int, default=1,
                   help='失败重试次数（0..2），默认 1；400/401/403 不重试，429/5xx 与网络错误才重试')
    p.add_argument('--overwrite', action='store_true',
                   help='允许覆盖已存在的输出文件（覆盖前自动做时间戳备份）')
    a = p.parse_args()

    if not 0 <= a.retries <= 2:
        raise SystemExit('[ask] --retries 必须在 0..2 之间')
    if not math.isfinite(a.timeout) or not 0 < a.timeout <= 3600:
        raise SystemExit('[ask] --timeout 必须为 0..3600 内有限正数（秒）')

    t0 = time.time()
    if a.batch:
        run_batch(a, t0)
    else:
        run_single(a, t0)


def supervised_cli():
    """CLI watchdog: urllib socket timeouts alone do not bound a trickling response."""
    if os.environ.get('_ASK_SUPERVISED') == str(Path(__file__).resolve()):
        return main()
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument('--timeout', type=float, default=60.0)
    options, _ = probe.parse_known_args()
    if not math.isfinite(options.timeout) or not 0 < options.timeout <= 3600:
        return main()  # ordinary argument error, no request
    env = dict(os.environ, _ASK_SUPERVISED=str(Path(__file__).resolve()))
    try:
        result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), *sys.argv[1:]],
                                env=env, timeout=options.timeout + 5)
        raise SystemExit(result.returncode)
    except subprocess.TimeoutExpired:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {'ts': datetime.now(BEIJING).replace(tzinfo=None).isoformat(),
                  'unit_id': 'ask-' + uuid.uuid4().hex[:12], 'type': 'ask', 'model': MODEL,
                  'trace_id': os.environ.get('DEEPSEEK_TRACE_ID'), 'node_id': os.environ.get('DEEPSEEK_NODE_ID'),
                  'status': 'timeout', 'exit_code': 124, 'duration_sec': options.timeout + 5,
                  'cost_estimated_cny': None, 'files_changed': [],
                  'unverified': ['Usage unavailable; inspect any output before retrying.'],
                  'blocked_reason': 'CLI hard deadline exceeded'}
        with LOG.open('a', encoding='utf-8') as output:
            output.write(json.dumps(record, ensure_ascii=False) + '\n')
        print('[ask] hard deadline exceeded (API budget + 5s CLI allowance); result not accepted.', file=sys.stderr)
        raise SystemExit(124)


if __name__ == '__main__':
    supervised_cli()

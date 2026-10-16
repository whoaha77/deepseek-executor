#!/usr/bin/env python3
"""watch_deepseek —— 每周巡检：有没有更划算/更新的 DeepSeek Flash 版本

巡检口径（重要）：
  - API 的 model id 恒为 `deepseek-flash`（无版本号），**版本号只在 /models 的 `name` 字段**
    （例如 name = "DeepSeek-V4.1-Flash"）→ 版本比较必须读 name 字段。
  - 第二信号源：官方更新日志页的指纹（有变化就提示去人看）。
  - 只提醒，不改任何配置（切换由用户点头）。

退出码：0=无变化  10=有变化（建议看提案）  1=出错
用法：python -I watch_deepseek.py [--quiet]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'state' / 'state.json'
LOGDIR = ROOT / 'logs'
MODELS_URL = 'https://api.deepseek.com/models'
CHANGELOG_URL = 'https://api-docs.deepseek.com/updates/'


def load_key() -> str:
    """密钥单一来源：环境变量 DEEPSEEK_API_KEY > worker-home/config.toml（脚本内不存 key）。"""
    k = os.environ.get('DEEPSEEK_API_KEY')
    if k:
        return k
    cfg = ROOT / 'worker-home' / 'config.toml'
    if cfg.exists():
        m = re.search(r'experimental_bearer_token\s*=\s*"([^"]+)"', cfg.read_text(encoding='utf-8'))
        if m:
            return m.group(1)
    return ''


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        **({'Authorization': f'Bearer {load_key()}'} if url == MODELS_URL else {}),
        'User-Agent': 'Mozilla/5.0 (compatible; deepseek-executor-watch/1.0)',
        'Accept': 'application/json, text/html;q=0.9',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', errors='replace')


def version_tuple(name: str):
    """从 'DeepSeek-V4.1-Flash' 之类的名字里抽出可比较的版本元组。"""
    m = re.search(r'[Vv](\d+(?:\.\d+)*)', name or '')
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split('.'))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding='utf-8'))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--acknowledge', action='store_true', help='Clear a reviewed pending update without a network request')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8') if not sys.stdout.isatty() else None

    now = datetime.now().astimezone()
    prev = load_state()
    state = {'last_check': now.isoformat(timespec='seconds')}
    if a.acknowledge:
        prev['update_available'] = False
        prev['notes'] = []
        STATE.write_text(json.dumps(prev, ensure_ascii=False, indent=2), encoding='utf-8')
        print('[watch] pending update acknowledged')
        return 0
    changed, notes, errors = False, [], []
    for field in ('models', 'flash', 'pro', 'model_ids', 'changelog_hash'):
        if field in prev:
            state[field] = prev[field]

    # ---- 1) 模型清单 ----
    try:
        data = json.loads(fetch(MODELS_URL))
        models = data.get('data', [])
        state['models'] = [{'id': m.get('id'), 'name': m.get('name'),
                            'context_window': m.get('context_window'),
                            'effort': (m.get('effort') or {}).get('supported_levels')} for m in models]

        flash = next((m for m in models if m.get('id') == 'deepseek-flash'), None)
        if not flash:
            changed = True
            notes.append('⚠️ /models 里已找不到 `deepseek-flash` —— 可能改名/下线，需要人工确认并改 worker-home\\config.toml')
            state['flash'] = None
        else:
            name = flash.get('name', '')
            state['flash'] = {'id': 'deepseek-flash', 'name': name,
                              'version': '.'.join(map(str, version_tuple(name))) or None,
                              'context_window': flash.get('context_window'),
                              'supported_levels': (flash.get('effort') or {}).get('supported_levels')}
            old_name = (prev.get('flash') or {}).get('name')
            if old_name and old_name != name:
                changed = True
                notes.append(f'🎉 flash 版本变化：{old_name} → {name}')
                notes.append('   建议：跑一次回归（run.ps1 两三个小任务）确认行为无异常；'
                             '新版若无行为变化，只需在 lessons.md 记一笔，配置无需改（model id 恒为 deepseek-flash）')
            elif not old_name:
                changed = True
                notes.append(f'首次巡检建立基线：flash = {name}')

        # 关注 Pro 线（虽然本项目只用 flash，但 Pro 上线往往意味着 flash 也要换代）
        pro = next((m for m in models if m.get('id') == 'deepseek-v4-pro'), None)
        if pro:
            state['pro'] = {'id': pro.get('id'), 'name': pro.get('name')}
            old_pro = (prev.get('pro') or {}).get('name')
            if old_pro and old_pro != pro.get('name'):
                changed = True
                notes.append(f'ℹ️ Pro 线变化：{old_pro} → {pro.get("name")}（本项目只用 flash，仅作行情参考）')
        state['model_ids'] = sorted(m.get('id', '') for m in models)
        if prev.get('model_ids') and state['model_ids'] != prev['model_ids']:
            changed = True
            notes.append(f'ℹ️ 模型清单变化：{prev["model_ids"]} → {state["model_ids"]}')
    except Exception as e:  # noqa: BLE001
        errors.append(f'拉取 /models 失败：{e}')
        state['flash'] = prev.get('flash')

    # ---- 2) 官方更新日志指纹 ----
    try:
        html = fetch(CHANGELOG_URL)
        text = re.sub(r'<script.*?</script>|<style.*?</style>', '', html, flags=re.S)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text)
        digested = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
        state['changelog_hash'] = digested
        if prev.get('changelog_hash') and prev['changelog_hash'] != digested:
            changed = True
            notes.append(f'📰 官方更新日志有变动（指纹 {prev["changelog_hash"]} → {digested}）：{CHANGELOG_URL}')
            m = re.findall(r'(20\d{2}[-/年]\d{1,2}[-/月]\d{1,2})', text)
            if m:
                notes.append(f'   其中最新出现的日期字样：{m[-1]}')
    except Exception as e:  # noqa: BLE001
        errors.append(f'拉取更新日志失败：{e}')

    state['update_available'] = bool(changed or prev.get('update_available'))
    state['notes'] = list(dict.fromkeys((prev.get('notes', []) if prev.get('update_available') else []) + notes))
    state['errors'] = errors

    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

    LOGDIR.mkdir(parents=True, exist_ok=True)
    with open(LOGDIR / 'watch.log', 'a', encoding='utf-8') as fh:
        fh.write(f"[{now:%Y-%m-%d %H:%M:%S}] checked  changed={changed}  flash={(state.get('flash') or {}).get('name')}"
                 f"  errors={len(errors)}\n")
        for n in notes:
            fh.write(f"    {n}\n")
        for e in errors:
            fh.write(f"    ERROR {e}\n")

    # ---- 3) 有变化 → 写提案（只提不改）----
    if changed:
        wk = now.strftime('%Y-W%V')
        prop = STATE.parent / f'proposals-{wk}.md'
        lines = [f'# DeepSeek 巡检提案（{now:%Y-%m-%d %H:%M}）', '']
        lines += [f'- {n}' for n in notes]
        lines += ['', '## 人工确认清单', '',
                  '- [ ] 是否需要改配置？（本项目 model id 恒为 `deepseek-flash`，通常**无需改**）',
                  '- [ ] 是否需要跑一次回归（run.ps1 派 2~3 个小任务）？',
                  '- [ ] 结论写进 `lessons/lessons.md`，并把本条提案标记为已处理。', '']
        if errors:
            lines += ['## 巡检错误', ''] + [f'- {e}' for e in errors] + ['']
        prop.write_text('\n'.join(lines), encoding='utf-8')

    if not a.quiet:
        print(f"[watch] flash={(state.get('flash') or {}).get('name')}  changed={changed}")
        for n in notes:
            print('  ' + n)
        for e in errors:
            print('  ERROR ' + e)

    if errors and not changed:
        return 1
    return 10 if changed else 0


if __name__ == '__main__':
    raise SystemExit(main())

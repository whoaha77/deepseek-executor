#!/usr/bin/env python3
"""cost_report —— 成本报表：读本项目的 delegations.jsonl（可加读 CC Switch 账本）

用法：
  python -I cost_report.py                 # 全部历史汇总
  python -I cost_report.py --days 7        # 最近 7 天
  python -I cost_report.py --cc-switch     # 附带 CC Switch 账本（Claude/Codex 应用流量）
  python -I cost_report.py --proposals     # 写周报草案 state/cost-report-YYYY-Www.md（自优化闭环用）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / 'logs' / 'delegations.jsonl'
CCSW_DB = Path.home() / '.cc-switch' / 'cc-switch.db'


def load(days: int):
    if not JSONL.exists():
        return []
    cutoff = datetime.now().astimezone() - timedelta(days=days) if days else None
    rows = []
    for line in JSONL.read_text(encoding='utf-8-sig', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if cutoff:
            try:
                if datetime.fromisoformat(r.get('ts', '')).astimezone() < cutoff:
                    continue
            except Exception:  # noqa: BLE001
                pass
        rows.append(r)
    return rows


def summarize(rows):
    by_type = defaultdict(lambda: {'n': 0, 'cost': None, 'sec': 0.0, 'rework': 0,
                                  'in': None, 'out': None, 'cache': None,
                                  'unknown_cost': 0, 'unknown_in': 0,
                                  'unknown_out': 0, 'unknown_cache': 0})
    total_cost = None
    for r in rows:
        t = r.get('type', '?')
        d = by_type[t]
        d['n'] += 1
        legacy_unknown = (not r.get('usage_status') and r.get('cost_estimated_cny') is None
                          and r.get('status') != 'done'
                          and all(r.get(k) in (None,0) for k in ('input_tokens','output_tokens','cached_input_tokens')))
        for field, key in [('cost_estimated_cny', 'cost'), ('input_tokens', 'in'),
                           ('output_tokens', 'out'), ('cached_input_tokens', 'cache')]:
            value = r.get(field)
            if legacy_unknown and key!='cost': value=None
            if value is None:
                d['unknown_' + key] += 1
            else:
                d[key] = (d[key] if d[key] is not None else 0) + value
        c = r.get('cost_estimated_cny')
        if c is not None:
            total_cost = (total_cost if total_cost is not None else 0.0) + c
        d['sec'] += r.get('duration_sec') or 0
        if r.get('status') not in ('done', None) or r.get('blocked_reason'):
            d['rework'] += 1
    return by_type, total_cost if by_type else 0.0


def format_known(value, unknown=0, *, money=False):
    """Known subtotal and missing coverage are distinct; zero is a valid value."""
    if value is None:
        return '未知'
    shown = f'{value:.4f}' if money else f'{value:,}'
    return shown + (f'（{unknown}条未知）' if unknown else '')


def cc_switch_summary(days):
    """读 CC Switch 自带账本（只读）。覆盖 Claude / Codex 应用侧流量。"""
    if not CCSW_DB.exists():
        return []
    try:
        con = sqlite3.connect(f'file:{CCSW_DB}?mode=ro', uri=True)
        cur = con.cursor()
        since = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        q = ("select date, app_type, request_model, count(*), sum(input_tokens), sum(output_tokens), "
             "sum(cache_read_tokens), round(sum(total_cost_usd),4) from usage_daily_rollups "
             "where date >= ? group by date, app_type, request_model order by date desc, sum(total_cost_usd) desc limit 40")
        return list(cur.execute(q, (since,)))
    except Exception as e:  # noqa: BLE001
        return [('ERROR', str(e), '', 0, 0, 0, 0, 0)]


def main():
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=0)
    ap.add_argument('--cc-switch', action='store_true')
    ap.add_argument('--proposals', action='store_true')
    ap.add_argument('--trace', help='Restrict to one explicit team trace id')
    a = ap.parse_args()
    if a.trace and a.proposals:
        ap.error('--trace cannot overwrite the global weekly report; omit --proposals')

    rows = load(a.days)
    if a.trace:
        rows = [row for row in rows if row.get('trace_id') == a.trace]
    by_type, total = summarize(rows)

    lines = []
    span = f'最近 {a.days} 天' if a.days else '全部历史'
    if a.trace: span += ' / trace=' + a.trace
    lines.append(f'# 执行器成本报表（{span}，生成于 {datetime.now():%Y-%m-%d %H:%M}）')
    lines.append('')
    unknown_costs = sum(1 for row in rows if row.get('cost_estimated_cny') is None)
    lines.append(f'费用按配置费率估算；{unknown_costs} 条调用费用未知，未计入金额合计。实际以官方账单为准。')
    lines.append('各用量列为已知合计；括号注明缺失记录数，全缺失显示未知。缓存是总输入的子集，不另加到输入。')
    lines.append('旧失败记录若无usage标记、费用未知且全部token为零/缺失，保守视为用量未知；原始账本不改写。')
    lines.append('')
    lines.append('| 类型 | 次数 | 已知估计成本(¥) | 平均耗时(s) | 输入tokens | 命中缓存 | 输出tokens | 失败/返工 |')
    lines.append('|---|---:|---:|---:|---:|---:|---:|---:|')
    for t, d in sorted(by_type.items(), key=lambda kv: -(kv[1]['cost'] or 0)):
        avg = d['sec'] / d['n'] if d['n'] else 0
        cells = [t, str(d['n']), format_known(d['cost'], d['unknown_cost'], money=True),
                 f'{avg:.1f}', format_known(d['in'], d['unknown_in']),
                 format_known(d['cache'], d['unknown_cache']),
                 format_known(d['out'], d['unknown_out']), str(d['rework'])]
        lines.append('| ' + ' | '.join(cells) + ' |')
    lines.append(f"| **合计** | **{sum(d['n'] for d in by_type.values())}** | **{format_known(total, unknown_costs, money=True)}** | | | | | |")
    lines.append('')

    if a.cc_switch:
        lines.append('## CC Switch 账本（Claude / Codex 应用侧，只读）')
        lines.append('')
        lines.append('| 日期 | 应用 | 模型 | 请求数 | 输入 | 输出 | 缓存读 | 成本(USD) |')
        lines.append('|---|---|---|---:|---:|---:|---:|---:|')
        for r in cc_switch_summary(a.days or 30):
            lines.append('| ' + ' | '.join(str(x) for x in r) + ' |')
        lines.append('')

    text = '\n'.join(lines)
    print(text)

    if a.proposals:
        wk = datetime.now().strftime('%Y-W%V')
        out = ROOT / 'state' / f'cost-report-{wk}.md'
        out.parent.mkdir(parents=True, exist_ok=True)
        tips = []
        if by_type.get('ask', {}).get('n', 0) and by_type.get('general', {}).get('n', 0):
            pass
        heavy = sorted(by_type.items(), key=lambda kv: -(kv[1]['sec'] / kv[1]['n'] if kv[1]['n'] else 0))[:3]
        for t, d in heavy:
            avg = d['sec'] / d['n'] if d['n'] else 0
            if avg > 60:
                tips.append(f'- `{t}` 平均 {avg:.0f}s 偏慢：可考虑降到 T0/T1 层（能脚本化的脚本化，能纯文本的走 ask.py）')
        for t, d in by_type.items():
            if d['n'] >= 3 and d['rework'] / d['n'] > 0.3:
                tips.append(f'- `{t}` 返工率 {d["rework"]}/{d["n"]} 偏高：任务规格模板要更精确（附带输入清单与验收标准）')
        out.write_text(text + '\n## 自优化建议（草案，待人工确认）\n\n' + ('\n'.join(tips) if tips else '- 暂无明显问题。') + '\n', encoding='utf-8')
        print(f'\n[cost] 周报草案 -> {out}')


if __name__ == '__main__':
    main()

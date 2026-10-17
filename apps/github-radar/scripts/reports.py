"""Chinese reports and manifest-only retention within the configured report folder."""
import hashlib
import html
import os
import re
from datetime import datetime, timezone
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_reparse(path):
    try:
        return bool(getattr(Path(path).lstat(), 'st_file_attributes', 0) & 0x400) or Path(path).is_symlink()
    except FileNotFoundError:
        return False


def safe_folder(folder):
    folder = Path(folder).absolute()
    # Reject junctions/symlinks in every ancestor before any write/delete.
    if any(is_reparse(p) for p in (folder, *folder.parents)):
        raise ValueError('报告目录或上级目录是链接/重解析点，停止操作')
    return folder


def mdtext(value):
    return str(value or '').replace('\r', ' ').replace('\n', ' ').replace('|', '\\|')


def render(run, cards, budget):
    spending_limit = '不限额' if run['daily_budget'] is None else f"¥{run['daily_budget']:.2f}"
    lines = ['# GitHub 项目发现报告', '', '**时间：** ' + run['local_time'], '',
             '**运行状态：** ' + run['status'], '',
             '优先科研与效率工具；项目内容未经安装或运行验证。', '',
             f"本轮发现 {run['discovered']} 个项目，读取 {run['evidence_count']} 个证据包，"
             f"新增 DS 分析 {run['analyzed']} 个，复用分析 {run['cache_hits']} 个。", '',
             f"今日 DS 预算占用（保守估算）：¥{budget['charged_micro']/1000000:.4f} / {spending_limit}。"
             f"费用未知 {budget['unknown_count']} 笔；未知调用保留预算。", '',
             '这是本程序的独立预算，不包含其他 DS 工具调用或订阅消耗。', '']
    if run.get('errors'):
        lines += ['## 本轮限制', ''] + ['- ' + mdtext(x) for x in run['errors']] + ['']
    if not cards:
        lines += ['本轮没有可新增推荐的项目。请结合运行状态区分无新增与采集/分析失败。', '']
    for index, card in enumerate(cards, 1):
        repo, analysis = card['repo'], card.get('analysis')
        lines += [f"## {index}. {mdtext(repo['full_name'])}", '', repo['html_url'], '',
                  f"**分组：** {mdtext(card['category'])}　**变化：** {mdtext(card['change'])}", '',
                  f"**Star：** {repo.get('stargazers_count', 0):,}　**语言：** {mdtext(repo.get('language') or '未标注')}　"
                  f"**License：** {mdtext((repo.get('license') or {}).get('spdx_id') or '未确认')}", '']
        growth = card.get('growth')
        lines += [('观察期 %.2f 天，Star 净变化 %+d。' % (growth['days'], growth['net_stars']))
                  if growth else 'Star 增长：历史不足（不推测近 7 天增长）。', '']
        if analysis:
            for label, key in [('解决什么问题', 'summary'), ('与你的关系', 'fit'),
                               ('相对已有工具的增量', 'difference'), ('采用条件', 'adoption')]:
                lines += [f'**{label}：** ' + mdtext(analysis[key]), '']
            lines += [f"**参考评分：** {card['score']:.1f}/100（固定权重计算，仅作筛选参考）。", '',
                      '**主要未知：** ' + '；'.join(mdtext(x) for x in analysis['unknowns']), '',
                      '**证据等级：** 仓库材料支持；未进行运行实测。', '']
            codex = analysis.get('codex')
            if codex:
                lines += ['**Codex 适配：** ' + mdtext(codex['integration']) + '；优先级 ' + mdtext(codex['priority']), '',
                          '**接入价值与重叠：** ' + mdtext(codex['reason'] + '；' + codex['overlap']), '',
                          '**依赖与风险：** ' + mdtext(codex['requirements'] + '；' + codex['risk']), '',
                          '**部署前核查：** ' + mdtext(codex['next_step']), '',
                          '仅提示候选；等待你主动提出安装，本查找器不执行部署。', '']
            for claim in analysis['claims']:
                source = card['sources'].get(claim['source'])
                lines += [f"- {mdtext(claim['text'])}（{source['url']}，证据 {claim['source']} 第 {claim['line']} 行）"]
            lines += ['']
        else:
            lines += ['**客观候选清单：** ' + mdtext(repo.get('description') or '未提供描述'), '',
                      'DS 分析未取得或未启用；本条没有经过语义推荐验收。', '']
        lines += [f"反馈命令：`run.ps1 feedback {repo['id']} saved --reason 有用`", '']
    lines += ['## 关于本报告', '', '报告保留最近 30 天，至少保留最新 10 份；只清理清单登记且内容未被改动的历史报告。',
              '查找器源码与使用说明位于本仓库；数据库、证据、模型输入输出和运行日志仅保存在运行机器上。',
              '引用行号对应项目中保存的证据版本，不保证与远端后来更新的网页行号一致。', '']
    markdown = '\n'.join(lines)
    # Render from trusted template only. Remote text is always escaped.
    sections = []
    for line in lines:
        escaped = html.escape(line)
        if line.startswith('# '):
            sections.append('<h1>' + html.escape(line[2:]) + '</h1>')
        elif line.startswith('## '):
            sections.append('<h2>' + html.escape(line[3:]) + '</h2>')
        elif line:
            if re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', line):
                escaped = '<a href="' + escaped + '" rel="noreferrer">' + escaped + '</a>'
            sections.append('<p>' + escaped + '</p>')
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
    page += '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">'
    page += '<title>GitHub 项目发现报告</title><style>body{font:16px/1.8 system-ui,sans-serif;max-width:900px;margin:40px auto;padding:0 24px;color:#24312c;background:#fafbf8}h1,h2{line-height:1.4}h2{margin-top:40px;border-top:1px solid #ccd4cc;padding-top:24px}p{overflow-wrap:anywhere}a{color:#17684d}</style>'
    return markdown, page + '<body>' + ''.join(sections) + '</body></html>'


def write_report(store, folder, run, cards, budget):
    folder = safe_folder(folder)
    folder.mkdir(parents=True, exist_ok=True)
    report_id = run['run_id']
    if not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}', report_id):
        raise ValueError('invalid report id')
    markdown, page = render(run, cards, budget)
    files = []
    for suffix, body in [('md', markdown), ('html', page)]:
        name = 'github-report-' + report_id + '.' + suffix
        target = folder / name
        # Exclusive creation prevents clobbering an unrelated/user file.
        with target.open('x', encoding='utf-8') as stream:
            stream.write(body)
        files.append({'name': name, 'sha256': digest(target)})
    store.save_report(report_id, datetime.now(timezone.utc).isoformat(), str(folder), files)
    return [str(folder / f['name']) for f in files]


def cleanup(store, folder, keep_days=30, keep_latest=10, apply=False, now=None):
    """Delete only registered, old, unchanged regular files. Never recurse."""
    if isinstance(keep_days, bool) or isinstance(keep_latest, bool) or keep_days < 1 or keep_latest < 1:
        raise ValueError('invalid retention')
    folder = safe_folder(folder)
    now = now or datetime.now(timezone.utc)
    matching = [r for r in store.reports() if Path(r['root']).absolute() == folder]
    result = {'eligible': [], 'deleted': [], 'protected': []}
    for report in matching[keep_latest:]:
        if (now - datetime.fromisoformat(report['at'])).total_seconds() <= keep_days * 86400:
            continue
        checked = []
        for entry in report['files']:
            name = entry.get('name', '')
            expected = r'github-report-' + re.escape(report['report_id']) + r'\.(md|html)'
            if not re.fullmatch(expected, name):
                result['protected'].append(report['report_id'])
                break
            target = folder / name
            if target.parent != folder or is_reparse(target):
                result['protected'].append(report['report_id'])
                break
            if target.exists() and (not target.is_file() or digest(target) != entry.get('sha256')):
                result['protected'].append(report['report_id'])
                break
            checked.append(target)
        else:
            result['eligible'].append(report['report_id'])
            if apply:
                for target in checked:
                    if target.exists():
                        target.unlink()
                store.remove_report(report['report_id'])
                result['deleted'].append(report['report_id'])
    return result

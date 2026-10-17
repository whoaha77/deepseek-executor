"""Lifetime shortlist and advisory Codex integration plans. This module never installs."""
import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from reports import safe_folder

WEIGHTS = {'relevance': 6, 'value': 5, 'adoption': 4, 'engineering': 3, 'community': 2}
TYPES = {'skill': 'Skill', 'mcp': 'MCP 服务', 'cli': '命令行工具', 'library': '代码库',
         'application': '独立应用', 'reference': '参考资料', 'mixed': '混合工具集', 'unknown': '待识别'}
PLACEMENT = {
    'skill': '先确认具体 SKILL.md 和依赖；按本机权威技能目录规则审核、备份、预览同步，尚未安装。',
    'mcp': '属于 MCP 连接与配置；须核对服务来源、权限、凭证及本地/远程运行方式，尚未连接。',
    'cli': '属于本机命令行依赖；先确认 Windows 支持与隔离环境，再决定如何由 Codex 调用。',
    'library': '通常放入具体项目的依赖环境，不作为全局 Skill 安装。',
    'application': '通常作为独立软件使用，不等于可直接装入 Codex。',
    'reference': '用于借鉴或学习，不作为可直接部署的工具。',
    'mixed': '必须拆分具体 Skill、脚本、MCP 或软件组件逐项核对，不能整库盲装。',
    'unknown': '集成方式尚未确定，先核对仓库实际内容。'}


def validate_codex(value):
    if not isinstance(value, dict) or value.get('integration') not in TYPES or value.get('priority') not in ('高', '中', '低', '不适用'):
        raise ValueError('invalid Codex integration type/priority')
    if set(value) != {'integration', 'priority', 'reason', 'overlap', 'requirements', 'risk', 'next_step'}:
        raise ValueError('unexpected Codex fields; no approval/install state may be asserted')
    for key in ('reason', 'overlap', 'requirements', 'risk', 'next_step'):
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 400:
            raise ValueError('invalid Codex analysis field: ' + key)
    return value


def score_analysis(repo, analysis):
    scores = analysis.get('scores')
    if not isinstance(scores, dict) or set(scores) != set(WEIGHTS):
        raise ValueError('invalid stored scores')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 5 for v in scores.values()):
        raise ValueError('invalid stored score value')
    values = dict(scores)
    stars = repo.get('stargazers_count', 0)
    if isinstance(stars, bool) or not isinstance(stars, int) or stars < 0:
        raise ValueError('invalid stored stars')
    values['community'] = min(5, math.log10(1 + stars))
    return sum(values[k] * v for k, v in WEIGHTS.items())


def validate_sharing(value):
    if not isinstance(value, dict) or set(value) != {'why','try_idea','curiosity'}:
        raise ValueError('invalid sharing structure')
    for key in ('why','try_idea'):
        if not isinstance(value[key], str) or not 1 <= len(value[key]) <= 400:
            raise ValueError('invalid sharing text')
    score = value['curiosity']
    if isinstance(score, bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0 <= score <= 5:
        raise ValueError('invalid curiosity score')
    return value


def latest_rows(store, current=None, include_exploration=False, include_filtered=False):
    """One latest accepted version per repo, never the maximum old score."""
    current = current or datetime.now(timezone.utc)
    feedback = store.feedback()
    repos = {r['id']: r for r in store.repos()}
    seen, rows = set(), []
    for record in store.db.execute('SELECT repo_id,input_hash,body,at FROM analyses ORDER BY at DESC,rowid DESC'):
        repo_id = record['repo_id']
        if repo_id in seen:
            continue
        seen.add(repo_id)
        if repo_id not in repos or (not include_filtered and feedback.get(repo_id, {}).get('action') in ('ignore', 'later', 'tried_failed')):
            continue
        try:
            analysis = json.loads(record['body'])
            if 'sharing' in analysis:
                validate_sharing(analysis['sharing'])
            repo = repos[repo_id]
            score = score_analysis(repo, analysis)
            if not include_exploration and (score < 55 or analysis['scores']['relevance'] < 2.5):
                continue
            if not all(isinstance(analysis.get(k), str) and analysis[k] for k in ('summary', 'fit', 'difference', 'adoption')):
                continue
            sources = analysis.get('_sources')
            if not isinstance(sources, dict) or not sources or not analysis.get('claims'):
                continue
            for claim in analysis['claims']:
                source = sources[claim['source']]
                line = claim['line']
                quote = claim['quote']
                if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= len(source['lines']) or not isinstance(quote, str) or len(quote) < 8 or quote not in source['lines'][line-1]:
                    raise ValueError('stored evidence no longer validates')
            reviewed_at = analysis.get('_base_reviewed_at', record['at'])
            reviewed = datetime.fromisoformat(reviewed_at)
            if reviewed.tzinfo is None:
                raise ValueError('analysis timestamp must include timezone')
            age = max(0, (current - reviewed).days)
            codex = analysis.get('codex')
            if codex is not None:
                validate_codex(codex)
                review = store.get('codex-review:' + str(repo_id))
                if isinstance(review, dict) and review.get('input_hash') == record['input_hash']:
                    corrected = dict(codex)
                    corrected.update(review.get('overrides', {}))
                    validate_codex(corrected)
                    codex = corrected
                    analysis['codex'] = codex
                    analysis['_codex_main_review_at'] = review.get('at', '未记录')
            rows.append({'repo_id': repo_id, 'repo': repo, 'analysis': analysis, 'input_hash': record['input_hash'],
                         'score': score, 'reviewed_at': reviewed_at, 'age_days': age,
                         'stale': (current-reviewed).total_seconds() > 30*86400, 'codex': codex,
                         'feedback': feedback.get(repo_id, {}).get('action', '未反馈')})
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            # A malformed latest row does not revive an older flattering version.
            continue
    return sorted(rows, key=lambda r: (-r['score'], r['repo_id']))


def safe_text(text):
    import html
    return html.escape(str(text or ''), quote=False).replace('\r', ' ').replace('\n', ' ').replace('|', '\\|')


def recommendation(row, threshold=75):
    codex = row['codex']
    if row['stale']:
        return '分析超过30天，先复核'
    if not codex:
        return '部署分析待补充'
    if row['repo'].get('archived'):
        return '仓库已归档，先核查替代方案'
    if codex['integration'] == 'unknown':
        return '集成方式未明确，继续核查'
    if codex['integration'] in ('application', 'reference'):
        return '独立使用或借鉴，不直接部署'
    if codex['priority'] == '高' and row['score'] >= threshold:
        return '高价值核查候选，等你主动提出安装'
    return '继续观察，暂不建议接入'


def detail(row, threshold):
    repo, analysis, codex = row['repo'], row['analysis'], row['codex']
    lines = ['# ' + safe_text(repo['full_name']), '', repo['html_url'], '',
             f"参考评分：**{row['score']:.1f}/100**；最近分析：{row['reviewed_at']}。", '',
             '**证据等级：** 公开仓库材料支持；未安装或运行实测。', '',
             '**入选原因：** ' + safe_text(analysis['fit']), '',
             '**功能：** ' + safe_text(analysis['summary']), '',
             '**与其他工具的差异：** ' + safe_text(analysis['difference']), '',
             '**采用条件：** ' + safe_text(analysis['adoption']), '',
             '## Codex 部署分析', '',
             '**当前决策：** ' + recommendation(row, threshold), '',
             '**授权状态：** 本查找器不执行安装；仅在你主动提出安装后另行核查和部署。', '',
             '**当前环境状态：** 是否已经安装或存在相同组件尚未核验，不能由 Star、名称或 known 反馈推断。', '']
    if codex:
        lines += ['**集成类型：** ' + TYPES[codex['integration']], '',
                  '**可能接入位置：** ' + PLACEMENT[codex['integration']], '']
        if analysis.get('_codex_reviewed_at'):
            lines += ['**适配分析补充时间：** ' + analysis['_codex_reviewed_at'] + '；原有项目分析时间保持不变。', '']
        if analysis.get('_codex_main_review_at'):
            lines += ['**主代理复核：** ' + analysis['_codex_main_review_at'] + ' 校正本机重叠描述；这不等于已安装或完成运行测试。', '']
        for label, key in [('为什么值得考虑', 'reason'), ('与现有能力的重叠（未核对安装清单）', 'overlap'),
                           ('依赖与采用成本', 'requirements'), ('风险和未知', 'risk'), ('部署前下一步核查', 'next_step')]:
            lines += ['**' + label + '：** ' + safe_text(codex[key]), '']
        lines += ['以上为模型依据仓库材料提出的集成建议，具体组件路径、权限和安装方案仍待核查。', '']
    else:
        lines += ['历史分析尚无独立的 Codex 适配字段；暂不把它归为可部署候选。', '']
    lines += ['## 证据与未知', '']
    for claim in analysis['claims']:
        source = analysis['_sources'][claim['source']]
        lines += ['- ' + safe_text(claim['text']) + '：' + str(source['url']) +
                  f"（证据 {claim['source']} 第 {claim['line']} 行）"]
    lines += [''] + ['- ' + safe_text(x) for x in analysis.get('unknowns', [])]
    return '\n'.join(lines) + '\n'


def managed_write(app, path, content):
    """Update only fixed catalog paths, backing up changed existing files."""
    app, path = Path(app), Path(path)
    safe_folder(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = content.encode('utf-8')
    if path.exists():
        if path.read_bytes() == raw:
            return
        name = hashlib.sha256(str(path.relative_to(app)).encode()).hexdigest()[:12]
        backup = app/'backups/catalog'/(name + '-' + uuid.uuid4().hex + '.bak')
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(raw)
    temporary.replace(path)


def write_catalog(store, app, cfg, current=None):
    """Publishable views contain derived advice, not raw prompts/local paths/secrets."""
    current = current or datetime.now(timezone.utc)
    rows = latest_rows(store, current)
    top = rows[:50]
    threshold = cfg.get('codex_candidate_min_score', 75)
    high = [r for r in top if recommendation(r, threshold).startswith('高价值')]
    folder = Path(app)/'reports'
    header = ['# 历史优选 Top 50', '', f"累计有 {len(rows)} 个合格项目，本页展示 {len(top)} 个。",
              '只从本查找器累计分析通过的项目中排序，不是全 GitHub 排名；不足50不凑数。', '',
              '同一仓库只占一个位置；使用最新有效分析，不取过时的历史最高分。反馈忽略/暂缓/试用失败的项目不入榜。',
              '超过30天未复核会标记过期，不进入高价值部署候选。分数仅作筛选参考。', '',
              '[Codex 部署候选与规则](codex-deployment.md) · [报告入口](README.md)', '',
              '| 排名 | 项目 | 分数 | 类型 | 部署建议 | 最近分析 |', '| --- | --- | --- | --- | --- | --- |']
    for i, row in enumerate(top, 1):
        name = safe_text(row['repo']['full_name'])
        kind = TYPES[row['codex']['integration']] if row['codex'] else '部署分析待补充'
        header.append(f"| {i} | [{name}](projects/{row['repo_id']}.md) | {row['score']:.1f} | {kind} | {recommendation(row, threshold)} | {row['reviewed_at'][:10]} |")
        managed_write(app, folder/'projects'/f"{row['repo_id']}.md", detail(row, threshold))
    managed_write(app, folder/'top-50.md', '\n'.join(header)+'\n')
    deployment = ['# Codex 部署分析', '', '**规则：只提示高价值候选，等你主动说安装后再部署；不每轮询问，不自动安装。**', '',
                  '榜单排名不等于部署许可。Skill、MCP、CLI、代码库、独立应用与参考资料采用不同接入方式。',
                  '当前查找器没有安装执行入口，也不会修改 Codex 配置或执行候选仓库命令。', '',
                  f'高价值提示门槛：参考分至少 {threshold}，DS适配优先级为高，分析未超过30天；仍须核查具体组件、来源、权限、依赖、重叠与回滚方案。', '',
                  '## 高价值核查候选', '']
    if not high:
        deployment += ['目前没有同时满足条件的候选；不把资料不足的项目写成已具备安装条件。', '']
    for row in high:
        codex = row['codex']
        deployment += [f"### [{safe_text(row['repo']['full_name'])}](projects/{row['repo_id']}.md)", '',
                       '**为什么值得：** ' + safe_text(codex['reason']), '',
                       '**重叠与增量（未核对安装清单）：** ' + safe_text(codex['overlap']), '',
                       '**代价与风险：** ' + safe_text(codex['requirements'] + '；' + codex['risk']), '',
                       '**部署前核查：** ' + safe_text(codex['next_step']), '']
    deployment += ['## 不同类型如何接入', ''] + ['- **'+TYPES[k]+'：** '+v for k,v in PLACEMENT.items()]
    deployment += ['', '官方资料：[Skills](https://developers.openai.com/plugins/concepts/skills)、[Codex MCP](https://developers.openai.com/codex/mcp)。', '',
                   'Top50、部署建议和项目详情持续更新，不属于30天轮次报告清理对象；所有建议都不等于安装批准。']
    managed_write(app, folder/'codex-deployment.md', '\n'.join(deployment)+'\n')
    public = {'schema': 1, 'pool_size': len(rows), 'shown': len(top), 'capacity': 50,
              'deployment_policy': 'user_initiated_only', 'high_value_candidates': len(high), 'projects': [
                  {'repo_id': r['repo_id'], 'name': r['repo']['full_name'], 'url': r['repo']['html_url'],
                   'score': round(r['score'], 2), 'reviewed_at': r['reviewed_at'], 'stale': r['stale'],
                   'reason': r['analysis']['fit'], 'codex': r['codex'], 'deployment_advice': recommendation(r, threshold)} for r in top]}
    managed_write(app, folder/'catalog.json', json.dumps(public, ensure_ascii=False, indent=2)+'\n')
    return {'qualified_pool': len(rows), 'top_count': len(top), 'high_value_candidates': len(high)}

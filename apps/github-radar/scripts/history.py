"""Persistent full library, paged backfill and small discovery stories, without models."""
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from catalog import latest_rows, managed_write, safe_text


def query_plan(store, cfg):
    queries = cfg['queries']
    cursor = store.get('query_cursor', 0)
    count = min(cfg['searches_per_scan'], len(queries))
    picked = []
    # Every normal round can revisit an old classic and explore something playful.
    for category in ('经典回溯', '趣味探索'):
        choices = [i for i,q in enumerate(queries) if q['category'] == category]
        if choices and len(picked) < count:
            picked.append(choices[(cursor//max(1,count)) % len(choices)])
    for offset in range(len(queries)):
        if len(picked) >= count:
            break
        index = (cursor+offset) % len(queries)
        if index not in picked:
            picked.append(index)
        if len(picked) >= count:
            break
    store.put('query_cursor', cursor+count)
    return [queries[i] for i in picked]


def page_key(query, sort, per_page):
    # Rolling cutoffs must not reset pagination at midnight.
    query = re.sub(r'created:([<>])\d{4}-\d{2}-\d{2}', r'created:\1{rolling-date}', query)
    return 'search-page:' + hashlib.sha256(json.dumps([query,sort,per_page]).encode()).hexdigest()


def current_page(store, query, sort, per_page):
    return max(1, int(store.get(page_key(query,sort,per_page),1)))


def advance_page(store, query, sort, per_page, total, cap=50):
    pages = max(1,min(cap,math.ceil(min(max(0,total),1000)/per_page)))
    current = current_page(store,query,sort,per_page)
    store.put(page_key(query,sort,per_page), current % pages+1)


def library_records(store):
    valid = {r['repo_id']:r for r in latest_rows(store,include_exploration=True,include_filtered=True)}
    feedback = store.feedback()
    recorded = {row[0] for row in store.db.execute('SELECT DISTINCT repo_id FROM analyses')}
    records = []
    for row in store.db.execute('SELECT repo_id,body,first_seen,last_seen FROM repos ORDER BY repo_id'):
        repo = json.loads(row['body'])
        analysis = valid.get(row['repo_id'])
        action = feedback.get(row['repo_id'],{}).get('action')
        status = {'ignore':'已忽略','later':'暂缓','tried_failed':'试用失败'}.get(action)
        if not status:
            status = '已分析' if analysis else ('分析受阻' if store.get('analysis-blocked:'+str(row['repo_id'])) else ('需复核' if row['repo_id'] in recorded else '待分析'))
        records.append({'repo_id':row['repo_id'],'name':repo['full_name'],'url':repo['html_url'],
                        'aliases':repo.get('_known_names',[]),'description':repo.get('description') or '',
                        'topics':repo.get('topics',[]),'language':repo.get('language'),
                        'created_at':repo.get('created_at'),'pushed_at':repo.get('pushed_at'),
                        'stars':repo.get('stargazers_count',0),'archived':bool(repo.get('archived')),
                        'first_seen':row['first_seen'],'last_seen':row['last_seen'],
                        'status':status,'feedback':action,'score':round(analysis['score'],2) if analysis else None,
                        'analysis_block':store.get('analysis-blocked:'+str(row['repo_id'])) if not analysis else None,
                        'summary':analysis['analysis']['summary'] if analysis else None,
                        'reviewed_at':analysis['reviewed_at'] if analysis else None,
                        'material_limits':analysis['analysis'].get('_material_limits',[]) if analysis else [],
                        'analysis':{k:v for k,v in analysis['analysis'].items() if not k.startswith('_')} if analysis else None,
                        'evidence_urls':{k:v['url'] for k,v in analysis['analysis']['_sources'].items()} if analysis else {}})
    return records


def search_library(store, query='', status='all', limit=20):
    terms = query.casefold().split()
    rows = []
    for record in library_records(store):
        text = ' '.join([record['name'],record['description'],record['summary'] or '',
                         *record['topics'],*record['aliases']]).casefold()
        if all(term in text for term in terms) and (status=='all' or status==record['status']):
            rows.append(record)
    return {'total_matches':len(rows),'items':rows[:limit]}


def story(row):
    repo,analysis = row['repo'],row['analysis']
    text = (' '.join(repo.get('topics',[]))+' '+str(repo.get('description') or '')+' '+analysis['summary']).lower()
    playful = ('game','creative','generative-art','simulation','visualization','interactive','graph','pixel','游戏','可视化','图谱','艺术','模拟')
    curiosity = sum(1 for term in playful if term in text)
    why = analysis['summary']
    idea = '先看它的示例和演示，想想能给自己的工作或日常省掉哪一步；不用先决定是否安装。'
    if any(t in text for t in ('graph','图谱')):
        idea = '看看仓库的图谱示例：一堆文字、代码或资料被组织成图之后，会不会更容易看出联系？'
    elif any(t in text for t in ('game','游戏','pixel')):
        idea = '如果有在线演示，可以先玩一小会儿，看看玩法或实现有什么巧思，不必当成安装任务。'
    elif any(t in text for t in ('creative','generative-art','艺术')):
        idea = '先欣赏几张示例作品，挑一种喜欢的效果看看它是如何做出来的。'
    elif 'pdf' in text:
        idea = '找一篇公开论文，对照仓库演示里的阅读、翻译或排版效果，看看体验有什么不同。'
    elif any(t in text for t in ('simulation','模拟')):
        idea = '从演示里挑一个参数变化，看看它如何把现实机制变成可以探索的模拟。'
    elif 'automation' in text or '自动化' in text:
        idea = '设想一条日常小流程，看看哪些步骤能像搭积木一样串起来；先看示例就行。'
    sharing = analysis.get('sharing')
    if isinstance(sharing,dict):
        why,idea = sharing['why'],sharing['try_idea']
        curiosity = max(curiosity,sharing['curiosity'])
    created = repo.get('created_at')
    classic = False
    try:
        classic = (datetime.now(timezone.utc)-datetime.fromisoformat(created.replace('Z','+00:00'))).days>=730
    except (AttributeError,ValueError,TypeError):
        pass
    content = {'repo_id':row['repo_id'],'name':repo['full_name'],'url':repo['html_url'],
               'kind':'有趣发现' if curiosity else '实用发现','classic':classic,
               'why':why,'try_idea':idea,'reviewed_at':row['reviewed_at'],
               'note':'来自仓库材料与已有分析，尚未亲自运行体验；不是安装请求。'}
    identity = {k:content[k] for k in ('repo_id','why','try_idea')}
    content['share_id'] = hashlib.sha256(json.dumps(identity,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    return content,curiosity


def write_history(store, app):
    """The full library includes pending/ignored entries; top50 is only a view."""
    app=Path(app)
    records=library_records(store)
    folder=app/'reports/library'
    managed_write(app,folder/'index.json',json.dumps({'schema':1,'count':len(records),'projects':records},ensure_ascii=False,indent=2)+'\n')
    intro=['# 历史项目大库','','这是累计发现库，不是推荐清单。保留老项目、未分析项目和忽略记录；不随30天报告清理删除。',
           '项目按GitHub数字ID去重，保留首次/最近发现、创建时间、旧名称、分析与反馈状态。',
           '新鲜发现与经典回溯并行，按查询逐页推进；不以近期提交或创建时间作为全部来源的门槛。', '',
           f'当前累计 **{len(records)}** 个项目，其中 **{sum(r["status"]=="已分析" for r in records)}** 个状态为已分析。', '',
           '[可检索完整JSON](index.json) · [历史Top50](../top-50.md) · [有趣与实用发现](../discoveries.md)', '',
           '本地检索：`run.ps1 library --query 关键词`。网页也可在各页用浏览器查找。', '']
    pages=max(1,math.ceil(len(records)/100))
    for i in range(pages):
        name=f'page-{i+1:03d}.md'
        intro.append(f'- [第{i+1}页]({name})')
        lines=[f'# 历史库 · 第{i+1}页','','| 项目 | 状态 | 创建时间 | 简介 |','| --- | --- | --- | --- |']
        for r in records[i*100:(i+1)*100]:
            lines.append(f"| [{safe_text(r['name'])}]({r['url']}) | {r['status']} | {(r['created_at'] or '未知')[:10]} | {safe_text(r['description'][:120])} |")
        managed_write(app,folder/name,'\n'.join(lines)+'\n')
    managed_write(app,folder/'README.md','\n'.join(intro)+'\n')
    choices=[]
    for row in latest_rows(store,include_exploration=True):
        if row['stale']:
            continue
        item,curiosity=story(row)
        previous=store.get('sharing-published:'+str(row['repo_id']))
        choices.append((previous.get('at','') if previous else '',-curiosity,-row['score'],row['repo_id'],item))
    choices.sort(key=lambda x:x[:4])
    items=[entry[4] for entry in choices[:3]]
    packet={'schema':1,'repository':'whoaha77/github-radar-reports','items':items,
            'delivery_policy':'Share interesting/useful discoveries; no installation request; deduplicate chat delivery.'}
    managed_write(app,app/'reports/sharing.json',json.dumps(packet,ensure_ascii=False,indent=2)+'\n')
    stories=['# 有趣与实用发现','','这里聊好玩在哪里、能做什么；它不是待安装审核清单。老项目也可能值得重新发现。', '',
             '[历史项目大库](library/README.md) · [Top50](top-50.md) · [部署分析](codex-deployment.md)', '']
    for item in items:
        stories += [f"## [{safe_text(item['name'])}]({item['url']})",'',
                    ('**老项目回看** · ' if item['classic'] else '')+item['kind'], '',safe_text(item['why']),'',
                    '**可以怎样探索：** '+safe_text(item['try_idea']),'',item['note'],'']
    if not items:
        stories += ['本轮还没有证据充分、可分享的新发现，不为凑数推荐。']
    managed_write(app,app/'reports/discoveries.md','\n'.join(stories)+'\n')
    return {'library_size':len(records),'sharing_items':len(items)}


def mark_sharing_published(store, app):
    path=Path(app)/'reports/sharing.json'
    if not path.exists():
        return
    packet=json.loads(path.read_text(encoding='utf-8'))
    for item in packet['items']:
        store.put('sharing-published:'+str(item['repo_id']),{'share_id':item['share_id'],'at':datetime.now(timezone.utc).isoformat()})

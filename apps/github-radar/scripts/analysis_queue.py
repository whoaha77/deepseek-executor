"""Drain every discovered project, streaming validated batches with durable progress."""
import json
import time
from datetime import timezone, timedelta
from pathlib import Path
from catalog import latest_rows


def pending_repos(store):
    accepted = {r['repo_id'] for r in latest_rows(store, include_exploration=True, include_filtered=True)}
    return sorted((r for r in store.repos() if r['id'] not in accepted), key=lambda r:r['id'])


def pause_reason(store,cfg,current):
    pause = store.get('analysis-pause')
    if not pause:
        return None
    day = current.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    if (pause['kind']=='budget' and (pause['day']!=day or ('daily_budget_cny' in cfg and cfg['daily_budget_cny'] is None))) or (pause['kind']=='pricing' and pause.get('pricing')!=cfg['pricing']):
        store.put('analysis-pause',None)
        return None
    return pause['reason']


def drain(store, cfg, api, run_dir, deadline, analyze_fn, pack_fn, hash_fn, now_fn, salvage_fn, repos=None, limit=None):
    repos = pending_repos(store) if repos is None else repos
    if limit is not None:
        repos = repos[:limit]
    result = {'contexts':[], 'analyzed':0, 'cache_hits':0, 'evidence_count':0, 'errors':[]}
    paused = pause_reason(store,cfg,now_fn())
    if paused:
        result.update(errors=[paused],remaining=len(pending_repos(store)))
        return result
    batch, batch_index = [], 0
    reserve_seconds = cfg['model_timeout_seconds'] + 20
    blocked = False

    def flush():
        nonlocal batch_index, blocked
        if not batch or not cfg['model_enabled']:
            batch.clear()
            return
        packs = [c['pack'] for c in batch]
        failure = ''
        attempt_dir, attempt_index = Path(run_dir),batch_index
        try:
            for retry in range(3):
                try:
                    accepted = analyze_fn(store,packs,cfg,attempt_dir,attempt_index,deadline)
                    break
                except (ValueError,OSError) as exc:
                    log = attempt_dir/f'batch-{attempt_index}.process.log'
                    detail = log.read_text(encoding='utf-8',errors='replace') if log.exists() else ''
                    transient = 'DS调用失败' in str(exc) and any(t in detail for t in ('WinError 10054','HTTP 429','HTTP 500','HTTP 502','HTTP 503','HTTP 504'))
                    if not transient or retry==2 or time.monotonic()+reserve_seconds>=deadline:
                        raise
                    attempt_dir = Path(run_dir)/f'batch-{batch_index}-retry-{retry+1}'
                    attempt_dir.mkdir(exist_ok=False)
                    attempt_index = 0
        except (ValueError,OSError) as exc:
            failure = str(exc)
            result['errors'].append(f'分析批次{batch_index}：{failure}')
            if any(t in failure for t in ('DS调用失败','预算','价格','时间')):
                kind = 'network' if 'DS调用失败' in failure else ('pricing' if '价格' in failure else 'budget')
                if '时间' not in failure:
                    store.put('analysis-pause',{'kind':kind,'reason':failure,'pricing':cfg['pricing'],
                              'day':now_fn().astimezone(timezone(timedelta(hours=8))).date().isoformat()})
                blocked = True
                batch.clear()
                return
            try:
                accepted = salvage_fn(attempt_dir,attempt_index,packs)
            except (ValueError,OSError,TypeError,KeyError):
                accepted = {}
        for ctx in batch:
            rid = ctx['repo']['id']
            if rid in accepted:
                ctx['analysis'] = dict(accepted[rid],_material_limits=ctx.get('_material_limits',[]))
                store.save_analysis(rid,ctx['hash'],ctx['analysis'],now_fn().isoformat())
                store.put('analysis-blocked:'+str(rid),None)
                result['analyzed'] += 1
            else:
                store.put('analysis-blocked:'+str(rid),{'reason':failure or 'missing result id','at':now_fn().isoformat()})
        batch.clear()
        batch_index += 1

    prepared = {}
    for position,repo in enumerate(repos):
        if blocked or time.monotonic()+reserve_seconds >= deadline:
            break
        rid = repo['id']
        if store.get('analysis-blocked:'+str(rid)):
            continue
        try:
            if hasattr(api,'evidence_many') and position % cfg['analysis_batch_size']==0:
                group = [r for r in repos[position:position+cfg['analysis_batch_size']] if not store.get('analysis-blocked:'+str(r['id']))]
                prepared = api.evidence_many(group,char_limit=10000)
            evidence = prepared[rid] if rid in prepared else api.evidence(repo,char_limit=10000)
            if isinstance(evidence,Exception):
                raise evidence
            pack = pack_fn(evidence)
            (Path(run_dir)/f'{rid}.evidence.json').write_text(json.dumps(pack,ensure_ascii=False,indent=2),encoding='utf-8')
        except (ValueError,OSError,RuntimeError) as exc:
            result['errors'].append(f'{repo["full_name"]}取证失败：{exc}')
            # Rate/time/network blocks remain retryable next check, not permanent failures.
            blocked = True
            break
        result['evidence_count'] += 1
        ctx = {'repo':repo,'hash':hash_fn(pack,cfg),'pack':pack,'growth':0,
               'change':'即时分析','category':repo.get('_category','历史补齐'),
               '_material_limits':evidence.get('warnings',[])}
        result['contexts'].append(ctx)
        cached = store.analysis(rid,ctx['hash'])
        if cached:
            ctx['analysis'] = cached
            result['cache_hits'] += 1
        else:
            batch.append(ctx)
            if len(batch) >= cfg['analysis_batch_size']:
                flush()
    if batch and not blocked and time.monotonic()+reserve_seconds < deadline:
        flush()
    result['remaining'] = len(pending_repos(store))
    return result

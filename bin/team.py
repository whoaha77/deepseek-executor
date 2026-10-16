"""Bounded DAG orchestration over local tools, Flash text calls and Flash workers.

Default is planning only. --execute runs an explicitly reviewed plan; no self-spawning,
automatic retries or automatic model escalation. Checks validate stated properties,
not overall semantic correctness. No third-party framework dependency.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent))
from budget import resolve as resolve_budget

ENGINE = Path(__file__).resolve().parent.parent
FORBIDDEN = [Path('D:/protected-data').resolve(), Path('D:/research-raw').resolve(), (Path.home()/'Desktop').resolve()]


def under(path, root):
    return path == root or root in path.parents


def scoped(workspace, value):
    if not isinstance(value, str) or not value or any(c in value for c in '*?'):
        raise ValueError('paths must be explicit file names')
    path = (workspace/value).resolve()
    if not under(path, workspace):
        raise ValueError('path outside workspace: '+value)
    return path


def validate(plan):
    nodes = plan.get('nodes')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 12:
        raise ValueError('plan needs 1..12 nodes')
    if type(plan.get('max_parallel', 2)) is not int or not 1 <= plan.get('max_parallel', 2) <= 3:
        raise ValueError('max_parallel must be 1..3')
    if type(plan.get('timeout_sec', 240)) is not int or not 1 <= plan.get('timeout_sec', 240) <= 3600:
        raise ValueError('team timeout must be 1..3600')
    registry = {}
    for node in nodes:
        name = node.get('id','')
        if not isinstance(name,str) or not name or len(name)>60 or not all(c.isascii() and (c.isalnum() or c in '-_') for c in name) or name in registry:
            raise ValueError('invalid or duplicate node id')
        workspace = Path(node['workspace']).resolve(strict=True)
        if not workspace.is_dir() or any(under(workspace,p) for p in FORBIDDEN):
            raise ValueError('workspace is not an allowed directory')
        if node.get('backend') not in ('local','text','worker'):
            raise ValueError('backend must be local, text or worker')
        if type(node.get('timeout_sec',60)) is not int or not 1 <= node.get('timeout_sec',60) <= 600:
            raise ValueError('node timeout must be 1..600')
        if not isinstance(node.get('depends_on',[]),list) or len(set(node.get('depends_on',[]))) != len(node.get('depends_on',[])):
            raise ValueError('dependencies must be unique ids')
        if not isinstance(node.get('outputs'),list) or not isinstance(node.get('inputs',[]),list):
            raise ValueError('inputs and outputs must be lists')
        outputs = [scoped(workspace,p) for p in node.get('outputs',[])]
        inputs = [scoped(workspace,p) for p in node.get('inputs',[])]
        if len(set(outputs)) != len(outputs) or any(any(under(p,r) for r in FORBIDDEN) for p in outputs):
            raise ValueError('duplicate or protected output')
        checks = node.get('checks',[])
        if not outputs or not checks:
            raise ValueError('every node needs declared outputs and deterministic checks')
        checked = set()
        for check in checks:
            if check.get('type') not in ('nonempty','json','sha256','contains'):
                raise ValueError('unsupported check')
            target = scoped(workspace,check['path'])
            if target not in outputs:
                raise ValueError('check path must be a declared output')
            if check['type'] in ('sha256','contains') and not isinstance(check.get('value'),str):
                raise ValueError('check needs a string value')
            checked.add(target)
        if checked != set(outputs):
            raise ValueError('every output needs a check')
        if node['backend']=='text' and (len(outputs)!=1 or not isinstance(node.get('spec'),str) or not node['spec'].strip()):
            raise ValueError('text nodes need one output and a nonempty spec')
        if node['backend']=='worker' and (not isinstance(node.get('spec'),str) or not node['spec'].strip()):
            raise ValueError('worker nodes need a nonempty spec')
        if node['backend']=='worker':
            budget=resolve_budget(node)
            if len(outputs)>budget['max_output_files']:
                raise ValueError('worker packet exceeds declared file budget; split or justify a custom budget')
            if not isinstance(node.get('objective'),str) or not node['objective'].strip():
                raise ValueError('worker packet needs one explicit objective')
            acceptance=node.get('acceptance_argv')
            if not isinstance(acceptance,list) or not acceptance or not all(isinstance(x,str) for x in acceptance):
                raise ValueError('worker packet needs local acceptance_argv')
        if node['backend']=='local' and (not isinstance(node.get('argv'),list) or not node['argv'] or not all(isinstance(a,str) for a in node['argv'])):
            raise ValueError('local argv must be a nonempty string array')
        registry[name] = dict(node, _workspace=workspace, _inputs=inputs, _outputs=outputs)
        if node['backend']=='worker':registry[name]['_budget']=budget
    ancestors = {}; pending = set(registry)
    while pending:
        ready = [n for n in pending if all(d in ancestors for d in registry[n].get('depends_on',[]))]
        if not ready:
            raise ValueError('cycle or missing dependency')
        for name in ready:
            deps = registry[name].get('depends_on',[])
            ancestors[name] = set(deps).union(*(ancestors[d] for d in deps))
            pending.remove(name)
    names = list(registry)
    wave=plan.get('max_worker_packets',2)
    if type(wave) is not int or not 1<=wave<=3:
        raise ValueError('max_worker_packets must be 1..3')
    if wave>2 and (not isinstance(plan.get('wave_reason'),str) or not plan['wave_reason'].strip()):
        raise ValueError('three-worker waves need an independence/cost rationale')
    if sum(n['backend']=='worker' for n in registry.values())>wave:
        raise ValueError('too many worker packets for this review wave')
    for name,node in registry.items():
        if node['backend'] in ('worker','text') and any(registry[a]['backend']=='worker' for a in ancestors[name]):
            raise ValueError('paid work after a worker requires main-agent acceptance and a new plan')
    for i,a in enumerate(names):
        for b in names[i+1:]:
            if a in ancestors[b] or b in ancestors[a]:
                continue
            left, right = registry[a],registry[b]
            for p in left['_outputs']:
                if any(under(p,q) or under(q,p) for q in right['_inputs']+right['_outputs']):
                    raise ValueError('unordered file conflict: '+a+' / '+b)
            for p in right['_outputs']:
                if any(under(p,q) or under(q,p) for q in left['_inputs']):
                    raise ValueError('unordered file conflict: '+a+' / '+b)
    return registry


def check_outputs(node):
    evidence=[]
    for check in node['checks']:
        path=scoped(node['_workspace'],check['path'])
        if not path.is_file():
            raise ValueError('missing output: '+check['path'])
        if path.stat().st_size > 16*1024*1024: raise ValueError('output exceeds 16 MB verification limit')
        data=path.read_bytes(); digest=hashlib.sha256(data).hexdigest()
        kind=check['type']
        if kind=='nonempty' and not data.strip(): raise ValueError('empty output')
        if kind=='json': json.loads(data.decode('utf-8-sig'))
        if kind=='sha256' and digest!=check['value']: raise ValueError('hash mismatch')
        if kind=='contains' and check['value'] not in data.decode('utf-8-sig'): raise ValueError('required text missing')
        evidence.append(dict(path=check['path'],check=kind,sha256=digest))
    return evidence


def kill_tree(process):
    if os.name=='nt':
        try:
            subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.SubprocessError:
            process.kill()
    else:
        try:os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError:pass
    try: process.wait(timeout=5)
    except subprocess.TimeoutExpired: process.kill()


def run_node(node,folder,deadline):
    started=time.monotonic(); name=node['id']; spec=folder/(name+'.spec.txt')
    record={'status':'failed','backend':node['backend'],'main_agent_acceptance':'pending'}
    budget=node.get('_budget')
    def phase(value,**details):
        state=dict(phase=value,node=name,elapsed_sec=round(time.monotonic()-started,3),budget=budget,**details)
        node['_live_phase']=state
        target=folder/(name+'.phase.json');temp=target.with_suffix('.tmp')
        temp.write_text(json.dumps(state),encoding='utf-8');os.replace(temp,target)
    # Only explicit plan inputs, outputs and specification are passed; no other node history.
    contract={'objective':node.get('objective'), 'inputs':node.get('inputs',[]),'outputs':node['outputs'],
              'checks':node['checks'], 'budget':budget}
    spec.write_text(node.get('spec','')+'\nOnly modify declared outputs. No nested agents, cleanup, deletion, commits or pushes. Stop and hand back on scope/design changes. Supervisor runs acceptance separately; do not repeat its full test suite.\nContract:\n'+json.dumps(contract,ensure_ascii=False),encoding='utf-8')
    timeout=budget['implementation_sec'] if budget else node.get('timeout_sec',60)
    if node['backend']=='local': argv=node['argv']
    elif node['backend']=='text':
        argv=[sys.executable,'-I',str(ENGINE/'bin'/'ask.py'),node['spec'],'--out',str(node['_outputs'][0]),'--timeout',str(timeout),'--retries','0','--max-tokens','4096']
        if node['_inputs']: argv+=['--files',*[str(p) for p in node['_inputs']]]
    else:
        watch=folder/(name+'.watch.json');watch.write_text(json.dumps([str(p) for p in node['_outputs']]),encoding='utf-8')
        argv=['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ENGINE/'bin'/'delegate.ps1'),'-SpecFile',str(spec),'-Workspace',str(node['_workspace']),'-UnitId',name,'-TimeoutSec',str(timeout),'-MaxToolCommands',str(budget['max_commands']),'-MaxPatchFailures',str(budget['max_patch_errors']),'-IdleSec',str(budget['idle_sec']),'-BudgetProfile',budget['profile'],'-BudgetReason',budget['rationale'],'-WatchPathsFile',str(watch)]
    try:
        phase('preflight')
        # Re-resolve just before execution to catch changed links and unavailable dependency outputs.
        for value in node.get('inputs',[]):
            source=scoped(node['_workspace'],value)
            if not source.is_file(): raise ValueError('input missing: '+value)
            if node['backend']=='text' and (source.name.lower().startswith('.env') or source.name.lower() in ('config.toml','auth.json','credentials.json') or source.suffix.lower() in ('.key','.pem')):
                raise ValueError('sensitive input; use a redacted evidence file')
        for value in node['outputs']:
            target=scoped(node['_workspace'],value)
            if any(under(target,r) for r in FORBIDDEN): raise ValueError('protected output')
            if target.exists():
                if not target.is_file(): raise ValueError('output must be a file')
                # Absolute output declarations are scoped, but backups always use relative names.
                saved=folder/'backups'/name/target.relative_to(node['_workspace'])
                saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
        baseline={str(p):hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in node['_outputs']}
        phase('implementing')
        with (folder/(name+'.stdout.log')).open('wb') as out, (folder/(name+'.stderr.log')).open('wb') as err:
            if deadline<=time.monotonic():raise ValueError('team deadline exhausted before implementation launch')
            process=subprocess.Popen(argv,cwd=node['_workspace'],stdout=out,stderr=err,stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,start_new_session=os.name!='nt',
                env=dict(os.environ,DEEPSEEK_TRACE_ID=node.get('_trace_id',folder.name),DEEPSEEK_NODE_ID=name))
            allowance=0 if node['backend']=='local' else 10
            try: code=process.wait(timeout=max(0.001,min(timeout+allowance,deadline-time.monotonic())))
            except subprocess.TimeoutExpired:
                kill_tree(process); code=124
        record['exit_code']=code
        if code: record['status']='timeout' if code==124 else 'failed'
        else:
            record['evidence']=check_outputs(node)
            if node['backend']=='worker':
                record['implementation_sec']=round(time.monotonic()-started,3)
                phase('verifying',implementation_sec=record['implementation_sec'])
                remaining=min(budget['verification_sec'],deadline-time.monotonic())
                if remaining<=0: raise ValueError('packet budget exhausted before local acceptance')
                before_verifier={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in node['_outputs']}
                verification_started=time.monotonic()
                with (folder/(name+'.acceptance.stdout.log')).open('wb') as out, (folder/(name+'.acceptance.stderr.log')).open('wb') as err:
                    if deadline<=time.monotonic():raise ValueError('team deadline exhausted before verification launch')
                    verifier=subprocess.Popen(node['acceptance_argv'],cwd=node['_workspace'],stdout=out,stderr=err,
                        stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                        start_new_session=os.name!='nt')
                    try: verified=verifier.wait(timeout=max(0.001,remaining))
                    except subprocess.TimeoutExpired:
                        kill_tree(verifier);verified=124
                record['acceptance_exit_code']=verified
                record['verification_sec']=round(time.monotonic()-verification_started,3)
                if verified: raise ValueError('local acceptance failed; main-agent handoff required')
                record['evidence']=check_outputs(node)
                if any(hashlib.sha256(p.read_bytes()).hexdigest()!=before_verifier[str(p)] for p in node['_outputs']):
                    raise ValueError('acceptance mutated declared source outputs; inspect diff')
                record['handoff']='local checks passed; inspect diff before approving next paid wave'
            record['status']='done'
        record['declared_changes']=[str(p.relative_to(node['_workspace'])) for p in node['_outputs']
            if (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)!=baseline[str(p)]]
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        record['error']=str(error)
    record['duration_sec']=round(time.monotonic()-started,3)
    phase(record['status'],error=record.get('error'),main_agent_acceptance='pending')
    return record


def execute(plan,registry,folder):
    folder.mkdir(parents=True,exist_ok=False)
    started=time.monotonic(); deadline=started+plan.get('timeout_sec',240)
    trace_id='team-'+uuid.uuid4().hex
    registry={key:dict(node,_trace_id=trace_id) for key,node in registry.items()}
    records={};pending=set(registry);running={}
    def checkpoint():
        state={'nodes':records,'trace_id':trace_id,'duration_sec':round(time.monotonic()-started,3),
               'plan_sha256':hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest(),
               'main_agent_acceptance':'pending','resume_reuses_results':False}
        state['running']={name:registry[name].get('_live_phase',{'phase':'starting'})
                          for name in running.values()}
        temp=folder/'checkpoint.tmp';temp.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8');os.replace(temp,folder/'checkpoint.json')
    with ThreadPoolExecutor(max_workers=plan.get('max_parallel',2)) as executor:
        while pending or running:
            for name in sorted(list(pending)):
                deps=registry[name].get('depends_on',[])
                if any(d in records and records[d]['status']!='done' for d in deps):
                    records[name]={'status':'blocked_dependency'};pending.remove(name)
                elif time.monotonic()>=deadline:
                    records[name]={'status':'team_timeout'};pending.remove(name)
                elif len(running)<plan.get('max_parallel',2) and all(d in records and records[d]['status']=='done' for d in deps):
                    running[executor.submit(run_node,registry[name],folder,deadline)]=name;pending.remove(name)
            if running:
                done,_=wait(running,return_when=FIRST_COMPLETED,timeout=1)
                for future in done:
                    name=running.pop(future)
                    try: records[name]=future.result()
                    except Exception as error: records[name]={'status':'failed','error':str(error)}
            checkpoint()
    return 0 if all(r['status']=='done' for r in records.values()) else 1


def main():
    p=argparse.ArgumentParser();p.add_argument('--plan',required=True);p.add_argument('--execute',action='store_true');p.add_argument('--out-dir')
    args=p.parse_args();plan=json.loads(Path(args.plan).read_text(encoding='utf-8-sig'));registry=validate(plan)
    if not args.execute:
        warnings=[k+': phase estimates exceed team deadline; choose an explicit feasible plan'
                  for k,n in registry.items() if '_budget' in n and
                  n['_budget']['implementation_sec']+n['_budget']['verification_sec']>plan.get('timeout_sec',240)]
        print(json.dumps({'valid':True,'nodes':len(registry),'max_parallel':plan.get('max_parallel',2),'timeout_sec':plan.get('timeout_sec',240),'worker_budgets':{k:n['_budget'] for k,n in registry.items() if '_budget' in n},'warnings':warnings,'execution':'not started'},ensure_ascii=False));return 0
    folder=Path(args.out_dir).resolve() if args.out_dir else ENGINE/'logs'/'teams'/('team-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    if any(under(folder,r) for r in FORBIDDEN): raise ValueError('protected artifact directory')
    result=execute(plan,registry,folder);print('Team checkpoint: '+str(folder/'checkpoint.json'));return result


if __name__=='__main__':
    try: sys.exit(main())
    except (ValueError,OSError,KeyError,TypeError) as error:
        print('[team] '+str(error),file=sys.stderr);sys.exit(2)

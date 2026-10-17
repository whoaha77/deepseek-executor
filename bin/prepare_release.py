"""Prepare a reviewed public-source export in a fresh checkout; never commit or push."""
import argparse,hashlib,json,shutil,subprocess,re
from pathlib import Path

def prepare(export,out,repository,tag):
    if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repository):raise ValueError('invalid repository')
    if not re.fullmatch(r'v\d+\.\d+\.\d+',tag):raise ValueError('invalid tag')
    export=Path(export).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('new output directory required')
    m=json.loads((export/'manifest.json').read_text(encoding='utf-8'))
    if m.get('main_agent_acceptance')!='accepted':raise ValueError('export not accepted')
    expected={}
    for item in m['files']:
        name=item['path'];p=Path(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name or '.git' in p.parts:raise ValueError('unsafe manifest path')
        if name in expected:raise ValueError('duplicate manifest path')
        src=(export/'repository'/p).resolve()
        if not src.is_relative_to(export/'repository') or src.is_symlink():raise ValueError('source escape')
        if hashlib.sha256(src.read_bytes()).hexdigest()!=item['sha256']:raise ValueError('source hash mismatch')
        expected[name]=item['sha256']
    if not expected:raise ValueError('empty manifest')
    out.mkdir(parents=True);repo=out/'repository'
    subprocess.run(['git','clone','--quiet',repository+'.git',str(repo)],check=True,timeout=120)
    def git(*args):return subprocess.check_output(['git','-C',str(repo),*args],encoding='utf-8',stderr=subprocess.DEVNULL,timeout=120).strip()
    git('config','core.quotepath','false')
    if git('branch','--show-current')!='main' or git('status','--porcelain'):raise ValueError('clean main required')
    if set(git('ls-files').splitlines())-set(expected):raise ValueError('remote extra paths require reconciliation')
    base=git('rev-parse','HEAD')
    for field,fmt in [('user.name','%an'),('user.email','%ae')]:git('config',field,git('log','-1','--format='+fmt))
    git('config','core.hooksPath','.git/hooks');git('config','core.autocrlf','false')
    for name in expected:
        dst=repo/name;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(export/'repository'/name,dst)
    git('add','--',*sorted(expected))
    (out/'publication.diff').write_text(git('diff','--cached','--no-ext-diff','--no-textconv'),encoding='utf-8')
    plan=out/'plan.json';plan.write_text(json.dumps(dict(repository=repository,base=base,branch='main',tag=tag,files=expected,export=str(export)),indent=2),encoding='utf-8')
    return dict(status='prepared',plan=str(plan),files=len(expected),committed=False)

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('export','out','repository','tag'):p.add_argument('--'+name,required=True)
    a=p.parse_args()
    try:print(json.dumps(prepare(a.export,a.out,a.repository,a.tag)))
    except Exception as e:print(json.dumps(dict(error=type(e).__name__)));raise SystemExit(1)

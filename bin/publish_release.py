"""Publish the already-reviewed export to the existing repository; never force push."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import argparse
p=argparse.ArgumentParser(description='Publish an explicitly accepted export using a prepared plan; no automatic retries.')
p.add_argument('--plan',required=True,type=Path)
p.add_argument('--acceptance',required=True,type=Path)
p.add_argument('--title',required=True)
p.add_argument('--body-file',required=True,type=Path)
p.add_argument('--check',action='store_true',help='Validate only; no commit, push or release')
a=p.parse_args()
PLAN=json.loads(a.plan.read_text(encoding='utf-8'))
OUT=a.plan.resolve().parent
REPO=OUT/'repository'
EXPORT=Path(PLAN['export']);TAG=PLAN['tag']
parts=urllib.parse.urlsplit(PLAN['repository'])
if parts.scheme!='https' or parts.netloc!='github.com' or parts.query or parts.fragment:raise RuntimeError('unexpected repository')
pieces=parts.path.strip('/').split('/')
if len(pieces)!=2 or not all(x and all(c.isalnum() or c in '-_.' for c in x) for x in pieces):raise RuntimeError('unexpected repository')
OWNER,NAME=pieces
body=a.body_file.read_text(encoding='utf-8')
if not a.title.strip() or not body.strip():raise RuntimeError('empty release description')

env=dict(os.environ,GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never')
token=''
def git(*args):
    r=subprocess.run(['git','-C',str(REPO),*args],env=env,capture_output=True,text=True,encoding='utf-8',timeout=120)
    if r.returncode:raise RuntimeError(('git '+args[0]+' failed: '+r.stderr[-1200:]).replace(token,'[redacted]') if token else 'git '+args[0]+' failed')
    return r.stdout.strip()
def api(path,method='GET',data=None,binary=False,allow404=False,auth=True):
    url=path if path.startswith('https://') else 'https://api.github.com'+path
    if urllib.parse.urlparse(url).hostname not in {'api.github.com','uploads.github.com'}:raise RuntimeError('unexpected API host')
    headers={'Accept':'application/vnd.github+json','User-Agent':'deepseek-executor-publisher',
             'Content-Type':'application/zip' if binary else 'application/json'}
    if auth:headers['Authorization']='Bearer '+token
    payload=data if binary else json.dumps(data).encode('utf-8') if data is not None else None
    request=urllib.request.Request(url,data=payload,method=method,headers=headers)
    try:
        with urllib.request.urlopen(request,timeout=60) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code==404 and allow404:return None
        try:message=json.loads(error.read()).get('message','GitHub request failed')
        except Exception:message='GitHub request failed'
        raise RuntimeError(f'GitHub HTTP {error.code}: {message}'.replace(token,'[redacted]')) from None

# All integrity and remote-state checks precede mutations.
if git('remote','get-url','origin')!=PLAN['repository']+'.git':raise RuntimeError('remote changed')
git('fetch','origin','main')
if git('rev-parse','FETCH_HEAD')!=PLAN['base'] or git('rev-parse','HEAD')!=PLAN['base']:raise RuntimeError('base changed; reconcile first')
if git('branch','--show-current')!='main':raise RuntimeError('branch changed')
expected=PLAN['files']
if set(git('ls-files').splitlines())!=set(expected):raise RuntimeError('unexpected tracked paths')
for name,digest in expected.items():
    if hashlib.sha256((REPO/name).read_bytes()).hexdigest()!=digest:raise RuntimeError('source changed: '+name)
    staged=git('rev-parse',':'+name)
    if staged!=git('hash-object','--',name):raise RuntimeError('stage changed: '+name)
acceptance=json.loads(a.acceptance.read_text(encoding='utf-8'))
archive=EXPORT/'deepseek-executor-share.zip'
if acceptance['main_agent_acceptance']!='accepted' or hashlib.sha256(archive.read_bytes()).hexdigest()!=acceptance['archive_sha256']:raise RuntimeError('acceptance/hash mismatch')
with zipfile.ZipFile(archive) as bundle:
    names=bundle.namelist()
    if len(names)!=len(set(names)) or set(names)!={'deepseek-executor/'+n for n in expected}:raise RuntimeError('archive paths mismatch')
    for name,digest in expected.items():
        h=hashlib.sha256()
        with bundle.open('deepseek-executor/'+name) as member:
            for chunk in iter(lambda:member.read(1024*1024),b''):h.update(chunk)
        if h.hexdigest()!=digest:raise RuntimeError('archive member hash mismatch')
if git('ls-remote','--tags','origin','refs/tags/'+TAG):raise RuntimeError('tag already exists; preserve release')

credential=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',capture_output=True,text=True,env=env,timeout=20)
values=dict(line.split('=',1) for line in credential.stdout.splitlines() if '=' in line)
token=values.get('password','')
if credential.returncode or not token:raise RuntimeError('No usable cached GitHub credential')
if api('/user')['login']!=OWNER:raise RuntimeError('credential owner mismatch')
metadata=api(f'/repos/{OWNER}/{NAME}')
if metadata['private'] or metadata['default_branch']!='main':raise RuntimeError('repository settings changed')
if api(f'/repos/{OWNER}/{NAME}/releases/tags/{TAG}',allow404=True) is not None:raise RuntimeError('release already exists')

if a.check:
    print(json.dumps(dict(status='validated',mutated=False,files=len(expected),tag=TAG)));raise SystemExit(0)

record=dict(repository=PLAN['repository'],base=PLAN['base'],tag=TAG,stage='verified')
def save():
    (OUT/'publication.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
save()
git('-c','commit.gpgsign=false','commit','-m',a.title)
commit=git('rev-parse','HEAD');record.update(stage='committed',commit=commit);save()
git('push','origin','main');record['stage']='pushed';save()
if api(f'/repos/{OWNER}/{NAME}/git/ref/heads/main')['object']['sha']!=commit:raise RuntimeError('remote commit mismatch')
tree=api(f'/repos/{OWNER}/{NAME}/git/trees/{commit}?recursive=1')
if tree.get('truncated'):raise RuntimeError('remote tree truncated')
actual={item['path']:item['sha'] for item in tree['tree'] if item['type']=='blob'}
local={name:git('hash-object','--',name) for name in expected}
if actual!=local:raise RuntimeError('remote tree mismatch')
release=api(f'/repos/{OWNER}/{NAME}/releases','POST',dict(tag_name=TAG,target_commitish=commit,
    name='DeepSeek Executor '+TAG,draft=False,prerelease=False,
    body=body))
record.update(stage='release_created',release=release['html_url']);save()
asset=api(release['upload_url'].split('{',1)[0]+'?name='+urllib.parse.quote(archive.name),'POST',archive.read_bytes(),binary=True)
record.update(stage='published',download=asset['browser_download_url'],archive_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),files=len(expected));save()
if git('status','--porcelain'):raise RuntimeError('checkout not clean')
# Anonymous reads independently verify public availability and the uploaded asset bytes.
try:
    public=api(f'/repos/{OWNER}/{NAME}/releases/tags/{TAG}',auth=False)
except RuntimeError as error:
    if 'GitHub HTTP 403' not in str(error) or 'API rate limit exceeded' not in str(error):raise
    public=None
    record['anonymous_release_api_note']='Anonymous metadata rate limited; full anonymous asset download is still required.'
record['anonymous_release_api_verified']=public is not None
if public is not None and public['tag_name']!=TAG:raise RuntimeError('anonymous release mismatch')
with urllib.request.urlopen(urllib.request.Request(record['download'],headers={'User-Agent':'deepseek-executor-publisher'}),timeout=60) as response:
    downloaded=hashlib.sha256()
    for chunk in iter(lambda:response.read(1024*1024),b''):downloaded.update(chunk)
if downloaded.hexdigest()!=record['archive_sha256']:raise RuntimeError('download hash mismatch')
record['anonymous_download_verified']=True;record['stage']='verified_public';save()
print(json.dumps(record))

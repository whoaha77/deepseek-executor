"""Start the pinned official DSH Web app independently of a Codex turn."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import webbrowser

sys.path.insert(0,str(Path(__file__).resolve().parent))
from dsh_bridge import API, BridgeError, PROTECTED_ROOTS, _is_within

ROOT=Path(__file__).resolve().parents[1]
VERSION='0.2.0-rc.2'

def start(state,home=None,port=3088,open_browser=False):
    state=Path(state).resolve()
    if type(port) is not int or not 1<=port<=65535:raise BridgeError('invalid port')
    if any(_is_within(state,p) for p in PROTECTED_ROOTS):raise BridgeError('protected state')
    previous={}
    metadata=state/'process.json'
    if metadata.exists():
        previous=json.loads(metadata.read_text(encoding='utf-8'))
        try:
            api=API(state);api.rpc('session/modelCatalog',{})
        except Exception:
            pass
        else:
            if home and Path(home).resolve()!=Path(previous['home']).resolve():raise BridgeError('active home differs')
            if open_browser:webbrowser.open(api.launch)
            return dict(status='ready',reused=True,base_url=api.base,pid=previous['pid'])
    home=Path(home or previous.get('home') or os.environ.get('DSH_RELAY_HOME') or state/'home').resolve()
    if any(_is_within(home,p) for p in PROTECTED_ROOTS):raise BridgeError('protected home')
    runtime=ROOT/'state/dsh/runtime/node_modules/@deepseek-ai/dsh'
    package=runtime/'package.json'
    if not package.is_file() or json.loads(package.read_text())['version']!=VERSION:
        raise BridgeError('install pinned official runtime first; see docs/dsh-relay.md')
    node=shutil.which('node')
    if not node:raise BridgeError('Node.js is unavailable')
    with socket.socket() as probe:probe.bind(('127.0.0.1',port))
    from ask import load_key
    env=dict(os.environ,DSH_HOME=str(home),DEEPSEEK_API_KEY=load_key())
    env.pop('OPENAI_API_KEY',None)
    state.mkdir(parents=True,exist_ok=True);home.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
    stdout=state/'web.stdout.log';stderr=state/'web.stderr.log'
    for path in (metadata,stdout,stderr):
        if path.exists():shutil.copy2(path,path.with_name(path.name+'.bak-'+stamp))
    flags=(subprocess.CREATE_NO_WINDOW|subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
    with stdout.open('wb') as out,stderr.open('wb') as err:
        child=subprocess.Popen([node,str(runtime/'lib/bin.js'),'--profile','web','--no-open','--port',str(port)],
            cwd=ROOT,env=env,stdin=subprocess.DEVNULL,stdout=out,stderr=err,
            creationflags=flags,start_new_session=(os.name!='nt'))
    metadata.write_text(json.dumps(dict(pid=child.pid,port=port,home=str(home),cwd=str(ROOT),started=time.time())),encoding='utf-8')
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        if child.poll() is not None:raise BridgeError('DSH exited; inspect private stderr log')
        try:
            api=API(state);api.rpc('session/modelCatalog',{})
        except Exception:
            time.sleep(.5)
        else:
            if open_browser:webbrowser.open(api.launch)
            return dict(status='ready',reused=False,base_url=api.base,pid=child.pid)
    raise BridgeError('startup not yet ready; preserve logs and retry status, not a second launch')

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state',type=Path,default=ROOT/'state/dsh')
    p.add_argument('--home',type=Path)
    p.add_argument('--port',type=int,default=3088)
    p.add_argument('--open',action='store_true')
    a=p.parse_args()
    try:result=start(a.state,a.home,a.port,a.open)
    except Exception as error:
        print('DSH startup failed: '+type(error).__name__+'; inspect local prerequisites and private logs.',file=sys.stderr)
        return 1
    print(json.dumps(result,ensure_ascii=False));return 0

if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())


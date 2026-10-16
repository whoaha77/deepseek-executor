"""Explicit local setup; default is preview. No network or model calls."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parent
def write(path,text):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists():
        stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        shutil.copy2(path,path.with_name(path.name+'.bak-'+stamp))
    path.write_text(text,encoding='utf-8')
def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--configure',action='store_true')
    p.add_argument('--install-skill',action='store_true')
    p.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex'))))
    a=p.parse_args()
    if not a.configure and not a.install_skill:
        print(json.dumps(dict(config=str(ROOT/'worker-home/config.toml'),skill=str(a.codex_home/'skills/deepseek-executor'),action='preview only')))
        return
    if a.configure:
        key=os.environ.get('DEEPSEEK_API_KEY','').strip()
        if not key or key=='YOUR_DEEPSEEK_API_KEY':p.error('Set your own DEEPSEEK_API_KEY for this shell; its value will not be printed.')
        template=(ROOT/'worker-home/config.example.toml').read_text(encoding='utf-8')
        write(ROOT/'worker-home/config.toml',template.replace('"YOUR_DEEPSEEK_API_KEY"',json.dumps(key)))
        print('Local worker config written. No network request made.')
    if a.install_skill:
        for source in (ROOT/'codex-skill').rglob('*.md'):
            target=a.codex_home/'skills/deepseek-executor'/source.relative_to(ROOT/'codex-skill')
            write(target,source.read_text(encoding='utf-8').replace('{{ENGINE_ROOT}}',ROOT.as_posix()))
        print('Skill installed; existing files backed up. Global AGENTS.md unchanged.')
if __name__=='__main__':main()

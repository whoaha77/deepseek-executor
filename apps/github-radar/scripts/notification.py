"""Event-driven Windows notifications of published DS-written discoveries; no GPT."""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def eligible(items, delivered, current):
    result = []
    for item in items:
        previous = delivered.get(str(item['repo_id']))
        if previous:
            # No repeated content, and at least 14 days between the same project.
            if previous.get('share_id') == item['share_id']:
                continue
            try:
                if (current-datetime.fromisoformat(previous['at'])).total_seconds() < 14*86400:
                    continue
            except (KeyError, ValueError, TypeError):
                continue
        result.append(item)
    return result[:3]


def notify_discoveries(store, app, cfg):
    if not cfg.get('desktop_notifications', True) or sys.platform != 'win32':
        return {'status':'disabled_or_unsupported','gpt_calls':0}
    current = datetime.now(timezone.utc)
    app = Path(app)
    try:
        packet = json.loads((app/'reports/sharing.json').read_text(encoding='utf-8'))
        delivered = store.get('notification-delivered', {})
        items = eligible(packet['items'], delivered, current)
        if not items:
            return {'status':'unchanged','gpt_calls':0}
        # Snapshot the exact selection; the PowerShell script reads data, never evaluates it.
        out = app/'output/notification.json'
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({'items':items},ensure_ascii=False),encoding='utf-8')
        response = subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-WindowStyle','Hidden',
                                   '-ExecutionPolicy','Bypass','-File',str(app/'scripts/notify.ps1'),
                                   '-PacketPath',str(out)],capture_output=True,timeout=20)
        if response.returncode:
            return {'status':'failed','reason':'Windows toast submission failed; published sharing page remains available','gpt_calls':0}
        for item in items:
            delivered[str(item['repo_id'])] = {'share_id':item['share_id'],'at':current.isoformat()}
        store.put('notification-delivered',delivered)
        return {'status':'submitted_to_windows','projects':len(items),'gpt_calls':0,
                'note':'Visibility depends on Windows notification settings; not proof of being read'}
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired):
        return {'status':'failed','reason':'Notification unavailable; published sharing page remains available','gpt_calls':0}

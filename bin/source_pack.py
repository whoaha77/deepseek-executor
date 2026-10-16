"""Collect explicit public HTTPS references without model calls. Content is untrusted."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import urllib.request
from urllib.parse import urlsplit

HOSTS = {'developers.openai.com', 'platform.openai.com', 'www.anthropic.com',
         'docs.anthropic.com', 'github.com', 'raw.githubusercontent.com'}
TYPES = {'text/html', 'text/plain', 'application/json', 'text/markdown'}


def validate_url(url):
    u = urlsplit(url)
    if (u.scheme != 'https' or u.hostname not in HOSTS or u.username is not None
            or u.password is not None or u.port not in (None, 443)):
        raise ValueError('URL outside public HTTPS allowlist')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Visible(HTMLParser):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'nav'):
            self.depth += 1
        if not self.depth and tag in ('p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'pre'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'nav') and self.depth:
            self.depth -= 1
        if not self.depth:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.depth:
            self.parts.append(data)


def load_manifest(path):
    entries = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(entries, list) or not 1 <= len(entries) <= 12:
        raise ValueError('manifest must contain 1..12 entries')
    ids = set()
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', item['id'])
                or item['id'].lower() in ids):
            raise ValueError('invalid or duplicate id')
        if item['id'].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10))}:
            raise ValueError('reserved filename')
        ids.add(item['id'].lower())
        validate_url(item['url'])
    return entries


def fetch(item, out, timeout, max_bytes):
    record = dict(item, fetched_at=datetime.now(timezone.utc).isoformat(), status='failed')
    try:
        opener = urllib.request.build_opener(Redirect())
        req = urllib.request.Request(item['url'], headers={'User-Agent': 'EvidenceCollector/1.0'})
        with opener.open(req, timeout=timeout) as response:
            record['final_url'] = validate_url(response.url)
            if response.headers.get_content_type() not in TYPES:
                raise ValueError('unsupported content type')
            # Single bounded read; timeout is socket-operation timeout, not a global deadline.
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError('source exceeds byte limit')
            declared = response.headers.get('Content-Length')
            if declared is not None and len(raw) != int(declared):
                raise ValueError('incomplete response body')
            text = raw.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
            if response.headers.get_content_type() == 'text/html':
                parser = Visible()
                parser.feed(text)
                text = ''.join(parser.parts)
        lines = [' '.join(line.split()) for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError('empty extracted source')
        content = '\n'.join(f'{i}: {line}' for i, line in enumerate(lines, 1)) + '\n'
        target = out / (item['id'] + '.txt')
        target.write_text(content, encoding='utf-8')
        record.update(status='ok', bytes=len(raw), lines=len(lines), path=target.name,
                      raw_sha256=hashlib.sha256(raw).hexdigest(),
                      sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    except Exception as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
    return record


def collect(entries, out, workers=2, timeout=15, max_bytes=500000):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        records = list(pool.map(lambda item: fetch(item, out, timeout, max_bytes), entries))
    report = dict(untrusted_reference=True, main_agent_acceptance='pending', sources=records,
                  timeout_semantics='per socket operation; no whole-run deadline')
    (out / 'manifest.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', required=True)
    p.add_argument('--out-dir', required=True)
    p.add_argument('--workers', type=int, choices=range(1, 4), default=2)
    p.add_argument('--timeout', type=int, choices=range(1, 31), default=15)
    p.add_argument('--max-bytes', type=int, default=500000)
    a = p.parse_args()
    if not 1 <= a.max_bytes <= 2000000:
        p.error('max-bytes must be 1..2000000')
    entries = load_manifest(a.manifest)
    rows = collect(entries, a.out_dir, a.workers, a.timeout, a.max_bytes)
    failed = sum(r['status'] != 'ok' for r in rows)
    print(json.dumps(dict(ok=len(rows)-failed, failed=failed,
                          manifest=str(Path(a.out_dir)/'manifest.json'))))
    return int(bool(failed))


if __name__ == '__main__':
    raise SystemExit(main())

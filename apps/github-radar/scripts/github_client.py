"""Bounded public GitHub reads; credentials never enter evidence or logs."""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


class FetchError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials to a URL supplied by a remote response.
        return None


def github_token():
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if token:
        return token, 'environment'
    try:
        result = subprocess.run(
            ['git', 'credential', 'fill'], input='protocol=https\nhost=github.com\n\n',
            capture_output=True, text=True, timeout=8,
            env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never'))
        fields = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if result.returncode == 0 and fields.get('password'):
            return fields['password'], 'git-credential-manager'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None, 'anonymous'


class GitHub:
    def __init__(self, store, deadline, max_requests=110, credentials=None):
        self.store, self.deadline = store, deadline
        self.token, self.auth_source = credentials if credentials is not None else github_token()
        self.requests = 0
        self.max_requests = max_requests if self.token else min(max_requests, 35)
        self.next_search = 0.0
        self.rate = {}
        self.opener = urllib.request.build_opener(NoRedirect())

    def get(self, path, params=None, ttl=3600):
        if not path.startswith('/') or path.startswith('//'):
            raise FetchError('invalid API path')
        url = 'https://api.github.com' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        cached = self.store.cache(url)
        now = datetime.now(timezone.utc)
        if cached and (now - datetime.fromisoformat(cached['at'])).total_seconds() < ttl:
            return cached['body'], url
        for attempt in range(2):
            if self.requests >= self.max_requests or time.monotonic() + 2 >= self.deadline:
                raise FetchError('本轮请求或时间预算已用完')
            delay = max(0, self.next_search - time.monotonic()) if path.startswith('/search/') else 0
            if time.monotonic() + delay + 2 >= self.deadline:
                raise FetchError('搜索排队超出本轮时间预算')
            if delay:
                time.sleep(delay)
            headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'PersonalGitHubRadar/0.1'}
            if self.token:
                headers['Authorization'] = 'Bearer ' + self.token
            if cached and cached.get('etag'):
                headers['If-None-Match'] = cached['etag']
            self.requests += 1
            if path.startswith('/search/'):
                self.next_search = time.monotonic() + (2.2 if self.token else 6.2)
            try:
                with self.opener.open(urllib.request.Request(url, headers=headers),
                                      timeout=min(18, max(1, self.deadline - time.monotonic()))) as response:
                    raw = response.read(1200001)
                    self.rate[response.headers.get('x-ratelimit-resource', 'core')] = {
                        'remaining': response.headers.get('x-ratelimit-remaining'),
                        'reset': response.headers.get('x-ratelimit-reset')}
                    if len(raw) > 1200000:
                        raise FetchError('API 响应超出体积上限')
                    body = json.loads(raw)
                    self.store.save_cache(url, response.headers.get('etag'), body, now.isoformat())
                    return body, url
            except urllib.error.HTTPError as exc:
                if exc.code == 304 and cached:
                    self.store.save_cache(url, cached['etag'], cached['body'], now.isoformat())
                    return cached['body'], url
                remaining = exc.headers.get('x-ratelimit-remaining')
                wait = exc.headers.get('retry-after')
                limited = exc.code == 429 or (exc.code == 403 and (remaining == '0' or wait))
                if limited:
                    try:
                        seconds = float(wait) if wait else max(60, float(exc.headers.get('x-ratelimit-reset', '0')) - time.time())
                    except ValueError:
                        seconds = 60
                    # Long waits belong to a later run; never spin on rate limits.
                    if attempt == 0 and seconds <= 45 and time.monotonic() + seconds + 20 < self.deadline:
                        time.sleep(max(1, seconds))
                        continue
                    raise FetchError('GitHub 限流；保留本轮已取得结果') from None
                if exc.code in (500, 502, 503, 504) and attempt == 0:
                    time.sleep(1)
                    continue
                raise FetchError('GitHub HTTP ' + str(exc.code)) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == 0 and time.monotonic() + 20 < self.deadline:
                    time.sleep(1)
                    continue
                raise FetchError('GitHub 网络读取失败：' + type(exc).__name__) from None
        raise FetchError('GitHub 请求未完成')

    def evidence_many(self, repos, char_limit=10000):
        """Three independent reads, each with its own SQLite connection; no paid workers."""
        from concurrent.futures import ThreadPoolExecutor
        from radar_store import Store
        def fetch(repo):
            store = Store(self.store.path)
            client = GitHub(store,self.deadline,max_requests=6,credentials=(self.token,self.auth_source))
            try:
                try:
                    value = client.evidence(repo,char_limit=char_limit,include_release=False)
                except (FetchError,ValueError,OSError) as exc:
                    value = exc
                return repo['id'],value,client.requests,client.rate
            finally:
                store.close()
        results = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            for rid,value,requests,rate in pool.map(fetch,repos):
                self.requests += requests
                self.rate.update(rate)
                results[rid] = value
        return results

    def evidence(self, repo, char_limit=18000, include_release=True):
        """Small, versioned evidence pack. No repository code is executed."""
        name = repo['full_name']
        if not isinstance(name, str) or len(name.split('/')) != 2:
            raise FetchError('invalid repository name')
        api_name = '/'.join(urllib.parse.quote(part, safe='') for part in name.split('/'))
        base = '/repos/' + api_name
        pieces, warnings = [], []
        try:
            data, url = self.get(base + '/readme')
            if data.get('encoding') != 'base64' or not isinstance(data.get('content'), str):
                raise FetchError('README encoding unsupported')
            raw = base64.b64decode(data['content'], validate=False)
            text = raw.decode('utf-8', errors='replace')
            truncated = len(text) > char_limit
            text = text[:char_limit]
            pieces.append({'id': 'readme', 'url': data.get('html_url', repo['html_url']),
                           'api_url': url, 'blob_sha': data.get('sha'), 'truncated': truncated,
                           'lines': [line[:1200] for line in text.splitlines()[:240]]})
            if truncated or len(text.splitlines()) > 240:
                warnings.append('README 已按长度/行数截取，未覆盖全文')
        except (FetchError, ValueError) as exc:
            warnings.append(str(exc))
        try:
            releases, url = self.get(base + '/releases', {'per_page': 1}) if include_release else ([], '')
            if releases:
                item = releases[0]
                release_lines = ['tag: ' + str(item.get('tag_name') or ''),
                                 'published: ' + str(item.get('published_at') or '')]
                release_lines += str(item.get('body') or '')[:3000].splitlines()[:60]
                pieces.append({'id': 'release', 'url': item.get('html_url', repo['html_url']),
                               'api_url': url, 'lines': release_lines})
        except FetchError as exc:
            warnings.append(str(exc))
        # Metadata is authoritative API data, separate from model interpretation.
        meta = {key: repo.get(key) for key in ('id', 'full_name', 'description', 'topics', 'language',
                'license', 'archived', 'fork', 'created_at', 'pushed_at', 'stargazers_count', 'homepage')}
        pieces.insert(0, {'id': 'metadata', 'url': repo['html_url'],
                         'lines': [json.dumps(meta, ensure_ascii=False)]})
        return {'repo_id': repo['id'], 'full_name': name, 'pieces': pieces, 'warnings': warnings}

"""Publish only explicitly owned report paths to an already configured private repo."""
import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from github_client import NoRedirect, github_token


class PublishError(RuntimeError):
    pass


def git(root, *args, timeout=45):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                            encoding='utf-8', errors='replace', timeout=timeout,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never'))
    if result.returncode:
        # Git output can contain credential-bearing URLs; never expose it verbatim.
        raise PublishError('Git 操作失败：' + args[0] + '；原有文件与提交已保留')
    return result.stdout.strip()


def verify_remote(root, repository):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise PublishError('invalid target repository')
    expected = 'https://github.com/' + repository + '.git'
    if git(root, 'remote', 'get-url', 'origin').casefold() != expected.casefold():
        raise PublishError('origin 与已配置的目标仓库不一致，停止发布')
    if git(root, 'branch', '--show-current') != 'main':
        raise PublishError('只允许发布 main 分支')
    token, _ = github_token()
    if not token:
        raise PublishError('GitHub 身份凭证不可用')
    req = urllib.request.Request('https://api.github.com/repos/' + repository,
                                 headers={'Authorization': 'Bearer ' + token,
                                          'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'PersonalGitHubRadar/0.1'})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            body = json.loads(response.read(100000))
    except (OSError, urllib.error.URLError, ValueError):
        raise PublishError('无法核实目标仓库可见性，停止发布') from None
    if body.get('full_name', '').casefold() != repository.casefold() or body.get('private') is not True:
        raise PublishError('目标不是已授权的私有仓库，停止发布')


def prepare(root, repository):
    verify_remote(root, repository)
    # Preserve all local work; a conflict is a failure, never a reset/force push.
    git(root, 'fetch', 'origin', 'main')
    git(root, 'merge', '--ff-only', 'origin/main')


def publish_reports(root, repository, run_id):
    verify_remote(root, repository)
    # Never include staged code/config changes in a scheduled report commit.
    already_staged = git(root, 'diff', '--cached', '--name-only').splitlines()
    if any(not path.startswith('reports/') for path in already_staged):
        raise PublishError('暂存区存在源码或配置变更，日常任务不会一并提交')
    git(root, 'add', '-A', '--', 'reports/')
    changed = git(root, 'diff', '--cached', '--name-only').splitlines()
    if any(not path.startswith('reports/') for path in changed):
        raise PublishError('报告提交范围检查失败')
    if changed:
        git(root, 'commit', '-m', 'reports: ' + run_id)
    git(root, 'push', 'origin', 'main', timeout=60)
    return {'url': 'https://github.com/' + repository + '/tree/main/reports',
            'commit': git(root, 'rev-parse', 'HEAD'), 'changed_files': len(changed)}

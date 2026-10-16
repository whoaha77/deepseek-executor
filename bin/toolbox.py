#!/usr/bin/env python3
"""toolbox —— 零 token 本地工具集（T0 层）

设计原则：确定性的机械活根本不该花 token。凡是能用脚本一次做对的，就不调模型。
所有会改文件的命令默认 --dry-run，确认无误再加 --apply。

用法：python -I toolbox.py <command> [args...]
      python -I toolbox.py list          # 列出全部命令
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------- helpers

def _iter_files(paths, patterns):
    """展开目标：目录 + glob 模式；跳过 _ 开头目录由调用方决定。"""
    out = []
    for p in paths:
        pth = Path(p)
        if pth.is_dir():
            for pat in (patterns or ['*']):
                out.extend(sorted(pth.rglob(pat)))
        else:
            out.append(pth)
    return [f for f in out if f.is_file()]


def _read_text(p: Path) -> str:
    raw = p.read_bytes()
    if raw.startswith(b'\xef\xbb\xbf'):
        return raw.decode('utf-8-sig')
    for enc in ('utf-8', 'gb18030', 'utf-16'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _write_text(p: Path, text: str, bom: bool = False):
    data = text.encode('utf-8')
    if bom:
        data = b'\xef\xbb\xbf' + data
    p.write_bytes(data)


# ---------------------------------------------------------------- commands

def cmd_rename(a):
    """批量重命名：正则替换 / 序号补零 / 去多余空格。"""
    files = _iter_files(a.targets, a.glob)
    plan = []
    for f in files:
        name = f.name
        if a.regex:
            new = re.sub(a.regex, a.repl, name)
        else:
            new = name
        if a.squeeze_spaces:
            new = re.sub(r'\s+', ' ', new).strip()
        if a.prefix:
            new = a.prefix + new
        if new != name:
            plan.append((f, f.with_name(new)))
    if a.number:
        # 按名称排序后加序号前缀（补零宽度自动）
        plan = []
        files = sorted(files, key=lambda x: x.name)
        width = len(str(len(files) + a.number - 1))
        for i, f in enumerate(files, start=a.number):
            new = f'{i:0{width}d}{a.sep}{f.name}' if not a.replace else f'{i:0{width}d}{f.suffix}'
            plan.append((f, f.with_name(new)))
    _apply_plan(plan, a)


def _apply_plan(plan, a):
    if not plan:
        print('没有需要改动的文件。')
        return
    print(f'{"[DRY-RUN] " if not a.apply else ""}共 {len(plan)} 项：')
    for src, dst in plan:
        flag = 'OK ' if not dst.exists() or dst == src else '!! 目标已存在'
        print(f'  {flag} {src.name}  ->  {dst.name}')
    if not a.apply:
        print('\n（未改动。确认后加 --apply 执行）')
        return
    for src, dst in plan:
        if dst.exists() and dst != src:
            print(f'  跳过（目标已存在）：{src.name}')
            continue
        src.rename(dst)
    print(f'已重命名 {len(plan)} 个文件。')


def cmd_lines(a):
    """行处理：去重 / 排序 / 去空行 / 去重保序 / 过滤（正则）。"""
    src = Path(a.file)
    lines = _read_text(src).splitlines()
    if a.strip:
        lines = [l.strip() for l in lines]
    if a.drop_blank:
        lines = [l for l in lines if l.strip()]
    if a.match:
        rx = re.compile(a.match)
        lines = [l for l in lines if rx.search(l)]
    if a.dedupe:
        seen, out = set(), []
        for l in lines:
            if l not in seen:
                seen.add(l)
                out.append(l)
        lines = out
    if a.sort:
        lines = sorted(lines)
    text = '\n'.join(lines) + ('\n' if lines else '')
    if a.out:
        _write_text(Path(a.out), text)
        print(f'{len(lines)} 行 -> {a.out}')
    else:
        sys.stdout.write(text)


def cmd_extract(a):
    """按正则抽取匹配行（可选加行号/来源文件名），写入新文件。"""
    files = _iter_files(a.targets, a.glob)
    rx = re.compile(a.regex)
    out = []
    for f in files:
        try:
            for i, line in enumerate(_read_text(f).splitlines(), 1):
                if rx.search(line):
                    prefix = f'{f.name}:{i}: ' if a.with_source else ''
                    out.append(prefix + line)
        except Exception as e:  # noqa: BLE001
            print(f'  跳过 {f}: {e}', file=sys.stderr)
    text = '\n'.join(out) + ('\n' if out else '')
    if a.out:
        _write_text(Path(a.out), text)
        print(f'{len(out)} 条 -> {a.out}')
    else:
        sys.stdout.write(text)


def cmd_manifest(a):
    """目录清单：文件树 + 大小 + 修改时间，输出 md（可当交付物索引）。"""
    files = _iter_files(a.targets, a.glob)
    rows = []
    for f in files:
        st = f.stat()
        rows.append((str(f), st.st_size, st.st_mtime))
    rows.sort()
    fmt = a.format or 'md'
    lines = []
    if fmt == 'md':
        lines.append('| 文件 | 大小 | 修改时间 |')
        lines.append('|---|---:|---|')
        for path, size, mt in rows:
            import datetime
            ts = datetime.datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M')
            lines.append(f'| {path} | {size} | {ts} |')
        lines.append(f'\n共 {len(rows)} 个文件。')
    else:
        lines.append('path,size,mtime')
        for path, size, mt in rows:
            lines.append(f'{path},{size},{mt}')
    text = '\n'.join(lines) + '\n'
    if a.out:
        _write_text(Path(a.out), text)
        print(f'{len(rows)} 个文件 -> {a.out}')
    else:
        sys.stdout.write(text)


def cmd_encoding(a):
    """编码归一：转 UTF-8（可选加/去 BOM），或仅检测报告。"""
    files = _iter_files(a.targets, a.glob)
    plan = []
    for f in files:
        raw = f.read_bytes()
        has_bom = raw.startswith(b'\xef\xbb\xbf')
        enc = None
        for e in ('utf-8', 'gb18030', 'utf-16'):
            try:
                raw.decode(e if not has_bom else 'utf-8-sig')
                enc = e
                break
            except UnicodeDecodeError:
                continue
        want_bom = a.bom
        needs = (enc != 'utf-8') or (has_bom != want_bom)
        if a.report or needs:
            print(f'{f}  检测={enc or "未知"}  BOM={has_bom}  ->  目标=UTF-8 BOM={want_bom}')
        if needs and not a.report:
            plan.append(f)
    if a.report:
        return
    if not a.apply:
        print(f'\n[DRY-RUN] 将转换 {len(plan)} 个文件。确认后加 --apply。')
        return
    for f in plan:
        text = _read_text(f)
        _write_text(f, text, bom=a.bom)
    print(f'已转换 {len(plan)} 个文件。')


def cmd_tidy_md(a):
    """Markdown 整理：标题前后空行、去掉行尾空格、统一列表符号、收缩 3+ 空行。"""
    files = _iter_files(a.targets, a.glob)
    plan = []
    for f in files:
        src = _read_text(f)
        out = src
        out = re.sub(r'[ \t]+$', '', out, flags=re.M)          # 行尾空格
        out = re.sub(r'\n{3,}', '\n\n', out)                    # 3+ 空行
        out = re.sub(r'^(#{1,6} .+)$', r'\n\1\n', out, flags=re.M)  # 标题前后空行
        out = re.sub(r'\n{3,}', '\n\n', out)
        out = re.sub(r'^(\s*)[-*+] ', r'\1- ', out, flags=re.M)  # 统一列表符
        if not out.endswith('\n'):
            out += '\n'
        if out != src:
            plan.append((f, out))
    if not plan:
        print('无需整理。')
        return
    print(f'{"[DRY-RUN] " if not a.apply else ""}将整理 {len(plan)} 个文件：')
    for f, _ in plan:
        print(f'  {f}')
    if not a.apply:
        print('\n确认后加 --apply。')
        return
    for f, out in plan:
        _write_text(f, out)
    print(f'已整理 {len(plan)} 个文件。')


def _zip_xml_text(path: Path, member_prefix: str) -> str:
    """从 docx/pptx（zip+xml）里抽纯文本，零依赖。"""
    chunks = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if n.startswith(member_prefix) and n.endswith('.xml')]
        for n in sorted(names):
            xml = z.read(n).decode('utf-8', errors='replace')
            xml = re.sub(r'<w:p[ >]', '\n<w:p ', xml)          # docx 段落
            xml = re.sub(r'<a:p>', '\n<a:p>', xml)              # pptx 段落
            text = re.sub(r'<[^>]+>', '', xml)
            text = re.sub(r'\n{2,}', '\n', text)
            title = n.split('/')[-1]
            chunks.append(f'--- {title} ---\n{text.strip()}')
    return '\n\n'.join(chunks)


def cmd_docx_text(a):
    """抽取 .docx 纯文本（零依赖），输出到 stdout 或 --out。"""
    parts = []
    for f in _iter_files(a.targets, a.glob or ['*.docx']):
        if f.suffix.lower() != '.docx':
            continue
        parts.append(_zip_xml_text(f, 'word/'))
    text = '\n\n'.join(parts)
    if a.out:
        _write_text(Path(a.out), text)
        print(f'-> {a.out}（{len(text)} 字符）')
    else:
        sys.stdout.write(text)


def cmd_pptx_text(a):
    """抽取 .pptx 每页文字（零依赖），输出到 stdout 或 --out。"""
    parts = []
    for f in _iter_files(a.targets, a.glob or ['*.pptx']):
        if f.suffix.lower() != '.pptx':
            continue
        parts.append(f'===== {f.name} =====\n' + _zip_xml_text(f, 'ppt/slides/'))
    text = '\n\n'.join(parts)
    if a.out:
        _write_text(Path(a.out), text)
        print(f'-> {a.out}（{len(text)} 字符）')
    else:
        sys.stdout.write(text)


def cmd_csv_stats(a):
    """CSV 速览：行列数、每列空值数、唯一值数、前几行样例。"""
    for f in _iter_files(a.targets, a.glob or ['*.csv']):
        with open(f, 'r', encoding='utf-8-sig', errors='replace', newline='') as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel
            rows = list(csv.reader(fh, dialect))
        if not rows:
            print(f'{f}: 空文件'); continue
        header, body = rows[0], rows[1:]
        print(f'== {f} ==\n行数={len(body)}  列数={len(header)}')
        for i, col in enumerate(header):
            vals = [r[i] if i < len(r) else '' for r in body]
            empties = sum(1 for v in vals if not v.strip())
            uniq = len(set(vals))
            print(f'  {i:>2} {col}: 空值={empties} 唯一值={uniq}')
        print()


# ---------------------------------------------------------------- CLI

def main():
    # 被管道/重定向时输出 UTF-8（供 Claude/Codex 读取）；直连终端时保持控制台编码（中文 Windows 是 GBK）
    try:
        if not sys.stdout.isatty():
            sys.stdout.reconfigure(encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass
    p = argparse.ArgumentParser(prog='toolbox', description='零 token 本地工具集（T0 层）')
    sub = p.add_subparsers(dest='cmd', required=True)

    def add_common(sp):
        sp.add_argument('targets', nargs='+', help='文件或目录')
        sp.add_argument('--glob', action='append', help='glob 模式，可多次（默认 *）')
        sp.add_argument('--apply', action='store_true', help='真正执行（默认 dry-run）')

    sp = sub.add_parser('rename', help='批量重命名'); add_common(sp)
    sp.add_argument('--regex'); sp.add_argument('--repl', default='')
    sp.add_argument('--squeeze-spaces', action='store_true')
    sp.add_argument('--prefix', default='')
    sp.add_argument('--number', type=int, help='从 N 开始加序号前缀')
    sp.add_argument('--sep', default='_'); sp.add_argument('--replace', action='store_true')
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser('lines', help='行处理：去重/排序/过滤'); sp.add_argument('file')
    sp.add_argument('--strip', action='store_true'); sp.add_argument('--drop-blank', action='store_true')
    sp.add_argument('--dedupe', action='store_true'); sp.add_argument('--sort', action='store_true')
    sp.add_argument('--match'); sp.add_argument('--out')
    sp.set_defaults(func=cmd_lines)

    sp = sub.add_parser('extract', help='按正则抽行'); add_common(sp)
    sp.add_argument('--regex', required=True); sp.add_argument('--with-source', action='store_true')
    sp.add_argument('--out')
    sp.set_defaults(func=cmd_extract)

    sp = sub.add_parser('manifest', help='目录清单(md/csv)'); add_common(sp)
    sp.add_argument('--format', choices=['md', 'csv']); sp.add_argument('--out')
    sp.set_defaults(func=cmd_manifest)

    sp = sub.add_parser('encoding', help='编码归一/检测'); add_common(sp)
    sp.add_argument('--bom', action='store_true', help='目标带 BOM（默认不带）')
    sp.add_argument('--report', action='store_true')
    sp.set_defaults(func=cmd_encoding)

    sp = sub.add_parser('tidy-md', help='Markdown 整理'); add_common(sp)
    sp.set_defaults(func=cmd_tidy_md)

    sp = sub.add_parser('docx-text', help='抽 docx 纯文本'); add_common(sp)
    sp.add_argument('--out'); sp.set_defaults(func=cmd_docx_text)

    sp = sub.add_parser('pptx-text', help='抽 pptx 每页文字'); add_common(sp)
    sp.add_argument('--out'); sp.set_defaults(func=cmd_pptx_text)

    sp = sub.add_parser('csv-stats', help='CSV 速览'); add_common(sp)
    sp.set_defaults(func=cmd_csv_stats)

    sp = sub.add_parser('list', help='列出全部命令'); sp.set_defaults(func=lambda a: print(__doc__))

    a = p.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()

import argparse
import ast
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

MAX_BYTES = 1024 * 1024
MAX_SYMBOLS = 10000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
             "site-packages", "logs", "archive", "archives", "_archive", "backups",
             "releases", "dist", "build", ".next", "coverage"}


def _read_bytes(path):
    p = Path(path)
    if p.suffix.lower() != ".py":
        raise ValueError("only Python .py source is supported")
    if not stat.S_ISREG(p.stat().st_mode):
        raise ValueError("source must be a regular file")
    with p.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file exceeds 1 MiB limit")
    return data


def _decode(data):
    return data.decode("utf-8-sig")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _arg_names(args):
    names = []
    for a in args.posonlyargs:
        names.append(a.arg)
    if args.posonlyargs:
        names.append("/")
    for a in args.args:
        names.append(a.arg)
    if args.vararg:
        names.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        names.append("*")
    for a in args.kwonlyargs:
        names.append(a.arg)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return names


def _signature(node):
    name = node.name
    try:
        args = _arg_names(node.args)
    except Exception:
        args = []
    return name + "(" + ", ".join(args) + ")"


def _start_line(node):
    lineno = node.lineno
    for d in getattr(node, "decorator_list", []):
        if d.lineno < lineno:
            lineno = d.lineno
    return lineno


def _kind(node):
    if isinstance(node, ast.ClassDef):
        return "class"
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_function"
    return "function"


def _collect(body, prefix, out):
    for node in body:
        scope = prefix
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = prefix + "." + node.name if prefix else node.name
            if len(out) >= MAX_SYMBOLS:
                raise ValueError("symbol count exceeds limit")
            out.append({"name": scope, "kind": _kind(node),
                        "start": _start_line(node), "end": node.end_lineno,
                        "signature": _signature(node)})
        _collect(ast.iter_child_nodes(node), scope, out)


def _parse(data):
    text = _decode(data)
    tree = ast.parse(text)
    return text, tree


def outline(path, limit=100):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    p = Path(path)
    data = _read_bytes(p)
    text, tree = _parse(data)
    symbols = []
    _collect(tree.body, "", symbols)
    line_count = len(text.splitlines())
    return {
        "path": str(p),
        "sha256": _sha256(data),
        "language": "python",
        "extraction": "ast",
        "line_count": line_count,
        "total_symbols": len(symbols),
        "truncated": len(symbols) > limit,
        "symbols": symbols[:limit],
    }


def _is_under(root, path):
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        return os.path.commonpath([rp, rr]) == rr
    except Exception:
        return False


def find_symbols(root, query, limit=10, max_files=500):
    if type(limit) is not int or limit < 1 or limit > 100:
        raise ValueError("limit must be 1..100")
    if type(max_files) is not int or max_files < 1 or max_files > 5000:
        raise ValueError("max_files must be 1..5000")
    rr = os.path.realpath(root)
    rootp = Path(root)
    if not rootp.exists():
        raise ValueError("root does not exist")
    if not rootp.is_dir():
        raise ValueError("root is not a directory")
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ValueError("query must contain 1..500 characters")
    q = query.casefold()
    files_scanned = 0
    errors = []
    matches = []
    truncated = False
    stop = False
    def walk_error(error):
        errors.append({"path": str(error.filename or rr), "error": str(error)})
    for dirpath, dirnames, filenames in os.walk(rr, followlinks=False, onerror=walk_error):
        if stop:
            break
        dirnames[:] = sorted(d for d in dirnames if d.casefold() not in SKIP_DIRS)
        for fn in sorted(filenames):
            if not fn.lower().endswith(".py"):
                continue
            if files_scanned >= max_files:
                truncated = True
                stop = True
                break
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):
                if not _is_under(rr, full):
                    errors.append({"path": full, "error": "symlink escapes root"})
                    continue
            if not _is_under(rr, full):
                errors.append({"path": full, "error": "path outside root"})
                continue
            files_scanned += 1
            try:
                data = _read_bytes(full)
                _, tree = _parse(data)
                sha = _sha256(data)
                out = []
                _collect(tree.body, "", out)
                for s in out:
                    if q in s["name"].casefold() and len(matches) <= limit:
                        matches.append({
                            "path": full,
                            "sha256": sha,
                            "name": s["name"],
                            "kind": s["kind"],
                            "start": s["start"],
                            "end": s["end"],
                            "signature": s["signature"],
                        })
            except Exception as e:
                errors.append({"path": full, "error": str(e)})
            if stop:
                break
    if len(matches) > limit:
        truncated = True
        matches = matches[:limit]
    return {
        "root": str(rootp),
        "query": query,
        "language": "python",
        "extraction": "ast",
        "files_scanned": files_scanned,
        "errors": errors,
        "truncated": truncated,
        "symbols": matches,
    }


def _find_exact(entries, symbol):
    hits = [e for e in entries if e["name"] == symbol]
    return hits


def show_symbol(path, symbol, expected_sha=None, max_chars=12000):
    if type(max_chars) is not int or max_chars < 1 or max_chars > 50000:
        raise ValueError("max_chars must be 1..50000")
    p = Path(path)
    data = _read_bytes(p)
    sha = _sha256(data)
    if expected_sha is not None and expected_sha != sha:
        raise ValueError("sha256 mismatch")
    text, tree = _parse(data)
    entries = []
    _collect(tree.body, "", entries)
    hits = _find_exact(entries, symbol)
    if not hits:
        raise ValueError("symbol not found: " + symbol)
    if len(hits) > 1:
        raise ValueError("ambiguous symbol: " + symbol)
    hit = hits[0]
    lines = text.splitlines(keepends=True)
    start = hit["start"]
    end = hit["end"]
    chunk = "".join(lines[start - 1:end])
    if len(chunk) > max_chars:
        raise ValueError("symbol text exceeds max_chars")
    return {
        "path": str(p),
        "sha256": sha,
        "language": "python",
        "extraction": "ast",
        "name": hit["name"],
        "kind": hit["kind"],
        "start": start,
        "end": end,
        "signature": hit["signature"],
        "text": chunk,
    }


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def _main(argv):
    parser = argparse.ArgumentParser(prog="code_context")
    sub = parser.add_subparsers(dest="cmd", required=True)

    po = sub.add_parser("outline")
    po.add_argument("path")
    po.add_argument("--limit", type=int, default=100)

    pf = sub.add_parser("find")
    pf.add_argument("--root", required=True)
    pf.add_argument("--query", required=True)
    pf.add_argument("--limit", type=int, default=10)
    pf.add_argument("--max-files", type=int, default=500)

    ps = sub.add_parser("show")
    ps.add_argument("path")
    ps.add_argument("--symbol", required=True)
    ps.add_argument("--sha256", default=None)
    ps.add_argument("--max-chars", type=int, default=12000)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "outline":
            res = outline(args.path, args.limit)
        elif args.cmd == "find":
            res = find_symbols(args.root, args.query, args.limit, args.max_files)
        else:
            res = show_symbol(args.path, args.symbol, args.sha256, args.max_chars)
        _emit(res)
        return 0
    except Exception as e:
        sys.stderr.write(str(e) + "\n")
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(_main(sys.argv[1:]))

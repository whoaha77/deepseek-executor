import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import stat
import sys
import uuid
from datetime import datetime, timezone

SCHEMA_VERSION = 1
MAX_FILE_SIZE = 2 * 1024 * 1024
MAX_FILES = 50
DEFAULT_CHUNK_LINES = 30
MIN_CHUNK_LINES = 1
MAX_CHUNK_LINES = 100
MAX_QUERY_TERMS = 16
MAX_QUERY_CHARS = 500
MAX_LIMIT = 100
MAX_CHARS = 50000

SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"BEGIN PRIVATE KEY"),
    re.compile(r"BEGIN RSA PRIVATE KEY"),
    re.compile(r"BEGIN EC PRIVATE KEY"),
    re.compile(r"BEGIN OPENSSH PRIVATE KEY"),
    re.compile(r"BEGIN DSA PRIVATE KEY"),
    re.compile(r"BEGIN ENCRYPTED PRIVATE KEY"),
]

FORBIDDEN_COMPONENTS = {".git", "worker-home", ".codex"}
FORBIDDEN_NAMES = {".env"}


class IndexError_(Exception):
    pass


def _fail(msg):
    raise IndexError_(msg)


def _is_forbidden_component(part):
    low = part.lower()
    if low in FORBIDDEN_COMPONENTS:
        return True
    if low == ".env" or low.startswith(".env."):
        return True
    return False


def _contains_secret(text):
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            return True
    return False


def _normkey(path):
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except Exception:
        return os.path.normcase(str(path))


def _validate_relative(rel):
    if not rel or os.path.isabs(rel):
        _fail("files must be relative paths")
    p = pathlib.PurePath(rel)
    if any(part == ".." for part in p.parts):
        _fail("path traversal not allowed")
    for part in p.parts:
        if _is_forbidden_component(part):
            _fail("forbidden path component")
    return p


def _check_signature(name, content):
    if _contains_secret(content):
        _fail("forbidden content in " + name)


def _read_source(path):
    try:
        st = os.stat(path)
    except OSError:
        _fail("cannot stat source")
    if not stat.S_ISREG(st.st_mode):
        _fail("source must be a regular file")
    if st.st_size > MAX_FILE_SIZE:
        _fail("source too large")
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_SIZE + 1)
    except OSError:
        _fail("cannot read source")
    if len(raw) > MAX_FILE_SIZE:
        _fail("source too large")
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        _fail("source not valid utf-8")
    _check_signature("source", decoded)
    return raw, decoded


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _title_of(decoded, filename):
    for line in decoded.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            t = stripped.lstrip("#").strip()
            if t:
                return t
    return filename


def _chunk_ranges(num_lines, chunk_lines):
    ranges = []
    if num_lines == 0:
        return ranges
    start = 1
    while start <= num_lines:
        end = min(start + chunk_lines - 1, num_lines)
        ranges.append((start, end))
        start = end + 1
    return ranges


def _split_keepends(decoded):
    lines = decoded.splitlines(keepends=True)
    if decoded and not lines:
        lines = [decoded]
    if decoded and lines and not lines[-1].endswith(("\n", "\r")):
        pass
    return lines


def _fold(text):
    return text.casefold()


def _open_readonly(db):
    try:
        uri = pathlib.Path(db).resolve().as_uri() + "?mode=ro"
    except Exception:
        _fail("invalid db path")
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        _fail("cannot open db read-only")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        conn.close()
        _fail("cannot set query_only")
    return conn


def _validate_schema(conn):
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
    except sqlite3.Error:
        _fail("cannot read schema version")
    if row is None or row[0] != SCHEMA_VERSION:
        _fail("unsupported schema version")


def index_documents(root, files, db, replace=False, chunk_lines=30):
    if not isinstance(chunk_lines, int) or isinstance(chunk_lines, bool):
        _fail("chunk_lines must be integer")
    if chunk_lines < MIN_CHUNK_LINES or chunk_lines > MAX_CHUNK_LINES:
        _fail("chunk_lines out of range")
    if not files:
        _fail("no files provided")
    if len(files) > MAX_FILES:
        _fail("too many files")

    root_resolved = pathlib.Path(root).resolve()
    if not root_resolved.is_dir():
        _fail("root must be an existing directory")
    db_path = pathlib.Path(db).resolve()
    protected = [pathlib.Path("D:/protected-data"), pathlib.Path("D:/research-raw")]
    if os.name == "nt":
        protected.append(pathlib.Path.home() / "Desktop")
    for directory in protected:
        if db_path.is_relative_to(directory.resolve()):
            _fail("protected output directory")

    db_exists = db_path.exists()
    if db_exists and not replace:
        _fail("db exists; use replace")

    sources = []
    seen = set()
    for rel in files:
        if not isinstance(rel, str):
            _fail("files must be strings")
        _validate_relative(rel)
        candidate = (root_resolved / rel)
        try:
            real = pathlib.Path(os.path.realpath(str(candidate)))
        except Exception:
            _fail("cannot resolve path")
        try:
            real.relative_to(root_resolved)
        except ValueError:
            _fail("path outside root")
        for part in real.parts:
            if _is_forbidden_component(part):
                _fail("forbidden path component")
        key = _normkey(real)
        if key in seen:
            _fail("duplicate path")
        seen.add(key)
        if not real.exists() or not real.is_file():
            _fail("missing source file")
        ext = real.suffix.lower()
        if ext not in (".md", ".txt"):
            _fail("unsupported file type")
        sources.append((rel, real))

    db_key = _normkey(db_path)
    for rel, real in sources:
        if _normkey(real) == db_key:
            _fail("db path equals source")

    docs = []
    for rel, real in sources:
        raw, decoded = _read_source(real)
        docs.append((rel, real, raw, decoded))

    # Validate every source before creating directories or touching the old index.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    build_path = db_path.with_name(db_path.name + ".build-" + uuid.uuid4().hex)
    conn = sqlite3.connect(str(build_path))
    try:
        conn.execute("BEGIN")
        conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        conn.execute(
            "CREATE TABLE documents("
            "id INTEGER PRIMARY KEY,"
            "path TEXT NOT NULL,"
            "sha256 TEXT NOT NULL,"
            "title TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE chunks("
            "id INTEGER PRIMARY KEY,"
            "doc_id INTEGER NOT NULL,"
            "start_line INTEGER NOT NULL,"
            "end_line INTEGER NOT NULL,"
            "body TEXT NOT NULL,"
            "folded TEXT NOT NULL)"
        )
        doc_count = 0
        chunk_count = 0
        for rel, real, raw, decoded in docs:
            doc_count += 1
            title = _title_of(decoded, real.name)
            sha = _sha256(raw)
            cur = conn.execute(
                "INSERT INTO documents(path,sha256,title) VALUES(?,?,?)",
                (str(real), sha, title),
            )
            doc_id = cur.lastrowid
            lines = _split_keepends(decoded)
            num_lines = len(lines)
            for start, end in _chunk_ranges(num_lines, chunk_lines):
                body = "".join(lines[start - 1:end])
                conn.execute(
                    "INSERT INTO chunks(doc_id,start_line,end_line,body,folded)"
                    " VALUES(?,?,?,?,?)",
                    (doc_id, start, end, body, _fold(body)),
                )
                chunk_count += 1
        conn.commit()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        _fail("build failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    try:
        if db_exists:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex
            backup = db_path.with_name(db_path.name + ".backup-" + stamp)
            shutil.copy2(db_path, backup)
        if db_exists:
            os.replace(str(build_path), str(db_path))
        else:
            # Atomic create-only publication, even if another process created db meanwhile.
            os.link(build_path, db_path)
            build_path.unlink()
    except OSError:
        _fail("cannot publish db")

    return {
        "ok": True,
        "document_count": doc_count,
        "chunk_count": chunk_count,
        "db": str(db_path),
        "db_path": str(db_path),
        "replaced": db_exists,
        "backup": str(backup) if backup else None,
    }


def _parse_query(query):
    if not isinstance(query, str):
        _fail("query must be string")
    if len(query) > MAX_QUERY_CHARS:
        _fail("query too long")
    terms = query.split()
    if not terms:
        _fail("empty query")
    if len(terms) > MAX_QUERY_TERMS:
        _fail("too many terms")
    return list(dict.fromkeys(t.casefold() for t in terms))


def search_index(db, query, limit=5, match="all"):
    terms = _parse_query(query)
    if match not in ("all", "any"):
        _fail("match must be all or any")
    if not isinstance(limit, int) or isinstance(limit, bool):
        _fail("limit must be integer")
    if not 1 <= limit <= MAX_LIMIT:
        _fail("limit must be 1..100")
    conn = _open_readonly(db)
    try:
        _validate_schema(conn)
        where = (" AND " if match == "all" else " OR ").join(
            "instr(c.folded,?) > 0" for _ in terms)
        tables = " FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE " + where
        total = conn.execute("SELECT COUNT(*)" + tables, terms).fetchone()[0]
        score = " + ".join("(instr(c.folded,?) > 0)" for _ in terms)
        sql = ("SELECT c.id AS cid, c.doc_id AS did, c.start_line AS s,"
               " c.end_line AS e, d.path AS p, d.title AS t, d.sha256 AS h, (" + score +
               ") AS sc" + tables + " ORDER BY sc DESC, c.id ASC LIMIT ?")
        rows = conn.execute(sql, terms + terms + [limit]).fetchall()
    except sqlite3.Error:
        _fail("search failed")
    finally:
        conn.close()

    results = []
    for r in rows:
        results.append({
            "id": r["cid"],
            "chunk_id": r["cid"],
            "doc_id": r["did"],
            "path": r["p"],
            "title": r["t"],
            "start_line": r["s"],
            "end_line": r["e"],
            "sha256": r["h"],
            "score": r["sc"],
            "stale_status": "unchecked",
        })
    return {
        "ok": True,
        "results": results,
        "total": total,
        "limit": limit,
        "truncated": total > limit,
    }


def get_chunk(db, chunk_id, max_chars=12000):
    if not isinstance(chunk_id, int) or isinstance(chunk_id, bool):
        _fail("chunk id must be integer")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        _fail("max_chars must be integer")
    if max_chars <= 0 or max_chars > MAX_CHARS:
        _fail("max_chars out of range")

    conn = _open_readonly(db)
    try:
        _validate_schema(conn)
        row = conn.execute(
            "SELECT c.id AS cid, c.start_line AS s, c.end_line AS e,"
            " d.path AS p, d.title AS t, d.sha256 AS h"
            " FROM chunks c JOIN documents d ON d.id=c.doc_id"
            " WHERE c.id=?",
            (chunk_id,),
        ).fetchone()
    except sqlite3.Error:
        _fail("lookup failed")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if row is None:
        _fail("chunk not found")

    path = pathlib.Path(row["p"])
    if not path.exists() or not path.is_file():
        _fail("source stale: missing")
    real = pathlib.Path(os.path.realpath(str(path)))
    if os.path.normcase(str(real)) != os.path.normcase(os.path.abspath(path)):
        _fail("source stale: redirected path")
    for part in real.parts:
        if _is_forbidden_component(part):
            _fail("source no longer eligible")
    if path.suffix.lower() not in (".md", ".txt"):
        _fail("source no longer eligible")

    raw, decoded = _read_source(real)
    cur_sha = _sha256(raw)
    if cur_sha != row["h"]:
        _fail("source stale: changed")

    lines = _split_keepends(decoded)
    start = row["s"]
    end = row["e"]
    if start < 1 or end < start or end > len(lines):
        _fail("source stale: range")
    body = "".join(lines[start - 1:end])
    if len(body) > max_chars:
        _fail("excerpt too large")

    return {
        "ok": True,
        "chunk_id": row["cid"],
        "path": str(real),
        "title": row["t"],
        "start_line": start,
        "end_line": end,
        "sha256": cur_sha,
        "text": body,
        "stale_status": "verified",
    }


def _emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="memory_index")
    sub = parser.add_subparsers(dest="cmd")

    p_idx = sub.add_parser("index")
    p_idx.add_argument("--root", required=True)
    p_idx.add_argument("--files", nargs="+", required=True)
    p_idx.add_argument("--db", required=True)
    p_idx.add_argument("--replace", action="store_true")
    p_idx.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES)

    p_srch = sub.add_parser("search")
    p_srch.add_argument("--db", required=True)
    p_srch.add_argument("--query", required=True)
    p_srch.add_argument("--limit", type=int, default=5)
    p_srch.add_argument("--match", choices=["all", "any"], default="all")

    p_get = sub.add_parser("get")
    p_get.add_argument("--db", required=True)
    p_get.add_argument("--id", type=int, required=True)
    p_get.add_argument("--max-chars", type=int, default=12000)

    args = parser.parse_args(argv)

    try:
        if args.cmd == "index":
            res = index_documents(
                args.root, args.files, args.db,
                replace=args.replace, chunk_lines=args.chunk_lines,
            )
        elif args.cmd == "search":
            res = search_index(
                args.db, args.query, limit=args.limit, match=args.match,
            )
        elif args.cmd == "get":
            res = get_chunk(args.db, args.id, max_chars=args.max_chars)
        else:
            parser.print_usage(sys.stderr)
            return 2
    except IndexError_ as exc:
        sys.stderr.write("error: " + str(exc) + "\n")
        return 2
    except Exception:
        sys.stderr.write("error: internal failure\n")
        return 2

    _emit(res)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())

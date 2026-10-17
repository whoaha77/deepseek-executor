import json
import pathlib
import sqlite3
import datetime
import os
import sys


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(text: str):
    return json.loads(text)


def _is_positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_micro(value, name: str):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer micro amount")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _parse_aware(value: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid ISO timestamp: {value!r}")
    return parsed


def _to_epoch(value: str) -> float:
    dt = _parse_aware(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


class Store:
    """Deterministic SQLite persistence component."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        parent = self.path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        with self.db:
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS repos ("
                "repo_id INTEGER PRIMARY KEY, "
                "body TEXT NOT NULL, "
                "first_seen TEXT NOT NULL, "
                "last_seen TEXT NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS snapshots ("
                "repo_id INTEGER, "
                "at TEXT, "
                "stars INTEGER, "
                "PRIMARY KEY(repo_id, at))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS analyses ("
                "repo_id INTEGER, "
                "input_hash TEXT, "
                "body TEXT, "
                "at TEXT, "
                "PRIMARY KEY(repo_id, input_hash))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS feedback ("
                "repo_id INTEGER PRIMARY KEY, "
                "action TEXT, "
                "reason TEXT, "
                "at TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS runs ("
                "run_id TEXT PRIMARY KEY, "
                "at TEXT, "
                "status TEXT, "
                "body TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS charges ("
                "trace_id TEXT PRIMARY KEY, "
                "day TEXT, "
                "reserved_micro INTEGER, "
                "charged_micro INTEGER, "
                "status TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS reports ("
                "report_id TEXT PRIMARY KEY, "
                "at TEXT, "
                "root TEXT, "
                "files TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS kv ("
                "key TEXT PRIMARY KEY, "
                "body TEXT)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS http_cache ("
                "url TEXT PRIMARY KEY, "
                "etag TEXT, "
                "body TEXT, "
                "at TEXT)"
            )

    def close(self) -> None:
        self.db.close()

    def upsert_repo(self, body: dict, at: str) -> None:
        if not isinstance(body, dict):
            raise ValueError("body must be a dict")
        if "id" not in body:
            raise ValueError("body must contain 'id'")
        repo_id = body["id"]
        if not _is_positive_int(repo_id):
            raise ValueError("body['id'] must be a positive int")
        body = dict(body)
        with self.db:
            row = self.db.execute(
                "SELECT first_seen,body FROM repos WHERE repo_id=?", (repo_id,)
            ).fetchone()
            if row is not None:
                previous = _json_loads(row['body'])
                names = set(previous.get('_known_names', []))
                if previous.get('full_name') != body.get('full_name') and previous.get('full_name'):
                    names.add(previous['full_name'])
                if names:
                    body['_known_names'] = sorted(names)
            encoded = _json_dumps(body)
            if row is None:
                self.db.execute(
                    "INSERT INTO repos(repo_id, body, first_seen, last_seen) "
                    "VALUES(?,?,?,?)",
                    (repo_id, encoded, at, at),
                )
            else:
                self.db.execute(
                    "UPDATE repos SET body=?, last_seen=? WHERE repo_id=?",
                    (encoded, at, repo_id),
                )

    def repos(self) -> list:
        rows = self.db.execute(
            "SELECT body FROM repos ORDER BY repo_id ASC"
        ).fetchall()
        return [_json_loads(row["body"]) for row in rows]

    def snapshot(self, repo_id: int, at: str, stars: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO snapshots(repo_id, at, stars) "
                "VALUES(?,?,?)",
                (repo_id, at, stars),
            )

    def growth(self, repo_id: int, at: str, stars: int) -> dict | None:
        rows = self.db.execute(
            "SELECT at, stars FROM snapshots WHERE repo_id=?",
            (repo_id,),
        ).fetchall()
        target = _to_epoch(at)
        best = None
        best_epoch = None
        for row in rows:
            try:
                epoch = _to_epoch(row["at"])
            except ValueError:
                continue
            if epoch < target:
                if best_epoch is None or epoch < best_epoch:
                    best_epoch = epoch
                    best = row
        if best is None:
            return None
        elapsed = target - best_epoch
        if elapsed < 3600:
            return None
        return {
            "days": round(elapsed / 86400, 2),
            "net_stars": stars - best["stars"],
        }

    def analysis(self, repo_id: int, input_hash: str) -> dict | None:
        row = self.db.execute(
            "SELECT body FROM analyses WHERE repo_id=? AND input_hash=?",
            (repo_id, input_hash),
        ).fetchone()
        if row is None:
            return None
        return _json_loads(row["body"])

    def save_analysis(
        self, repo_id: int, input_hash: str, body: dict, at: str
    ) -> None:
        encoded = _json_dumps(body)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO analyses(repo_id, input_hash, body, at) "
                "VALUES(?,?,?,?)",
                (repo_id, input_hash, encoded, at),
            )

    def feedback(self) -> dict:
        rows = self.db.execute(
            "SELECT repo_id, action, reason, at FROM feedback"
        ).fetchall()
        result = {}
        for row in rows:
            result[row["repo_id"]] = {
                "action": row["action"],
                "reason": row["reason"],
                "at": row["at"],
            }
        return result

    def set_feedback(
        self, repo_id: int, action: str, reason: str, at: str
    ) -> None:
        allowed = {
            "saved",
            "known",
            "ignore",
            "later",
            "tried_ok",
            "tried_failed",
            "clear",
        }
        if action not in allowed:
            raise ValueError(f"invalid feedback action: {action!r}")
        with self.db:
            if action == "clear":
                self.db.execute(
                    "DELETE FROM feedback WHERE repo_id=?", (repo_id,)
                )
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO feedback(repo_id, action, reason, at) "
                    "VALUES(?,?,?,?)",
                    (repo_id, action, reason, at),
                )

    def run(self, run_id: str, at: str, status: str, body: dict) -> None:
        encoded = _json_dumps(body)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO runs(run_id, at, status, body) "
                "VALUES(?,?,?,?)",
                (run_id, at, status, encoded),
            )

    def recent_runs(self, limit: int = 10) -> list:
        rows = self.db.execute(
            "SELECT run_id, at, status, body FROM runs "
            "ORDER BY at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "run_id": row["run_id"],
                "at": row["at"],
                "status": row["status"],
                "body": _json_loads(row["body"]),
            }
            for row in rows
        ]

    def get(self, key: str, default=None):
        row = self.db.execute(
            "SELECT body FROM kv WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        return _json_loads(row["body"])

    def put(self, key: str, value) -> None:
        encoded = _json_dumps(value)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO kv(key, body) VALUES(?,?)",
                (key, encoded),
            )

    def cache(self, url: str) -> dict | None:
        row = self.db.execute(
            "SELECT etag, body, at FROM http_cache WHERE url=?", (url,)
        ).fetchone()
        if row is None:
            return None
        return {
            "etag": row["etag"],
            "body": _json_loads(row["body"]),
            "at": row["at"],
        }

    def save_cache(
        self, url: str, etag: str | None, body, at: str
    ) -> None:
        encoded = _json_dumps(body)
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO http_cache(url, etag, body, at) "
                "VALUES(?,?,?,?)",
                (url, etag, encoded, at),
            )

    def budget(self, day: str) -> dict:
        row = self.db.execute(
            "SELECT "
            "COALESCE(SUM(reserved_micro), 0) AS reserved_micro, "
            "COALESCE(SUM(COALESCE(charged_micro, reserved_micro)), 0) AS charged_micro, "
            "COALESCE(SUM(CASE WHEN status='unknown' THEN 1 ELSE 0 END), 0) AS unknown_count, "
            "COALESCE(SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END), 0) AS pending_count "
            "FROM charges WHERE day=?",
            (day,),
        ).fetchone()
        return {
            "reserved_micro": row["reserved_micro"],
            "charged_micro": row["charged_micro"],
            "unknown_count": row["unknown_count"],
            "pending_count": row["pending_count"],
        }

    def reserve(
        self, trace_id: str, day: str, amount_micro: int, limit_micro: int | None
    ) -> bool:
        amount = _require_micro(amount_micro, "amount_micro")
        limit = None if limit_micro is None else _require_micro(limit_micro, "limit_micro")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            dup = self.db.execute(
                "SELECT 1 FROM charges WHERE trace_id=?", (trace_id,)
            ).fetchone()
            if dup is not None:
                self.db.rollback()
                raise ValueError(f"duplicate trace_id: {trace_id!r}")
            row = self.db.execute(
                "SELECT COALESCE(SUM(COALESCE(charged_micro, reserved_micro)), 0) "
                "AS total FROM charges WHERE day=?",
                (day,),
            ).fetchone()
            current = row["total"] or 0
            if limit is not None and current + amount > limit:
                self.db.rollback()
                return False
            self.db.execute(
                "INSERT INTO charges(trace_id, day, reserved_micro, charged_micro, status) "
                "VALUES(?,?,?,NULL,'reserved')",
                (trace_id, day, amount),
            )
            self.db.commit()
            return True
        except Exception:
            try:
                self.db.rollback()
            except Exception:
                pass
            raise

    def settle(self, trace_id: str, cost_micro: int | None) -> None:
        with self.db:
            row = self.db.execute(
                "SELECT reserved_micro, status FROM charges WHERE trace_id=?",
                (trace_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown trace_id: {trace_id!r}")
            if row["status"] != "reserved":
                raise ValueError(
                    f"trace {trace_id!r} is not reserved (status={row['status']!r})"
                )
            if cost_micro is None:
                self.db.execute(
                    "UPDATE charges SET charged_micro=NULL, status='unknown' "
                    "WHERE trace_id=?",
                    (trace_id,),
                )
                return
            cost = _require_micro(cost_micro, "cost_micro")
            if cost > row["reserved_micro"]:
                status = "overrun"
            else:
                status = "settled"
            self.db.execute(
                "UPDATE charges SET charged_micro=?, status=? WHERE trace_id=?",
                (cost, status, trace_id),
            )

    def save_report(
        self, report_id: str, at: str, root: str, files: list
    ) -> None:
        encoded = _json_dumps(files)
        with self.db:
            exists = self.db.execute(
                "SELECT 1 FROM reports WHERE report_id=?", (report_id,)
            ).fetchone()
            if exists is not None:
                raise ValueError(f"duplicate report_id: {report_id!r}")
            self.db.execute(
                "INSERT INTO reports(report_id, at, root, files) VALUES(?,?,?,?)",
                (report_id, at, root, encoded),
            )

    def reports(self) -> list:
        rows = self.db.execute(
            "SELECT report_id, at, root, files FROM reports ORDER BY at DESC"
        ).fetchall()
        return [
            {
                "report_id": row["report_id"],
                "at": row["at"],
                "root": row["root"],
                "files": _json_loads(row["files"]),
            }
            for row in rows
        ]

    def remove_report(self, report_id: str) -> None:
        with self.db:
            self.db.execute(
                "DELETE FROM reports WHERE report_id=?", (report_id,)
            )


class FileLock:
    """OS exclusive byte lock context manager."""

    def __init__(self, path: pathlib.Path):
        self.path = pathlib.Path(path)
        self.handle = None

    def __enter__(self):
        parent = self.path.parent
        if parent and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    handle.close()
                    raise RuntimeError("another scan is already running")
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    raise RuntimeError("another scan is already running")
        except RuntimeError:
            raise
        except Exception:
            handle.close()
            raise
        self.handle = handle
        return self

    def __exit__(self, exc_type, exc, tb):
        handle = self.handle
        self.handle = None
        if handle is None:
            return False
        try:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
                else:
                    import fcntl

                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
            finally:
                handle.close()
        except Exception:
            pass
        return False

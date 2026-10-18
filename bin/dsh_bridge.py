import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import uuid
import urllib.request
import urllib.error
import urllib.parse
import http.cookiejar
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

METHOD_RE = re.compile(r"^(?:session/(?:create|selectModel|modelCatalog|rename|prompt|list|projections|page))$")
SAFE_METHOD_RE = re.compile(r"^[A-Za-z/]+$")

PROTECTED_ROOTS = [
    Path("D:/protected-data"),
    Path("D:/research-raw"),
]
try:
    PROTECTED_ROOTS.append(Path.home() / "Desktop")
except Exception:
    pass

FORBIDDEN_COMPONENTS = {".git", ".codex", "worker-home"}
FORBIDDEN_FP_COMPONENTS = {"state", "logs", "archives", "backups", "releases", "node_modules"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9]{20,}"),
    re.compile(r"BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY"),
]
MAX_TASK_BYTES = 32768
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class BridgeError(Exception):
    pass


def _is_within(child, parent):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _scan_secret(text):
    if not isinstance(text, str):
        return
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            raise BridgeError("forbidden content detected")


def validate_workspace(path):
    p = Path(path)
    if not p.is_dir():
        raise BridgeError("workspace not a directory")
    for root in PROTECTED_ROOTS:
        if _is_within(p, root):
            raise BridgeError("workspace protected")
    return p.resolve()


def read_task(path):
    p = Path(path).resolve()
    if not p.is_file():
        raise BridgeError("task not a file")
    if p.suffix.casefold() not in (".md", ".txt"):
        raise BridgeError("task wrong type")
    parts = {part.casefold() for part in p.parts}
    if parts & FORBIDDEN_COMPONENTS:
        raise BridgeError("task path forbidden")
    for part in parts:
        if part == ".env" or part.startswith(".env."):
            raise BridgeError("task path forbidden")
    with open(p, "rb") as fh:
        raw = fh.read(MAX_TASK_BYTES + 1)
    if len(raw) > MAX_TASK_BYTES:
        raise BridgeError("task too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise BridgeError("task not utf8")
    _scan_secret(text)
    return text


def fingerprint(workspace, files):
    ws = Path(workspace).resolve()
    if not files or len(files) > 16:
        raise BridgeError("invalid declared file count")
    seen = set()
    result = {}
    for rel in files:
        rp = Path(rel)
        if rp.is_absolute() or rp.drive:
            raise BridgeError("absolute declared path")
        if ".." in rp.parts:
            raise BridgeError("parent traversal declared path")
        parts = {part.casefold() for part in rp.parts}
        if parts & FORBIDDEN_FP_COMPONENTS:
            raise BridgeError("forbidden declared component")
        for part in parts:
            if part == ".env" or part.startswith(".env."):
                raise BridgeError("forbidden declared component")
        if parts & FORBIDDEN_COMPONENTS:
            raise BridgeError("forbidden declared component")
        target = ws / rp
        try:
            resolved = target.resolve(strict=False)
            resolved.relative_to(ws)
        except (ValueError, OSError):
            raise BridgeError("declared path escapes workspace")
        key = os.path.normcase(str(resolved))
        if key in seen:
            raise BridgeError("duplicate declared file")
        seen.add(key)
        if resolved.is_symlink():
            raise BridgeError("symlink declared path")
        if not resolved.exists():
            result[rel] = None
            continue
        if not resolved.is_file():
            raise BridgeError("declared path not a regular file")
        size = resolved.stat().st_size
        if size > MAX_FILE_BYTES:
            raise BridgeError("declared file too large")
        h = hashlib.sha256()
        with open(resolved, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise BridgeError("declared file grew beyond limit")
        h.update(data)
        result[rel] = h.hexdigest()
    return result


def _extract_text_blocks(content):
    if not isinstance(content, list):
        return None
    texts = []
    for block in content:
        if not isinstance(block, dict):
            return None
        if block.get("type") != "text":
            continue
        if not isinstance(block.get("text"), str):
            return None
        texts.append(block["text"])
    return "\n".join(texts)


def public_messages(records):
    out = []
    for rec in records:
        if not isinstance(rec, dict) or rec.get("type") != "event":
            raise BridgeError("unexpected record shape")
        event = rec.get("event")
        if not isinstance(event, dict):
            raise BridgeError("unexpected event shape")
        etype = event.get("type")
        seq = event.get("seq")
        data = event.get("data")
        if etype == "user/message":
            if not isinstance(data, dict):
                raise BridgeError("unexpected user message shape")
            source = data.get("source")
            if isinstance(source, dict) and source.get("kind") in ("runtime-context", "skill-catalog"):
                continue
            if data.get("role") != "user":
                continue
            if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
                raise BridgeError("unexpected user message source")
            if source["kind"] != "user":
                continue
            content = data.get("content")
            text = _extract_text_blocks(content)
            if text is None:
                raise BridgeError("unexpected user message content")
            _scan_secret(text)
            out.append({"role": "user", "text": text, "seq": seq})
        elif etype == "assistant/message":
            if not isinstance(data, dict):
                raise BridgeError("unexpected assistant message shape")
            message = data.get("message")
            if not isinstance(message, dict):
                raise BridgeError("unexpected assistant message shape")
            if message.get("role") != "assistant":
                continue
            content = message.get("content")
            text = _extract_text_blocks(content)
            if text is None:
                raise BridgeError("unexpected assistant message content")
            _scan_secret(text)
            out.append({"role": "assistant", "text": text, "seq": seq})
        else:
            continue
    out.sort(key=lambda m: m["seq"] if isinstance(m["seq"], int) else 0)
    return out


def validate_launch_url(url, port):
    if not isinstance(url, str) or type(port) is not int or not 1 <= port <= 65535:
        raise BridgeError("invalid launch url")
    m = re.match(r"^http://127\.0\.0\.1:(\d+)/(?:\?[^\s#]*)?$", url)
    if not m:
        raise BridgeError("invalid launch url")
    if int(m.group(1)) != port:
        raise BridgeError("launch url port mismatch")
    if "#" in url:
        raise BridgeError("fragment not allowed")
    if "@" in url.split("://", 1)[1]:
        raise BridgeError("userinfo not allowed")
    query = url.split("?", 1)[1] if "?" in url else ""
    params = [kv for kv in query.split("&") if kv]
    tokens = urllib.parse.parse_qs(query, keep_blank_values=True).get("token", [])
    if len(tokens) != 1 or not tokens[0]:
        raise BridgeError("token required")
    return url


def rpc_envelope(method, args, rpc_id):
    if not isinstance(method, str) or not SAFE_METHOD_RE.match(method) or not METHOD_RE.match(method):
        raise BridgeError("bad method")
    if not isinstance(args, dict):
        raise BridgeError("bad args")
    if not isinstance(rpc_id, str) or not rpc_id:
        raise BridgeError("bad rpc id")
    return {"type": "client-request", "rpcId": rpc_id, "method": method, "payload": {"args": args}}


def write_json(path, data, overwrite=False):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and not overwrite:
        raise BridgeError("output exists")
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, str(p))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            o = urllib.parse.urlsplit(self.origin)
            n = urllib.parse.urlsplit(newurl)
        except Exception:
            raise urllib.error.HTTPError(newurl, code, "redirect blocked", headers, fp)
        if n.username or n.password:
            raise urllib.error.HTTPError(newurl, code, "redirect blocked", headers, fp)
        if (n.scheme, n.hostname, n.port) != (o.scheme, o.hostname, o.port):
            raise urllib.error.HTTPError(newurl, code, "redirect blocked", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class API:
    def __init__(self, state):
        self.state = Path(state)
        process = self.state / "process.json"
        if not process.is_file():
            raise BridgeError("no process")
        try:
            info = json.loads(process.read_text(encoding="utf-8"))
        except Exception:
            raise BridgeError("bad process file")
        self.pid = info.get("pid")
        self.port = info.get("port")
        if not isinstance(self.port, int):
            raise BridgeError("bad port")
        self._launch = None
        stdout = self.state / "web.stdout.log"
        if stdout.is_file():
            try:
                content = stdout.read_text(encoding="utf-8", errors="replace")
            except Exception:
                content = ""
            pat = re.compile(r"http://127\.0\.0\.1:%d/\?[^\s\"']*token=[^\s\"'&]+" % self.port)
            m = pat.search(content)
            if m:
                self._launch = validate_launch_url(m.group(0), self.port)
        if not self._launch:
            raise BridgeError("no launch url")
        self._base = "http://127.0.0.1:%d/" % self.port
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            _SameOriginRedirect(self._base.rstrip("/")),
        )
        auth_req = urllib.request.Request(self._launch, method="GET")
        try:
            with self._opener.open(auth_req, timeout=15) as resp:
                if len(resp.read(MAX_RESPONSE_BYTES + 1)) > MAX_RESPONSE_BYTES:
                    raise BridgeError("authentication response too large")
        except Exception:
            raise BridgeError("auth failed")

    @property
    def base(self):
        return self._base.rstrip("/")

    @property
    def launch(self):
        return self._launch

    def rpc(self, method, args):
        if not isinstance(method, str) or not SAFE_METHOD_RE.match(method):
            raise BridgeError("bad method")
        if not METHOD_RE.match(method):
            raise BridgeError("unsupported method")
        rpc_id = str(uuid.uuid4())
        env = rpc_envelope(method, args, rpc_id)
        url = self.base + "/api/" + method
        body = json.dumps(env).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", self.base)
        try:
            with self._opener.open(req, timeout=15) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except Exception:
            raise BridgeError("rpc failed")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise BridgeError("response too large")
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BridgeError("bad response")
        if not isinstance(data, dict) or data.get("type") != "server-response":
            raise BridgeError("bad response type")
        if data.get("rpcId") != rpc_id:
            raise BridgeError("rpc id mismatch")
        result = data.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise BridgeError("rpc not ok")
        return result.get("value")


def handoff(state, workspace, task_path, files, title, effort="high", execute=False):
    if type(execute) is not bool:
        raise BridgeError("invalid execute mode")
    if effort not in ("off", "low", "high", "max"):
        raise BridgeError("invalid reasoning effort")
    st = Path(state).resolve()
    for root in PROTECTED_ROOTS:
        if _is_within(st, root):
            raise BridgeError("state protected")
    ws = validate_workspace(workspace)
    task_text = read_task(task_path)
    baseline = fingerprint(ws, files)
    hid = "handoff-" + uuid.uuid4().hex
    session_id = "codex-relay-" + uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    hdir = st / "handoffs" / hid
    if not _is_within(hdir, st):
        raise BridgeError("handoff dir escapes state")
    hdir.mkdir(parents=True, exist_ok=False)
    manifest_path = hdir / "manifest.json"
    manifest = {
        "handoff_id": hid,
        "workspace": str(ws),
        "baseline": baseline,
        "task_text": task_text,
        "session_id": session_id,
        "request_id": request_id,
        "dispatch_mode": "execute" if execute else "acknowledge",
        "status": "preparing",
    }
    write_json(manifest_path, manifest)

    def update_status(status):
        manifest["status"] = status
        write_json(manifest_path, manifest, overwrite=True)

    try:
        api = API(state)
        created = api.rpc("session/create", {"request": {"sessionId": session_id, "cwd": str(ws), "agentPreset": "standard"}})
        if not isinstance(created, dict) or not created.get("sessionId"):
            raise BridgeError("bad session create")
        sid = created["sessionId"]
        if not isinstance(sid, str) or not sid.startswith("codex-relay-"):
            raise BridgeError("bad session id")
        manifest["session_id"] = sid
        update_status("preparing")
        model = api.rpc("session/selectModel", {"request": {"sessionId": sid, "provider": "deepseek-official", "model": "deepseek-flash", "reasoningEffort": effort}})
        if not isinstance(model, dict):
            raise BridgeError("bad select model")
        selected = model.get("selected")
        if not isinstance(selected, dict):
            raise BridgeError("bad selected model")
        if selected.get("provider") != "deepseek-official" or selected.get("model") != "deepseek-flash":
            raise BridgeError("model route mismatch")
        api.rpc("session/rename", {"request": {"sessionId": sid, "title": title}})
        declared = "\n".join("- " + f for f in baseline.keys())
        action = (
            "\n\n现在直接执行上述已授权任务，无需再等待第二条消息。完成必要的实现、产物生成、"
            "本地检查与原范围内常规修复；遇到范围/设计变化或任务截止，保存状态并报告阻塞。"
            "允许任务所需的公开搜索和GitHub下载至工作区；明确离线任务仍离线。下载不自动授权"
            "安装、执行未知程序、提权或发布。完成后报告路径、验证结果、未验证范围和待裁决问题。"
            if execute else
            "\n\n只确认收到交接，等待用户在DSH继续对话；此条不调用工具、不读写文件。"
        )
        prompt = (task_text + "\n\n声明路径:\n" + declared + action +
                  "不访问密钥、私人会话、科研原始数据。声明路径不是文件权限沙箱。")
        _scan_secret(prompt)
        accepted = api.rpc("session/prompt", {"request": {"sessionId": sid, "requestId": request_id, "mode": "queue", "content": [{"type": "text", "text": prompt}], "clientTimeZone": "Asia/Shanghai"}})
        if not isinstance(accepted, dict) or accepted.get("accepted") is not True:
            raise BridgeError("prompt not accepted")
        manifest["session_id"] = sid
        update_status("submitted")
    except Exception:
        try:
            update_status("unknown_or_failed")
        except Exception:
            pass
        raise BridgeError("handoff failed")
    return {"handoff_id": hid, "session_id": manifest["session_id"], "packet_path": str(manifest_path), "base_url": api.base}


def return_packet(state, handoff_id):
    if not isinstance(handoff_id, str) or not re.match(r"^[A-Za-z0-9_-]{1,80}$", handoff_id):
        raise BridgeError("bad handoff id")
    st = Path(state).resolve()
    for root in PROTECTED_ROOTS:
        if _is_within(st, root):
            raise BridgeError("state protected")
    hdir = st / "handoffs" / handoff_id
    if not _is_within(hdir, st):
        raise BridgeError("handoff dir escapes state")
    manifest_path = hdir / "manifest.json"
    if not manifest_path.is_file():
        raise BridgeError("no manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("handoff_id") != handoff_id:
        raise BridgeError("manifest mismatch")
    if manifest.get("status") != "submitted":
        raise BridgeError("bad status")
    sid = manifest.get("session_id")
    if not isinstance(sid, str) or not sid.startswith("codex-relay-"):
        raise BridgeError("bad session id")
    api = API(state)
    listing = api.rpc("session/list", {"_request": {}})
    if not isinstance(listing, dict):
        raise BridgeError("bad session list")
    items = listing.get("items")
    if not isinstance(items, list):
        raise BridgeError("bad session list")
    found = None
    for it in items:
        if isinstance(it, dict) and it.get("sessionId") == sid:
            found = it
            break
    if found is None or found.get("running") is not False:
        raise BridgeError("session not idle")

    proj = api.rpc("session/projections", {"request": {"sessionId": sid}})
    if not isinstance(proj, dict):
        raise BridgeError("bad projections")
    as_of = proj.get("asOfSeq")
    values = proj.get("values")
    if type(as_of) is not int or as_of < 0 or not isinstance(values, dict):
        raise BridgeError("bad projections")
    inbox = values.get("inbox")
    if isinstance(inbox, dict):
        for key in ("next-turn", "next-step"):
            q = inbox.get(key)
            if isinstance(q, list) and len(q) > 0:
                raise BridgeError("pending queue")
    usage = values.get("tokenUsage")

    records = []
    seen = {}
    before_seq = None
    pages = 0
    complete = False
    min_seq = None
    while pages < 10:
        pages += 1
        page_req = {"request": {"address": {"kind": "session", "sessionId": sid}, "throughSeq": as_of, "maxMessages": 100}}
        if before_seq is not None:
            page_req["request"]["beforeSeq"] = before_seq
        page = api.rpc("session/page", page_req)
        if not isinstance(page, dict):
            raise BridgeError("bad page")
        recs = page.get("records")
        has_more = page.get("hasMore")
        if not isinstance(recs, list) or not isinstance(has_more, bool):
            raise BridgeError("bad page")
        for rec in recs:
            ev = rec.get("event") if isinstance(rec, dict) else None
            if not isinstance(ev, dict) or type(ev.get("seq")) is not int or not 0 <= ev["seq"] <= as_of:
                raise BridgeError("invalid history sequence")
            seq = ev["seq"]
            if seq in seen and seen[seq] != ev:
                raise BridgeError("conflicting history sequence")
            if seq not in seen:
                seen[seq] = ev
                records.append(rec)
            if min_seq is None or seq < min_seq:
                min_seq = seq
        if not has_more:
            complete = True
            break
        if min_seq is None or (before_seq is not None and min_seq >= before_seq):
            raise BridgeError("no progress")
        before_seq = min_seq
    if not complete:
        raise BridgeError("incomplete history")
    records.sort(key=lambda r: r.get("event", {}).get("seq", 0) if isinstance(r, dict) else 0)

    check_idle = api.rpc("session/list", {"_request": {}})
    if not isinstance(check_idle, dict):
        raise BridgeError("bad session list")
    found2 = None
    for it in check_idle.get("items", []):
        if isinstance(it, dict) and it.get("sessionId") == sid:
            found2 = it
            break
    if found2 is None or found2.get("running") is not False:
        raise BridgeError("session not idle at end")
    proj2 = api.rpc("session/projections", {"request": {"sessionId": sid}})
    if not isinstance(proj2, dict) or proj2.get("asOfSeq") != as_of:
        raise BridgeError("projection race")

    messages = public_messages(records)
    baseline = manifest.get("baseline") or {}
    current = fingerprint(manifest.get("workspace"), list(baseline.keys()))
    changed = [k for k in baseline.keys() if baseline.get(k) != current.get(k)]

    rid = "return-" + uuid.uuid4().hex
    packet_dir = hdir / "packets"
    if not _is_within(packet_dir, st):
        raise BridgeError("packet dir escapes state")
    packet_dir.mkdir(parents=True, exist_ok=True)
    json_path = packet_dir / (rid + ".json")
    md_path = packet_dir / (rid + ".md")
    report = {
        "handoff_id": handoff_id,
        "session_id": sid,
        "messages": messages,
        "changed_declared_files": changed,
        "baseline": baseline,
        "current": current,
        "reported_usage": usage,
        "main_agent_acceptance": "pending",
        "coordination": "manual; stop DSH work before Codex resumes; not an OS lock",
    }
    write_json(json_path, report)
    lines = ["# Return %s" % handoff_id, "", "session_id: %s" % sid, ""]
    lines.append("## Messages")
    for m in messages:
        lines.append("### %s (seq %s)" % (m["role"], m["seq"]))
        lines.append(m["text"])
        lines.append("")
    lines.append("## Changed declared files")
    if changed:
        for c in changed:
            lines.append("- %s" % c)
    else:
        lines.append("- none")
    lines.append("")
    lines.append("main_agent_acceptance: pending")
    lines.append("coordination: manual; stop DSH work before Codex resumes; not an OS lock")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"packet_path": str(json_path), "json_path": str(json_path), "markdown_path": str(md_path), "md_path": str(md_path)}


def _cli():
    import argparse
    import webbrowser
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Manual handoff to official DSH Web, with public-only handback.")
    parser.add_argument("--state", type=Path, default=ROOT / "state/dsh")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("open")
    h = sub.add_parser("handoff")
    h.add_argument("--workspace", required=True)
    h.add_argument("--task", required=True)
    h.add_argument("--files", nargs="+", required=True)
    h.add_argument("--execute", action="store_true", help="立即执行明确任务；默认只确认接收")
    h.add_argument("--title", default="Codex交接")
    h.add_argument("--effort", choices=("off", "low", "high", "max"), default="high")
    back = sub.add_parser("return")
    back.add_argument("--id", required=True)
    args = parser.parse_args()
    try:
        if args.command in ("status", "open"):
            api = API(args.state)
            api.rpc("session/modelCatalog", {})
            if args.command == "open":
                webbrowser.open(api.launch)
            result = {"base_url": api.base, "status": "ready"}
        elif args.command == "handoff":
            result = handoff(args.state, args.workspace, args.task, args.files, args.title, args.effort, execute=args.execute)
        else:
            result = return_packet(args.state, args.id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print("DSH action failed: " + type(error).__name__ + "; preserve private state and inspect the packet.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())

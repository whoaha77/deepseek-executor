import sys, os, json, uuid, time, math, ast, tempfile, subprocess, shutil, hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dsh_bridge as b
import check_bundle as c

MAX_AGG = 128 * 1024
MAX_REPAIRS = 2


def _has_reparse_or_symlink(path, stop_at=None):
    p = Path(path)
    try:
        p = Path(os.path.abspath(p))
    except OSError:
        return True
    parts = []
    cur = p
    stop = None if stop_at is None else Path(stop_at).resolve()
    while True:
        parts.append(cur)
        if stop is not None and cur == stop:
            break
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    for comp in parts:
        try:
            st = os.lstat(str(comp))
        except OSError:
            continue
        if stat_is_reparse(st):
            return True
    return False


def stat_is_reparse(st):
    try:
        import stat as _st
        mode = st.st_mode
        if _st.S_ISLNK(mode):
            return True
    except Exception:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    try:
        flag = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if attrs and (attrs & flag):
            return True
    except Exception:
        pass
    return False


def _assert_no_links(ws, rel):
    p = ws / rel
    if _has_reparse_or_symlink(p, stop_at=ws):
        raise ValueError("link component rejected")
    return p


def _read_bounded(path):
    p = Path(path)
    if not p.is_file():
        raise ValueError("not a file")
    size = p.stat().st_size
    if size > b.MAX_FILE_BYTES:
        raise ValueError("file too large")
    with open(p, "rb") as fh:
        raw = fh.read(b.MAX_FILE_BYTES + 1)
    if len(raw) > b.MAX_FILE_BYTES:
        raise ValueError("file grew")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not utf8")
    b._scan_secret(text)
    return size


def _check_invariants(workspace, names, task_path, expected):
    for name in names:
        _assert_no_links(workspace, name)
    if b.fingerprint(workspace, names) != expected:
        raise RuntimeError("declared source drifted")
    return True


def _task_sha(path):
    import hashlib
    p = Path(path)
    with open(p, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _generate(instruction, files, candidate, logprefix, remaining, trace_id):
    if remaining <= 5:
        raise RuntimeError("deadline")
    ask = b.ROOT / "bin" / "ask.py"
    argv = [sys.executable, "-X", "utf8", "-I", str(ask), instruction,
            "--files", *[str(f) for f in files], "--out", str(candidate),
            "--thinking", "off", "--max-tokens", "8192",
            "--timeout", str(int(remaining - 5)), "--retries", "0"]
    env = dict(os.environ)
    env["DEEPSEEK_TRACE_ID"] = trace_id
    env["DEEPSEEK_NODE_ID"] = str(uuid.uuid4())
    so = Path(str(logprefix) + ".stdout.log")
    se = Path(str(logprefix) + ".stderr.log")
    proc = None
    with open(so, "wb") as fo, open(se, "wb") as fe:
        try:
            proc = subprocess.Popen(argv, env=env, stdout=fo, stderr=fe,
                                    **c._popen_kwargs())
        except (OSError, ValueError) as exc:
            raise RuntimeError("launch failed") from exc
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            c._kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise RuntimeError("generation timeout")
    if proc.returncode != 0:
        raise RuntimeError("generation failed")
    return True


def _validate_candidate(candidate, is_py, limit=2 * 1024 * 1024):
    p = Path(candidate)
    if not p.is_file():
        raise ValueError("no candidate")
    with open(p, "rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("candidate too large")
    if not raw or not raw.strip():
        raise ValueError("candidate blank")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("candidate not utf8")
    if not text.strip():
        raise ValueError("candidate blank")
    b._scan_secret(text)
    stripped = text.strip()
    if stripped.startswith("```") or stripped.endswith("```"):
        raise ValueError("candidate contains fences")
    if is_py:
        try:
            ast.parse(text)
        except SyntaxError as exc:
            raise ValueError("syntax: line %s: %s" % (exc.lineno, exc.msg))
    return text


def _feedback(paths, limit=4000):
    pieces = []
    for p in paths:
        try:
            with open(p, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                start = max(0, size - limit)
                fh.seek(start)
                data = fh.read(limit)
            pieces.append(data.decode("utf-8", "replace"))
        except OSError:
            continue
    joined = "\n".join(pieces)
    return joined[-limit:]


def _atomic_write(target, data_bytes):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data_bytes)
        os.replace(tmp, str(target))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _build_parser():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--inputs", nargs="*", default=[])
    ap.add_argument("--out", required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-plan", default=None)
    ap.add_argument("--repair-rounds", type=int, default=1)
    ap.add_argument("--timeout-sec", type=float, default=180)
    return ap


def run(args):
    ws = b.validate_workspace(args.workspace)
    if isinstance(args.repair_rounds, bool) or not isinstance(args.repair_rounds, int) or not 0 <= args.repair_rounds <= MAX_REPAIRS:
        raise ValueError("bad repair rounds")
    t = args.timeout_sec
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 15 <= t <= 3600:
        raise ValueError("bad timeout")
    out = Path(args.out)
    out_abs = out.resolve()
    if out.exists():
        raise ValueError("out exists")
    for prot in b.PROTECTED_ROOTS:
        if b._is_within(out, prot):
            raise ValueError("out protected")
    if _has_reparse_or_symlink(out):
        raise ValueError("out link")
    task_path = Path(args.task)
    if not task_path.is_absolute():
        task_path = ws / task_path
    task_path = task_path.resolve()
    if _has_reparse_or_symlink(task_path, stop_at=ws) and not _is_under(task_path, ws):
        raise ValueError("task link")
    if _has_reparse_or_symlink(task_path.parent):
        raise ValueError("task link")
    target = args.target
    if Path(target).is_absolute() or Path(target).drive:
        raise ValueError("bad target")
    if ".." in Path(target).parts:
        raise ValueError("bad target")
    target_res = (ws / target).resolve()
    if not _is_under(target_res, ws.resolve()):
        raise ValueError("bad target")
    if _has_reparse_or_symlink(ws / target, stop_at=ws):
        raise ValueError("target link")
    if task_path == target_res:
        raise ValueError("task equals target")
    inputs = list(args.inputs or [])
    in_fp = b.fingerprint(ws, inputs) if inputs else {}
    canonical = [target]
    seen_res = {target_res}
    unresolved = []
    for rel in inputs:
        rp = Path(rel)
        if rp.is_absolute() or rp.drive or ".." in rp.parts:
            raise ValueError("bad input")
        res = (ws / rel).resolve()
        if not _is_under(res, ws.resolve()):
            raise ValueError("bad input")
        if _has_reparse_or_symlink(ws / rel, stop_at=ws):
            raise ValueError("input link")
        if res in seen_res:
            if res != target_res:
                raise ValueError("duplicate input")
            continue
        if rel not in in_fp:
            unresolved.append(rel)
            continue
        seen_res.add(res)
        canonical.append(rel)
    if len(canonical) + len(unresolved) > 16:
        raise ValueError("too many files")
    tgt_exists = (ws / target).is_file()
    tgt_fp = b.fingerprint(ws, [target])
    if target not in tgt_fp:
        if tgt_exists:
            raise ValueError("target unreadable")
    names = canonical
    fp_names = [n for n in names if n == target or n in in_fp]
    fps = b.fingerprint(ws, fp_names)
    agg = 0
    for k in names:
        p = ws / k
        if not p.is_file():
            if k == target:
                continue
            raise ValueError("missing input")
        try:
            agg += _read_bounded(p)
        except ValueError:
            raise
    if agg > MAX_AGG:
        raise ValueError("aggregate too large")
    for name in names:
        if b._is_within((ws / name).resolve(), out_abs):
            raise ValueError("file under out")
    if b._is_within(task_path, out_abs):
        raise ValueError("task under out")
    if b._is_within(target_res, out_abs):
        raise ValueError("target under out")
    task_text = b.read_task(task_path)
    plan = None
    plan_path = None
    if args.check_plan:
        plan_path = Path(args.check_plan)
        if not plan_path.is_absolute():
            plan_path = ws / plan_path
        plan_path = plan_path.resolve()
        if plan_path == target_res:
            raise ValueError("plan equals target")
        if _has_reparse_or_symlink(plan_path.parent):
            raise ValueError("plan link")
        if b._is_within(plan_path, out_abs):
            raise ValueError("plan under out")
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:
            raise ValueError("check plan unreadable")
        if not isinstance(plan, dict) or Path(plan.get("workspace", "")).resolve() != ws.resolve():
            raise ValueError("plan workspace mismatch")
        errs = c.validate_plan(plan, out / "preflight")
        if errs:
            raise ValueError("invalid plan: " + errs[0])
    deadline = time.monotonic() + t
    if not args.execute or getattr(args, "dry_run", False):
        return {"status": "dry_run", "receipt": None, "target": target, "attempt_count": 0, "pending": True}
    out.mkdir(parents=True, exist_ok=False)
    trace_id = str(uuid.uuid4())
    receipt = {
        "model_requested": "deepseek-flash",
        "trace_id": trace_id,
        "status": "preparing",
        "attempts": [],
        "main_agent_acceptance": "pending",
        "usage": "unknown",
        "fees": "unknown",
        "baseline": fps,
        "task_sha": _task_sha(task_path),
        "checks": [],
        "candidates": [],
    }
    b.write_json(out / "run.json", receipt)
    expected = dict(fps)
    backup = None
    applied = False
    backed_up = not tgt_exists
    try:
        tgt = ws / target
        suffix = Path(target).suffix
        max_attempts = 1 + args.repair_rounds
        feedback_text = None
        attempt = 0
        while attempt < max_attempts:
            remaining = deadline - time.monotonic()
            if remaining <= 5:
                raise RuntimeError("deadline")
            if _task_sha(task_path) != receipt["task_sha"]:
                raise RuntimeError("task changed")
            _check_invariants(ws, sorted(fp_names), task_path, expected)
            for k in names:
                if _has_reparse_or_symlink(ws / k, stop_at=ws):
                    raise RuntimeError("link drift")
            for k in names:
                if k == target and not tgt_exists:
                    continue
                if (ws / k).is_file():
                    _read_bounded(ws / k)
            attempt += 1
            refs = [task_path]
            for n in names:
                p = ws / n
                if p.is_file():
                    refs.append(p)
            if feedback_text:
                fb = out / "feedback.txt"
                fb.write_text(feedback_text, encoding="utf-8")
                _read_bounded(fb)
                refs.append(fb)
            cand = out / ("candidate-%d%s" % (attempt, suffix))
            rec = {"attempt": attempt, "candidate": str(cand), "status": "generating"}
            receipt["attempts"].append(rec)
            b.write_json(out / "run.json", receipt, overwrite=True)
            instruction = task_text + "\n\nReturn the entire raw file only, no fences."
            try:
                _generate(instruction, refs, cand, out / ("attempt-%d" % attempt), remaining, trace_id)
            except RuntimeError as exc:
                rec["status"] = "generation_failed"
                raise
            receipt["candidates"].append(str(cand))
            try:
                _validate_candidate(cand, suffix == ".py")
            except ValueError as exc:
                rec["status"] = "invalid_candidate"
                if attempt >= max_attempts:
                    raise RuntimeError("invalid candidate")
                fb_parts = [task_text, "Previous candidate failed: " + str(exc)]
                try:
                    fb_parts.append(cand.read_text(encoding="utf-8")[:8000])
                except OSError:
                    pass
                feedback_text = "\n\n".join(fb_parts)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("deadline")
            _check_invariants(ws, sorted(fp_names), task_path, expected)
            if _task_sha(task_path) != receipt["task_sha"]:
                raise RuntimeError("task changed")
            if _has_reparse_or_symlink(tgt, stop_at=ws):
                raise RuntimeError("link drift")
            if not backed_up and tgt.is_file():
                backup = out / ("original" + suffix)
                shutil.copy2(tgt, backup)
                backed_up = True
            data = cand.read_bytes()
            receipt["status"] = "applied_pending_checks"
            b.write_json(out / "run.json", receipt, overwrite=True)
            _atomic_write(tgt, data)
            applied = True
            expected[target] = hashlib.sha256(data).hexdigest()
            rec["status"] = "applied"
            b.write_json(out / "run.json", receipt, overwrite=True)
            if not plan:
                if time.monotonic() > deadline:
                    receipt["status"] = "blocked"
                    b.write_json(out / "run.json", receipt, overwrite=True)
                    return _summary(out, receipt, target)
                receipt["status"] = "generated_pending_review"
                receipt["main_agent_acceptance"] = "pending"
                b.write_json(out / "run.json", receipt, overwrite=True)
                return _summary(out, receipt, target)
            results = []
            failed = False
            for idx, chk in enumerate(plan["checks"]):
                remaining = deadline - time.monotonic()
                if remaining < 1:
                    receipt["status"] = "blocked"
                    b.write_json(out / "run.json", receipt, overwrite=True)
                    raise RuntimeError("deadline before check")
                chk2 = dict(chk)
                chk2["timeout_sec"] = min(float(chk["timeout_sec"]), remaining)
                sub = out / ("check-%d-%s" % (attempt, chk["id"]))
                res = c.run_plan({"workspace": plan["workspace"], "checks": [chk2]}, sub)
                results.append(res)
                entries = [{"id": chk["id"], "attempt": attempt, "status": res.get("status")}]
                for subc in res.get("checks", []):
                    entries.append({"id": subc.get("id"), "attempt": attempt,
                                    "status": subc.get("status"),
                                    "stdout_log": subc.get("stdout_log"),
                                    "stderr_log": subc.get("stderr_log")})
                receipt["checks"].extend(entries)
                b.write_json(out / "run.json", receipt, overwrite=True)
                if res.get("status") != "ok":
                    failed = True
            if time.monotonic() > deadline:
                receipt["status"] = "blocked"
                b.write_json(out / "run.json", receipt, overwrite=True)
                return _summary(out, receipt, target)
            if _task_sha(task_path) != receipt["task_sha"]:
                raise RuntimeError("task changed")
            if any(_has_reparse_or_symlink(ws / n, stop_at=ws) for n in expected) or b.fingerprint(ws, sorted(expected.keys())) != expected:
                receipt["status"] = "needs_attention"
                b.write_json(out / "run.json", receipt, overwrite=True)
                return _summary(out, receipt, target)
            if not failed:
                receipt["status"] = "verified_pending_review"
                b.write_json(out / "run.json", receipt, overwrite=True)
                return _summary(out, receipt, target)
            if attempt >= max_attempts:
                receipt["status"] = "blocked"
                b.write_json(out / "run.json", receipt, overwrite=True)
                return _summary(out, receipt, target)
            logs = []
            for r in results:
                for subc in r.get("checks", []):
                    logs.append(subc.get("stdout_log"))
                    logs.append(subc.get("stderr_log"))
            tail = _feedback([Path(p) for p in logs if p])
            feedback_text = task_text + "\n\nPrevious checks failed:\n" + tail
        receipt["status"] = "blocked"
        b.write_json(out / "run.json", receipt, overwrite=True)
        return _summary(out, receipt, target)
    except Exception as exc:
        receipt["status"] = "blocked"
        receipt["reason"] = type(exc).__name__
        try:
            b.write_json(out / "run.json", receipt, overwrite=True)
        except Exception:
            pass
        return _summary(out, receipt, target)


def _is_under(p, root):
    try:
        Path(p).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _summary(out, receipt, target):
    return {
        "status": receipt.get("status"),
        "receipt": str(out / "run.json"),
        "target": target,
        "attempt_count": len(receipt.get("attempts", [])),
        "pending": receipt.get("main_agent_acceptance") == "pending",
    }


def main(argv=None):
    ap = _build_parser()
    args = ap.parse_args(argv)
    try:
        result = run(args)
    except ValueError as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 1
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}))
        return 1
    print(json.dumps(result))
    return 0 if result.get("status") in ("dry_run", "generated_pending_review", "verified_pending_review") else 1


if __name__ == "__main__":
    sys.exit(main())
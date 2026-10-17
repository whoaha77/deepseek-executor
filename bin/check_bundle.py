import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_CHECKS = 32
MAX_TIMEOUT = 3600
PROTECTED = [Path.home()/"Desktop", Path("C:/Users/Public/Desktop"), Path("D:/protected-data"), Path("D:/research-raw")]
SAFE_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def safe_id(value):
    if not isinstance(value, str) or not value or len(value) > 64:
        return False
    return all(c in SAFE_ID_CHARS for c in value)


def is_protected(path):
    try:
        resolved = Path(path).resolve()
    except OSError:
        return True
    for prot in PROTECTED:
        try:
            pr = prot.resolve()
        except OSError:
            continue
        if resolved == pr or pr in resolved.parents:
            return True
    return False


def validate_plan(plan, out):
    errors = []
    if not isinstance(plan, dict):
        return ["plan must be object"]
    ws = plan.get("workspace")
    if not isinstance(ws, str) or not ws:
        errors.append("workspace missing")
        workspace = None
    else:
        workspace = Path(ws)
        if not workspace.is_dir():
            errors.append("workspace not a directory")
        elif is_protected(workspace):
            errors.append("workspace protected")
    checks = plan.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be nonempty list")
        checks = []
    if len(checks) > MAX_CHECKS:
        errors.append("too many checks")
    seen = set()
    for i, chk in enumerate(checks):
        if not isinstance(chk, dict):
            errors.append("check %d not object" % i)
            continue
        cid = chk.get("id")
        if not safe_id(cid):
            errors.append("check %d bad id" % i)
        elif cid in seen:
            errors.append("duplicate id %s" % cid)
        else:
            seen.add(cid)
        argv = chk.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            errors.append("check %d bad argv" % i)
        else:
            if shutil.which(argv[0]) is None:
                errors.append("check %d executable not found" % i)
        t = chk.get("timeout_sec")
        ok = isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and 1 <= t <= MAX_TIMEOUT
        if not ok:
            errors.append("check %d bad timeout" % i)
    if not isinstance(out, Path):
        errors.append("out invalid")
    elif out.exists():
        errors.append("out exists")
    elif is_protected(out):
        errors.append("out protected")
    return errors


def _kill_tree(proc):
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _popen_kwargs():
    kw = dict(stdin=subprocess.DEVNULL, shell=False)
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return kw


def run_plan(plan, out):
    errors = validate_plan(plan, out)
    if errors:
        return {"status": "invalid", "out": str(out), "errors": errors,
                "results": [], "result_count": 0, "failed_ids": []}
    workspace = Path(plan["workspace"]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    results = []
    try:
        for chk in plan["checks"]:
            results.append(_run_check(chk, workspace, out))
    except KeyboardInterrupt:
        _write_summary(out, results, interrupted=True)
        raise
    summary = _write_summary(out, results, interrupted=False)
    return summary


def _run_check(chk, workspace, out):
    cid = chk["id"]
    tmo = float(chk["timeout_sec"])
    so = out / (cid + ".stdout.log")
    se = out / (cid + ".stderr.log")
    start = time.monotonic()
    status = "failed"
    exit_code = None
    with open(so, "wb") as fo, open(se, "wb") as fe:
        try:
            argv = [shutil.which(chk['argv'][0]), *chk['argv'][1:]]
            proc = subprocess.Popen(argv, cwd=str(workspace),
                                    stdout=fo, stderr=fe, **_popen_kwargs())
        except (OSError, ValueError):
            status = "launch_failed"
            exit_code = 127
            proc = None
        if proc is not None:
            try:
                proc.wait(timeout=tmo)
                exit_code = proc.returncode
                status = "passed" if exit_code == 0 else "failed"
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                status = "timeout"
                exit_code = 124
            except KeyboardInterrupt:
                _kill_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise
    dur = round(time.monotonic() - start, 3)
    out_bytes = so.stat().st_size if so.exists() else 0
    err_bytes = se.stat().st_size if se.exists() else 0
    return {"id": cid, "status": status, "exit_code": exit_code,
            "duration_sec": dur, "stdout_log": str(so), "stderr_log": str(se),
            "stdout_bytes": out_bytes, "stderr_bytes": err_bytes}


def _write_summary(out, results, interrupted):
    failed = [r["id"] for r in results if r["status"] != "passed"]
    status = "ok" if not failed and not interrupted else "failed"
    summary = {
        "status": status,
        "out": str(out),
        "result_count": len(results),
        "failed_ids": failed,
        "main_agent_acceptance": "pending",
        "interrupted": bool(interrupted),
        "checks": results,
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main(argv=None):
    ap = argparse.ArgumentParser(description="deterministic batch checks")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _emit({"status": "invalid", "out": args.out, "result_count": 0,
               "failed_ids": [], "error": "plan unreadable"})
        return 1
    except Exception as exc:
        _emit({'status':'failed','out':args.out,'error':type(exc).__name__})
        return 1
    try:
        summary = run_plan(plan, Path(args.out))
    except KeyboardInterrupt:
        _emit({"status": "interrupted", "out": args.out, "result_count": 0,
               "failed_ids": []})
        return 1
    _emit(summary)
    return 0 if summary["status"] == "ok" else 1


def _emit(summary):
    allowed = ["status", "out", "result_count", "failed_ids", "error"]
    payload = {k: summary.get(k) for k in allowed if k in summary}
    text = json.dumps(payload)
    if len(text) > 1500:
        payload = {k: summary.get(k) for k in ("status", "result_count")}
        payload['summary_path_in_output_directory'] = True
        text = json.dumps(payload)
    print(text)


if __name__ == "__main__":
    sys.exit(main())

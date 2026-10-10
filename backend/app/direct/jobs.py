"""The direct report's background run: read the catalogs (ATNM databases, then AlTasnimBI),
grade every mapping, write the files. Its own state, log and folder - nothing of the
migration report (app/report) is read or written. Each catalog read takes the run guard,
so it never runs at the same time as a heavy step of another run, and a lost connection
(VPN) is tried again, then waited for.
"""
import json
import re
import shutil
import threading
from datetime import datetime, timezone

from .. import runguard
from .. import mapping as plan_mod
from ..ATNM import catalog, conn
from ..ATNM import settings as atnm_settings
from ..report import plain
from . import check, settings

STAGES = (("catalogs", "Read the table lists"), ("mappings", "Check every mapping"), ("files", "Write the report"))

_lock = threading.Lock()
_cancel = threading.Event()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fresh_job():
    return {"running": False, "cancelling": False, "run_id": None, "started_at": None, "finished_at": None,
            "stage": None, "error": None, "result": None, "test": None, "waiting": None,
            "stages": {k: {"title": t, "status": "waiting", "done": 0, "total": 0, "current": None} for k, t in STAGES}}


job = _fresh_job()
log = {"seq": 0, "items": []}


def _log(text, level="info"):
    with _lock:
        log["seq"] += 1
        log["items"].append({"seq": log["seq"], "at": _now(), "level": level, "text": text})
        del log["items"][:-500]


def _stage(key, status, **facts):
    s = job["stages"][key]
    if status == "running" and s["status"] != "running":
        job["stage"] = key
        _log(f"{s['title']}: started.")
    s["status"] = status
    s.update(facts)


def labels():
    return {"source": atnm_settings.SOURCE.label, "target": atnm_settings.TARGET.label}


def _servers():
    return {"source": atnm_settings.SOURCE, "target": atnm_settings.TARGET}


# ---- reading a catalog, with retries -----------------------------------------------------------

def _read(server_key, database):
    """The catalog of one database (fresh, not shared with the other pages), or {"error": text}
    once the run is cancelled or the failure is not a lost connection."""
    server = _servers()[server_key]
    waits = list(atnm_settings.RETRY_WAITS)
    while True:
        try:
            with runguard.slot(f"the direct report ({database})", cancelled=_cancel.is_set):
                return catalog._read(server, database)
        except runguard.Cancelled:
            return {"error": "Stopped."}
        except Exception as exc:
            text, todo = conn.explain(exc, server, database)
            if conn.kind(exc) != "connection":
                _log(f"{database}: {text}", "error")
                return {"error": text + (f" {todo}" if todo else "")}
            wait = waits.pop(0) if waits else atnm_settings.PAUSE_POLL
            job["waiting"] = f"{server.label} cannot be reached (VPN?) - trying again in {wait} s. Stop ends the run."
            _log(f"{database}: {text} Trying again in {wait} s.", "warn")
            if _cancel.wait(wait):
                job["waiting"] = None
                return {"error": "Stopped."}
            job["waiting"] = None


# ---- the run -----------------------------------------------------------------------------------------

def _pick_test(items, n):
    """The n smallest mappings that have client records (catalog counts), in plan order."""
    sized = [(sum(s["rows"] or 0 for s in i["sources"]), k) for k, i in enumerate(items)
             if any(s["rows"] for s in i["sources"]) and i["type"] not in ("excluded", "transform")]
    keep = {k for _, k in sorted(sized)[:n]}
    return [i for k, i in enumerate(items) if k in keep]


def _work(run_id):
    try:
        plan = plan_mod.load()
        dbs = check.databases(plan)
        _stage("catalogs", "running", total=len(dbs), done=0)
        cats = {}
        for n, (server, db) in enumerate(dbs, 1):
            if _cancel.is_set():
                break
            job["stages"]["catalogs"]["current"] = f"{_servers()[server].label} · {db}"
            cat = _read(server, db)
            cats[(server, db.lower())] = cat
            _log(f"{db}: {len(cat['tables'])} tables read." if "tables" in cat else f"{db}: not read - {cat['error']}",
                 "ok" if "tables" in cat else "warn")
            _stage("catalogs", "running", done=n)
        _stage("catalogs", "stopped" if _cancel.is_set() else "done", current=None)

        if not _cancel.is_set():
            _stage("mappings", "running", total=len(plan.mappings), done=0)
            items = []
            for n, m in enumerate(plan.mappings, 1):
                job["stages"]["mappings"]["current"] = m.id
                items.append(check.evaluate(m, plan, cats, labels()))
                job["stages"]["mappings"]["done"] = n
            if settings.TEST_MAPPINGS:
                items = _pick_test(items, settings.TEST_MAPPINGS)
                job["test"] = (f"TEST RUN: only the {len(items)} smallest of {len(plan.mappings)} mappings were checked. "
                               "This is not the final result.")
                _log(job["test"], "warn")
            _stage("mappings", "done", current=None)

            _stage("files", "running")
            ev = _evidence(run_id, plan, cats, items)
            meta = _write(run_id, ev)
            job["result"] = meta
            _stage("files", "done")
            _log(f"Report ready: {ev['overall']}.", "ok")
        else:
            _log("Stopped: no report was written.", "warn")
    except Exception as exc:          # the run ends with the reason; nothing is left half written
        job["error"] = str(exc)
        _log(f"The run failed: {exc}", "error")
        for s in job["stages"].values():
            if s["status"] == "running":
                s["status"] = "failed"
    finally:
        job.update(running=False, cancelling=False, finished_at=_now(), waiting=None)


def _evidence(run_id, plan, cats, items):
    issues = []
    for i in items:
        for x in i["issues"]:
            issues.append(x)
    issues.sort(key=lambda x: (plain.ORDER[x["result"]], x["mapping"]))
    for n, x in enumerate(issues, 1):
        x["id"] = f"D-{n:03d}"
    tally = {r: sum(1 for i in items if i["result"] == r) for r in plain.RESULTS}
    return {
        "run": {"id": run_id, "started_at": job["started_at"], "finished_at": _now(), "test": job["test"]},
        "overall": plain.overall(i["result"] for i in items),
        "totals": tally,
        "items": items,
        "issues": issues,
        "tables": check.table_count(plan, cats, labels()),
        "target_db": check.target_db(),
        "databases": [{"server": labels()[s], "database": d} for s, d in check.databases(plan)],
        "version": "catalogs: tables, record counts, columns (values not compared)",
    }


# ---- files ---------------------------------------------------------------------------------------------

_RUN_ID = re.compile(r"^\d{8}-\d{6}$")


def _folder(run_id):
    return settings.REPORT_DIR / run_id


def _write(run_id, ev):
    from . import xlsx
    folder = _folder(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    name = f"Direct_Migration_Report_{stamp}{'_TEST' if ev['run']['test'] else ''}.xlsx"
    xlsx.build(ev, folder / name)
    (folder / "evidence.json").write_text(json.dumps(ev, indent=1, default=str), encoding="utf-8")
    meta = {"run_id": run_id, "started_at": ev["run"]["started_at"], "finished_at": ev["run"]["finished_at"],
            "overall": ev["overall"], "totals": ev["totals"], "issues": len(ev["issues"]),
            "test": ev["run"]["test"], "files": {"xlsx": name}}
    (folder / "report.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    _prune()
    return meta


def reports():
    """The saved direct reports, newest first."""
    out = []
    if settings.REPORT_DIR.exists():
        for f in settings.REPORT_DIR.glob("*/report.json"):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda r: r.get("run_id") or "", reverse=True)


def _prune():
    for old in reports()[settings.KEEP:]:
        if _RUN_ID.match(old.get("run_id") or ""):
            shutil.rmtree(_folder(old["run_id"]), ignore_errors=True)


def file_of(run_id):
    """The workbook of a saved run, or None."""
    if not _RUN_ID.match(run_id or ""):
        return None
    meta = next((r for r in reports() if r["run_id"] == run_id), None)
    if not meta:
        return None
    path = _folder(run_id) / meta["files"]["xlsx"]
    return path if path.exists() else None


# ---- start, stop, status -------------------------------------------------------------------------------

def busy():
    """Other runs going in this backend (read only: their state is never changed here)."""
    out = []
    try:
        from ..report import jobs as report_jobs
        if report_jobs.job.get("running"):
            out.append("the migration report")
    except Exception:
        pass
    try:
        from ..ATNM import jobs as atnm_jobs
        if atnm_jobs.job.get("running"):
            out.append("the ATNM copy check")
    except Exception:
        pass
    try:
        from .. import main
        if main._checks.job.get("running"):
            out.append("the RDS data check run")
    except Exception:
        pass
    return out


def preflight():
    out = {"atnm": {"ok": False, "label": atnm_settings.SOURCE.label, "error": None},
           "rds": {"ok": False, "label": atnm_settings.TARGET.label, "error": None},
           "plan": None, "busy": busy(), "test": settings.TEST_MAPPINGS or None}
    try:
        plan = plan_mod.load()
        dbs = check.databases(plan)
        out["plan"] = {"ok": True, "mappings": len(plan.mappings),
                       "databases": [{"server": labels()[s], "database": d} for s, d in dbs]}
    except Exception as exc:
        out["plan"] = {"ok": False, "error": str(exc)}
        dbs = []
    for key, server, name in (("atnm", "source", None), ("rds", "target", check.target_db())):
        db = name or next((d for s, d in dbs if s == "source"), None)
        if not db:
            out[key]["error"] = "No ATNM database pair matches the plan (ATNM_DB_<n>_SOURCE / _TARGET in .env)."
            continue
        out[key]["ok"] = conn.reachable(_servers()[server], db)
        if not out[key]["ok"]:
            out[key]["error"] = f"{_servers()[server].label} cannot be reached" + (" (VPN?)" if server == "source" else "")
    out["ok"] = out["atnm"]["ok"] and out["rds"]["ok"] and bool(out["plan"] and out["plan"]["ok"])
    return out


def start():
    with _lock:
        if job["running"]:
            return False, "A direct report is already being made."
        _cancel.clear()
        fresh = _fresh_job()
        job.clear()
        job.update(fresh)
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        job.update(running=True, run_id=run_id, started_at=_now())
        log["items"].clear()
    _log("Direct report started (read-only: catalogs only).")
    threading.Thread(target=_work, args=(run_id,), daemon=True, name="direct-report").start()
    return True, None


def cancel():
    if not job["running"]:
        return False
    job["cancelling"] = True
    _cancel.set()
    _log("Stopping…", "warn")
    return True


def status(since=None):
    out = {**job, "stages": [{"key": k, **v} for k, v in job["stages"].items()], "log_seq": log["seq"]}
    if since is not None:
        out["log"] = [e for e in log["items"] if e["seq"] > since]
    return out

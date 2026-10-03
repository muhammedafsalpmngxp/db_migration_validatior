"""The report run: check everything live, then write one Word and one Excel file.

    1. Preflight   both servers answer, the plan reads, no other run is going
    2. Section 1      the ATNM copy check of the required tables (app/ATNM/jobs.py)
    3. Section 2      every mapping: data check, key mapping, renames, target table checks
                   ("Re-run all data checks", app/main.py)
    4. Analysis    catalogs and row counts read again (tables that changed meanwhile are
                   marked), every result collected and graded (collect.py)
    5. Summary     written by the AI from the facts (synth.py)
    6. Files       Excel and Word, then the self-check (validate.py)

Parts 1 and 2 are the app's own runs, so they keep their behaviour: read-only, one heavy
step at a time on the shared servers (app/runguard.py), waiting out a lost VPN. Only the
results measured during this run count. A stopped run still writes a (partial) report; a
run cut short by a restart can be resumed, skipping what it had already checked.
"""
import json
import logging
import shutil
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import collect, docx_writer, plain, settings, synth, testmode, validate, xlsx_writer

logger = logging.getLogger("report")

STAGES = [("preflight", "Preflight"), ("part1", "Section 1 · copy ATNM → RDS"), ("part2", "Section 2 · RDS → new system"),
          ("collect", "Final count check and analysis"), ("ai", "Writing the summary"),
          ("files", "Building the Word and Excel files")]
WEIGHT = {"preflight": 0.02, "part1": 0.55, "part2": 0.33, "collect": 0.05, "ai": 0.03, "files": 0.02}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Log:
    """What the run did, for the page (newest last) and the backend console."""

    def __init__(self, keep=2000):
        self.items, self.seq, self.lock = deque(maxlen=keep), 0, threading.Lock()

    def clear(self):
        with self.lock:
            self.items.clear()

    def add(self, text, level="info", stage=None):
        with self.lock:
            self.seq += 1
            self.items.append({"seq": self.seq, "at": _now(), "level": level, "text": text, "stage": stage})
        getattr(logger, {"error": "error", "warn": "warning"}.get(level, "info"))(text)

    def since(self, seq):
        with self.lock:
            return [x for x in self.items if x["seq"] > seq]

    def all(self):
        with self.lock:
            return list(self.items)


log = Log()
_lock = threading.Lock()
_cancel = threading.Event()


def _blank_job():
    return {"running": False, "cancelling": False, "run_id": None, "started_at": None, "finished_at": None,
            "mode": None, "stage": None, "error": None, "result": None, "test": None,
            "stages": {k: {"title": t, "status": "waiting", "done": 0, "total": 0, "current": None, "step": None,
                           "fraction": None, "eta_seconds": None, "waiting": None, "started_at": None,
                           "finished_at": None, "note": None} for k, t in STAGES}}


job = _blank_job()


# ---- the run file (resume after a restart) ----------------------------------------------------

def _write_run(state):
    try:
        settings.RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        settings.RUN_FILE.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def _read_run():
    try:
        return json.loads(settings.RUN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def interrupted():
    """The run that a restart cut short (resume it to go on), or None."""
    r = _read_run()
    if r and r.get("status") == "running" and not job["running"]:
        return r
    return None


# ---- preflight --------------------------------------------------------------------------------

def preflight():
    """Light checks before a run: servers, plan, other runs, AI. Nothing heavy is read."""
    from .. import ai, main
    from ..ATNM import api as atnm_api
    from ..ATNM import jobs as atnm_jobs
    from ..ATNM import required as atnm_required
    from ..ATNM import settings as atnm_settings

    out = {"atnm": None, "rds": None, "plan": None, "busy": None, "ai": None, "test": None}
    try:
        h = atnm_api.health()
        src = next((s for s in h["servers"] if s["key"] == "source"), None)
        out["atnm"] = {"ok": bool(src and src["ok"]), "label": atnm_settings.SOURCE.label,
                       "error": None if src and src["ok"] else "; ".join(dict.fromkeys(
                           (d.get("error") or {}).get("message", "") for d in (src or {}).get("databases", []) if not d["ok"]))
                       or "The server cannot be reached. Connect the VPN."}
    except Exception as exc:
        out["atnm"] = {"ok": False, "label": "ATNM", "error": str(exc)}
    try:
        h = main.health()
        bad = [d for d in h["databases"] if not d["ok"]]
        out["rds"] = {"ok": not bad, "label": atnm_settings.TARGET.label,
                      "error": "; ".join(f"{d['name']}: {d['error']}" for d in bad) or None}
    except Exception as exc:
        out["rds"] = {"ok": False, "label": "RDS", "error": str(exc)}
    try:
        plan = main.get_plan()
        req = {p.id: atnm_required.tables(p) for p in atnm_settings.PAIRS}
        out["plan"] = {"ok": True, "tables": sum(len(v) for v in req.values()), "mappings": len(plan.mappings),
                       "databases": [{"source_db": p.source_db, "target_db": p.target_db, "tables": len(req[p.id])}
                                     for p in atnm_settings.PAIRS]}
        out["test"] = testmode.info(out["plan"]["tables"], out["plan"]["mappings"])
    except Exception as exc:
        out["plan"] = {"ok": False, "error": f"The migration plan cannot be read: {exc}"}
    busy = []
    if atnm_jobs.job.get("running") and not job["running"]:
        busy.append("the ATNM copy check")
    if main._checks.job.get("running") and not job["running"]:
        busy.append("the RDS data check run")
    out["busy"] = busy
    a = ai.status()
    out["ai"] = {"enabled": bool(settings.AI and a["enabled"]), "model": a.get("model"),
                 "reason": None if settings.AI and a["enabled"] else ("REPORT_AI=no" if not settings.AI else a["reason"])}
    out["ok"] = bool(out["atnm"]["ok"] and out["rds"]["ok"] and out["plan"] and out["plan"].get("ok") and not busy)
    return out


# ---- the run ----------------------------------------------------------------------------------

def _stage(name, status, **facts):
    s = job["stages"][name]
    if status == "running" and s["status"] != "running":
        s["started_at"] = _now()
        job["stage"] = name
        log.add(f"{s['title']}: started.", "info", stage=name)
    if status in ("done", "failed", "skipped", "stopped"):
        s["finished_at"] = _now()
    s["status"] = status
    s.update(facts)
    _write_run({"run_id": job["run_id"], "started_at": job["started_at"], "stage": job["stage"],
                "status": "running" if job["running"] else "finished", "test": job.get("test")})


class _Failed(Exception):
    """The run cannot go on; the message says why in plain words."""


def _wait(seconds):
    """Sleep, waking at once when the run is cancelled."""
    _cancel.wait(seconds)


def _part1(resume, since):
    from ..ATNM import jobs as atnm_jobs
    from ..ATNM import required as atnm_required
    from ..ATNM import settings as atnm_settings

    pairs = atnm_settings.PAIRS
    only = {p.id: set(atnm_required.tables(p)) for p in pairs}
    if job.get("test"):                           # test mode: only the picked tables
        only = {pid: keys & testmode.keep1(job["test"], pid) for pid, keys in only.items()}
    total = sum(len(v) for v in only.values())
    skip = set()
    if resume:
        for p in pairs:
            for key in only[p.id]:
                r = atnm_jobs.store.get(p.id, key)
                if r and (r.get("checked_at") or "") >= since and r.get("status") in ("identical", "different"):
                    skip.add((p.id, key))
        if skip:
            log.add(f"Section 1: {len(skip)} of {total} tables were checked before the restart; they are left out.",
                    stage="part1")
    _stage("part1", "running", total=total, done=len(skip), note=f"{total} required tables")
    if not total:
        log.add("Section 1: no table to check.", "warn", stage="part1")
        _stage("part1", "done", fraction=1.0)
        return
    while atnm_jobs.job.get("running"):           # someone started it meanwhile: let it finish first
        _stage("part1", "running", waiting="Another ATNM copy check is running; waiting for it.")
        _wait(5)
        if _cancel.is_set():
            return
    if not atnm_jobs.start(pairs, None, only=only, tables="required", skip=skip or None):
        raise _Failed("The ATNM copy check could not be started.")
    seen = None
    while True:
        st = atnm_jobs.status()
        current = f"{st.get('database')} · {st.get('current')}" if st.get("current") else None
        if current and current != seen:
            seen = current
            log.add(f"Section 1 · table {min(st.get('done', 0) + 1, st.get('total') or total)} of "
                    f"{st.get('total') or total}: {current}", stage="part1")
        prog = st.get("progress") or {}
        waiting = st.get("waiting")
        _stage("part1", "running", done=len(skip) + (st.get("done") or 0), total=total, current=current,
               step=st.get("step"), fraction=prog.get("fraction"), eta_seconds=prog.get("eta_seconds"),
               waiting=(waiting.get("reason") + (" " + waiting["hint"] if waiting.get("hint") else "")) if waiting else None)
        if not st.get("running"):
            break
        if _cancel.is_set() and not st.get("cancelling"):
            log.add("Stopping the ATNM copy check (the table in progress is not saved).", "warn", stage="part1")
            atnm_jobs.cancel()
        _wait(2)
    st = atnm_jobs.status()
    if st.get("error"):
        log.add(f"Section 1 ended early: {st['error']}", "error", stage="part1")
    res = st.get("results") or {}
    log.add(f"Section 1 finished: {', '.join(f'{v} {k}' for k, v in res.items()) or 'nothing checked'}.",
            "ok" if not st.get("error") else "warn", stage="part1")
    _stage("part1", "stopped" if _cancel.is_set() else "done", current=None, step=None, waiting=None, fraction=1.0,
           eta_seconds=None)


def _part2(resume, since):
    from .. import main

    plan = main.get_plan()
    mappings = list(plan.mappings)
    if job.get("test"):                           # test mode: only the picked mappings
        keep = testmode.keep2(job["test"])
        mappings = [m for m in mappings if m.id in keep]
    total = len(mappings)
    done_before = 0
    if resume:
        keep = []
        for m in mappings:
            r = main._checks.get(m.id)
            if r and (r.get("checked_at") or "") >= since:
                done_before += 1
            else:
                keep.append(m)
        mappings = keep
        if done_before:
            log.add(f"Section 2: {done_before} of {total} mappings were checked before the restart; they are left out.",
                    stage="part2")
    _stage("part2", "running", total=total, done=done_before, note=f"{total} mappings")
    if not mappings:
        _stage("part2", "done", fraction=1.0)
        return
    while not main._checks.run_all(mappings, main._run_all_checks):
        _stage("part2", "running", waiting="Another RDS data check run is going; waiting for it.")
        _wait(5)
        if _cancel.is_set():
            return
    seen = None
    while True:
        j = dict(main._checks.job)
        cur = j.get("current")
        if cur and cur != seen:
            seen = cur
            m = next((x for x in mappings if x.id == cur), None)
            what = f"{cur} ({', '.join(t.ref.ref.split('.', 1)[1] for t in m.targets)})" if m and m.targets else cur
            log.add(f"Section 2 · mapping {done_before + (j.get('done') or 0) + 1} of {total}: {what}", stage="part2")
        step = j.get("step")
        _stage("part2", "running", done=done_before + (j.get("done") or 0), total=total, current=cur,
               step=step, fraction=(done_before + (j.get("done") or 0)) / total if total else 1.0,
               waiting=step if step and step.startswith("waiting") else None)
        if not j.get("running"):
            break
        if _cancel.is_set() and not j.get("cancelling"):
            log.add("Stopping the RDS run after the mapping in progress.", "warn", stage="part2")
            main._checks.cancel()
        _wait(2)
    log.add(f"Section 2 finished: {main._checks.job.get('done', 0)} mappings checked.", "ok", stage="part2")
    _stage("part2", "stopped" if _cancel.is_set() else "done", current=None, step=None, waiting=None, fraction=1.0)


def _folder(run_id):
    return settings.REPORT_DIR / run_id


def _write_files(ev, run_id):
    """Excel, Word and the self-check of evidence `ev`; returns the report's summary record."""
    folder = _folder(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromisoformat(ev["run"]["started_at"]).astimezone().strftime("%Y-%m-%d_%H%M")
    tag = "_TEST" if ev["run"].get("test") else ""
    xlsx, docx = folder / f"Migration_Report_{stamp}{tag}.xlsx", folder / f"Migration_Report_{stamp}{tag}.docx"
    xlsx_writer.build(ev, xlsx)
    docx_writer.build(ev, docx)
    problems = validate.evidence(ev, ev["run"].get("expected_tables"), ev["run"].get("expected_mappings"))
    problems += validate.files(ev, xlsx, docx)
    meta = {"run_id": run_id, "started_at": ev["run"]["started_at"], "finished_at": ev["run"].get("finished_at"),
            "mode": ev["run"].get("mode"), "overall": ev["overall"], "totals": ev["totals"],
            "not_checked": len(ev["not_checked"]), "summary_source": (ev.get("summary") or {}).get("source"),
            "files": {"xlsx": xlsx.name, "docx": docx.name}, "self_check": problems, "built_at": _now(),
            "test": (ev["run"].get("test") or {}).get("text")}
    (folder / "evidence.json").write_text(json.dumps(ev, default=str), encoding="utf-8")
    (folder / "report.json").write_text(json.dumps(meta, default=str, indent=1), encoding="utf-8")
    return meta


def _prune():
    runs = sorted((p for p in settings.REPORT_DIR.glob("*") if (p / "report.json").exists()), key=lambda p: p.name)
    for old in runs[:-settings.KEEP]:
        shutil.rmtree(old, ignore_errors=True)


def _work(resume, expected):
    run = {"id": job["run_id"], "started_at": job["started_at"], "mode": job["mode"],
           "expected_tables": expected.get("tables"), "expected_mappings": expected.get("mappings")}
    since = job["started_at"]
    try:
        if job.get("test") is None and testmode.on():
            try:
                job["test"] = testmode.pick()
            except Exception as exc:
                raise _Failed(str(exc)) from exc
            _stage("preflight", "done",       # also keeps the choice in the run file for a resume
                   note=f"{testmode.LABEL}: {testmode.count1(job['test'])} tables, {len(job['test']['part2'])} mappings")
        if job.get("test"):
            sel = job["test"]
            run.update(test=sel, expected_tables=testmode.count1(sel), expected_mappings=len(sel["part2"]))
            log.add(sel["text"], "warn")
            log.add("Test tables (Section 1): " + (", ".join(k for v in sel["part1"].values() for k in v) or "none") + ".",
                    stage="preflight")
            log.add("Test mappings (Section 2): " + (", ".join(sel["part2"]) or "none") + ".", stage="preflight")
        if not _cancel.is_set():
            _part1(resume, since)
        else:
            _stage("part1", "skipped")
        if not _cancel.is_set():
            _part2(resume, since)
        else:
            _stage("part2", "skipped")
        stopped = _cancel.is_set()
        if stopped:
            run["mode"] = (run["mode"] or "") + " · stopped early: partial report"
            log.add("The run was stopped: the report shows what was checked; the rest is listed as not checked.", "warn")

        _stage("collect", "running", step="Reading the catalogs and row counts again, and collecting every result.")
        ev = collect.build(run, since)
        _stage("collect", "done", step=None, note=f"{len(ev['issues'])} issues, {len(ev['not_checked'])} not checked")
        log.add(f"Analysis: overall {ev['overall']}; {ev['totals']['issues'][plain.MUST_FIX]} must be fixed, "
                f"{ev['totals']['issues'][plain.DECIDE]} need a decision, {len(ev['not_checked'])} not checked.",
                "ok", stage="collect")

        _stage("ai", "running", step="Writing the summary from the facts.")
        ev["summary"] = synth.write(ev, note=lambda text, level="info": log.add(text, level, stage="ai"))
        _stage("ai", "done", step=None, note="written by AI, checked" if ev["summary"]["source"] == "ai"
               else f"plain sentences ({ev['summary'].get('why')})")

        _stage("files", "running", step="Writing the Excel and Word files, then checking them.")
        ev["run"]["finished_at"] = _now()
        ev["log"] = log.all()
        meta = _write_files(ev, job["run_id"])
        if meta["self_check"]:
            for p in meta["self_check"]:
                log.add(f"Self-check: {p}", "error", stage="files")
        _stage("files", "done", step=None, note="self-check passed" if not meta["self_check"]
               else f"{len(meta['self_check'])} self-check problems")
        _prune()
        job["result"] = meta
        log.add(f"Report ready: {meta['files']['docx']} and {meta['files']['xlsx']} ({ev['overall']}).", "ok")
    except _Failed as exc:
        job["error"] = str(exc)
        log.add(f"The report could not be made: {exc}", "error")
        if job["stage"]:
            _stage(job["stage"], "failed")
    except Exception as exc:
        logger.exception("report run failed")
        job["error"] = f"Unexpected error: {exc}"
        log.add(job["error"], "error")
        if job["stage"]:
            _stage(job["stage"], "failed")
    finally:
        job.update(running=False, cancelling=False, finished_at=_now())
        _write_run({"run_id": job["run_id"], "started_at": job["started_at"], "stage": job["stage"],
                    "status": "failed" if job["error"] else "finished", "test": job.get("test")})


def start(resume=False):
    """Start a report run (or resume the one a restart cut short). Returns (started, message)."""
    global job
    with _lock:
        if job["running"]:
            return False, "A report is already being made."
        pf = preflight()
        if not pf["ok"]:
            why = [x for x in (
                None if pf["atnm"]["ok"] else f"{pf['atnm']['label']}: {pf['atnm']['error']}",
                None if pf["rds"]["ok"] else f"{pf['rds']['label']}: {pf['rds']['error']}",
                None if pf["plan"] and pf["plan"].get("ok") else (pf["plan"] or {}).get("error"),
                f"Wait until {' and '.join(pf['busy'])} has finished." if pf["busy"] else None) if x]
            return False, " ".join(why)
        before = interrupted() if resume else None
        if resume and not before:
            return False, "There is no interrupted report run to resume."
        job = _blank_job()
        _cancel.clear()
        log.clear()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        test = (before or {}).get("test")
        job.update(running=True, run_id=before["run_id"] if before else stamp,
                   started_at=before["started_at"] if before else _now(),
                   mode="Resumed after a restart" if before else "Full live check", test=test)
        if test or (testmode.on() and not before):
            job["mode"] = f"{testmode.LABEL}{' · resumed after a restart' if before else ''}"
        _stage("preflight", "done", note=f"{pf['plan']['tables']} tables, {pf['plan']['mappings']} mappings; AI "
                                         f"{'on' if pf['ai']['enabled'] else 'off'}")
        if job["test"] or testmode.on():
            log.add(f"Report test run {job['run_id']} started: only a few of the smallest tables are checked, live, "
                    "one step at a time.", "ok")
        else:
            log.add(f"Report run {job['run_id']} started: {pf['plan']['tables']} required tables (Section 1) and "
                    f"{pf['plan']['mappings']} mappings (Section 2), checked live, one step at a time.", "ok")
    threading.Thread(target=_work, args=(resume, {"tables": pf["plan"]["tables"], "mappings": pf["plan"]["mappings"]}),
                     daemon=True, name="report").start()
    return True, "Started."


def cancel():
    if not job["running"]:
        return False
    job["cancelling"] = True
    _cancel.set()
    log.add("Stop requested: the step in progress finishes, then a partial report is written.", "warn")
    return True


def status(since=None):
    out = {k: v for k, v in job.items() if k != "stages"}
    out["stages"] = [{"key": k, **job["stages"][k]} for k, _ in STAGES]
    done = 0.0
    for k, _ in STAGES:
        s = job["stages"][k]
        part = 1.0 if s["status"] in ("done", "skipped", "stopped") else (
            s["fraction"] if s["fraction"] is not None else (s["done"] / s["total"] if s["total"] else 0.0)
            if s["status"] == "running" else 0.0)
        done += WEIGHT[k] * min(1.0, max(0.0, part))
    out["fraction"] = round(done, 4) if job["run_id"] else None
    out["interrupted"] = interrupted()
    out["log_seq"] = log.seq
    if since is not None:
        out["log"] = log.since(since)
    return out


# ---- finished reports ---------------------------------------------------------------------------

def reports():
    out = []
    if settings.REPORT_DIR.exists():
        for p in sorted(settings.REPORT_DIR.glob("*/report.json"), reverse=True):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out


def file_of(run_id, kind):
    """The path of a report file, or None. `run_id` must be one of the saved runs."""
    meta = next((r for r in reports() if r["run_id"] == run_id), None)
    if not meta or kind not in ("docx", "xlsx"):
        return None
    path = _folder(run_id) / meta["files"][kind]
    return path if path.exists() else None


def rebuild(run_id):
    """Write the files of a saved run again from its evidence (no database is read); the AI
    summary is reused when the facts are unchanged. Returns the summary record."""
    folder = _folder(run_id)
    ev = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
    ev["summary"] = synth.write(ev, note=lambda text, level="info": log.add(text, level, stage="ai"))
    return _write_files(ev, run_id)

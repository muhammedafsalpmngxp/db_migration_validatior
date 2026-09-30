"""Saved data check results, and the background run that checks many tables in a row.

Results are kept per pair and table in ATNM_RESULT_FILE (a JSON file), written after every
table. A check that could not finish (connection lost, timeout, lock) never replaces a
measured result: it is kept beside it as `last_attempt`.

A run
  - waits while an RDS -> AlTasnimBI run is going (both read RDS: one heavy run at a time);
  - tries a lost connection again after ATNM_RETRY_WAITS, then pauses and tries every
    ATNM_PAUSE_POLL seconds, going on by itself once the server answers (VPN back);
  - tries a locked table once more at the end;
  - stops on a refused login or a setting that is wrong, which waiting cannot fix;
  - is written to ATNM_RUN_FILE as it goes, so after a restart it can be resumed, skipping
    the tables it had finished.
One run at a time.
"""
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

from . import activity, catalog, conn, datacheck, options, settings

MEASURED = ("identical", "different", "skipped")
FAILED = ("error", "timeout", "changed", "locked")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(n):
    return f"{n:,}"


class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.results = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.results = {}

    @staticmethod
    def _id(pair_id, key):
        return f"{pair_id}|{key.lower()}"

    def get(self, pair_id, key):
        return self.results.get(self._id(pair_id, key))

    def put(self, result):
        """Save a result. A failed attempt does not replace a measured result: the measured
        one stays, with the attempt beside it."""
        with self.lock:
            rid = self._id(result["pair"], result["key"])
            old = self.results.get(rid)
            if result.get("status") in FAILED and old and old.get("status") in MEASURED:
                kept = dict(old)
                kept["last_attempt"] = {k: result.get(k) for k in ("status", "headline", "checked_at", "error_kind")}
                self.results[rid] = kept
            else:
                self.results[rid] = result
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self.results, default=str), encoding="utf-8")
                tmp.replace(self.path)      # never leave a half-written file behind
            except OSError:
                pass


store = Store(settings.RESULT_FILE)

_job_lock = threading.Lock()
job = {"running": False, "cancelling": False, "done": 0, "total": 0, "current": None, "pair": None,
       "started_at": None, "finished_at": None, "error": None, "scope": None,
       "database": None, "table_started_at": None, "table_rows": None, "results": {},
       "waiting": None, "rows_total": 0, "rows_done": 0, "read": None, "speed": {"rows": 0, "seconds": 0.0},
       "resumed_from": None, "interrupted": None}
_ctx = None
log = activity.Activity()

RESULT_WORDS = {"identical": "Identical", "different": "Different", "error": "Failed", "locked": "Locked",
                "skipped": "Skipped", "timeout": "Timed out", "changed": "Changed during the check"}
RESULT_LEVEL = {"identical": "ok", "different": "warn", "error": "error", "locked": "warn", "skipped": "warn",
                "timeout": "error", "changed": "warn"}
OVERHEAD_ROWS = 1000    # progress weight of a table beyond its rows: an empty table still takes a moment


# ---- the run file: what a restart needs to resume -------------------------------------------

def _read_run():
    try:
        return json.loads(settings.RUN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_run(state):
    try:
        settings.RUN_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings.RUN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, default=str), encoding="utf-8")
        tmp.replace(settings.RUN_FILE)
    except OSError:
        pass


def _interrupted():
    """The run the last backend was doing when it stopped (restart, crash), or None."""
    state = _read_run()
    if not state or state.get("status") not in ("running", "paused"):
        return None
    return {"started_at": state.get("started_at"), "done": len(state.get("done") or []),
            "total": state.get("total"), "tables": state.get("tables"), "pairs": state.get("pairs"),
            "table": state.get("table"), "updated_at": state.get("updated_at")}


job["interrupted"] = _interrupted()


# ---- progress ------------------------------------------------------------------------------------

def _progress():
    """How far the run is, weighted by rows, and about how long it still takes: measured
    speed (rows a second over the full reads made so far), the reads the current table still
    needs, and one read for every table still to come (the least they can take)."""
    total = job["rows_total"] or 0
    if not job["running"] or not total:
        return None
    sp = job["speed"]
    speed = sp["rows"] / sp["seconds"] if sp["seconds"] > 0 and sp["rows"] >= 200_000 else None
    cur_rows = (job["table_rows"] or 0) + OVERHEAD_ROWS if job["current"] else 0
    p = job["read"]
    pass_view, table_frac, eta = None, 0.0, None
    if p:
        elapsed = time.time() - p["started_at"]
        estimate = p["rows"] / speed if speed else None
        pass_frac = min(0.95, elapsed / estimate) if estimate else 0.0
        table_frac = min(0.99, ((p["n"] - 1) + pass_frac) / max(1, p["of"]))
        pass_view = {"n": p["n"], "of": p["of"], "label": p["label"], "elapsed": round(elapsed),
                     "estimate": round(estimate) if estimate else None}
        if speed:
            this_pass = max(0.0, estimate - elapsed)
            later_passes = (p["of"] - p["n"]) * p["rows"] / speed
            later_tables = max(0, total - job["rows_done"] - cur_rows) / speed
            eta = round(this_pass + later_passes + later_tables)
    done = job["rows_done"] + cur_rows * table_frac
    return {"fraction": round(min(1.0, done / total), 4), "rows_done": int(done), "rows_total": total,
            "eta_seconds": eta, "eta_is_minimum": True, "speed_rows_per_second": round(speed) if speed else None,
            "pass": pass_view}


def status(since=None):
    """The run's state, its step in progress, progress and ETA, and - with `since` - the
    activity entries after that number (0: all that are kept)."""
    out = {k: v for k, v in job.items() if k not in ("speed", "read")}
    out["results"] = dict(job["results"])
    out["progress"] = _progress()
    out["resumable"] = resumable()
    out["step"], out["step_started_at"], out["log_seq"] = log.step, log.step_started_at, log.last
    if since is not None:
        out["log"] = log.since(since)
    return out


# ---- waiting: for a lost connection, for the other run ---------------------------------------

class _Stop(Exception):
    """The run cannot go on (cancelled, or a failure waiting cannot fix)."""


def _db_of(p, server):
    return p.source_db if server.key == "source" else p.target_db


def _retry(ctx, attempt, server, database, message):
    """After a lost connection: wait ATNM_RETRY_WAITS[attempt], or - when the retries are used
    up - pause until the server answers again. False when the run was stopped meanwhile."""
    waits = settings.RETRY_WAITS
    if attempt < len(waits):
        w = waits[attempt]
        log.add(f"{message} Trying again in {w} s (retry {attempt + 1} of {len(waits)}).", "warn")
        job["waiting"] = {"reason": message, "paused": False, "until": time.time() + w, "server": server.label}
        stopped = ctx.wait(w)
        job["waiting"] = None
        return not stopped
    hint = "Is the VPN connected?" if server.key == "source" else "Is the network connection up?"
    log.add(f"Paused: {message} {hint} Trying every {settings.PAUSE_POLL} s; the run goes on by itself once "
            f"{server.label} answers. Press Stop to end it.", "error", step=True)
    job["waiting"] = {"reason": message, "hint": hint, "paused": True, "since": time.time(), "server": server.label}
    _mark_run("paused")
    try:
        while not ctx.wait(settings.PAUSE_POLL):
            if conn.reachable(server, database):
                log.add(f"{server.label} answers again: going on.", "ok", step=True)
                _mark_run("running")
                return True
        return False
    finally:
        job["waiting"] = None


def _catalog(ctx, p, server):
    """A fresh table list; a lost connection is waited for, a login or setting problem stops."""
    database = _db_of(p, server)
    attempt = 0
    while True:
        log.add(f"Reading the table list of {database} on {server.label}.", database=database, step=True)
        started = time.time()
        try:
            cat = catalog.read(server, database, refresh=True)
        except Exception as exc:
            message, hint = conn.explain(exc, server, database)
            text = f"{server.label} ({database}): {message}"
            if conn.kind(exc) != "connection":
                raise _Stop(text + (f" {hint}" if hint else ""))
            if not _retry(ctx, attempt, server, database, text):
                raise _Stop("Stopped.")
            # After a pause the server answered again: a later drop gets its retries anew.
            attempt = attempt + 1 if attempt < len(settings.RETRY_WAITS) else 0
            continue
        log.add(f"{len(cat['tables'])} tables listed in {database} on {server.label} ({time.time() - started:.1f} s).",
                database=database)
        return cat


def _rds_run_going():
    """Is the RDS -> AlTasnimBI section running its checks? (read only: nothing is changed)"""
    try:
        from .. import main as app_main
        return bool(getattr(app_main, "_checks").job.get("running"))
    except Exception:
        return False


def _wait_for_rds_run(ctx):
    if not _rds_run_going():
        return True
    log.add("Waiting for the RDS → AlTasnimBI check to finish: both read the RDS server, so one heavy run goes "
            "at a time.", "warn", step=True)
    job["waiting"] = {"reason": "The RDS → AlTasnimBI check is running.", "paused": False, "server": "RDS"}
    try:
        while _rds_run_going():
            if ctx.wait(15):
                return False
    finally:
        job["waiting"] = None
    log.add("The RDS → AlTasnimBI check has finished: going on.", "ok")
    return True


# ---- the run -----------------------------------------------------------------------------------

_run_state = {}


def _mark_run(status_word):
    if _run_state:
        _run_state.update(status=status_word, updated_at=_now())
        _write_run(_run_state)


def start(pairs, table=None, only=None, tables="all", skip=None, resumed_from=None):
    """Check every table present on both sides of each pair (or just `table`, or only the
    tables in `only` - {pair id: set of lower-case schema.table}), in the background, leaving
    out `skip` ({(pair id, key)}: already done by the run being resumed).
    False when a run is already going."""
    global _ctx
    what = "one table" if table else {"required": "the required tables", "all": "all tables"}.get(tables, tables)
    with _job_lock:
        if job["running"]:
            return False
        log.clear()
        _ctx = datacheck.Context()
        job.update(running=True, cancelling=False, done=0, total=0, current=None, pair=None, started_at=_now(),
                   finished_at=None, error=None, database=None, table_started_at=None, table_rows=None,
                   results={}, waiting=None, rows_total=0, rows_done=0, read=None,
                   speed={"rows": 0, "seconds": 0.0}, resumed_from=resumed_from, interrupted=None,
                   scope={"pairs": [p.id for p in pairs], "table": table, "tables": "one" if table else tables})
        ctx = _ctx
    _run_state.clear()
    _run_state.update(started_at=job["started_at"], pairs=[p.id for p in pairs], table=table, tables=tables,
                      only={k: sorted(v) for k, v in only.items()} if only is not None else None,
                      done=[list(x) for x in sorted(skip or [])], total=None, status="running",
                      resumed_from=resumed_from, updated_at=_now())
    _write_run(_run_state)
    if resumed_from:
        log.add(f"Resuming the run started at {resumed_from}: {len(skip or [])} tables were already checked and are "
                "left out.", step=True)
    log.add(f"Run started: {what} of {', '.join(p.source_db for p in pairs)} "
            f"({settings.SOURCE.label} → {settings.TARGET.label}).", step=True)
    if options.error():
        log.add(f"The options file cannot be read, so no cutoff is used: {options.error()}", "warn")

    def note(text, level, step):
        log.add(text, level, table=job["current"], database=job["database"], step=step)

    def on_pass(event, **facts):
        if event == "start":
            job["read"] = {"n": facts["n"], "of": facts["of"], "label": facts["label"], "rows": facts["rows"],
                           "started_at": time.time()}
        elif facts["rows"] >= 50_000:     # small tables are all overhead: they would spoil the speed
            job["speed"]["rows"] += facts["rows"]
            job["speed"]["seconds"] += facts["seconds"]

    ctx.on_note, ctx.on_pass = note, on_pass

    def check(p, s, t, infos):
        """One table, waiting out a lost connection. Raises _Stop when the run must end."""
        attempt = 0
        while True:
            result = datacheck.check_table(p, s, t, *infos[p.id], ctx=ctx, cutoff=options.cutoff(p, s["key"]))
            kind = result.get("error_kind")
            if kind in ("login", "config"):
                store.put(result)
                raise _Stop(result["headline"])
            if kind != "connection":
                return result
            server = settings.SERVERS.get(result.get("error_server"), settings.SOURCE)
            if not _retry(ctx, attempt, server, _db_of(p, server), result["headline"]):
                raise _Stop("Stopped.")
            attempt = attempt + 1 if attempt < len(settings.RETRY_WAITS) else 0

    def work():
        run_started = time.time()
        try:
            if not _wait_for_rds_run(ctx):
                raise _Stop("Stopped.")
            tasks, infos = [], {}
            for p in pairs:
                src = _catalog(ctx, p, settings.SOURCE)
                tgt = _catalog(ctx, p, settings.TARGET)
                infos[p.id] = (src["server"], tgt["server"])
                keys = [k for k in sorted(src["tables"]) if k in tgt["tables"]]
                if table:
                    keys = [k for k in keys if k == table.lower()]
                if only is not None:
                    wanted = only.get(p.id, set())
                    absent = sorted(k for k in wanted if k not in keys)
                    if absent:
                        log.add(f"{len(absent)} required tables are not in both databases, so their values cannot be "
                                f"compared: {', '.join(absent[:10])}{' …' if len(absent) > 10 else ''}.", "warn",
                                database=p.source_db)
                    keys = [k for k in keys if k in wanted]
                # Smallest tables first: most results arrive early, the long ones come last.
                keys.sort(key=lambda k: max(src["tables"][k]["rows"], tgt["tables"][k]["rows"]))
                tasks += [(p, src["tables"][k], tgt["tables"][k]) for k in keys]
            everything = len(tasks)
            if skip:
                tasks = [x for x in tasks if (x[0].id, x[1]["key"]) not in skip]
            job["total"] = len(tasks)
            job["rows_total"] = sum(max(s["rows"], t["rows"]) + OVERHEAD_ROWS for _, s, t in tasks)
            _run_state["total"] = everything
            _write_run(_run_state)
            log.add(f"{len(tasks)} table{'s' if len(tasks) != 1 else ''} to check, about "
                    f"{_fmt(sum(max(s['rows'], t['rows']) for _, s, t in tasks))} rows in all; smallest first.")

            queue, relocked, n = deque(tasks), set(), 0
            while queue:
                if ctx.cancelled:
                    break
                if not _wait_for_rds_run(ctx):
                    break
                p, s, t = queue.popleft()
                n += 1
                weight = max(s["rows"], t["rows"]) + OVERHEAD_ROWS
                job.update(current=f"{s['schema']}.{s['table']}", pair=p.id, database=p.source_db,
                           table_started_at=_now(), table_rows=max(s["rows"], t["rows"]), read=None)
                log.add(f"Table {min(n, len(tasks))} of {len(tasks)}: about {_fmt(s['rows'])} rows in "
                        f"{settings.SOURCE.label}, {_fmt(t['rows'])} in {settings.TARGET.label}, "
                        f"{len(s['columns'])} columns.", table=job["current"], database=p.source_db, step=True)
                try:
                    result = check(p, s, t, infos)
                except datacheck.Stopped:
                    log.add("Stopped while checking this table; its result is not saved.", "warn",
                            table=job["current"], database=p.source_db)
                    break
                store.put(result)
                if result["status"] == "locked" and (p.id, s["key"]) not in relocked:
                    relocked.add((p.id, s["key"]))
                    queue.append((p, s, t))
                    n -= 1
                    log.add("The table is locked by another session: it is tried once more at the end.", "warn",
                            table=job["current"], database=p.source_db)
                    continue
                job["done"] += 1
                job["rows_done"] += weight
                job["results"][result["status"]] = job["results"].get(result["status"], 0) + 1
                _run_state["done"].append([p.id, s["key"]])
                _mark_run("running")
                kept = " The earlier result of this table is kept." if result["status"] in FAILED else ""
                log.add(f"{RESULT_WORDS.get(result['status'], result['status'])} in {result['seconds']} s: "
                        f"{result.get('headline', '')}{kept}", RESULT_LEVEL.get(result["status"], "info"),
                        table=job["current"], database=p.source_db)
        except _Stop as exc:
            if not ctx.cancelled:
                job["error"] = str(exc)
                log.add(f"The run stopped: {exc}", "error")
        except Exception as exc:   # anything unexpected: say so, keep what was saved
            job["error"] = str(exc)
            log.add(f"The run stopped: {exc}", "error")
        finally:
            took = datacheck._took(time.time() - run_started)
            counts = ", ".join(f"{v} {RESULT_WORDS.get(k, k).lower()}" for k, v in job["results"].items())
            summary = f"{job['done']} of {job['total']} tables checked{' (' + counts + ')' if counts else ''}"
            if ctx.cancelled:
                log.add(f"Run stopped by the user after {took}: {summary}.", "warn", step=True)
                _mark_run("stopped")
            elif job["error"]:
                log.add(f"Run ended after {took}: {summary}. It can be resumed once the problem is fixed.",
                        "error", step=True)
                _mark_run("failed")
            else:
                log.add(f"Run finished in {took}: {summary}.", "ok", step=True)
                _mark_run("finished")
            job.update(running=False, cancelling=False, current=None, database=None, table_started_at=None,
                       table_rows=None, finished_at=_now(), waiting=None, read=None)

    threading.Thread(target=work, daemon=True, name="atnm-check").start()
    return True


def resume():
    """Start the last unfinished run again (after a restart, a failure or a stop), leaving
    out the tables it had finished. None when there is nothing to resume, False when a run
    is going."""
    state = _read_run()
    if not state or state.get("status") == "finished":
        return None
    pairs = [p for p in (settings.pair(i) for i in state.get("pairs") or []) if p]
    if not pairs:
        return None
    only = {k: set(v) for k, v in state["only"].items()} if state.get("only") is not None else None
    skip = {tuple(x) for x in state.get("done") or []}
    return start(pairs, state.get("table"), only=only, tables=state.get("tables") or "all", skip=skip,
                 resumed_from=state.get("resumed_from") or state.get("started_at"))


def resumable():
    """The last run, when it did not finish and can be resumed, else None."""
    state = _read_run()
    if not state or state.get("status") == "finished" or job["running"]:
        return None
    return {"started_at": state.get("started_at"), "status": state.get("status"),
            "done": len(state.get("done") or []), "total": state.get("total"), "tables": state.get("tables"),
            "table": state.get("table"), "updated_at": state.get("updated_at")}


def cancel():
    with _job_lock:
        if not job["running"] or _ctx is None:
            return False
        job["cancelling"] = True
        log.add("Stop requested: cancelling the queries that are running on both servers.", "warn", step=True)
        _ctx.cancel()
        return True

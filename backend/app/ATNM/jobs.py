"""Saved data check results, and the background run that checks many tables in a row.

Results are kept per pair and table in ATNM_RESULT_FILE (a JSON file), written after every
table, so a restart or a cancelled run keeps everything checked so far. One run at a time.
"""
import json
import threading
import time
from datetime import datetime, timezone

from . import activity, catalog, conn, datacheck, settings


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        with self.lock:
            self.results[self._id(result["pair"], result["key"])] = result
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
       "database": None, "table_started_at": None, "table_rows": None, "results": {}}
_ctx = None
log = activity.Activity()

RESULT_WORDS = {"identical": "Identical", "different": "Different", "error": "Failed", "locked": "Locked",
                "skipped": "Skipped"}
RESULT_LEVEL = {"identical": "ok", "different": "warn", "error": "error", "locked": "warn", "skipped": "warn"}


def status(since=None):
    """The run's state, its step in progress, and - with `since` - the activity entries
    after that number (0: all that are kept)."""
    out = dict(job)
    out["results"] = dict(job["results"])
    out["step"], out["step_started_at"], out["log_seq"] = log.step, log.step_started_at, log.last
    if since is not None:
        out["log"] = log.since(since)
    return out


def _catalog(server, database):
    """A fresh table list, or an error in plain words naming the server."""
    log.add(f"Reading the table list of {database} on {server.label}.", database=database, step=True)
    started = time.time()
    try:
        cat = catalog.read(server, database, refresh=True)
    except Exception as exc:
        message, hint = conn.explain(exc, server, database)
        raise RuntimeError(f"{server.label} ({database}): {message}" + (f" {hint}" if hint else "")) from exc
    log.add(f"{len(cat['tables'])} tables listed in {database} on {server.label} ({time.time() - started:.1f} s).",
            database=database)
    return cat


def _fmt(n):
    return f"{n:,}"


def start(pairs, table=None, only=None, tables="all"):
    """Check every table present on both sides of each pair (or just `table`, or only the
    tables in `only` - {pair id: set of lower-case schema.table}), in the background.
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
                   results={}, scope={"pairs": [p.id for p in pairs], "table": table, "tables": "one" if table else tables})
        ctx = _ctx
    log.add(f"Run started: {what} of {', '.join(p.source_db for p in pairs)} "
            f"({settings.SOURCE.label} → {settings.TARGET.label}).", step=True)

    def note(text, level, step):
        log.add(text, level, table=job["current"], database=job["database"], step=step)

    ctx.on_note = note

    def work():
        run_started = time.time()
        try:
            tasks = []
            infos = {}
            for p in pairs:
                src = _catalog(settings.SOURCE, p.source_db)
                tgt = _catalog(settings.TARGET, p.target_db)
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
            job["total"] = len(tasks)
            rows = sum(max(s["rows"], t["rows"]) for _, s, t in tasks)
            log.add(f"{len(tasks)} table{'s' if len(tasks) != 1 else ''} to check, about {_fmt(rows)} rows in all; smallest first.")
            for n, (p, s, t) in enumerate(tasks, 1):
                if ctx.cancelled:
                    break
                job.update(current=f"{s['schema']}.{s['table']}", pair=p.id, database=p.source_db,
                           table_started_at=_now(), table_rows=max(s["rows"], t["rows"]))
                log.add(f"Table {n} of {len(tasks)}: about {_fmt(s['rows'])} rows in {settings.SOURCE.label}, "
                        f"{_fmt(t['rows'])} in {settings.TARGET.label}, {len(s['columns'])} columns.",
                        table=job["current"], database=p.source_db, step=True)
                try:
                    result = datacheck.check_table(p, s, t, *infos[p.id], ctx=ctx)
                except datacheck.Stopped:
                    log.add("Stopped while checking this table; its result is not saved.", "warn",
                            table=job["current"], database=p.source_db)
                    break
                store.put(result)
                job["done"] += 1
                job["results"][result["status"]] = job["results"].get(result["status"], 0) + 1
                log.add(f"{RESULT_WORDS.get(result['status'], result['status'])} in {result['seconds']} s: "
                        f"{result.get('headline', '')}", RESULT_LEVEL.get(result["status"], "info"),
                        table=job["current"], database=p.source_db)
        except Exception as exc:   # a catalog read failed (VPN down, login ...): say so, keep what was saved
            job["error"] = str(exc)
            log.add(f"The run stopped: {exc}", "error")
        finally:
            took = datacheck._took(time.time() - run_started)
            counts = ", ".join(f"{v} {RESULT_WORDS.get(k, k).lower()}" for k, v in job["results"].items())
            if ctx.cancelled:
                log.add(f"Run stopped by the user after {took}: {job['done']} of {job['total']} tables checked"
                        f"{' (' + counts + ')' if counts else ''}.", "warn", step=True)
            elif not job["error"]:
                log.add(f"Run finished in {took}: {job['done']} of {job['total']} tables checked"
                        f"{' (' + counts + ')' if counts else ''}.", "ok", step=True)
            job.update(running=False, cancelling=False, current=None, database=None, table_started_at=None,
                       table_rows=None, finished_at=_now())

    threading.Thread(target=work, daemon=True, name="atnm-check").start()
    return True


def cancel():
    with _job_lock:
        if not job["running"] or _ctx is None:
            return False
        job["cancelling"] = True
        log.add("Stop requested: cancelling the queries that are running on both servers.", "warn", step=True)
        _ctx.cancel()
        return True

"""HTTP API of the ATNM section: is each ATNM database copied to RDS completely?

    GET  /api/atnm/health          can both servers and every database be reached?
    GET  /api/atnm/overview        every table of every pair: exists, columns, rows, data check
    GET  /api/atnm/table           one table: its columns side by side and its last data check
    POST /api/atnm/check           run the data check in the background (all, one pair, one table)
    GET  /api/atnm/check/status    progress of that run, and what it is doing (?since=n: its activity log)
    POST /api/atnm/check/resume    start the last unfinished run again, skipping what it finished
    POST /api/atnm/check/cancel    stop it
    GET  /api/atnm/export.csv      the overview as a spreadsheet
"""
import csv
import io
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from . import catalog, conn, jobs, options, required, settings, structure

router = APIRouter(prefix="/api/atnm", tags=["ATNM"])


def _pair(pair_id):
    p = settings.pair(pair_id)
    if p is None:
        raise HTTPException(status_code=404, detail=f"No database pair {pair_id!r} (ATNM_DB_<n>_SOURCE / _TARGET).")
    return p


def _error(exc, server, database):
    message, hint = conn.explain(exc, server, database)
    return {"server": server.key, "label": server.label, "database": database, "message": message, "hint": hint}


def _read_all(pairs, refresh):
    """Both catalogs of every pair, read in parallel: {(pair id, side): catalog or error}."""
    work = [(p, side, server, db) for p in pairs
            for side, server, db in (("source", settings.SOURCE, p.source_db), ("target", settings.TARGET, p.target_db))]

    def one(item):
        p, side, server, db = item
        try:
            return (p.id, side), catalog.read(server, db, refresh=refresh)
        except Exception as exc:
            return (p.id, side), {"error": _error(exc, server, db)}

    if not work:
        return {}
    with ThreadPoolExecutor(max_workers=min(4, len(work))) as pool:
        return dict(pool.map(one, work))


def _live(p, key):
    """How the row counts of a table moved lately on each server (None: not moving)."""
    return {"source": catalog.movement(settings.SOURCE, p.source_db, key),
            "target": catalog.movement(settings.TARGET, p.target_db, key)}


def _pair_view(p, src, tgt):
    base = {**p.public(), "source": None, "target": None, "errors": [], "tables": [], "summary": None}
    for side, cat in (("source", src), ("target", tgt)):
        if "error" in cat:
            base["errors"].append(cat["error"])
        else:
            base[side] = {"database": cat["database"], "server": cat["server"], "read_at": cat["read_at"],
                          "tables": len(cat["tables"])}
    if base["errors"]:
        return base
    tables = structure.pair_tables(src, tgt, lambda key: jobs.store.get(p.id, key),
                                   cutoff_of=lambda key: options.cutoff(p, key), live_of=lambda key: _live(p, key))
    # The tables the migration uses (the plan's source tables); one it names that neither
    # database has is listed too, as a problem.
    try:
        req, req_error = required.tables(p), None
    except Exception as exc:
        req, req_error = {}, f"The required tables cannot be read from the migration plan: {exc}"
    seen = {t["key"] for t in tables}
    tables += [structure.absent_view(k, r["schema"], r["table"]) for k, r in sorted(req.items()) if k not in seen]
    for t in tables:
        t["required"] = t["key"] in req
        t["mapping"] = req[t["key"]]["mapping"] if t["key"] in req else None
    needed = [t for t in tables if t["required"]]
    base["tables"] = tables
    base["summary"] = structure.summarize([t for t in tables if t["in_source"] or t["in_target"]])
    base["required"] = {"count": len(req), "summary": structure.summarize(needed), "error": req_error}
    notes = []
    s, t = src["server"], tgt["server"]
    if (s["collation"] or "").lower() != (t["collation"] or "").lower():
        notes.append(f"The databases use different collations ({s['collation']} in {settings.SOURCE.label}, "
                     f"{t['collation']} in {settings.TARGET.label}); text is compared exactly as stored.")
    if min(s["major"] or 0, t["major"] or 0) < 13:
        notes.append("One server is older than SQL Server 2016: long text values are compared on their first "
                     "4,000 characters.")
    base["notes"] = notes
    return base


@router.get("/health")
def health():
    """Can each server be reached, and each database opened?"""
    checks = [(server, db) for server, attr in ((settings.SOURCE, "source_db"), (settings.TARGET, "target_db"))
              for db in dict.fromkeys(getattr(p, attr) for p in settings.PAIRS)]

    def one(item):
        server, db = item
        started = time.time()
        try:
            with closing(conn.connect(server, db, timeout=30)) as con:
                info = conn.fetch(con.cursor(), catalog.INFO_SQL)[0]
            return {"server": server.key, "database": db, "ok": True, "error": None,
                    "version": info["version"], "seconds": round(time.time() - started, 2)}
        except Exception as exc:
            return {"server": server.key, "database": db, "ok": False, "error": _error(exc, server, db),
                    "seconds": round(time.time() - started, 2)}

    with ThreadPoolExecutor(max_workers=max(1, min(4, len(checks)))) as pool:
        results = list(pool.map(one, checks)) if checks else []
    servers = []
    for server in (settings.SOURCE, settings.TARGET):
        mine = [r for r in results if r["server"] == server.key]
        servers.append({**server.public(), "ok": bool(mine) and all(r["ok"] for r in mine), "databases": mine})
    return {"ok": all(s["ok"] for s in servers), "servers": servers, "pairs": [p.public() for p in settings.PAIRS]}


@router.get("/overview")
def overview(refresh: bool = False):
    """Every table of every pair, with its status and the reason for it."""
    cats = _read_all(settings.PAIRS, refresh)
    pairs = [_pair_view(p, cats[(p.id, "source")], cats[(p.id, "target")]) for p in settings.PAIRS]
    return {
        "source": settings.SOURCE.public(),
        "target": settings.TARGET.public(),
        "pairs": pairs,
        "job": jobs.status(),
        "settings": {"diff_rows_max": settings.DIFF_ROWS_MAX, "data_check_max_rows": settings.DATA_CHECK_MAX_ROWS},
        "options_error": options.error(),
    }


@router.get("/table")
def table(pair: str = Query(..., description="Pair id, e.g. 1"),
          table: str = Query(..., description="schema.table, e.g. dbo.Company")):
    """One table: its columns on both sides, its row key and its last data check."""
    p = _pair(pair)
    cats = _read_all([p], refresh=False)
    src, tgt = cats[(p.id, "source")], cats[(p.id, "target")]
    for cat in (src, tgt):
        if "error" in cat:
            raise HTTPException(status_code=502, detail=f"{cat['error']['label']}: {cat['error']['message']}")
    key = table.lower()
    s, t = src["tables"].get(key), tgt["tables"].get(key)
    if s is None and t is None:
        raise HTTPException(status_code=404, detail=f"No table {table!r} in {p.source_db} or {p.target_db}.")
    saved = jobs.store.get(p.id, key)
    view = structure.table_view(key, s, t, saved, options.cutoff(p, key), _live(p, key))
    columns = structure.compare_columns(s["columns"] if s else [], t["columns"] if t else [])
    if view.get("data") and not view["data"]["stale"]:
        columns = structure.apply_renames(columns, saved)
    return {"pair": p.public(), "table": view, "columns": columns, "data": saved,
            "job": jobs.status()}


class CheckRequest(BaseModel):
    pair: str | None = None
    table: str | None = None
    # "required": only the tables the migration uses (the plan's source tables); "all": every table
    tables: str = "all"


@router.post("/check")
def check(req: CheckRequest):
    """Start the data check: every pair or one pair - its required tables or all of them -
    or one table of a pair."""
    if req.table and not req.pair:
        raise HTTPException(status_code=400, detail="A table needs its pair.")
    if req.tables not in ("all", "required"):
        raise HTTPException(status_code=400, detail="tables must be 'all' or 'required'.")
    pairs = [_pair(req.pair)] if req.pair else list(settings.PAIRS)
    if not pairs:
        raise HTTPException(status_code=409, detail="No database pairs are set up (ATNM_DB_<n>_SOURCE / _TARGET).")
    only = None
    if req.tables == "required" and not req.table:
        try:
            only = {p.id: set(required.tables(p)) for p in pairs}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"The required tables cannot be read from the migration plan: {exc}")
    started = jobs.start(pairs, req.table, only=only, tables=req.tables)
    if not started:
        raise HTTPException(status_code=409, detail="A data check is already running.")
    return jobs.status()


@router.get("/check/status")
def check_status(since: int | None = Query(None, ge=0, description="Activity entries after this number; 0 = all kept")):
    """Progress of the run, the step in progress, and (with `since`) its activity log."""
    return jobs.status(since)


@router.post("/check/resume")
def check_resume():
    """Start the last unfinished run again, leaving out the tables it had finished."""
    started = jobs.resume()
    if started is None:
        raise HTTPException(status_code=409, detail="There is no unfinished run to resume.")
    if not started:
        raise HTTPException(status_code=409, detail="A data check is already running.")
    return jobs.status()


@router.post("/check/cancel")
def check_cancel():
    return {"cancelled": jobs.cancel(), "job": jobs.status()}


@router.get("/export.csv")
def export(pair: str | None = None, tables: str = Query("all", pattern="^(all|required)$")):
    """Every table of every pair (or of one), one row each, as CSV; `tables=required` keeps
    only the tables the migration uses."""
    pairs = [_pair(pair)] if pair else list(settings.PAIRS)
    cats = _read_all(pairs, refresh=False)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([f"{settings.SOURCE.label} database", f"{settings.TARGET.label} database", "schema", "table",
                "required", "mapping", "status",
                "reason", f"in {settings.SOURCE.label}", f"in {settings.TARGET.label}",
                f"rows {settings.SOURCE.label}", f"rows {settings.TARGET.label}", "rows exact",
                f"columns {settings.SOURCE.label}", f"columns {settings.TARGET.label}",
                f"columns missing in {settings.TARGET.label}", f"columns only in {settings.TARGET.label}",
                "columns changed", "data check", "data checked at"])
    for p in pairs:
        view = _pair_view(p, cats[(p.id, "source")], cats[(p.id, "target")])
        for e in view["errors"]:
            w.writerow([p.source_db, p.target_db, "", "", "error", f"{e['label']}: {e['message']}"])
        for t in view["tables"]:
            if tables == "required" and not t.get("required"):
                continue
            c = t["columns"] or {}
            d = t["data"] or {}
            w.writerow([p.source_db, p.target_db, t["schema"], t["table"], "yes" if t.get("required") else "no",
                        t.get("mapping") or "", t["status"], t["reason"],
                        "yes" if t["in_source"] else "no", "yes" if t["in_target"] else "no",
                        t["rows"]["source"], t["rows"]["target"], "yes" if t["rows"]["exact"] else "no",
                        c.get("source"), c.get("target"), "; ".join(c.get("missing", [])),
                        "; ".join(c.get("extra", [])),
                        "; ".join(f"{x['name']} ({', '.join(x['notes'])})" for x in c.get("changed", [])),
                        ("out of date" if d.get("stale") else d.get("status")) or "not checked",
                        d.get("checked_at") or ""])
    name = f"atnm_copy_check_{tables}{'_' + pair if pair else ''}.csv"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})

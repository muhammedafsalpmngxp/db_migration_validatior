"""HTTP API for the migration validator.

Run from backend/:  uvicorn app.main:app --reload --port 8000
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

import pyodbc
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import compare, config, db
from . import mapping as plan_mod

logger = logging.getLogger("migration_validator")

app = FastAPI(title="DB Migration Validator", version="0.2.0")

_plan_cache = {"mtime": None, "plan": None}


def get_plan():
    """The mapping file, re-read whenever it changes on disk."""
    mtime = config.MAPPING_FILE.stat().st_mtime
    if _plan_cache["mtime"] != mtime:
        _plan_cache["plan"] = plan_mod.load()
        _plan_cache["mtime"] = mtime
    return _plan_cache["plan"]


@app.exception_handler(pyodbc.Error)
def _db_error(_request, exc):
    logger.error("database error: %s", exc)
    return JSONResponse(status_code=502, content={"detail": f"Database error: {exc}"})


@app.exception_handler(plan_mod.PlanError)
def _plan_error(_request, exc):
    return JSONResponse(status_code=500, content={"detail": f"Mapping file error: {exc}"})


def _database(side):
    d = config.DATABASES[side]
    return {"side": side, "name": d["name"], "label": d["label"], "role": d["role"]}


def _live_tables(refresh=False):
    """Live facts for every table in the plan, per side, read in parallel.

    Only the plan's tables are looked up (by name), so a busy table elsewhere in a
    database cannot slow this down, and a busy table in the plan comes back locked.
    """
    plan = get_plan()
    names = {side: set() for side in config.DATABASES}
    for m in plan.mappings:
        for member in m.sources + m.targets:
            names[member.ref.side].add((member.ref.schema, member.ref.table))
    sides = list(config.DATABASES)
    with ThreadPoolExecutor(max_workers=len(sides)) as pool:
        facts = pool.map(lambda s: db.describe_tables(s, names[s], refresh), sides)
    return dict(zip(sides, facts))


def _entry(live, ref):
    return live[ref.side].get((ref.schema.lower(), ref.table.lower()))


def _table_view(ref, entry, **extra):
    """One table as the UI shows it: identity from the plan, everything else from the DB."""
    return {
        "ref": f"{ref.side}.{entry['schema']}.{entry['table']}" if entry else ref.ref,
        "side": ref.side,
        "database": config.DATABASES[ref.side]["name"],
        "schema": entry["schema"] if entry else ref.schema,
        "table": entry["table"] if entry else ref.table,
        "exists": entry is not None,
        "locked": bool(entry and entry["locked"]),
        "rows": entry["rows"] if entry else None,
        "columns": entry["columns"] if entry else None,
        "size_kb": entry["size_kb"] if entry else None,
        "created": entry["created"] if entry else None,
        **extra,
    }


def _row_check(m, live):
    rows, locked = {}, []
    for member in m.sources + m.targets:
        e = _entry(live, member.ref)
        rows[member.ref.key] = e["rows"] if e else None
        if e and e["locked"]:
            locked.append(member.ref.table)
    check = compare.row_check(m, rows)
    if locked and check["status"] not in ("excluded", "info"):
        check = {**check, "status": "locked",
                 "rule": f"{', '.join(locked)} is locked by another session (a load in progress?); "
                         "its row count cannot be read until it is released."}
    return check


@app.get("/api/health")
def health():
    """Can each of the three databases be reached?"""
    def check(side):
        try:
            db.ping(side)
            return {**_database(side), "ok": True, "error": None}
        except pyodbc.Error as exc:
            return {**_database(side), "ok": False, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(check, config.DATABASES))
    return {"ok": all(r["ok"] for r in results), "databases": results}


@app.get("/api/scope")
def scope(refresh: bool = False):
    """The migration scope: every source table in the plan, in plan order, with live
    stats, its targets, and whether the mapping's row count rule currently holds."""
    plan = get_plan()
    live = _live_tables(refresh)
    checks = {m.id: _row_check(m, live) for m in plan.mappings}

    groups = {side: [] for side in config.SOURCE_SIDES}
    for m, src in plan.scope():
        groups[src.ref.side].append(_table_view(
            src.ref, _entry(live, src.ref),
            role=src.role,
            mapping={"id": m.id, "type": m.type, "sources": len(m.sources)},
            targets=[_table_view(t.ref, _entry(live, t.ref)) for t in m.targets],
            check=checks[m.id],
        ))

    statuses = {}
    for c in checks.values():
        statuses[c["status"]] = statuses.get(c["status"], 0) + 1
    targets = {t.ref.key for m in plan.mappings for t in m.targets}
    members = [t for m in plan.mappings for t in m.sources + m.targets]
    missing = sum(1 for t in members if _entry(live, t.ref) is None)
    locked = sorted({t.ref.ref for t in members if (_entry(live, t.ref) or {}).get("locked")})
    return {
        "databases": [_database(s) for s in config.DATABASES],
        "read_at": min(filter(None, (db.read_at(s) for s in config.DATABASES)), default=time.time()),
        "groups": [{**_database(side), "tables": groups[side]} for side in config.SOURCE_SIDES],
        "summary": {
            "source_tables": sum(len(g) for g in groups.values()),
            "target_tables": len(targets),
            "mappings": len(plan.mappings),
            "checks": statuses,
            "missing_tables": missing,
            "locked_tables": locked,
        },
    }


@app.get("/api/compare")
def compare_table(table: str = Query(..., description="Source table in the plan, e.g. A.dbo.Company")):
    """One in-scope source table: its mapping, live row counts, and column comparison."""
    try:
        ref = plan_mod.parse_ref(table)
    except plan_mod.PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    plan = get_plan()
    m = plan.for_source(ref)
    if m is None:
        raise HTTPException(status_code=404, detail=f"{ref.ref} is not in the migration scope.")

    live = _live_tables()
    selected_entry = _entry(live, ref)
    selected = _table_view(ref, selected_entry)

    def member(mem):
        return _table_view(mem.ref, _entry(live, mem.ref), role=mem.role,
                           selected=mem.ref.key == ref.key)

    result = {
        "selected": selected,
        "mapping": {
            "id": m.id, "type": m.type, "note": m.note,
            "sources": [member(s) for s in m.sources],
            "targets": [member(t) for t in m.targets],
        },
        "row_check": _row_check(m, live),
        "source_columns": [],
        "comparisons": [],
    }
    if not selected_entry or selected_entry["locked"]:
        return result

    def columns(entry):
        """Column list of a live table, or None when it is missing or locked."""
        if not entry or entry["locked"]:
            return None
        try:
            return db.table_columns(entry["side"], entry["object_id"])
        except db.TableLocked:
            return None

    # Columns of the selected source and of each target, fetched in parallel.
    targets = [(t, _entry(live, t.ref)) for t in m.targets]
    wanted = [{**selected_entry, "side": ref.side}] + [
        {**e, "side": t.ref.side} if e else None for t, e in targets
    ]
    with ThreadPoolExecutor(max_workers=min(4, len(wanted))) as pool:
        fetched = list(pool.map(columns, wanted))
    source_cols = fetched[0]
    if source_cols is None:
        return result
    result["source_columns"] = source_cols

    for (t, e), target_cols in zip(targets, fetched[1:]):
        name = f"T.{e['schema']}.{e['table']}" if e else t.ref.ref
        if target_cols is None:
            result["comparisons"].append({
                "target": name, "exists": e is not None, "locked": bool(e), "rows": [], "summary": None,
            })
            continue
        rows, summary = compare.align_columns(
            source_cols, target_cols, target_table=e["table"], declared=m.columns
        )
        result["comparisons"].append({
            "target": name, "exists": True, "locked": False, "rows": rows, "summary": summary,
        })
    return result


@app.get("/api/row-count")
def row_count(table: str = Query(..., description="Any table in the plan, e.g. T.ref.crew")):
    """Exact COUNT_BIG(*) of one table in the plan. Slow on big tables; asked for on demand."""
    try:
        ref = plan_mod.parse_ref(table)
    except plan_mod.PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not get_plan().contains(ref):
        raise HTTPException(status_code=404, detail=f"{ref.ref} is not in the migration plan.")
    entry = _entry(_live_tables(), ref)
    if not entry:
        raise HTTPException(status_code=404, detail=f"{ref.ref} does not exist.")
    locked = HTTPException(
        status_code=409,
        detail=f"{ref.ref} is locked by another session (a load in progress?); try again later.",
    )
    if entry["locked"]:
        raise locked
    try:
        rows, seconds = db.exact_count(ref.side, entry["schema"], entry["table"])
    except db.TableLocked:
        raise locked
    except pyodbc.Error as exc:
        if "HYT00" in str(exc) or "timeout" in str(exc).lower():
            raise HTTPException(
                status_code=504,
                detail=f"COUNT(*) did not finish within {config.MSSQL_COUNT_TIMEOUT}s.",
            )
        raise
    return {"ref": table, "rows": rows, "seconds": seconds, "metadata_rows": entry["rows"]}


def _plan_table(table):
    """A table reference from the query string, required to be in the plan and to exist."""
    try:
        ref = plan_mod.parse_ref(table)
    except plan_mod.PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not get_plan().contains(ref):
        raise HTTPException(status_code=404, detail=f"{ref.ref} is not in the migration plan.")
    entry = _entry(_live_tables(), ref)
    if not entry:
        raise HTTPException(status_code=404, detail=f"{ref.ref} does not exist.")
    return ref, entry


@app.get("/api/keys")
def table_keys(table: str = Query(..., description="A table in the plan, e.g. T.dbo.activity_codes_norms")):
    """Primary/unique keys of one table, the foreign keys it holds, and the ones pointing
    at it - with live row counts of every linked table."""
    ref, entry = _plan_table(table)
    if entry["locked"]:
        return {"ref": ref.ref, "locked": True, "keys": [], "foreign_keys": []}
    try:
        keys = db.table_keys(ref.side, entry["object_id"])
    except db.TableLocked:
        return {"ref": ref.ref, "locked": True, "keys": [], "foreign_keys": []}

    plan = get_plan()
    for fk in keys["foreign_keys"]:
        for end in ("parent", "referenced"):
            t = fk[end]
            t["ref"] = f"{ref.side}.{t['schema']}.{t['table']}"
            t["in_plan"] = plan.contains(plan_mod.parse_ref(t["ref"]))
    return {"ref": f"{ref.side}.{entry['schema']}.{entry['table']}", "locked": False, **keys}


@app.get("/api/fk-check")
def foreign_key_check(
    table: str = Query(..., description="The table holding the foreign key"),
    fk: str = Query(..., description="Foreign key constraint name"),
):
    """Exact counts for one foreign key: rows filled in, distinct values, orphans."""
    ref, entry = _plan_table(table)
    locked = HTTPException(status_code=409, detail="The table is locked by another session; try again later.")
    if entry["locked"]:
        raise locked
    try:
        found = next(
            (f for f in db.table_keys(ref.side, entry["object_id"])["foreign_keys"]
             if f["name"] == fk and f["direction"] == "outgoing"),
            None,
        )
        if not found:
            raise HTTPException(status_code=404, detail=f"{ref.ref} has no foreign key {fk!r}.")
        return {"table": ref.ref, "fk": fk, **db.foreign_key_check(ref.side, found)}
    except db.TableLocked:
        raise locked

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
            source_cols, target_cols, target_table=e["table"], declared=m.columns,
            fk_columns=db.fk_columns(t.ref.side, e["object_id"]),
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


# ---- Data check: value by value comparison ----------------------------------------------

from . import datacheck  # noqa: E402

_checks = datacheck.Store(config.DATA_CHECK_FILE)


def _run_data_check(m):
    live = _live_tables()
    return datacheck.check(m, lambda member: _entry(live, member.ref))


def _mapping_by_id(mapping_id):
    m = next((x for x in get_plan().mappings if x.id == mapping_id), None)
    if not m:
        raise HTTPException(status_code=404, detail=f"No mapping {mapping_id!r} in the plan.")
    return m


@app.get("/api/data-check")
def data_check(mapping: str = Query(..., description="Mapping id, e.g. crew_type"), refresh: bool = False):
    """The data check of one mapping: the saved result, or a new run when asked (or none yet).
    `run=false` style reads use /api/data-checks; this endpoint runs when there is no result."""
    m = _mapping_by_id(mapping)
    saved = _checks.get(m.id)
    if saved and not refresh:
        return saved
    result = _run_data_check(m)
    _checks.put(result)
    return result


@app.get("/api/data-check/saved")
def data_check_saved(mapping: str = Query(...)):
    """The saved result of one mapping without running anything (null when never checked)."""
    _mapping_by_id(mapping)
    return {"result": _checks.get(mapping)}


@app.get("/api/data-checks")
def data_checks():
    """Status of every mapping's last data check, and of the background run."""
    return {"results": _checks.summaries(), "job": dict(_checks.job)}


@app.post("/api/data-checks/run")
def data_checks_run():
    """Check every mapping in the background, one after another: the data check, then the
    key mapping check (each kept in its own store)."""
    mappings = [m for m in get_plan().mappings]
    started = _checks.run_all(mappings, _run_all_checks)
    return {"started": started, "job": dict(_checks.job)}


def _run_all_checks(m):
    result = _run_data_check(m)
    try:
        _keymaps.put(_run_key_mapping(m))
    except Exception as exc:  # the data check result is still saved
        logger.error("key mapping check of %s failed: %s", m.id, exc)
        _keymaps.put(_failed_key_mapping(m, exc))
    return result


# ---- Key mapping check: do the foreign keys point at the right rows? ----------------------

from datetime import datetime, timezone  # noqa: E402

from . import keymap  # noqa: E402

_keymaps = datacheck.Store(config.KEY_MAPPING_FILE)


def _run_key_mapping(m):
    live = _live_tables()
    return keymap.check(m, lambda member: _entry(live, member.ref), get_plan())


def _failed_key_mapping(m, exc):
    return {"mapping": m.id, "type": m.type, "status": "error", "headline": f"Check failed: {exc}", "links": [],
            "findings": [{"severity": "error", "text": f"Check failed: {exc}"}],
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


@app.get("/api/key-mapping")
def key_mapping(mapping: str = Query(..., description="Mapping id, e.g. activity_codes_norms"), refresh: bool = False):
    """The key mapping check of one mapping: for every foreign key of the target, the source
    column it was made from and whether each id points at the row holding the source value.
    Returns the saved result, or runs the check when asked (or when there is none yet)."""
    m = _mapping_by_id(mapping)
    saved = _keymaps.get(m.id)
    if saved and not refresh:
        return saved
    result = _run_key_mapping(m)
    _keymaps.put(result)
    return result


@app.get("/api/key-mapping/saved")
def key_mapping_saved(mapping: str = Query(...)):
    """The saved key mapping check of one mapping without running anything (null when none)."""
    _mapping_by_id(mapping)
    return {"result": _keymaps.get(mapping)}


@app.get("/api/key-mapping/rows")
def key_mapping_rows(
    mapping: str = Query(..., description="Mapping id"),
    column: str = Query(..., description="The target's foreign key column, e.g. crew_type_id"),
    filter: str = Query("problems", description="problems, all, or one bucket: correct, not_filled, wrong ..."),
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=keymap.MAX_PAGE_SIZE),
    reveal: bool = False,
):
    """The paired rows behind one link: source value, target id and the value that id points at."""
    m = _mapping_by_id(mapping)
    live = _live_tables()
    try:
        return keymap.rows(m, lambda member: _entry(live, member.ref), _keymaps.get(m.id), column,
                           flt=filter, page=page, size=size, reveal=reveal)
    except keymap.NotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except datacheck.Skipped as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ---- Values behind a data check ---------------------------------------------------------

from fastapi.responses import Response  # noqa: E402

from . import values as values_mod  # noqa: E402


@app.get("/api/data-check/values")
def data_check_values(
    mapping: str = Query(..., description="Mapping id, e.g. crew_type"),
    column: str = Query(..., description="Source (or target) column name"),
    view: str = Query("rows", pattern="^(rows|counts)$"),
    filter: str = Query("all"),
    q: str = Query("", max_length=200),
    page: int = Query(0, ge=0),
    size: int = Query(50, ge=1, le=values_mod.MAX_PAGE_SIZE),
    reveal: bool = False,
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """The real values of one column: rows side by side (tables matched on a key) or value
    counts (every table). `format=csv` returns up to 100,000 rows of the same view."""
    m = _mapping_by_id(mapping)
    live = _live_tables()
    fn = values_mod.rows if view == "rows" else values_mod.counts
    try:
        result = fn(m, lambda member: _entry(live, member.ref), _checks.get(m.id), column,
                    flt=filter, q=q, page=page, size=size, reveal=reveal,
                    limit=values_mod.CSV_MAX_ROWS if format == "csv" else None)
    except values_mod.NotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except datacheck.Skipped as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    if format == "csv":
        name = f"{m.id}_{column}_{view}.csv".replace(" ", "_")
        return Response(values_mod.as_csv(result), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return result


# ---- AI summary -------------------------------------------------------------------------

from pydantic import BaseModel  # noqa: E402

from . import ai  # noqa: E402

_ai = ai.Store(config.AI_SUMMARY_FILE)


def _ai_facts(m):
    """(fact sheet text, saved data check, saved key mapping check) for mapping `m`, from what
    the app already knows."""
    live = _live_tables()
    check = _checks.get(m.id)
    links = _keymaps.get(m.id)

    def name(member):
        e = _entry(live, member.ref)
        return f"{e['schema']}.{e['table']}" if e else f"{member.ref.schema}.{member.ref.table}"

    members = [{"kind": "old", "name": name(s) + (f" ({s.role})" if s.role else ""),
                "rows": (_entry(live, s.ref) or {}).get("rows")} for s in m.sources]
    members += [{"kind": "new", "name": name(t), "rows": (_entry(live, t.ref) or {}).get("rows")} for t in m.targets]

    primary = next((s for s in m.sources if s.role == "driving"), m.sources[0])
    comparisons = []
    try:
        comparisons = compare_table(table=primary.ref.ref).get("comparisons") or []
    except HTTPException:
        comparisons = []

    keys_by_target = []
    for t in m.targets:
        e = _entry(live, t.ref)
        if not e:
            continue
        try:
            keys = None if e["locked"] else db.table_keys(t.ref.side, e["object_id"])
        except db.TableLocked:
            keys = None
        keys_by_target.append((f"{e['schema']}.{e['table']}", keys))

    hints = []
    if check and check.get("status") not in ("skipped", "error") and comparisons:
        hints = ai.rename_hints(m, lambda member: _entry(live, member.ref), comparisons[0])

    facts = ai.build_facts(m, members, _row_check(m, live), check, comparisons, keys_by_target, hints, links=links)
    return facts, check, links


def _stale(saved):
    """A summary is stale once the data check or the key mapping check ran again after it."""
    check = _checks.get(saved["mapping"])
    links = _keymaps.get(saved["mapping"])
    return bool((check and check.get("checked_at") != saved.get("data_check_at"))
                or (links and links.get("checked_at") != saved.get("key_mapping_at")))


@app.get("/api/ai/status")
def ai_status():
    """Whether AI summaries are set up (key and model in backend/.env)."""
    return ai.status()


@app.get("/api/ai/facts")
def ai_facts(mapping: str = Query(..., description="Mapping id, e.g. crew_type")):
    """Exactly what would be sent to the AI for this mapping. Calls no AI."""
    m = _mapping_by_id(mapping)
    facts, check, _links = _ai_facts(m)
    return {"mapping": m.id, "format": "toon", "approx_tokens": ai.approx_tokens(facts),
            "data_check_at": (check or {}).get("checked_at"), "facts": facts}


@app.get("/api/ai/summary/saved")
def ai_summary_saved(mapping: str = Query(...)):
    """The saved summary of a mapping (null when none yet). Calls no AI."""
    m = _mapping_by_id(mapping)
    saved = _ai.get(m.id)
    return {"result": {**saved, "stale": _stale(saved)} if saved else None}


class SummaryRequest(BaseModel):
    mapping: str
    refresh: bool = False


@app.post("/api/ai/summary")
def ai_summary(req: SummaryRequest):
    """Write (or return the saved) plain-words summary of one mapping.

    Runs the data check first when the mapping has none yet, so the summary always rests
    on measured values."""
    m = _mapping_by_id(req.mapping)
    state = ai.status()
    if not state["enabled"]:
        raise HTTPException(status_code=409, detail=state["reason"])
    saved = _ai.get(m.id)
    if saved and not req.refresh and not _stale(saved):
        return {**saved, "stale": False}
    if m.type not in ("transform", "excluded") and not _checks.get(m.id):
        _checks.put(_run_data_check(m))
    if m.type not in ("transform", "excluded") and not _keymaps.get(m.id):
        try:
            _keymaps.put(_run_key_mapping(m))
        except Exception as exc:  # the summary can still be written from the data check
            logger.error("key mapping check of %s failed: %s", m.id, exc)
    facts, check, links = _ai_facts(m)
    try:
        answer, usage, bad, attempts = ai.summarize(facts)
    except ai.AiError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    result = ai.make_result(m.id, check, facts, answer, usage, bad, attempts, links=links)
    _ai.put(result)
    return {**result, "stale": False}

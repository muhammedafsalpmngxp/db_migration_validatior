"""Target table checks: is each target table of a mapping sound on its own?

The data check compares values with the source; this looks at the target table itself:

1. Primary key: does the target have one, and did the source? Without one nothing stops
   duplicate rows.
2. Duplicate rows: with no primary key, how many rows repeat another row exactly (every
   comparable column; identity columns are left out because the server generates them),
   next to the same count in the source table (one_to_one and merge).
3. Identity counter: will the next new row get a number that is already used?
4. Foreign keys, both directions - the ones the table holds and the ones of other tables
   that point at it: enabled and trusted, and the rows that point at a row that does not
   exist (orphans). An enabled, trusted key cannot have orphans, so it is not queried.

Read-only and read uncommitted, like the data check. Results are kept per mapping in
TABLE_CHECK_FILE. "Re-run all data checks" runs it after each mapping's other checks.

    GET /api/table-checks?mapping=...          the saved result, or a new check (refresh=true, or none yet)
    GET /api/table-checks/saved?mapping=...    the saved result without running anything
"""
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pyodbc
from fastapi import APIRouter, HTTPException, Query

from . import config, db, identity
from . import datacheck as dc

RESULT_FILE = Path(config._env("TABLE_CHECK_FILE", config.BACKEND_DIR / ".cache" / "table_checks.json"))
TARGET = config.TARGET_SIDE
NULL = "NCHAR(9216)"
SEP = "NCHAR(31)"
CHUNK = 100     # CONCAT takes at most 254 arguments (values and separators)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fmt(n):
    return f"{n:,}"


def _q(name):
    return "[" + name.replace("]", "]]") + "]"


def _text(col):
    """One value as text, written the same way whatever the server settings."""
    c, b = f"x.{_q(col['name'])}", dc._bt(col["type"])
    if b in dc.DATES:
        e = f"CONVERT(nvarchar(40), {c}, 126)"
    elif b in ("float", "real"):
        e = f"CONVERT(nvarchar(40), {c}, 3)"
    elif b in dc.BINARY:
        e = f"CONVERT(nvarchar(max), CAST({c} AS varbinary(max)), 1)"
    else:
        e = f"CONVERT(nvarchar(max), {c})"
    return f"ISNULL({e}, {NULL})"


def _duplicates(side, entry, cols):
    """Rows that repeat another row exactly: {rows, duplicate_rows, left_out}."""
    used = [c for c in cols if dc._bt(c["type"]) not in dc.NOCOMPARE and not c.get("identity")]
    left_out = [c["name"] for c in cols if c not in used]
    if not used:
        return {"rows": entry["rows"], "duplicate_rows": None, "left_out": left_out}
    hashes = []
    for i in range(0, len(used), CHUNK):
        part = [_text(c) for c in used[i:i + CHUNK]]
        body = part[0] if len(part) == 1 else "CONCAT(" + f", {SEP}, ".join(part) + ")"
        hashes.append(f"HASHBYTES('SHA2_256', {body})")
    h = hashes[0] if len(hashes) == 1 else "HASHBYTES('SHA2_256', " + " + ".join(
        f"CAST({x} AS varbinary(32))" for x in hashes) + ")"
    table = f"{_q(entry['schema'])}.{_q(entry['table'])}"
    sql = f"SELECT COUNT_BIG(*) AS n, COUNT_BIG(DISTINCT h) AS d FROM (SELECT {h} AS h FROM {table} x) q"
    with closing(db.connect(side)) as con:
        con.timeout = config.DATA_CHECK_TIMEOUT
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        try:
            n, d = cur.execute(sql).fetchone()
        except pyodbc.Error as exc:
            if db.is_lock_timeout(exc):
                raise db.TableLocked(f"{entry['schema']}.{entry['table']}") from exc
            raise
    return {"rows": int(n or 0), "duplicate_rows": int(n or 0) - int(d or 0), "left_out": left_out}


def _identity(side, entry, cols):
    """The identity counter of each identity column (app/identity.py)."""
    out = []
    names = [c["name"] for c in cols if c.get("identity")]
    if not names:
        return out
    with closing(db.connect(side)) as con:
        con.timeout = config.DATA_CHECK_TIMEOUT
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        for name in names:
            sql, params = identity.query(entry["schema"], entry["table"], name)
            try:
                cur.execute(sql, params)
            except pyodbc.Error as exc:
                if db.is_lock_timeout(exc):
                    raise db.TableLocked(f"{entry['schema']}.{entry['table']}") from exc
                raise
            row = cur.fetchone()
            if row is not None:
                out.append(identity.state(name, dict(zip([d[0] for d in cur.description], row))))
    return out


def _primary(keys):
    pk = next((k for k in keys["keys"] if k["kind"] == "primary"), None)
    return pk["columns"] if pk else None


def _one_source(m, entry_of):
    """The source table a target is compared with: one_to_one its source, merge its
    driving table; None for union and transform (several sources, no one-to-one count)."""
    if m.type == "one_to_one":
        mem = m.sources[0]
    elif m.type == "merge":
        mem = next((s for s in m.sources if s.role == "driving"), m.sources[0])
    else:
        return None, None
    return mem, entry_of(mem)


def _target(m, tmember, entry_of, findings):
    name = tmember.ref.ref.split(".", 1)[1]
    e = entry_of(tmember)
    if e is None:
        findings.append({"severity": "error", "text": f"{name} does not exist."})
        return {"table": name, "exists": False}
    if e["locked"]:
        raise db.TableLocked(name)
    keys = db.table_keys(TARGET, e["object_id"])
    pk = _primary(keys)
    out = {"table": name, "exists": True, "rows": e["rows"], "busy": bool(e.get("busy")), "primary_key": pk,
           "unique_keys": [k["columns"] for k in keys["keys"] if k["kind"] == "unique"],
           "duplicates": None, "source_duplicates": None, "foreign_keys": []}

    # 1-2. Primary key and duplicate rows (only possible without a primary key).
    smem, sentry = _one_source(m, entry_of)
    spk = None
    if sentry and not sentry["locked"]:
        spk = _primary(db.table_keys(smem.ref.side, sentry["object_id"]))
        out["source_primary_key"] = spk
    if pk is None:
        dup = _duplicates(TARGET, e, db.table_columns(TARGET, e["object_id"]))
        out["duplicates"] = dup
        sdup = None
        if sentry and not sentry["locked"]:
            sdup = ({"rows": sentry["rows"], "duplicate_rows": 0, "left_out": []} if spk
                    else _duplicates(smem.ref.side, sentry, db.table_columns(smem.ref.side, sentry["object_id"])))
            out["source_duplicates"] = sdup
        if spk:
            findings.append({"severity": "review", "text": f"{name} has no primary key, although the source table has "
                                                           f"one ({', '.join(spk)}): nothing stops duplicate rows."})
        else:
            findings.append({"severity": "info", "text": f"{name} has no primary key: nothing stops duplicate rows."})
        n = dup["duplicate_rows"]
        if n:
            if sdup is not None and sdup["duplicate_rows"] is not None and n > sdup["duplicate_rows"]:
                findings.append({"severity": "error", "text": f"{_fmt(n)} rows of {name} repeat another row exactly; "
                                                              f"the source has {_fmt(sdup['duplicate_rows'])}."})
            elif sdup is not None and sdup["duplicate_rows"] is not None:
                findings.append({"severity": "info", "text": f"{_fmt(n)} rows of {name} repeat another row exactly, "
                                                             f"as in the source ({_fmt(sdup['duplicate_rows'])})."})
            else:
                findings.append({"severity": "review", "text": f"{_fmt(n)} rows of {name} repeat another row exactly."})

    # 3. Identity counter.
    out["identity"] = _identity(TARGET, e, db.table_columns(TARGET, e["object_id"]))
    for s in out["identity"]:
        if s["behind"]:
            findings.append({"severity": "error", "text": identity.text(name, s)})

    # 4. Foreign keys in both directions.
    for f in keys["foreign_keys"]:
        child = f"{f['parent']['schema']}.{f['parent']['table']}"
        ref = f"{f['referenced']['schema']}.{f['referenced']['table']}"
        rec = {"name": f["name"], "direction": f["direction"], "child": child, "columns": f["columns"],
               "references": ref, "ref_columns": f["ref_columns"], "enabled": f["enabled"], "trusted": f["trusted"],
               "orphans": 0, "counted": False}
        what = f"{child}({', '.join(f['columns'])}) → {ref}"
        if not f["enabled"]:
            findings.append({"severity": "review", "text": f"Foreign key {what} is disabled: the link is not enforced."})
        elif not f["trusted"]:
            findings.append({"severity": "review", "text": f"Foreign key {what} is not trusted: existing rows were "
                                                           "never checked against it."})
        if not (f["enabled"] and f["trusted"]):
            try:
                rec["orphans"] = db.foreign_key_check(TARGET, f)["orphan_rows"]
                rec["counted"] = True
            except db.TableLocked:
                rec["orphans"] = None
                findings.append({"severity": "info", "text": f"Orphans of {what} not counted: {child} is locked."})
            if rec["orphans"]:
                findings.append({"severity": "error", "text": f"{_fmt(rec['orphans'])} rows of {child} point to a "
                                                              f"{ref} row that does not exist ({', '.join(f['columns'])})."})
        out["foreign_keys"].append(rec)
    return out


def check(m, entry_of):
    """The target table checks of mapping `m` (every target table)."""
    started = time.time()
    result = {"mapping": m.id, "type": m.type, "checked_at": _now(), "tables": [], "findings": []}
    if not m.targets:
        result.update(status="skipped", headline="Excluded from the migration: there is no target table to check.",
                      seconds=0)
        return result
    try:
        for tmember in m.targets:
            result["tables"].append(_target(m, tmember, entry_of, result["findings"]))
    except db.TableLocked as exc:
        result.update(status="locked", headline=f"{exc} is locked by another session (a load in progress?). "
                                                "Try again later.", seconds=round(time.time() - started, 1))
        return result
    except pyodbc.Error as exc:
        text = str(exc)
        result.update(status="timeout" if "timeout" in text.lower() else "error", headline=f"Check failed: {text}",
                      seconds=round(time.time() - started, 1))
        return result
    errors = [f["text"] for f in result["findings"] if f["severity"] == "error"]
    reviews = [f["text"] for f in result["findings"] if f["severity"] == "review"]
    if errors:
        result.update(status="problems", headline=errors[0])
    elif reviews:
        result.update(status="review", headline=reviews[0])
    else:
        result.update(status="ok", headline="Primary key, duplicate rows and every foreign key are in order.")
    result["seconds"] = round(time.time() - started, 1)
    return result


store = dc.Store(RESULT_FILE)


def check_now(m, entry_of):
    """Check one mapping and save the result (the data check run calls this)."""
    result = check(m, entry_of)
    store.put(result)
    return result


# ---- HTTP API ---------------------------------------------------------------------------------

router = APIRouter(prefix="/api/table-checks", tags=["Target table checks"])


def _app():
    from . import main as app_main
    return app_main


@router.get("")
def run(mapping: str = Query(..., description="Mapping id, e.g. task_daily"), refresh: bool = False):
    """The target table checks of one mapping: the saved result, or a new check when asked
    (or when there is none yet)."""
    app_main = _app()
    m = app_main._mapping_by_id(mapping)
    saved = store.get(m.id)
    if saved and not refresh:
        return saved
    live = app_main._live_tables()
    return check_now(m, lambda member: app_main._entry(live, member.ref))


@router.get("/saved")
def saved(mapping: str = Query(...)):
    """The saved result of one mapping without running anything (null when never checked)."""
    m = _app()._mapping_by_id(mapping)
    if m is None:
        raise HTTPException(status_code=404, detail=f"No mapping {mapping!r}.")
    return {"result": store.get(m.id)}

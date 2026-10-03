"""Coverage: the tables of each database (A, B and T) that no mapping of the plan mentions.

The plan says which tables are migrated; this lists every other user table, so nothing is
left out without anyone noticing - a source table nobody mapped, or a target table nobody
checks. Catalog views only (names and row counts), read-only and read uncommitted like the
rest of the app. Tables matching COVERAGE_IGNORE (comma-separated patterns on schema.table,
`*` and `?` as wildcards, e.g. dbo.awsdms_*) are counted but not listed.

    GET /api/coverage    every database: its tables, how many the plan covers, the others
"""
import fnmatch
import time
from contextlib import closing

import pyodbc
from fastapi import APIRouter

from . import config, db

IGNORE = [p.strip().lower() for p in str(config._env("COVERAGE_IGNORE", "")).split(",") if p.strip()]

TABLES_SQL = """
SELECT s.name AS [schema], t.name AS [table],
       (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS [rows]
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
"""
# Without the row counts: used when another session holds the row count metadata.
NAMES_SQL = """
SELECT s.name AS [schema], t.name AS [table], CAST(NULL AS bigint) AS [rows]
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
"""

router = APIRouter(prefix="/api/coverage", tags=["Coverage"])


def _tables(side):
    """Every user table of one database: [{schema, table, rows}] (rows None when the row
    counts are held by another session)."""
    with closing(db.connect(side)) as con:
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        for sql in (TABLES_SQL, NAMES_SQL):
            try:
                cur.execute(sql)
            except pyodbc.Error as exc:
                if db.is_lock_timeout(exc) and sql is TABLES_SQL:
                    continue
                raise
            names = [d[0] for d in cur.description]
            return [dict(zip(names, r)) for r in cur.fetchall()]
    return []


def _ignored(schema, table):
    name = f"{schema}.{table}".lower()
    return any(fnmatch.fnmatchcase(name, p) for p in IGNORE)


def report(plan):
    """Coverage of every database of the app, from the live catalogs and the plan."""
    started = time.time()
    in_plan = {}
    for m in plan.mappings:
        for member in m.sources + m.targets:
            in_plan.setdefault(member.ref.side, set()).add((member.ref.schema.lower(), member.ref.table.lower()))
    out = []
    for side, d in config.DATABASES.items():
        entry = {"side": side, "name": d["name"], "label": d["label"], "role": d["role"],
                 "tables": None, "in_plan": None, "ignored": None, "not_in_plan": [], "error": None}
        try:
            tables = _tables(side)
        except pyodbc.Error as exc:
            entry["error"] = ("A table is locked by another session, so the table list cannot be read right now."
                              if db.is_lock_timeout(exc) else str(exc))
            out.append(entry)
            continue
        mine = in_plan.get(side, set())
        covered = [t for t in tables if (t["schema"].lower(), t["table"].lower()) in mine]
        others = [t for t in tables if (t["schema"].lower(), t["table"].lower()) not in mine]
        ignored = [t for t in others if _ignored(t["schema"], t["table"])]
        listed = [t for t in others if not _ignored(t["schema"], t["table"])]
        entry.update(tables=len(tables), in_plan=len(covered), ignored=len(ignored),
                     not_in_plan=sorted(({"schema": t["schema"], "table": t["table"],
                                          "rows": None if t["rows"] is None else int(t["rows"])} for t in listed),
                                        key=lambda t: (t["schema"].lower(), t["table"].lower())))
        out.append(entry)
    return {"databases": out, "ignore": IGNORE, "seconds": round(time.time() - started, 2)}


@router.get("")
def coverage():
    """Every database of the app: how many of its tables the plan covers, and the others."""
    from . import main as app_main
    return report(app_main.get_plan())

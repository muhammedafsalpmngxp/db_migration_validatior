"""Read-only access to the three databases: table facts, row counts, column definitions.

Catalog queries are parameterised; the one query that reads user data, `exact_count`,
only ever names a table taken from the live catalog. Every connection sets a short
LOCK_TIMEOUT, so a table held by a running load is reported as locked, not waited on.
"""
import threading
import time
from contextlib import closing

import pyodbc

from . import config

# Tables are looked up by name, never by scanning the catalog. A table that is being loaded
# holds a schema lock, and any query touching its catalog row - sys.tables, sys.objects,
# INFORMATION_SCHEMA, even under READ UNCOMMITTED - waits until the load finishes.
# Resolving names with a short LOCK_TIMEOUT lets every other table through and reports the
# busy one as locked instead of hanging the whole request.
RESOLVE_SQL = "SELECT v.i, OBJECT_ID(v.n) FROM (VALUES {values}) AS v(i, n)"

# Row counts come from sys.partitions (heap or clustered index), not COUNT(*): one of the
# source tables holds ~73 million rows. Size is the pages in use across all indexes, in KB.
STATS_SQL = """
SELECT t.object_id, s.name AS [schema], t.name AS [table], t.create_date AS created,
       (SELECT SUM(p.rows) FROM sys.partitions p
         WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS [rows],
       (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = t.object_id) AS [columns],
       (SELECT SUM(a.used_pages) * 8 FROM sys.partitions p
          JOIN sys.allocation_units a ON a.container_id = p.partition_id
         WHERE p.object_id = t.object_id) AS size_kb
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.object_id IN ({ids})
"""

COLUMNS_SQL = """
SELECT c.column_id AS [position], c.name AS [name], ty.name AS [type],
       c.max_length, c.precision, c.scale, c.is_nullable, c.is_identity,
       CASE WHEN EXISTS (
           SELECT 1 FROM sys.index_columns ic
           JOIN sys.indexes i ON i.object_id = ic.object_id AND i.index_id = ic.index_id
           WHERE i.is_primary_key = 1 AND ic.object_id = c.object_id AND ic.column_id = c.column_id
       ) THEN 1 ELSE 0 END AS is_pk
FROM sys.columns c
JOIN sys.types ty ON ty.user_type_id = c.user_type_id
WHERE c.object_id = ?
ORDER BY c.column_id
"""

LOCKED = object()


class TableLocked(Exception):
    """The table is held by another session, typically a load in progress."""


def is_lock_timeout(exc):
    text = str(exc)
    return "1222" in text or "Lock request time out" in text


def connect(side):
    con = pyodbc.connect(
        config.dsn(config.DATABASES[side]["name"]),
        timeout=config.MSSQL_LOGIN_TIMEOUT,
        readonly=True,
        autocommit=True,
    )
    con.timeout = config.MSSQL_QUERY_TIMEOUT
    con.cursor().execute(f"SET LOCK_TIMEOUT {int(config.MSSQL_LOCK_TIMEOUT_MS)}")
    return con


def query(side, sql, params=()):
    with closing(connect(side)) as con:
        cur = con.cursor()
        cur.execute(sql, params)
        names = [d[0] for d in cur.description]
        return [dict(zip(names, row)) for row in cur.fetchall()]


def ping(side):
    return query(side, "SELECT DB_NAME() AS db")[0]


def _quote(name):
    return "[" + name.replace("]", "]]") + "]"


def _resolve(cur, names):
    """OBJECT_ID of each (schema, table): an int, None when it does not exist, or LOCKED."""
    values = ", ".join("(?, ?)" for _ in names)
    params = [p for i, (s, t) in enumerate(names) for p in (i, f"{_quote(s)}.{_quote(t)}")]
    try:
        rows = cur.execute(RESOLVE_SQL.format(values=values), params).fetchall()
        return {names[i]: oid for i, oid in rows}
    except pyodbc.Error as exc:
        if not is_lock_timeout(exc):
            raise
    # Something in the batch is locked: find out which, one name at a time.
    out = {}
    for s, t in names:
        try:
            out[(s, t)] = cur.execute("SELECT OBJECT_ID(?)", f"{_quote(s)}.{_quote(t)}").fetchone()[0]
        except pyodbc.Error as exc:
            if not is_lock_timeout(exc):
                raise
            out[(s, t)] = LOCKED
    return out


def _stats(cur, ids):
    """STATS_SQL rows keyed by object_id; one id at a time if the batch is blocked."""
    def run(chunk):
        cur.execute(STATS_SQL.format(ids=", ".join(str(int(i)) for i in chunk)))
        names = [d[0] for d in cur.description]
        return {r[0]: dict(zip(names, r)) for r in cur.fetchall()}

    try:
        return run(ids)
    except pyodbc.Error as exc:
        if not is_lock_timeout(exc):
            raise
    out = {}
    for i in ids:
        try:
            out.update(run([i]))
        except pyodbc.Error as exc:
            if not is_lock_timeout(exc):
                raise
            out[i] = LOCKED
    return out


_cache = {}
_cache_lock = threading.Lock()


def describe_tables(side, names, refresh=False):
    """Live facts about the named tables of one database.

    `names` is a list of (schema, table). Returns {(schema.lower(), table.lower()): entry}
    with the real schema/table names, object_id, rows, columns, size_kb and created - or
    `locked: True` when another session holds the table. Missing tables are left out.
    """
    names = sorted(set(names), key=lambda n: (n[0].lower(), n[1].lower()))
    wanted = frozenset((s.lower(), t.lower()) for s, t in names)
    with _cache_lock:
        hit = _cache.get(side)
        if (hit and not refresh and hit[1] == wanted
                and time.time() - hit[0] < config.TABLE_CACHE_SECONDS):
            return hit[2]

    read_at = time.time()
    result = {}
    if names:
        with closing(connect(side)) as con:
            cur = con.cursor()
            ids = _resolve(cur, names)
            found = [oid for oid in ids.values() if oid is not None and oid is not LOCKED]
            stats = _stats(cur, found) if found else {}
        for (s, t), oid in ids.items():
            if oid is None:
                continue
            key = (s.lower(), t.lower())
            row = LOCKED if oid is LOCKED else stats.get(oid, LOCKED)
            if row is LOCKED:
                result[key] = {"schema": s, "table": t, "object_id": None, "locked": True,
                               "rows": None, "columns": None, "size_kb": None, "created": None}
                continue
            result[key] = {
                "schema": row["schema"],
                "table": row["table"],
                "object_id": row["object_id"],
                "locked": False,
                "rows": int(row["rows"] or 0),
                "columns": row["columns"],
                "size_kb": int(row["size_kb"] or 0),
                "created": row["created"].isoformat(timespec="seconds") if row["created"] else None,
            }
    with _cache_lock:
        _cache[side] = (read_at, wanted, result)
    return result


def read_at(side):
    """When the cached tables of `side` were read (epoch seconds), or None."""
    hit = _cache.get(side)
    return hit[0] if hit else None


def exact_count(side, schema, table):
    """COUNT_BIG(*) of one table, read uncommitted so ordinary writes do not block it.

    `schema` and `table` must come from the live catalog (`describe_tables`), never from
    input. Raises TableLocked when a load holds the table.
    """
    started = time.time()
    with closing(connect(side)) as con:
        con.timeout = config.MSSQL_COUNT_TIMEOUT
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        try:
            cur.execute(f"SELECT COUNT_BIG(*) FROM {_quote(schema)}.{_quote(table)}")
        except pyodbc.Error as exc:
            if is_lock_timeout(exc):
                raise TableLocked(f"{schema}.{table}") from exc
            raise
        rows = int(cur.fetchone()[0])
    return rows, round(time.time() - started, 2)


def render_type(col):
    """nvarchar(50), decimal(18,2), datetime2(7), varchar(max) ... as SQL Server shows them."""
    name = col["type"].lower()
    length = col["max_length"]
    if name in ("nvarchar", "nchar"):
        return f"{name}({'max' if length == -1 else length // 2})"
    if name in ("varchar", "char", "varbinary", "binary"):
        return f"{name}({'max' if length == -1 else length})"
    if name in ("decimal", "numeric"):
        return f"{name}({col['precision']},{col['scale']})"
    if name in ("datetime2", "time", "datetimeoffset"):
        return f"{name}({col['scale']})"
    return name


def table_columns(side, object_id):
    """Column definitions of one table. Raises TableLocked when a load holds the table."""
    try:
        rows = query(side, COLUMNS_SQL, (object_id,))
    except pyodbc.Error as exc:
        if is_lock_timeout(exc):
            raise TableLocked(str(object_id)) from exc
        raise
    return [
        {
            "position": r["position"],
            "name": r["name"],
            "type": render_type(r),
            "nullable": bool(r["is_nullable"]),
            "identity": bool(r["is_identity"]),
            "pk": bool(r["is_pk"]),
        }
        for r in rows
    ]

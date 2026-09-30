"""Every user table of one database: its columns, row key and row count, read from the
catalog views. Nothing is configured per table - whatever the database holds is listed.
"""
import threading
import time
from contextlib import closing

from .. import db as app_db
from . import conn, settings

INFO_SQL = """
SELECT DB_NAME() AS db,
       CAST(SERVERPROPERTY('ProductMajorVersion') AS int) AS major,
       CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)) AS version,
       CAST(SERVERPROPERTY('Edition') AS nvarchar(200)) AS edition,
       CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(200)) AS collation
"""

# Row counts from sys.partitions (heap or clustered index): instant on any size of table.
TABLES_SQL = """
SELECT t.object_id, s.name AS [schema], t.name AS [table], t.create_date AS created,
       (SELECT SUM(p.rows) FROM sys.partitions p
         WHERE p.object_id = t.object_id AND p.index_id IN (0, 1)) AS [rows]
FROM sys.tables t
JOIN sys.schemas s ON s.schema_id = t.schema_id
WHERE t.is_ms_shipped = 0
"""

COLUMNS_SQL = """
SELECT c.object_id, c.column_id AS position, c.name, TYPE_NAME(c.user_type_id) AS [type],
       TYPE_NAME(c.system_type_id) AS base, c.max_length, c.precision, c.scale,
       c.is_nullable, c.is_identity, c.is_computed, c.collation_name
FROM sys.columns c
JOIN sys.tables t ON t.object_id = c.object_id
WHERE t.is_ms_shipped = 0
ORDER BY c.object_id, c.column_id
"""

# The primary key, else a unique index on columns that are never NULL: what identifies a row.
KEYS_SQL = """
SELECT i.object_id, i.index_id, i.is_primary_key, c.name, ic.key_ordinal, c.is_nullable
FROM sys.indexes i
JOIN sys.tables t ON t.object_id = i.object_id
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE t.is_ms_shipped = 0 AND i.is_unique = 1 AND i.has_filter = 0 AND ic.key_ordinal > 0
ORDER BY i.object_id, i.is_primary_key DESC, i.index_id, ic.key_ordinal
"""


def _type_text(col):
    """nvarchar(50), decimal(18,2), datetime2(7) ... as SQL Server shows them."""
    return app_db.render_type({"type": col["type"], "max_length": col["max_length"],
                               "precision": col["precision"], "scale": col["scale"]})


def _read(server, database):
    started = time.time()
    with closing(conn.connect(server, database, timeout=max(60, server.login_timeout))) as con:
        cur = con.cursor()
        info = conn.fetch(cur, INFO_SQL)[0]
        tables = conn.fetch(cur, TABLES_SQL)
        columns = conn.fetch(cur, COLUMNS_SQL)
        keys = conn.fetch(cur, KEYS_SQL)

    cols_of = {}
    for c in columns:
        cols_of.setdefault(c["object_id"], []).append({
            "position": c["position"],
            "name": c["name"],
            "type": _type_text(c),
            "base": (c["base"] or c["type"] or "").lower(),
            "max_length": c["max_length"],
            "nullable": bool(c["is_nullable"]),
            "identity": bool(c["is_identity"]),
            "computed": bool(c["is_computed"]),
            "collation": c["collation_name"],
        })

    # First usable index per table: the primary key comes first, then any unique index
    # whose columns are all NOT NULL.
    indexes = {}
    for k in keys:
        indexes.setdefault(k["object_id"], {}).setdefault(k["index_id"], {"primary": bool(k["is_primary_key"]),
                                                                           "columns": [], "nullable": False})
        ix = indexes[k["object_id"]][k["index_id"]]
        ix["columns"].append(k["name"])
        ix["nullable"] = ix["nullable"] or bool(k["is_nullable"])
    key_of = {}
    for oid, ixs in indexes.items():
        usable = [ix for ix in ixs.values() if ix["primary"] or not ix["nullable"]]
        if usable:
            best = usable[0]
            key_of[oid] = {"kind": "primary key" if best["primary"] else "unique index", "columns": best["columns"]}

    out = {}
    for t in tables:
        key = f"{t['schema']}.{t['table']}".lower()
        out[key] = {
            "key": key,
            "schema": t["schema"],
            "table": t["table"],
            "object_id": t["object_id"],
            "rows": int(t["rows"] or 0),
            "created": t["created"].isoformat(timespec="seconds") if t["created"] else None,
            "columns": cols_of.get(t["object_id"], []),
            "row_key": key_of.get(t["object_id"]),
        }
    return {
        "database": info["db"],
        "server": {"major": info["major"], "version": info["version"], "edition": info["edition"],
                   "collation": info["collation"]},
        "tables": out,
        "read_at": time.time(),
        "seconds": round(time.time() - started, 2),
    }


_cache = {}
_locks = {}
_guard = threading.Lock()

# Row counts seen at each fresh read, per table: a table whose count keeps moving is live
# (still being written to, or still being copied into), so its check can only be a snapshot.
_history = {}
HISTORY_KEEP = 60
LIVE_WINDOW = 3600      # seconds: a change within the last hour counts


def _remember(server, database, result):
    at = result["read_at"]
    for key, t in result["tables"].items():
        h = _history.setdefault((server.key, database.lower(), key), [])
        if not h or h[-1][1] != t["rows"] or at - h[-1][0] > 300:
            h.append((at, t["rows"]))
            del h[:-HISTORY_KEEP]


def movement(server, database, key):
    """How the row count of a table moved during the last hour of reads - {rows_then,
    rows_now, change, minutes} - or None when it did not move (or was read only once)."""
    h = _history.get((server.key, database.lower(), key)) or []
    if len(h) < 2:
        return None
    now_at, now_rows = h[-1]
    recent = [x for x in h if now_at - x[0] <= LIVE_WINDOW]
    then_at, then_rows = recent[0]
    if then_rows == now_rows and all(r == now_rows for _, r in recent):
        return None
    return {"rows_then": then_rows, "rows_now": now_rows, "change": now_rows - then_rows,
            "minutes": max(1, round((now_at - then_at) / 60))}


def read(server, database, refresh=False):
    """The catalog of one database, reused for ATNM_CACHE_SECONDS. Raises the connection
    error (conn.NotConfigured, conn.Locked or pyodbc.Error) when it cannot be read."""
    key = (server.key, database.lower())
    with _guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock:   # one read per database at a time; others wait and reuse it
        hit = _cache.get(key)
        if hit and not refresh and time.time() - hit["read_at"] < settings.CACHE_SECONDS:
            return hit
        result = _read(server, database)
        _cache[key] = result
        _remember(server, database, result)
        return result

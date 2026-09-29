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


# ---- Keys: primary / unique keys and foreign keys ------------------------------------

KEYS_SQL = """
SELECT i.name, i.is_primary_key, i.type_desc, c.name AS [column]
FROM sys.indexes i
JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
WHERE i.object_id = ? AND (i.is_primary_key = 1 OR i.is_unique_constraint = 1) AND ic.key_ordinal > 0
ORDER BY i.is_primary_key DESC, i.name, ic.key_ordinal
"""

# Both directions: keys this table holds (parent = this table) and keys pointing at it.
# Row counts of both ends come from sys.partitions, like everywhere else.
FOREIGN_KEYS_SQL = """
SELECT fk.name, fk.parent_object_id, fk.referenced_object_id,
       fk.is_disabled, fk.is_not_trusted,
       fk.delete_referential_action_desc AS on_delete,
       fk.update_referential_action_desc AS on_update,
       OBJECT_SCHEMA_NAME(fk.parent_object_id) AS parent_schema,
       OBJECT_NAME(fk.parent_object_id) AS parent_table,
       OBJECT_SCHEMA_NAME(fk.referenced_object_id) AS ref_schema,
       OBJECT_NAME(fk.referenced_object_id) AS ref_table,
       pc.name AS parent_column, rc.name AS ref_column,
       (SELECT SUM(p.rows) FROM sys.partitions p
         WHERE p.object_id = fk.parent_object_id AND p.index_id IN (0, 1)) AS parent_rows,
       (SELECT SUM(p.rows) FROM sys.partitions p
         WHERE p.object_id = fk.referenced_object_id AND p.index_id IN (0, 1)) AS ref_rows
FROM sys.foreign_keys fk
JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
WHERE fk.parent_object_id = ? OR fk.referenced_object_id = ?
ORDER BY fk.name, fkc.constraint_column_id
"""


def table_keys(side, object_id):
    """Primary/unique keys and foreign keys (outgoing and incoming) of one table.

    Raises TableLocked when a load holds the table or a table it is linked to.
    """
    try:
        key_rows = query(side, KEYS_SQL, (object_id,))
        fk_rows = query(side, FOREIGN_KEYS_SQL, (object_id, object_id))
    except pyodbc.Error as exc:
        if is_lock_timeout(exc):
            raise TableLocked(str(object_id)) from exc
        raise

    keys = {}
    for r in key_rows:
        k = keys.setdefault(r["name"], {
            "name": r["name"],
            "kind": "primary" if r["is_primary_key"] else "unique",
            "index": r["type_desc"].lower(),
            "columns": [],
        })
        k["columns"].append(r["column"])

    fks = {}
    for r in fk_rows:
        f = fks.setdefault(r["name"], {
            "name": r["name"],
            "direction": "outgoing" if r["parent_object_id"] == object_id else "incoming",
            "parent": {"schema": r["parent_schema"], "table": r["parent_table"],
                       "rows": int(r["parent_rows"] or 0)},
            "referenced": {"schema": r["ref_schema"], "table": r["ref_table"],
                           "rows": int(r["ref_rows"] or 0)},
            "columns": [],
            "ref_columns": [],
            "enabled": not r["is_disabled"],
            "trusted": not r["is_not_trusted"],
            "on_delete": r["on_delete"].lower().replace("_", " "),
            "on_update": r["on_update"].lower().replace("_", " "),
        })
        f["columns"].append(r["parent_column"])
        f["ref_columns"].append(r["ref_column"])

    return {"keys": list(keys.values()), "foreign_keys": list(fks.values())}


def foreign_key_check(side, fk):
    """How the child table's key values relate to the referenced table, counted exactly.

    `fk` is one entry from `table_keys` (names from the live catalog). Returns child rows,
    rows with the key filled in, distinct key values used, and orphans - filled-in values
    with no matching row in the referenced table (0 unless the key is disabled/untrusted).
    """
    child = f"{_quote(fk['parent']['schema'])}.{_quote(fk['parent']['table'])}"
    parent = f"{_quote(fk['referenced']['schema'])}.{_quote(fk['referenced']['table'])}"
    cols = list(zip(fk["columns"], fk["ref_columns"]))
    filled = " AND ".join(f"c.{_quote(c)} IS NOT NULL" for c, _ in cols)
    join = " AND ".join(f"p.{_quote(r)} = c.{_quote(c)}" for c, r in cols)
    distinct = (f"COUNT_BIG(DISTINCT c.{_quote(cols[0][0])})" if len(cols) == 1
                else "CAST(NULL AS BIGINT)")
    sql = f"""
        SELECT COUNT_BIG(*) AS child_rows,
               SUM(CASE WHEN {filled} THEN 1 ELSE 0 END) AS filled_rows,
               {distinct} AS distinct_values,
               SUM(CASE WHEN {filled} AND p.{_quote(cols[0][1])} IS NULL
                        THEN 1 ELSE 0 END) AS orphan_rows
        FROM {child} c
        LEFT JOIN {parent} p ON {join}
    """
    # A foreign key always references a primary or unique key, so the join matches at
    # most one parent row and cannot inflate the child counts.
    started = time.time()
    with closing(connect(side)) as con:
        con.timeout = config.MSSQL_COUNT_TIMEOUT
        cur = con.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        try:
            row = cur.execute(sql).fetchone()
        except pyodbc.Error as exc:
            if is_lock_timeout(exc):
                raise TableLocked(child) from exc
            raise
    child_rows, filled_rows, distinct_values, orphan_rows = (
        None if v is None else int(v) for v in row
    )
    return {
        "child_rows": child_rows,
        "filled_rows": filled_rows or 0,
        "null_rows": child_rows - (filled_rows or 0),
        "distinct_values": distinct_values,
        "orphan_rows": orphan_rows or 0,
        "seconds": round(time.time() - started, 2),
    }
